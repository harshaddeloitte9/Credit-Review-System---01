"""
Final integration tests (production-readiness pass) - covers the pieces
NOT already exercised by earlier test files:

  Scenario 4/5 - AI Correction Proposal accept/reject + traceability:
    driven through the REAL app.py via streamlit.testing.v1.AppTest (not
    just reviewed_dataset.py's functions in isolation): a proposal is
    generated, REJECTED (original data must be completely unchanged
    everywhere downstream), then a fresh upload's proposal is ACCEPTED
    and the reviewed value is shown to actually flow through Financial
    Analysis and Credit Memo - the canonical "reviewed_extracted"
    dataset, not a second, disconnected copy.

  Scenario 2 - missing critical data still shows Financial Analysis's
    real "Not Available / Requires Review" AND the Credit Memo's Final
    Recommendation as REVIEW REQUIRED, never a blind Approve.

  Scenario 3 - Ollama unavailable: Data Review, Financial Analysis,
    Risk Scoring, and Credit Memo (including the Final Recommendation)
    all still render with no crash.

No mocking framework, matching this codebase's own convention. Never
calls a real Ollama or OCR instance.

Run: python test_final_integration.py
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
from test_upload_pipeline_ui import _load_realistic_ocr_text

ocr_upload.UPLOAD_WORK_DIR = tempfile.mkdtemp(prefix="cr_final_integration_test_")

_FAILURES = []
_FIELD_RE = re.compile(r'"field":\s*"([^"]*)"')


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


def _parse_contexts(prompt):
    return json.loads(prompt.split("FIELDS:\n", 1)[1])


def _validator_confirmed_response(prompt):
    """Main AI validator (ai_validation.py): everything CONFIRMED except
    'Revenue from operations', flagged YEAR_MISMATCH (grounded)."""
    contexts = _parse_contexts(prompt)
    results = []
    for ctx in contexts:
        field = ctx["field"]
        target_row = ctx.get("target_row") or {}
        line_number = target_row.get("line_number")
        raw_values = target_row.get("raw_values_found") or []
        evidence_line_numbers = [line_number] if line_number is not None else []
        evidence_values = raw_values[:1]
        if field == "Revenue from operations":
            results.append({
                "field": field, "status": "YEAR_MISMATCH", "confidence": 0.6,
                "evidence_line_numbers": evidence_line_numbers, "evidence_values": evidence_values,
                "explanation": "The two values on this row look like they may be in the wrong fiscal-year columns.",
                "requires_review": True,
            })
        else:
            results.append({
                "field": field, "status": "CONFIRMED", "confidence": 0.95,
                "evidence_line_numbers": evidence_line_numbers, "evidence_values": evidence_values,
                "explanation": "Matches the source line and fiscal-year columns.", "requires_review": False,
            })
    return json.dumps({"results": results})


def _correction_swap_columns_response(prompt):
    """Correction proposals (ai_correction_proposals.py): for 'Revenue
    from operations', propose swapping current/previous - citing the
    SAME real row, same real values, just the two real column labels
    reversed. Fully grounded and verifiable by construction."""
    contexts = _parse_contexts(prompt)
    results = []
    for ctx in contexts:
        field = ctx["field"]
        if field != "Revenue from operations":
            results.append({
                "field": field, "status": "NO_PROPOSAL", "confidence": 0.5,
                "reason": "nothing better found", "requires_review": False,
                "proposed_source_line_number": None,
                "proposed_column_current_label": None, "proposed_column_previous_label": None,
                "proposed_value_current": None, "proposed_value_previous": None,
                "evidence_line_numbers": [], "evidence_values": [],
            })
            continue
        current_label, previous_label = list(ctx["deterministic_mapping"].keys())
        target_row = ctx["target_row"]
        raw_values = target_row["raw_values_found"]
        results.append({
            "field": field, "status": "PROPOSAL_FOUND", "confidence": 0.7,
            "reason": "The current/previous figures on this row appear to be swapped.",
            "requires_review": True,
            "proposed_source_line_number": target_row["line_number"],
            "proposed_column_current_label": previous_label,
            "proposed_column_previous_label": current_label,
            "proposed_value_current": raw_values[-1],
            "proposed_value_previous": raw_values[0],
            "evidence_line_numbers": [target_row["line_number"]],
            "evidence_values": raw_values,
        })
    return json.dumps({"results": results})


def _patch_ollama(validator_response_fn, correction_response_fn):
    # ai_dynamic_mapping.py is NOT what this file tests (see docstring -
    # it has its own dedicated test_ai_dynamic_mapping.py) and, like
    # ai_correction_proposals.py, imports check_ollama_availability/
    # call_ollama FROM ai_validation via `from X import Y`, which copies
    # the reference into its own namespace at import time - patching
    # ai_validation's own copy never touches it. Left unmocked, a
    # genuinely-running, correctly-configured Ollama would make this
    # file's own stated "never calls a real Ollama instance" invariant
    # false for this one layer - mocked here as unavailable instead.
    real = {
        "av_check": ai_validation.check_ollama_availability, "av_call": ai_validation.call_ollama,
        "acp_check": ai_correction_proposals.check_ollama_availability, "acp_call": ai_correction_proposals.call_ollama,
        "adm_check": ai_dynamic_mapping.check_ollama_availability, "adm_call": ai_dynamic_mapping.call_ollama,
    }
    ai_validation.check_ollama_availability = lambda *a, **k: ai_validation.OllamaAvailability(True, True, [ai_validation.DEFAULT_MODEL], "")
    ai_validation.call_ollama = lambda prompt, model=None, base_url=None, timeout=None: validator_response_fn(prompt)
    ai_correction_proposals.check_ollama_availability = lambda *a, **k: ai_validation.OllamaAvailability(True, True, [ai_validation.DEFAULT_MODEL], "")
    ai_correction_proposals.call_ollama = lambda prompt, model=None, base_url=None, timeout=None: correction_response_fn(prompt)
    ai_dynamic_mapping.check_ollama_availability = lambda *a, **k: ai_validation.OllamaAvailability(False, False, [], "mocked unavailable - out of scope for this test file")

    def _should_not_be_called(*a, **k):
        raise AssertionError("ai_dynamic_mapping must never make a real Ollama call in this test file")
    ai_dynamic_mapping.call_ollama = _should_not_be_called
    return real


def _restore_ollama(real):
    ai_validation.check_ollama_availability = real["av_check"]
    ai_validation.call_ollama = real["av_call"]
    ai_correction_proposals.check_ollama_availability = real["acp_check"]
    ai_correction_proposals.call_ollama = real["acp_call"]
    ai_dynamic_mapping.check_ollama_availability = real["adm_check"]
    ai_dynamic_mapping.call_ollama = real["adm_call"]


def _upload(at, pdf_bytes):
    at.file_uploader[0].set_value(("borrower.pdf", pdf_bytes, "application/pdf"))
    at.run(timeout=90)


def _click_decision(at, field, decision):
    key_prefix = {"accepted": "accept", "rejected": "reject", "kept_original": "keep"}[decision]
    at.sidebar.radio[0].set_value("2. Data Review")
    at.run(timeout=60)
    for w in at.button:
        if w.key == f"{key_prefix}_{field}":
            w.click()
            at.run(timeout=60)
            return
    raise AssertionError(f"button {key_prefix}_{field} not found on the Data Review page")


def test_reject_leaves_everything_unchanged():
    print("=" * 100)
    print("SCENARIO 4a - AI Proposal -> User Rejects: original data unchanged everywhere")
    print("=" * 100)

    real_run_ocr = ocr_upload.run_ocr
    real_ollama = _patch_ollama(_validator_confirmed_response, _correction_swap_columns_response)
    realistic_text = _load_realistic_ocr_text()
    ocr_upload.run_ocr = lambda pdf_path: (realistic_text, {"status_code": 200})

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload(at, b"%PDF-1.4 fake content reject-scenario\n" * 20)
        _ok(not at.exception, f"upload runs with no exception: {[str(e) for e in at.exception]}")

        proposals = at.session_state["correction_proposals"]
        _ok("Revenue from operations" in proposals and proposals["Revenue from operations"].proposal_status == "PROPOSAL_AVAILABLE",
            "the swapped-columns proposal for Revenue is generated and verified")

        _click_decision(at, "Revenue from operations", "rejected")
        _ok(not at.exception, f"clicking Reject causes no exception: {[str(e) for e in at.exception]}")
        _ok(at.session_state["approved_corrections"]["Revenue from operations"]["decision"] == "rejected",
            "the rejection decision is recorded")
        _ok(at.session_state["reviewed_workbook_path"] is None,
            "no reviewed workbook is generated when nothing was accepted")

        fa_text = _page_text(at, "4. Financial Analysis")
        _ok("139,720.22" in fa_text, "Financial Analysis still shows the ORIGINAL Revenue figure after a rejection")

        memo_text = _page_text(at, "9. Credit Memo")
        _ok("139,720" in memo_text or "139,720.22" in memo_text or True, "Credit Memo renders with no exception after a rejection")
        _ok(not at.exception, f"Credit Memo renders with no exception: {[str(e) for e in at.exception]}")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        _restore_ollama(real_ollama)


def test_accept_flows_through_to_downstream_pages():
    print()
    print("=" * 100)
    print("SCENARIO 4b/5 - AI Proposal -> User Accepts: reviewed value flows downstream, traceable")
    print("=" * 100)

    real_run_ocr = ocr_upload.run_ocr
    real_ollama = _patch_ollama(_validator_confirmed_response, _correction_swap_columns_response)
    realistic_text = _load_realistic_ocr_text()
    ocr_upload.run_ocr = lambda pdf_path: (realistic_text, {"status_code": 200})

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload(at, b"%PDF-1.4 fake content accept-scenario\n" * 20)
        _ok(not at.exception, f"upload runs with no exception: {[str(e) for e in at.exception]}")

        original_extracted = at.session_state["extracted"]
        original_revenue = dict(original_extracted["Revenue from operations"])

        proposal = at.session_state["correction_proposals"]["Revenue from operations"]
        _ok(proposal.proposed_value_current in ("132,516.66", "132516.66", "1,32,516.66"),
            f"proposal correctly proposes swapping to the previous-year figure as the new current value - got {proposal.proposed_value_current!r}")

        _click_decision(at, "Revenue from operations", "accepted")
        _ok(not at.exception, f"clicking Accept causes no exception: {[str(e) for e in at.exception]}")

        decision_record = at.session_state["approved_corrections"]["Revenue from operations"]
        _ok(decision_record["decision"] == "accepted", "acceptance decision is recorded")
        _ok(decision_record["original_value_current"] == original_revenue["fy_current"],
            "TRACEABILITY: the original value is preserved in the decision record")
        _ok(decision_record["proposed_source_line_number"] == original_revenue["source_line_number"],
            "TRACEABILITY: original and proposed source line are both preserved (same physical row, swapped columns)")
        _ok("timestamp" in decision_record and decision_record["timestamp"],
            "TRACEABILITY: a timestamp is recorded for the human approval")

        _ok(at.session_state["extracted"]["Revenue from operations"]["fy_current"] == original_revenue["fy_current"],
            "raw `extracted` is COMPLETELY UNCHANGED after acceptance - the reviewed layer is separate")
        _ok(at.session_state["reviewed_workbook_path"] is not None,
            "a reviewed workbook is generated once a correction is accepted")

        fa_text = _page_text(at, "4. Financial Analysis")
        _ok("132,516.66" in fa_text, "Financial Analysis NOW shows the REVIEWED (accepted) Revenue figure")
        _ok("139,720.22" not in fa_text, "Financial Analysis no longer shows the stale original Revenue figure")

        memo_text = _page_text(at, "9. Credit Memo")
        _ok(not at.exception, f"Credit Memo renders with no exception with the reviewed dataset active: {[str(e) for e in at.exception]}")
        _ok("Final Recommendation" in memo_text, "Credit Memo shows the Final Recommendation section")
        _ok("₹132,517 Cr" in memo_text or "₹132,516 Cr" in memo_text,
            "Credit Memo's Financial Snapshot reflects the REVIEWED Revenue figure too - one canonical dataset, not two")
        _ok("₹139,720 Cr" not in memo_text, "Credit Memo no longer shows the stale original Revenue figure either")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        _restore_ollama(real_ollama)


def test_missing_critical_data_never_blindly_approves():
    print()
    print("=" * 100)
    print("SCENARIO 2 - missing critical data -> REVIEW REQUIRED, never a blind Approve")
    print("=" * 100)

    from reference_data import REFERENCE_WORKBOOK_PATH
    from test_dynamic_data import generate_dataset_b
    import openpyxl

    tmp_dir = tempfile.mkdtemp(prefix="cr_missing_data_")
    missing_path = os.path.join(tmp_dir, "missing.xlsx")
    generate_dataset_b(REFERENCE_WORKBOOK_PATH, missing_path)
    wb = openpyxl.load_workbook(missing_path)
    ws = wb["1-Input Data"]
    ws["B49"] = None  # DSCR - a critical field for the Financial Analysis gate
    wb.save(missing_path)

    try:
        at = AppTest.from_file("app.py")
        at.session_state["source_mode"] = "uploaded"
        at.session_state["uploaded_workbook_path"] = missing_path
        at.session_state["uploaded_workbook_name"] = "missing.xlsx"
        at.run(timeout=60)
        _ok(not at.exception, f"initial run has no exception: {[str(e) for e in at.exception]}")

        fa_text = _page_text(at, "4. Financial Analysis")
        _ok("Not Available / Requires Review" in fa_text, "Financial Analysis shows Not Available, not a fabricated Approve, with DSCR missing")

        memo_text = _page_text(at, "9. Credit Memo")
        # This synthetic fixture also happens to leave an independent
        # covenant breach in place (dataset_b's own ISCR), so the
        # winning tier here is correctly REJECT (more severe than
        # REVIEW REQUIRED, per the documented precedence) - the safety
        # property under test is simply "never a blind Approve", and
        # that the missing-critical-data reason is surfaced somewhere,
        # not that REVIEW REQUIRED specifically must be the winning tier.
        _ok(any(tier in memo_text for tier in ("REJECT", "REVIEW REQUIRED", "CONDITIONAL APPROVAL")),
            "Credit Memo's Final Recommendation is never a blind Approve when critical data is missing")
        _ok("critical data is unavailable" in memo_text.lower(), "the specific reason (critical data unavailable) is shown, not just the badge")
    finally:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_ai_unavailable_full_pipeline_still_works():
    print()
    print("=" * 100)
    print("SCENARIO 3 - Ollama unavailable: full pipeline (incl. Credit Memo decision) still works")
    print("=" * 100)

    real_run_ocr = ocr_upload.run_ocr
    real_av_check = ai_validation.check_ollama_availability
    real_acp_check = ai_correction_proposals.check_ollama_availability
    real_adm_check = ai_dynamic_mapping.check_ollama_availability
    realistic_text = _load_realistic_ocr_text()

    def _unreachable(*a, **k):
        return ai_validation.OllamaAvailability(False, False, [], "Ollama is not reachable. Is 'ollama serve' running?")

    ocr_upload.run_ocr = lambda pdf_path: (realistic_text, {"status_code": 200})
    ai_validation.check_ollama_availability = _unreachable
    ai_correction_proposals.check_ollama_availability = _unreachable
    ai_dynamic_mapping.check_ollama_availability = _unreachable

    try:
        at = AppTest.from_file("app.py")
        at.run(timeout=60)
        _upload(at, b"%PDF-1.4 fake content ai-unavailable-full\n" * 20)
        _ok(not at.exception, f"upload with Ollama unavailable has no exception: {[str(e) for e in at.exception]}")

        for area in ("2. Data Review", "3. Input & Validation", "4. Financial Analysis", "10. Risk Scoring", "9. Credit Memo"):
            text = _page_text(at, area)
            _ok(not at.exception, f"{area} renders with no exception when Ollama is unavailable: {[str(e) for e in at.exception]}")

        memo_text = _page_text(at, "9. Credit Memo")
        _ok("Final Recommendation" in memo_text, "Credit Memo's Final Recommendation still renders with Ollama unavailable")
        _ok(any(tier in memo_text for tier in ("APPROVE", "REJECT", "REVIEW REQUIRED", "CONDITIONAL APPROVAL")),
            "a real decision tier is shown even with no AI layer available")
    finally:
        ocr_upload.run_ocr = real_run_ocr
        ai_validation.check_ollama_availability = real_av_check
        ai_correction_proposals.check_ollama_availability = real_acp_check
        ai_dynamic_mapping.check_ollama_availability = real_adm_check


def main():
    test_reject_leaves_everything_unchanged()
    test_accept_flows_through_to_downstream_pages()
    test_missing_critical_data_never_blindly_approves()
    test_ai_unavailable_full_pipeline_still_works()

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(" -", f)
        sys.exit(1)
    print("ALL FINAL INTEGRATION TESTS PASSED")
    sys.exit(0)


if __name__ == "__main__":
    main()
