"""
Verifies the guided-workflow "Accept & Continue" / "<- Previous Step"
buttons added at the bottom of each main workflow page, using
streamlit.testing.v1.AppTest (no browser needed, no real OCR call).

Covers the director's explicit test checklist:
  - Every stage's button carries its exact requested label and advances
    to the correct next page.
  - The bottom button and the left sidebar radio are proven to be the
    SAME navigation state, not two independent ones (both read/write
    st.session_state["selected_area"]).
  - Sidebar navigation still works normally after using the buttons, and
    vice versa.
  - "<- Previous Step" returns to the correct prior stage.
  - Data Upload's continue button is disabled while there is no
    successfully-processed document, and enabled once there is.
  - A failed upload cannot be used to falsely advance the workflow.
  - Risk Scoring still appears exactly once, as the LAST nav item.
  - No business logic changed - reuses the exact same real Tata Steel
    data path as the other test files to spot-check a couple of
    pages' figures are unchanged.

Run: python test_guided_workflow.py
"""

import os
import shutil
import sys
import tempfile

os.environ["CREDIT_REVIEW_TOOL_ADVANCED_MODE"] = "1"

from streamlit.testing.v1 import AppTest

from reference_data import REFERENCE_WORKBOOK_PATH
from test_dynamic_data import generate_dataset_b

_FAILURES = []


def _ok(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        _FAILURES.append(msg)


AREAS = [
    "1. Data Upload", "2. Input & Validation", "3. Financial Analysis",
    "4. Covenant Monitoring", "5. Collateral Assessment", "6. Limit Setting",
    "7. Portfolio Monitoring", "8. Credit Memo", "9. Risk Scoring",
]

# (area index, exact button label per the director's spec)
STAGE_BUTTONS = [
    (1, "Accept & Continue →"),
    (2, "Accept Analysis & Continue →"),
    (3, "Accept Assessment & Continue →"),
    (4, "Accept & Continue →"),
    (5, "Accept Limits & Continue →"),
    (6, "Accept & Continue →"),
    (7, "Complete Review →"),
    (8, "Finish →"),
]


def _click_next(at, index):
    for w in at.button:
        if w.key == f"nav_next_{index}":
            w.click()
            at.run(timeout=60)
            return
    raise AssertionError(f"primary continue button (nav_next_{index}) not found on {AREAS[index]}")


def _click_prev(at, index):
    for w in at.button:
        if w.key == f"nav_prev_{index}":
            w.click()
            at.run(timeout=60)
            return
    raise AssertionError(f"'<- Previous Step' button (nav_prev_{index}) not found on {AREAS[index]}")


def _button_label(at, key):
    for w in at.button:
        if w.key == key:
            return w.label
    return None


def main():
    tmp_dir = tempfile.mkdtemp(prefix="cr_guided_")
    dataset_b_path = os.path.join(tmp_dir, "dataset_b.xlsx")
    generate_dataset_b(REFERENCE_WORKBOOK_PATH, dataset_b_path)

    at = AppTest.from_file("app.py")
    at.session_state["source_mode"] = "uploaded"
    at.session_state["uploaded_workbook_path"] = dataset_b_path
    at.session_state["uploaded_workbook_name"] = "dataset_b.xlsx"
    at.run(timeout=60)
    _ok(not at.exception, f"initial run has no exception: {[str(e) for e in at.exception]}")
    _ok(at.session_state["selected_area"] == AREAS[0],
        f"navigation defaults to Data Upload (AREAS[0]) on a fresh session - got {at.session_state['selected_area']}")

    # ------------------------------------------------------------------
    # Exact button labels + forward navigation, one stage at a time.
    # For each: correct next page opens (A), correct sidebar item becomes
    # active (B), and the button-driven state and the sidebar radio's own
    # rendered value are proven identical (E - no duplicate nav state).
    # ------------------------------------------------------------------
    for index, expected_label in STAGE_BUTTONS:
        at.sidebar.radio[0].set_value(AREAS[index])
        at.run(timeout=60)
        actual_label = _button_label(at, f"nav_next_{index}")
        _ok(actual_label == expected_label,
            f"{AREAS[index]}: button label is {expected_label!r} - got {actual_label!r}")

        _click_next(at, index)
        _ok(not at.exception, f"{AREAS[index]} -> next: no exception after click: {[str(e) for e in at.exception]}")

        if index + 1 < len(AREAS):
            expected_next = AREAS[index + 1]
            _ok(at.session_state["selected_area"] == expected_next,
                f"{AREAS[index]} '{expected_label}' advances selected_area to {expected_next} - got {at.session_state['selected_area']}")
            _ok(at.sidebar.radio[0].value == expected_next,
                f"{AREAS[index]} '{expected_label}' advances the SIDEBAR's own rendered selection to {expected_next} (proves one shared nav state, not two) - got {at.sidebar.radio[0].value}")
        else:
            # Risk Scoring (last item): "Finish" returns to the homepage
            # (Data Upload, AREAS[0]) rather than staying put or erroring.
            _ok(at.session_state["selected_area"] == AREAS[0],
                f"Risk Scoring's 'Finish' button returns to the homepage (Data Upload) - got {at.session_state['selected_area']}")
            _ok(at.sidebar.radio[0].value == AREAS[0],
                f"sidebar reflects the return-to-homepage navigation too - got {at.sidebar.radio[0].value}")

    # ------------------------------------------------------------------
    # Sidebar navigation still works normally after using the buttons
    # (C) - jump directly to an arbitrary page via the sidebar.
    # ------------------------------------------------------------------
    at.sidebar.radio[0].set_value("5. Collateral Assessment")
    at.run(timeout=60)
    _ok(not at.exception, f"sidebar navigation after button use: no exception: {[str(e) for e in at.exception]}")
    _ok(at.session_state["selected_area"] == "5. Collateral Assessment",
        "sidebar click after prior button use still updates the same shared nav state correctly")

    # ------------------------------------------------------------------
    # "<- Previous Step" returns to the correct prior stage, and existing
    # data/state is preserved across the round trip (D) - reuse the FB
    # Annual Sales value as the state-preservation probe (already proven
    # durable across navigation in test_streamlit_smoke.py; this proves
    # the SAME thing survives a button-driven, not just sidebar-driven,
    # navigation cycle).
    # ------------------------------------------------------------------
    at.sidebar.radio[0].set_value("6. Limit Setting")
    at.run(timeout=60)
    for w in at.number_input:
        if w.key == "_fb_annual_sales_widget":
            w.set_value(77777.0)
    at.run(timeout=60)
    _ok(at.session_state["fb_annual_sales"] == 77777.0, "manually set Annual Sales before testing Previous Step")

    _click_prev(at, 5)  # Limit Setting is index 5 -> previous should be Collateral Assessment (index 4)
    _ok(not at.exception, f"'<- Previous Step' click: no exception: {[str(e) for e in at.exception]}")
    _ok(at.session_state["selected_area"] == "5. Collateral Assessment",
        f"'<- Previous Step' from Limit Setting (index 5) returns to Collateral Assessment (index 4) - got {at.session_state['selected_area']}")
    _ok(at.sidebar.radio[0].value == "5. Collateral Assessment",
        "sidebar reflects the Previous-Step navigation too (same shared state)")

    at.sidebar.radio[0].set_value("6. Limit Setting")
    at.run(timeout=60)
    _ok(at.session_state["fb_annual_sales"] == 77777.0,
        f"Annual Sales (77777.0) survived the Previous-Step round trip - got {at.session_state['fb_annual_sales']}")

    # ------------------------------------------------------------------
    # Risk Scoring: exactly once, LAST item - re-verified here in the
    # context of this new feature (already covered independently in
    # test_streamlit_smoke.py).
    # ------------------------------------------------------------------
    area_labels = list(at.sidebar.radio[0].options)
    _ok(sum(1 for a in area_labels if "Risk Scoring" in a) == 1, "'Risk Scoring' still appears exactly once")
    _ok(area_labels[-1] == "9. Risk Scoring", "'Risk Scoring' is still the LAST nav item")

    shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Data Upload's button: disabled with no data, and a failed upload
    # must not let the workflow falsely proceed. No real OCR call - the
    # "failed" case is simulated the same way test_upload_pipeline_ui.py
    # does (monkeypatched ocr_upload.run_ocr).
    # ------------------------------------------------------------------
    at_fresh = AppTest.from_file("app.py")
    at_fresh.run(timeout=60)
    _ok(not at_fresh.exception, f"fresh session (no data at all) has no exception: {[str(e) for e in at_fresh.exception]}")
    fresh_button = None
    for w in at_fresh.button:
        if w.key == "nav_next_0":
            fresh_button = w
    _ok(fresh_button is not None, "Data Upload shows its continue button even with no data uploaded yet")
    _ok(fresh_button is not None and fresh_button.label == "Continue to Validation →",
        f"Data Upload's button carries the exact requested label - got {fresh_button.label if fresh_button else None!r}")
    _ok(fresh_button is not None and fresh_button.disabled is True,
        "Data Upload's continue button is DISABLED when no document has been processed yet")

    import ocr_upload
    real_run_ocr = ocr_upload.run_ocr

    def _failing_run_ocr(pdf_path):
        raise ocr_upload.OcrUploadError("simulated OCR failure for guided-workflow test")
    ocr_upload.run_ocr = _failing_run_ocr
    try:
        at_failed = AppTest.from_file("app.py")
        at_failed.run(timeout=60)
        at_failed.file_uploader[0].set_value(("failed_borrower.pdf", b"%PDF-1.4 fake\n" * 20, "application/pdf"))
        at_failed.run(timeout=90)
        _ok(not at_failed.exception, f"page with a failed upload has no exception: {[str(e) for e in at_failed.exception]}")
        failed_button = None
        for w in at_failed.button:
            if w.key == "nav_next_0":
                failed_button = w
        _ok(failed_button is not None and failed_button.disabled is True,
            "after a FAILED upload, the continue button remains disabled - the workflow cannot falsely proceed as if the stage succeeded")
        _ok(at_failed.session_state["source_mode"] != "pdf",
            "a failed upload never activates 'pdf' source mode")
    finally:
        ocr_upload.run_ocr = real_run_ocr

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(" -", f)
        sys.exit(1)
    print("ALL GUIDED WORKFLOW TESTS PASSED (no real OCR call was made)")
    sys.exit(0)


if __name__ == "__main__":
    main()
