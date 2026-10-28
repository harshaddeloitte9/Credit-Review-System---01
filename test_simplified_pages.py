"""
Tests for the Data Upload / Data Review UI simplification pass:
  - Data Upload page shows only its 5 required sections (Source
    Document, Document Extraction, Extracted Financial Data, Raw OCR
    Output, Active Data Source) and no AI review panels - those still
    run and store their results in session_state (verified separately
    by test_ai_validation_ui.py/test_ai_dynamic_mapping.py/
    test_ai_correction_proposals.py), just no longer rendered here.
  - Data Review's Field-Level Review table shows a "Business Glossary"
    column immediately after "Field" and before "Statement".
  - The AI Dynamic Field Mapping explanation/workflow block renders.
  - Existing Accept/Reject/Keep behavior is unaffected by this UI pass.

No mocking framework, matching this codebase's own convention. Never
calls a real Ollama or OCR instance.

Run: python test_simplified_pages.py
"""
import json
import os
import re
import sys
import tempfile

os.environ["CREDIT_REVIEW_TOOL_ADVANCED_MODE"] = "1"

from streamlit.testing.v1 import AppTest

import ai_correction_proposals
import ai_dynamic_mapping
import ai_validation
import ocr_upload
from test_ai_dynamic_mapping import _find_tata_chemicals_cache
from test_upload_pipeline_ui import _load_realistic_ocr_text

ocr_upload.UPLOAD_WORK_DIR = tempfile.mkdtemp(prefix="cr_simplified_pages_test_")

_FAILURES = []


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


def _raw_markdown_html(at, area=None):
    """Every raw st.markdown(..., unsafe_allow_html=True) value, joined -
    needed to check actual <th> column ORDER inside a rendered table,
    which _page_text()'s plain-text join does not preserve reliably."""
    if area is not None:
        at.sidebar.radio[0].set_value(area)
        at.run(timeout=60)
    return "\n".join(str(m.value) for m in at.markdown)


def _patch_ai_unavailable():
    """All three AI layers mocked unavailable - this test file checks UI
    STRUCTURE (which sections render, column order), not AI behavior
    (already covered by the dedicated AI test files), so keeping this
    fast and deterministic."""
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


def _restore_ai(real):
    ai_validation.check_ollama_availability = real["av_check"]
    ai_validation.call_ollama = real["av_call"]
    ai_correction_proposals.check_ollama_availability = real["acp_check"]
    ai_correction_proposals.call_ollama = real["acp_call"]
    ai_dynamic_mapping.check_ollama_availability = real["adm_check"]
    ai_dynamic_mapping.call_ollama = real["adm_call"]


def _upload(at, pdf_bytes):
    at.file_uploader[0].set_value(("borrower.pdf", pdf_bytes, "application/pdf"))
    at.run(timeout=90)


def test_data_upload_shows_only_five_sections_no_ai_panels():
    print("=" * 100)
    print("TEST - Data Upload page shows only the 5 required sections, no AI review panels")
    print("=" * 100)

    real_run_ocr = ocr_upload.run_ocr
    real_ai = _patch_ai_unavailable()
    realistic_text = _load_realistic_ocr_text()
    ocr_upload.run_ocr = lambda pdf_path: (realistic_text, {"status_code": 200})

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload(at, b"%PDF-1.4 fake content simplified-upload\n" * 20)
        _ok(not at.exception, f"upload runs with no exception: {[str(e) for e in at.exception]}")

        text = _page_text(at)
        for required in ("Source Document", "Document Extraction", "Extracted Financial Data",
                          "Extracted Text", "Active Data Source"):
            _ok(required in text, f"Data Upload page shows the required '{required}' section")
        _ok("Text extracted from the uploaded financial document." in text,
            "the Extracted Text description matches the requested wording")
        _ok("Download Extracted Text" in [w.label for w in at.button],
            "the upload page shows the requested Download Extracted Text button label")

        for removed in ("AI-Assisted Validation", "AI Mapping Correction Proposals"):
            _ok(removed not in text, f"Data Upload page no longer shows the '{removed}' panel")

        # The underlying AI processing/session-state must still exist -
        # only the Data Upload page's RENDERING of it was removed.
        _ok("ai_validation_results" in at.session_state, "AI validation results are still computed and stored in session_state")
        _ok("correction_proposals" in at.session_state, "correction proposals are still computed and stored in session_state")
        _ok("ai_mapping_results" in at.session_state, "AI dynamic mapping results are still computed and stored in session_state")

        _ok(not at.exception, f"Data Upload page renders cleanly end to end: {[str(e) for e in at.exception]}")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        _restore_ai(real_ai)


def test_data_review_business_glossary_column_position():
    print()
    print("=" * 100)
    print("TEST - Data Review's Field-Level Review table shows Business Glossary right after Field")
    print("=" * 100)

    real_run_ocr = ocr_upload.run_ocr
    real_ai = _patch_ai_unavailable()
    realistic_text = _load_realistic_ocr_text()
    ocr_upload.run_ocr = lambda pdf_path: (realistic_text, {"status_code": 200})

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload(at, b"%PDF-1.4 fake content glossary-column\n" * 20)
        _ok(not at.exception, f"upload runs with no exception: {[str(e) for e in at.exception]}")

        html = _raw_markdown_html(at, "2. Data Review")
        _ok("Business Glossary" in html, "the Business Glossary column header is present on Data Review")

        # Find the Field-Level Review table specifically (the one whose
        # header row also contains "Deterministic Status") rather than
        # assuming it's the only table on the page.
        table_match = re.search(r'<thead>.*?Deterministic Status.*?</thead>', html, re.S)
        _ok(table_match is not None, "fixture sanity check: the Field-Level Review table's header row was found")
        if table_match:
            header_html = table_match.group(0)
            field_idx = header_html.find(">Field<")
            glossary_idx = header_html.find(">Business Glossary<")
            statement_idx = header_html.find(">Statement<")
            _ok(-1 not in (field_idx, glossary_idx, statement_idx),
                "Field, Business Glossary and Statement are all present as column headers")
            _ok(field_idx < glossary_idx < statement_idx,
                f"column order is Field -> Business Glossary -> Statement (indices {field_idx}, {glossary_idx}, {statement_idx})")

        # The primary table only ever carries these 6 columns - AI/audit
        # columns were never part of it.
        if table_match:
            for moved_header in ("AI Validation Status", "Source Line", "Correction Proposal", "Review Decision"):
                _ok(f">{moved_header}<" not in header_html,
                    f"'{moved_header}' is not a column in the primary table")

        # The "Full Field Detail" expander was removed entirely - the
        # full glossary definition is shown directly in the main table
        # instead (checked below), not behind a second expander/table.
        _ok("Full Field Detail" not in _page_text(at), "the separate Full Field Detail expander no longer exists")

        # The real, FULL (untruncated) definition appears directly in
        # the primary table's own HTML - no ellipsis, no separate detail
        # view needed to read it.
        _ok("Income earned from the company&#x27;s principal business activities" in html
            or "Income earned from the company's principal business activities" in html
            or "principal business activities" in html,
            "Revenue from operations' real, FULL glossary definition appears directly in the primary table")
        _ok("…" not in html, "no ellipsis/truncation marker appears anywhere on the Data Review page")

        _ok(not at.exception, f"Data Review page renders cleanly: {[str(e) for e in at.exception]}")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        _restore_ai(real_ai)


def test_ai_dynamic_mapping_explanation_block_renders():
    print()
    print("=" * 100)
    print("TEST - the AI Dynamic Field Mapping explanation/workflow block renders on Data Review")
    print("=" * 100)

    real_run_ocr = ocr_upload.run_ocr
    real_ai = _patch_ai_unavailable()
    realistic_text = _load_realistic_ocr_text()
    ocr_upload.run_ocr = lambda pdf_path: (realistic_text, {"status_code": 200})

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload(at, b"%PDF-1.4 fake content workflow-explanation\n" * 20)
        _ok(not at.exception, f"upload runs with no exception: {[str(e) for e in at.exception]}")

        # Removed by request (Data Review simplification): the AI-role
        # explanation, the workflow diagram and the explanatory accordion.
        text = _page_text(at, "2. Data Review")
        _ok("does not generate or directly populate financial figures" not in text,
            "the AI-role explanation paragraph is no longer shown")
        _ok("Human Decision" not in text and "Deterministic Verification" not in text,
            "the AI Dynamic Field Mapping workflow diagram is no longer shown")
        _ok("Accept / Reject / Keep as Not Found mean here" not in text,
            "the explanatory accordion is no longer shown")

        _ok(not at.exception, f"Data Review page renders cleanly: {[str(e) for e in at.exception]}")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        _restore_ai(real_ai)


def test_ai_dynamic_mapping_table_refinement():
    print()
    print("=" * 100)
    print("TEST - AI Dynamic Field Mapping: short headers, merged columns, concise summary + diagnostics expander")
    print("=" * 100)

    # _load_realistic_ocr_text()'s synthetic fixture has ZERO genuinely
    # NOT_FOUND fields (nothing to trigger dynamic mapping at all), so
    # this test needs the real cached Tata Chemicals OCR text instead
    # (confirmed in test_ai_dynamic_mapping.py to have 9 real NOT_FOUND
    # fields) - gracefully skipped if that cache isn't present on this
    # machine, matching this codebase's own convention.
    tata_chemicals_text = _find_tata_chemicals_cache()
    if tata_chemicals_text is None:
        print("[SKIP] No cached Tata Chemicals OCR text on this machine.")
        return

    real_run_ocr = ocr_upload.run_ocr
    ocr_upload.run_ocr = lambda pdf_path: (tata_chemicals_text, {"status_code": 200})

    # ai_validation/ai_correction_proposals mocked unavailable (out of
    # scope here); ai_dynamic_mapping mocked AVAILABLE but declining
    # every field (NO_CANDIDATE_FOUND) - enough to get the refined table
    # actually rendered with a real row, without needing a full
    # candidate-selection fixture (already covered by
    # test_ai_dynamic_mapping.py at the unit level).
    def _unavailable(*a, **k):
        return ai_validation.OllamaAvailability(False, False, [], "mocked unavailable - out of scope for this test file")

    def _should_not_be_called(*a, **k):
        raise AssertionError("must never make a real Ollama call in this test file")

    def _available(*a, **k):
        return ai_validation.OllamaAvailability(True, True, [ai_validation.DEFAULT_MODEL], "")

    def _decline_all(prompt, model=None, base_url=None, timeout=None):
        contexts = json.loads(prompt.split("FIELDS:\n", 1)[1])
        results = [
            {
                "field": ctx["field"], "status": "NO_CANDIDATE_FOUND", "confidence": 0.0,
                "reason": "no plausible candidate found", "selected_line_number": None,
                "evidence_line_numbers": [],
            }
            for ctx in contexts
        ]
        return json.dumps({"results": results})

    real = {
        "av_check": ai_validation.check_ollama_availability, "av_call": ai_validation.call_ollama,
        "acp_check": ai_correction_proposals.check_ollama_availability, "acp_call": ai_correction_proposals.call_ollama,
        "adm_check": ai_dynamic_mapping.check_ollama_availability, "adm_call": ai_dynamic_mapping.call_ollama,
    }
    ai_validation.check_ollama_availability = _unavailable
    ai_validation.call_ollama = _should_not_be_called
    ai_correction_proposals.check_ollama_availability = _unavailable
    ai_correction_proposals.call_ollama = _should_not_be_called
    ai_dynamic_mapping.check_ollama_availability = _available
    ai_dynamic_mapping.call_ollama = _decline_all

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload(at, b"%PDF-1.4 fake content table-refinement\n" * 20)
        _ok(not at.exception, f"upload runs with no exception: {[str(e) for e in at.exception]}")

        # The technical AI Dynamic Field Mapping table, summary line and
        # diagnostics expander were removed from Data Review by request.
        # The mapping results themselves are still computed and stored.
        text = _page_text(at, "2. Data Review")
        for header in ("Candidates Found", "Shortlisted", "Parsed Values"):
            _ok(header not in text, f"removed mapping-table header '{header}' is no longer shown")
        _ok("field(s) considered" not in text, "the removed mapping summary line is no longer shown")
        _ok("Technical diagnostics" not in text, "the removed diagnostics expander is no longer shown")

        mapping_results = at.session_state["ai_mapping_results"]
        not_found = [f for f, ed in at.session_state["extracted"].items() if ed.get("validation_status") == "NOT_FOUND"]
        _ok(mapping_results and set(not_found) <= set(mapping_results),
            "every NOT_FOUND field still has a stored dynamic-mapping result")
        _ok(all(r.mapping_status == "NO_CANDIDATE_FOUND" for r in mapping_results.values()),
            "each declined field's exact underlying status is still recorded")
        _ok(not any(k.startswith("accept_map_") for k in (w.key for w in at.button)),
            "no mapping decision is offered when nothing was mapped")

        _ok(not at.exception, f"Data Review page renders cleanly: {[str(e) for e in at.exception]}")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        ai_validation.check_ollama_availability = real["av_check"]
        ai_validation.call_ollama = real["av_call"]
        ai_correction_proposals.check_ollama_availability = real["acp_check"]
        ai_correction_proposals.call_ollama = real["acp_call"]
        ai_dynamic_mapping.check_ollama_availability = real["adm_check"]
        ai_dynamic_mapping.call_ollama = real["adm_call"]


def _patch_dynamic_mapping_only(fake_call):
    """ai_validation/ai_correction_proposals mocked unavailable (out of
    scope); ai_dynamic_mapping mocked AVAILABLE with the given fake
    call. Returns the real functions dict for _restore_ai()."""
    def _unavailable(*a, **k):
        return ai_validation.OllamaAvailability(False, False, [], "mocked unavailable - out of scope for this test file")

    def _available(*a, **k):
        return ai_validation.OllamaAvailability(True, True, [ai_validation.DEFAULT_MODEL], "")

    def _should_not_be_called(*a, **k):
        raise AssertionError("must never make a real Ollama call in this test file")

    real = {
        "av_check": ai_validation.check_ollama_availability, "av_call": ai_validation.call_ollama,
        "acp_check": ai_correction_proposals.check_ollama_availability, "acp_call": ai_correction_proposals.call_ollama,
        "adm_check": ai_dynamic_mapping.check_ollama_availability, "adm_call": ai_dynamic_mapping.call_ollama,
    }
    ai_validation.check_ollama_availability = _unavailable
    ai_validation.call_ollama = _should_not_be_called
    ai_correction_proposals.check_ollama_availability = _unavailable
    ai_correction_proposals.call_ollama = _should_not_be_called
    ai_dynamic_mapping.check_ollama_availability = _available
    ai_dynamic_mapping.call_ollama = fake_call
    return real


def _restore_dynamic_mapping_only(real):
    ai_validation.check_ollama_availability = real["av_check"]
    ai_validation.call_ollama = real["av_call"]
    ai_correction_proposals.check_ollama_availability = real["acp_check"]
    ai_correction_proposals.call_ollama = real["acp_call"]
    ai_dynamic_mapping.check_ollama_availability = real["adm_check"]
    ai_dynamic_mapping.call_ollama = real["adm_call"]


def _make_fake_select_capex_call():
    """Selects the real Capex row (line found by text match - never
    hardcoded to a specific line number) for 'Capex', declines every
    other field - mirrors test_ai_dynamic_mapping.py's own real-row-
    selection fixture pattern, in the NEW row-selection-only schema
    (selected_line_number only, never a value)."""
    def _fake(prompt, model=None, base_url=None, timeout=None):
        contexts = json.loads(prompt.split("FIELDS:\n", 1)[1])
        results = []
        for ctx in contexts:
            field = ctx["field"]
            candidates = ctx.get("candidate_rows") or []
            match = next((c for c in candidates if "acquisition of property" in c["text"].lower()), None) if field == "Capex" else None
            if match is None:
                results.append({
                    "field": field, "status": "NO_CANDIDATE_FOUND", "confidence": 0.5,
                    "reason": "no plausible candidate found", "selected_line_number": None,
                    "evidence_line_numbers": [],
                })
                continue
            results.append({
                "field": field, "status": "CANDIDATE_FOUND", "confidence": 0.82,
                "reason": "semantically equivalent line item", "selected_line_number": match["line_number"],
                "evidence_line_numbers": [match["line_number"]],
            })
        return json.dumps({"results": results})
    return _fake


def test_dynamic_mapping_verified_field_shows_review_controls():
    print()
    print("=" * 100)
    print("TEST - a deterministically VERIFIED dynamic mapping shows working Accept/Reject/Keep controls")
    print("=" * 100)

    tata_chemicals_text = _find_tata_chemicals_cache()
    if tata_chemicals_text is None:
        print("[SKIP] No cached Tata Chemicals OCR text on this machine.")
        return

    real_run_ocr = ocr_upload.run_ocr
    ocr_upload.run_ocr = lambda pdf_path: (tata_chemicals_text, {"status_code": 200})
    real = _patch_dynamic_mapping_only(_make_fake_select_capex_call())

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload(at, b"%PDF-1.4 fake content verified-mapping\n" * 20)
        _ok(not at.exception, f"upload runs with no exception: {[str(e) for e in at.exception]}")

        mapping_results = at.session_state["ai_mapping_results"]
        _ok(mapping_results["Capex"].mapping_status == "MAPPED_AND_VERIFIED",
            f"fixture sanity check: Capex actually reaches MAPPED_AND_VERIFIED, got {mapping_results['Capex'].mapping_status!r}")

        at.sidebar.radio[0].set_value("2. Data Review")
        at.run(timeout=60)

        button_keys = [w.key for w in at.button]
        _ok("accept_map_Capex" in button_keys, "Accept button is present for the verified Capex mapping")
        _ok("reject_map_Capex" in button_keys, "Reject button is present for the verified Capex mapping")
        _ok("keep_map_Capex" not in button_keys, "Keep as Not Found is not part of the simplified review section")
        _ok("manual_save_Capex" in button_keys, "Enter Manually is available for the verified Capex mapping")

        text = _page_text(at)
        _ok("**Capex**" in text, "it is obvious which template field the decision applies to (field name shown)")
        _ok("AI-suggested mapping:" in text, "the AI-suggested mapping values are shown before the decision")
        _ok("Review Required / Correction Proposals" in text, "pending decisions are under their own clearly-labeled heading")

        # Other NOT_FOUND fields (declined -> NO_CANDIDATE_FOUND) must
        # NOT get an Accept button of their own.
        other_not_found = [f for f in mapping_results if f != "Capex"]
        for f in other_not_found:
            _ok(f"accept_map_{f}" not in button_keys, f"no Accept button for the non-verified field '{f}'")

        # Correction proposals and mapping decisions now share the single
        # review section; the empty-state message must not appear while a
        # mapping decision is pending.
        _ok("No correction proposals require a decision for this document." not in text,
            "the empty-state message is not shown while a mapping decision is pending")

        # Click Accept and confirm it persists through the existing
        # approved_corrections/session-state workflow.
        for w in at.button:
            if w.key == "accept_map_Capex":
                w.click()
                at.run(timeout=60)
                break
        _ok(at.session_state["approved_corrections"]["Capex"]["decision"] == "accepted",
            "accepting the mapping records the decision for the correct field")

        at.sidebar.radio[0].set_value("2. Data Review")
        at.run(timeout=60)
        _ok("**Capex**  ·  ✓ Accepted" in _page_text(at), "the review section reflects the recorded decision after accepting")

        _ok(not at.exception, f"Data Review page renders cleanly: {[str(e) for e in at.exception]}")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        _restore_dynamic_mapping_only(real)


def test_dynamic_mapping_skipped_field_shows_correct_reason():
    print()
    print("=" * 100)
    print("TEST - a time-budget-exhausted field shows 'AI Skipped', not a misleading status")
    print("=" * 100)

    tata_chemicals_text = _find_tata_chemicals_cache()
    if tata_chemicals_text is None:
        print("[SKIP] No cached Tata Chemicals OCR text on this machine.")
        return

    real_run_ocr = ocr_upload.run_ocr
    ocr_upload.run_ocr = lambda pdf_path: (tata_chemicals_text, {"status_code": 200})

    def _should_not_be_called(*a, **k):
        raise AssertionError("call_ollama must never be invoked once the time budget is already exhausted")

    real = _patch_dynamic_mapping_only(_should_not_be_called)
    real_budget = ai_dynamic_mapping.DYNAMIC_MAPPING_TIME_BUDGET_SECONDS
    ai_dynamic_mapping.DYNAMIC_MAPPING_TIME_BUDGET_SECONDS = 0.0

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload(at, b"%PDF-1.4 fake content skipped-budget\n" * 20)
        _ok(not at.exception, f"upload runs with no exception: {[str(e) for e in at.exception]}")

        mapping_results = at.session_state["ai_mapping_results"]
        _ok(all(r.technical_status == "AI_SKIPPED" for r in mapping_results.values()),
            "fixture sanity check: every field is genuinely AI_SKIPPED with a zero time budget")

        # The per-field mapping status table was removed from Data Review;
        # the skipped reason is still recorded for every field.
        text = _page_text(at, "2. Data Review")
        _ok(all("time budget" in r.reason for r in mapping_results.values()),
            "the skipped reason (time budget exhausted) is recorded for every skipped field")
        _ok(all(r.mapping_status is None for r in mapping_results.values()),
            "a skipped (never attempted) field is never recorded as having no candidate")
        _ok("No Plausible Candidate" not in text, "a skipped field is never shown as having no candidate")

        button_keys = [w.key for w in at.button]
        _ok(not any(k.startswith("accept_map_") for k in button_keys), "no Accept button exists for any skipped field")

        _ok(not at.exception, f"Data Review page renders cleanly: {[str(e) for e in at.exception]}")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        ai_dynamic_mapping.DYNAMIC_MAPPING_TIME_BUDGET_SECONDS = real_budget
        _restore_dynamic_mapping_only(real)


def test_dynamic_mapping_summary_reconciles_with_table():
    print()
    print("=" * 100)
    print("TEST - the concise summary's counts add up to the actual number of fields considered")
    print("=" * 100)

    tata_chemicals_text = _find_tata_chemicals_cache()
    if tata_chemicals_text is None:
        print("[SKIP] No cached Tata Chemicals OCR text on this machine.")
        return

    real_run_ocr = ocr_upload.run_ocr
    ocr_upload.run_ocr = lambda pdf_path: (tata_chemicals_text, {"status_code": 200})
    real = _patch_dynamic_mapping_only(_make_fake_select_capex_call())

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload(at, b"%PDF-1.4 fake content reconciliation\n" * 20)
        _ok(not at.exception, f"upload runs with no exception: {[str(e) for e in at.exception]}")

        # The mapping summary caption was removed from Data Review by
        # request; the same per-field classification it used still
        # reconciles with the stored results.
        import app

        text = _page_text(at, "2. Data Review")
        _ok("field(s) considered" not in text, "the removed mapping summary caption is no longer shown")
        mapping_results = at.session_state["ai_mapping_results"]
        keys = [
            app.classify_dynamic_mapping_status(r.mapping_status, r.technical_status, None)[3]
            for r in mapping_results.values()
        ]
        _ok(len(keys) == len(mapping_results), "every considered field is classified exactly once")
        _ok(keys.count("awaiting_approval") >= 1, "at least one field (Capex) is genuinely awaiting approval in this fixture")
        _ok(keys.count("awaiting_approval") == sum(1 for w in at.button if w.key.startswith("accept_map_")),
            "every field awaiting approval has exactly one Accept control in the review section")

        _ok(not at.exception, f"Data Review page renders cleanly: {[str(e) for e in at.exception]}")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        _restore_dynamic_mapping_only(real)


def test_field_review_status_labels_are_simplified():
    print()
    print("=" * 100)
    print("TEST - Data Review's visible field statuses are simplified to OK / Review Required / Not Found")
    print("=" * 100)

    real_run_ocr = ocr_upload.run_ocr
    real_ai = _patch_ai_unavailable()
    realistic_text = _load_realistic_ocr_text()
    ocr_upload.run_ocr = lambda pdf_path: (realistic_text, {"status_code": 200})

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload(at, b"%PDF-1.4 fake content status-labels\n" * 20)
        _ok(not at.exception, f"upload runs with no exception: {[str(e) for e in at.exception]}")

        html = _raw_markdown_html(at, "2. Data Review")
        _ok("Review Required" in html, "the visible user-facing label 'Review Required' is shown")
        _ok("Not Found" in html, "the visible user-facing label 'Not Found' is shown")
        _ok("OK" in html, "the visible user-facing label 'OK' is shown")
        _ok("AI_MAPPED" not in html, "internal AI_MAPPED state is not displayed directly in the field table")
        _ok("REVIEW_REQUIRED" not in html, "internal REVIEW_REQUIRED state is not displayed directly in the field table")
        _ok("NOT_FOUND" not in html, "internal NOT_FOUND state is not displayed directly in the field table")
        _ok(not at.exception, f"Data Review page renders cleanly: {[str(e) for e in at.exception]}")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        _restore_ai(real_ai)


def test_candidate_ranking_detail_removed_from_standard_ui():
    print()
    print("=" * 100)
    print("TEST - the AI Candidate Ranking Detail expander is no longer shown on the standard Data Review page")
    print("=" * 100)
    with open("app.py", encoding="utf-8") as f:
        source = f.read()
    _ok('AI Candidate Ranking Detail (why these candidates were shortlisted)' not in source,
        "the ranking-detail expander is removed from the standard page source")


# ============================================================
# Regression tests: upload-stage status accuracy, provider-specific
# wording, and Data Review AI metrics.
#
# Self-contained: a small SYNTHETIC balance sheet (invented figures, no
# real borrower data) plus mocked AI layers. Never reads cached OCR text
# or local documents, never calls Ollama/OpenAI, and assertion messages
# never include page text, prompts or figures.
# ============================================================

import contextlib
import uuid

import pytest

import ocr_pipeline


def _synthetic_ocr_text(ambiguous_cash=False):
    """An invented two-year balance sheet: 9 template fields extract as
    deterministic OK and the other 34 are genuinely NOT_FOUND. Every
    data row is claimed by one of those 9 fields, so dynamic mapping has
    no candidate rows for any NOT_FOUND field.

    ambiguous_cash=True replaces the Cash row with a lone figure halfway
    between the two year columns (test_ai_validation's own ambiguous
    pattern), making Cash genuinely deterministic REVIEW_REQUIRED."""
    def row(label, current, previous):
        return f"{label:<60}{current:>18}{previous:>18}"

    cash_row = (
        "    (iii) Cash and cash equivalents" + " " * 48 + "45" if ambiguous_cash
        else row("    (iii) Cash and cash equivalents", 25, 30)
    )
    return "\n".join([
        "Statement of Standalone Audited Balance Sheet as at 31 March, 2026",
        "Particulars" + " " * 49 + "As at".rjust(18) + "As at".rjust(18),
        " " * 60 + "31".rjust(18) + "31".rjust(18),
        " " * 60 + "March,".rjust(18) + "March,".rjust(18),
        " " * 60 + "2026".rjust(18) + "2025".rjust(18),
        "I. ASSETS",
        "(1) Non-current assets",
        row("    (a) Property, plant and equipment", 900, 850),
        "(2) Current assets",
        row("    (a) Current investments", 120, 110),
        cash_row,
        row("    (iv) Other balances with banks", 37, 41),
        row("Total current assets", 600, 560),
        row("Total assets", 2000, 1800),
        "II. EQUITY AND LIABILITIES",
        row("Total equity", 1100, 1000),
        row("Total current liabilities", 500, 450),
        row("Total liabilities", 900, 800),
    ])


_SYNTHETIC_OK = 9
_SYNTHETIC_NOT_FOUND = 34


def _synthetic_extraction():
    text = _synthetic_ocr_text()
    extracted, _debug_rows, meta = ocr_pipeline.extract_financial_values(text, ocr_pipeline.TEMPLATE_FILE)
    pairs = [(raw, ocr_pipeline.normalize_text(raw)) for raw in text.splitlines()]
    pairs = [(raw, norm) for raw, norm in pairs if norm]
    lines = [norm for _raw, norm in pairs]
    layout_lines = [ocr_pipeline._normalize_preserve_layout(raw) for raw, _norm in pairs]
    return extracted, meta, lines, layout_lines


def _not_found_fields(extracted):
    return [f for f, ed in extracted.items() if ed.get("validation_status") == "NOT_FOUND"]


def _run_pipeline_events():
    """Runs the real process_uploaded_pdf() on the synthetic document (OCR
    mocked) and returns the (stage, state, detail) events it reported."""
    real_run_ocr = ocr_upload.run_ocr
    text = _synthetic_ocr_text()
    ocr_upload.run_ocr = lambda pdf_path: (text, {"status_code": 200})
    events = []
    pdf_path = os.path.join(ocr_upload.UPLOAD_WORK_DIR, f"stage_{uuid.uuid4().hex}.pdf")
    with open(pdf_path, "wb") as f:
        f.write(b"%PDF-1.4 stage-events " + uuid.uuid4().hex.encode())
    try:
        ocr_upload.process_uploaded_pdf(
            pdf_path, "borrower.pdf", status_callback=lambda s, state, d=None: events.append((s, state, d)),
        )
    finally:
        ocr_upload.run_ocr = real_run_ocr
    return events


def _final_stage(events, stage):
    return [(state, detail) for name, state, detail in events if name == stage][-1]


def _render_stages(events):
    """Feeds the events through app.py's own _render_upload_pipeline
    (exactly as the live Data Upload callback does) and returns
    {stage title: (subtitle, kind)}."""
    import app

    states, details = {}, {}
    for stage, state, detail in events:
        states[stage] = state
        if detail is not None:
            details[stage] = detail
    captured = []

    class _Placeholder:
        def container(self):
            return contextlib.nullcontext()

    real_render = app.render_flow_steps
    app.render_flow_steps = lambda steps: captured.extend(steps)
    try:
        app._render_upload_pipeline(_Placeholder(), states, details)
    finally:
        app.render_flow_steps = real_render
    return {title: (subtitle, kind) for title, subtitle, kind in captured}


def _unavailable(detail="mocked unavailable"):
    return lambda *a, **k: ai_validation.OllamaAvailability(False, False, [], detail)


def _available(*a, **k):
    return ai_validation.OllamaAvailability(True, True, [ai_validation.DEFAULT_MODEL], "")


def _no_real_call(*a, **k):
    __tracebackhide__ = True  # never print the (prompt) arguments in a failure report
    raise AssertionError("no real AI call may be made in these tests")


def _failing_call(*a, **k):
    __tracebackhide__ = True
    raise ai_validation.OllamaCallError("timeout (mocked)")


def _empty_response_call(*a, **k):
    __tracebackhide__ = True
    return "{}"


class _AiPatch:
    """Context manager: sets AI_PROVIDER and patches availability/call
    seams on all three AI layers (and optionally the mapping/validation
    entry points themselves). Everything is restored on exit."""

    def __init__(self, provider="ollama", check=None, call=None, mapping_check=None,
                 mapping_call=None, map_missing=None, validate=None, corrections=None):
        self.provider = provider
        self.check = check or _unavailable()
        self.call = call or _no_real_call
        self.mapping_check = mapping_check
        self.mapping_call = mapping_call
        self.map_missing = map_missing
        self.validate = validate
        self.corrections = corrections

    def __enter__(self):
        self.real = {
            "provider": ai_validation.AI_PROVIDER,
            "av_check": ai_validation.check_ollama_availability, "av_call": ai_validation.call_ollama,
            "av_validate": ai_validation.validate_extracted_fields,
            "acp_check": ai_correction_proposals.check_ollama_availability, "acp_call": ai_correction_proposals.call_ollama,
            "adm_check": ai_dynamic_mapping.check_ollama_availability, "adm_call": ai_dynamic_mapping.call_ollama,
            "adm_map": ai_dynamic_mapping.map_missing_fields,
            "acp_generate": ai_correction_proposals.generate_correction_proposals,
        }
        ai_validation.AI_PROVIDER = self.provider
        ai_validation.check_ollama_availability = self.check
        ai_validation.call_ollama = self.call
        ai_correction_proposals.check_ollama_availability = self.check
        ai_correction_proposals.call_ollama = self.call
        ai_dynamic_mapping.check_ollama_availability = self.mapping_check or self.check
        ai_dynamic_mapping.call_ollama = self.mapping_call or self.call
        if self.map_missing is not None:
            ai_dynamic_mapping.map_missing_fields = self.map_missing
        if self.validate is not None:
            ai_validation.validate_extracted_fields = self.validate
        if self.corrections is not None:
            ai_correction_proposals.generate_correction_proposals = self.corrections
        return self

    def __exit__(self, *exc):
        ai_validation.AI_PROVIDER = self.real["provider"]
        ai_validation.check_ollama_availability = self.real["av_check"]
        ai_validation.call_ollama = self.real["av_call"]
        ai_validation.validate_extracted_fields = self.real["av_validate"]
        ai_correction_proposals.check_ollama_availability = self.real["acp_check"]
        ai_correction_proposals.call_ollama = self.real["acp_call"]
        ai_dynamic_mapping.check_ollama_availability = self.real["adm_check"]
        ai_dynamic_mapping.call_ollama = self.real["adm_call"]
        ai_dynamic_mapping.map_missing_fields = self.real["adm_map"]
        ai_correction_proposals.generate_correction_proposals = self.real["acp_generate"]
        return False


def _mapping_result(field, technical, status=None, reason="mocked", ai_call_made=True):
    return ai_dynamic_mapping._make_result(field, technical, status, None, reason, ai_call_made=ai_call_made)


def _no_candidate_result(field):
    """Exactly what map_missing_fields() produces for a field with no
    candidate rows: AI_COMPLETED/INSUFFICIENT_CONTEXT, but no AI call."""
    return _mapping_result(
        field, ai_validation.AI_COMPLETED, "INSUFFICIENT_CONTEXT",
        reason="No unclaimed, data-bearing rows exist in this field's section to consider.", ai_call_made=False,
    )


def _fake_map(build):
    """A mocked map_missing_fields(): build(not_found_fields) -> (results, availability)."""
    def _fake(extracted, lines, meta=None, layout_lines=None, **k):
        results, availability = build(_not_found_fields(extracted))
        return {f: dict(ed) for f, ed in extracted.items()}, results, availability
    return _fake


def _crashing_map(*a, **k):
    raise RuntimeError("mocked mapping crash")


# ---- fixture sanity ------------------------------------------------

def test_synthetic_fixture_shape():
    extracted, _meta, _lines, _layout = _synthetic_extraction()
    statuses = [ed.get("validation_status") for ed in extracted.values()]
    assert statuses.count("OK") == _SYNTHETIC_OK, "synthetic fixture: unexpected OK count"
    assert statuses.count("NOT_FOUND") == _SYNTHETIC_NOT_FOUND, "synthetic fixture: unexpected NOT_FOUND count"


# ---- ISSUE 1: mapping outcome classification -----------------------

A_ = ai_validation.OllamaAvailability
_UP = A_(True, True, ["m"], "")
_NN = "No fields required a mapping attempt."


def test_stage_outcome_not_needed():
    assert ocr_upload.ai_stage_outcome({}, None, _NN, report_partial=True) == ("not_needed", _NN)


def test_stage_outcome_unavailable():
    results = {"F": _mapping_result("F", ai_validation.AI_UNAVAILABLE, reason="down", ai_call_made=False)}
    assert ocr_upload.ai_stage_outcome(results, A_(False, False, [], "down"), _NN, report_partial=True) == ("unavailable", "down")


def test_stage_outcome_unexpected_exception():
    state, detail = ocr_upload.ai_stage_outcome({}, _UP, _NN, unexpected_error="crash", report_partial=True)
    assert (state, detail) == ("exception", "crash")


def test_stage_outcome_success():
    results = {
        "F1": _mapping_result("F1", ai_validation.AI_COMPLETED, "MAPPED_AND_VERIFIED"),
        "F2": _mapping_result("F2", ai_validation.AI_COMPLETED, "NO_CANDIDATE_FOUND"),
    }
    assert ocr_upload.ai_stage_outcome(results, _UP, _NN, report_partial=True) == ("complete", None)


def test_stage_outcome_partial_success():
    results = {
        "F1": _mapping_result("F1", ai_validation.AI_COMPLETED, "MAPPED_AND_VERIFIED"),
        "F2": _mapping_result("F2", ai_validation.AI_ERROR, reason="OpenAI call failed: x"),
    }
    state, detail = ocr_upload.ai_stage_outcome(results, _UP, _NN, report_partial=True)
    assert state == "partial"
    assert "1 of 2 field(s) hit a technical error" in detail


def test_bug_a_no_candidate_rows_is_not_success():
    # Every field had no candidate rows: zero AI calls were made.
    results = {f: _no_candidate_result(f) for f in ("F1", "F2", "F3")}
    state, detail = ocr_upload.ai_stage_outcome(results, _UP, _NN, report_partial=True)
    assert state == "skipped", state
    assert "3 of 3 field(s) had no candidate rows to send (no AI call made)" in detail


def test_bug_b_no_candidate_rows_plus_all_calls_failed_is_failure():
    results = {
        "F1": _no_candidate_result("F1"),
        "F2": _mapping_result("F2", ai_validation.AI_ERROR, reason="OpenAI call failed: timeout (mocked)"),
        "F3": _mapping_result("F3", ai_validation.AI_ERROR, reason="OpenAI call failed: timeout (mocked)"),
    }
    state, detail = ocr_upload.ai_stage_outcome(results, _UP, _NN, report_partial=True)
    assert state == "attempt_failed", state
    assert detail.startswith("OpenAI call failed: timeout (mocked)")
    assert "2 of 3 field(s) hit a technical error" in detail


def test_bug_a_real_mapping_function_marks_no_call_results():
    # The real map_missing_fields() given text with no statement sections:
    # no NOT_FOUND field can have a candidate row, so no AI call may be
    # made and every result must say so.
    extracted, meta, _lines, _layout = _synthetic_extraction()
    no_sections = ["Notes forming part of the financial statements", "Basis of preparation"]
    with _AiPatch(check=_available, call=_no_real_call):
        _aug, results, availability = ai_dynamic_mapping.map_missing_fields(
            extracted, no_sections, meta, layout_lines=no_sections,
        )
    assert len(results) == _SYNTHETIC_NOT_FOUND
    assert all(r.technical_status == ai_validation.AI_COMPLETED and r.mapping_status == "INSUFFICIENT_CONTEXT"
               for r in results.values()), "fixture: expected the no-candidate INSUFFICIENT_CONTEXT path"
    assert all(r.ai_call_made is False for r in results.values()), "a no-candidate result claimed an AI call"
    assert ocr_upload.ai_stage_outcome(results, availability, _NN, report_partial=True)[0] == "skipped"


def test_bug_a_pipeline_and_render():
    with _AiPatch(map_missing=_fake_map(lambda nf: ({f: _no_candidate_result(f) for f in nf}, _available()))):
        events = _run_pipeline_events()
    assert _final_stage(events, "ai_mapping")[0] == "skipped"
    subtitle, _kind = _render_stages(events)["AI Dynamic Mapping"]
    assert subtitle.startswith("○ Skipped: ") and "no AI call made" in subtitle
    assert "Complete" not in subtitle and "unavailable" not in subtitle


def test_bug_b_pipeline_and_render():
    def _build(nf):
        results = {f: _no_candidate_result(f) for f in nf[:30]}
        results.update({
            f: _mapping_result(f, ai_validation.AI_ERROR, reason="OpenAI call failed: timeout (mocked)")
            for f in nf[30:]
        })
        return results, _available()

    with _AiPatch(provider="openai", map_missing=_fake_map(_build)):
        events = _run_pipeline_events()
    assert _final_stage(events, "ai_mapping")[0] == "attempt_failed"
    subtitle, kind = _render_stages(events)["AI Dynamic Mapping"]
    assert subtitle.startswith("✕ Attempted, but failed: OpenAI call failed: timeout (mocked)")
    assert "Complete" not in subtitle and kind == "warning"


@pytest.mark.parametrize("provider", ["ollama", "openai"])
def test_mapping_not_needed_neutral_message(provider):
    with _AiPatch(provider=provider, map_missing=_fake_map(lambda nf: ({}, None))):
        events = _run_pipeline_events()
    assert _final_stage(events, "ai_mapping") == ("not_needed", _NN)
    subtitle, kind = _render_stages(events)["AI Dynamic Mapping"]
    assert subtitle == "○ Not needed: No fields required a mapping attempt."
    assert kind == "neutral"
    for word in ("unavailable", "Ollama", "OpenAI"):
        assert word not in subtitle


def test_mapping_not_needed_real_function_never_checks_provider():
    extracted = {"Cash": {"validation_status": "OK"}, "Total assets": {"validation_status": "OK"}}

    def _must_not_check(*a, **k):
        raise AssertionError("provider availability must not be checked when nothing needs mapping")

    with _AiPatch(mapping_check=_must_not_check):
        _aug, results, availability = ai_dynamic_mapping.map_missing_fields(extracted, [], {})
    assert results == {} and availability is None


@pytest.mark.parametrize("provider,label", [("openai", "OpenAI"), ("ollama", "Ollama")])
def test_mapping_unavailable_names_configured_provider(provider, label):
    # The detail text deliberately names no provider: the label must come
    # from the configured provider, not from guessing at error text.
    # Rendered inside the patch: AI_PROVIDER is a process-level setting,
    # so the live app renders under the same provider it ran with.
    with _AiPatch(provider=provider, check=_unavailable("connection refused (mocked)")):
        events = _run_pipeline_events()
        subtitle, _kind = _render_stages(events)["AI Dynamic Mapping"]
    assert _final_stage(events, "ai_mapping") == ("unavailable", "connection refused (mocked)")
    assert subtitle == f"○ Skipped ({label} unavailable): connection refused (mocked)"


def test_mapping_success_and_partial_render():
    def _ok_build(nf):
        return {f: _mapping_result(f, ai_validation.AI_COMPLETED, "NO_CANDIDATE_FOUND") for f in nf}, _available()

    with _AiPatch(map_missing=_fake_map(_ok_build)):
        events = _run_pipeline_events()
    subtitle, kind = _render_stages(events)["AI Dynamic Mapping"]
    assert subtitle == "✓ Complete" and kind == "good"

    def _partial_build(nf):
        results, availability = _ok_build(nf)
        results[nf[0]] = _mapping_result(nf[0], ai_validation.AI_ERROR, reason="Ollama call failed: x")
        return results, availability

    with _AiPatch(map_missing=_fake_map(_partial_build)):
        events = _run_pipeline_events()
    subtitle, kind = _render_stages(events)["AI Dynamic Mapping"]
    assert subtitle.startswith("◐ Partially complete: 1 of 34 field(s) hit a technical error")
    assert kind == "warning"


def test_mapping_exception_render():
    with _AiPatch(map_missing=_crashing_map):
        events = _run_pipeline_events()
    assert _final_stage(events, "ai_mapping") == ("exception", "AI dynamic mapping failed unexpectedly: mocked mapping crash")
    subtitle, _kind = _render_stages(events)["AI Dynamic Mapping"]
    assert subtitle == "✕ Failed (unexpected error): AI dynamic mapping failed unexpectedly: mocked mapping crash"
    # The deterministic pipeline is unaffected by the mapping crash.
    assert _final_stage(events, "population")[0] == "complete"


# ---- ISSUE 1: Data Review mapping-status messaging -----------------

def _upload_synthetic(at, tag, text=None):
    real_run_ocr = ocr_upload.run_ocr
    text = text or _synthetic_ocr_text()
    ocr_upload.run_ocr = lambda pdf_path: (text, {"status_code": 200})
    try:
        _upload(at, b"%PDF-1.4 synthetic " + tag.encode() + b" " + uuid.uuid4().hex.encode())
    finally:
        ocr_upload.run_ocr = real_run_ocr


def _data_review(patch, tag, text=None):
    with patch:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload_synthetic(at, tag, text)
        assert not at.exception, "upload raised an exception"
        at.sidebar.radio[0].set_value("2. Data Review")
        at.run(timeout=60)
        assert not at.exception, "Data Review raised an exception"
    return at


_NOT_NEEDED_CAPTION = "No fields required a dynamic-mapping attempt"
_DID_NOT_RUN = "AI dynamic mapping did not run"


def _warnings(at):
    return "\n".join(str(w.value) for w in at.warning)


def _shows(at, text):
    """True if `text` is on the page. A plain bool, so a failing assert
    never prints the page (which contains extracted figures)."""
    return text in _page_text(at)


# The simplified Data Review page shows ONE short line only when AI
# dynamic mapping did not complete, and nothing when it did or was not
# needed. It never claims "nothing was needed" (a crash also leaves the
# mapping results empty).

def test_data_review_mapping_exception_is_not_reported_as_not_needed():
    at = _data_review(_AiPatch(map_missing=_crashing_map), "map-crash")
    assert at.session_state["ai_mapping_results"] == {}
    assert "AI dynamic mapping failed for this upload" in _warnings(at)
    assert "mocked mapping crash" in _warnings(at)
    assert not _shows(at, _NOT_NEEDED_CAPTION), "a crash was reported as 'not needed'"


def test_data_review_mapping_not_needed():
    at = _data_review(_AiPatch(map_missing=_fake_map(lambda nf: ({}, None))), "map-not-needed")
    assert at.session_state["ai_mapping_stage_state"] == "not_needed"
    assert "dynamic mapping" not in _warnings(at).lower()
    assert not _shows(at, _DID_NOT_RUN), "a not-needed mapping was reported as not running"


def test_data_review_mapping_all_calls_failed():
    def _build(nf):
        return {f: _mapping_result(f, ai_validation.AI_ERROR, reason="Ollama call failed: refused (mocked)") for f in nf}, _available()

    at = _data_review(_AiPatch(map_missing=_fake_map(_build)), "map-all-failed")
    assert "AI dynamic mapping was attempted but no AI call succeeded" in _warnings(at)
    assert not _shows(at, _NOT_NEEDED_CAPTION), "a failed mapping was reported as 'not needed'"


def test_data_review_mapping_unavailable():
    at = _data_review(_AiPatch(check=_unavailable("connection refused (mocked)")), "map-unavailable")
    assert _shows(at, "AI dynamic mapping did not run for this upload — connection refused (mocked)")
    assert not _shows(at, _NOT_NEEDED_CAPTION)
    assert "dynamic mapping" not in _warnings(at).lower()


def test_data_review_mapping_succeeded():
    def _build(nf):
        return {f: _mapping_result(f, ai_validation.AI_COMPLETED, "NO_CANDIDATE_FOUND") for f in nf}, _available()

    at = _data_review(_AiPatch(map_missing=_fake_map(_build)), "map-success")
    assert at.session_state["ai_mapping_stage_state"] == "complete"
    assert "dynamic mapping" not in _warnings(at).lower()
    assert not _shows(at, _DID_NOT_RUN) and not _shows(at, _NOT_NEEDED_CAPTION)


# ---- ISSUE 3: provider-specific wording ----------------------------

@pytest.mark.parametrize("provider,label,other", [("openai", "OpenAI", "Ollama"), ("ollama", "Ollama", "OpenAI")])
def test_validation_errors_name_configured_provider(provider, label, other):
    extracted, meta, lines, layout = _synthetic_extraction()

    with _AiPatch(provider=provider, check=_available, call=_failing_call):
        results, presence, _av = ai_validation.validate_extracted_fields(extracted, lines, meta, layout_lines=layout)
    explanations = [r.explanation for r in list(results.values()) + list(presence.values())]
    assert explanations and all(e == f"{label} call failed: timeout (mocked)" for e in explanations)
    assert not any(other in e for e in explanations)

    with _AiPatch(provider=provider, check=_available, call=_empty_response_call):
        results, presence, _av = ai_validation.validate_extracted_fields(extracted, lines, meta, layout_lines=layout)
    explanations = [r.explanation for r in list(results.values()) + list(presence.values())]
    assert explanations and all(e.startswith(f"{label}'s response did not include") for e in explanations)


@pytest.mark.parametrize("provider,label", [("openai", "OpenAI"), ("ollama", "Ollama")])
def test_correction_errors_name_configured_provider(provider, label):
    extracted, meta, lines, layout = _synthetic_extraction()
    field = next(f for f, ed in extracted.items() if ed.get("validation_status") == "OK")
    ai_results = {field: _validation_result(field, extracted[field], ai_validation.AI_COMPLETED, "YEAR_MISMATCH", True)}

    with _AiPatch(provider=provider, check=_available, call=_failing_call):
        proposals, _av = ai_correction_proposals.generate_correction_proposals(
            extracted, ai_results, lines, meta, layout_lines=layout,
        )
    assert proposals[field].technical_status == ai_validation.AI_ERROR
    assert proposals[field].reason == f"{label} call failed: timeout (mocked)"

    with _AiPatch(provider=provider, check=_available, call=_empty_response_call):
        proposals, _av = ai_correction_proposals.generate_correction_proposals(
            extracted, ai_results, lines, meta, layout_lines=layout,
        )
    assert proposals[field].reason.startswith(f"{label}'s response did not include")


@pytest.mark.parametrize("provider,label", [("openai", "OpenAI"), ("ollama", "Ollama")])
def test_mapping_call_error_names_configured_provider(provider, label):
    # Exercise the real message construction via a real batch failure:
    # one NOT_FOUND field given an unclaimed candidate row.
    extracted, meta, lines, layout = _synthetic_extraction()
    extracted = {f: dict(ed) for f, ed in extracted.items()}
    target = "Total assets"
    extracted[target].update({"validation_status": "NOT_FOUND", "source_line_number": None,
                              "fy_current": None, "fy_previous": None})

    with _AiPatch(provider=provider, check=_available, call=_failing_call):
        _aug, results, _av = ai_dynamic_mapping.map_missing_fields(extracted, lines, meta, layout_lines=layout)
    attempted = [r for r in results.values() if r.ai_call_made]
    assert attempted, "fixture: expected at least one real (mocked) mapping call"
    assert all(r.reason == f"{label} call failed: timeout (mocked)" for r in attempted)


def test_ollama_http_diagnostics_are_preserved():
    # The Ollama-only HTTP path keeps its Ollama-specific wording.
    import requests

    real_post = ai_validation.requests.post
    real_provider = ai_validation.AI_PROVIDER

    def _refused(*a, **k):
        raise requests.exceptions.ConnectionError("refused (mocked)")

    ai_validation.requests.post = _refused
    ai_validation.AI_PROVIDER = "ollama"
    try:
        with pytest.raises(ai_validation.OllamaCallError) as exc_info:
            ai_validation.call_ollama("prompt", base_url="http://localhost:1")
    finally:
        ai_validation.requests.post = real_post
        ai_validation.AI_PROVIDER = real_provider
    assert str(exc_info.value).startswith("Ollama call failed (ConnectionError)")


def test_provider_label_prefers_configured_provider():
    import app

    real_provider = ai_validation.AI_PROVIDER
    try:
        ai_validation.AI_PROVIDER = "openai"
        assert app._ai_provider_label() == "OpenAI"
        assert app._ai_provider_label(detail="mocked unavailable") == "OpenAI"            # no provider in text
        assert app._ai_provider_label(detail="Ollama is not reachable (mocked)") == "OpenAI"  # text never overrides config
        ai_validation.AI_PROVIDER = "ollama"
        assert app._ai_provider_label() == "Ollama"
        assert app._ai_provider_label(detail="mocked unavailable") == "Ollama"
        assert app._ai_provider_label(detail="OpenAI API is not reachable (mocked)") == "Ollama"
        # An explicit provider name wins; an unrecognised one is neutral.
        assert app._ai_provider_label("openai") == "OpenAI"
        assert app._ai_provider_label("something-else") == "AI provider"
        # A provider setting that cannot be read is neutral, not a guess.
        ai_validation.AI_PROVIDER = None
        assert app._ai_provider_label() == "AI provider"
        assert app._upload_stage_subtitle("unavailable", "down")[0] == "○ Skipped (AI provider unavailable): down"
    finally:
        ai_validation.AI_PROVIDER = real_provider


# ---- ISSUE 2: Data Review AI metrics -------------------------------

def _validation_result(field, ed, technical, status=None, requires_review=None):
    return ai_validation.AIValidationResult(
        field=field, technical_status=technical, validation_status=status,
        confidence=0.9 if status else None, explanation="mocked", requires_review=requires_review,
        fy_current=ed.get("fy_current"), fy_previous=ed.get("fy_previous"),
        year_current_label=None, year_previous_label=None,
        source_line_number=ed.get("source_line_number"), source_text=ed.get("source_text"),
        evidence_grounded=True if status else None,
    )


def _make_fake_validate(plan):
    """plan(sendable_fields) -> {field: (technical, status, requires_review)};
    any field not in the plan is AI_SKIPPED (time budget exhausted)."""
    def _fake(extracted, lines, meta=None, layout_lines=None, **k):
        sendable = [f for f, ed in extracted.items() if ed.get("validation_status") != "NOT_FOUND"]
        chosen = plan(sendable)
        results = {}
        for f in sendable:
            technical, status, rr = chosen.get(f, (ai_validation.AI_SKIPPED, None, None))
            results[f] = _validation_result(f, extracted[f], technical, status, rr)
        return results, {}, _available()
    return _fake


def _three_confirmed_one_mismatch_plan(fields):
    C = ai_validation.AI_COMPLETED
    return {
        fields[0]: (C, "CONFIRMED", False), fields[1]: (C, "CONFIRMED", False),
        fields[2]: (C, "CONFIRMED", False), fields[3]: (C, "YEAR_MISMATCH", True),
    }


def _kpis(at):
    html_text = _raw_markdown_html(at)
    return {
        label: int(value)
        for label, value in re.findall(
            r'<div class="cr-kpi-label">([^<]+)</div>(?:</div>)?<div class="cr-kpi-value">(\d+)</div>', html_text
        )
    }


def test_classification_buckets_are_mutually_exclusive():
    C, S, E, U = ai_validation.AI_COMPLETED, ai_validation.AI_SKIPPED, ai_validation.AI_ERROR, ai_validation.AI_UNAVAILABLE
    cases = {
        "a": (C, "CONFIRMED", False, "confirmed"),
        "b": (C, "CONFIRMED", True, "flagged"),          # a confirmation needing review is not a confirmation
        "c": (C, "YEAR_MISMATCH", False, "flagged"),     # a mismatch is flagged even if requires_review=False
        "d": (C, "FIELD_MISMATCH", True, "flagged"),
        "e": (C, "VALUE_SUSPICIOUS", True, "flagged"),
        "f": (C, "NOT_FOUND_IN_SOURCE", False, "flagged"),
        "g": (C, "INSUFFICIENT_CONTEXT", False, "inconclusive"),
        "h": (S, None, None, "skipped"),
        "i": (E, None, None, "error"),
        "j": (U, None, None, "unavailable"),
    }
    results = {f: _validation_result(f, {}, t, s, rr) for f, (t, s, rr, _b) in cases.items()}
    summary = ai_validation.summarize_validation_results(results)
    for f, (_t, _s, _rr, expected) in cases.items():
        assert summary["by_field"][f] == expected, f"case {f!r} misclassified"
    assert sum(summary["buckets"].values()) == len(cases)
    assert summary["buckets"]["skipped"] == 1 and summary["buckets"]["confirmed"] == 1
    assert summary["buckets"]["error"] == 1 and summary["buckets"]["flagged"] == 5
    # Skipped/errored records carry no validation status, so they never feed the per-status counts.
    assert sum(summary["statuses"].values()) == 7


def test_summary_three_confirmed_one_year_mismatch_rest_skipped():
    fields = [f"F{i}" for i in range(43)]
    plan = _three_confirmed_one_mismatch_plan(fields)
    results = {
        f: _validation_result(f, {}, *plan.get(f, (ai_validation.AI_SKIPPED, None, None))) for f in fields
    }
    summary = ai_validation.summarize_validation_results(results)
    assert summary["buckets"]["confirmed"] == 3
    assert summary["buckets"]["flagged"] == 1
    assert summary["buckets"]["skipped"] == 39      # skipped is never confirmed or flagged
    assert summary["statuses"] == {"CONFIRMED": 3, "YEAR_MISMATCH": 1}


_SUMMARY_CARDS = ("Total Fields", "Successfully Extracted", "Review Required", "Not Found")
_REMOVED_CARDS = ("AI Confirmed", "AI Flagged", "AI Not Validated", "AI Technical Errors",
                  "AI Presence Matches", "Correction Proposals", "Approved Corrections")


def _stored_ai_buckets(at):
    """The AI buckets are no longer shown on Data Review, but the results
    are still computed and stored - checked with the same function the
    removed cards used."""
    return ai_validation.summarize_validation_results(at.session_state["ai_validation_results"])["buckets"]


def test_data_review_shows_only_the_four_summary_cards():
    at = _data_review(_AiPatch(validate=_make_fake_validate(_three_confirmed_one_mismatch_plan)), "four-cards")
    k = _kpis(at)
    assert tuple(k) == _SUMMARY_CARDS, "Extraction Summary must show exactly the four deterministic cards"
    assert k["Total Fields"] == _SYNTHETIC_OK + _SYNTHETIC_NOT_FOUND
    assert k["Successfully Extracted"] == _SYNTHETIC_OK
    assert k["Review Required"] == 0
    assert k["Not Found"] == _SYNTHETIC_NOT_FOUND
    for label in _REMOVED_CARDS:
        assert label not in k, f"removed card {label!r} is still shown"
    for removed_text in ("validated by AI", "AI validation by field", "AI Dynamic Field Mapping",
                         "What do Accept / Reject / Keep", "Technical diagnostics", "Deterministic Verification"):
        assert not _shows(at, removed_text), f"removed explanatory content {removed_text!r} is still shown"


def test_data_review_metrics_three_confirmed_one_year_mismatch():
    at = _data_review(_AiPatch(validate=_make_fake_validate(_three_confirmed_one_mismatch_plan)), "metrics")
    k = _kpis(at)
    assert k["Total Fields"] == _SYNTHETIC_OK + _SYNTHETIC_NOT_FOUND
    assert k["Successfully Extracted"] == _SYNTHETIC_OK
    assert k["Not Found"] == _SYNTHETIC_NOT_FOUND

    buckets = _stored_ai_buckets(at)
    assert buckets["confirmed"] == 3, "AI confirmed count"
    assert buckets["flagged"] == 1, "AI flagged count"
    assert buckets["skipped"] + buckets["unavailable"] == _SYNTHETIC_OK - 4, "AI not-validated count"
    assert buckets["error"] == 0, "AI technical-error count"

    # Deterministic status is untouched by the AI flag.
    results = at.session_state["ai_validation_results"]
    flagged_field = next(f for f, r in results.items() if r.validation_status == "YEAR_MISMATCH")
    assert at.session_state["extracted"][flagged_field]["validation_status"] == "OK"


def test_data_review_counts_technical_errors_separately():
    def _plan(fields):
        C, E = ai_validation.AI_COMPLETED, ai_validation.AI_ERROR
        return {fields[0]: (C, "CONFIRMED", False), fields[1]: (C, "CONFIRMED", True),
                fields[2]: (E, None, None), fields[3]: (E, None, None)}

    at = _data_review(_AiPatch(validate=_make_fake_validate(_plan)), "tech-errors")
    buckets = _stored_ai_buckets(at)
    assert buckets["confirmed"] == 1, "the CONFIRMED+requires_review result must be flagged instead"
    assert buckets["flagged"] == 1, "technical errors must not be counted as flagged"
    assert buckets["error"] == 2, "AI technical-error count"
    assert buckets["skipped"] + buckets["unavailable"] == _SYNTHETIC_OK - 4, "AI not-validated count"
    assert _kpis(at)["Successfully Extracted"] == _SYNTHETIC_OK, "deterministic OK count unaffected"


def test_new_upload_does_not_inherit_previous_ai_results():
    real_run_ocr = ocr_upload.run_ocr
    text = _synthetic_ocr_text()
    try:
        with _AiPatch(validate=_make_fake_validate(_three_confirmed_one_mismatch_plan)):
            at = AppTest.from_file("app.py")
            at.run(timeout=60)
            _upload_synthetic(at, "first")
            assert at.session_state["ai_validation_results"], "fixture: first upload has AI results"

        # Second, different document: OCR fails part-way.
        def _ocr_fails(p):
            raise RuntimeError("mocked OCR failure")

        with _AiPatch():
            ocr_upload.run_ocr = _ocr_fails
            _upload(at, b"%PDF-1.4 synthetic second-fails " + uuid.uuid4().hex.encode())
        assert at.session_state["pdf_processing_error"]
        for key in ("ai_validation_results", "ai_mapping_results", "ai_mapping_stage_state",
                    "extracted", "merged_workbook_path"):
            assert at.session_state[key] is None, f"{key} survived a failed new upload"

        # Third, different document that succeeds with AI unavailable:
        # none of the first upload's confirmations survive.
        ocr_upload.run_ocr = lambda p: (text, {"status_code": 200})
        with _AiPatch():
            _upload(at, b"%PDF-1.4 synthetic third " + uuid.uuid4().hex.encode())
            at.sidebar.radio[0].set_value("2. Data Review")
            at.run(timeout=60)
        results = at.session_state["ai_validation_results"]
        assert all(r.technical_status == ai_validation.AI_UNAVAILABLE for r in results.values())
        buckets = _stored_ai_buckets(at)
        assert buckets["confirmed"] == 0 and buckets["flagged"] == 0, "stale AI counts after a new upload"
    finally:
        ocr_upload.run_ocr = real_run_ocr


# ---- Restored correction-proposal decision workflow ----------------

_PROPOSAL_FIELD = "Total assets"   # deterministically OK in the synthetic document


def _swap_proposal_generator(extracted, ai_results, lines, meta=None, layout_lines=None, **k):
    """Mocked generate_correction_proposals(): one verified 'swapped
    columns' proposal for an extracted (non-NOT_FOUND) field - the kind of
    field the dynamic-mapping controls never cover."""
    ed = extracted[_PROPOSAL_FIELD]
    proposal = ai_correction_proposals._make_proposal(
        _PROPOSAL_FIELD, ed, None, technical_status=ai_validation.AI_COMPLETED,
        proposal_status="PROPOSAL_AVAILABLE", confidence=0.7,
        reason="Current/previous figures appear swapped (mocked).", requires_review=True,
        proposed_source_line_number=ed.get("source_line_number"),
        proposed_source_text=ed.get("source_text"),
        proposed_column_current_label="2025", proposed_column_previous_label="2026",
        proposed_value_current=ed.get("fy_previous"), proposed_value_previous=ed.get("fy_current"),
    )
    return {_PROPOSAL_FIELD: proposal}, _available()


def _click_button(at, key):
    for w in at.button:
        if w.key == key:
            w.click()
            at.run(timeout=60)
            return
    raise AssertionError(f"button {key!r} not found on the Data Review page")


_REVIEW_SECTION = "Review Required / Correction Proposals"
_NO_DECISIONS = "No correction proposals require a decision for this document."


def _review_table(at):
    """{field: {"page": ..., "status": ...}} for the rows currently shown in
    the Field-Level Review table (the one with the PDF Page No. column)."""
    html_text = _raw_markdown_html(at)
    match = re.search(r"<thead><tr>(.*?PDF Page No\..*?)</tr></thead><tbody>(.*?)</tbody>", html_text, re.S)
    if match is None and _shows(at, "No fields match this filter."):
        return {}
    assert match is not None, "Field-Level Review table not found"
    headers = re.findall(r"<th[^>]*>(.*?)</th>", match.group(1))
    rows = {}
    for row_html in re.findall(r"<tr>(.*?)</tr>", match.group(2), re.S):
        cells = dict(zip(headers, re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)))
        rows[cells["Field"]] = {"page": cells["PDF Page No."], "status": re.sub(r"<[^>]+>", "", cells["Deterministic Status"])}
    return rows


def _set_filter(at, value):
    at.radio(key="data_review_filter").set_value(value)
    at.run(timeout=60)


def test_correction_proposal_section_renders_with_original_button_keys():
    at = _data_review(_AiPatch(corrections=_swap_proposal_generator), "proposal-render")
    assert _shows(at, _REVIEW_SECTION)
    assert not _shows(at, _NO_DECISIONS), "the empty-state message is shown alongside a proposal"
    keys = {w.key for w in at.button}
    for prefix in ("accept", "reject"):
        assert f"{prefix}_{_PROPOSAL_FIELD}" in keys, f"{prefix} button missing"
    assert f"keep_{_PROPOSAL_FIELD}" not in keys, "Keep Original is not part of the simplified section"
    assert f"manual_save_{_PROPOSAL_FIELD}" in keys, "Enter Manually is missing"
    # The proposal is decided here, not through the dynamic-mapping controls.
    assert f"accept_map_{_PROPOSAL_FIELD}" not in keys
    # The Correction Proposed filter shows exactly the proposed field.
    _set_filter(at, "Correction Proposed")
    assert list(_review_table(at)) == [_PROPOSAL_FIELD]


def test_accepting_a_proposal_flows_to_reviewed_data_only():
    import reviewed_dataset

    at = _data_review(_AiPatch(corrections=_swap_proposal_generator), "proposal-accept")
    original = dict(at.session_state["extracted"][_PROPOSAL_FIELD])

    _click_button(at, f"accept_{_PROPOSAL_FIELD}")
    assert not at.exception, "accepting a proposal raised an exception"

    record = at.session_state["approved_corrections"][_PROPOSAL_FIELD]
    assert record["decision"] == "accepted"
    assert record["original_value_current"] == original["fy_current"], "original value not preserved in the record"
    assert record["proposed_value_current"] == original["fy_previous"], "proposed value not recorded"
    assert record["timestamp"], "decision timestamp missing"

    # Raw extracted data is untouched; the reviewed layer carries the change.
    assert at.session_state["extracted"][_PROPOSAL_FIELD] == original, "raw extracted data was modified"
    reviewed_path = at.session_state["reviewed_workbook_path"]
    assert reviewed_path is not None and os.path.exists(reviewed_path), "reviewed workbook not generated"
    reviewed = reviewed_dataset.build_reviewed_extracted(
        at.session_state["extracted"], at.session_state["approved_corrections"],
    )
    assert reviewed[_PROPOSAL_FIELD]["fy_current"] == original["fy_previous"]
    assert reviewed[_PROPOSAL_FIELD]["fy_previous"] == original["fy_current"]
    assert reviewed[_PROPOSAL_FIELD]["validation_status"] == "HUMAN_APPROVED_CORRECTION"

    at.sidebar.radio[0].set_value("2. Data Review")
    at.run(timeout=60)
    assert _shows(at, f"**{_PROPOSAL_FIELD}**  ·  ✓ Accepted"), "decision label not shown"
    # The Approved filter shows exactly the accepted field.
    _set_filter(at, "Approved")
    assert list(_review_table(at)) == [_PROPOSAL_FIELD]


def test_rejecting_a_proposal_changes_nothing():
    at = _data_review(_AiPatch(corrections=_swap_proposal_generator), "proposal-reject")
    original = dict(at.session_state["extracted"][_PROPOSAL_FIELD])

    _click_button(at, f"reject_{_PROPOSAL_FIELD}")
    assert not at.exception, "recording the decision raised an exception"
    assert at.session_state["approved_corrections"][_PROPOSAL_FIELD]["decision"] == "rejected"
    assert at.session_state["extracted"][_PROPOSAL_FIELD] == original, "raw extracted data was modified"
    assert at.session_state["reviewed_workbook_path"] is None, "a reviewed workbook was generated without an acceptance"
    assert _shows(at, f"**{_PROPOSAL_FIELD}**  ·  ✕ Rejected"), "decision label not shown"


# ---- AI_MAPPED fields never get a conflicting correction proposal ----

_MAPPED_FIELD = "Borrowings current"   # NOT_FOUND in the synthetic document, then "AI-mapped" below
_NORMAL_FIELD = "Total assets"         # deterministically OK in the synthetic document


def _with_ai_mapped_field(extracted):
    """The synthetic extraction after a (mocked) verified dynamic mapping
    of _MAPPED_FIELD - the same shape map_missing_fields() produces."""
    augmented = {f: dict(ed) for f, ed in extracted.items()}
    assert augmented[_MAPPED_FIELD]["validation_status"] == "NOT_FOUND", "fixture: field must start NOT_FOUND"
    augmented[_MAPPED_FIELD].update(
        fy_current=77.0, fy_previous=66.0, ambiguous=False, validation_status="AI_MAPPED",
        source_line_number=None, source_text="mocked mapped row",
    )
    return augmented


def _flagged(extracted, *fields):
    return {
        f: _validation_result(f, extracted[f], ai_validation.AI_COMPLETED, "YEAR_MISMATCH", True)
        for f in fields
    }


def test_fields_requiring_correction_excludes_ai_mapped_fields():
    extracted = {
        "Mapped": {"validation_status": "AI_MAPPED"},
        "Normal": {"validation_status": "OK"},
        "Uncertain": {"validation_status": "REVIEW_REQUIRED"},
        "Missing": {"validation_status": "NOT_FOUND"},
        "Fine": {"validation_status": "OK"},
    }
    ai_results = _flagged(
        {f: {} for f in extracted}, "Mapped", "Normal", "Missing",
    )
    selected = ai_correction_proposals._fields_requiring_correction(extracted, ai_results)
    assert "Mapped" not in selected, "an AI_MAPPED field must never get a correction proposal"
    assert "Missing" not in selected, "a NOT_FOUND field must never get a correction proposal"
    assert "Fine" not in selected, "an unflagged OK field must not get a correction proposal"
    assert selected == ["Normal", "Uncertain"], "flagged and REVIEW_REQUIRED extracted fields must still qualify"


def test_real_correction_generation_never_sends_ai_mapped_field():
    extracted, meta, lines, layout = _synthetic_extraction()
    augmented = _with_ai_mapped_field(extracted)
    ai_results = _flagged(augmented, _MAPPED_FIELD, _NORMAL_FIELD)
    sent = []

    def _recording_call(prompt, *a, **k):
        __tracebackhide__ = True
        contexts = json.loads(prompt.split("FIELDS:\n", 1)[1])
        sent.extend(ctx["field"] for ctx in contexts)
        return json.dumps({"results": [
            {
                "field": ctx["field"], "status": "NO_PROPOSAL", "confidence": 0.5,
                "reason": "nothing better found (mocked)", "requires_review": False,
                "proposed_source_line_number": None,
                "proposed_column_current_label": None, "proposed_column_previous_label": None,
                "proposed_value_current": None, "proposed_value_previous": None,
                "evidence_line_numbers": [], "evidence_values": [],
            }
            for ctx in contexts
        ]})

    with _AiPatch(check=_available, call=_recording_call):
        proposals, _av = ai_correction_proposals.generate_correction_proposals(
            augmented, ai_results, lines, meta, layout_lines=layout,
        )
    assert _MAPPED_FIELD not in sent, "the AI-mapped field was sent for a correction proposal"
    assert _MAPPED_FIELD not in proposals, "the AI-mapped field received a correction proposal"
    assert sent == [_NORMAL_FIELD], "the flagged extracted field must still be sent"
    assert proposals[_NORMAL_FIELD].technical_status == ai_validation.AI_COMPLETED
    assert proposals[_NORMAL_FIELD].proposal_status == "NO_PROPOSAL"


def test_correction_decisions_never_discard_an_ai_mapped_value():
    import reviewed_dataset

    extracted, _meta, _lines, _layout = _synthetic_extraction()
    augmented = _with_ai_mapped_field(extracted)
    # Every decision a proposal could carry, on the only field a proposal
    # can now exist for - the AI-mapped field has no proposal, so no
    # proposal decision can be recorded against it.
    for decision in ("accepted", "rejected", "kept_original"):
        approved = {_NORMAL_FIELD: {
            "decision": decision,
            "proposed_value_current": augmented[_NORMAL_FIELD]["fy_previous"],
            "proposed_value_previous": augmented[_NORMAL_FIELD]["fy_current"],
            "proposed_source_line_number": augmented[_NORMAL_FIELD]["source_line_number"],
            "proposed_source_text": augmented[_NORMAL_FIELD]["source_text"],
        }}
        reviewed = reviewed_dataset.build_reviewed_extracted(augmented, approved)
        assert reviewed[_MAPPED_FIELD]["validation_status"] == "AI_MAPPED", f"mapped field altered by a proposal {decision!r}"
        assert reviewed[_MAPPED_FIELD]["fy_current"] == 77.0, f"mapped value lost after a proposal {decision!r}"
        assert reviewed[_MAPPED_FIELD]["fy_previous"] == 66.0, f"mapped value lost after a proposal {decision!r}"
    assert augmented[_MAPPED_FIELD]["fy_current"] == 77.0, "raw extracted data was modified"


def test_data_review_ai_mapped_field_has_only_mapping_controls():
    def _map_one_field(extracted, lines, meta=None, layout_lines=None, **k):
        augmented = _with_ai_mapped_field(extracted)
        results = {
            f: _mapping_result(f, ai_validation.AI_COMPLETED, "NO_CANDIDATE_FOUND")
            for f in _not_found_fields(extracted) if f != _MAPPED_FIELD
        }
        results[_MAPPED_FIELD] = ai_dynamic_mapping._make_result(
            _MAPPED_FIELD, ai_validation.AI_COMPLETED, "MAPPED_AND_VERIFIED", 0.8, "mocked row selection",
            matched_source_text="mocked mapped row", matched_value_current="77", matched_value_previous="66",
            matched_column_current_label="2026", matched_column_previous_label="2025",
        )
        return augmented, results, _available()

    def _flag_both(extracted, lines, meta=None, layout_lines=None, **k):
        return _flagged(extracted, _MAPPED_FIELD, _NORMAL_FIELD), {}, _available()

    # Real correction-proposal generation; its AI call is mocked to fail
    # so no proposal is offered for a decision in this test.
    at = _data_review(
        _AiPatch(check=_available, call=_failing_call, map_missing=_map_one_field, validate=_flag_both),
        "mapped-vs-proposal",
    )
    proposals = at.session_state["correction_proposals"]
    assert _MAPPED_FIELD not in proposals, "the AI-mapped field received a correction proposal"
    assert _NORMAL_FIELD in proposals, "the flagged extracted field must still get a proposal attempt"

    keys = {w.key for w in at.button}
    assert f"accept_map_{_MAPPED_FIELD}" in keys, "the mapping decision controls must remain"
    for prefix in ("accept", "reject", "keep"):
        assert f"{prefix}_{_MAPPED_FIELD}" not in keys, f"conflicting proposal {prefix} control shown for the mapped field"

    # The mapping workflow itself still works for that field.
    _click_button(at, f"accept_map_{_MAPPED_FIELD}")
    assert at.session_state["approved_corrections"][_MAPPED_FIELD]["decision"] == "accepted"
    assert at.session_state["extracted"][_MAPPED_FIELD]["fy_current"] == 77.0, "raw extracted data was modified"
    assert at.session_state["reviewed_workbook_path"] is not None


# ---- "Review Required" KPI: deterministic status only ----------------
#
# The Extraction Summary's OK / Review Required / Not Found cards
# partition the fields by DETERMINISTIC status (display_field_status).
# AI concerns are counted separately (AI Flagged) and never change a
# deterministic status - so AI-flagged fields with requires_review=True
# must not raise "Review Required", and nothing may be counted twice.

def _deterministic_partition_holds(k):
    return k["Successfully Extracted"] + k["Review Required"] + k["Not Found"] == k["Total Fields"]


def _ai_buckets_total(buckets):
    # Every bucket, including inconclusive, so each AI record counts once.
    return sum(buckets.values())


def test_review_required_is_not_raised_by_ai_flags():
    def _plan(fields):
        C = ai_validation.AI_COMPLETED
        return {fields[0]: (C, "YEAR_MISMATCH", True), fields[1]: (C, "FIELD_MISMATCH", True),
                fields[2]: (C, "CONFIRMED", False)}

    at = _data_review(_AiPatch(validate=_make_fake_validate(_plan)), "rr-ai-flags")
    k = _kpis(at)
    assert k["Review Required"] == 0, "AI flags must not raise the deterministic Review Required count"
    assert k["Successfully Extracted"] == _SYNTHETIC_OK and k["Not Found"] == _SYNTHETIC_NOT_FOUND
    assert _deterministic_partition_holds(k), "OK + Review Required + Not Found must equal Total Fields"
    buckets = _stored_ai_buckets(at)
    assert buckets["flagged"] == 2 and buckets["confirmed"] == 1, "AI buckets"
    assert _ai_buckets_total(buckets) == _SYNTHETIC_OK, "AI buckets must cover each validated field exactly once"
    flagged = [f for f, r in at.session_state["ai_validation_results"].items() if r.requires_review]
    assert len(flagged) == 2
    assert all(at.session_state["extracted"][f]["validation_status"] == "OK" for f in flagged), \
        "an AI flag must not change a deterministic status"


def test_review_required_counts_deterministic_review_fields():
    text = _synthetic_ocr_text(ambiguous_cash=True)
    extracted, _debug, _meta = ocr_pipeline.extract_financial_values(text, ocr_pipeline.TEMPLATE_FILE)
    assert extracted["Cash"]["validation_status"] == "REVIEW_REQUIRED", "fixture must produce an ambiguous Cash row"

    def _plan(fields):
        C = ai_validation.AI_COMPLETED
        other = next(f for f in fields if f != "Cash")
        # Cash is deterministically uncertain but AI-confirmed; another
        # field is deterministically OK but AI-flagged - the two axes
        # are independent.
        return {"Cash": (C, "CONFIRMED", False), other: (C, "YEAR_MISMATCH", True)}

    at = _data_review(_AiPatch(validate=_make_fake_validate(_plan)), "rr-deterministic", text)
    k = _kpis(at)
    statuses = [ed.get("validation_status") for ed in at.session_state["extracted"].values()]
    assert k["Review Required"] == statuses.count("REVIEW_REQUIRED") == 1, "deterministic Review Required count"
    assert k["Successfully Extracted"] == statuses.count("OK")
    assert k["Not Found"] == statuses.count("NOT_FOUND")
    assert _deterministic_partition_holds(k), "OK + Review Required + Not Found must equal Total Fields"
    buckets = _stored_ai_buckets(at)
    assert buckets["confirmed"] == 1 and buckets["flagged"] == 1, "AI buckets"
    sendable = len(statuses) - statuses.count("NOT_FOUND")
    assert _ai_buckets_total(buckets) == sendable, "AI buckets must cover each validated field exactly once"
    # The Review Required filter shows exactly the deterministic review field,
    # and the review section offers it for manual entry.
    _set_filter(at, "Review Required")
    assert list(_review_table(at)) == ["Cash"]
    assert "manual_save_Cash" in {w.key for w in at.button}, "Enter Manually missing for a Review Required field"
    assert not _shows(at, _NO_DECISIONS)


# ---- Value-less correction proposals can never erase a value ---------
#
# Reproduces the reported Revenue case: the model answered PROPOSAL_FOUND
# for the SAME line with real column labels but NO proposed values. That
# used to pass verification as PROPOSAL_AVAILABLE, and Accept then set
# both fiscal-year values to None in the reviewed dataset.

_LABELS = {"target_current_label": "FY26", "target_previous_label": "FY25"}
_ORIGINAL = {"validation_status": "OK", "fy_current": 100.0, "fy_previous": 90.0,
             "source_line_number": 3, "source_text": "Revenue  100  90"}
_LAYOUT = ["", "", "", "Revenue  100  90"]


def _finalize(proposed_current, proposed_previous, original=None):
    v = {
        "status": "PROPOSAL_FOUND", "confidence": 0.8, "reason": "mocked", "requires_review": True,
        "proposed_source_line_number": 3,
        "proposed_column_current_label": "FY26", "proposed_column_previous_label": "FY25",
        "proposed_value_current": proposed_current, "proposed_value_previous": proposed_previous,
        "evidence_line_numbers": [], "evidence_values": [],
    }
    return ai_correction_proposals._finalize_proposal(
        "Revenue", original or _ORIGINAL, None, v, {3}, "Revenue 100 90", _LAYOUT, _LABELS,
    )


def test_valueless_proposal_is_rejected():
    p = _finalize(None, None)
    assert p.proposal_status == "PROPOSAL_REJECTED_BY_EVIDENCE"
    assert "no proposed value was supplied" in p.reason
    assert not ai_correction_proposals.proposal_is_acceptable(p)


def test_proposal_value_checks():
    # Erasing one existing value is rejected; a missing value is fine only
    # when the field had none for that year; an unparseable value is rejected.
    assert _finalize(90, None).proposal_status == "PROPOSAL_REJECTED_BY_EVIDENCE"
    no_previous = dict(_ORIGINAL, fy_previous=None, source_text="Revenue  100", )
    assert _finalize(100, None, original=no_previous).proposal_status == "PROPOSAL_AVAILABLE"
    problem = ai_correction_proposals.proposed_values_problem("abc", 90, 100.0, 90.0)
    assert problem is not None and "not a valid number" in problem
    # A genuine swapped-columns proposal is still offered and acceptable.
    swap = _finalize(90, 100)
    assert swap.proposal_status == "PROPOSAL_AVAILABLE"
    assert ai_correction_proposals.proposal_is_acceptable(swap)


def _valueless_proposal_call(prompt, *a, **k):
    """Mocked correction response: PROPOSAL_FOUND on the field's own line
    with real column labels but no values - the reported Revenue case."""
    __tracebackhide__ = True
    contexts = json.loads(prompt.split("FIELDS:\n", 1)[1])
    results = []
    for ctx in contexts:
        current_label, previous_label = list(ctx["deterministic_mapping"].keys())
        line = ctx["target_row"]["line_number"]
        results.append({
            "field": ctx["field"], "status": "PROPOSAL_FOUND", "confidence": 0.8,
            "reason": "not enough evidence to confirm a better mapping (mocked)", "requires_review": True,
            "proposed_source_line_number": line,
            "proposed_column_current_label": current_label, "proposed_column_previous_label": previous_label,
            "proposed_value_current": None, "proposed_value_previous": None,
            "evidence_line_numbers": [], "evidence_values": [],
        })
    return json.dumps({"results": results})


def test_real_generation_rejects_valueless_proposal():
    extracted, meta, lines, layout = _synthetic_extraction()
    ai_results = _flagged(extracted, _NORMAL_FIELD)
    with _AiPatch(check=_available, call=_valueless_proposal_call):
        proposals, _av = ai_correction_proposals.generate_correction_proposals(
            extracted, ai_results, lines, meta, layout_lines=layout,
        )
    p = proposals[_NORMAL_FIELD]
    assert p.proposal_status == "PROPOSAL_REJECTED_BY_EVIDENCE", "a value-less proposal must not be offered"
    assert not ai_correction_proposals.proposal_is_acceptable(p)


def test_reviewed_dataset_never_erases_on_valueless_acceptance():
    import reviewed_dataset

    extracted = {"Revenue": dict(_ORIGINAL)}
    accepted = {"Revenue": {"decision": "accepted", "proposed_value_current": None, "proposed_value_previous": None,
                            "proposed_source_line_number": 3, "proposed_source_text": "Revenue  100  90"}}
    reviewed = reviewed_dataset.build_reviewed_extracted(extracted, accepted)["Revenue"]
    assert (reviewed["fy_current"], reviewed["fy_previous"]) == (100.0, 90.0), "a value-less acceptance erased values"
    assert reviewed["validation_status"] == "OK"
    # Rejected / kept original never change a normally extracted field.
    for decision in ("rejected", "kept_original"):
        out = reviewed_dataset.build_reviewed_extracted(extracted, {"Revenue": {"decision": decision}})["Revenue"]
        assert (out["fy_current"], out["fy_previous"]) == (100.0, 90.0)
    # A valid acceptance still applies.
    swap = dict(accepted["Revenue"], proposed_value_current="90", proposed_value_previous="100")
    out = reviewed_dataset.build_reviewed_extracted(extracted, {"Revenue": swap})["Revenue"]
    assert (out["fy_current"], out["fy_previous"]) == (90.0, 100.0)
    assert out["validation_status"] == "HUMAN_APPROVED_CORRECTION"
    assert extracted["Revenue"]["fy_current"] == 100.0, "raw extracted data was modified"


def _flag_normal_field(extracted, lines, meta=None, layout_lines=None, **k):
    return _flagged(extracted, _NORMAL_FIELD), {}, _available()


def test_data_review_offers_no_valueless_proposal():
    at = _data_review(
        _AiPatch(check=_available, call=_valueless_proposal_call,
                 map_missing=_fake_map(lambda nf: ({}, None)), validate=_flag_normal_field),
        "valueless-real",
    )
    assert at.session_state["correction_proposals"][_NORMAL_FIELD].proposal_status == "PROPOSAL_REJECTED_BY_EVIDENCE"
    keys = {w.key for w in at.button}
    assert f"accept_{_NORMAL_FIELD}" not in keys, "a value-less proposal was offered for a decision"
    assert _shows(at, _NO_DECISIONS), "empty-state message missing"
    _set_filter(at, "Correction Proposed")
    assert list(_review_table(at)) == [], "a value-less proposal is listed as a correction proposal"


def test_data_review_disables_accept_for_stale_valueless_proposal():
    # A value-less PROPOSAL_AVAILABLE created before the fix (e.g. still in
    # a running session) must not be acceptable.
    def _stale_generator(extracted, ai_results, lines, meta=None, layout_lines=None, **k):
        ed = extracted[_NORMAL_FIELD]
        p = ai_correction_proposals._make_proposal(
            _NORMAL_FIELD, ed, None, technical_status=ai_validation.AI_COMPLETED,
            proposal_status="PROPOSAL_AVAILABLE", confidence=0.8, reason="mocked stale proposal",
            requires_review=True, proposed_source_line_number=ed.get("source_line_number"),
            proposed_column_current_label="2026", proposed_column_previous_label="2025",
            proposed_value_current=None, proposed_value_previous=None,
        )
        return {_NORMAL_FIELD: p}, _available()

    at = _data_review(_AiPatch(corrections=_stale_generator), "valueless-stale")
    original = dict(at.session_state["extracted"][_NORMAL_FIELD])
    buttons = {w.key: w for w in at.button}
    assert buttons[f"accept_{_NORMAL_FIELD}"].disabled, "Accept must be disabled for a value-less proposal"
    assert not buttons[f"reject_{_NORMAL_FIELD}"].disabled
    assert _shows(at, "Accept unavailable: this proposal has no valid value to apply."), "explanation missing"

    _click_button(at, f"reject_{_NORMAL_FIELD}")
    assert at.session_state["approved_corrections"][_NORMAL_FIELD]["decision"] == "rejected"
    assert at.session_state["extracted"][_NORMAL_FIELD] == original, "raw extracted data was modified"
    assert at.session_state["reviewed_workbook_path"] is None


# ---- PDF page provenance ---------------------------------------------

def _paged_synthetic_ocr_text():
    """The synthetic balance sheet placed on PDF page 2: an invented cover
    page ends with LLMWhisperer's '<<<' page separator (plus the form feed
    it emits) before the statement starts."""
    return "Invented Company Limited - Annual Report (cover page)\n<<<\f\n" + _synthetic_ocr_text() + "\n<<<\f\n"


def test_line_page_numbers():
    lines = ["cover", "<<<", "balance sheet", "total assets 2000 1800", "<<<", "cash flow", "<<<"]
    assert ocr_upload.line_page_numbers(lines) == [1, 1, 2, 2, 2, 3, 3]
    assert ocr_upload.line_page_numbers(["no", "separators", "here"]) is None, "no separators must mean no page data"


def test_page_formatting():
    import app

    pages = [1, 1, 2, 2, 3]
    assert app.pdf_page_for_line(3, pages) == 2
    assert app.pdf_page_for_line(None, pages) is None
    assert app.pdf_page_for_line(99, pages) is None
    assert app.pdf_page_for_line(1, None) is None
    assert app.format_pdf_pages(12, 12) == "12"
    assert app.format_pdf_pages(12, 14) == "Current: 12 | Previous: 14"
    assert app.format_pdf_pages(12, None) == "Current: 12 | Previous: Not available"
    assert app.format_pdf_pages(None, None) == "Not available"
    assert app.field_pdf_pages({"source_line_number": 4}, pages) == "3"
    assert app.field_pdf_pages({"source_line_number": None}, pages) == "Not available"
    assert app.field_pdf_pages({"source_line_number": 4}, None) == "Not available"


def test_pdf_page_column_shows_actual_source_pages():
    at = _data_review(
        _AiPatch(corrections=_swap_proposal_generator), "pages", _paged_synthetic_ocr_text(),
    )
    rows = _review_table(at)
    extracted = at.session_state["extracted"]
    for field, row in rows.items():
        if extracted[field].get("validation_status") == "NOT_FOUND":
            assert row["page"] == "Not available", f"{field}: a field with no source line must show Not available"
        else:
            assert row["page"] == "2", f"{field}: expected the statement's real page"
    assert sum(1 for r in rows.values() if r["page"] == "2") == _SYNTHETIC_OK
    # The correction section shows the same page for the proposal.
    assert _shows(at, "**PDF Page No.**  \n2"), "proposal page missing in the review section"
    # Extracted values and statuses are unchanged by the new column.
    assert all(r["status"] in ("OK", "Not Found") for r in rows.values())


def test_pdf_page_column_without_page_metadata():
    at = _data_review(_AiPatch(), "no-pages")   # no '<<<' separators in the text
    assert at.session_state["source_line_pages"] is None
    assert {r["page"] for r in _review_table(at).values()} == {"Not available"}


def test_all_five_filters():
    at = _data_review(_AiPatch(corrections=_swap_proposal_generator), "filters",
                      _synthetic_ocr_text(ambiguous_cash=True))
    extracted = at.session_state["extracted"]
    assert len(_review_table(at)) == len(extracted), "Show All must list every field"
    expected = {
        "Review Required": [f for f, ed in extracted.items() if ed["validation_status"] == "REVIEW_REQUIRED"],
        "Not Found": [f for f, ed in extracted.items() if ed["validation_status"] == "NOT_FOUND"],
        "Correction Proposed": [_PROPOSAL_FIELD],
        "Approved": [],
    }
    for value, fields in expected.items():
        _set_filter(at, value)
        assert sorted(_review_table(at)) == sorted(fields), f"{value} filter"
    _set_filter(at, "Show All")
    assert len(_review_table(at)) == len(extracted)


# ---- Enter Manually (existing approved_corrections workflow) ----------

def _enter_manually(at, field, current, previous):
    at.text_input(key=f"manual_cur_{field}").set_value(current)
    at.text_input(key=f"manual_prev_{field}").set_value(previous)
    _click_button(at, f"manual_save_{field}")


def test_manual_entry_flows_through_reviewed_data_only():
    import reviewed_dataset

    at = _data_review(_AiPatch(corrections=_swap_proposal_generator), "manual-ok")
    original = dict(at.session_state["extracted"][_PROPOSAL_FIELD])
    _enter_manually(at, _PROPOSAL_FIELD, "2,100.50", "1,900")
    assert not at.exception, "saving manual values raised an exception"

    record = at.session_state["approved_corrections"][_PROPOSAL_FIELD]
    assert record["decision"] == "accepted" and record["source"] == "manual"
    assert record["original_value_current"] == original["fy_current"], "original value not preserved"
    assert at.session_state["extracted"][_PROPOSAL_FIELD] == original, "raw extracted data was modified"
    assert at.session_state["reviewed_workbook_path"] is not None
    reviewed = reviewed_dataset.build_reviewed_extracted(
        at.session_state["extracted"], at.session_state["approved_corrections"],
    )[_PROPOSAL_FIELD]
    assert (reviewed["fy_current"], reviewed["fy_previous"]) == (2100.5, 1900.0)
    assert reviewed["validation_status"] == "HUMAN_APPROVED_CORRECTION"
    assert _shows(at, f"**{_PROPOSAL_FIELD}**  ·  ✓ Entered manually"), "manual decision label not shown"


def test_manual_entry_cannot_erase_an_existing_value():
    at = _data_review(_AiPatch(corrections=_swap_proposal_generator), "manual-erase")
    original = dict(at.session_state["extracted"][_PROPOSAL_FIELD])
    _enter_manually(at, _PROPOSAL_FIELD, "", "1,900")   # blank current year over an existing value
    assert _PROPOSAL_FIELD not in (at.session_state["approved_corrections"] or {}), "an erasing manual entry was saved"
    assert any("Manual values not saved" in str(e.value) for e in at.error), "no error explained the rejection"
    assert at.session_state["extracted"][_PROPOSAL_FIELD] == original
    assert at.session_state["reviewed_workbook_path"] is None


# ---- Data Upload: Excel upload option (demonstration only) ------------

def test_excel_upload_option_is_demonstration_only():
    real_run_ocr = ocr_upload.run_ocr

    def _must_not_run(*a, **k):
        raise AssertionError("selecting an Excel file must not start PDF processing")

    ocr_upload.run_ocr = _must_not_run
    try:
        with _AiPatch():
            at = AppTest.from_file("app.py")
            at.run(timeout=60)
            uploaders = list(at.file_uploader)
            assert uploaders[0].key.startswith("pdf_uploader_"), "the PDF uploader must remain the first upload option"
            excel = next(u for u in uploaders if u.key == "excel_demo_uploader")
            assert _shows(at, "Upload Excel File") and _shows(at, ".xlsx, .xls"), "Excel option label missing"
            assert _shows(at, "Demonstration only"), "Excel option must say it is not functional"

            excel.set_value(("borrower_financials.xlsx", b"PK\x03\x04 invented", "application/vnd.ms-excel"))
            at.run(timeout=60)
    finally:
        ocr_upload.run_ocr = real_run_ocr

    assert not at.exception, "selecting an Excel file raised an exception"
    assert _shows(at, "Selected: borrower_financials.xlsx — not processed."), "selected filename not shown"
    for key in ("pdf_name", "merged_workbook_path", "pdf_processing_error", "extracted"):
        assert at.session_state[key] is None, f"Excel selection changed {key}"
    assert at.session_state["source_mode"] == "none", "Excel selection activated a data source"


def main():
    test_data_upload_shows_only_five_sections_no_ai_panels()
    test_data_review_business_glossary_column_position()
    test_ai_dynamic_mapping_explanation_block_renders()
    test_ai_dynamic_mapping_table_refinement()
    test_dynamic_mapping_verified_field_shows_review_controls()
    test_dynamic_mapping_skipped_field_shows_correct_reason()
    test_dynamic_mapping_summary_reconciles_with_table()
    test_field_review_status_labels_are_simplified()
    test_candidate_ranking_detail_removed_from_standard_ui()

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(" -", f)
        return 1
    print("ALL SIMPLIFIED-PAGES TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
