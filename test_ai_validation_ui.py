"""
Drives a full simulated PDF upload through the REAL Data Upload page UI
(streamlit.testing.v1.AppTest, no browser) with ai_validation.py's Ollama
seams (check_ollama_availability / call_ollama) monkeypatched - never a
real Ollama call. Proves the new AI Validation UI block added to app.py
(Phase 2B) behaves correctly end-to-end, per the user's explicit
before-finalizing checklist:

  - A valid, well-formed mocked AI response renders the correct AI
    Validation counts and a "flagged for review" table, AND the field it
    flags as FIELD_MISMATCH still shows its ORIGINAL deterministic value,
    unchanged, on the Input & Validation page (advisory-only, never
    mutates extracted data - see test_ai_validation.py for the same
    guarantee at the pure-function level).
  - A malformed/invalid AI JSON response is safely rejected (surfaced as
    AI_ERROR, not a crash, not INSUFFICIENT_CONTEXT).
  - Ollama unreachable, and Ollama reachable but the model not installed,
    both show a clear "did not run" state with no crash and the rest of
    the upload pipeline completely unaffected.

Never calls the real LLMWhisperer or Ollama APIs - ocr_upload.run_ocr and
ai_validation.check_ollama_availability / ai_validation.call_ollama are
all monkeypatched.

Run: python test_ai_validation_ui.py
"""

import json
import os
import re
import sys
import tempfile

os.environ["CREDIT_REVIEW_TOOL_ADVANCED_MODE"] = "1"

from streamlit.testing.v1 import AppTest

import ai_validation
import ocr_upload
from test_upload_pipeline_ui import _load_realistic_ocr_text

# Isolated from every other test's OCR cache - see test_upload_pipeline_ui.py.
ocr_upload.UPLOAD_WORK_DIR = tempfile.mkdtemp(prefix="cr_ai_validation_ui_test_")

_FAILURES = []
_FIELD_RE = re.compile(r'"field":\s*"([^"]*)"')

# Known-good Tata Steel figures (see test_extraction_audit.py) - used to
# prove the AI's FIELD_MISMATCH flag never alters the deterministically
# extracted value shown elsewhere in the app.
_KNOWN_REVENUE_CURRENT = "139,720.22"
_KNOWN_REVENUE_PREVIOUS = "132,516.66"


def _ok(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        _FAILURES.append(msg)


def _page_text(at, area=None):
    if area is not None:
        at.sidebar.radio[0].set_value(area)
        at.run(timeout=60)
    texts = (
        [m.value for m in at.markdown] + [c.value for c in at.caption]
        + [e.value for e in at.error] + [c.value for c in at.code]
        + [exp.label for exp in at.expander]
    )
    return "\n".join(str(t) for t in texts)


def _make_fake_call_ollama(flag_field=None):
    """Parses the ACTUAL table-aware context objects the prompt was built
    from (never hard-coded), so this works regardless of exactly which
    fields the real Tata Steel OCR text yields, and cites each field's own
    real target-row line number/value as evidence - so every result
    grounds successfully (see ai_validation._ground_evidence) and isn't
    downgraded, keeping the confidence values below meaningful for the
    assertions. Confirms `flag_field` as FIELD_MISMATCH, everything else
    CONFIRMED."""
    def _fake_call_ollama(prompt, model=None, base_url=None, timeout=None):
        contexts = json.loads(prompt.split("FIELDS:\n", 1)[1])
        results = []
        for ctx in contexts:
            f = ctx["field"]
            target_row = ctx.get("target_row") or {}
            line_number = target_row.get("line_number")
            raw_values = target_row.get("raw_values_found") or []
            evidence_line_numbers = [line_number] if line_number is not None else []
            evidence_values = raw_values[:1]
            if f == flag_field:
                results.append({
                    "field": f, "status": "FIELD_MISMATCH", "confidence": 0.42,
                    "evidence_line_numbers": evidence_line_numbers,
                    "evidence_values": evidence_values,
                    "explanation": "This value appears to align with a different line item in the source text.",
                    "requires_review": True,
                })
            else:
                results.append({
                    "field": f, "status": "CONFIRMED", "confidence": 0.95,
                    "evidence_line_numbers": evidence_line_numbers,
                    "evidence_values": evidence_values,
                    "explanation": "Matches the source line and fiscal-year columns.",
                    "requires_review": False,
                })
        return json.dumps({"results": results})
    return _fake_call_ollama


def _available(model_installed=True):
    return ai_validation.OllamaAvailability(
        reachable=True, model_installed=model_installed,
        installed_models=[ai_validation.DEFAULT_MODEL] if model_installed else [],
        detail="" if model_installed else f"Ollama is running, but '{ai_validation.DEFAULT_MODEL}' is not installed.",
    )


def _upload(at, pdf_bytes):
    at.file_uploader[0].set_value(("borrower.pdf", pdf_bytes, "application/pdf"))
    at.run(timeout=90)


def test_valid_response_renders_and_does_not_mutate_extracted_value():
    print("=" * 100)
    print("TEST - valid mocked AI response: correct UI + original value unchanged")
    print("=" * 100)

    real_run_ocr = ocr_upload.run_ocr
    real_check_availability = ai_validation.check_ollama_availability
    real_call_ollama = ai_validation.call_ollama
    realistic_text = _load_realistic_ocr_text()

    ocr_upload.run_ocr = lambda pdf_path: (realistic_text, {"status_code": 200})
    ai_validation.check_ollama_availability = lambda *a, **k: _available(True)
    ai_validation.call_ollama = _make_fake_call_ollama(flag_field="Revenue from operations")

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload(at, b"%PDF-1.4 fake content ai-valid\n" * 20)
        _ok(not at.exception, f"upload page runs with no exception: {[str(e) for e in at.exception]}")

        text = _page_text(at)
        _ok("AI-Assisted Validation" in text, "AI Validation section renders on the upload summary page")
        _ok("1 field mismatches" in text, "caption reports exactly one FIELD_MISMATCH result")
        _ok("Fields flagged for review (1)" in text, "flagged-for-review expander shows exactly 1 field")
        _ok("Revenue from operations" in text and "FIELD_MISMATCH" in text,
            "flagged field name and its AI status both appear in the review table")
        _ok("42%" in text, "the mocked confidence score (0.42) is displayed")

        input_val_text = _page_text(at, "2. Input & Validation")
        _ok(_KNOWN_REVENUE_CURRENT in input_val_text and _KNOWN_REVENUE_PREVIOUS in input_val_text,
            "Input & Validation still shows the ORIGINAL deterministic Revenue figures unchanged, "
            "despite the AI flagging that field as a FIELD_MISMATCH (advisory-only, never overwrites)")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        ai_validation.check_ollama_availability = real_check_availability
        ai_validation.call_ollama = real_call_ollama


def test_invalid_json_from_ollama_is_safely_rejected():
    print()
    print("=" * 100)
    print("TEST - malformed AI JSON response: rejected safely, no crash")
    print("=" * 100)

    real_run_ocr = ocr_upload.run_ocr
    real_check_availability = ai_validation.check_ollama_availability
    real_call_ollama = ai_validation.call_ollama
    realistic_text = _load_realistic_ocr_text()

    ocr_upload.run_ocr = lambda pdf_path: (realistic_text, {"status_code": 200})
    ai_validation.check_ollama_availability = lambda *a, **k: _available(True)
    ai_validation.call_ollama = lambda prompt, model=None, base_url=None, timeout=None: "not valid json {{{"

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload(at, b"%PDF-1.4 fake content ai-badjson\n" * 20)
        _ok(not at.exception, f"upload page runs with no exception on malformed AI response: {[str(e) for e in at.exception]}")

        text = _page_text(at)
        _ok("AI-Assisted Validation" in text, "AI Validation section still renders")
        _ok("could not be validated (technical error)" in text, "a technical-error count is shown")
        _ok("INSUFFICIENT_CONTEXT" not in text, "malformed response is never reported as INSUFFICIENT_CONTEXT (that is a content judgement, not a technical failure)")
        _ok("Financial Data Extraction" in text and "Extracted Financial Data" in text,
            "the rest of the upload pipeline (deterministic extraction) completed normally despite the AI failure")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        ai_validation.check_ollama_availability = real_check_availability
        ai_validation.call_ollama = real_call_ollama


def test_ollama_unreachable_shows_unavailable_state_no_crash():
    print()
    print("=" * 100)
    print("TEST - Ollama unreachable: clear 'did not run' state, pipeline unaffected")
    print("=" * 100)

    real_run_ocr = ocr_upload.run_ocr
    real_check_availability = ai_validation.check_ollama_availability
    real_call_ollama = ai_validation.call_ollama
    realistic_text = _load_realistic_ocr_text()

    def _unreachable(*a, **k):
        return ai_validation.OllamaAvailability(
            reachable=False, model_installed=False, installed_models=[],
            detail="Ollama is not reachable at http://localhost:11434 (ConnectionError). Is 'ollama serve' running?",
        )

    def _should_not_be_called(prompt, model=None, base_url=None, timeout=None):
        raise AssertionError("call_ollama must never be invoked when Ollama is unreachable")

    ocr_upload.run_ocr = lambda pdf_path: (realistic_text, {"status_code": 200})
    ai_validation.check_ollama_availability = _unreachable
    ai_validation.call_ollama = _should_not_be_called

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload(at, b"%PDF-1.4 fake content ai-unreachable\n" * 20)
        _ok(not at.exception, f"upload page runs with no exception when Ollama is unreachable: {[str(e) for e in at.exception]}")

        text = _page_text(at)
        _ok("AI validation did not run for this upload" in text, "a clear, non-crashing 'did not run' message is shown")
        _ok("Is 'ollama serve' running?" in text, "the specific unreachable-detail reason is surfaced, not a generic message")
        _ok(at.session_state["source_mode"] == "pdf", "the upload pipeline still completes and activates the dataset normally")
        _ok("Financial Data Extraction" in text and "Extracted Financial Data" in text,
            "deterministic extraction is completely unaffected by Ollama being unreachable")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        ai_validation.check_ollama_availability = real_check_availability
        ai_validation.call_ollama = real_call_ollama


def test_model_missing_shows_unavailable_state():
    print()
    print("=" * 100)
    print("TEST - Ollama reachable but model not installed: clear state, no crash")
    print("=" * 100)

    real_run_ocr = ocr_upload.run_ocr
    real_check_availability = ai_validation.check_ollama_availability
    real_call_ollama = ai_validation.call_ollama
    realistic_text = _load_realistic_ocr_text()

    def _should_not_be_called(prompt, model=None, base_url=None, timeout=None):
        raise AssertionError("call_ollama must never be invoked when the model is not installed")

    ocr_upload.run_ocr = lambda pdf_path: (realistic_text, {"status_code": 200})
    ai_validation.check_ollama_availability = lambda *a, **k: _available(False)
    ai_validation.call_ollama = _should_not_be_called

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload(at, b"%PDF-1.4 fake content ai-nomodel\n" * 20)
        _ok(not at.exception, f"upload page runs with no exception when the model is not installed: {[str(e) for e in at.exception]}")

        text = _page_text(at)
        _ok("AI validation did not run for this upload" in text, "a clear 'did not run' message is shown for a missing model too")
        _ok("is not installed" in text, "the specific missing-model reason is surfaced")
        _ok(at.session_state["source_mode"] == "pdf", "the upload pipeline still completes and activates the dataset normally")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        ai_validation.check_ollama_availability = real_check_availability
        ai_validation.call_ollama = real_call_ollama


def main():
    test_valid_response_renders_and_does_not_mutate_extracted_value()
    test_invalid_json_from_ollama_is_safely_rejected()
    test_ollama_unreachable_shows_unavailable_state_no_crash()
    test_model_missing_shows_unavailable_state()
    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(" -", f)
        sys.exit(1)
    print("ALL AI VALIDATION UI TESTS PASSED (no real Ollama or OCR call was made)")
    sys.exit(0)


if __name__ == "__main__":
    main()
