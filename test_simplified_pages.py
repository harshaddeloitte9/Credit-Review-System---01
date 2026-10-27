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


def test_openai_unavailable_status_uses_openai_label():
    print()
    print("=" * 100)
    print("TEST - the upload pipeline shows the selected provider name when AI is unavailable")
    print("=" * 100)

    real_run_ocr = ocr_upload.run_ocr
    real_provider = ai_validation.AI_PROVIDER
    real_av_check = ai_validation.check_ollama_availability
    real_adm_check = ai_dynamic_mapping.check_ollama_availability
    realistic_text = _load_realistic_ocr_text()
    ocr_upload.run_ocr = lambda pdf_path: (realistic_text, {"status_code": 200})

    def _fake_openai_unavailable(*a, **k):
        return ai_validation.OllamaAvailability(False, False, [], "OpenAI API is not reachable.")

    ai_validation.check_ollama_availability = _fake_openai_unavailable
    ai_dynamic_mapping.check_ollama_availability = _fake_openai_unavailable
    ai_validation.AI_PROVIDER = "openai"

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload(at, b"%PDF-1.4 fake content openai-unavailable\n" * 20)
        _ok(not at.exception, f"upload runs with no exception: {[str(e) for e in at.exception]}")

        text = _page_text(at)
        _ok("Skipped (OpenAI unavailable)" in text, "the upload pipeline uses the selected provider name in the unavailable status")
        _ok("Skipped (Ollama unavailable)" not in text, "the old hardcoded Ollama wording is not shown for an OpenAI-selected run")
        _ok(not at.exception, f"Data Upload page renders cleanly end to end: {[str(e) for e in at.exception]}")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        ai_validation.AI_PROVIDER = real_provider
        ai_validation.check_ollama_availability = real_av_check
        ai_dynamic_mapping.check_ollama_availability = real_adm_check


def test_ai_provider_label_uses_detail_when_provider_is_not_set_explicitly():
    import app

    _ok(app._ai_provider_label(detail="OpenAI API is not reachable.") == "OpenAI",
        "the skipped-stage label follows the actual provider in the failure detail")
    _ok(app._ai_provider_label(detail="Ollama is not reachable at http://localhost:11434") == "Ollama",
        "the skipped-stage label falls back to Ollama when that is the actual provider detail")


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

        text = _page_text(at, "2. Data Review")
        _ok("does not generate or directly populate financial figures" in text,
            "the plain-English AI-role explanation is shown")
        _ok("Standard Field" in text and "AI-Selected Row" in text and "Human Decision" in text,
            "the compact workflow (Standard Field -> ... -> Human Decision) is shown")
        _ok("Accept / Reject / Keep as Not Found mean here" in text,
            "the Accept/Reject/Keep as Not Found explanations are available")

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

        text = _page_text(at, "2. Data Review")
        for header in ("Candidates Found", "Shortlisted", "AI-Selected Row", "Confidence",
                        "Parsed Values", "Review Status"):
            _ok(header in text, f"shortened column header '{header}' is present")
        for old_header in ("Candidates Discovered", "Candidates Shortlisted", "AI-Selected Source Row",
                           "Deterministically Parsed Values", "Year Mapping", "Population Status"):
            _ok(old_header not in text, f"old long column header '{old_header}' no longer appears")

        _ok("field(s) considered" in text, "the concise status summary line is shown by default")
        _ok("Technical diagnostics" in text, "the detailed technical breakdown is available in its own expander")
        _ok("No Plausible Candidate" in text, "a declined field's compact Review Status badge is shown in the refined table")

        html = _raw_markdown_html(at, "2. Data Review")
        _ok('title="NO_CANDIDATE_FOUND"' in html,
            "the compact Review Status badge still carries the exact underlying status as a hover title - never actually lost")

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
        _ok("accept_map_Capex" in button_keys, "Accept Mapping button is present for the verified Capex mapping")
        _ok("reject_map_Capex" in button_keys, "Reject Mapping button is present for the verified Capex mapping")
        _ok("keep_map_Capex" in button_keys, "Keep as Not Found button is present for the verified Capex mapping")

        text = _page_text(at)
        _ok("Capex" in text, "it is obvious which template field the decision applies to (field name shown)")
        _ok("Acquisition of property" in text, "the AI-selected source row is shown before the decision")
        _ok("82%" in text, "the AI row-selection confidence is shown before the decision")
        _ok("Review Decisions" in text, "pending decisions are under their own clearly-labeled heading")

        # Other NOT_FOUND fields (declined -> NO_CANDIDATE_FOUND) must
        # NOT get an Accept button of their own.
        other_not_found = [f for f in mapping_results if f != "Capex"]
        for f in other_not_found:
            _ok(f"accept_map_{f}" not in button_keys, f"no Accept button for the non-verified field '{f}'")

        # The separate correction-proposal workflow is redundant with the
        # dynamic-mapping Review Decisions controls and is no longer shown
        # in the standard Data Review UI.
        _ok("Correction Proposals Awaiting Decision" not in text,
            "the redundant correction-proposal section is hidden from the standard Data Review UI")

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
        _ok("✓ Approved" in _page_text(at), "the Review Status badge reflects the recorded decision after accepting")

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

        text = _page_text(at, "2. Data Review")
        _ok("AI Skipped — Time Budget Exhausted" in text, "the skipped reason is shown clearly, not a generic/misleading label")
        _ok("No Plausible Candidate" not in text, "a skipped (never attempted) field is never mislabeled as having no candidate")

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

        text = _page_text(at, "2. Data Review")
        m = re.search(
            r"(\d+) field\(s\) considered — (\d+) awaiting your approval, (\d+) already decided, "
            r"(\d+) no plausible candidate, (\d+) skipped \(time budget\), (\d+) need attention",
            text,
        )
        _ok(m is not None, "fixture sanity check: the concise summary caption was found on the page")
        if m:
            total, awaiting, decided, no_candidate, skipped, needs_attention = (int(g) for g in m.groups())
            _ok(total == len(at.session_state["ai_mapping_results"]),
                f"the summary's own total ({total}) matches the real number of fields considered ({len(at.session_state['ai_mapping_results'])})")
            _ok(awaiting + decided + no_candidate + skipped + needs_attention == total,
                f"the five summary buckets add up to the total ({awaiting}+{decided}+{no_candidate}+{skipped}+{needs_attention} == {total})")
            _ok(awaiting >= 1, "at least one field (Capex) is genuinely awaiting approval in this fixture")

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
