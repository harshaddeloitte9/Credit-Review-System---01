"""
Drives a full simulated PDF upload through the REAL Data Upload page UI
(streamlit.testing.v1.AppTest, no browser) - not just the underlying
ocr_upload.process_uploaded_pdf() in isolation. Proves the live per-stage
status pipeline is wired to the real pipeline (success path, and a failure
path with the real exception message surfaced, per "do not hide the real
error"). Never calls the real LLMWhisperer API - ocr_upload.run_ocr is
monkeypatched.

Run: python test_upload_pipeline_ui.py
"""

import glob
import os
import sys
import tempfile

os.environ["CREDIT_REVIEW_TOOL_ADVANCED_MODE"] = "1"

from streamlit.testing.v1 import AppTest

import ai_correction_proposals
import ai_dynamic_mapping
import ai_validation
import ocr_upload

# Isolate this test's OCR cache from any real cache (and from other test
# runs / earlier invocations of this same script) - each test upload uses
# distinct fake PDF bytes below, but redirecting UPLOAD_WORK_DIR too means
# this test can never accidentally reuse - or pollute - a real cached OCR
# result for the actual Tata Steel PDF.
ocr_upload.UPLOAD_WORK_DIR = tempfile.mkdtemp(prefix="cr_upload_ui_test_")

_FAILURES = []


def _load_realistic_ocr_text():
    """Reuses a genuine, previously-saved raw OCR result already sitting
    in this machine's temp dir from earlier real testing of this app
    (large files only, to skip small placeholder/test artifacts) - so
    this test exercises the full P&L/BS/CF field set real extraction
    needs (compute_derived_metrics has no None-guard for most P&L lines)
    without ever calling the OCR provider itself. Falls back to a minimal
    stand-in (accepting that downstream metrics may show "not available")
    if no such artifact exists on this machine.

    Specifically the real TATA STEEL filing, not just "whichever cached
    OCR text happens to be largest" - this machine's shared temp dir can
    (and, once a second real borrower PDF like Tata Chemicals has been
    tested here, does) also hold cached OCR text for other companies,
    some larger than Tata Steel's own. Every candidate is checked for a
    Tata Steel-specific marker before being accepted, so this function
    can never silently hand a different company's document to a test
    whose whole point is exercising the genuine, known-good Tata Steel
    baseline."""
    candidates = sorted(
        glob.glob(os.path.join(tempfile.gettempdir(), "credit_review_tool_pdf_upload", "raw_ocr_*.txt")),
        key=os.path.getsize, reverse=True,
    )
    for path in candidates:
        if os.path.getsize(path) <= 50_000:
            continue
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        if "tata steel" in text.lower():
            return text
    return "Revenue from operations FY2026 1,234.56 FY2025 1,100.00\n"


def _load_tata_chemicals_ocr_text():
    """Same mechanism as _load_realistic_ocr_text(), for the genuine Tata
    Chemicals filing cached on this machine after that document was
    manually tested through the live app - a real, different-company
    document (a numbers-line-precedes-label-line P&L/Cash Flow table
    layout that Tata Steel's own filing does not use), reused here rather
    than calling the OCR provider again. Returns None if no such cached
    text exists on this machine (e.g. a different environment) - callers
    must skip the Tata-Chemicals-specific checks in that case rather than
    fabricating a stand-in document."""
    candidates = glob.glob(os.path.join(tempfile.gettempdir(), "credit_review_tool_pdf_upload", "raw_ocr_*.txt"))
    for path in candidates:
        if os.path.getsize(path) <= 50_000:
            continue
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        if "tata chemicals" in text.lower():
            return text
    return None


def _ok(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        _FAILURES.append(msg)


def _page_text(at):
    texts = (
        [m.value for m in at.markdown] + [c.value for c in at.caption]
        + [e.value for e in at.error] + [c.value for c in at.code]
    )
    return "\n".join(str(t) for t in texts)


def _patch_ai_layers_unavailable():
    """This file tests the upload PIPELINE STAGES (OCR/extraction/
    population), never AI behavior - ai_validation.py/ai_correction_
    proposals.py/ai_dynamic_mapping.py each import check_ollama_
    availability/call_ollama FROM ai_validation via `from X import Y`,
    which copies the reference into their own namespace at import time,
    so each needs its own mock. With a genuinely-running, correctly-
    configured Ollama, leaving these unmocked would make a real upload
    here attempt real, slow inference this file was never designed to
    wait on (see test_ai_dynamic_mapping.py/test_ai_validation_ui.py for
    those layers' own dedicated tests)."""
    def _unavailable(*a, **k):
        return ai_validation.OllamaAvailability(False, False, [], "mocked unavailable - out of scope for this test file")

    def _should_not_be_called(*a, **k):
        raise AssertionError("no AI layer must ever make a real Ollama call in this test file")

    real = {
        "av_check": ai_validation.check_ollama_availability, "av_call": ai_validation.call_ollama,
        "acp_check": ai_correction_proposals.check_ollama_availability, "acp_call": ai_correction_proposals.call_ollama,
        "adm_check": ai_dynamic_mapping.check_ollama_availability, "adm_call": ai_dynamic_mapping.call_ollama,
    }
    ai_validation.check_ollama_availability = _unavailable
    ai_validation.call_ollama = _should_not_be_called
    ai_correction_proposals.check_ollama_availability = _unavailable
    ai_correction_proposals.call_ollama = _should_not_be_called
    ai_dynamic_mapping.check_ollama_availability = _unavailable
    ai_dynamic_mapping.call_ollama = _should_not_be_called
    return real


def _restore_ai_layers(real):
    ai_validation.check_ollama_availability = real["av_check"]
    ai_validation.call_ollama = real["av_call"]
    ai_correction_proposals.check_ollama_availability = real["acp_check"]
    ai_correction_proposals.call_ollama = real["acp_call"]
    ai_dynamic_mapping.check_ollama_availability = real["adm_check"]
    ai_dynamic_mapping.call_ollama = real["adm_call"]


def test_successful_upload_shows_real_pipeline():
    real_run_ocr = ocr_upload.run_ocr
    realistic_text = _load_realistic_ocr_text()

    def _fake_run_ocr(pdf_path):
        return realistic_text, {"status_code": 200}
    ocr_upload.run_ocr = _fake_run_ocr
    _real_ai = _patch_ai_layers_unavailable()

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        at.file_uploader[0].set_value(("borrower.pdf", b"%PDF-1.4 fake content\n" * 20, "application/pdf"))
        at.run(timeout=90)
        _ok(not at.exception, f"upload page runs with no exception: {[str(e) for e in at.exception]}")

        text = _page_text(at)
        _ok("Source Document" in text, "pipeline shows Source Document stage")
        _ok("OCR Extraction" in text, "pipeline shows OCR Extraction stage")
        _ok("Financial Data Extraction" in text, "pipeline shows Financial Data Extraction stage")
        _ok("Data Population" in text, "pipeline shows Data Population stage")
        _ok("✓ Complete" in text or "Completed" in text, "at least one stage shows a real completion, not a fake/simulated marker")
        _ok(at.session_state["source_mode"] == "pdf", "upload activates 'pdf' source mode on success")
        _ok(at.session_state["ocr_reused"] is False, "first upload of this content reports ocr_reused=False (genuinely called OCR)")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        _restore_ai_layers(_real_ai)


def test_ocr_failure_shows_real_error_on_correct_stage():
    real_run_ocr = ocr_upload.run_ocr

    def _failing_run_ocr(pdf_path):
        raise ocr_upload.OcrUploadError("LLMWhisperer upload rejected: HTTP 429 - quota exceeded (simulated)")
    ocr_upload.run_ocr = _failing_run_ocr

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        at.file_uploader[0].set_value(("borrower2.pdf", b"%PDF-1.4 fake content 2\n" * 20, "application/pdf"))
        at.run(timeout=90)
        _ok(not at.exception, f"upload page runs with no exception even when OCR fails: {[str(e) for e in at.exception]}")

        text = _page_text(at)
        _ok("quota exceeded (simulated)" in text, "the REAL OCR exception message is shown, not a generic/hidden error")
        _ok(at.session_state["pdf_processing_error"] and "quota exceeded" in at.session_state["pdf_processing_error"],
            "pdf_processing_error session-state holds the real exception text")
        _ok(at.session_state["source_mode"] != "pdf", "a failed OCR call does not activate a dataset")
    finally:
        ocr_upload.run_ocr = real_run_ocr


def test_extraction_failure_marks_extraction_not_ocr():
    real_run_ocr = ocr_upload.run_ocr
    import ocr_pipeline
    real_extract = ocr_pipeline.extract_financial_values

    def _fake_run_ocr(pdf_path):
        return "some ocr text", {"status_code": 200}

    def _failing_extract(*args, **kwargs):
        raise ValueError("simulated extraction failure - malformed OCR text")

    ocr_upload.run_ocr = _fake_run_ocr
    ocr_pipeline.extract_financial_values = _failing_extract

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        at.file_uploader[0].set_value(("borrower3.pdf", b"%PDF-1.4 fake content 3\n" * 20, "application/pdf"))
        at.run(timeout=90)
        _ok(not at.exception, f"upload page runs with no exception even when extraction fails: {[str(e) for e in at.exception]}")
        text = _page_text(at)
        _ok("simulated extraction failure" in text, "the real extraction exception message is shown")
        _ok("Financial Data Extraction" in text, "the failure is attributed to the Financial Data Extraction stage, not OCR (OCR itself succeeded)")
        _ok(at.session_state["pdf_processing_failed_stage"] == "Financial Data Extraction",
            f"pdf_processing_failed_stage correctly records which stage failed - got {at.session_state['pdf_processing_failed_stage']!r}")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        ocr_pipeline.extract_financial_values = real_extract


def main():
    test_successful_upload_shows_real_pipeline()
    test_ocr_failure_shows_real_error_on_correct_stage()
    test_extraction_failure_marks_extraction_not_ocr()
    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(" -", f)
        sys.exit(1)
    print("ALL UPLOAD PIPELINE UI TESTS PASSED (no real OCR call was made)")
    sys.exit(0)


if __name__ == "__main__":
    main()
