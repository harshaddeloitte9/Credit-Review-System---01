"""
Proves the application is genuinely multi-borrower/multi-document capable
through the REAL production pipeline app.py itself uses for a PDF upload -
ocr_pipeline.populate_workbook() -> ocr_upload._anonymize_header() ->
demo_source.build_demo_workbook() -> app.py with source_mode="pdf" - not
the direct single-workbook-edit shortcut test_dynamic_data.py uses (which
never exercises build_demo_workbook() and therefore cannot catch a bug
where a policy-workbook sheet leaks into every PDF-uploaded borrower).

Three "documents" are run through that real pipeline:

  Borrower A = the real Tata Steel PDF's own OCR text (already saved on
               this machine - no live OCR call), full Reported Ratios
               disclosure.
  Borrower B = a synthetic, hand-built "extracted" dict (same dict shape
               ocr_pipeline.extract_financial_values() produces), fed
               through the real populate_workbook()/build_demo_workbook()
               calls - a different company profile, full Reported Ratios
               disclosure.
  Borrower C = the same synthetic company as Borrower B, but with its
               entire Reported Ratios block (DSCR/ISCR/Current Ratio/
               Debt-Equity/Debt-Assets/Debtors & Inventory Turnover) and
               Net Worth left out entirely - a real, plausible outcome for
               a genuinely different filing that (unlike Tata Steel's)
               isn't a listed non-convertible-debt issuer subject to that
               specific disclosure requirement.
  Borrower D = the REAL Tata Chemicals filing's own OCR text (cached on
               this machine from manual testing - skipped if unavailable).
               Manual validation against this real document (Phase 1 of
               the extraction/completeness audit) found Revenue, Total
               Expenses, Operating Cash Flow and the entire Reported
               Ratios block (DSCR/ISCR/Current Ratio/Debt-Equity/Net
               Worth/Net Profit Margin) reporting NOT FOUND despite being
               present in the OCR text - root-caused to the section-
               heading regex matching inside ordinary auditor's-report
               prose ("...risks of material misSTATEMENT OF the...
               financial results...") and anchoring the wrong section
               boundary before the real table, plus the P&L's own
               Segment-wise disclosure being silently absorbed into the
               same section as the real P&L table. Both are fixed (see
               ocr_pipeline.SECTION_HEADING_PATTERNS / _merge_adjacent_
               same_heading / the "SEGMENT" boundary type) - this test now
               asserts those fields DO populate with this document's own
               real figures. The same audit also found Current Borrowings
               (₹481 crore) corrupted to ₹4.81 crore by a sibling-
               magnitude "repair" heuristic that guessed a /100 scaling
               error from comparing it to a different year's value, and
               Cash's lone reported figure (₹30 crore, genuinely the
               FY-previous column with FY-current blank/nil) wrongly
               assigned to FY-current - both fixed via, respectively,
               repair_ocr_decimal_defect() (a per-token, comma-shape-only
               repair that never compares to another value) and real
               column-position calibration (see "YEAR-COLUMN ALIGNMENT"
               in ocr_pipeline.py). Most Balance Sheet fields already
               extracted correctly for this document even before that
               work, proving genuinely available fields populate rather
               than the whole document degrading to "Not Available" -
               regression-guarded below alongside the newer fixes. Also
               regression-guards the follow-up fix for four Balance
               Sheet fields (PP&E, Current Investments, Cash, Current
               Liabilities) that used to silently extract a NEIGHBOURING
               line's value instead of their own (ocr_pipeline.
               locate_field's lookahead used to scan forward before
               backward, and never recognised an untracked neighbouring
               line - e.g. Trade Receivables, Bank Balances - as
               belonging to someone else).

This proves, through the real merge path:
  1. No crash for any of the three documents, on any page.
  2. Borrower A's and Borrower B's rendered figures differ everywhere they
     should, and neither's figures appear on the other's pages.
  3. Collateral Assessment shows "Unavailable" for ALL THREE (PDF mode) -
     not Tata Steel's own collateral schedule leaking into a different
     borrower (the specific bug this test suite was written to catch).
  4. Borrower C (missing Reported Ratios) degrades to "Not Available /
     Requires Review" on Financial Analysis, Covenant Monitoring and Risk
     Scoring - never a crash, never a fabricated number.
  5. Re-processing Borrower A again after B and C restores A's exact
     original figures - the pipeline is a pure function of each
     document's own data, not accumulating state across uploads.

Run: python test_multi_borrower_pdf.py
"""

import os
import shutil
import sys
import tempfile

from streamlit.testing.v1 import AppTest

import ocr_pipeline
import ocr_upload
from demo_source import build_demo_workbook
from test_upload_pipeline_ui import _load_realistic_ocr_text, _load_tata_chemicals_ocr_text

os.environ["CREDIT_REVIEW_TOOL_ADVANCED_MODE"] = "1"

_FAILURES = []


def _ok(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        _FAILURES.append(msg)


def _yf(current, previous, ambiguous=False):
    return {"fy_current": current, "fy_previous": previous, "ambiguous": ambiguous}


# A different, smaller company's profile - every figure distinct from
# Tata Steel's real filing. Full Reported Ratios disclosure (this
# borrower's own, not derived from anything Tata-Steel-specific).
BORROWER_B_EXTRACTED = {
    "Revenue from operations": _yf(42000.0, 38000.0),
    "Other income": _yf(300.0, 250.0),
    "Total expenses": _yf(35000.0, 33000.0),
    "Finance costs": _yf(1200.0, 1100.0),
    "Depreciation": _yf(1800.0, 1700.0),
    "Interest income": _yf(150.0, 120.0),
    "Exceptional items": _yf(0.0, 0.0),
    "Total tax expense": _yf(1200.0, 1000.0),

    "Property, plant": _yf(25000.0, 24000.0),
    "Total assets": _yf(60000.0, 55000.0),
    "Total equity": _yf(30000.0, 27000.0),
    "Borrowings non current": _yf(8000.0, 7500.0),
    "Borrowings current": _yf(3000.0, 2800.0),
    "Lease liabilities non current": _yf(500.0, 450.0),
    "Lease liabilities current": _yf(200.0, 180.0),
    "Current investments": _yf(1000.0, 900.0),
    "Cash": _yf(2000.0, 1800.0),
    "Other balances": _yf(300.0, 250.0),
    "Current assets": _yf(15000.0, 14000.0),
    "Current liabilities": _yf(9000.0, 8500.0),
    "Total liabilities": _yf(30000.0, 28000.0),

    "Operating cash flow": _yf(5000.0, 4500.0),
    "Income taxes paid": _yf(1100.0, 950.0),
    "Interest paid": _yf(1150.0, 1050.0),
    "Repayment borrowings": _yf(900.0, 800.0),
    "Lease payment": _yf(100.0, 90.0),
    "Capex": _yf(2500.0, 2300.0),
    "Dividend": _yf(500.0, 400.0),
    "Total comprehensive income": _yf(3000.0, 2600.0),

    "DSCR": _yf(1.80, 1.60),
    "ISCR": _yf(4.20, 3.90),
    "Current ratio": _yf(1.67, 1.65),
    "Debt equity": _yf(0.35, 0.38),
    "Debt assets": _yf(0.185, 0.196),
    "Operating EBITDA margin": _yf(14.5, 13.8),   # percentage points - PERCENTAGE_ROWS divides by 100
    "Net profit margin": _yf(6.2, 5.8),            # percentage points
    "Net worth": _yf(30000.0, 27000.0),
    "Debtors turnover": _yf(35.0, 33.0),
    "Inventory turnover": _yf(48.0, 46.0),

    "Total income": _yf(42300.0, 38250.0),
    "Profit before exceptional items": _yf(7300.0, 5250.0),
    "Profit before tax": _yf(7300.0, 5250.0),
    "Net profit": _yf(6100.0, 4250.0),
}

# Same company as Borrower B, but this filing simply does not carry the
# Reported Ratios / Regulation-52(4)-style disclosure block at all - a
# real, plausible shape for a genuinely different (non-debt-listed)
# borrower's financial statement.
BORROWER_C_EXTRACTED = {
    k: v for k, v in BORROWER_B_EXTRACTED.items()
    if k not in (
        "DSCR", "ISCR", "Current ratio", "Debt equity", "Debt assets",
        "Net worth", "Debtors turnover", "Inventory turnover",
    )
}


def _build_merged_workbook(extracted, out_dir, name):
    """Runs the REAL production sequence app.py's ocr_upload.py uses after
    OCR text has been turned into an `extracted` dict: populate_workbook()
    -> _anonymize_header() -> build_demo_workbook(). Only the OCR/
    extraction step itself is swapped out (a hand-built dict instead of a
    live LLMWhisperer call + extract_financial_values() parse), exactly
    like test_ocr_cache.py / test_upload_pipeline_ui.py already avoid a
    real OCR call elsewhere in this test suite."""
    wb = ocr_pipeline.populate_workbook(ocr_pipeline.TEMPLATE_FILE, extracted)
    extracted_path = os.path.join(out_dir, f"extracted_{name}.xlsx")
    wb.save(extracted_path)
    ocr_upload._anonymize_header(extracted_path)
    merged_path = build_demo_workbook(extracted_sheet_path=extracted_path)
    # build_demo_workbook() writes to a single fixed path shared by every
    # call (see demo_source.py) - copy out immediately so this borrower's
    # own merged snapshot survives the next borrower's call overwriting it.
    stable_path = os.path.join(out_dir, f"merged_{name}.xlsx")
    shutil.copy(merged_path, stable_path)
    return stable_path


def _run_all_pages(merged_path, pdf_name):
    at = AppTest.from_file("app.py")
    at.session_state["source_mode"] = "pdf"
    at.session_state["merged_workbook_path"] = merged_path
    at.session_state["pdf_name"] = pdf_name
    at.run(timeout=60)
    _ok(not at.exception, f"{pdf_name}: Data Upload page has no exception: {[str(e) for e in at.exception]}")

    page_text = {}
    for area in list(at.sidebar.radio[0].options):
        at.sidebar.radio[0].set_value(area)
        at.run(timeout=60)
        _ok(not at.exception, f"{pdf_name}: page '{area}' has no exception: {[str(e) for e in at.exception]}")
        texts = (
            [str(m.value) for m in at.markdown] + [str(c.value) for c in at.caption]
            + [str(w.value) for w in at.warning]
        )
        page_text[area] = "\n".join(texts)
    return page_text


def main():
    tmp_dir = tempfile.mkdtemp(prefix="cr_multi_borrower_")
    try:
        print("=" * 100)
        print("BUILDING THREE BORROWERS THROUGH THE REAL PDF-UPLOAD PIPELINE")
        print("=" * 100)

        # Borrower A: the real Tata Steel PDF's own OCR text (already
        # cached on this machine - see test_upload_pipeline_ui.py).
        ocr_text = _load_realistic_ocr_text()
        extracted_a, _debug_rows, _meta = ocr_pipeline.extract_financial_values(ocr_text, ocr_pipeline.TEMPLATE_FILE)
        merged_a = _build_merged_workbook(extracted_a, tmp_dir, "borrower_a")
        merged_b = _build_merged_workbook(BORROWER_B_EXTRACTED, tmp_dir, "borrower_b")
        merged_c = _build_merged_workbook(BORROWER_C_EXTRACTED, tmp_dir, "borrower_c")
        print(f"  Borrower A (Tata Steel, real OCR) -> {merged_a}")
        print(f"  Borrower B (synthetic, full ratios) -> {merged_b}")
        print(f"  Borrower C (synthetic, no Reported Ratios block) -> {merged_c}")

        tata_chem_text = _load_tata_chemicals_ocr_text()
        merged_d = None
        if tata_chem_text is not None:
            extracted_d, _debug_rows_d, _meta_d = ocr_pipeline.extract_financial_values(tata_chem_text, ocr_pipeline.TEMPLATE_FILE)
            merged_d = _build_merged_workbook(extracted_d, tmp_dir, "borrower_d")
            print(f"  Borrower D (Tata Chemicals, real OCR) -> {merged_d}")
        else:
            print("  Borrower D (Tata Chemicals) skipped - no cached OCR text found on this machine")

        print()
        print("=" * 100)
        print("1. RUNNING BORROWER A THROUGH EVERY PAGE")
        print("=" * 100)
        text_a = _run_all_pages(merged_a, "tata_steel.pdf")

        print()
        print("=" * 100)
        print("2. RUNNING BORROWER B THROUGH EVERY PAGE")
        print("=" * 100)
        text_b = _run_all_pages(merged_b, "borrower_b.pdf")

        print()
        print("=" * 100)
        print("3. RUNNING BORROWER C (NO REPORTED RATIOS BLOCK) THROUGH EVERY PAGE")
        print("=" * 100)
        text_c = _run_all_pages(merged_c, "borrower_c.pdf")

        print()
        print("=" * 100)
        print("4. BORROWER A'S FIGURES DO NOT APPEAR ON BORROWER B'S PAGES (OR VICE VERSA)")
        print("=" * 100)
        # Tata Steel's own distinctive figures (revenue ~139,720; PP&E book
        # value 109,060) must never appear on Borrower B's pages, and
        # Borrower B's distinctive figures (revenue 42,000; PP&E 25,000)
        # must never appear on Borrower A's pages.
        tata_markers = ["139,720", "109,060"]
        borrower_b_markers = ["42,000", "25,000"]
        all_b_text = "\n".join(text_b.values())
        all_a_text = "\n".join(text_a.values())
        for marker in tata_markers:
            _ok(marker not in all_b_text, f"Tata Steel figure '{marker}' does not appear anywhere on Borrower B's pages")
        for marker in borrower_b_markers:
            _ok(marker not in all_a_text, f"Borrower B figure '{marker}' does not appear anywhere on Borrower A's pages")

        fa_a = text_a["4. Financial Analysis"]
        fa_b = text_b["4. Financial Analysis"]
        _ok("Approve" in fa_a or "Reject" in fa_a, "Borrower A's Financial Analysis shows a real Approve/Reject status")
        _ok("Approve" in fa_b or "Reject" in fa_b, "Borrower B's Financial Analysis shows a real Approve/Reject status")
        _ok(fa_a != fa_b, "Borrower A's and Borrower B's Financial Analysis page text differ")

        text_d = None
        if merged_d is not None:
            print()
            print("=" * 100)
            print("3b. RUNNING BORROWER D (REAL TATA CHEMICALS PDF) THROUGH EVERY PAGE")
            print("=" * 100)
            text_d = _run_all_pages(merged_d, "Tata Chemical.pdf")

        print()
        print("=" * 100)
        print("5. COLLATERAL ASSESSMENT: 'Unavailable' for every PDF-uploaded borrower")
        print("=" * 100)
        _borrowers = [("Borrower A (Tata Steel)", text_a), ("Borrower B", text_b), ("Borrower C", text_c)]
        if text_d is not None:
            _borrowers.append(("Borrower D (Tata Chemicals)", text_d))
        for name, text in _borrowers:
            coll_page = text["6. Collateral Assessment"]
            _ok("Unavailable" in coll_page, f"{name}: Collateral Assessment shows 'Unavailable', not a collateral schedule")
            _ok("109,060" not in coll_page, f"{name}: Collateral Assessment does not show Tata Steel's PP&E book value (109,060)")

        print()
        print("=" * 100)
        print("6. BORROWER C (NO REPORTED RATIOS): GRACEFUL 'NOT AVAILABLE', NEVER A CRASH")
        print("=" * 100)
        cov_c = text_c["5. Covenant Monitoring"]
        risk_c = text_c["10. Risk Scoring"]
        fa_c = text_c["4. Financial Analysis"]
        _ok("Not Available" in fa_c, "Borrower C's Financial Analysis shows 'Not Available' for the missing Reported Ratios metrics")
        _ok("Not Available" in cov_c, "Borrower C's Covenant Monitoring shows 'Not Available' rather than crashing on missing DSCR/ISCR")
        _ok("Not Available" in risk_c, "Borrower C's Risk Scoring shows 'Not Available' rather than crashing on missing DSCR/ISCR/Current Ratio/Debt-Equity")

        if text_d is not None:
            print()
            print("=" * 100)
            print("6b. BORROWER D (TATA CHEMICALS): PHASE 1 EXTRACTION FIXES - REVENUE, OCF, THE")
            print("    REPORTED RATIOS BLOCK AND CURRENT BORROWINGS NOW POPULATE CORRECTLY")
            print("=" * 100)
            iv_d = text_d["3. Input & Validation"]
            fa_d = text_d["4. Financial Analysis"]
            # Balance Sheet fields DO extract for this real document (that
            # table's layout matches Tata Steel's) - Total Assets (25,305)
            # and Total Equity (19,308), the Standalone figures, must show.
            _ok("25,305" in iv_d or "25,305" in fa_d, "Borrower D: Total Assets (25,305, this document's own real figure) populates")
            _ok("19,308" in iv_d, "Borrower D: Total Equity (19,308, this document's own real figure) populates")

            # Regression guard for the "wrong adjacent line" extraction fix:
            # Current Investments, Cash, Current Liabilities and PP&E (the
            # PP&E case is the same bug class, caught as a side effect of the
            # same fix) must show THIS document's own correct values, never
            # the neighbouring line's ("PP&E" grabbing "Capital work-in-
            # progress", "Current Investments" grabbing "Trade Receivables",
            # "Cash" grabbing "Bank Balances", "Current Liabilities" grabbing
            # "Other Current Liabilities").
            _ok("5,236.00" in iv_d, "Borrower D: PP&E shows its own correct value (5,236), not Capital WIP's (473)")
            _ok("473.00" not in iv_d, "Borrower D: PP&E does not show Capital Work-in-Progress's value (473)")
            _ok("209.00" in iv_d, "Borrower D: Current Investments shows its own correct value (209), not Trade Receivables' (252)")
            _ok("252.00" not in iv_d, "Borrower D: Current Investments does not show Trade Receivables' value (252)")
            _ok("30.00" in iv_d, "Borrower D: Cash shows its own correct value (30, FY-previous column), not Bank Balances' (37)")
            _ok("37.00" not in iv_d, "Borrower D: Cash does not show Bank Balances' value (37)")
            _ok("1,370.00" in iv_d, "Borrower D: Current Liabilities shows the Total Current Liabilities figure (1,370), not the 'Other Current Liabilities' sub-line's (105)")
            _ok("105.00" not in iv_d, "Borrower D: Current Liabilities does not show 'Other Current Liabilities' sub-line's value (105)")

            # Phase 1 extraction-audit regression guards - the two
            # concretely reported mapping bugs.
            # Current Borrowings: FY2026=12, FY2025=481 (a real ~40x
            # paydown) - the sibling-magnitude "repair" heuristic used to
            # mangle 481 into 4.81 because it looked "too different" from
            # 12; repair_ocr_decimal_defect() (comma-shape-only, never a
            # sibling comparison) leaves a clean "481" untouched.
            _ok("481.00" in iv_d, "Borrower D: Current Borrowings FY-previous shows its real value (481), not corrupted by the old sibling-magnitude heuristic")
            _ok("12.00" in iv_d, "Borrower D: Current Borrowings FY-current shows its real value (12)")
            _ok("4.81" not in iv_d, "Borrower D: Current Borrowings is NOT corrupted to 4.81 (the reported /100 scaling bug)")

            # Revenue, Total Expenses and Operating Cash Flow used to
            # report NOT FOUND (false-positive section-heading match on
            # ordinary auditor's-report prose - see the module docstring)
            # despite being present in the OCR text a few lines later; now
            # populate with this document's own real figures.
            _ok("4,831" in fa_d and "4,441" in fa_d, "Borrower D: Revenue (4,831 / 4,441, this document's own real figures) populates in Financial Analysis")
            _ok("4,524" in iv_d or "4,524" in fa_d, "Borrower D: Total Expenses (4,524) populates")
            _ok("1,119" in fa_d, "Borrower D: Operating Cash Flow (1,119) populates in Financial Analysis")

            # The entire Reported Ratios block used to report NOT FOUND
            # for the same false-positive-section-heading reason (plus a
            # second false match swallowing the P&L section's segment-wise
            # breakdown) - now populates with this document's own real
            # ratios.
            _ok("5.21x" in fa_d, "Borrower D: DSCR (5.21x, this document's own real figure) populates in Financial Analysis")
            _ok("4.36x" in fa_d, "Borrower D: ISCR (4.36x) populates in Financial Analysis")
            _ok("1.35x" in fa_d, "Borrower D: Current Ratio (1.35x) populates in Financial Analysis")
            _ok("0.18x" in fa_d, "Borrower D: Debt-Equity (0.18x) populates in Financial Analysis")

            # Financial Analysis's overall Approve/Reject status is still
            # "Not Available / Requires Review" for this document - but
            # now for a real, narrow, correctly-identified reason (Net
            # Profit's own derivation depends on Exceptional Items, whose
            # row could not be confidently attributed to a fiscal year on
            # this filing's own numbers - see select_fy_values's
            # REVIEW_REQUIRED path), not because Revenue/OCF/the Reported
            # Ratios block were unlocatable (the old, wrong reason - see
            # above, all of them now populate). This proves the Phase 1
            # fix narrows the gap to what's genuinely unresolvable rather
            # than either fabricating Net Profit or silently reusing
            # Revenue/OCF/the ratios' now-correct availability to force
            # an Approve/Reject this document's own numbers do not
            # support.
            _ok("Net Profit" in fa_d and "Not Available / Requires Review" in fa_d,
                "Borrower D: Financial Analysis overall status is 'Not Available / Requires Review', "
                "specifically because Net Profit (not Revenue/OCF/the Reported Ratios) could not be reliably computed")

            cov_d = text_d["5. Covenant Monitoring"]
            risk_d = text_d["10. Risk Scoring"]
            # Risk Scoring's Financial Strength component only needs
            # DSCR/ISCR/Current Ratio/Debt-Equity (not Net Profit), all
            # four of which now extract correctly - so Risk Scoring now
            # resolves to a real grade instead of "Not Available".
            _ok("Low Risk" in risk_d, "Borrower D: Risk Scoring resolves to a real grade (Low Risk) now that DSCR/ISCR/Current Ratio/Debt-Equity all extract correctly")
            # No other borrower's figures leak into Borrower D's pages, and
            # vice versa.
            all_d_text = "\n".join(text_d.values())
            _ok("139,720" not in all_d_text, "Borrower D: Tata Steel's Revenue (139,720) does not appear anywhere")
            _ok("42,000" not in all_d_text, "Borrower D: Borrower B's Revenue (42,000) does not appear anywhere")
            _ok("25,305" not in all_a_text, "Borrower A: Borrower D's Total Assets (25,305) does not appear anywhere")

        print()
        print("=" * 100)
        print("7. RE-PROCESSING BORROWER A AFTER B, C AND D RESTORES A'S EXACT ORIGINAL FIGURES")
        print("=" * 100)
        text_a_again = _run_all_pages(merged_a, "tata_steel.pdf")
        _ok(
            text_a_again["4. Financial Analysis"] == text_a["4. Financial Analysis"],
            "Borrower A's Financial Analysis page is byte-identical before and after Borrower B/C/D were processed",
        )
        _ok(
            text_a_again["10. Risk Scoring"] == text_a["10. Risk Scoring"],
            "Borrower A's Risk Scoring page is byte-identical before and after Borrower B/C/D were processed",
        )

        print()
        if _FAILURES:
            print(f"{len(_FAILURES)} FAILURE(S):")
            for f in _FAILURES:
                print(" -", f)
            return 1
        print("ALL MULTI-BORROWER PDF PIPELINE TESTS PASSED (no fabricated fallback, no crash, no cross-contamination)")
        return 0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
