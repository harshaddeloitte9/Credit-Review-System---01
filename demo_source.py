"""
Builds the CURRENT demo dataset by combining:

  - '1-Input Data' (+ 'Sheet1' notes) from "Demo Extracted Sheet.xlsx" - the
    already-extracted, already-validated financial data. This is the
    current source of truth for Input & Validation and everything
    downstream of it, standing in for what a PDF -> OCR -> extraction
    pipeline would eventually produce (ocr_pipeline.py exists for that but
    is not run here - see the Data Upload page).

  - '2-Financial Analysis' through '9 - Credit Memo' policy/reference
    cells from "Credit Review Automation Example.xlsx" - thresholds,
    weights, qualitative scores, collateral book values, portfolio
    inputs, and narrative commentary. Demo Extracted Sheet.xlsx does not
    carry any of this (it only represents the extracted P&L/BS/CF/Ratios
    data), so this remains the only place it exists today.

This module performs no financial calculation. It only constructs the
workbook that the already-frozen/validated calculation modules read from,
using the same load_reference_bundle(path=...) mechanism the dynamic
upload feature already uses - reference_data.py is not modified, and
nothing here is a fallback: if either source file is missing or malformed,
this raises rather than silently substituting the other file's data.

Future PDF integration (see README note in ocr_pipeline.py): once OCR is
reactivated, whatever produces a "Generated Extracted Sheet" from a user's
uploaded PDF can call build_demo_workbook(that_path, POLICY_WORKBOOK_PATH)
with no change needed here or in any downstream module - only the first
argument changes.
"""

import os
import shutil
import tempfile

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell

APP_DIRECTORY = os.path.dirname(os.path.abspath(__file__))

EXTRACTED_SHEET_FILE = "Demo Extracted Sheet.xlsx"
EXTRACTED_SHEET_PATH = os.path.join(APP_DIRECTORY, EXTRACTED_SHEET_FILE)

POLICY_WORKBOOK_FILE = "Credit Review Automation Example.xlsx"
POLICY_WORKBOOK_PATH = os.path.join(APP_DIRECTORY, POLICY_WORKBOOK_FILE)

INPUT_SHEET = "1-Input Data"

# Fixed (not randomly-named) temp path: there is only ever one "current
# demo" state, so each call simply overwrites the same file rather than
# accumulating a new temp file on every Streamlit rerun.
_MERGED_DEMO_PATH = os.path.join(tempfile.gettempdir(), "credit_review_tool_demo_merged.xlsx")


class DemoSourceError(RuntimeError):
    """Raised when the extracted-data source or the policy workbook is
    missing or not shaped as expected - never silently worked around."""


def build_demo_workbook(extracted_sheet_path=EXTRACTED_SHEET_PATH,
                         policy_workbook_path=POLICY_WORKBOOK_PATH) -> str:
    """Returns the path to a merged workbook: policy_workbook_path's own
    '2-Financial Analysis' through '9 - Credit Memo' sheets, with
    '1-Input Data' replaced cell-for-cell by extracted_sheet_path's own
    '1-Input Data' - never the other direction, and never a partial guess
    at either sheet's content."""
    if not os.path.exists(extracted_sheet_path):
        raise DemoSourceError(f"Extracted data source not found: {extracted_sheet_path}")
    if not os.path.exists(policy_workbook_path):
        raise DemoSourceError(f"Policy/reference workbook not found: {policy_workbook_path}")

    extracted_wb = load_workbook(extracted_sheet_path, data_only=True)
    if INPUT_SHEET not in extracted_wb.sheetnames:
        raise DemoSourceError(f"'{extracted_sheet_path}' has no '{INPUT_SHEET}' sheet.")
    source_ws = extracted_wb[INPUT_SHEET]

    shutil.copy(policy_workbook_path, _MERGED_DEMO_PATH)
    merged_wb = load_workbook(_MERGED_DEMO_PATH, data_only=False)
    if INPUT_SHEET not in merged_wb.sheetnames:
        raise DemoSourceError(f"'{policy_workbook_path}' has no '{INPUT_SHEET}' sheet.")
    target_ws = merged_wb[INPUT_SHEET]

    for row in source_ws.iter_rows():
        for cell in row:
            target_cell = target_ws[cell.coordinate]
            # A75:D77 is a merged footer range in this template - only its
            # top-left anchor cell is writable; the rest are intentionally
            # skipped rather than unmerging the sheet.
            if isinstance(target_cell, MergedCell):
                continue
            target_cell.value = cell.value

    merged_wb.save(_MERGED_DEMO_PATH)
    return _MERGED_DEMO_PATH
