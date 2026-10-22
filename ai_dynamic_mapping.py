"""
AI-Assisted Dynamic Financial Data Mapping (this phase) - a third
advisory layer, alongside ai_validation.py (judges an existing mapping)
and ai_correction_proposals.py (proposes an alternative to an existing
mapping). This module handles the case neither of those two can: a
field the deterministic pipeline's alias list never matched AT ALL
(validation_status == "NOT_FOUND") - e.g. a report saying "Turnover"
where the pipeline's aliases only know "Revenue from Operations".

Ollama's role here is strictly semantic ROW SELECTION, never value
extraction: it is given a small, deterministically ranked shortlist of
candidate OCR rows (never the whole section, never the whole document)
and asked ONLY to say WHICH of those rows (if any) is this field - it
is never asked for, and cannot express, any number. A real live test
against this document proved why this boundary matters: llama3.2:latest
correctly identified the right row for "Capex" out of its shortlist, but
then hallucinated the row's own printed value (139, when the row
actually prints 547/1,057) - a small model is good enough at semantic
row-matching, but must never be trusted to transcribe a number. Once a
row selection is verified (see below), Python reads that row's actual
numbers and assigns them to fiscal years using the EXACT SAME
deterministic parser (ocr_pipeline.extract_number_tokens/select_fy_values)
the non-AI exact/alias-match pipeline already uses for every other
field - not a reimplementation, the same function, called a second time.

Architectural boundary (identical in spirit to the other two AI layers):
  - ocr_pipeline.py is never modified by this module. It is called a
    second time for its existing PUBLIC value-parsing functions
    (ALIASES, clean_number, extract_number_tokens, select_fy_values,
    select_fy_values_net_worth) - this is deliberate: once a row
    selection is verified, values are read using the EXACT SAME parser
    the non-AI exact/alias-match pipeline uses, not a reimplementation.
    One private helper, _with_layout_offsets, is also reused (it has no
    public equivalent and is the only thing that correctly remaps a
    token's position onto the real layout-preserving line for column-
    position comparison) - the same narrow, documented private-helper
    reuse pattern already established for ai_validation.py's own
    internals (see ai_correction_proposals.py's docstring), extended
    here to this one ocr_pipeline.py helper for this one purpose.
  - This module NEVER mutates the `extracted` dict it is given - it
    returns a NEW dict. A field is only ever changed away from
    NOT_FOUND when every deterministic verification check below passes.
  - A newly-populated field is marked with the distinct
    `validation_status = "AI_MAPPED"` (never "OK") so it stays visibly
    different from a genuine deterministic match everywhere downstream
    (the existing AI validator, the Data Review page, the reviewed-
    dataset layer) - and, per explicit product decision, is ALWAYS
    surfaced for human Accept/Reject/Keep-as-Not-Found on the Data
    Review page, regardless of what the (separate, unmodified) AI
    validator later says about it.
  - A field that fails verification, has no plausible candidate, or
    hits INSUFFICIENT_CONTEXT keeps `validation_status == "NOT_FOUND"`
    exactly as before and falls through unchanged to the existing
    presence-check (ai_validation.py's own NOT_FOUND path) - this
    module only ever adds a chance to resolve a field, never removes
    the existing safety net for whatever it can't resolve.

CONTENT STATUS VOCABULARY (mapping_status - set only when a real
attempt was made and its response parsed):
  MAPPED_AND_VERIFIED  - a row was selected, verified as one of the
                        candidates actually shown, AND the deterministic
                        parser confidently read this field's fiscal-year
                        values directly off that row
  MAPPED_BUT_REJECTED  - the model selected a row, but it wasn't among
                        the candidates shown for this field (hallucinated
                        line), or a duplicate claim within the same batch
  NO_CANDIDATE_FOUND    - the model looked at the candidates and found
                        none of them plausible
  INSUFFICIENT_CONTEXT  - no section/candidates existed to search, too
                        many candidates existed to search safely, the
                        model itself couldn't decide among several
                        plausible candidates, or a unit mismatch was
                        detected on the selected row (see below)
  AI_ROW_SELECTED_VALUES_UNRESOLVED - the model selected a real,
                        verified candidate row, but this row's OWN
                        numbers could not be confidently attributed to a
                        fiscal year by the deterministic parser (e.g.
                        genuinely ambiguous column alignment, or no
                        numeric tokens at all on that exact row) - a row
                        was correctly found; a value simply could not be
                        safely read from it. Never guessed past.

TECHNICAL STATUS VOCABULARY: reused verbatim from ai_validation.py
(AI_COMPLETED/AI_SKIPPED/AI_UNAVAILABLE/AI_ERROR) - "time budget
exhausted" is AI_SKIPPED with a descriptive explanation, the same
convention already used throughout this codebase.

UNITS/SCALING: this module NEVER auto-rescales a value. If a matched
row's own text mentions a unit (lakh/thousand/million) different from
this codebase's uniform crore assumption, the field is downgraded to
INSUFFICIENT_CONTEXT with an explicit note - flagged for a human,
never silently converted (auto-converting would itself be "inventing"
a number from an assumption).
"""

import difflib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import ai_validation
import ocr_pipeline
from ai_validation import (
    AI_COMPLETED, AI_ERROR, AI_SKIPPED, AI_UNAVAILABLE,
    OllamaAvailability, OllamaCallError,
    DEFAULT_MODEL, OLLAMA_BASE_URL,
    call_ollama, check_ollama_availability,
)

# ============================================================
# CONFIGURATION - deliberately separate from both ai_validation.py's and
# ai_correction_proposals.py's own budgets, so this phase adds a
# bounded, known amount of latency rather than silently competing with
# either existing budget.
# ============================================================

# Calibrated against the real Tata Chemicals document: its largest
# genuine statement sections produce up to ~45 candidate rows (Balance
# Sheet 42, Cash Flow 45) - a section that size is normal, not a
# section-detection failure. 60 gives real sections comfortable margin
# while still guarding against a genuinely pathological/mis-detected
# section (hundreds of lines) - see _field_mapping_context's
# INSUFFICIENT_CONTEXT fallback above that cap.
MAX_CANDIDATE_ROWS = int(os.environ.get("AI_MAPPING_MAX_CANDIDATE_ROWS", "60"))
DYNAMIC_MAPPING_MAX_FIELDS_PER_BATCH = int(os.environ.get("AI_MAPPING_MAX_FIELDS_PER_BATCH", "2"))
DYNAMIC_MAPPING_CALL_TIMEOUT = int(os.environ.get("AI_MAPPING_CALL_TIMEOUT", "120"))
DYNAMIC_MAPPING_TIME_BUDGET_SECONDS = float(os.environ.get("AI_MAPPING_TIME_BUDGET_SECONDS", "120"))

# A CPU-bound small model does noticeably worse (both slower and less
# accurate) when handed a large, unranked candidate list - narrow to a
# small, deterministically-ranked shortlist before the prompt is ever
# built. MAX_CANDIDATE_ROWS above still guards discovery itself against
# a pathological/mis-detected section; this cap is a second, separate
# narrowing step applied only to what's actually sent to the model.
TOP_K_CANDIDATES = int(os.environ.get("AI_MAPPING_TOP_K_CANDIDATES", "8"))

AI_MAPPED = "AI_MAPPED"

MAPPING_STATUSES = frozenset({
    "MAPPED_AND_VERIFIED", "MAPPED_BUT_REJECTED", "NO_CANDIDATE_FOUND", "INSUFFICIENT_CONTEXT",
    "AI_ROW_SELECTED_VALUES_UNRESOLVED",
})
_MODEL_STATUSES = frozenset({"CANDIDATE_FOUND", "NO_CANDIDATE_FOUND", "INSUFFICIENT_CONTEXT"})

_UNIT_MISMATCH_KEYWORDS = ("lakh", "lakhs", "in '000", "in thousands", "in million", "in millions")


@dataclass(frozen=True)
class AIFieldMappingResult:
    field: str
    technical_status: str
    mapping_status: Optional[str]
    confidence: Optional[float]
    reason: str

    matched_source_line_number: Optional[int] = None
    matched_source_text: Optional[str] = None

    # These four are NEVER supplied by Ollama - Ollama's response
    # contains only a selected_line_number (see _MAPPING_INSTRUCTIONS).
    # Once that line is verified as one of the candidates actually shown,
    # Python deterministically reads its numbers via
    # ocr_pipeline.extract_number_tokens()/select_fy_values() (the same
    # parser the non-AI exact/alias-match pipeline uses) and assigns
    # these four fields directly - the labels are just this document's
    # own resolved target fiscal-year labels, and the values are exactly
    # what that row's own text contains. Populated only when
    # mapping_status == MAPPED_AND_VERIFIED.
    matched_column_current_label: Optional[str] = None
    matched_column_previous_label: Optional[str] = None
    matched_value_current: Optional[object] = None
    matched_value_previous: Optional[object] = None

    # Supporting line-number citations only (never values) - optional,
    # informational, verified only for membership in the shown
    # candidates; never blocks a verified row selection on its own.
    evidence_line_numbers: tuple = ()
    evidence_grounded: Optional[bool] = None

    # Candidate-ranking transparency: candidate_count is the total
    # number of candidate rows discovered in this field's section (before
    # ranking); shortlisted_count is how many of those were actually
    # ranked highly enough to be sent to Ollama; candidate_rankings is
    # the FULL ranked list (score + reason per candidate, highest first)
    # so a demo/reviewer can see why the shortlist was chosen - and, if
    # the outcome is NO_CANDIDATE_FOUND, whether the true row was simply
    # ranked too low to be shown rather than genuinely absent.
    candidate_count: int = 0
    shortlisted_count: int = 0
    candidate_rankings: tuple = ()


def _make_result(field, technical_status, mapping_status, confidence, reason, **overrides):
    kwargs = dict(
        field=field, technical_status=technical_status, mapping_status=mapping_status,
        confidence=confidence, reason=reason,
    )
    kwargs.update(overrides)
    return AIFieldMappingResult(**kwargs)


# ============================================================
# CANDIDATE ROW DISCOVERY (deterministic, bounded)
# ============================================================

def _claimed_line_numbers(extracted):
    claimed = set()
    for field_data in extracted.values():
        line_number = field_data.get("source_line_number")
        if line_number is not None:
            claimed.add(line_number)
    return claimed


def _discover_candidates(section, layout_lines, claimed_lines):
    """Returns a list of candidate rows, or None if there is no section
    at all to search (nothing this module can do for this field)."""
    if section is None:
        return None
    candidates = []
    for idx in range(section["start"], section["end"]):
        if idx in claimed_lines:
            continue
        raw_values = ai_validation._raw_values_on_line(layout_lines[idx])
        if not raw_values:
            continue
        text = ai_validation._clean(layout_lines[idx])
        if not text:
            continue
        candidates.append({"line_number": idx, "text": text, "raw_values": raw_values})
    return candidates


def _detect_unit_mismatch(row_text):
    lowered = row_text.lower()
    for kw in _UNIT_MISMATCH_KEYWORDS:
        if kw in lowered:
            return kw
    return None


# ============================================================
# CANDIDATE RANKING (deterministic, explainable, stdlib-only - no
# embeddings/new AI dependency: a small CPU-bound model performs
# noticeably worse, both in latency and in semantic-selection accuracy,
# when handed a large unranked candidate list, so this narrows what it
# is shown down to a small, ranked Top-K shortlist BEFORE the prompt is
# built. Statement/section relevance is already satisfied structurally
# - candidates are pre-scoped to the field's own detected section by
# _discover_candidates() before ranking ever runs.
# ============================================================

_RANKING_STOPWORDS = frozenset({"of", "the", "and", "for", "to", "in", "on", "a", "an", "is", "are"})


def _tokenize_for_ranking(text):
    return {t for t in re.findall(r"[a-z]+", text.lower()) if t not in _RANKING_STOPWORDS and len(t) > 2}


def _normalize_for_ranking(text):
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower())


def _rank_candidates(field, candidates):
    """Pure, deterministic, directly unit-testable without Ollama.
    Scores each candidate against the field's own name + its existing
    ocr_pipeline.ALIASES (no new synonym dictionary, no company-specific
    lists - reuses exactly the vocabulary already consulted for the
    prompt itself). Returns a NEW list, sorted by score descending, each
    candidate dict augmented with 'score' (float) and 'reason' (str)."""
    field_terms = [field] + list(ocr_pipeline.ALIASES.get(field, []))
    field_tokens = set()
    for term in field_terms:
        field_tokens |= _tokenize_for_ranking(term)
    normalized_terms = [(term, _normalize_for_ranking(term)) for term in field_terms]

    ranked = []
    for c in candidates:
        row_norm = _normalize_for_ranking(c["text"])
        row_tokens = _tokenize_for_ranking(c["text"])

        best_ratio, best_term = 0.0, None
        for term, term_norm in normalized_terms:
            ratio = difflib.SequenceMatcher(None, row_norm, term_norm).ratio()
            if ratio > best_ratio:
                best_ratio, best_term = ratio, term

        overlap = field_tokens & row_tokens
        overlap_score = (len(overlap) / len(field_tokens)) if field_tokens else 0.0

        # A row with exactly the canonical "two fiscal years" shape is
        # more likely to be a genuine top-level line item than a row
        # with a single figure or many (a sub-schedule/breakdown row) -
        # a small tie-breaking nudge, not a primary signal.
        shape_bonus = 0.05 if len(c["raw_values"]) == 2 else 0.0

        # A row whose own numbers all look like genuine financial
        # figures (comma-grouped, decimal, parenthesized-negative, or a
        # percentage) is far more likely to be real statement data than
        # one whose only numbers are bare short digit groups - a common
        # real-document pattern for a page heading's year, a document
        # footer's page number, or an address's PIN code (e.g. "Mumbai -
        # 400 063" itself passes _discover_candidates' own ">=1 numeric
        # token" test). A PENALTY, never an exclusion - discovery still
        # finds and ranks every such row, and a strong lexical/overlap
        # match can still outweigh this, so a genuine row with unusual
        # formatting is never silently discarded, only deprioritized.
        _looks_financial = any(
            ("," in str(v) or "." in str(v) or str(v).startswith("(") or str(v).endswith("%"))
            for v in c["raw_values"]
        )
        non_financial_penalty = 0.0 if _looks_financial else 0.15

        score = round(max(best_ratio, overlap_score) + shape_bonus - non_financial_penalty, 3)
        reason_bits = [f"lexical similarity {best_ratio:.2f} vs {best_term!r}"]
        if overlap:
            reason_bits.append(f"shares term(s): {', '.join(sorted(overlap))}")
        if non_financial_penalty:
            reason_bits.append("penalized: no number on this row looks like a formatted financial figure")
        ranked.append({**c, "score": score, "reason": "; ".join(reason_bits)})

    ranked.sort(key=lambda c: c["score"], reverse=True)
    return ranked


# ============================================================
# PROMPT BUILDING
# ============================================================

_MAPPING_INSTRUCTIONS = """You are a financial-statement field-mapping assistant. For each FIELD
below, a deterministic extraction pipeline searched its expected
statement section for this field's known alternate names (aliases) and
found NO match anywhere - the report may use different wording for the
same line (e.g. "Turnover" instead of "Revenue from Operations").

You will be given: the field's own aliases (which did NOT match - look
PAST these for a semantically equivalent row, not for these exact
words), and a short, pre-ranked list of CANDIDATE ROWS from that same
section (each with its own line number and text) that the deterministic
pipeline has not already assigned to any other field.

Your ONLY job is to decide whether one of the candidate rows is
semantically this field - e.g. a differently-worded label for the same
line item. You are NOT asked to read, transcribe, or reason about any
number on that row - a separate, deterministic step (not you) will read
this row's actual figures once your row selection is verified. Do not
include any number, amount, or figure anywhere in your response - your
response is a decision about WHICH ROW, nothing else.

CRITICAL RULES:
 - "selected_line_number" must be one of the candidate rows' own line
   numbers given to you for THIS field - never any other line number,
   never a line from a different field's candidates, never a number you
   were not shown.
 - NEVER invent a row that is not one of the candidates shown. If you
   are not confident, or if two or more candidates seem equally
   plausible and you cannot tell them apart, say so - do not guess.
 - Do not attempt to read this row's own printed figures, do not state
   a unit, and do not state a fiscal-year value - that is out of scope
   for this decision entirely.

For EACH field, in the SAME ORDER given, return one object with EXACTLY
these keys and no others:
  "field": the exact field name as given (copy it verbatim)
  "status": exactly one of CANDIDATE_FOUND, NO_CANDIDATE_FOUND, INSUFFICIENT_CONTEXT
  "confidence": a number from 0.0 to 1.0 (your confidence in the ROW selection only)
  "reason": one or two sentences explaining your judgement (row identity only - no figures)
  "selected_line_number": an integer from this field's OWN candidate
      rows if status is CANDIDATE_FOUND, otherwise null
  "evidence_line_numbers": a list of OTHER line numbers (from the
      candidates shown) that support your judgement - may be empty -
      never a value, only line numbers

Return ONLY a single JSON object of the shape {"results": [ ... ]}. No
other keys, no prose before or after it, no numbers anywhere.

FIELDS:
"""


def _build_mapping_prompt(contexts):
    return _MAPPING_INSTRUCTIONS + json.dumps(contexts, indent=2, default=str)


def _empty_ranking_info(candidate_count=0):
    return {"candidate_count": candidate_count, "shortlisted_count": 0, "candidate_rankings": ()}


def _field_mapping_context(field, structure, layout_lines, claimed_lines):
    """Returns (context_for_prompt, context_line_numbers, context_text,
    reason_code, ranking_info). context_for_prompt is None when there is
    nothing sendable for this field (reason_code explains why).
    ranking_info always carries whatever candidate-count/ranking
    information is known, even when context_for_prompt is None, so a
    reviewer can see e.g. "45 candidates existed but none were sent".
    No fiscal-year/column info is built here - Ollama only selects a
    row; the year/value assignment happens later, deterministically, in
    _finalize_mapping() once that row is verified."""

    section, _header_info, _current_x, _previous_x, _column_source = ai_validation._section_for_field(field, structure)
    candidates = _discover_candidates(section, layout_lines, claimed_lines)

    if candidates is None:
        return None, set(), "", "This field's statement section could not be located in this document at all.", _empty_ranking_info()
    if len(candidates) > MAX_CANDIDATE_ROWS:
        return None, set(), "", (
            f"This field's section produced {len(candidates)} candidate rows, more than the "
            f"{MAX_CANDIDATE_ROWS} considered safe to search - likely a section-detection issue "
            f"rather than something to resolve with a very large prompt."
        ), _empty_ranking_info(len(candidates))
    if not candidates:
        return None, set(), "", "No unclaimed, data-bearing rows exist in this field's section to consider.", _empty_ranking_info()

    ranked = _rank_candidates(field, candidates)
    shortlisted = ranked[:TOP_K_CANDIDATES]
    ranking_info = {
        "candidate_count": len(candidates),
        "shortlisted_count": len(shortlisted),
        "candidate_rankings": tuple(
            {"line_number": c["line_number"], "text": c["text"], "score": c["score"], "reason": c["reason"]}
            for c in ranked
        ),
    }

    # Only the shortlisted rows are ever placed into the prompt/context -
    # context_line_numbers is what _finalize_mapping's "may only cite
    # what it was shown" check verifies against, so it must reflect
    # exactly what Ollama actually saw, not the full discovered set.
    # No table header/column mapping is sent - Ollama is only deciding
    # WHICH ROW, never a value or fiscal-year assignment, so it has no
    # need for the column-to-year mapping at all (that's resolved
    # entirely deterministically once a row is verified - see
    # _finalize_mapping).
    context_line_numbers = {c["line_number"] for c in shortlisted}
    context_text_parts = [c["text"] for c in shortlisted]

    context_for_prompt = {
        "field": field,
        "aliases": ocr_pipeline.ALIASES.get(field, []),
        "candidate_rows": [{"line_number": c["line_number"], "text": c["text"]} for c in shortlisted],
    }
    context_text = " ".join(context_text_parts)
    return context_for_prompt, context_line_numbers, context_text, None, ranking_info


# ============================================================
# RESPONSE SCHEMA VALIDATION
# ============================================================

# Deliberately does NOT include any value/column-label key - Ollama's
# response is read through .get() below, so even if a raw response
# includes an extra "matched_value_current"-style key (e.g. an older
# prompt convention, a model ignoring instructions), it is never read
# into `validated` and can never reach a populated field - there is no
# code path left, anywhere in this module, that reads a number out of
# Ollama's response.
_REQUIRED_MAPPING_KEYS = {
    "field", "status", "confidence", "reason",
    "selected_line_number", "evidence_line_numbers",
}


def _validate_mapping_response(raw_text, expected_fields):
    try:
        parsed = json.loads(raw_text)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    results = parsed.get("results")
    if not isinstance(results, list):
        return {}

    validated = {}

    for item in results:
        if not isinstance(item, dict):
            continue
        if not _REQUIRED_MAPPING_KEYS.issubset(item.keys()):
            continue

        field = item.get("field")
        if field not in expected_fields:
            continue

        status = item.get("status")
        if status not in _MODEL_STATUSES:
            continue

        confidence = item.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        if not (0.0 <= float(confidence) <= 1.0):
            continue

        reason = item.get("reason")
        if not isinstance(reason, str):
            continue

        line_number = item.get("selected_line_number")
        if line_number is not None and (isinstance(line_number, bool) or not isinstance(line_number, int)):
            continue
        if status == "CANDIDATE_FOUND" and line_number is None:
            continue
        if status != "CANDIDATE_FOUND" and line_number is not None:
            continue

        evidence_line_numbers = item.get("evidence_line_numbers")
        if not isinstance(evidence_line_numbers, list) or not all(ai_validation._is_number(n) for n in evidence_line_numbers):
            continue

        validated[field] = {
            "status": status, "confidence": float(confidence), "reason": reason,
            "selected_line_number": line_number,
            "evidence_line_numbers": [int(n) for n in evidence_line_numbers],
        }

    return validated


# ============================================================
# DETERMINISTIC EVIDENCE VERIFICATION
# ============================================================

def _finalize_mapping(field, v, context_line_numbers, lines, layout_lines, meta, structure, ranking_info=None):
    """Ollama's contribution ends the moment `v["selected_line_number"]`
    is verified below - every value ever populated by this function is
    read by Python, from the real OCR text, via the same deterministic
    parser (ocr_pipeline.extract_number_tokens/select_fy_values) the
    non-AI exact/alias-match pipeline already uses. There is no branch
    anywhere below that reads a number out of `v` - it doesn't have one."""
    ranking_info = ranking_info or _empty_ranking_info()
    ranking_kwargs = dict(
        candidate_count=ranking_info["candidate_count"],
        shortlisted_count=ranking_info["shortlisted_count"],
        candidate_rankings=ranking_info["candidate_rankings"],
    )

    if v["status"] in ("NO_CANDIDATE_FOUND", "INSUFFICIENT_CONTEXT"):
        return _make_result(field, technical_status=AI_COMPLETED, mapping_status=v["status"],
                             confidence=v["confidence"], reason=v["reason"], **ranking_kwargs)

    line_number = v["selected_line_number"]

    def _rejected(detail, **overrides):
        kwargs = dict(
            field=field, technical_status=AI_COMPLETED, mapping_status="MAPPED_BUT_REJECTED",
            confidence=min(v["confidence"], 0.3), reason=f"[Rejected: {detail}] {v['reason']}",
            matched_source_line_number=line_number,
            evidence_line_numbers=tuple(v["evidence_line_numbers"]),
            evidence_grounded=False,
            **ranking_kwargs,
        )
        kwargs.update(overrides)
        return AIFieldMappingResult(**kwargs)

    # A. Selected line must be one of the candidates actually shown -
    # the one hard anti-hallucination check this module has always had,
    # unchanged by this redesign.
    if line_number not in context_line_numbers or not (0 <= line_number < len(layout_lines)):
        return _rejected(f"selected line {line_number!r} was not among the candidate rows shown for this field")

    # Optional supporting citations (line numbers only, never values) -
    # informational, never blocks an otherwise-verified row selection.
    valid_citation_lines = context_line_numbers | {line_number}
    evidence_grounded = (
        all(ln in valid_citation_lines for ln in v["evidence_line_numbers"])
        if v["evidence_line_numbers"] else None
    )

    matched_row_text = ai_validation._clean(layout_lines[line_number])

    # Units/scaling: never auto-convert - flag and stop rather than guess.
    unit_flag = _detect_unit_mismatch(matched_row_text)
    if unit_flag:
        return _make_result(
            field, technical_status=AI_COMPLETED, mapping_status="INSUFFICIENT_CONTEXT",
            confidence=min(v["confidence"], 0.3),
            reason=f"[Unit mismatch suspected ({unit_flag!r}) - not auto-converted] {v['reason']}",
            matched_source_line_number=line_number, matched_source_text=matched_row_text,
            evidence_line_numbers=tuple(v["evidence_line_numbers"]), evidence_grounded=evidence_grounded,
            **ranking_kwargs,
        )

    # Deterministic value extraction - the ONLY source of any number in
    # this module's output. Same function calls, same order, as
    # ocr_pipeline.extract_financial_values()'s own non-AI path
    # (ocr_pipeline.py locate_field()/select_fy_values() call sites).
    _section, header_info, current_x, previous_x, _column_source = ai_validation._section_for_field(field, structure)
    expected_count = header_info["count"] if header_info else None

    tokens = ocr_pipeline.extract_number_tokens(lines[line_number])
    tokens = ocr_pipeline._with_layout_offsets(tokens, layout_lines[line_number])
    selector = ocr_pipeline.select_fy_values_net_worth if field == "Net worth" else ocr_pipeline.select_fy_values
    fy_current, fy_previous, _det_confidence, _filtered_tokens, parse_status = selector(
        tokens, expected_count, current_x, previous_x
    )

    if parse_status == "REVIEW_REQUIRED" or (fy_current is None and fy_previous is None):
        return _make_result(
            field, technical_status=AI_COMPLETED, mapping_status="AI_ROW_SELECTED_VALUES_UNRESOLVED",
            confidence=v["confidence"],
            reason=(
                "The AI-selected row is verified as a real candidate, but this row's own numbers could not "
                "be confidently attributed to a fiscal year by the deterministic parser - nothing was "
                "populated. AI reasoning for the row selection: " + v["reason"]
            ),
            matched_source_line_number=line_number, matched_source_text=matched_row_text,
            evidence_line_numbers=tuple(v["evidence_line_numbers"]), evidence_grounded=evidence_grounded,
            **ranking_kwargs,
        )

    return _make_result(
        field, technical_status=AI_COMPLETED, mapping_status="MAPPED_AND_VERIFIED",
        confidence=v["confidence"], reason=v["reason"],
        matched_source_line_number=line_number, matched_source_text=matched_row_text,
        matched_column_current_label=meta.get("target_current_label"),
        matched_column_previous_label=meta.get("target_previous_label"),
        matched_value_current=fy_current, matched_value_previous=fy_previous,
        evidence_line_numbers=tuple(v["evidence_line_numbers"]), evidence_grounded=evidence_grounded,
        **ranking_kwargs,
    )


# ============================================================
# ORCHESTRATION
# ============================================================

def map_missing_fields(extracted, lines, meta=None, model=DEFAULT_MODEL, base_url=OLLAMA_BASE_URL,
                        layout_lines=None):
    """Top-level entry point. `extracted` is exactly what
    ocr_pipeline.extract_financial_values() produces - never mutated;
    this function always returns a NEW dict.

    Returns (augmented_extracted, mapping_results, availability):
      augmented_extracted: a copy of `extracted`, with every field this
                          module successfully mapped-and-verified now
                          populated (validation_status="AI_MAPPED").
                          Every other field is byte-for-byte identical
                          to the input.
      mapping_results:    {field: AIFieldMappingResult} - one entry for
                          every field that was NOT_FOUND on entry (never
                          partial). Empty if nothing was NOT_FOUND.
      availability:       the OllamaAvailability this run observed, or
                          None if there was nothing to map and Ollama
                          was never even probed.
    """

    if layout_lines is None:
        layout_lines = lines

    augmented = {field: dict(field_data) for field, field_data in extracted.items()}

    trigger_fields = [f for f, ed in extracted.items() if ed.get("validation_status") == "NOT_FOUND"]
    if not trigger_fields:
        return augmented, {}, None

    availability = check_ollama_availability(model=model, base_url=base_url)
    mapping_results = {}

    if not availability.reachable or not availability.model_installed:
        for field in trigger_fields:
            mapping_results[field] = _make_result(
                field, technical_status=AI_UNAVAILABLE, mapping_status=None,
                confidence=None, reason=availability.detail,
            )
        return augmented, mapping_results, availability

    structure = ai_validation._resolve_document_structure(lines, layout_lines, meta or {})
    claimed_lines = _claimed_line_numbers(extracted)

    batches = []
    for _section, section_fields in ai_validation._group_by_section(trigger_fields).items():
        batches.extend(list(ai_validation._chunk(section_fields, DYNAMIC_MAPPING_MAX_FIELDS_PER_BATCH)))

    deadline = time.monotonic() + DYNAMIC_MAPPING_TIME_BUDGET_SECONDS
    budget_note = (
        f"Skipped: document-level AI mapping time budget "
        f"({DYNAMIC_MAPPING_TIME_BUDGET_SECONDS:.0f}s) was exhausted before this batch could be started."
    )

    for batch_fields in batches:
        if ai_validation._budget_exhausted(deadline):
            for field in batch_fields:
                mapping_results[field] = _make_result(
                    field, technical_status=AI_SKIPPED, mapping_status=None,
                    confidence=None, reason=budget_note,
                )
            continue

        field_contexts = {}
        contexts_for_prompt = []
        for field in batch_fields:
            ctx, line_numbers, _text, reason_code, ranking_info = _field_mapping_context(
                field, structure, layout_lines, claimed_lines
            )
            if ctx is None:
                mapping_results[field] = _make_result(
                    field, technical_status=AI_COMPLETED, mapping_status="INSUFFICIENT_CONTEXT",
                    confidence=None, reason=reason_code,
                    candidate_count=ranking_info["candidate_count"],
                    shortlisted_count=ranking_info["shortlisted_count"],
                    candidate_rankings=ranking_info["candidate_rankings"],
                )
                continue
            field_contexts[field] = (line_numbers, ranking_info)
            contexts_for_prompt.append(ctx)

        if not contexts_for_prompt:
            continue

        prompt = _build_mapping_prompt(contexts_for_prompt)

        try:
            raw = call_ollama(prompt, model=model, base_url=base_url, timeout=DYNAMIC_MAPPING_CALL_TIMEOUT)
        except OllamaCallError as exc:
            for field in field_contexts:
                mapping_results[field] = _make_result(
                    field, technical_status=AI_ERROR, mapping_status=None,
                    confidence=None, reason=f"Ollama call failed: {exc}",
                )
            continue

        validated = _validate_mapping_response(raw, set(field_contexts.keys()))

        # Two fields in the SAME batch could both cite the same
        # candidate line (they were shown identical candidate lists
        # before either assignment was known) - process the
        # higher-confidence claim first so a later, lower-confidence
        # duplicate is rejected rather than silently also populated.
        ordered_fields = sorted(
            field_contexts.keys(),
            key=lambda f: validated.get(f, {}).get("confidence", 0.0),
            reverse=True,
        )
        used_lines_this_batch = set()

        for field in ordered_fields:
            if field not in validated:
                mapping_results[field] = _make_result(
                    field, technical_status=AI_ERROR, mapping_status=None, confidence=None,
                    reason="Ollama's response did not include a valid, schema-conforming mapping result for this field.",
                )
                continue

            v = validated[field]
            line_numbers, ranking_info = field_contexts[field]
            claimed_line = v.get("selected_line_number")

            if claimed_line is not None and claimed_line in used_lines_this_batch:
                mapping_results[field] = _make_result(
                    field, technical_status=AI_COMPLETED, mapping_status="MAPPED_BUT_REJECTED",
                    confidence=min(v["confidence"], 0.3),
                    reason=f"[Rejected: line {claimed_line} was already claimed by another field with equal or "
                           f"higher confidence in this same batch] {v['reason']}",
                    matched_source_line_number=claimed_line, evidence_grounded=False,
                    candidate_count=ranking_info["candidate_count"],
                    shortlisted_count=ranking_info["shortlisted_count"],
                    candidate_rankings=ranking_info["candidate_rankings"],
                )
                continue

            result = _finalize_mapping(field, v, line_numbers, lines, layout_lines, meta or {}, structure, ranking_info=ranking_info)
            mapping_results[field] = result

            if result.mapping_status == "MAPPED_AND_VERIFIED":
                used_lines_this_batch.add(result.matched_source_line_number)
                claimed_lines.add(result.matched_source_line_number)
                augmented[field]["fy_current"] = ocr_pipeline.clean_number(result.matched_value_current)
                augmented[field]["fy_previous"] = ocr_pipeline.clean_number(result.matched_value_previous)
                augmented[field]["ambiguous"] = False
                augmented[field]["validation_status"] = AI_MAPPED
                augmented[field]["source_line_number"] = result.matched_source_line_number
                augmented[field]["source_text"] = result.matched_source_text

    return augmented, mapping_results, availability
