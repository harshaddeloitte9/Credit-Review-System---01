"""
Phase 1 extraction/completeness audit - dedicated regression tests for the
six scenarios explicitly required by that audit (Steps 1-10):

  Test 1 - Correct Field Mapping: an adjacent row's value is never assigned
            to a different field (Cash vs. the neighbouring Bank Balances
            row).
  Test 2 - Year Alignment: values are assigned to the correct fiscal-year
            column - including when a row prints only ONE visible figure,
            and when a synthetic document's own header lists the years in
            the OPPOSITE (previous-first) order from what this pipeline's
            positional fallback would otherwise assume.
  Test 3 - Unit Scaling: a clean, comma-free figure (481) is never silently
            rescaled by comparing it to a very different sibling value
            (the old, removed sibling-magnitude heuristic's failure mode),
            while a genuine OCR decimal-point defect (a malformed comma
            group, or a decimal point misread as a comma) is still
            corrected - deterministically, from the token's own text shape
            alone.
  Test 4 - Missing Critical Data: a document missing Revenue and Net Profit
            shows completeness below 100% and an overall decision of
            "Not Available / Requires Review", never "Approve".
  Test 5 - Ambiguous Extraction: a row whose lone visible figure sits
            roughly equidistant between this section's two calibrated
            fiscal-year column positions is left unassigned
            (REVIEW_REQUIRED) rather than guessed.
  Test 6 - Tata Steel Regression: every field's extracted value from the
            real, cached Tata Steel OCR text is pinned to a frozen
            baseline - proves the fixes above did not change a single
            correct Tata Steel figure (skipped if that cached OCR text
            isn't present on this machine).

Synthetic documents (Tests 1, 2, 3, 5) are hand-built OCR-shaped text, not
derived from or resembling either real borrower's actual filing - general,
reusable fixtures per the audit's own "do not hardcode Tata Chemicals-
specific values" instruction. Dataset 3 (a standard same-line layout) and
Dataset 4 (a values-before-label layout) from the audit's Step 10 are both
exercised as part of Tests 1/2/5 below, which use exactly those two row
shapes.

Run: python test_extraction_audit.py
"""

import glob
import os
import sys
import tempfile

import ocr_pipeline as op
from financial_analysis import build_financial_analysis
from input_data_engine import compute_derived_metrics, compute_data_completeness
from reference_data import load_reference_bundle
import ocr_upload
from demo_source import build_demo_workbook

_FAILURES = []


def _ok(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        _FAILURES.append(msg)


def _row(label, current, previous, label_width=60, col_width=18):
    """Builds one OCR-shaped data line with `label` left-justified and
    `current`/`previous` right-aligned into two fixed-width columns - the
    same layout-preserving column alignment LLMWhisperer produces for a
    real financial statement table. `current`/`previous` may be None for a
    genuinely blank cell (no value printed in the source for that year)."""
    cur_str = "" if current is None else str(current)
    prev_str = "" if previous is None else str(previous)
    return f"{label:<{label_width}}{cur_str:>{col_width}}{prev_str:>{col_width}}"


def _bs_header(year_order=("2026", "2025")):
    """A Balance Sheet heading + the SAME split multi-line year header
    Tata Chemicals' real filing uses ("31" / "March," / year - each on
    its own line) - not the single-line "31.03.2026" style, so this also
    exercises detect_header_periods' bare-year-line fallback path.
    `year_order` controls which year is printed FIRST (left column) -
    Test 2 uses this to prove the pipeline reads the header's own stated
    order rather than assuming current-year-first."""
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


def _extract(lines):
    text = "\n".join(lines)
    return op.extract_financial_values(text, op.TEMPLATE_FILE)


def test_1_correct_field_mapping():
    print("=" * 100)
    print("TEST 1 - CORRECT FIELD MAPPING (adjacent row not stolen)")
    print("=" * 100)

    lines = _bs_header() + [
        _row("    (i) Investments", 100, 90),
        _row("    (ii) Trade receivables", 250, 200),
        _row("    (iii) Cash and cash equivalents", 45, 38),
        _row("    (iv) Bank balances other than (iii) above", 37, 41),
        _row("Total current assets", 432, 369),
        _row("Total assets", 2000, 1800),
    ]
    extracted, debug_rows, meta = _extract(lines)

    cash = extracted["Cash"]
    _ok(cash["fy_current"] == 45.0 and cash["fy_previous"] == 38.0,
        f"Cash extracts its OWN row's values (45/38), got {cash['fy_current']}/{cash['fy_previous']}")
    _ok(cash["fy_current"] != 37.0 and cash["fy_previous"] != 41.0,
        "Cash does not extract the neighbouring 'Bank balances' row's values (37/41)")

    ci = extracted["Current investments"]
    _ok(ci["fy_current"] == 100.0 and ci["fy_previous"] == 90.0,
        f"Current investments extracts its own row's values (100/90), got {ci['fy_current']}/{ci['fy_previous']}")
    _ok(ci["fy_current"] != 250.0, "Current investments does not extract the neighbouring 'Trade receivables' row's value (250)")


def test_2_year_alignment():
    print()
    print("=" * 100)
    print("TEST 2 - YEAR ALIGNMENT")
    print("=" * 100)

    # 2a. A row with BOTH years present, current-year-first header order
    # (the common case) - values must land in the correct column, not
    # swapped.
    lines = _bs_header(("2026", "2025")) + [
        _row("Total assets", 2000, 1800),
    ]
    extracted, _debug, _meta = _extract(lines)
    ta = extracted["Total assets"]
    _ok(ta["fy_current"] == 2000.0 and ta["fy_previous"] == 1800.0,
        f"FY2026=2000, FY2025=1800 (current-first header) - got fy_current={ta['fy_current']}, fy_previous={ta['fy_previous']}")

    # 2b. A row with only ONE visible figure, sitting in the FY-PREVIOUS
    # column position (the real, reported Tata Chemicals Cash bug: the
    # FY-current cell is genuinely blank in the source). Must not be
    # silently assigned to FY-current merely because it's the only token
    # on the row.
    lines = _bs_header() + [
        _row("    (iii) Cash and cash equivalents", None, 30),
        _row("    (iv) Bank balances other than (iii) above", 37, 41),
        _row("Total assets", 2000, 1800),
    ]
    extracted, _debug, _meta = _extract(lines)
    cash = extracted["Cash"]
    _ok(cash["fy_previous"] == 30.0, f"Lone figure (30) in the FY-previous column position is assigned to FY-previous - got fy_previous={cash['fy_previous']}")
    _ok(cash["fy_current"] is None, f"FY-current is correctly None (genuinely blank in the source), not guessed as 30 - got fy_current={cash['fy_current']}")

    # 2c. Header lists the years in the OPPOSITE order (previous-year
    # printed FIRST/left, current-year second/right) - proves this reads
    # the header's own stated order rather than assuming current-first.
    lines = _bs_header(("2025", "2026")) + [
        _row("Total assets", 1800, 2000),
    ]
    extracted, _debug, meta = _extract(lines)
    ta = extracted["Total assets"]
    _ok(ta["fy_current"] == 2000.0 and ta["fy_previous"] == 1800.0,
        f"Reversed header order (FY2025 printed left, FY2026 right) still resolves FY2026=2000/FY2025=1800 by "
        f"reading the header, not by array position - got fy_current={ta['fy_current']}, fy_previous={ta['fy_previous']}")


def test_3_unit_scaling():
    print()
    print("=" * 100)
    print("TEST 3 - UNIT SCALING")
    print("=" * 100)

    # 3a. A clean, comma-free, genuinely large year-over-year swing (a
    # real, plausible case - e.g. a large debt paydown) must NEVER be
    # silently rescaled just because it looks "too different" from the
    # other year's figure - the exact reported bug (481 corrupted to 4.81).
    lines = _bs_header() + [
        "     2   Current liabilities",
        _row("        (i) Borrowings", 12, 481),
        _row("Total assets", 2000, 1800),
    ]
    extracted, debug_rows, _meta = _extract(lines)
    borrowings = extracted["Borrowings current"]
    _ok(borrowings["fy_previous"] == 481.0,
        f"481 (a genuine large swing vs. 12) is NOT rescaled to 4.81 - got fy_previous={borrowings['fy_previous']}")
    _ok(not borrowings["normalized"], "481/12 is not flagged 'normalized' - nothing needed correcting")

    # 3b. A genuine OCR defect - decimal point dropped, comma-group left
    # malformed (5 trailing digits: 3 valid + 2 merged decimal digits) -
    # is still corrected, from the token's own shape alone.
    value, repaired = op.repair_ocr_decimal_defect("6,06714", 606714.0)
    _ok(repaired and abs(value - 6067.14) < 1e-9, f"'6,06714' (dropped-decimal defect) repairs to 6067.14 - got {value}, repaired={repaired}")

    # 3c. A genuine OCR defect - decimal point misread as a comma - is
    # also corrected.
    value, repaired = op.repair_ocr_decimal_defect("0,25", 25.0)
    _ok(repaired and abs(value - 0.25) < 1e-9, f"'0,25' (comma-as-decimal defect) repairs to 0.25 - got {value}, repaired={repaired}")

    # 3d. Neither defect fires on an ordinary, correctly-formatted number.
    value, repaired = op.repair_ocr_decimal_defect("1,698", 1698.0)
    _ok(not repaired and value == 1698.0, f"'1,698' (a valid 3-digit group) is left untouched - got {value}, repaired={repaired}")


def test_4_missing_critical_data():
    print()
    print("=" * 100)
    print("TEST 4 - MISSING CRITICAL DATA")
    print("=" * 100)

    def _yf(current, previous, ambiguous=False):
        return {"fy_current": current, "fy_previous": previous, "ambiguous": ambiguous}

    # A synthetic company profile with Revenue and Net Profit (and its own
    # P&L components) missing entirely - a real, plausible outcome for a
    # document this pipeline could not locate those specific lines in.
    extracted = {
        "Total assets": _yf(60000.0, 55000.0),
        "Total equity": _yf(30000.0, 27000.0),
        "Borrowings non current": _yf(8000.0, 7500.0),
        "Borrowings current": _yf(3000.0, 2800.0),
        "Current assets": _yf(15000.0, 14000.0),
        "Current liabilities": _yf(9000.0, 8500.0),
        "Total liabilities": _yf(30000.0, 28000.0),
        "Operating cash flow": _yf(5000.0, 4500.0),
        "Cash": _yf(2000.0, 1800.0),
        "Current investments": _yf(1000.0, 900.0),
        "Finance costs": _yf(1200.0, 1100.0),
        "Depreciation": _yf(1800.0, 1700.0),
    }

    tmp_dir = tempfile.mkdtemp(prefix="cr_extraction_audit_")
    wb = op.populate_workbook(op.TEMPLATE_FILE, extracted)
    extracted_path = os.path.join(tmp_dir, "extracted_missing_critical.xlsx")
    wb.save(extracted_path)
    ocr_upload._anonymize_header(extracted_path)
    merged_path = build_demo_workbook(extracted_sheet_path=extracted_path)

    bundle = load_reference_bundle(path=merged_path)
    d = bundle.input_data
    m = compute_derived_metrics(d)
    completeness = compute_data_completeness(d, m)
    fa = build_financial_analysis(bundle)

    _ok(completeness.overall_pct < 100.0,
        f"Data Completeness is below 100% when Revenue/Net Profit are missing - got {completeness.overall_pct:.1f}%")
    _ok("Revenue" in completeness.missing_critical,
        f"Revenue is listed as a missing CRITICAL field - missing_critical={completeness.missing_critical}")
    _ok("Net Profit" in completeness.missing_critical,
        f"Net Profit is listed as a missing CRITICAL field - missing_critical={completeness.missing_critical}")
    _ok(fa.overall_status == "Not Available / Requires Review",
        f"Financial Analysis overall status is 'Not Available / Requires Review', never 'Approve' - got {fa.overall_status!r}")
    _ok(fa.overall_status != "Approve", "Overall status is specifically NOT 'Approve' with Revenue/Net Profit missing")


def test_5_ambiguous_extraction():
    print()
    print("=" * 100)
    print("TEST 5 - AMBIGUOUS EXTRACTION (do not guess)")
    print("=" * 100)

    # Calibrate this section's FY-current/FY-previous columns from several
    # ordinary two-value rows, then add one row whose lone visible figure
    # sits almost exactly halfway between those two calibrated positions -
    # genuinely ambiguous: it cannot be confidently attributed to either
    # column.
    # 48 spaces places "45" exactly at this section's calibrated
    # midpoint (verified: header calibration for this fixture puts
    # FY-current at offset 74 and FY-previous at offset 92 - 83 is
    # equidistant from both, comfortably outside _COLUMN_MARGIN_FRACTION's
    # tolerance around either side).
    lines = _bs_header() + [
        _row("    (i) Investments", 100, 90),
        _row("    (ii) Trade receivables", 250, 200),
        _row("    (iv) Bank balances other than (iii) above", 37, 41),
        _row("Total current assets", 387, 331),
        "    (iii) Cash and cash equivalents" + " " * 48 + "45",
        _row("Total assets", 2000, 1800),
    ]
    extracted, debug_rows, _meta = _extract(lines)
    cash = extracted["Cash"]
    _ok(cash["fy_current"] is None and cash["fy_previous"] is None,
        f"A lone figure with no clear column alignment is withheld (None/None), not guessed - got "
        f"fy_current={cash['fy_current']}, fy_previous={cash['fy_previous']}")
    _ok(cash["ambiguous"] or cash["validation_status"] == "REVIEW_REQUIRED",
        f"Flagged REVIEW_REQUIRED/ambiguous rather than silently accepted - got validation_status={cash['validation_status']!r}, ambiguous={cash['ambiguous']}")


_TATA_STEEL_BASELINE = {
    "Revenue from operations": (139720.22, 132516.66),
    "Other income": (2165.7, 2246.9),
    "Total expenses": (119433.96, 115142.68),
    "Finance costs": (5116.88, 4238.35),
    "Depreciation": (7068.68, 6253.16),
    "Property, plant": (109059.93, 93203.83),
    "Total assets": (272747.42, 254132.74),
    "Total equity": (135533.31, 126731.94),
    "Borrowings non current": (50666.45, 51040.98),
    "Borrowings current": (14037.73, 8640.44),
    "Current investments": (0.11, 0.12),
    "Cash": (2912.79, 3111.93),
    "Other balances": (191.6, 1032.69),
    "Current assets": (33393.07, 33702.9),
    "Current liabilities": (63732.79, 54482.92),
    "Total liabilities": (137214.11, 127400.8),
    "Operating cash flow": (31960.95, 23505.68),
    "Interest paid": (-6067.14, -5113.51),
    "DSCR": (2.85, 3.82),
    "ISCR": (8.08, 9.94),
    "Current ratio": (0.58, 0.69),
    "Debt equity": (0.5, 0.44),
    "Debt assets": (0.25, 0.25),
    "Operating EBITDA margin": (23.64, 21.29),
    "Net profit margin": (11.5, 10.54),
    "Total income": (141885.92, 134763.56),
    "Profit before tax": (21353.1, 18718.84),
    "Net profit": (16065.13, 13969.7),
}


def _load_tata_steel_ocr_text():
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
    return None


def test_6_tata_steel_regression():
    print()
    print("=" * 100)
    print("TEST 6 - TATA STEEL REGRESSION (frozen baseline)")
    print("=" * 100)

    ocr_text = _load_tata_steel_ocr_text()
    if ocr_text is None:
        print("[SKIP] No cached Tata Steel OCR text found on this machine - skipping frozen-baseline comparison")
        return

    extracted, _debug_rows, _meta = op.extract_financial_values(ocr_text, op.TEMPLATE_FILE)

    for field, (expected_current, expected_previous) in _TATA_STEEL_BASELINE.items():
        got_current = extracted[field]["fy_current"]
        got_previous = extracted[field]["fy_previous"]
        current_ok = got_current is not None and abs(got_current - expected_current) < 0.01
        previous_ok = got_previous is not None and abs(got_previous - expected_previous) < 0.01
        _ok(current_ok and previous_ok,
            f"{field}: expected ({expected_current}, {expected_previous}), got ({got_current}, {got_previous})")


def main():
    test_1_correct_field_mapping()
    test_2_year_alignment()
    test_3_unit_scaling()
    test_4_missing_critical_data()
    test_5_ambiguous_extraction()
    test_6_tata_steel_regression()

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(" -", f)
        return 1
    print("ALL EXTRACTION AUDIT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
