"""
PDF-upload integration layer: wires the validated ocr_pipeline.py functions
into the live Streamlit app.

Nothing here re-implements OCR/extraction logic - it only orchestrates the
existing, validated ocr_pipeline.py functions (run_llmwhisperer,
extract_financial_values, populate_workbook) plus demo_source's own
build_demo_workbook(), exactly as demo_source.py's own docstring anticipated:
"whatever produces a 'Generated Extracted Sheet' from a user's uploaded PDF
can call build_demo_workbook(that_path, POLICY_WORKBOOK_PATH) with no change
needed here or in any downstream module - only the first argument changes."

The one genuinely new piece is the network-transport fallback: this
environment's Python `requests` library gets its connection reset uploading
a large PDF to the LLMWhisperer endpoint (confirmed during OCR-pipeline
validation - WinError 10053, reproducible, unrelated to file content), while
curl reaches the same endpoint with the same file without issue. This tries
the plain SDK path first (so a deployment where that quirk doesn't exist
just works normally) and only falls back to the curl-based transport on a
connection-level failure - never on an HTTP-level error, which is a real
API response and should propagate as-is.
"""

import csv
import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.parse
from datetime import datetime

import requests
from openpyxl import load_workbook

import ocr_pipeline
from demo_source import build_demo_workbook, DemoSourceError

UPLOAD_WORK_DIR = os.path.join(tempfile.gettempdir(), "credit_review_tool_pdf_upload")

CLIENT_API_TIMEOUT = 300


class OcrUploadError(RuntimeError):
    """Raised when OCR, extraction, or workbook generation fails for an
    uploaded PDF. Never silently worked around - surfaced to the caller
    with a clear, specific message."""


def _ensure_work_dir():
    os.makedirs(UPLOAD_WORK_DIR, exist_ok=True)


def _run_llmwhisperer_via_curl(pdf_path):
    """Exact transport-only fallback validated during the OCR pipeline
    audit: every parameter, the auth header, and the async status-polling/
    retrieve loop are an unmodified replication of what
    LLMWhispererClientV2.whisper() itself sends and does - only the large
    upload POST is sent via curl instead of `requests`."""

    params = {
        "mode": ocr_pipeline.OCR_MODE,
        "output_mode": ocr_pipeline.OCR_OUTPUT_MODE,
        "page_separator": "<<<",
        "pages_to_extract": "",
        "median_filter_size": 0,
        "gaussian_blur_radius": 0,
        "line_splitter_tolerance": 0.4,
        "horizontal_stretch_factor": 1.0,
        "mark_vertical_lines": False,
        "mark_horizontal_lines": False,
        "line_splitter_strategy": "left-priority",
        "add_line_nos": True,
        "include_line_confidence": False,
        "word_confidence_threshold": 0.3,
        "lang": ocr_pipeline.OCR_LANGUAGE,
        "tag": "default",
        "file_name": "",
        "webhook_metadata": "",
        "use_webhook": "",
    }
    query = urllib.parse.urlencode(params)
    url = f"{ocr_pipeline.LLMWHISPERER_BASE_URL}/whisper?{query}"

    _ensure_work_dir()
    response_path = os.path.join(UPLOAD_WORK_DIR, "llmwhisperer_upload_response.json")

    curl_cmd = [
        "curl", "-s",
        "-X", "POST",
        "-H", f"unstract-key: {ocr_pipeline.LLMWHISPERER_API_KEY}",
        "-H", "Content-Type:",
        "--data-binary", f"@{pdf_path}",
        "-o", response_path,
        "-w", "%{http_code}",
        "--max-time", "300",
        url,
    ]

    proc = subprocess.run(curl_cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise OcrUploadError(f"Upload transport (curl) failed (exit {proc.returncode}): {proc.stderr}")

    http_code = proc.stdout.strip()
    status_code = int(http_code) if http_code.isdigit() else -1

    with open(response_path, "r", encoding="utf-8") as f:
        body_text = f.read()

    try:
        message = json.loads(body_text) if body_text else {}
        if not isinstance(message, dict):
            message = {"message": str(message)}
    except (json.JSONDecodeError, ValueError):
        message = {"message": body_text}

    message["status_code"] = status_code

    if status_code not in (200, 202):
        message["extraction"] = {}
        raise OcrUploadError(f"LLMWhisperer upload rejected: HTTP {status_code} - {message}")

    client = ocr_pipeline.LLMWhispererClientV2(
        base_url=ocr_pipeline.LLMWHISPERER_BASE_URL,
        api_key=ocr_pipeline.LLMWHISPERER_API_KEY,
        logging_level="ERROR",
    )

    if status_code == 202:
        message["extraction"] = {}
        whisper_hash = message["whisper_hash"]
        start_time = time.time()
        wait_timeout = ocr_pipeline.OCR_WAIT_TIMEOUT

        while time.time() - start_time < wait_timeout:
            status = client.whisper_status(whisper_hash=whisper_hash)
            if status["status_code"] != 200:
                raise OcrUploadError(f"Whisper status check failed: {status}")

            if status["status"] in ("accepted", "processing"):
                pass
            elif status["status"] == "processed":
                resultx = client.whisper_retrieve(whisper_hash=whisper_hash)
                if resultx["status_code"] == 200:
                    message["status_code"] = 200
                    message["extraction"] = resultx["extraction"]
                    break
                raise OcrUploadError(f"Whisper retrieve failed: {resultx}")
            elif status["status"] == "error" or "error" in status["status"]:
                raise OcrUploadError(f"Whisper processing failed: {status}")

            time.sleep(5)
        else:
            raise OcrUploadError("Whisper client operation timed out waiting for completion.")

    extraction = message.get("extraction", {})
    if isinstance(extraction, dict):
        text = extraction.get("result_text", "")
    else:
        text = str(extraction)

    if not text:
        raise OcrUploadError("LLMWhisperer completed, but no extracted text was returned.")

    return text, message


def run_ocr(pdf_path):
    """Run OCR on `pdf_path` via ocr_pipeline.run_llmwhisperer() (unmodified)
    first; only on a connection-level transport failure, retry via the
    curl-based fallback validated for this environment. An HTTP-level
    error (bad API key, quota exceeded, etc.) is a real response and is
    never masked by the fallback - it propagates immediately."""

    try:
        return ocr_pipeline.run_llmwhisperer(pdf_path)
    except (requests.exceptions.ConnectionError, ConnectionAbortedError, ConnectionResetError) as exc:
        try:
            return _run_llmwhisperer_via_curl(pdf_path)
        except Exception as fallback_exc:
            raise OcrUploadError(
                f"OCR upload failed via both the standard transport ({exc}) "
                f"and the curl fallback ({fallback_exc})."
            ) from fallback_exc


# A1's company/scope/unit header text must stay parseable by
# reference_data._parse_header() ("Area 1 - Extracted & Validated Data:
# {company}, {scope} ({unit})"). The template's own default names a
# specific company - replaced here with a neutral placeholder for every
# freshly generated workbook, since the real company name is not
# separately extracted from the OCR text in this build (see the
# integration report's known-limitations section). Never left as the
# template's original name, which would misattribute a different
# uploaded company's data.
GENERIC_BORROWER_LABEL = "Uploaded Borrower"


def _anonymize_header(workbook_path):
    wb = load_workbook(workbook_path)
    ws = wb[ocr_pipeline.TARGET_SHEET]
    ws["A1"] = f"Area 1 - Extracted & Validated Data: {GENERIC_BORROWER_LABEL}, Standalone ({chr(8377)} crore)"
    wb.save(workbook_path)


def process_uploaded_pdf(pdf_path, original_filename):
    """End-to-end: PDF -> OCR -> extraction -> fresh workbook (from the
    unmodified template) -> merged with the policy workbook. Returns a
    dict with every artifact the UI needs. Raises OcrUploadError /
    ocr_pipeline / demo_source's own exceptions on failure - nothing here
    invents a fallback value."""

    _ensure_work_dir()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    ocr_text, raw_result = run_ocr(pdf_path)

    raw_txt_path = os.path.join(UPLOAD_WORK_DIR, f"raw_ocr_{timestamp}.txt")
    with open(raw_txt_path, "w", encoding="utf-8") as f:
        f.write(ocr_text)

    extracted, debug_rows, meta = ocr_pipeline.extract_financial_values(
        ocr_text, ocr_pipeline.TEMPLATE_FILE
    )

    wb = ocr_pipeline.populate_workbook(ocr_pipeline.TEMPLATE_FILE, extracted)
    extracted_xlsx_path = os.path.join(
        UPLOAD_WORK_DIR, f"extracted_{timestamp}.xlsx"
    )
    wb.save(extracted_xlsx_path)
    _anonymize_header(extracted_xlsx_path)

    try:
        merged_path = build_demo_workbook(extracted_sheet_path=extracted_xlsx_path)
    except DemoSourceError as exc:
        raise OcrUploadError(f"Could not merge extracted data with the policy workbook: {exc}") from exc

    status_counts = {
        "CORRECT": 0, "NOT_FOUND_IN_OCR": 0, "AMBIGUOUS": 0, "NORMALIZED_FROM_OCR": 0,
    }
    for row in debug_rows:
        field = row["Field"]
        ocr_source_line = row["OCR Source Line"]
        fy_current = row["FY2026 Selected"]
        normalized = row["Normalized"]
        ambiguous = row["Ambiguous"]
        if ocr_source_line == "NOT FOUND" or fy_current is None:
            status_counts["NOT_FOUND_IN_OCR"] += 1
        elif ambiguous:
            status_counts["AMBIGUOUS"] += 1
        elif normalized:
            status_counts["NORMALIZED_FROM_OCR"] += 1
        else:
            status_counts["CORRECT"] += 1

    return {
        "pdf_name": original_filename,
        "timestamp": timestamp,
        "raw_ocr_text": ocr_text,
        "raw_ocr_path": raw_txt_path,
        "extracted_workbook_path": extracted_xlsx_path,
        "extracted_workbook_name": os.path.basename(extracted_xlsx_path),
        "merged_workbook_path": merged_path,
        "debug_rows": debug_rows,
        "status_counts": status_counts,
        "total_fields": len(debug_rows),
    }
