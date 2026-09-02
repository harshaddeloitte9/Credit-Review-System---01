"""
Unit tests for ai_correction_proposals.py (Phase 3A).

IMPORTANT monkeypatch note: ai_correction_proposals.py imports
check_ollama_availability/call_ollama FROM ai_validation via
`from ai_validation import (..., call_ollama, check_ollama_availability)`
- that copies the reference into ai_correction_proposals's OWN module
namespace at import time. Patching ai_validation.check_ollama_
availability afterward would NOT affect ai_correction_proposals's
internal calls (a classic `from X import Y` gotcha) - every monkeypatch
below is therefore applied to `acp.check_ollama_availability` /
`acp.call_ollama` directly, never to the `ai_validation` module.

No mocking framework, matching this codebase's own convention. Never
calls a real Ollama instance.

Run: python test_ai_correction_proposals.py
"""

import copy
import glob
import json
import os
import re
import sys
import tempfile

import ai_correction_proposals as acp
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
# Synthetic OCR fixtures (same style as test_ai_validation.py - never
# real borrower data).
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
    raw_and_normalized = [(raw, op.normalize_text(raw)) for raw in text.splitlines()]
    raw_and_normalized = [(raw, norm) for raw, norm in raw_and_normalized if norm]
    lines = [norm for _raw, norm in raw_and_normalized]
    layout_lines = [op._normalize_preserve_layout(raw) for raw, _norm in raw_and_normalized]
    return lines, layout_lines


def _ambiguous_fixture():
    """Same shape as test_ai_validation.py's own - a lone Cash figure
    sitting almost exactly halfway between the section's two calibrated
    column positions - genuinely REVIEW_REQUIRED. The "(iv) Bank
    balances" row (37/41) sits within Cash's own +/-2 line window, so
    fakes below can point to it as a plausible alternative match."""
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


def _ok_fixture():
    """No REVIEW_REQUIRED, no AI-flagged fields at all (empty ai_results)
    - nothing should trigger a correction attempt."""
    lines = _bs_header() + [
        _row("    (iii) Cash and cash equivalents", 30, 25),
        _row("    (iv) Bank balances other than (iii) above", 37, 41),
        _row("Total assets", 2000, 1800),
    ]
    text = "\n".join(lines)
    extracted, debug_rows, meta = op.extract_financial_values(text, op.TEMPLATE_FILE)
    assert extracted["Cash"]["validation_status"] == "OK", "fixture sanity check: Cash must be unambiguous here"
    ai_lines, ai_layout_lines = _build_lines_for_ai(text)
    return extracted, meta, ai_lines, ai_layout_lines


_FIELD_RE = re.compile(r'"field":\s*"([^"]*)"')


def _parse_contexts(prompt):
    return json.loads(prompt.split("FIELDS:\n", 1)[1])


def _no_proposal_response(contexts):
    return json.dumps({"results": [
        {
            "field": ctx["field"], "status": "NO_PROPOSAL", "confidence": 0.5,
            "reason": "nothing better found", "requires_review": False,
            "proposed_source_line_number": None,
            "proposed_column_current_label": None, "proposed_column_previous_label": None,
            "proposed_value_current": None, "proposed_value_previous": None,
            "evidence_line_numbers": [], "evidence_values": [],
        }
        for ctx in contexts
    ]})


def _good_proposal_response(contexts):
    """Points at the real "(iv) Bank balances" nearby row (37/41) as an
    alternative for Cash - genuinely grounded (real line, real values,
    real column labels)."""
    results = []
    for ctx in contexts:
        current_label, previous_label = list(ctx["deterministic_mapping"].keys())
        nearby = ctx.get("nearby_rows") or []
        candidate = next((r for r in nearby if "37" in r["text"]), None)
        if candidate is None:
            results.append(json.loads(_no_proposal_response([ctx]))["results"][0])
            continue
        results.append({
            "field": ctx["field"], "status": "PROPOSAL_FOUND", "confidence": 0.8,
            "reason": "The bank balances row appears to be the real match for this field.",
            "requires_review": True,
            "proposed_source_line_number": candidate["line_number"],
            "proposed_column_current_label": current_label,
            "proposed_column_previous_label": previous_label,
            "proposed_value_current": "37", "proposed_value_previous": "41",
            "evidence_line_numbers": [candidate["line_number"]],
            "evidence_values": ["37", "41"],
        })
    return json.dumps({"results": results})


def test_1_correct_proposal_is_verified_and_stored_separately():
    print("=" * 100)
    print("TEST 1 - correct proposal: verified, grounded, extracted unchanged")
    print("=" * 100)

    extracted, meta, lines, layout_lines = _ambiguous_fixture()
    snapshot = copy.deepcopy(extracted)
    real_check, real_call = acp.check_ollama_availability, acp.call_ollama

    acp.check_ollama_availability = lambda **k: av.OllamaAvailability(True, True, [av.DEFAULT_MODEL], "")
    acp.call_ollama = lambda prompt, model=None, base_url=None, timeout=None: _good_proposal_response(_parse_contexts(prompt))

    try:
        proposals, availability = acp.generate_correction_proposals(extracted, {}, lines, meta, layout_lines=layout_lines)
    finally:
        acp.check_ollama_availability = real_check
        acp.call_ollama = real_call

    _ok("Cash" in proposals, "Cash (REVIEW_REQUIRED) received a correction-proposal attempt")
    p = proposals["Cash"]
    _ok(p.proposal_status == "PROPOSAL_AVAILABLE", f"proposal verified as available, got {p.proposal_status!r}")
    _ok(p.evidence_grounded is not False, "secondary evidence citations are grounded (or not required)")
    _ok(p.proposed_value_current == "37" and p.proposed_value_previous == "41", "proposed values are recorded correctly")
    _ok(p.original_value_current == extracted["Cash"]["fy_current"], "original value is copied verbatim, not recomputed")
    _ok(extracted == snapshot, "extracted dict is byte-for-byte unchanged after generating a proposal")
    _ok(availability is not None and availability.reachable, "availability reflects the real (mocked) check")


def test_2_hallucinated_value_is_rejected():
    print()
    print("=" * 100)
    print("TEST 2 - hallucinated proposed value (481, appears nowhere) -> rejected")
    print("=" * 100)

    extracted, meta, lines, layout_lines = _ambiguous_fixture()
    snapshot = copy.deepcopy(extracted)
    real_check, real_call = acp.check_ollama_availability, acp.call_ollama

    def _hallucinating_call(prompt, model=None, base_url=None, timeout=None):
        contexts = _parse_contexts(prompt)
        results = []
        for ctx in contexts:
            current_label, previous_label = list(ctx["deterministic_mapping"].keys())
            nearby = ctx.get("nearby_rows") or []
            candidate = next((r for r in nearby if "37" in r["text"]), None)
            results.append({
                "field": ctx["field"], "status": "PROPOSAL_FOUND", "confidence": 0.8,
                "reason": "looks like a unit/alignment issue", "requires_review": True,
                "proposed_source_line_number": candidate["line_number"] if candidate else None,
                "proposed_column_current_label": current_label, "proposed_column_previous_label": previous_label,
                "proposed_value_current": 481, "proposed_value_previous": None,  # never appears anywhere
                "evidence_line_numbers": [candidate["line_number"]] if candidate else [],
                "evidence_values": [481],
            })
        return json.dumps({"results": results})

    acp.check_ollama_availability = lambda **k: av.OllamaAvailability(True, True, [av.DEFAULT_MODEL], "")
    acp.call_ollama = _hallucinating_call

    try:
        proposals, _availability = acp.generate_correction_proposals(extracted, {}, lines, meta, layout_lines=layout_lines)
    finally:
        acp.check_ollama_availability = real_check
        acp.call_ollama = real_call

    p = proposals["Cash"]
    _ok(p.proposal_status == "PROPOSAL_REJECTED_BY_EVIDENCE", f"hallucinated value -> PROPOSAL_REJECTED_BY_EVIDENCE, got {p.proposal_status!r}")
    _ok(p.evidence_grounded is False, "evidence_grounded is explicitly False")
    _ok(p.requires_review is True, "requires_review is forced True")
    _ok(p.confidence <= 0.3, f"confidence is downgraded, got {p.confidence}")
    _ok(extracted == snapshot, "extracted dict is unchanged even when a hallucinated proposal is rejected")


def test_3_invalid_line_number_is_rejected_no_crash():
    print()
    print("=" * 100)
    print("TEST 3 - proposed line number outside what was shown -> rejected, no crash")
    print("=" * 100)

    extracted, meta, lines, layout_lines = _ambiguous_fixture()
    real_check, real_call = acp.check_ollama_availability, acp.call_ollama

    def _bad_line_call(prompt, model=None, base_url=None, timeout=None):
        contexts = _parse_contexts(prompt)
        results = []
        for ctx in contexts:
            current_label, previous_label = list(ctx["deterministic_mapping"].keys())
            results.append({
                "field": ctx["field"], "status": "PROPOSAL_FOUND", "confidence": 0.8,
                "reason": "x", "requires_review": True,
                "proposed_source_line_number": 999999,  # never shown to the model
                "proposed_column_current_label": current_label, "proposed_column_previous_label": previous_label,
                "proposed_value_current": 37, "proposed_value_previous": 41,
                "evidence_line_numbers": [999999], "evidence_values": [37, 41],
            })
        return json.dumps({"results": results})

    acp.check_ollama_availability = lambda **k: av.OllamaAvailability(True, True, [av.DEFAULT_MODEL], "")
    acp.call_ollama = _bad_line_call

    try:
        proposals, _availability = acp.generate_correction_proposals(extracted, {}, lines, meta, layout_lines=layout_lines)
        crashed = False
    except Exception as exc:
        crashed = True
        proposals = {}
        print(f"    unexpected exception: {exc}")
    finally:
        acp.check_ollama_availability = real_check
        acp.call_ollama = real_call

    _ok(not crashed, "an out-of-range proposed line number never crashes the application")
    _ok(proposals.get("Cash") is not None and proposals["Cash"].proposal_status == "PROPOSAL_REJECTED_BY_EVIDENCE",
        f"out-of-range line number -> rejected, got {proposals.get('Cash') and proposals['Cash'].proposal_status!r}")


def test_4_invalid_fiscal_year_is_rejected():
    print()
    print("=" * 100)
    print("TEST 4 - proposed fiscal-year label does not match a detected header -> rejected")
    print("=" * 100)

    extracted, meta, lines, layout_lines = _ambiguous_fixture()
    real_check, real_call = acp.check_ollama_availability, acp.call_ollama

    def _bad_year_call(prompt, model=None, base_url=None, timeout=None):
        contexts = _parse_contexts(prompt)
        results = []
        for ctx in contexts:
            nearby = ctx.get("nearby_rows") or []
            candidate = next((r for r in nearby if "37" in r["text"]), None)
            results.append({
                "field": ctx["field"], "status": "PROPOSAL_FOUND", "confidence": 0.8,
                "reason": "x", "requires_review": True,
                "proposed_source_line_number": candidate["line_number"] if candidate else None,
                "proposed_column_current_label": "FY 31.03.2099",  # not a real detected label
                "proposed_column_previous_label": "FY 31.03.2098",
                "proposed_value_current": 37, "proposed_value_previous": 41,
                "evidence_line_numbers": [candidate["line_number"]] if candidate else [],
                "evidence_values": [37, 41],
            })
        return json.dumps({"results": results})

    acp.check_ollama_availability = lambda **k: av.OllamaAvailability(True, True, [av.DEFAULT_MODEL], "")
    acp.call_ollama = _bad_year_call

    try:
        proposals, _availability = acp.generate_correction_proposals(extracted, {}, lines, meta, layout_lines=layout_lines)
    finally:
        acp.check_ollama_availability = real_check
        acp.call_ollama = real_call

    p = proposals["Cash"]
    _ok(p.proposal_status in ("PROPOSAL_REJECTED_BY_EVIDENCE", "PROPOSAL_UNVERIFIED"),
        f"invented fiscal-year label -> rejected or unverified, got {p.proposal_status!r}")


def test_5_ai_unavailable_pipeline_unaffected():
    print()
    print("=" * 100)
    print("TEST 5 - Ollama unavailable: deterministic pipeline unaffected, proposals AI_UNAVAILABLE")
    print("=" * 100)

    extracted, meta, lines, layout_lines = _ambiguous_fixture()
    snapshot = copy.deepcopy(extracted)
    real_check, real_call = acp.check_ollama_availability, acp.call_ollama

    def _unreachable(**k):
        return av.OllamaAvailability(False, False, [], "Ollama is not reachable. Is 'ollama serve' running?")

    def _should_not_be_called(*a, **k):
        raise AssertionError("call_ollama must never be invoked when Ollama is unreachable")

    acp.check_ollama_availability = _unreachable
    acp.call_ollama = _should_not_be_called

    try:
        proposals, availability = acp.generate_correction_proposals(extracted, {}, lines, meta, layout_lines=layout_lines)
    finally:
        acp.check_ollama_availability = real_check
        acp.call_ollama = real_call

    _ok(proposals["Cash"].technical_status == "AI_UNAVAILABLE", "Cash's proposal technical_status is AI_UNAVAILABLE")
    _ok(proposals["Cash"].proposal_status is None, "no content status is fabricated when unavailable")
    _ok(extracted == snapshot, "deterministic extraction is completely unaffected")
    _ok(availability is not None and not availability.reachable, "availability correctly reports unreachable")


def test_6_time_budget_exhausted_no_hang():
    print()
    print("=" * 100)
    print("TEST 6 - time budget exhausted: remaining proposals skipped, no hang")
    print("=" * 100)

    extracted, meta, lines, layout_lines = _ambiguous_fixture()
    real_check, real_call = acp.check_ollama_availability, acp.call_ollama
    real_budget = acp.CORRECTION_TIME_BUDGET_SECONDS

    def _should_not_be_called(*a, **k):
        raise AssertionError("call_ollama must never be invoked once the time budget is already exhausted")

    acp.check_ollama_availability = lambda **k: av.OllamaAvailability(True, True, [av.DEFAULT_MODEL], "")
    acp.call_ollama = _should_not_be_called
    acp.CORRECTION_TIME_BUDGET_SECONDS = 0.0

    try:
        proposals, _availability = acp.generate_correction_proposals(extracted, {}, lines, meta, layout_lines=layout_lines)
    finally:
        acp.check_ollama_availability = real_check
        acp.call_ollama = real_call
        acp.CORRECTION_TIME_BUDGET_SECONDS = real_budget

    _ok(proposals["Cash"].technical_status == "AI_SKIPPED", "Cash's proposal is AI_SKIPPED once the budget is exhausted")
    _ok("time budget" in proposals["Cash"].reason.lower(), "the skip reason names the time budget, not a generic failure")


def test_7_no_suspicious_fields_no_ollama_call():
    print()
    print("=" * 100)
    print("TEST 7 - no suspicious fields: Ollama is never probed at all")
    print("=" * 100)

    extracted, meta, lines, layout_lines = _ok_fixture()
    real_check, real_call = acp.check_ollama_availability, acp.call_ollama

    def _should_not_be_called(*a, **k):
        raise AssertionError("Ollama must never be probed when nothing requires a correction proposal")

    acp.check_ollama_availability = _should_not_be_called
    acp.call_ollama = _should_not_be_called

    try:
        proposals, availability = acp.generate_correction_proposals(extracted, {}, lines, meta, layout_lines=layout_lines)
    finally:
        acp.check_ollama_availability = real_check
        acp.call_ollama = real_call

    _ok(proposals == {}, "no proposals are generated when every field is fine")
    _ok(availability is None, "availability is None - Ollama was never even probed")


def test_8_deterministic_safety_snapshot():
    print()
    print("=" * 100)
    print("TEST 8 - deterministic safety: extracted is byte-for-byte unchanged")
    print("=" * 100)

    extracted, meta, lines, layout_lines = _ambiguous_fixture()
    snapshot = copy.deepcopy(extracted)
    real_check, real_call = acp.check_ollama_availability, acp.call_ollama

    acp.check_ollama_availability = lambda **k: av.OllamaAvailability(True, True, [av.DEFAULT_MODEL], "")
    acp.call_ollama = lambda prompt, model=None, base_url=None, timeout=None: _good_proposal_response(_parse_contexts(prompt))

    try:
        acp.generate_correction_proposals(extracted, {}, lines, meta, layout_lines=layout_lines)
    finally:
        acp.check_ollama_availability = real_check
        acp.call_ollama = real_call

    _ok(extracted == snapshot, "extracted is identical before and after proposal generation")
    for field, ed in extracted.items():
        _ok(ed["fy_current"] == snapshot[field]["fy_current"] and ed["fy_previous"] == snapshot[field]["fy_previous"],
            f"{field}: fy_current/fy_previous never mutated") if field == "Cash" else None


def _find_tata_chemicals_cache():
    candidates = glob.glob(os.path.join(tempfile.gettempdir(), "credit_review_tool_pdf_upload", "raw_ocr_cache_*.txt"))
    for path in candidates:
        if os.path.getsize(path) <= 50_000:
            continue
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        if "tata chemicals" in text.lower():
            return text
    return None


def test_9_tata_chemicals_regression():
    print()
    print("=" * 100)
    print("TEST 9 - real Tata Chemicals fixture: known REVIEW_REQUIRED field")
    print("=" * 100)

    ocr_text = _find_tata_chemicals_cache()
    if ocr_text is None:
        print("[SKIP] No cached Tata Chemicals OCR text on this machine - skipping (matches test_extraction_audit.py's own convention).")
        return

    extracted, debug_rows, meta = op.extract_financial_values(ocr_text, op.TEMPLATE_FILE)
    snapshot = copy.deepcopy(extracted)
    lines, layout_lines = _build_lines_for_ai(ocr_text)

    review_required = [f for f, ed in extracted.items() if ed["validation_status"] == "REVIEW_REQUIRED"]
    _ok("Exceptional items" in review_required,
        f"the known real ambiguous field (Exceptional items) is still REVIEW_REQUIRED - got {review_required}")

    real_check, real_call = acp.check_ollama_availability, acp.call_ollama
    acp.check_ollama_availability = lambda **k: av.OllamaAvailability(True, True, [av.DEFAULT_MODEL], "")
    acp.call_ollama = lambda prompt, model=None, base_url=None, timeout=None: _no_proposal_response(_parse_contexts(prompt))

    try:
        proposals, availability = acp.generate_correction_proposals(extracted, {}, lines, meta, layout_lines=layout_lines)
    finally:
        acp.check_ollama_availability = real_check
        acp.call_ollama = real_call

    _ok("Exceptional items" in proposals, "a correction-proposal attempt was made for the real ambiguous field")
    _ok(proposals["Exceptional items"].technical_status == "AI_COMPLETED", "the attempt completed successfully (mocked)")
    _ok(extracted == snapshot, "the real Tata Chemicals extraction is unchanged after proposal generation")


def main():
    test_1_correct_proposal_is_verified_and_stored_separately()
    test_2_hallucinated_value_is_rejected()
    test_3_invalid_line_number_is_rejected_no_crash()
    test_4_invalid_fiscal_year_is_rejected()
    test_5_ai_unavailable_pipeline_unaffected()
    test_6_time_budget_exhausted_no_hang()
    test_7_no_suspicious_fields_no_ollama_call()
    test_8_deterministic_safety_snapshot()
    test_9_tata_chemicals_regression()

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(" -", f)
        return 1
    print("ALL AI CORRECTION PROPOSAL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
