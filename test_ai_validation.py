"""
Unit tests for ai_validation.py (Phase 2B, strengthened in Phase 2D).

Ollama itself (check_ollama_availability / call_ollama) is monkeypatched
with plain functions/lambdas throughout - no mocking framework, matching
this codebase's own convention (a module-level function saved, replaced,
and restored in a finally block). No real network or Ollama call is ever
made by this file.

Phase 2D added:
  - Table-aware context: _resolve_document_structure/_field_context now
    locate the real section header and year-to-column mapping (via
    ocr_pipeline's own existing public functions, called a second time -
    never duplicated/modified) instead of a blind +/- line window.
  - A grounded-evidence contract: every main-validator result must cite
    real line numbers/values from what it was actually given; unverifiable
    citations are downgraded, never trusted at face value.
  - A separate, narrow "field presence check" for deterministically
    NOT_FOUND fields - structurally incapable of proposing a value.
  - Bounded batching (MAX_FIELDS_PER_BATCH) and a document-level time
    budget (TIME_BUDGET_SECONDS) with graceful partial completion.

Run: python test_ai_validation.py
"""

import dataclasses
import json
import re

import ai_validation as av
import ocr_pipeline as op

_FAILURES = []


def _ok(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        _FAILURES.append(msg)


# ============================================================
# Synthetic OCR fixtures (hand-built, matching test_extraction_audit.py's
# own established _bs_header/_row layout-preserving pattern) - never
# real borrower data, so these tests never depend on any cached PDF
# OCR text existing on the machine running them.
# ============================================================

def _bs_header(year_order=("2026", "2025")):
    y1, y2 = year_order
    return [
        "Statement of Standalone Audited Balance Sheet as at 31 March, 2026",
        "Particulars" + " " * 49 + "As at".rjust(18) + "As at".rjust(18),
        " " * 60 + "31".rjust(18) + "31".rjust(18),
        " " * 60 + "March,".rjust(18) + "March,".rjust(18),
        " " * 60 + y1.rjust(18) + y2.rjust(18),
        "I. ASSETS",
        "(2) Current assets",
    ]


def _row(label, current, previous, label_width=60, col_width=18):
    cur_str = "" if current is None else str(current)
    prev_str = "" if previous is None else str(previous)
    return f"{label:<{label_width}}{cur_str:>{col_width}}{prev_str:>{col_width}}"


def _build_lines_for_ai(text):
    """Exactly mirrors ocr_upload.py's own construction - the SAME two
    index-aligned lists (collapsed + layout-preserving) validate_
    extracted_fields() expects."""
    raw_and_normalized = [(raw, op.normalize_text(raw)) for raw in text.splitlines()]
    raw_and_normalized = [(raw, norm) for raw, norm in raw_and_normalized if norm]
    lines = [norm for _raw, norm in raw_and_normalized]
    layout_lines = [op._normalize_preserve_layout(raw) for raw, _norm in raw_and_normalized]
    return lines, layout_lines


def _ok_fixture():
    """A small, unambiguous BS-shaped document: Cash has only its
    FY-previous figure visible (the real, reported Tata Chemicals
    pattern - see test_extraction_audit.py Test 2b), Bank balances and
    Total assets both have clean two-year figures. No PNL/CF/RATIO
    section exists at all, so every field belonging to those sections
    is genuinely NOT_FOUND - useful, for free, for presence-check tests."""
    lines = _bs_header() + [
        _row("    (iii) Cash and cash equivalents", None, 30),
        _row("    (iv) Bank balances other than (iii) above", 37, 41),
        _row("Total assets", 2000, 1800),
    ]
    text = "\n".join(lines)
    extracted, debug_rows, meta = op.extract_financial_values(text, op.TEMPLATE_FILE)
    ai_lines, ai_layout_lines = _build_lines_for_ai(text)
    return extracted, meta, ai_lines, ai_layout_lines


def _ambiguous_fixture():
    """Same shape as test_extraction_audit.py's own Test 5: a lone
    figure sitting almost exactly halfway between the section's two
    calibrated column positions - genuinely REVIEW_REQUIRED."""
    lines = _bs_header() + [
        _row("    (i) Investments", 100, 90),
        _row("    (ii) Trade receivables", 250, 200),
        _row("    (iv) Bank balances other than (iii) above", 37, 41),
        _row("Total current assets", 387, 331),
        "    (iii) Cash and cash equivalents" + " " * 48 + "45",
        _row("Total assets", 2000, 1800),
    ]
    text = "\n".join(lines)
    extracted, debug_rows, meta = op.extract_financial_values(text, op.TEMPLATE_FILE)
    assert extracted["Cash"]["validation_status"] == "REVIEW_REQUIRED", "fixture must produce an ambiguous Cash row"
    ai_lines, ai_layout_lines = _build_lines_for_ai(text)
    return extracted, meta, ai_lines, ai_layout_lines


_FIELD_RE = re.compile(r'"field":\s*"([^"]*)"')


def _fields_in_prompt(prompt):
    return _FIELD_RE.findall(prompt.split("FIELDS:\n", 1)[1])


def _grounded_confirmed_response(prompt, extracted):
    """Builds a schema-valid, HONESTLY-grounded CONFIRMED response for
    every field the prompt actually asked about, citing that field's own
    real source_line_number and one of its own real fy_current/
    fy_previous values - i.e. a "happy path" mock that should always
    ground successfully, regardless of exactly how fields were batched."""
    results = []
    for f in _fields_in_prompt(prompt):
        ed = extracted[f]
        value = ed["fy_current"] if ed["fy_current"] is not None else ed["fy_previous"]
        results.append({
            "field": f, "status": "CONFIRMED", "confidence": 0.9,
            "evidence_line_numbers": [ed["source_line_number"]],
            "evidence_values": [value],
            "explanation": "matches the source row", "requires_review": False,
        })
    return json.dumps({"results": results})


# ============================================================
# check_ollama_availability() - mocks requests.get
# (unchanged behaviour from Phase 2B - retained as regression coverage)
# ============================================================

class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text

    def json(self):
        if self._json_body is None:
            raise ValueError("no json")
        return self._json_body


def test_check_ollama_availability():
    print("=" * 100)
    print("check_ollama_availability()")
    print("=" * 100)

    import requests
    real_get = requests.get

    try:
        requests.get = lambda *a, **k: _FakeResponse(200, {"models": [{"name": "qwen2.5:7b-instruct"}]})
        result = av.check_ollama_availability(model="qwen2.5:7b-instruct")
        _ok(result.reachable and result.model_installed, "exact model name match -> reachable + installed")

        requests.get = lambda *a, **k: _FakeResponse(200, {"models": [{"name": "qwen2.5:7b-instruct"}]})
        result = av.check_ollama_availability(model="llama3.2")
        _ok(result.reachable and not result.model_installed, "configured model not in installed list -> not installed")

        requests.get = lambda *a, **k: _FakeResponse(200, {"models": [{"name": "llama3.2:latest"}]})
        result = av.check_ollama_availability(model="llama3.2")
        _ok(result.reachable and result.model_installed, "bare name matches an installed ':latest' tag")

        def _raise(*a, **k):
            raise requests.exceptions.ConnectionError("refused")
        requests.get = _raise
        result = av.check_ollama_availability()
        _ok(not result.reachable and "ollama serve" in result.detail, "connection error -> unreachable, clear detail")

        requests.get = lambda *a, **k: _FakeResponse(500, text="boom")
        result = av.check_ollama_availability()
        _ok(not result.reachable, "non-200 response -> unreachable")

        requests.get = lambda *a, **k: _FakeResponse(200, json_body=None)
        result = av.check_ollama_availability()
        _ok(not result.reachable, "unparseable JSON -> unreachable, never raises")
    finally:
        requests.get = real_get


# ============================================================
# call_ollama() - mocks requests.post
# (unchanged behaviour from Phase 2B - retained as regression coverage)
# ============================================================

def test_call_ollama():
    print()
    print("=" * 100)
    print("call_ollama()")
    print("=" * 100)

    import requests
    real_post = requests.post

    try:
        requests.post = lambda *a, **k: _FakeResponse(200, {"response": '{"results": []}'})
        out = av.call_ollama("prompt")
        _ok(out == '{"results": []}', "happy path returns the raw response text")

        requests.post = lambda *a, **k: _FakeResponse(404, text="not found")
        try:
            av.call_ollama("prompt")
            _ok(False, "non-200 should raise OllamaCallError")
        except av.OllamaCallError:
            _ok(True, "non-200 response raises OllamaCallError")

        def _raise(*a, **k):
            raise requests.exceptions.Timeout("slow")
        requests.post = _raise
        try:
            av.call_ollama("prompt")
            _ok(False, "timeout should raise OllamaCallError")
        except av.OllamaCallError:
            _ok(True, "requests.exceptions.Timeout is wrapped as OllamaCallError")
    finally:
        requests.post = real_post


# ============================================================
# Table-aware context (Phase 2D)
# ============================================================

def test_table_aware_context():
    print()
    print("=" * 100)
    print("TABLE-AWARE CONTEXT - header/column selection, FY labels, deterministic mapping")
    print("=" * 100)

    extracted, meta, lines, layout_lines = _ok_fixture()
    structure = av._resolve_document_structure(lines, layout_lines, meta)

    _ok(structure["header_by_type"]["BS"] is not None, "BS header located for the synthetic document")
    _ok(structure["columns_by_type"]["BS"][2] == "header",
        f"columns resolved FROM the header (not a data-median fallback) - got {structure['columns_by_type']['BS'][2]!r}")

    ctx, ctx_lines, ctx_text = av._field_context("Cash", extracted["Cash"], layout_lines, structure, meta)

    _ok(ctx["table_header"] is not None, "Cash's context includes a located table header")
    header_line_idx = structure["header_by_type"]["BS"]["line_idx"]
    _ok(ctx["table_header"]["line_number"] == header_line_idx,
        f"table_header cites the ACTUAL header line index ({header_line_idx}), got {ctx['table_header']['line_number']}")
    _ok(header_line_idx in ctx_lines, "the header's line number is part of the context lines shown to the model")

    _ok(meta["target_current_label"] in json.dumps(ctx["table_header"]["column_order"]),
        f"fiscal-year CURRENT label ({meta['target_current_label']!r}) is explicitly present in the column mapping")
    _ok(meta["target_previous_label"] in json.dumps(ctx["table_header"]["column_order"]),
        f"fiscal-year PREVIOUS label ({meta['target_previous_label']!r}) is explicitly present in the column mapping")

    _ok(ctx["deterministic_mapping"][meta["target_current_label"]] == "Not Available",
        "deterministic_mapping shows 'Not Available' for Cash's genuinely-blank FY-current cell")
    _ok(ctx["deterministic_mapping"][meta["target_previous_label"]] == 30.0,
        f"deterministic_mapping shows the real FY-previous value (30), got {ctx['deterministic_mapping'][meta['target_previous_label']]!r}")

    _ok(ctx["target_row"]["raw_values_found"] == ["30"],
        f"raw_values_found shows exactly what's printed on the row (['30']), got {ctx['target_row']['raw_values_found']!r}")

    _ok("status_note" not in ctx, "an OK-status field's context carries no uncertainty caveat")

    # Reversed header order (previous-year printed first/left) - column
    # mapping must follow the header's OWN order, never assume current-first.
    lines2 = _bs_header(("2025", "2026")) + [_row("Total assets", 2000, 1800)]
    text2 = "\n".join(lines2)
    extracted2, _debug2, meta2 = op.extract_financial_values(text2, op.TEMPLATE_FILE)
    ai_lines2, ai_layout_lines2 = _build_lines_for_ai(text2)
    structure2 = av._resolve_document_structure(ai_lines2, ai_layout_lines2, meta2)
    ctx2, _lines2, _text2 = av._field_context("Total assets", extracted2["Total assets"], ai_layout_lines2, structure2, meta2)
    order_text = json.dumps(ctx2["table_header"]["column_order"])
    current_pos = order_text.index(meta2["target_current_label"])
    previous_pos = order_text.index(meta2["target_previous_label"])
    _ok(previous_pos < current_pos,
        "with a previous-year-first header, column_order correctly puts the PREVIOUS label first, not assumed current-first")


def test_review_required_marked_uncertain_in_prompt():
    print()
    print("=" * 100)
    print("REVIEW_REQUIRED fields are explicitly marked uncertain in the prompt")
    print("=" * 100)

    extracted, meta, lines, layout_lines = _ambiguous_fixture()
    structure = av._resolve_document_structure(lines, layout_lines, meta)

    ctx_ok, _l1, _t1 = av._field_context("Total assets", extracted["Total assets"], layout_lines, structure, meta)
    ctx_review, _l2, _t2 = av._field_context("Cash", extracted["Cash"], layout_lines, structure, meta)

    _ok(extracted["Cash"]["validation_status"] == "REVIEW_REQUIRED", "fixture's Cash row is genuinely REVIEW_REQUIRED")
    _ok("status_note" in ctx_review, "REVIEW_REQUIRED field's context carries an explicit uncertainty note")
    _ok("status_note" not in ctx_ok, "an OK field in the SAME document carries no such note")

    prompt = av._build_prompt([ctx_review])
    _ok("carefully" in prompt.lower() or "scrutin" in prompt.lower(),
        "the built prompt for a REVIEW_REQUIRED field explicitly instructs closer scrutiny")


# ============================================================
# Grounded evidence (Phase 2D)
# ============================================================

def test_ground_evidence():
    print()
    print("=" * 100)
    print("_ground_evidence() - hallucinated citations are detected")
    print("=" * 100)

    context_lines = {10, 11, 12}
    context_text = "Total assets 2,000 1,800"

    _ok(av._ground_evidence("CONFIRMED", [11], ["2,000"], context_lines, context_text) is True,
        "a real, in-context line number and value grounds successfully")
    _ok(av._ground_evidence("CONFIRMED", [11], ["2000"], context_lines, context_text) is True,
        "value comparison tolerates comma formatting differences")
    _ok(av._ground_evidence("CONFIRMED", [999], ["2,000"], context_lines, context_text) is False,
        "a line number never shown to the model fails grounding")
    _ok(av._ground_evidence("CONFIRMED", [11], ["469"], context_lines, context_text) is False,
        "a value that appears nowhere in the given text fails grounding (the real Phase 2C hallucination shape)")
    _ok(av._ground_evidence("CONFIRMED", [], [], context_lines, context_text) is False,
        "an affirmative status with NO cited evidence at all fails grounding")
    _ok(av._ground_evidence("NOT_FOUND_IN_SOURCE", [], [], context_lines, context_text) is None,
        "NOT_FOUND_IN_SOURCE is evidence-exempt (nothing to point at) - not graded True/False")
    _ok(av._ground_evidence("INSUFFICIENT_CONTEXT", [], [], context_lines, context_text) is None,
        "INSUFFICIENT_CONTEXT is evidence-exempt")


def test_end_to_end_hallucinated_evidence_is_downgraded():
    print()
    print("=" * 100)
    print("End-to-end: a hallucinated citation is downgraded, not trusted")
    print("=" * 100)

    extracted, meta, lines, layout_lines = _ok_fixture()
    real_check = av.check_ollama_availability
    real_call = av.call_ollama

    def _hallucinating_call(prompt, model=None, base_url=None, timeout=None):
        results = []
        for f in _fields_in_prompt(prompt):
            if f == "Total assets":
                results.append({
                    "field": f, "status": "YEAR_MISMATCH", "confidence": 0.8,
                    "evidence_line_numbers": [extracted[f]["source_line_number"]],
                    "evidence_values": ["469"],  # never appears anywhere in the fixture
                    "explanation": "expected year 469", "requires_review": False,
                })
            else:
                ed = extracted[f]
                value = ed["fy_current"] if ed["fy_current"] is not None else ed["fy_previous"]
                results.append({
                    "field": f, "status": "CONFIRMED", "confidence": 0.9,
                    "evidence_line_numbers": [ed["source_line_number"]], "evidence_values": [value],
                    "explanation": "ok", "requires_review": False,
                })
        return json.dumps({"results": results})

    av.check_ollama_availability = lambda **k: av.OllamaAvailability(True, True, ["qwen2.5:7b-instruct"], "")
    av.call_ollama = _hallucinating_call

    try:
        results, presence_results, _availability = av.validate_extracted_fields(extracted, lines, meta, layout_lines=layout_lines)
    finally:
        av.check_ollama_availability = real_check
        av.call_ollama = real_call

    r = results["Total assets"]
    _ok(r.validation_status == "YEAR_MISMATCH", "the AI's own status is still reported (not silently discarded)")
    _ok(r.evidence_grounded is False, "evidence_grounded is explicitly False when the citation can't be verified")
    _ok(r.requires_review is True, "requires_review is forced True when evidence is ungrounded")
    _ok(r.confidence <= 0.3, f"confidence is downgraded to a low ceiling, got {r.confidence}")
    _ok("could not be verified" in r.explanation.lower(), "the explanation records that the evidence was ungrounded")

    r_cash = results["Cash"]
    _ok(r_cash.evidence_grounded is True, "a genuinely grounded field is NOT downgraded")
    _ok(r_cash.confidence == 0.9, f"a grounded field keeps its real confidence, got {r_cash.confidence}")


# ============================================================
# NOT_FOUND secondary "field presence check" (Phase 2D)
# ============================================================

def test_presence_check_cannot_carry_a_value():
    print()
    print("=" * 100)
    print("Field presence check - structurally cannot propose a financial value")
    print("=" * 100)

    field_names = {f.name for f in dataclasses.fields(av.FieldPresenceResult)}
    _ok(not ({"value", "values", "amount", "fy_current", "fy_previous"} & field_names),
        f"FieldPresenceResult has no value-carrying attribute at all - fields are {sorted(field_names)}")

    valid_payload = json.dumps({"results": [
        {"field": "Capex", "status": "FOUND_POSSIBLE_MATCH", "confidence": 0.6,
         "candidate_line_numbers": [5], "explanation": "a similarly-worded row appears nearby"},
    ]})
    validated = av._validate_presence_response(valid_payload, {"Capex"})
    _ok("Capex" in validated, "a well-formed presence response validates")

    smuggled_payload = json.dumps({"results": [
        {"field": "Capex", "status": "FOUND_POSSIBLE_MATCH", "confidence": 0.6,
         "candidate_line_numbers": [5], "explanation": "found it", "value": 1205},
    ]})
    validated = av._validate_presence_response(smuggled_payload, {"Capex"})
    _ok("Capex" not in validated,
        "a response smuggling in a 'value' key is REJECTED outright, even though every other key is well-formed")

    bad_status_payload = json.dumps({"results": [
        {"field": "Capex", "status": "CONFIRMED", "confidence": 0.6,
         "candidate_line_numbers": [5], "explanation": "x"},
    ]})
    validated = av._validate_presence_response(bad_status_payload, {"Capex"})
    _ok("Capex" not in validated, "a main-validator status (CONFIRMED) is not a valid presence status - rejected")


def test_end_to_end_not_found_goes_through_presence_check():
    print()
    print("=" * 100)
    print("End-to-end: NOT_FOUND fields are routed to the presence check, never the main validator")
    print("=" * 100)

    extracted, meta, lines, layout_lines = _ok_fixture()
    _ok(extracted["Revenue from operations"]["validation_status"] == "NOT_FOUND",
        "fixture sanity check: Revenue is genuinely NOT_FOUND (no PNL section in this synthetic document)")

    real_check = av.check_ollama_availability
    real_call = av.call_ollama

    def _fake_call(prompt, model=None, base_url=None, timeout=None):
        if "candidate_line_numbers" in prompt:
            fields = _FIELD_RE.findall(prompt.split("FIELDS:\n", 1)[1])
            return json.dumps({"results": [
                {"field": f, "status": "NOT_FOUND_IN_SOURCE", "confidence": 0.7,
                 "candidate_line_numbers": [], "explanation": "no similar wording found nearby"}
                for f in fields
            ]})
        return _grounded_confirmed_response(prompt, extracted)

    av.check_ollama_availability = lambda **k: av.OllamaAvailability(True, True, ["qwen2.5:7b-instruct"], "")
    av.call_ollama = _fake_call

    try:
        results, presence_results, _availability = av.validate_extracted_fields(extracted, lines, meta, layout_lines=layout_lines)
    finally:
        av.check_ollama_availability = real_check
        av.call_ollama = real_call

    _ok("Revenue from operations" not in results, "a NOT_FOUND field never appears in the main validator's results")
    _ok("Revenue from operations" in presence_results, "a NOT_FOUND field DOES appear in the presence-check results")
    _ok(presence_results["Revenue from operations"].presence_status == "NOT_FOUND_IN_SOURCE",
        "presence-check status is correctly reported")
    _ok("Cash" in results and "Cash" not in presence_results,
        "an OK field goes through the main validator, never the presence check")


# ============================================================
# Bounded batching / time budget / graceful partial completion (Phase 2D)
# ============================================================

def test_partial_batch_failure_does_not_discard_other_batches():
    print()
    print("=" * 100)
    print("A failed batch never discards results from other, successful batches")
    print("=" * 100)

    extracted, meta, lines, layout_lines = _ok_fixture()
    real_check = av.check_ollama_availability
    real_call = av.call_ollama
    real_max_batch = av.MAX_FIELDS_PER_BATCH

    call_number = {"n": 0}

    def _flaky_call(prompt, model=None, base_url=None, timeout=None):
        call_number["n"] += 1
        if call_number["n"] == 2:
            raise av.OllamaCallError("simulated mid-run failure")
        return _grounded_confirmed_response(prompt, extracted)

    av.check_ollama_availability = lambda **k: av.OllamaAvailability(True, True, ["qwen2.5:7b-instruct"], "")
    av.call_ollama = _flaky_call
    av.MAX_FIELDS_PER_BATCH = 1  # force one field per batch -> separate BS-field batches

    try:
        results, _presence, _availability = av.validate_extracted_fields(extracted, lines, meta, layout_lines=layout_lines)
    finally:
        av.check_ollama_availability = real_check
        av.call_ollama = real_call
        av.MAX_FIELDS_PER_BATCH = real_max_batch

    completed = [f for f, r in results.items() if r.technical_status == "AI_COMPLETED"]
    errored = [f for f, r in results.items() if r.technical_status == "AI_ERROR"]
    _ok(len(errored) >= 1, f"exactly the failing batch's field(s) are marked AI_ERROR, got errored={errored}")
    _ok(len(completed) >= 1,
        f"fields from OTHER, successful batches still show AI_COMPLETED with real results - got completed={completed}")


def test_time_budget_exhausted_skips_gracefully_without_calling_ollama():
    print()
    print("=" * 100)
    print("Document-level time budget: exhausted budget skips gracefully, never crashes")
    print("=" * 100)

    extracted, meta, lines, layout_lines = _ok_fixture()
    real_check = av.check_ollama_availability
    real_call = av.call_ollama
    real_budget = av.TIME_BUDGET_SECONDS

    def _should_not_be_called(*a, **k):
        raise AssertionError("call_ollama must never be invoked once the time budget is already exhausted")

    av.check_ollama_availability = lambda **k: av.OllamaAvailability(True, True, ["qwen2.5:7b-instruct"], "")
    av.call_ollama = _should_not_be_called
    av.TIME_BUDGET_SECONDS = 0.0

    try:
        results, presence_results, _availability = av.validate_extracted_fields(extracted, lines, meta, layout_lines=layout_lines)
    finally:
        av.check_ollama_availability = real_check
        av.call_ollama = real_call
        av.TIME_BUDGET_SECONDS = real_budget

    _ok(all(r.technical_status == "AI_SKIPPED" for r in results.values()),
        "every main-validator field is AI_SKIPPED when the budget is already exhausted")
    _ok(all("time budget" in r.explanation.lower() for r in results.values()),
        "the skip reason clearly names the time budget, not a generic failure")
    _ok(all(r.technical_status == "AI_SKIPPED" for r in presence_results.values()),
        "presence-check fields are ALSO gracefully skipped, not crashed or silently dropped")


def test_batch_size_is_capped():
    print()
    print("=" * 100)
    print("MAX_FIELDS_PER_BATCH bounds every call, even for a section with more fields")
    print("=" * 100)

    extracted, meta, lines, layout_lines = _ok_fixture()
    real_check = av.check_ollama_availability
    real_call = av.call_ollama
    real_max_batch = av.MAX_FIELDS_PER_BATCH

    batch_sizes = []

    def _recording_call(prompt, model=None, base_url=None, timeout=None):
        batch_sizes.append(len(_fields_in_prompt(prompt)))
        return _grounded_confirmed_response(prompt, extracted)

    av.check_ollama_availability = lambda **k: av.OllamaAvailability(True, True, ["qwen2.5:7b-instruct"], "")
    av.call_ollama = _recording_call
    av.MAX_FIELDS_PER_BATCH = 2

    try:
        av.validate_extracted_fields(extracted, lines, meta, layout_lines=layout_lines)
    finally:
        av.check_ollama_availability = real_check
        av.call_ollama = real_call
        av.MAX_FIELDS_PER_BATCH = real_max_batch

    main_batches = [n for n in batch_sizes if n > 0]
    _ok(all(n <= 2 for n in main_batches), f"no batch exceeded MAX_FIELDS_PER_BATCH=2, got sizes={main_batches}")
    _ok(len(main_batches) > 1, f"the fixture's fields were split across multiple batches, got {len(main_batches)}")


# ============================================================
# Schema validation (shape only - grounding is checked separately above)
# ============================================================

def test_validate_batch_response():
    print()
    print("=" * 100)
    print("_validate_batch_response() - strict schema check")
    print("=" * 100)

    good = json.dumps({"results": [
        {"field": "Cash", "status": "CONFIRMED", "confidence": 0.9,
         "evidence_line_numbers": [10], "evidence_values": ["30"],
         "explanation": "ok", "requires_review": False},
    ]})
    _ok("Cash" in av._validate_batch_response(good, {"Cash"}), "a well-formed response validates")

    missing_evidence_keys = json.dumps({"results": [
        {"field": "Cash", "status": "CONFIRMED", "confidence": 0.9,
         "explanation": "ok", "requires_review": False},
    ]})
    _ok("Cash" not in av._validate_batch_response(missing_evidence_keys, {"Cash"}),
        "a response missing evidence_line_numbers/evidence_values is rejected (Phase 2D contract)")

    bad_evidence_type = json.dumps({"results": [
        {"field": "Cash", "status": "CONFIRMED", "confidence": 0.9,
         "evidence_line_numbers": "10", "evidence_values": ["30"],
         "explanation": "ok", "requires_review": False},
    ]})
    _ok("Cash" not in av._validate_batch_response(bad_evidence_type, {"Cash"}),
        "evidence_line_numbers must be a list, not a bare string")

    not_json = "this is not json at all"
    _ok(av._validate_batch_response(not_json, {"Cash"}) == {}, "non-JSON text never raises, returns empty")

    unknown_field = json.dumps({"results": [
        {"field": "Not A Real Field", "status": "CONFIRMED", "confidence": 0.9,
         "evidence_line_numbers": [1], "evidence_values": ["1"],
         "explanation": "ok", "requires_review": False},
    ]})
    _ok(av._validate_batch_response(unknown_field, {"Cash"}) == {}, "a field outside expected_fields is dropped")

    bad_status = json.dumps({"results": [
        {"field": "Cash", "status": "MADE_UP_STATUS", "confidence": 0.9,
         "evidence_line_numbers": [1], "evidence_values": ["1"],
         "explanation": "ok", "requires_review": False},
    ]})
    _ok("Cash" not in av._validate_batch_response(bad_status, {"Cash"}), "an out-of-vocabulary status is rejected")


def main():
    test_check_ollama_availability()
    test_call_ollama()
    test_table_aware_context()
    test_review_required_marked_uncertain_in_prompt()
    test_ground_evidence()
    test_end_to_end_hallucinated_evidence_is_downgraded()
    test_presence_check_cannot_carry_a_value()
    test_end_to_end_not_found_goes_through_presence_check()
    test_partial_batch_failure_does_not_discard_other_batches()
    test_time_budget_exhausted_skips_gracefully_without_calling_ollama()
    test_batch_size_is_capped()
    test_validate_batch_response()

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(" -", f)
        return 1
    print("ALL AI VALIDATION UNIT TESTS PASSED")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
