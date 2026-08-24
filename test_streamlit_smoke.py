"""
Headless smoke test for app.py's navigation and page rendering, using
streamlit.testing.v1.AppTest (no browser needed). Written to verify the
director-requested navigation/page changes:
  - Continuous Monitoring is no longer a standalone nav entry.
  - Covenant Monitoring shows Financial Covenants, Non-Financial
    Covenants, and Continuous/Early-Warning Monitoring together.
  - Limit Setting shows Fund-Based (FB) and Non-Fund-Based (NFB)
    sections alongside the existing term-loan engine.
  - Credit Memo shows a consolidated Covenant Assessment grouping and
    the FB/NFB rows, with no standalone Continuous Monitoring row.
  - Risk Scoring appears exactly once, as the LAST nav item.
  - Every page renders without an exception, using a synthetic
    (non-Tata-Steel) dataset - proving nothing is hardcoded to the
    reference company.

Run: python test_streamlit_smoke.py
"""

import os
import shutil
import sys
import tempfile

from streamlit.testing.v1 import AppTest

from reference_data import REFERENCE_WORKBOOK_PATH
from test_dynamic_data import generate_dataset_b
from test_upload_pipeline_ui import _load_realistic_ocr_text
import ocr_pipeline
from demo_source import build_demo_workbook

os.environ["CREDIT_REVIEW_TOOL_ADVANCED_MODE"] = "1"

_FAILURES = []


def _ok(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        _FAILURES.append(msg)


def main():
    tmp_dir = tempfile.mkdtemp(prefix="cr_smoke_")
    dataset_b_path = os.path.join(tmp_dir, "dataset_b.xlsx")
    generate_dataset_b(REFERENCE_WORKBOOK_PATH, dataset_b_path)

    at = AppTest.from_file("app.py")
    at.session_state["source_mode"] = "uploaded"
    at.session_state["uploaded_workbook_path"] = dataset_b_path
    at.session_state["uploaded_workbook_name"] = "dataset_b.xlsx"
    # Deliberately pre-pollute with a stale non-None value, simulating what
    # session_state could hold left over from an earlier code revision
    # (Streamlit does not clear session_state on a hot-reload) - proves the
    # prefill is keyed on upload identity, not "is the value currently
    # None", and therefore self-heals rather than getting stuck at 0.0.
    at.session_state["fb_annual_sales"] = 0.0
    at.session_state["_fb_annual_sales_prefilled_for"] = "stale:from-a-previous-code-revision"
    at.run(timeout=60)
    _ok(not at.exception, f"initial run (Data Upload page) has no exception: {[str(e) for e in at.exception]}")
    _ok(at.session_state["fb_annual_sales"] == 95000.0,
        f"Annual Sales self-heals from a stale pre-existing 0.0 to Dataset B's real Revenue (95000.0) - got {at.session_state['fb_annual_sales']}")

    area_labels = list(at.sidebar.radio[0].options)
    print("AREAS:", area_labels)
    _ok(area_labels == [
        "1. Data Upload", "2. Input & Validation", "3. Financial Analysis",
        "4. Covenant Monitoring", "5. Collateral Assessment", "6. Limit Setting",
        "7. Portfolio Monitoring", "8. Credit Memo", "9. Risk Scoring",
    ], "AREAS list matches the approved final navigation exactly")
    _ok(not any("Continuous Monitoring" in a for a in area_labels),
        "'Continuous Monitoring' is not a standalone nav entry")
    _ok(sum(1 for a in area_labels if "Risk Scoring" in a) == 1,
        "'Risk Scoring' appears in the nav exactly once")
    _ok(area_labels[-1] == "9. Risk Scoring", "'Risk Scoring' is the LAST nav item")

    page_text_by_area = {}
    for area in area_labels:
        at.sidebar.radio[0].set_value(area)
        at.run(timeout=60)
        _ok(not at.exception, f"page '{area}' renders with no exception: {[str(e) for e in at.exception]}")
        texts = [m.value for m in at.markdown] + [c.value for c in at.caption]
        page_text_by_area[area] = "\n".join(str(t) for t in texts)

    cov_page = page_text_by_area["4. Covenant Monitoring"]
    _ok("Financial Covenants" in cov_page, "Covenant Monitoring page contains a Financial Covenants section")
    _ok("Non-Financial Covenants" in cov_page, "Covenant Monitoring page contains a Non-Financial Covenants section")
    _ok("Continuous / Early-Warning Monitoring" in cov_page, "Covenant Monitoring page contains the merged Continuous/Early-Warning Monitoring section")
    _ok("Not Available / Requires Review" in cov_page, "Non-financial covenants show 'Not Available / Requires Review', not a fabricated status")
    _ok("Additional Borrowing Restrictions" in cov_page, "Non-financial covenant taxonomy row present: Additional Borrowing Restrictions")
    _ok("Change of Control" in cov_page, "Non-financial covenant taxonomy row present: Change of Control")

    limit_page = page_text_by_area["6. Limit Setting"]
    _ok("Fund-Based (FB) Limit Assessment" in limit_page, "Limit Setting page contains Fund-Based (FB) Limit Assessment")
    _ok("Non-Fund-Based (NFB) Limit Assessment" in limit_page, "Limit Setting page contains Non-Fund-Based (NFB) Limit Assessment")
    _ok("NFB Benchmarking" in limit_page, "Limit Setting page contains an NFB Benchmarking section")
    _ok("Auto-populated from Revenue" in limit_page, "Annual Sales is labeled 'Auto-populated from Revenue' (provenance UI requirement)")
    _ok(limit_page.count("Analyst-entered") >= 8,
        f"all 8 fields with no data source (Inventory/Receivable/Payable Days, Contribution %, and 4 NFB fields) are labeled 'Analyst-entered' - found {limit_page.count('Analyst-entered')}")

    memo_page = page_text_by_area["8. Credit Memo"]
    _ok("Covenant Assessment" in memo_page, "Credit Memo contains a consolidated Covenant Assessment grouping")
    _ok("Area 7 - Continuous Monitoring" not in memo_page, "Credit Memo has no standalone 'Area 7 - Continuous Monitoring' row")
    _ok("Fund-Based (FB) Assessment" in memo_page, "Credit Memo reflects the Fund-Based (FB) assessment")
    _ok("Non-Fund-Based (NFB) Assessment" in memo_page, "Credit Memo reflects the Non-Fund-Based (NFB) assessment")

    risk_page = page_text_by_area["9. Risk Scoring"]
    _ok("Risk Factor Breakdown" in risk_page, "Risk Scoring page (moved to last) still renders its existing content unchanged")
    _ok("Financial Strength" in risk_page, "Risk Scoring page still shows its existing Financial Strength section")

    # Dataset B doesn't disclose Debtors/Inventory turnover ratios (its
    # '1-Input Data' rows 74/78 are blank, same as the un-extended policy
    # workbook it's built from) - both fields must correctly fall back to
    # analyst-entered/blank here, not silently show a stale or fabricated
    # value.
    _ok("Auto-populated from Inventory Turnover Ratio" not in limit_page,
        "Dataset B (no disclosed turnover ratios) does NOT show Inventory Days as auto-populated")
    _ok("Auto-populated from Debtors Turnover Ratio" not in limit_page,
        "Dataset B (no disclosed turnover ratios) does NOT show Receivable Days as auto-populated")

    # No Tata Steel example figures anywhere in any rendered page, for this
    # synthetic (non-Tata-Steel) dataset with no FB/NFB inputs entered.
    # Only the FB/NFB worked example's distinctive COMPUTED results are
    # checked here (9,041 / 2,260 / 6,781) - round inputs like "60,000"
    # are deliberately excluded, since an unrelated fixture value can
    # coincidentally match a round number (confirmed: Dataset B's own
    # Collateral Assessment fixture uses 60,000 for PP&E book value,
    # unrelated to the FB example's Annual Sales figure).
    tata_steel_numbers = ["139,720", "69,049", "9,041", "2,260", "6,781"]
    all_text = "\n".join(page_text_by_area.values())
    for n in tata_steel_numbers:
        _ok(n not in all_text, f"Tata Steel example figure '{n}' does not appear anywhere in the rendered app")

    # Strongest proof the FB calculation is genuinely data-driven, not
    # hardcoded: feed it the team's own worked-example inputs through the
    # real UI widgets and confirm the output matches that example's
    # arithmetic (~Rs 6,781 Cr) - then change ONE input and confirm the
    # output changes accordingly, proving it isn't pinned to that figure.
    at.sidebar.radio[0].set_value("6. Limit Setting")
    at.run(timeout=60)

    def _set_number_input(session_key, value):
        # Each field's real widget uses a private "_{session_key}_widget"
        # key, not session_key itself - see _persistent_number_input in
        # app.py (a widget-bound key can silently revert to an earlier
        # value once its widget is skipped for a run and re-instantiated
        # later, so the durable value lives under session_key while the
        # widget itself is bound to this derived key).
        widget_key = f"_{session_key}_widget"
        for w in at.number_input:
            if w.key == widget_key:
                w.set_value(value)
                return
        raise AssertionError(f"number_input with key={widget_key!r} not found")

    _set_number_input("fb_annual_sales", 60000.0)
    _set_number_input("fb_inventory_days", 60.0)
    _set_number_input("fb_receivable_days", 45.0)
    _set_number_input("fb_payable_days", 50.0)
    _set_number_input("fb_contribution_pct", 25.0)
    at.run(timeout=60)
    limit_page_a = "\n".join(str(m.value) for m in at.markdown)
    _ok("₹6,781 Cr" in limit_page_a or "₹6,780 Cr" in limit_page_a or "₹6,782 Cr" in limit_page_a,
        f"FB requirement matches the worked example's arithmetic (~Rs 6,781 Cr) when its exact inputs are entered through the real UI widgets")

    _set_number_input("fb_annual_sales", 24000.0)
    _set_number_input("fb_inventory_days", 20.0)
    _set_number_input("fb_receivable_days", 15.0)
    _set_number_input("fb_payable_days", 10.0)
    _set_number_input("fb_contribution_pct", 30.0)
    at.run(timeout=60)
    limit_page_b = "\n".join(str(m.value) for m in at.markdown)
    _ok("₹6,781 Cr" not in limit_page_b, "changing the FB inputs changes the FB requirement away from 6,781 Cr - not pinned to the worked example")
    # cycle = 20+15-10 = 25 days; WC = 24000*25/365 = 1,643.84; contribution 30% = 493.15; FB = 1,150.68 -> rounds to 1,151
    _ok("₹1,151 Cr" in limit_page_b, "FB requirement recalculates correctly for the new, different inputs (expected ~Rs 1,151 Cr)")

    # An analyst's manual edit must survive an unrelated rerun of the SAME
    # upload (navigating to another page and back) - the prefill must only
    # re-fire on a genuinely new/different upload identity, never clobber
    # an edit the analyst has already made for the document still loaded.
    at.sidebar.radio[0].set_value("3. Financial Analysis")
    at.run(timeout=60)
    at.sidebar.radio[0].set_value("6. Limit Setting")
    at.run(timeout=60)
    _ok(at.session_state["fb_annual_sales"] == 24000.0,
        f"analyst's manually-entered Annual Sales (24000.0) survives navigating away and back within the same upload - got {at.session_state['fb_annual_sales']}")

    shutil.rmtree(tmp_dir, ignore_errors=True)

    # --------------------------------------------------------------
    # Real Tata Steel data: confirm the newly-wired Debtors/Inventory
    # turnover fields show as genuinely auto-populated (not the removed
    # "reference workbook parity" echo, not analyst-entered/blank) - no
    # real OCR call, reusing the genuine previously-saved raw OCR text
    # already on this machine, same as test_upload_pipeline_ui.py.
    # --------------------------------------------------------------
    real_tmp_dir = tempfile.mkdtemp(prefix="cr_smoke_real_")
    ocr_text = _load_realistic_ocr_text()
    extracted, debug_rows, meta = ocr_pipeline.extract_financial_values(ocr_text, ocr_pipeline.TEMPLATE_FILE)
    wb = ocr_pipeline.populate_workbook(ocr_pipeline.TEMPLATE_FILE, extracted)
    extracted_path = os.path.join(real_tmp_dir, "extracted.xlsx")
    wb.save(extracted_path)
    merged_path = build_demo_workbook(extracted_sheet_path=extracted_path)

    at_real = AppTest.from_file("app.py")
    at_real.session_state["source_mode"] = "uploaded"
    at_real.session_state["uploaded_workbook_path"] = merged_path
    at_real.session_state["uploaded_workbook_name"] = "tata_steel_real.xlsx"
    at_real.run(timeout=60)
    _ok(not at_real.exception, f"real Tata Steel data: initial run has no exception: {[str(e) for e in at_real.exception]}")

    at_real.sidebar.radio[0].set_value("3. Financial Analysis")
    at_real.run(timeout=60)
    fa_text = "\n".join(str(m.value) for m in at_real.markdown) + "\n".join(str(c.value) for c in at_real.caption)
    _ok("reference-workbook" not in fa_text.lower() and "parity" not in fa_text.lower(),
        "Financial Analysis no longer shows the retired 'reference workbook parity' echo language")
    _ok("62" in fa_text or "67" in fa_text, "Financial Analysis's Key Ratios table shows the real disclosed Inventory Days value")

    at_real.sidebar.radio[0].set_value("6. Limit Setting")
    at_real.run(timeout=60)
    real_limit_text = "\n".join(str(m.value) for m in at_real.markdown) + "\n".join(str(c.value) for c in at_real.caption)
    _ok("Auto-populated from Inventory Turnover Ratio" in real_limit_text,
        "real Tata Steel data: Inventory Days shows as auto-populated from the disclosed turnover ratio")
    _ok("Auto-populated from Debtors Turnover Ratio" in real_limit_text,
        "real Tata Steel data: Receivable Days shows as auto-populated from the disclosed turnover ratio")
    _ok(at_real.session_state["fb_inventory_days"] == 62.0,
        f"Inventory Days prefilled with the real disclosed value (62.0) - got {at_real.session_state['fb_inventory_days']}")
    _ok(at_real.session_state["fb_receivable_days"] == 4.0,
        f"Receivable Days prefilled with the real disclosed value (4.0) - got {at_real.session_state['fb_receivable_days']}")

    shutil.rmtree(real_tmp_dir, ignore_errors=True)

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(" -", f)
        sys.exit(1)
    print("ALL STREAMLIT SMOKE TESTS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
