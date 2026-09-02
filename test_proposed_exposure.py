"""
Unit tests for proposed_exposure.py - the exposure builder and
reconciliation check are pure functions with no workbook/PDF dependency,
so these run directly against build_proposed_exposure() /
check_exposure_reconciliation() with hand-built inputs.

Run: python test_proposed_exposure.py
"""

import sys

from proposed_exposure import build_proposed_exposure, check_exposure_reconciliation

_FAILURES = []


def _ok(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        _FAILURES.append(msg)


def test_normal_case_no_override():
    print("=" * 100)
    print("Normal case: no sanctioned override, everything available")
    print("=" * 100)
    exposure = build_proposed_exposure(
        requested_amount=5000.0, recommended_limit=4000.0, sanctioned_override=None,
        fb_requirement=1000.0, nfb_commitment=500.0,
    )
    _ok(exposure.requested_amount == 5000.0, "requested_amount passes through unchanged")
    _ok(exposure.recommended_limit == 4000.0, "recommended_limit passes through unchanged")
    _ok(exposure.sanctioned_amount == 4000.0, "sanctioned_amount defaults to recommended_limit when no override")
    _ok(exposure.term_facility_exposure == 4000.0, "term_facility_exposure == sanctioned_amount")
    _ok(exposure.proposed_funded_exposure == 5000.0, "proposed_funded_exposure = term (4000) + FB (1000) = 5000")
    _ok(exposure.nfb_commitment == 500.0, "nfb_commitment kept as its own figure, not merged into funded")
    _ok(exposure.total_proposed_commitment == 5500.0, "total_proposed_commitment = funded (5000) + NFB (500) = 5500")


def test_sanctioned_override():
    print()
    print("=" * 100)
    print("Sanctioned override: committee approves a DIFFERENT amount than recommended")
    print("=" * 100)
    exposure = build_proposed_exposure(
        requested_amount=5000.0, recommended_limit=4000.0, sanctioned_override=3000.0,
        fb_requirement=1000.0, nfb_commitment=500.0,
    )
    _ok(exposure.recommended_limit == 4000.0, "recommended_limit is untouched by the override")
    _ok(exposure.sanctioned_amount == 3000.0, "sanctioned_amount uses the override, not recommended_limit")
    _ok(exposure.term_facility_exposure == 3000.0, "term_facility_exposure follows the override")
    _ok(exposure.proposed_funded_exposure == 4000.0, "proposed_funded_exposure recomputes from the override (3000 + 1000)")
    _ok(exposure.total_proposed_commitment == 4500.0, "total_proposed_commitment recomputes end to end (4000 + 500)")


def test_missing_term_facility():
    print()
    print("=" * 100)
    print("Term facility unavailable (EBIT/reported DSCR missing) - never guessed as 0")
    print("=" * 100)
    exposure = build_proposed_exposure(
        requested_amount=5000.0, recommended_limit=None, sanctioned_override=None,
        fb_requirement=1000.0, nfb_commitment=500.0,
    )
    _ok(exposure.sanctioned_amount is None, "sanctioned_amount is None, not 0, when recommended_limit is None and no override given")
    _ok(exposure.term_facility_exposure is None, "term_facility_exposure is None")
    _ok(exposure.proposed_funded_exposure is None,
        "proposed_funded_exposure is None (never silently just 'FB alone') when the term piece is missing")
    _ok(exposure.total_proposed_commitment is None, "total_proposed_commitment is None - the whole chain withholds rather than guesses")
    _ok(exposure.fb_requirement == 1000.0, "FB Requirement itself is still shown - it doesn't depend on the term facility")
    _ok(exposure.nfb_commitment == 500.0, "NFB Commitment itself is still shown - it doesn't depend on the term facility either")


def test_missing_fb_and_nfb():
    print()
    print("=" * 100)
    print("FB/NFB not yet entered by the analyst - term facility alone must not be blocked")
    print("=" * 100)
    exposure = build_proposed_exposure(
        requested_amount=5000.0, recommended_limit=4000.0, sanctioned_override=None,
        fb_requirement=None, nfb_commitment=None,
    )
    _ok(exposure.term_facility_exposure == 4000.0, "term_facility_exposure is available on its own")
    _ok(exposure.proposed_funded_exposure is None, "proposed_funded_exposure is None when FB is not yet entered")
    _ok(exposure.total_proposed_commitment is None, "total_proposed_commitment is None when FB/NFB are not yet entered")


def test_reconciliation_matched():
    print()
    print("=" * 100)
    print("Reconciliation: PDF-mode wiring (identical by construction)")
    print("=" * 100)
    exposure = build_proposed_exposure(5000.0, 4000.0, None, 1000.0, 500.0)
    result = check_exposure_reconciliation(exposure, portfolio_proposed_loan_amount=5500.0)
    _ok(result.status == "Reconciled", f"Same figure on both sides -> Reconciled, got {result.status!r}")


def test_reconciliation_mismatched():
    print()
    print("=" * 100)
    print("Reconciliation: Advanced mode, independently-sourced figures disagree")
    print("=" * 100)
    exposure = build_proposed_exposure(5000.0, 4000.0, None, 1000.0, 500.0)
    result = check_exposure_reconciliation(exposure, portfolio_proposed_loan_amount=9000.0)
    _ok(result.status == "Not Reconciled", f"Different figures -> Not Reconciled, got {result.status!r}")
    _ok("5,500" in result.note and "9,000" in result.note, f"Note names both actual figures - got: {result.note!r}")


def test_reconciliation_unavailable():
    print()
    print("=" * 100)
    print("Reconciliation: cannot be evaluated when either side is missing")
    print("=" * 100)
    exposure = build_proposed_exposure(5000.0, None, None, 1000.0, 500.0)
    result = check_exposure_reconciliation(exposure, portfolio_proposed_loan_amount=5500.0)
    _ok(result.status == "Not Available", f"Total Proposed Commitment unavailable -> Not Available, got {result.status!r}")

    exposure2 = build_proposed_exposure(5000.0, 4000.0, None, 1000.0, 500.0)
    result2 = check_exposure_reconciliation(exposure2, portfolio_proposed_loan_amount=None)
    _ok(result2.status == "Not Available", f"Portfolio figure unavailable -> Not Available, got {result2.status!r}")


def main():
    test_normal_case_no_override()
    test_sanctioned_override()
    test_missing_term_facility()
    test_missing_fb_and_nfb()
    test_reconciliation_matched()
    test_reconciliation_mismatched()
    test_reconciliation_unavailable()

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(" -", f)
        return 1
    print("ALL PROPOSED EXPOSURE UNIT TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
