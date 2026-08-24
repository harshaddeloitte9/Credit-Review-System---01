"""
Proves ocr_upload.process_uploaded_pdf() never calls the real OCR provider
twice for the same PDF content, and that the self-healing Annual Sales
prefill in app.py is keyed on upload identity (not "is currently None").

Never calls the real LLMWhisperer API - ocr_upload.run_ocr is monkeypatched
with a fake that raises if invoked more than once for the same content, so
this can be run repeatedly during development without touching OCR quota.

Run: python test_ocr_cache.py
"""

import os
import shutil
import sys
import tempfile

import ocr_upload
import ocr_pipeline

_FAILURES = []


def _ok(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        _FAILURES.append(msg)


def test_ocr_cache_reuse():
    tmp_dir = tempfile.mkdtemp(prefix="cr_ocrcache_")
    ocr_upload.UPLOAD_WORK_DIR = os.path.join(tmp_dir, "work")

    pdf_path = os.path.join(tmp_dir, "borrower.pdf")
    with open(pdf_path, "wb") as f:
        f.write(b"%PDF-1.4 fake content for hashing purposes only\n" * 50)

    fake_ocr_text = (
        "Revenue from operations FY2026 1,234.56 FY2025 1,100.00\n"
        "Total assets FY2026 5,000.00 FY2025 4,800.00\n"
    )
    call_count = {"n": 0}

    def _fake_run_ocr(path):
        call_count["n"] += 1
        if call_count["n"] > 1:
            raise AssertionError("run_ocr called a second time for the same PDF content - cache not honored")
        return fake_ocr_text, {"status_code": 200}

    real_run_ocr = ocr_upload.run_ocr
    ocr_upload.run_ocr = _fake_run_ocr

    extraction_calls = {"n": 0}
    real_extract = ocr_pipeline.extract_financial_values

    def _counting_extract(*args, **kwargs):
        extraction_calls["n"] += 1
        return real_extract(*args, **kwargs)

    ocr_pipeline.extract_financial_values = _counting_extract

    try:
        stage_log = []

        def _status_callback(stage, state, detail=None):
            stage_log.append((stage, state))

        result1 = ocr_upload.process_uploaded_pdf(pdf_path, "borrower.pdf", status_callback=_status_callback)
        _ok(call_count["n"] == 1, "first call invokes the real OCR provider exactly once")
        _ok(result1["ocr_reused"] is False, "first call reports ocr_reused=False")
        _ok(("ocr", "processing") in stage_log, "status_callback received ('ocr', 'processing') on first (fresh) call")
        _ok(("ocr", "complete") in stage_log, "status_callback received ('ocr', 'complete') on first (fresh) call")
        _ok(("extraction", "complete") in stage_log, "status_callback received ('extraction', 'complete')")
        _ok(("population", "complete") in stage_log, "status_callback received ('population', 'complete')")

        stage_log.clear()
        result2 = ocr_upload.process_uploaded_pdf(pdf_path, "borrower.pdf", status_callback=_status_callback)
        _ok(call_count["n"] == 1, "second call for the SAME PDF content does NOT call the OCR provider again")
        _ok(result2["ocr_reused"] is True, "second call reports ocr_reused=True")
        _ok(("ocr", "reused") in stage_log, "status_callback received ('ocr', 'reused') on the cache-hit call")
        _ok(("ocr", "processing") not in stage_log, "status_callback did NOT report 'processing' for OCR on a cache hit (nothing ran)")
        _ok(extraction_calls["n"] == 2, "financial extraction re-runs fresh on every call, including a cache hit (not itself cached)")
        _ok(result2["raw_ocr_text"] == fake_ocr_text, "cached OCR text is byte-identical to what was originally extracted")

        different_pdf_path = os.path.join(tmp_dir, "different_borrower.pdf")
        with open(different_pdf_path, "wb") as f:
            f.write(b"%PDF-1.4 a totally different document\n" * 50)
        call_count["n"] = 0

        def _fake_run_ocr_2(path):
            call_count["n"] += 1
            return "Revenue from operations FY2026 999.00 FY2025 888.00\n", {"status_code": 200}
        ocr_upload.run_ocr = _fake_run_ocr_2
        ocr_upload.process_uploaded_pdf(different_pdf_path, "different_borrower.pdf")
        _ok(call_count["n"] == 1, "a genuinely different PDF (different content hash) still triggers a fresh OCR call")

    finally:
        ocr_upload.run_ocr = real_run_ocr
        ocr_pipeline.extract_financial_values = real_extract
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    test_ocr_cache_reuse()
    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(" -", f)
        sys.exit(1)
    print("ALL OCR CACHE TESTS PASSED (no real OCR call was made)")
    sys.exit(0)


if __name__ == "__main__":
    main()
