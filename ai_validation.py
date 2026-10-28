"""
AI-assisted validation layer (Phase 2, strengthened in Phase 2D) - an
ADVISORY overlay on top of the deterministic extraction pipeline
(ocr_pipeline.py), never a replacement for it.

Architectural boundary, enforced throughout this module:
  - This module NEVER modifies fy_current, fy_previous, or the
    deterministic validation_status extract_financial_values() already
    produced (see ocr_pipeline.py) - it only ever ADDS a parallel,
    separately-keyed judgement that the UI may show alongside the
    deterministic result. Nothing downstream (input_data_engine.py,
    financial_analysis.py, risk_scoring.py, ...) reads anything from
    this module - they are untouched by this phase.
  - The AI is asked to JUDGE already-extracted values against real,
    located table structure (header, column-to-year mapping, the
    target row, nearby rows) - never to supply a missing value itself,
    and never to invent evidence. Its own output is treated as
    untrusted free-form text until it passes the strict schema check
    in _validate_batch_response() below, AND every line number/value it
    cites is programmatically verified against what it was actually
    given (see "GROUNDED EVIDENCE" below) - an unverifiable claim is
    downgraded, never trusted at face value.
  - ocr_pipeline.py itself is never modified or duplicated-with-drift:
    every piece of table structure this module needs (statement
    sections, header detection, year-to-column calibration, BS
    subsections) is obtained by calling ocr_pipeline's own existing,
    already-validated PUBLIC functions a second time on the same OCR
    lines - deterministic, side-effect-free functions, so this always
    reproduces exactly what extract_financial_values() itself used,
    never a second, potentially-drifting reimplementation.

Two DELIBERATELY SEPARATE status vocabularies for the MAIN validator
(do not conflate them):

  VALIDATION_STATUSES - the AI's own content judgement about a field,
  only ever set when the AI actually ran and returned a well-formed
  response:
    CONFIRMED            - extracted value(s) look correctly matched
    YEAR_MISMATCH         - value(s) look like they belong to the wrong
                            fiscal-year column
    FIELD_MISMATCH        - value(s) look like they belong to a
                            different field/row entirely
    VALUE_SUSPICIOUS      - value(s) look internally inconsistent with
                            the surrounding text (e.g. wrong scale)
    NOT_FOUND_IN_SOURCE   - the AI could not find this field's value in
                            the text it was given
    INSUFFICIENT_CONTEXT  - the AI examined the context and judged it
                            cannot confidently decide either way

  TECHNICAL STATUSES - whether the AI validation machinery itself ran
  successfully for a field, entirely independent of the judgement above:
    AI_COMPLETED   - a call was made and this field's result parsed
                     against the strict schema successfully
    AI_SKIPPED     - deliberately not sent to the AI this run (the
                     deterministic pipeline already found nothing to
                     validate, OR the document-level time budget ran
                     out before this field's batch could be started -
                     see "CPU PERFORMANCE STRATEGY" below)
    AI_UNAVAILABLE - Ollama is not reachable, or the configured model
                     is not installed - no attempt was made at all
    AI_ERROR       - an attempt WAS made (Ollama was available) but it
                     failed - a network/timeout error, a non-200
                     response, or a response that did not include a
                     valid, schema-conforming, EVIDENCE-VERIFIABLE
                     result for this field

  INSUFFICIENT_CONTEXT is a VALIDATION_STATUSES member (the AI's own
  judgement, produced by a successful call) and must never be used to
  paper over an outage or a technical failure - those are always
  AI_ERROR / AI_UNAVAILABLE with validation_status left as None.

A THIRD, separate vocabulary (PRESENCE_STATUSES) exists for the
narrow, secondary "field presence check" applied to deterministically
NOT_FOUND fields - see "NOT_FOUND SECONDARY VALIDATION" below. It is
never mixed with VALIDATION_STATUSES: a presence check is a location
question ("might this field's data exist somewhere under different
wording"), never a content judgement, and its output schema is
structurally incapable of carrying a financial value.

GROUNDED EVIDENCE (Phase 2D): a live smoke test (Phase 2C) proved a
small local model can hallucinate - e.g. citing a supporting number
that appears nowhere in the text it was given. The main validator's
JSON contract therefore requires evidence_line_numbers and
evidence_values for every result, and _ground_evidence() below checks
each one against exactly the line numbers and text this module actually
sent for that field. A result whose evidence cannot be verified is
never discarded outright (the AI's own judgement may still be useful
context for a human) - it is downgraded: confidence capped low,
requires_review forced True, and evidence_grounded set False, so a
reviewer can immediately see the citation could not be checked.

CPU PERFORMANCE STRATEGY (Phase 2D): Phase 2C's live timings on this
CPU-only machine - a single-field call ~28s, an 11-12 field batch
~271-278s - showed the old one-call-per-section design scales almost
linearly per field (~22-25s/field) and blows through any reasonable
fixed per-call timeout once a section has more than a handful of
fields. Rather than raising CALL_TIMEOUT to 400-600s (which would make
one slow section block the whole upload for that long), this module
now: (1) caps every batch at MAX_FIELDS_PER_BATCH fields, so a large
section becomes several small, fast, more-reliable calls instead of one
huge one; (2) tracks a document-level TIME_BUDGET_SECONDS wall-clock
budget across the whole validation run, so a document with many
sections degrades GRACEFULLY (remaining not-yet-started batches become
AI_SKIPPED with a clear "time budget exhausted" reason) rather than
making a Streamlit upload hang indefinitely; (3) prioritises fields the
deterministic pipeline already flagged REVIEW_REQUIRED, so if the
budget runs out, the fields most worth a second opinion were validated
first, not last.
"""

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Optional

import requests

import ocr_pipeline

# ============================================================
# CONFIGURATION
# ============================================================

# Which AI provider the three AI layers (this module, ai_dynamic_
# mapping.py, ai_correction_proposals.py) actually talk to -
# check_ollama_availability()/call_ollama() below dispatch on this.
# "ollama" (the default) runs the exact, unmodified code already in
# both functions; "openai" is an isolated alternative (see
# openai_provider.py) for testing against a real OpenAI account. Never
# switches silently - if the selected provider fails, that failure is
# reported exactly like an Ollama failure always has been (AI_UNAVAILABLE/
# AI_ERROR), never retried against the other provider.
AI_PROVIDER = os.environ.get("AI_PROVIDER", "ollama").strip().lower()

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

# qwen2.5:7b-instruct was Phase 2A's original recommendation, but was
# never actually installed in this environment across every phase of
# this project (network/registry issues pulling it) - every real live
# test this whole project has run against llama3.2:latest instead, via
# an explicit per-call override in throwaway scripts. Those overrides
# never touched this default, so the actually-running application (every
# real call site in ocr_upload.py calls with no `model=` override) was
# silently checking for/requesting a model that was never installed,
# unconditionally reporting AI_UNAVAILABLE for every field regardless of
# Ollama's real state - confirmed empirically: check_ollama_availability()
# with no override returned model_installed=False with detail "...
# 'qwen2.5:7b-instruct' is not installed..." even with Ollama running and
# llama3.2:latest genuinely installed and working. Never assumed
# installed either way - check_ollama_availability() below always
# verifies it (or whatever model is configured) is actually present
# before any call is attempted.
# Provider-aware: resolves to whichever provider AI_PROVIDER actually
# selects, each independently configurable/env-overridable, never a
# single hardcoded assumed model for either provider.
_OLLAMA_DEFAULT_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
_OPENAI_DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
DEFAULT_MODEL = _OPENAI_DEFAULT_MODEL if AI_PROVIDER == "openai" else _OLLAMA_DEFAULT_MODEL

AVAILABILITY_TIMEOUT = 1.5   # seconds - just checking the API is up

# A remote HTTPS round-trip to OpenAI is never as fast as a local-
# loopback ping to Ollama (AVAILABILITY_TIMEOUT above) - a separate,
# more generous timeout for the OpenAI availability check specifically,
# so a genuinely-reachable OpenAI account is never misreported as
# unreachable just because 1.5s wasn't enough for the real network
# round-trip.
OPENAI_AVAILABILITY_TIMEOUT = float(os.environ.get("OPENAI_AVAILABILITY_TIMEOUT", "10"))

# Per-call timeout. Raised from Phase 2B's 60s: real batches of even a
# handful of fields regularly exceed that on CPU-only hardware (see
# module docstring). 150s was originally sized for a
# MAX_FIELDS_PER_BATCH=4 batch, but real measurement on this CPU-only
# machine showed a 4-field batch ranging anywhere from ~125s to ~270s -
# close enough to 150s that it genuinely timed out in real use (a real
# requests.exceptions.ReadTimeout, not a hidden/hypothetical failure),
# marking every field in that batch AI_ERROR together. Left unchanged
# here - MAX_FIELDS_PER_BATCH below is lowered instead, which shrinks
# each call's own real duration well under this same timeout without
# touching the timeout value itself.
CALL_TIMEOUT = int(os.environ.get("OLLAMA_CALL_TIMEOUT", "150"))

# Hard cap on fields per Ollama call. A large section (e.g. a 12-field
# Balance Sheet) is now split into several small batches instead of one
# call that scales past any reasonable timeout - see module docstring.
# Lowered from 4 to 1: real measurement on this CPU-only machine showed
# a 4-field batch's own real duration (~125-270s) sitting right on top
# of CALL_TIMEOUT (150s), causing genuine timeouts that marked every
# field in the batch AI_ERROR together. A single-field call is both
# smaller (faster, safely under the timeout) and, if it does still time
# out, only ever costs that one field rather than up to three others
# that would have otherwise validated successfully in the same batch.
MAX_FIELDS_PER_BATCH = int(os.environ.get("AI_VALIDATION_MAX_FIELDS_PER_BATCH", "1"))

# Document-level wall-clock ceiling across the WHOLE validate_extracted_
# fields() run (all batches, both the main validator and the NOT_FOUND
# presence check combined). Bounds how long AI validation can add to a
# single Streamlit upload - once exhausted, every batch not yet started
# is marked AI_SKIPPED (graceful partial completion) rather than the
# upload hanging until every section finishes.
TIME_BUDGET_SECONDS = float(os.environ.get("AI_VALIDATION_TIME_BUDGET_SECONDS", "240"))

# How many OCR lines of context to include before/after each field's own
# matched source line as "nearby rows" - kept small deliberately: the
# table HEADER is now located and included explicitly (see
# _resolve_document_structure/_field_context below), so this window no
# longer needs to be widened just to hope it reaches the header.
CONTEXT_WINDOW = 2


# ============================================================
# STATUS VOCABULARIES
# ============================================================

VALIDATION_STATUSES = frozenset({
    "CONFIRMED",
    "YEAR_MISMATCH",
    "FIELD_MISMATCH",
    "VALUE_SUSPICIOUS",
    "NOT_FOUND_IN_SOURCE",
    "INSUFFICIENT_CONTEXT",
})

# Statuses that assert a specific, checkable fact about the source text
# (as opposed to "I looked and found/could decide nothing") - these
# require verifiable evidence; the other two are inherently evidence-
# free (there is nothing to point at when nothing was found, or when
# the model itself says the context wasn't enough to decide).
_EVIDENCE_REQUIRED_STATUSES = frozenset({
    "CONFIRMED", "YEAR_MISMATCH", "FIELD_MISMATCH", "VALUE_SUSPICIOUS",
})

AI_COMPLETED = "AI_COMPLETED"
AI_SKIPPED = "AI_SKIPPED"
AI_UNAVAILABLE = "AI_UNAVAILABLE"
AI_ERROR = "AI_ERROR"

TECHNICAL_STATUSES = frozenset({AI_COMPLETED, AI_SKIPPED, AI_UNAVAILABLE, AI_ERROR})

# The narrow, secondary "does this field's data possibly exist under
# different wording" check applied ONLY to deterministically NOT_FOUND
# fields - see "NOT_FOUND SECONDARY VALIDATION" below. Deliberately
# disjoint in spirit from VALIDATION_STATUSES even where a name is
# reused (NOT_FOUND_IN_SOURCE/INSUFFICIENT_CONTEXT): this is a location
# question, never a content judgement, and can never carry a value.
PRESENCE_STATUSES = frozenset({
    "FOUND_POSSIBLE_MATCH",
    "NOT_FOUND_IN_SOURCE",
    "INSUFFICIENT_CONTEXT",
})


class OllamaCallError(RuntimeError):
    """Raised by call_ollama() when the API call itself fails (network
    error, timeout, non-200 response, or an unparseable response
    envelope) - distinct from a successful call whose JSON payload
    didn't pass the field-level schema/evidence check (that is reported
    as AI_ERROR for the affected field(s) by validate_extracted_fields(),
    not raised as an exception - a technical failure at the batch level
    still lets other batches/fields proceed)."""


@dataclass(frozen=True)
class AIValidationResult:
    """One field's AI-validation record, from the MAIN validator (never
    used for NOT_FOUND fields - see FieldPresenceResult for those).
    Every field sent to the main validator gets exactly one of these -
    never a partial/missing entry.

    Evidence fields (fy_current through source_text) are copied straight
    from the deterministic extraction's own traceability metadata, never
    recomputed or altered here. evidence_line_numbers/evidence_values are
    what the AI itself CITED as support for its judgement; evidence_
    grounded records whether that citation was programmatically verified
    against the context this module actually gave it (None when the
    result's status doesn't require evidence at all - see
    _EVIDENCE_REQUIRED_STATUSES)."""

    field: str
    technical_status: str
    validation_status: Optional[str]
    confidence: Optional[float]
    explanation: str
    requires_review: Optional[bool]

    # Evidence, copied verbatim from the deterministic extraction - see
    # ocr_pipeline.py's extracted[field] dict (Phase 1).
    fy_current: Optional[float]
    fy_previous: Optional[float]
    year_current_label: Optional[str]
    year_previous_label: Optional[str]
    source_line_number: Optional[int]
    source_text: Optional[str]

    # What the AI itself cited (Phase 2D) and whether that citation was
    # verified against the real context it was given.
    evidence_line_numbers: tuple = ()
    evidence_values: tuple = ()
    evidence_grounded: Optional[bool] = None


@dataclass(frozen=True)
class FieldPresenceResult:
    """One NOT_FOUND field's result from the secondary, narrow
    "field presence check" (Phase 2D) - structurally incapable of
    carrying a financial value: there is no field here for one. See
    "NOT_FOUND SECONDARY VALIDATION" below."""

    field: str
    technical_status: str
    presence_status: Optional[str]
    confidence: Optional[float]
    explanation: str
    candidate_line_numbers: tuple = ()


# ============================================================
# DASHBOARD BUCKETS - one, mutually exclusive bucket per main-validator
# result, so "AI Confirmed", "AI Flagged" etc. always add up to the
# number of fields sent to the validator and never overlap. Advisory
# only: none of this ever changes a field's deterministic status.
# ============================================================

# Content judgements that mean "the extracted value may be wrong".
CONCERN_STATUSES = frozenset({
    "YEAR_MISMATCH", "FIELD_MISMATCH", "VALUE_SUSPICIOUS", "NOT_FOUND_IN_SOURCE",
})

BUCKET_CONFIRMED = "confirmed"        # AI_COMPLETED + CONFIRMED, no review requested
BUCKET_FLAGGED = "flagged"            # AI_COMPLETED + a concern status, OR any completed result with requires_review=True
BUCKET_INCONCLUSIVE = "inconclusive"  # AI_COMPLETED + INSUFFICIENT_CONTEXT, no review requested
BUCKET_SKIPPED = "skipped"            # AI_SKIPPED (e.g. time budget exhausted) - never sent to the AI
BUCKET_UNAVAILABLE = "unavailable"    # AI_UNAVAILABLE - provider not available, no attempt made
BUCKET_ERROR = "error"                # AI_ERROR - attempted, but the call/response failed

VALIDATION_BUCKETS = (
    BUCKET_CONFIRMED, BUCKET_FLAGGED, BUCKET_INCONCLUSIVE,
    BUCKET_SKIPPED, BUCKET_UNAVAILABLE, BUCKET_ERROR,
)


def classify_validation_result(result):
    """Exactly one VALIDATION_BUCKETS member for an AIValidationResult.
    A CONFIRMED result whose evidence could not be verified (or where the
    AI itself asked for review) is FLAGGED, not confirmed - a confirmation
    nobody can check is not counted as one."""
    technical = result.technical_status
    if technical == AI_SKIPPED:
        return BUCKET_SKIPPED
    if technical == AI_UNAVAILABLE:
        return BUCKET_UNAVAILABLE
    if technical != AI_COMPLETED:
        return BUCKET_ERROR
    if result.requires_review is True or result.validation_status in CONCERN_STATUSES:
        return BUCKET_FLAGGED
    if result.validation_status == "CONFIRMED":
        return BUCKET_CONFIRMED
    return BUCKET_INCONCLUSIVE


def summarize_validation_results(results):
    """Counts built straight from the per-field records (never from a
    separately-maintained counter). Returns:
      {"total": n, "buckets": {bucket: count}, "statuses": {validation_status: count},
       "by_field": {field: bucket}}"""
    buckets = {b: 0 for b in VALIDATION_BUCKETS}
    statuses = {}
    by_field = {}
    for field, result in results.items():
        bucket = classify_validation_result(result)
        buckets[bucket] += 1
        by_field[field] = bucket
        if result.technical_status == AI_COMPLETED and result.validation_status is not None:
            statuses[result.validation_status] = statuses.get(result.validation_status, 0) + 1
    return {"total": len(results), "buckets": buckets, "statuses": statuses, "by_field": by_field}


def provider_display_name():
    """The user-facing name of the provider check_ollama_availability()/
    call_ollama() actually dispatch to: exactly "openai" selects OpenAI;
    every other string value runs the Ollama code path. A neutral label
    is returned only if the setting itself cannot be read."""
    if not isinstance(AI_PROVIDER, str):
        return "AI provider"
    return "OpenAI" if AI_PROVIDER == "openai" else "Ollama"


@dataclass(frozen=True)
class OllamaAvailability:
    reachable: bool
    model_installed: bool
    installed_models: list
    detail: str


# ============================================================
# OLLAMA AVAILABILITY / MODEL DETECTION
# (unchanged from Phase 2B - no issues found here during Phase 2C)
# ============================================================

def _model_name_matches(configured, installed_name):
    """Ollama's own /api/tags list uses a bare tag ("qwen2.5:7b-instruct")
    or, for an untagged pull, an implicit ":latest" suffix. Matches
    either shape without guessing beyond that - a configured model that
    genuinely isn't installed must never be reported as installed."""
    if configured == installed_name:
        return True
    if ":" not in configured and installed_name == f"{configured}:latest":
        return True
    return False


def check_ollama_availability(model=DEFAULT_MODEL, base_url=OLLAMA_BASE_URL,
                               timeout=AVAILABILITY_TIMEOUT) -> OllamaAvailability:
    """Never raises - any failure to reach/parse Ollama's own API is
    reported as reachable=False with a clear, specific `detail`, so a
    caller can always show something sensible rather than crash.

    Despite the name (kept for backward compatibility with every
    existing caller/mock across ai_dynamic_mapping.py/
    ai_correction_proposals.py and the whole test suite), this function
    dispatches on AI_PROVIDER - see openai_provider.py for the
    alternative. Never falls back to Ollama if the selected provider
    fails; the failure is reported exactly like an Ollama failure would
    be."""
    if AI_PROVIDER == "openai":
        import openai_provider
        return openai_provider.check_openai_availability(model=model, timeout=OPENAI_AVAILABILITY_TIMEOUT)

    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=timeout)
    except requests.exceptions.RequestException as exc:
        return OllamaAvailability(
            reachable=False, model_installed=False, installed_models=[],
            detail=f"Ollama is not reachable at {base_url} ({exc.__class__.__name__}). "
                   f"Is 'ollama serve' running?",
        )

    if resp.status_code != 200:
        return OllamaAvailability(
            reachable=False, model_installed=False, installed_models=[],
            detail=f"Ollama responded with HTTP {resp.status_code} at {base_url}.",
        )

    try:
        body = resp.json()
    except ValueError:
        return OllamaAvailability(
            reachable=False, model_installed=False, installed_models=[],
            detail="Ollama's /api/tags response was not valid JSON.",
        )

    installed = [
        m.get("name", "") for m in body.get("models", []) if isinstance(m, dict)
    ]
    model_installed = any(_model_name_matches(model, name) for name in installed)

    if not model_installed:
        return OllamaAvailability(
            reachable=True, model_installed=False, installed_models=installed,
            detail=f"Ollama is running, but '{model}' is not installed. Run: ollama pull {model}",
        )

    return OllamaAvailability(reachable=True, model_installed=True, installed_models=installed, detail="")


# ============================================================
# OLLAMA CALL - the one seam tests monkeypatch (see test_ai_validation.py)
# (unchanged from Phase 2B)
# ============================================================

def call_ollama(prompt, model=DEFAULT_MODEL, base_url=OLLAMA_BASE_URL, timeout=CALL_TIMEOUT) -> str:
    """Single-shot call to Ollama's /api/generate, constrained to JSON
    output. Returns the raw response text (still untrusted at this
    point - _validate_batch_response() below does the real checking).
    Raises OllamaCallError on any network/timeout/non-200/malformed-
    envelope failure - never returns a fabricated placeholder.

    Despite the name (kept for backward compatibility - see
    check_ollama_availability()'s own docstring above), dispatches on
    AI_PROVIDER. `timeout` is forwarded as-is to OpenAI too (whatever
    the caller resolved it to, typically CALL_TIMEOUT - a generous but
    harmless ceiling for a hosted API as well)."""
    if AI_PROVIDER == "openai":
        import openai_provider
        return openai_provider.call_openai(prompt, model=model, timeout=timeout)

    try:
        resp = requests.post(
            f"{base_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False, "format": "json"},
            timeout=timeout,
        )
    except requests.exceptions.RequestException as exc:
        raise OllamaCallError(f"Ollama call failed ({exc.__class__.__name__}): {exc}") from exc

    if resp.status_code != 200:
        raise OllamaCallError(f"Ollama returned HTTP {resp.status_code}: {resp.text[:500]}")

    try:
        body = resp.json()
    except ValueError as exc:
        raise OllamaCallError(f"Ollama's response envelope was not valid JSON: {exc}") from exc

    return body.get("response", "")


# ============================================================
# TABLE-AWARE DOCUMENT STRUCTURE (Phase 2D)
#
# Re-derives the exact section / header / year-to-column structure
# extract_financial_values() computed internally, by calling
# ocr_pipeline's own existing PUBLIC, pure functions a second time on
# the same lines - never a re-implementation, never a modification of
# ocr_pipeline.py. Since these functions are deterministic and
# side-effect-free, calling them again on identical input reproduces
# identical output - there is no drift risk.
# ============================================================

_SECTION_TYPES = ("PNL", "BS", "CF", "RATIO")


def _resolve_document_structure(lines, layout_lines, meta):
    """Computed ONCE per validate_extracted_fields() call (not per
    field) - mirrors extract_financial_values()'s own per-section-type
    setup exactly (see ocr_pipeline.py)."""

    target_current_year = meta.get("target_current_year") if meta else None
    target_previous_year = meta.get("target_previous_year") if meta else None

    sections = ocr_pipeline.find_statement_sections(lines)

    section_by_type = {}
    header_by_type = {}
    columns_by_type = {}

    for section_type in _SECTION_TYPES:
        section = ocr_pipeline.pick_section(sections, section_type)
        section_by_type[section_type] = section

        if not section:
            header_by_type[section_type] = None
            columns_by_type[section_type] = (None, None, "unavailable")
            continue

        header_info = ocr_pipeline.detect_header_periods(
            layout_lines, section["start"], section["end"]
        )
        header_by_type[section_type] = header_info

        current_x, previous_x = ocr_pipeline.resolve_year_columns(
            header_info, target_current_year, target_previous_year
        )
        column_source = "header"

        if current_x is None or previous_x is None:
            current_x, previous_x, confident = ocr_pipeline.calibrate_columns_from_data(
                layout_lines, section
            )
            column_source = "data_median" if confident else "unavailable"

        columns_by_type[section_type] = (current_x, previous_x, column_source)

    bs_subsections = ocr_pipeline.find_bs_subsections(lines, section_by_type.get("BS"))

    return {
        "section_by_type": section_by_type,
        "header_by_type": header_by_type,
        "columns_by_type": columns_by_type,
        "bs_subsections": bs_subsections,
    }


def _section_for_field(field, structure):
    """Mirrors extract_financial_values()'s own per-field section
    resolution (subsection override for BS fields that have one,
    otherwise the section-type's own section) - see ocr_pipeline.py."""

    section_type = ocr_pipeline.FIELD_SECTION.get(field, "OTHER")
    subsection_name = ocr_pipeline.FIELD_SUBSECTION.get(field)

    if subsection_name and subsection_name in structure["bs_subsections"]:
        section = structure["bs_subsections"][subsection_name]
    else:
        section = structure["section_by_type"].get(section_type)

    header_info = structure["header_by_type"].get(section_type)
    current_x, previous_x, column_source = structure["columns_by_type"].get(
        section_type, (None, None, "unavailable")
    )

    return section, header_info, current_x, previous_x, column_source


def _clean(text):
    """Strips the OCR line-number/serial/sub-item prefix for display to
    a human or the model - never changes what a line MEANS, only how
    much leading boilerplate it carries. Uses the layout-preserving
    variant (bounded whitespace consumption) since this module always
    works with layout-preserving lines, to keep column alignment
    legible in what's shown."""
    return ocr_pipeline.strip_line_number_prefix_layout(text).rstrip()


_RAW_VALUE_RE = re.compile(r"\(?-?\d[\d,]*\.?\d*\)?%?")


def _raw_values_on_line(text):
    """Every number-shaped token visible on a line, exactly as printed
    (commas, parens-for-negative, percent sign kept) - shown to the
    model as "what's actually there", never the deterministic pipeline's
    own parsed/selected fy_current/fy_previous."""
    return _RAW_VALUE_RE.findall(_clean(text))


def _field_context(field, field_data, layout_lines, structure, meta):
    """Builds the table-aware context for one field: the located
    section header (with its own resolved year-to-column mapping), the
    field's own source row, a small window of nearby rows, and the
    deterministic pipeline's own current/previous assignment - so the
    model judges against real, located structure instead of being asked
    to infer table layout from an arbitrary slice of OCR text.

    Returns (context_for_prompt, context_line_numbers, context_text):
      context_for_prompt   - the JSON-serializable dict sent to the model
      context_line_numbers - the set of line numbers actually shown to
                             it (for the grounded-evidence check below)
      context_text         - the concatenated, prefix-stripped text of
                             everything shown (same purpose)
    """

    section, header_info, current_x, previous_x, column_source = _section_for_field(field, structure)

    current_label = meta.get("target_current_label") if meta else None
    previous_label = meta.get("target_previous_label") if meta else None

    context_line_numbers = set()
    context_text_parts = []

    table_header = None
    if header_info is not None and current_x is not None and previous_x is not None:
        header_line_idx = header_info["line_idx"]
        header_text = _clean(layout_lines[header_line_idx])
        # Left-to-right column order is derived from the ACTUAL resolved
        # offsets, never assumed - a filing may print either year first
        # (see test_extraction_audit.py Test 2c).
        if current_x <= previous_x:
            column_order = [f"Column 1 (leftmost) = {current_label}", f"Column 2 = {previous_label}"]
        else:
            column_order = [f"Column 1 (leftmost) = {previous_label}", f"Column 2 = {current_label}"]
        table_header = {
            "line_number": header_line_idx,
            "text": header_text,
            "column_order": column_order,
            "column_mapping_source": column_source,
        }
        context_line_numbers.add(header_line_idx)
        context_text_parts.append(header_text)

    target_row = None
    source_line_number = field_data.get("source_line_number")
    if source_line_number is not None:
        row_text = _clean(layout_lines[source_line_number])
        target_row = {
            "line_number": source_line_number,
            "text": row_text,
            "raw_values_found": _raw_values_on_line(row_text),
        }
        context_line_numbers.add(source_line_number)
        context_text_parts.append(row_text)

    nearby_rows = []
    if source_line_number is not None:
        start = max(0, source_line_number - CONTEXT_WINDOW)
        end = min(len(layout_lines), source_line_number + CONTEXT_WINDOW + 1)
        for i in range(start, end):
            if i == source_line_number:
                continue
            text = _clean(layout_lines[i])
            if not text:
                continue
            nearby_rows.append({"line_number": i, "text": text})
            context_line_numbers.add(i)
            context_text_parts.append(text)

    deterministic_status = field_data.get("validation_status")
    status_note = None
    if deterministic_status == "REVIEW_REQUIRED":
        status_note = (
            "The deterministic pipeline could NOT confidently resolve this row's "
            "year/column alignment and flagged it for review. Inspect the evidence "
            "carefully rather than assuming the mapping below is correct."
        )

    deterministic_mapping = {
        str(current_label): field_data.get("fy_current") if field_data.get("fy_current") is not None else "Not Available",
        str(previous_label): field_data.get("fy_previous") if field_data.get("fy_previous") is not None else "Not Available",
    }

    context_for_prompt = {
        "field": field,
        "aliases": ocr_pipeline.ALIASES.get(field, []),
        "deterministic_status": deterministic_status,
        "table_header": table_header,
        "target_row": target_row,
        "nearby_rows": nearby_rows,
        "deterministic_mapping": deterministic_mapping,
    }
    if status_note:
        context_for_prompt["status_note"] = status_note

    return context_for_prompt, context_line_numbers, " ".join(context_text_parts)


# ============================================================
# PROMPT BUILDING - MAIN VALIDATOR
# ============================================================

_INSTRUCTIONS = """You are a financial-statement extraction auditor.

You will be given a JSON array of FIELDS. Each one has: the field name a
deterministic extraction pipeline was trying to populate, its accepted
alternate names (aliases), the deterministic pipeline's own confidence
in this row ("deterministic_status": OK, or REVIEW_REQUIRED meaning it
could not confidently resolve the row itself), the statement table's
own HEADER row with its resolved column-to-fiscal-year mapping (or null
if no header could be located), the TARGET row (with every raw
number-shaped token actually printed on it), a few neighbouring rows for
context, and the deterministic pipeline's own current/previous-year
value assignment.

If "status_note" is present for a field, read it carefully before
judging - it means the deterministic pipeline was NOT confident about
this row and you should scrutinize the evidence rather than assume the
given mapping is correct.

Your ONLY job is to judge whether the deterministic mapping is correctly
matched to the field, the right source row, and the right fiscal-year
columns - based STRICTLY on the table_header/target_row/nearby_rows text
given to you. Do NOT invent, guess, recompute, or supply any financial
value of your own. Do NOT use outside knowledge about any company. If
the context given is not enough to decide confidently, say so rather
than guessing.

CRITICAL: every line number and every value you cite as evidence MUST
come from the table_header/target_row/nearby_rows you were actually
given for that field. Citing a line number that wasn't shown to you, or
a value that does not appear anywhere in the text you were given, will
be programmatically detected and will discard your judgement - so only
cite what you can actually see in the given text.

For EACH field, in the SAME ORDER given, return one object with EXACTLY
these keys and no others:
  "field": the exact field name as given (copy it verbatim)
  "status": exactly one of CONFIRMED, YEAR_MISMATCH, FIELD_MISMATCH, VALUE_SUSPICIOUS, NOT_FOUND_IN_SOURCE, INSUFFICIENT_CONTEXT
  "confidence": a number from 0.0 to 1.0
  "evidence_line_numbers": a list of the line numbers (integers, from
      table_header/target_row/nearby_rows ONLY) that support your status
  "evidence_values": a list of the actual value(s) (as printed - e.g.
      "30", "(14)", "4,831") from that same given text that support your
      status
  "explanation": one or two sentences, citing the source text you were given
  "requires_review": true or false

Return ONLY a single JSON object of the shape {"results": [ ... ]} - a
JSON array of one such object per field, under the "results" key. No
other keys, no prose before or after it.

FIELDS:
"""


def _build_prompt(contexts):
    return _INSTRUCTIONS + json.dumps(contexts, indent=2, default=str)


# ============================================================
# RESPONSE SCHEMA VALIDATION - MAIN VALIDATOR
# ============================================================

_REQUIRED_RESULT_KEYS = {
    "field", "status", "confidence", "evidence_line_numbers",
    "evidence_values", "explanation", "requires_review",
}


def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _validate_batch_response(raw_text, expected_fields):
    """Parses and strictly validates one batch's raw response text.
    Returns {field: {status, confidence, explanation, requires_review,
    evidence_line_numbers, evidence_values}} for every field that
    validated successfully - any field that is missing, malformed,
    names a status outside VALIDATION_STATUSES, or isn't one of
    `expected_fields` is simply absent from the returned dict (never
    raises, never guesses a value in its place). Evidence GROUNDING
    (whether the cited line numbers/values are real) is checked
    separately by _ground_evidence() - this function only checks shape."""

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
        if not _REQUIRED_RESULT_KEYS.issubset(item.keys()):
            continue

        field = item.get("field")
        if field not in expected_fields:
            continue

        status = item.get("status")
        if status not in VALIDATION_STATUSES:
            continue

        confidence = item.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        if not (0.0 <= float(confidence) <= 1.0):
            continue

        requires_review = item.get("requires_review")
        if not isinstance(requires_review, bool):
            continue

        explanation = item.get("explanation")
        if not isinstance(explanation, str):
            continue

        evidence_line_numbers = item.get("evidence_line_numbers")
        if not isinstance(evidence_line_numbers, list):
            continue
        if not all(_is_number(n) for n in evidence_line_numbers):
            continue

        evidence_values = item.get("evidence_values")
        if not isinstance(evidence_values, list):
            continue
        if not all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in evidence_values):
            continue

        validated[field] = {
            "status": status,
            "confidence": float(confidence),
            "explanation": explanation,
            "requires_review": requires_review,
            "evidence_line_numbers": [int(n) for n in evidence_line_numbers],
            "evidence_values": list(evidence_values),
        }

    return validated


# ============================================================
# GROUNDED EVIDENCE CHECK (Phase 2D)
# ============================================================

def _normalize_number_str(value):
    s = str(value).strip()
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace("%", "").strip()
    if negative and not s.startswith("-"):
        s = "-" + s
    return s


def _value_appears_in_text(value, text_norm):
    """`text_norm` is the context text with only commas stripped (see
    _ground_evidence) - a parenthesized negative like "(6,067.14)" is
    still literally "(6067.14)" there, never rewritten to a leading
    minus sign. _normalize_number_str() DOES rewrite it to "-6067.14"
    (a cleaner form for numeric comparison/float parsing) - so the
    candidate set must include the parenthesized form too, or a
    genuinely-matching parenthesized figure would never be found."""
    s = _normalize_number_str(value)
    if not s:
        return False
    negative = s.startswith("-")
    magnitude = s[1:] if negative else s
    candidates = {s, magnitude}
    if negative:
        candidates.add(f"({magnitude})")
    try:
        f = float(s)
        int_str = str(int(f)) if f == int(f) else str(f)
        candidates.add(int_str)
        abs_int_str = int_str[1:] if int_str.startswith("-") else int_str
        candidates.add(abs_int_str)
        if negative:
            candidates.add(f"({abs_int_str})")
    except ValueError:
        pass
    return any(c and c in text_norm for c in candidates)


def _ground_evidence(status, evidence_line_numbers, evidence_values, context_line_numbers, context_text):
    """Verifies that every cited line number was actually shown to the
    model for this field, and every cited value actually appears
    (allowing for comma/parenthesis/percent formatting differences) in
    the text this module gave it. Returns True/False, or None when the
    status is inherently evidence-free (nothing to point at when the AI
    itself says it found nothing or couldn't decide)."""

    if status not in _EVIDENCE_REQUIRED_STATUSES:
        return None

    if not evidence_line_numbers:
        return False

    if not all(int(n) in context_line_numbers for n in evidence_line_numbers):
        return False

    text_norm = context_text.replace(",", "")
    if evidence_values and not all(_value_appears_in_text(v, text_norm) for v in evidence_values):
        return False

    return True


# ============================================================
# NOT_FOUND SECONDARY VALIDATION - "field presence check" (Phase 2D)
#
# A field the deterministic pipeline could not locate at all is NEVER
# sent through the main validator above (there is no extracted value to
# judge). Phase 2C's live test showed this leaves an entire class of
# real issue invisible - a value present in the source under different
# wording than any configured alias. This secondary mode answers ONLY
# "does a plausible match for this field's label seem to exist nearby" -
# it is structurally incapable of proposing a value (see
# FieldPresenceResult and the schema check below): the AI must not
# invent missing financial values, so it is never even given the
# opportunity to state one here.
# ============================================================

_PRESENCE_INSTRUCTIONS = """You are checking whether a financial-statement field that a
deterministic extraction pipeline could NOT locate might actually be
present in the document under different wording or in a different row
layout than the pipeline's configured aliases expect.

You will be given a JSON array of FIELDS the pipeline did not find. Each
one has: the field name, its accepted alternate names (aliases), the
statement section it belongs to, and a block of OCR text from that
section (with each line's number).

Your ONLY job is to say whether a plausible match for the field's LABEL
might exist somewhere in the given text - NEVER to read off, state,
estimate, or propose any number, amount, or financial value for it,
under any circumstance, even if you can clearly see one sitting right
next to a matching label. This is a location check only.

For EACH field, in the SAME ORDER given, return one object with EXACTLY
these keys and no others:
  "field": the exact field name as given (copy it verbatim)
  "status": exactly one of FOUND_POSSIBLE_MATCH, NOT_FOUND_IN_SOURCE, INSUFFICIENT_CONTEXT
  "confidence": a number from 0.0 to 1.0
  "candidate_line_numbers": a list of line numbers (integers, from the
      text you were given) where a possible label match might be - an
      empty list if status is not FOUND_POSSIBLE_MATCH
  "explanation": one or two sentences describing the WORDING you found
      (never a number/amount)

Return ONLY {"results": [ ... ]}. No other keys, no prose, and your
response must NEVER contain a numeric financial value anywhere.

FIELDS:
"""

_PRESENCE_REQUIRED_KEYS = {"field", "status", "confidence", "candidate_line_numbers", "explanation"}

# Extra safety net for the "must not propose a value" rule: even a
# structurally-valid-looking presence-check entry is rejected outright
# if it also carries any key that looks like it's smuggling in a value.
_PRESENCE_FORBIDDEN_KEYS = {
    "value", "values", "amount", "amounts", "figure", "figures",
    "fy_current", "fy_previous", "current_value", "previous_value",
    "extracted_value", "number", "numbers",
}


def _presence_context(field, section_type, layout_lines, section):
    text_lines = []
    if section is not None:
        for i in range(section["start"], section["end"]):
            text = _clean(layout_lines[i])
            if text:
                text_lines.append({"line_number": i, "text": text})
    return {
        "field": field,
        "aliases": ocr_pipeline.ALIASES.get(field, []),
        "section": section_type,
        "section_text": text_lines,
    }


def _build_presence_prompt(contexts):
    return _PRESENCE_INSTRUCTIONS + json.dumps(contexts, indent=2, default=str)


def _validate_presence_response(raw_text, expected_fields):
    """Same never-raises, never-guesses posture as
    _validate_batch_response() above, PLUS a hard rejection of any
    entry carrying a value-shaped key - see _PRESENCE_FORBIDDEN_KEYS."""

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

        if _PRESENCE_FORBIDDEN_KEYS.intersection(item.keys()):
            continue

        if not _PRESENCE_REQUIRED_KEYS.issubset(item.keys()):
            continue

        field = item.get("field")
        if field not in expected_fields:
            continue

        status = item.get("status")
        if status not in PRESENCE_STATUSES:
            continue

        confidence = item.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            continue
        if not (0.0 <= float(confidence) <= 1.0):
            continue

        explanation = item.get("explanation")
        if not isinstance(explanation, str):
            continue

        candidate_line_numbers = item.get("candidate_line_numbers")
        if not isinstance(candidate_line_numbers, list):
            continue
        if not all(_is_number(n) for n in candidate_line_numbers):
            continue

        validated[field] = {
            "status": status,
            "confidence": float(confidence),
            "explanation": explanation,
            "candidate_line_numbers": [int(n) for n in candidate_line_numbers],
        }

    return validated


# ============================================================
# ORCHESTRATION
# ============================================================

def _chunk(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _section_of(field):
    return ocr_pipeline.FIELD_SECTION.get(field, "OTHER")


def _group_by_section(fields):
    grouped = {}
    for f in fields:
        grouped.setdefault(_section_of(f), []).append(f)
    return grouped


def _budget_exhausted(deadline):
    return time.monotonic() >= deadline


def validate_extracted_fields(extracted, lines, meta=None, model=DEFAULT_MODEL,
                               base_url=OLLAMA_BASE_URL, layout_lines=None):
    """Top-level entry point. `extracted` and `lines` are exactly what
    ocr_pipeline.extract_financial_values() already produces - this
    function reads them, never mutates them, and never returns anything
    that could be mistaken for a replacement value. `layout_lines` is
    the LAYOUT-PRESERVING counterpart (see ocr_pipeline's own
    _normalize_preserve_layout) needed to re-locate table headers and
    columns - if omitted, falls back to `lines` (header/column
    re-detection may then fail to find a header on a multi-column table,
    same as if none had been found; every other part of this function
    still works).

    Returns (results, presence_results, availability):
      results:           {field: AIValidationResult} - one entry for
                         every field extract_financial_values() did NOT
                         mark NOT_FOUND (always present, never partial).
      presence_results:  {field: FieldPresenceResult} - one entry for
                         every field that WAS marked NOT_FOUND (always
                         present, never partial).
      availability:      the OllamaAvailability this run observed.

    Batches fields (by statement section, capped at MAX_FIELDS_PER_BATCH
    per call - see module docstring) rather than one call per field or
    one call per whole section, and stops starting new batches once
    TIME_BUDGET_SECONDS has elapsed - any batch not yet started when
    that happens is marked AI_SKIPPED with a clear reason, while every
    batch already completed keeps its real result (graceful partial
    completion, never discarded)."""

    if layout_lines is None:
        layout_lines = lines

    availability = check_ollama_availability(model=model, base_url=base_url)
    results = {}
    presence_results = {}

    if not availability.reachable or not availability.model_installed:
        for field, field_data in extracted.items():
            if field_data.get("validation_status") == "NOT_FOUND":
                presence_results[field] = FieldPresenceResult(
                    field=field, technical_status=AI_UNAVAILABLE,
                    presence_status=None, confidence=None,
                    explanation=availability.detail,
                )
            else:
                results[field] = AIValidationResult(
                    field=field, technical_status=AI_UNAVAILABLE,
                    validation_status=None, confidence=None,
                    explanation=availability.detail, requires_review=None,
                    fy_current=field_data.get("fy_current"),
                    fy_previous=field_data.get("fy_previous"),
                    year_current_label=(meta or {}).get("target_current_label"),
                    year_previous_label=(meta or {}).get("target_previous_label"),
                    source_line_number=field_data.get("source_line_number"),
                    source_text=field_data.get("source_text"),
                )
        return results, presence_results, availability

    structure = _resolve_document_structure(lines, layout_lines, meta or {})

    sendable_fields = [f for f, ed in extracted.items() if ed.get("validation_status") != "NOT_FOUND"]
    not_found_fields = [f for f, ed in extracted.items() if ed.get("validation_status") == "NOT_FOUND"]

    # Prioritise deterministically-uncertain fields first, so if the
    # time budget runs out, the fields most worth a second opinion were
    # the ones actually validated.
    review_required_fields = [f for f in sendable_fields if extracted[f].get("validation_status") == "REVIEW_REQUIRED"]
    ok_fields = [f for f in sendable_fields if f not in set(review_required_fields)]

    main_batches = []
    for fields in (review_required_fields,):
        for section, section_fields in _group_by_section(fields).items():
            main_batches.extend(list(_chunk(section_fields, MAX_FIELDS_PER_BATCH)))
    for section, section_fields in _group_by_section(ok_fields).items():
        main_batches.extend(list(_chunk(section_fields, MAX_FIELDS_PER_BATCH)))

    presence_batches = []
    for section, section_fields in _group_by_section(not_found_fields).items():
        presence_batches.extend([(section, chunk) for chunk in _chunk(section_fields, MAX_FIELDS_PER_BATCH)])

    deadline = time.monotonic() + TIME_BUDGET_SECONDS
    budget_note = (
        f"Skipped: document-level AI validation time budget "
        f"({TIME_BUDGET_SECONDS:.0f}s) was exhausted before this batch could be started."
    )

    for batch_fields in main_batches:
        if _budget_exhausted(deadline):
            for f in batch_fields:
                ed = extracted[f]
                results[f] = AIValidationResult(
                    field=f, technical_status=AI_SKIPPED,
                    validation_status=None, confidence=None,
                    explanation=budget_note, requires_review=None,
                    fy_current=ed.get("fy_current"), fy_previous=ed.get("fy_previous"),
                    year_current_label=(meta or {}).get("target_current_label"),
                    year_previous_label=(meta or {}).get("target_previous_label"),
                    source_line_number=ed.get("source_line_number"),
                    source_text=ed.get("source_text"),
                )
            continue

        field_contexts = {}
        contexts_for_prompt = []
        for f in batch_fields:
            ctx, line_numbers, text = _field_context(f, extracted[f], layout_lines, structure, meta or {})
            field_contexts[f] = (line_numbers, text)
            contexts_for_prompt.append(ctx)

        prompt = _build_prompt(contexts_for_prompt)

        try:
            raw = call_ollama(prompt, model=model, base_url=base_url)
        except OllamaCallError as exc:
            for f in batch_fields:
                ed = extracted[f]
                results[f] = AIValidationResult(
                    field=f, technical_status=AI_ERROR,
                    validation_status=None, confidence=None,
                    explanation=f"{provider_display_name()} call failed: {exc}", requires_review=None,
                    fy_current=ed.get("fy_current"), fy_previous=ed.get("fy_previous"),
                    year_current_label=(meta or {}).get("target_current_label"),
                    year_previous_label=(meta or {}).get("target_previous_label"),
                    source_line_number=ed.get("source_line_number"),
                    source_text=ed.get("source_text"),
                )
            continue

        validated = _validate_batch_response(raw, set(batch_fields))

        for f in batch_fields:
            ed = extracted[f]
            common_kwargs = dict(
                fy_current=ed.get("fy_current"), fy_previous=ed.get("fy_previous"),
                year_current_label=(meta or {}).get("target_current_label"),
                year_previous_label=(meta or {}).get("target_previous_label"),
                source_line_number=ed.get("source_line_number"),
                source_text=ed.get("source_text"),
            )

            if f not in validated:
                results[f] = AIValidationResult(
                    field=f, technical_status=AI_ERROR,
                    validation_status=None, confidence=None,
                    explanation=f"{provider_display_name()}'s response did not include a valid, schema-conforming result for this field.",
                    requires_review=None, **common_kwargs,
                )
                continue

            v = validated[f]
            line_numbers, text = field_contexts[f]
            grounded = _ground_evidence(
                v["status"], v["evidence_line_numbers"], v["evidence_values"], line_numbers, text
            )

            confidence = v["confidence"]
            requires_review = v["requires_review"]
            explanation = v["explanation"]
            if grounded is False:
                confidence = min(confidence, 0.3)
                requires_review = True
                explanation = (
                    "[AI evidence could not be verified against the supplied context - "
                    "treat with caution] " + explanation
                )

            results[f] = AIValidationResult(
                field=f, technical_status=AI_COMPLETED,
                validation_status=v["status"], confidence=confidence,
                explanation=explanation, requires_review=requires_review,
                evidence_line_numbers=tuple(v["evidence_line_numbers"]),
                evidence_values=tuple(v["evidence_values"]),
                evidence_grounded=grounded,
                **common_kwargs,
            )

    for section, batch_fields in presence_batches:
        if _budget_exhausted(deadline):
            for f in batch_fields:
                presence_results[f] = FieldPresenceResult(
                    field=f, technical_status=AI_SKIPPED,
                    presence_status=None, confidence=None, explanation=budget_note,
                )
            continue

        section_obj = structure["section_by_type"].get(section)
        contexts_for_prompt = [
            _presence_context(f, section, layout_lines, section_obj) for f in batch_fields
        ]
        prompt = _build_presence_prompt(contexts_for_prompt)

        try:
            raw = call_ollama(prompt, model=model, base_url=base_url)
        except OllamaCallError as exc:
            for f in batch_fields:
                presence_results[f] = FieldPresenceResult(
                    field=f, technical_status=AI_ERROR,
                    presence_status=None, confidence=None,
                    explanation=f"{provider_display_name()} call failed: {exc}",
                )
            continue

        validated = _validate_presence_response(raw, set(batch_fields))

        for f in batch_fields:
            if f not in validated:
                presence_results[f] = FieldPresenceResult(
                    field=f, technical_status=AI_ERROR,
                    presence_status=None, confidence=None,
                    explanation=f"{provider_display_name()}'s response did not include a valid, schema-conforming presence result for this field.",
                )
                continue

            v = validated[f]
            presence_results[f] = FieldPresenceResult(
                field=f, technical_status=AI_COMPLETED,
                presence_status=v["status"], confidence=v["confidence"],
                explanation=v["explanation"],
                candidate_line_numbers=tuple(v["candidate_line_numbers"]),
            )

    return results, presence_results, availability
