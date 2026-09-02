"""
Integration tests for the shared Proposed Exposure wiring (Requested
Amount -> Recommended Limit -> Sanctioned Amount -> Proposed Funded
Exposure / NFB Commitment / Total Proposed Commitment), driven through
the REAL app.py via streamlit.testing.v1.AppTest - not just the pure
proposed_exposure.py functions (see test_proposed_exposure.py for those).

  Test A (PDF-upload mode): changing the analyst-entered Requested Amount
    on the Limit Setting page changes Portfolio Monitoring's "Proposed
    Loan Amount" (now = Total Proposed Commitment) accordingly - proving
    the two modules are actually wired together, not each reading its
    own frozen copy of "the loan amount" (the exact problem this phase
    set out to fix).

  Test B (Advanced/uploaded-workbook mode): the real reference workbook's
    existing behaviour is completely unchanged - Requested Amount stays
    read-only (sourced from '6-Limit Setting'!B4, not session-state),
    Portfolio Monitoring's proposed_loan_amount stays exactly what that
    workbook's own '8-Portfolio Monitoring'!C11 says (5000, per the
    reference workbook's own worked example), and Collateral Assessment's
    existing "as-is" coverage is untouched.

Run: python test_proposed_exposure_integration.py
"""

import os
import shutil
import sys
import tempfile

os.environ["CREDIT_REVIEW_TOOL_ADVANCED_MODE"] = "1"

from streamlit.testing.v1 import AppTest

import ocr_pipeline
import ocr_upload
from demo_source import build_demo_workbook
from reference_data import REFERENCE_WORKBOOK_PATH
from test_multi_borrower_pdf import BORROWER_B_EXTRACTED

_FAILURES = []


def _ok(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        _FAILURES.append(msg)


def _set_number_input(at, session_key, value):
    widget_key = f"_{session_key}_widget"
    for w in at.number_input:
        if w.key == widget_key:
            w.set_value(value)
            return
    raise AssertionError(f"number_input with key={widget_key!r} not found on the current page")


def _page_text(at, area):
    at.sidebar.radio[0].set_value(area)
    at.run(timeout=60)
    texts = [str(m.value) for m in at.markdown] + [str(c.value) for c in at.caption]
    return "\n".join(texts)


def test_a_pdf_mode_requested_amount_flows_downstream():
    print("=" * 100)
    print("TEST A - PDF MODE: changing Requested Amount changes Portfolio Monitoring's exposure")
    print("=" * 100)

    tmp_dir = tempfile.mkdtemp(prefix="cr_exposure_e2e_")
    try:
        wb = ocr_pipeline.populate_workbook(ocr_pipeline.TEMPLATE_FILE, BORROWER_B_EXTRACTED)
        extracted_path = os.path.join(tmp_dir, "extracted_b.xlsx")
        wb.save(extracted_path)
        ocr_upload._anonymize_header(extracted_path)
        merged_path = build_demo_workbook(extracted_sheet_path=extracted_path)
        stable_path = os.path.join(tmp_dir, "merged_b.xlsx")
        shutil.copy(merged_path, stable_path)

        at = AppTest.from_file("app.py")
        at.session_state["source_mode"] = "pdf"
        at.session_state["merged_workbook_path"] = stable_path
        at.session_state["pdf_name"] = "borrower_b.pdf"
        at.run(timeout=60)

        at.sidebar.radio[0].set_value("6. Limit Setting")
        at.run(timeout=60)

        _ok(any(w.key == "_deal_requested_amount_widget" for w in at.number_input),
            "PDF mode: Requested Amount renders as an EDITABLE analyst input on Limit Setting")

        # Zero out FB/NFB so Total Proposed Commitment reduces to exactly
        # the Term Facility Exposure (Sanctioned Amount) - keeps the
        # expected numbers simple and unambiguous for this test, without
        # touching how FB/NFB themselves are computed (see
        # test_proposed_exposure.py for that).
        for key in ("fb_inventory_days", "fb_receivable_days", "fb_payable_days", "nfb_peak_lc", "nfb_peak_bg"):
            _set_number_input(at, key, 0.0)
        _set_number_input(at, "deal_requested_amount", 1000.0)
        at.run(timeout=60)

        limit_page_1 = "\n".join(str(m.value) for m in at.markdown)
        _ok("₹1,000 Cr" in limit_page_1, f"Requested Amount = 1,000 reflected on Limit Setting (Recommended Limit uncapped at this level)")

        portfolio_page_1 = _page_text(at, "7. Portfolio Monitoring")
        _ok("₹1,000 Cr" in portfolio_page_1,
            "Portfolio Monitoring's Proposed Loan Amount reflects Requested Amount = 1,000 (Total Proposed Commitment, FB/NFB zeroed)")

        # Change Requested Amount to a different, still-uncapped value.
        at.sidebar.radio[0].set_value("6. Limit Setting")
        at.run(timeout=60)
        _set_number_input(at, "deal_requested_amount", 3000.0)
        at.run(timeout=60)

        portfolio_page_2 = _page_text(at, "7. Portfolio Monitoring")
        _ok("₹3,000 Cr" in portfolio_page_2,
            "Portfolio Monitoring's Proposed Loan Amount UPDATES to 3,000 after Requested Amount changes")
        _ok("₹1,000 Cr" not in portfolio_page_2,
            "Portfolio Monitoring no longer shows the OLD 1,000 figure - not stuck on a stale/frozen value")

        # Credit Memo's own Exposure Summary must show the SAME figure.
        memo_page_2 = _page_text(at, "8. Credit Memo")
        _ok("3,000" in memo_page_2, "Credit Memo's Proposed Exposure Summary also reflects the new Requested Amount (3,000)")
        _ok("Reconciled" in memo_page_2, "Credit Memo's reconciliation banner reports 'Reconciled' - PDF mode wiring is self-consistent by construction")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_b_advanced_mode_unchanged():
    print()
    print("=" * 100)
    print("TEST B - ADVANCED MODE: reference workbook's existing behaviour is unchanged")
    print("=" * 100)

    at = AppTest.from_file("app.py")
    at.session_state["source_mode"] = "uploaded"
    at.session_state["uploaded_workbook_path"] = REFERENCE_WORKBOOK_PATH
    at.session_state["uploaded_workbook_name"] = "Credit Review Automation Example.xlsx"
    at.run(timeout=60)

    at.sidebar.radio[0].set_value("6. Limit Setting")
    at.run(timeout=60)
    _ok(not at.exception, f"Limit Setting renders with no exception in Advanced mode: {[str(e) for e in at.exception]}")
    _ok(not any(w.key == "_deal_requested_amount_widget" for w in at.number_input),
        "Advanced mode: Requested Amount is NOT an editable widget - stays read-only, sourced from the workbook's own B4")
    limit_page = "\n".join(str(m.value) for m in at.markdown)
    _ok("₹5,000 Cr" in limit_page, "Advanced mode: Requested Amount KPI still shows the reference workbook's own B4 value (5,000), unchanged")

    portfolio_page = _page_text(at, "7. Portfolio Monitoring")
    _ok("₹5,000 Cr" in portfolio_page,
        "Advanced mode: Portfolio Monitoring's Proposed Loan Amount is UNCHANGED at the workbook's own C11 value (5,000)")

    collateral_page = _page_text(at, "5. Collateral Assessment")
    _ok("Post-Sanction Coverage" in collateral_page,
        "Advanced mode: the NEW Post-Sanction Coverage section is additive and still appears")
    _ok("205" in collateral_page or "2.05" in collateral_page,
        "Advanced mode: Existing Coverage Ratio (~205% / 2.05x) is still shown, unchanged by this phase's work")

    memo_page = _page_text(at, "8. Credit Memo")
    _ok("Proposed Exposure Summary" in memo_page, "Advanced mode: Credit Memo's new Exposure Summary section still renders (additive, informational)")


def main():
    test_a_pdf_mode_requested_amount_flows_downstream()
    test_b_advanced_mode_unchanged()

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(" -", f)
        return 1
    print("ALL PROPOSED EXPOSURE INTEGRATION TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
