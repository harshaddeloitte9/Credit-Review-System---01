"""
AI-assisted mapping CORRECTION PROPOSALS (Phase 3A) - a second, narrower
advisory layer on top of ai_validation.py's Phase 2D validator. Where
ai_validation.py only JUDGES whether an existing mapping looks right,
this module additionally asks the local Ollama model to SUGGEST a
better row/column mapping for the small subset of fields that were
already flagged as suspect - and, critically, verifies that suggestion
against the real OCR text in Python before it is ever shown to anyone.

Architectural boundary (identical in spirit to ai_validation.py's own):
  - This module NEVER writes to extracted[field], never calls
    ocr_pipeline.populate_workbook(), and is never read by
    input_data_engine.py / financial_analysis.py / risk_scoring.py.
    input_data_engine.py only ever reads from ReferenceInputData, built
    from the merged WORKBOOK - never from `extracted` directly - so a
    correction proposal is structurally unreachable by any downstream
    calculation regardless of what it contains.
  - Every proposal is advisory-only and always requires human review
    (see AICorrectionProposal.requires_review) - there is no "apply"
    path anywhere in this module or its caller.
  - Nothing here duplicates ocr_pipeline.py's extraction logic or
    ai_validation.py's table-structure logic: this module deliberately
    reuses ai_validation.py's existing PRIVATE helpers (same-layer
    sibling-module reuse - see the imports below) for locating a
    field's table header/target row/nearby rows and for the grounding
    primitives, rather than re-implementing any of it a second time.

WHICH FIELDS ARE CONSIDERED (see _fields_requiring_correction below):
  Only fields whose deterministic validation_status is REVIEW_REQUIRED,
  or whose Phase 2D AI validation status is YEAR_MISMATCH, FIELD_MISMATCH,
  or VALUE_SUSPICIOUS. Never CONFIRMED fields. Never NOT_FOUND fields -
  those remain exclusively Phase 2D's presence-check territory; this
  module does not touch them and does not propose extracting a value
  for a field with no deterministic mapping at all. Never AI_MAPPED
  fields either - those are decided only through the dynamic-mapping
  review (see _EXCLUDED_STATUSES).

TWO STATUS VOCABULARIES, same separation of concerns as ai_validation.py:

  PROPOSAL_STATUSES - the content judgement, set only when a real
  attempt was made and its response parsed:
    NO_PROPOSAL                  - the model looked and had nothing
                                    better to suggest
    PROPOSAL_AVAILABLE            - a proposal was made AND every
                                    deterministic check below passed
    PROPOSAL_UNVERIFIED           - the core proposed line/value/column
                                    passed verification, but the
                                    model's own SEPARATE supporting
                                    evidence citations did not ground
    PROPOSAL_REJECTED_BY_EVIDENCE - the core proposal itself failed a
                                    hard deterministic check (the
                                    proposed line wasn't one it was
                                    shown, the proposed value doesn't
                                    appear there, or the proposed
                                    fiscal-year column isn't real)
    INSUFFICIENT_CONTEXT          - the model itself said it can't
                                    tell, given what it was shown

  TECHNICAL STATUSES - reused verbatim from ai_validation.py
  (AI_COMPLETED/AI_SKIPPED/AI_UNAVAILABLE/AI_ERROR) - whether the
  machinery ran at all, entirely independent of the judgement above.
  "Time budget exhausted" is reported as AI_SKIPPED with a descriptive
  explanation, the same convention Phase 2D already uses, rather than a
  fifth technical status.
"""

import json
import os
import time
from dataclasses import dataclass
from typing import Optional

import ocr_pipeline
from ai_validation import (
    AI_COMPLETED, AI_ERROR, AI_SKIPPED, AI_UNAVAILABLE,
    OllamaAvailability, OllamaCallError,
    DEFAULT_MODEL, OLLAMA_BASE_URL,
    _budget_exhausted, _chunk, _clean, _field_context, _group_by_section,
    _ground_evidence, _is_number, _resolve_document_structure,
    _value_appears_in_text,
    call_ollama, check_ollama_availability, provider_display_name,
)

# ============================================================
# CONFIGURATION - correction-specific, deliberately smaller than
# ai_validation.py's own (see module docstring / Phase 3A plan): each
# correction prompt carries a heavier per-field payload (the field's
# full table-aware context PLUS its own AI-validation verdict), and the
# trigger set is already small by construction (only already-suspect
# fields), so a smaller batch/budget keeps the worst case bounded
# without needing to touch Phase 2D's own validator budget at all.
# ============================================================

CORRECTION_MAX_FIELDS_PER_BATCH = int(os.environ.get("AI_CORRECTION_MAX_FIELDS_PER_BATCH", "2"))
CORRECTION_CALL_TIMEOUT = int(os.environ.get("AI_CORRECTION_CALL_TIMEOUT", "120"))
CORRECTION_TIME_BUDGET_SECONDS = float(os.environ.get("AI_CORRECTION_TIME_BUDGET_SECONDS", "120"))


# ============================================================
# STATUS VOCABULARIES
# ============================================================

PROPOSAL_STATUSES = frozenset({
    "NO_PROPOSAL",
    "PROPOSAL_AVAILABLE",
    "PROPOSAL_UNVERIFIED",
    "PROPOSAL_REJECTED_BY_EVIDENCE",
    "INSUFFICIENT_CONTEXT",
})

# The narrower vocabulary the MODEL itself is asked to use - it can
# never claim "PROPOSAL_AVAILABLE" or "PROPOSAL_REJECTED_BY_EVIDENCE"
# directly, since those are Python's OWN verification verdicts, not
# something the model can know about its own output.
_MODEL_STATUSES = frozenset({"NO_PROPOSAL", "PROPOSAL_FOUND", "INSUFFICIENT_CONTEXT"})

# Which Phase 2D AI-validation statuses make a field eligible for a
# correction-proposal attempt (together with deterministic
# REVIEW_REQUIRED - see _fields_requiring_correction).
_AI_TRIGGER_STATUSES = frozenset({"YEAR_MISMATCH", "FIELD_MISMATCH", "VALUE_SUSPICIOUS"})


@dataclass(frozen=True)
class AICorrectionProposal:
    """One field's correction-proposal record. Every triggering field
    gets exactly one of these (never partial/missing, mirroring
    AIValidationResult's own convention) - a caller can always render
    something, regardless of what happened. Never mutates extracted[
    field] or anything ai_validation.py already produced."""

    field: str
    trigger_reason: str
    technical_status: str
    proposal_status: Optional[str]
    confidence: Optional[float]
    requires_review: Optional[bool]
    reason: str

    # The mapping as the deterministic pipeline currently has it -
    # copied verbatim, never recomputed.
    original_value_current: Optional[float]
    original_value_previous: Optional[float]
    original_source_line_number: Optional[int]
    original_source_text: Optional[str]

    # The AI's suggested alternative - populated only when
    # proposal_status came from an actual PROPOSAL_FOUND response.
    proposed_source_line_number: Optional[int] = None
    proposed_source_text: Optional[str] = None
    proposed_column_current_label: Optional[str] = None
    proposed_column_previous_label: Optional[str] = None
    proposed_value_current: Optional[float] = None
    proposed_value_previous: Optional[float] = None

    evidence_line_numbers: tuple = ()
    evidence_values: tuple = ()
    evidence_grounded: Optional[bool] = None


# ============================================================
# WHICH FIELDS TRIGGER A PROPOSAL ATTEMPT
# ============================================================

def _trigger_reason(field_data, ai_result):
    if ai_result is not None and ai_result.validation_status in _AI_TRIGGER_STATUSES:
        return f"AI validation: {ai_result.validation_status}"
    return f"Deterministic: {field_data.get('validation_status')}"


# Statuses whose fields are owned by another workflow and must never get
# a correction proposal:
#   NOT_FOUND - Phase 2D's presence-check / dynamic-mapping territory.
#   AI_MAPPED - populated by ai_dynamic_mapping.py and decided through its
#               own Accept / Reject / Keep as Not Found controls. Both
#               workflows record into the same approved_corrections[field]
#               entry, and reviewed_dataset reverts an AI_MAPPED field to
#               NOT_FOUND on any non-accept decision - so a proposal's
#               "Reject"/"Keep Original" would silently discard the mapping.
_EXCLUDED_STATUSES = frozenset({"NOT_FOUND", "AI_MAPPED"})


def _fields_requiring_correction(extracted, ai_results):
    """CONFIRMED, NOT_FOUND and AI_MAPPED fields are never included -
    NOT_FOUND stays exclusively Phase 2D's presence-check territory, and
    AI_MAPPED fields are decided only through the dynamic-mapping review
    (see _EXCLUDED_STATUSES). Dynamic mapping only ever produces results
    for fields that were NOT_FOUND on entry, which leaves them NOT_FOUND
    or AI_MAPPED, so the two workflows never cover the same field."""
    fields = []
    for field, field_data in extracted.items():
        if field_data.get("validation_status") in _EXCLUDED_STATUSES:
            continue
        det_review_required = field_data.get("validation_status") == "REVIEW_REQUIRED"
        ai_result = ai_results.get(field)
        ai_suspect = ai_result is not None and ai_result.validation_status in _AI_TRIGGER_STATUSES
        if det_review_required or ai_suspect:
            fields.append(field)
    return fields


# ============================================================
# PROMPT BUILDING
# ============================================================

_CORRECTION_INSTRUCTIONS = """You are a financial-statement mapping auditor. For each FIELD below, a
deterministic extraction pipeline's mapping has already been flagged as
possibly wrong (see "trigger_reason" and, where present, "ai_validation").

You will be given the field's aliases, the statement table's own header
(with its resolved column-to-fiscal-year mapping, or null if none was
found), the row the pipeline currently uses ("target_row"), a few
neighbouring rows ("nearby_rows"), and the pipeline's own current
current/previous-year assignment ("deterministic_mapping").

Your job is to suggest, ONLY IF you see clear evidence for one, a BETTER
row/column mapping for this field - never a value you have not seen
printed in the given text.

CRITICAL RULES:
 - You may ONLY propose "proposed_source_line_number" as one of the line
   numbers that ALREADY appear in this field's own table_header/
   target_row/nearby_rows above - never any other line number, never a
   line from a different field's context, never a number you have not
   been shown.
 - "proposed_value_current"/"proposed_value_previous" must be numbers
   you can actually see printed on that exact proposed line - never
   invent, estimate, or recompute a figure.
 - "proposed_column_current_label"/"proposed_column_previous_label"
   must be copied EXACTLY from the fiscal-year labels already given to
   you (the keys of deterministic_mapping) - never a different year.
 - If you do not see clear evidence for a better mapping, say so - do
   not guess just to have something to propose.

For EACH field, in the SAME ORDER given, return one object with EXACTLY
these keys and no others:
  "field": the exact field name as given (copy it verbatim)
  "status": exactly one of NO_PROPOSAL, PROPOSAL_FOUND, INSUFFICIENT_CONTEXT
  "confidence": a number from 0.0 to 1.0
  "reason": one or two sentences explaining your judgement
  "requires_review": true or false
  "proposed_source_line_number": an integer from this field's OWN given
      line numbers if status is PROPOSAL_FOUND, otherwise null
  "proposed_column_current_label": one of the two fiscal-year labels
      given to you, or null
  "proposed_column_previous_label": one of the two fiscal-year labels
      given to you, or null
  "proposed_value_current": a number actually printed on the proposed
      line, or null
  "proposed_value_previous": a number actually printed on the proposed
      line, or null
  "evidence_line_numbers": a list of line numbers (from what you were
      given) that support your judgement - may be empty
  "evidence_values": a list of the actual values (as printed) that
      support your judgement - may be empty

Return ONLY a single JSON object of the shape {"results": [ ... ]}. No
other keys, no prose before or after it.

FIELDS:
"""


def _build_correction_prompt(contexts):
    return _CORRECTION_INSTRUCTIONS + json.dumps(contexts, indent=2, default=str)


# ============================================================
# RESPONSE SCHEMA VALIDATION
# ============================================================

_REQUIRED_KEYS = {
    "field", "status", "confidence", "reason", "requires_review",
    "proposed_source_line_number", "proposed_column_current_label",
    "proposed_column_previous_label", "proposed_value_current",
    "proposed_value_previous", "evidence_line_numbers", "evidence_values",
}


def _validate_correction_response(raw_text, expected_fields):
    """Same never-raises, never-guesses posture as ai_validation.py's
    own _validate_batch_response(): anything missing, malformed, or
    inconsistent is simply absent from the returned dict."""

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
        if not _REQUIRED_KEYS.issubset(item.keys()):
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

        requires_review = item.get("requires_review")
        if not isinstance(requires_review, bool):
            continue

        reason = item.get("reason")
        if not isinstance(reason, str):
            continue

        line_number = item.get("proposed_source_line_number")
        if line_number is not None and (isinstance(line_number, bool) or not isinstance(line_number, int)):
            continue
        # A "found" proposal must name a line; anything else must not.
        if status == "PROPOSAL_FOUND" and line_number is None:
            continue
        if status != "PROPOSAL_FOUND" and line_number is not None:
            continue

        col_current = item.get("proposed_column_current_label")
        col_previous = item.get("proposed_column_previous_label")
        if col_current is not None and not isinstance(col_current, str):
            continue
        if col_previous is not None and not isinstance(col_previous, str):
            continue

        def _valid_optional_number(x):
            return x is None or (isinstance(x, (str, int, float)) and not isinstance(x, bool))

        val_current = item.get("proposed_value_current")
        val_previous = item.get("proposed_value_previous")
        if not _valid_optional_number(val_current) or not _valid_optional_number(val_previous):
            continue

        evidence_line_numbers = item.get("evidence_line_numbers")
        if not isinstance(evidence_line_numbers, list) or not all(_is_number(n) for n in evidence_line_numbers):
            continue

        evidence_values = item.get("evidence_values")
        if not isinstance(evidence_values, list) or not all(
            isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in evidence_values
        ):
            continue

        validated[field] = {
            "status": status, "confidence": float(confidence), "reason": reason,
            "requires_review": requires_review,
            "proposed_source_line_number": line_number,
            "proposed_column_current_label": col_current,
            "proposed_column_previous_label": col_previous,
            "proposed_value_current": val_current,
            "proposed_value_previous": val_previous,
            "evidence_line_numbers": [int(n) for n in evidence_line_numbers],
            "evidence_values": list(evidence_values),
        }

    return validated


# ============================================================
# DETERMINISTIC VERIFICATION (requirement 4 of the Phase 3A plan)
# ============================================================

def _make_proposal(field, field_data, ai_result, technical_status, proposal_status,
                    confidence, reason, requires_review, **overrides):
    kwargs = dict(
        field=field,
        trigger_reason=_trigger_reason(field_data, ai_result),
        technical_status=technical_status,
        proposal_status=proposal_status,
        confidence=confidence,
        requires_review=requires_review,
        reason=reason,
        original_value_current=field_data.get("fy_current"),
        original_value_previous=field_data.get("fy_previous"),
        original_source_line_number=field_data.get("source_line_number"),
        original_source_text=field_data.get("source_text"),
    )
    kwargs.update(overrides)
    return AICorrectionProposal(**kwargs)


REVIEWABLE_PROPOSAL_STATUSES = frozenset({"PROPOSAL_AVAILABLE", "PROPOSAL_UNVERIFIED"})


def proposed_values_problem(proposed_current, proposed_previous, original_current, original_previous):
    """None if accepting these proposed values is safe, otherwise a short
    reason. Safe means: at least one proposed value is present, every
    present value is a valid number, and no existing value would be
    replaced by nothing (accepting overwrites both fiscal-year values)."""
    pairs = (
        ("current", proposed_current, original_current),
        ("previous", proposed_previous, original_previous),
    )
    if all(value is None for _label, value, _original in pairs):
        return "no proposed value was supplied"
    for label, value, original in pairs:
        if value is None:
            if original is not None:
                return f"no proposed {label}-year value, but the field already has one - accepting would erase it"
        elif ocr_pipeline.clean_number(value) is None:
            return f"proposed {label}-year value {value!r} is not a valid number"
    return None


def proposal_is_acceptable(proposal):
    """True only for a reviewable proposal whose values are safe to apply
    - re-checked at decision time so an older in-session proposal created
    before check D existed can never be accepted either."""
    return (
        proposal.proposal_status in REVIEWABLE_PROPOSAL_STATUSES
        and proposed_values_problem(
            proposal.proposed_value_current, proposal.proposed_value_previous,
            proposal.original_value_current, proposal.original_value_previous,
        ) is None
    )


def _finalize_proposal(field, field_data, ai_result, v, context_line_numbers, context_text,
                        layout_lines, meta):
    """Runs the deterministic re-verification (A-E from the Phase 3A
    plan) against a schema-valid, parsed response `v` and returns the
    final AICorrectionProposal. Never trusts the model's own claims at
    face value - every check re-derives ground truth from layout_lines/
    meta, the same real data ai_validation.py itself works from."""

    if v["status"] in ("NO_PROPOSAL", "INSUFFICIENT_CONTEXT"):
        return _make_proposal(
            field, field_data, ai_result, technical_status=AI_COMPLETED,
            proposal_status=v["status"], confidence=v["confidence"],
            reason=v["reason"], requires_review=v["requires_review"],
            evidence_line_numbers=tuple(v["evidence_line_numbers"]),
            evidence_values=tuple(v["evidence_values"]),
        )

    # status == "PROPOSAL_FOUND" from here on.
    line_number = v["proposed_source_line_number"]

    def _rejected(detail):
        return _make_proposal(
            field, field_data, ai_result, technical_status=AI_COMPLETED,
            proposal_status="PROPOSAL_REJECTED_BY_EVIDENCE",
            confidence=min(v["confidence"], 0.3), requires_review=True,
            reason=f"[Rejected: {detail}] {v['reason']}",
            proposed_source_line_number=line_number,
            proposed_column_current_label=v["proposed_column_current_label"],
            proposed_column_previous_label=v["proposed_column_previous_label"],
            proposed_value_current=v["proposed_value_current"],
            proposed_value_previous=v["proposed_value_previous"],
            evidence_line_numbers=tuple(v["evidence_line_numbers"]),
            evidence_values=tuple(v["evidence_values"]),
            evidence_grounded=False,
        )

    # A. Proposed line must be one this field was ACTUALLY shown - never
    # any other line in the document, let alone one outside it.
    if line_number not in context_line_numbers or not (0 <= line_number < len(layout_lines)):
        return _rejected(f"proposed source line {line_number!r} was not among the lines shown for this field")

    proposed_row_text = _clean(layout_lines[line_number])
    proposed_row_norm = proposed_row_text.replace(",", "")

    # B. Every proposed value must actually appear on that real line.
    for value in (v["proposed_value_current"], v["proposed_value_previous"]):
        if value is not None and not _value_appears_in_text(value, proposed_row_norm):
            return _rejected(f"proposed value {value!r} does not appear on the proposed source line")

    # D. Accepting a proposal replaces BOTH fiscal-year values (see
    # reviewed_dataset.build_reviewed_extracted), so it must carry at
    # least one valid number and must never replace an existing value
    # with nothing.
    problem = proposed_values_problem(
        v["proposed_value_current"], v["proposed_value_previous"],
        field_data.get("fy_current"), field_data.get("fy_previous"),
    )
    if problem is not None:
        return _rejected(problem)

    # C. Proposed fiscal-year column(s) must be real, detected labels.
    valid_labels = {meta.get("target_current_label"), meta.get("target_previous_label")}
    for label in (v["proposed_column_current_label"], v["proposed_column_previous_label"]):
        if label is not None and label not in valid_labels:
            return _rejected(f"proposed column label {label!r} does not match a detected document column")

    # E. Secondary evidence grounding (the model's OWN supporting
    # citations, separate from the core proposal already verified
    # above). An empty citation list is not itself suspicious - the
    # core proposal already stands on its own - so only run the check
    # when the model actually cited something.
    if not v["evidence_line_numbers"] and not v["evidence_values"]:
        grounded = None
    else:
        full_context_lines = set(context_line_numbers) | {line_number}
        full_context_text = context_text + " " + proposed_row_text
        # "CONFIRMED" here only selects _ground_evidence's evidence-
        # required branch (see ai_validation.py) - it is not this
        # module's own proposal_status.
        grounded = _ground_evidence(
            "CONFIRMED", v["evidence_line_numbers"], v["evidence_values"],
            full_context_lines, full_context_text,
        )

    proposal_status = "PROPOSAL_UNVERIFIED" if grounded is False else "PROPOSAL_AVAILABLE"
    confidence = v["confidence"] if grounded is not False else min(v["confidence"], 0.3)
    requires_review = True if grounded is False else v["requires_review"]
    reason = v["reason"] if grounded is not False else (
        "[Core proposal verified, but the model's own supporting evidence citations could not be verified] "
        + v["reason"]
    )

    return _make_proposal(
        field, field_data, ai_result, technical_status=AI_COMPLETED,
        proposal_status=proposal_status, confidence=confidence,
        reason=reason, requires_review=requires_review,
        proposed_source_line_number=line_number, proposed_source_text=proposed_row_text,
        proposed_column_current_label=v["proposed_column_current_label"],
        proposed_column_previous_label=v["proposed_column_previous_label"],
        proposed_value_current=v["proposed_value_current"],
        proposed_value_previous=v["proposed_value_previous"],
        evidence_line_numbers=tuple(v["evidence_line_numbers"]),
        evidence_values=tuple(v["evidence_values"]),
        evidence_grounded=grounded,
    )


# ============================================================
# ORCHESTRATION
# ============================================================

def generate_correction_proposals(extracted, ai_results, lines, meta=None, model=DEFAULT_MODEL,
                                   base_url=OLLAMA_BASE_URL, layout_lines=None):
    """Top-level entry point. `extracted` is exactly what
    ocr_pipeline.extract_financial_values() produces; `ai_results` is
    exactly ai_validation.validate_extracted_fields()'s first return
    value. Neither is ever mutated.

    Returns (proposals, availability):
      proposals:    {field: AICorrectionProposal} - one entry for every
                   field _fields_requiring_correction() selects (never
                   partial). Empty if no field needed one.
      availability: the OllamaAvailability this run observed, or None
                   if there was nothing to correct and Ollama was never
                   even probed (see Test 7 in the Phase 3A plan).

    Batches by statement section (capped at CORRECTION_MAX_FIELDS_PER_
    BATCH per call) and stops starting new batches once CORRECTION_
    TIME_BUDGET_SECONDS has elapsed - exactly Phase 2D's own bounded-
    batching/time-budget/graceful-partial-completion pattern, reused
    verbatim via the imported helpers."""

    if layout_lines is None:
        layout_lines = lines

    trigger_fields = _fields_requiring_correction(extracted, ai_results)
    if not trigger_fields:
        return {}, None

    availability = check_ollama_availability(model=model, base_url=base_url)
    proposals = {}

    if not availability.reachable or not availability.model_installed:
        for field in trigger_fields:
            proposals[field] = _make_proposal(
                field, extracted[field], ai_results.get(field),
                technical_status=AI_UNAVAILABLE, proposal_status=None,
                confidence=None, reason=availability.detail, requires_review=None,
            )
        return proposals, availability

    structure = _resolve_document_structure(lines, layout_lines, meta or {})

    batches = []
    for _section, section_fields in _group_by_section(trigger_fields).items():
        batches.extend(list(_chunk(section_fields, CORRECTION_MAX_FIELDS_PER_BATCH)))

    deadline = time.monotonic() + CORRECTION_TIME_BUDGET_SECONDS
    budget_note = (
        f"Skipped: document-level AI correction-proposal time budget "
        f"({CORRECTION_TIME_BUDGET_SECONDS:.0f}s) was exhausted before this batch could be started."
    )

    for batch_fields in batches:
        if _budget_exhausted(deadline):
            for field in batch_fields:
                proposals[field] = _make_proposal(
                    field, extracted[field], ai_results.get(field),
                    technical_status=AI_SKIPPED, proposal_status=None,
                    confidence=None, reason=budget_note, requires_review=None,
                )
            continue

        field_contexts = {}
        contexts_for_prompt = []
        for field in batch_fields:
            ctx, line_numbers, text = _field_context(field, extracted[field], layout_lines, structure, meta or {})
            ai_result = ai_results.get(field)
            if ai_result is not None and ai_result.validation_status is not None:
                ctx["ai_validation"] = {
                    "status": ai_result.validation_status,
                    "explanation": ai_result.explanation,
                }
            ctx["trigger_reason"] = _trigger_reason(extracted[field], ai_result)
            field_contexts[field] = (line_numbers, text)
            contexts_for_prompt.append(ctx)

        prompt = _build_correction_prompt(contexts_for_prompt)

        try:
            raw = call_ollama(prompt, model=model, base_url=base_url, timeout=CORRECTION_CALL_TIMEOUT)
        except OllamaCallError as exc:
            for field in batch_fields:
                proposals[field] = _make_proposal(
                    field, extracted[field], ai_results.get(field),
                    technical_status=AI_ERROR, proposal_status=None,
                    confidence=None, reason=f"{provider_display_name()} call failed: {exc}", requires_review=None,
                )
            continue

        validated = _validate_correction_response(raw, set(batch_fields))

        for field in batch_fields:
            if field not in validated:
                proposals[field] = _make_proposal(
                    field, extracted[field], ai_results.get(field),
                    technical_status=AI_ERROR, proposal_status=None, confidence=None,
                    reason=f"{provider_display_name()}'s response did not include a valid, schema-conforming correction result for this field.",
                    requires_review=None,
                )
                continue

            line_numbers, text = field_contexts[field]
            proposals[field] = _finalize_proposal(
                field, extracted[field], ai_results.get(field), validated[field],
                line_numbers, text, layout_lines, meta or {},
            )

    return proposals, availability
