"""
Regression tests for the three Critical fixes from the workflow/UI review:

  1. continuous_monitoring.py - the overall-status gate used to compare
     each trigger's status against the literal string "NOT PASS", which
     no trigger can ever produce (they're "PASS"/"FAIL"/"WATCH LIST"/
     "NOT MONITORED"/not-available), so it always evaluated to "PASS"
     regardless of any real FAIL or WATCH LIST. Fixed by
     _aggregate_overall_status(), which applies explicit precedence
     across the five distinct trigger states instead.

  2. collateral_assessment.py - the coverage-rating band comparison
     compared a raw ratio (e.g. 2.05 for ~205% coverage) against
     percentage-shaped thresholds (125, 100), so a realistic ratio
     (never >100) always fell into the "<100" band regardless of actual
     coverage strength. Fixed by comparing against same-unit thresholds
     (1.25 / 1.00) instead.

  3. covenants.py - the overall covenant status excluded any covenant
     with no reported figure from the gate entirely, so a document
     missing (say) ISCR but passing DSCR still showed a clean overall
     "PASS" - compliance was never actually confirmed for the missing
     one. Fixed so a missing required covenant blocks a clean PASS
     (a breach still takes precedence over a gap).

Each is tested directly against the module's own build_*() function with
hand-built inputs - these are pure functions with no PDF/workbook
dependency, so no OCR text or reference workbook is needed.

Run: python test_credit_module_fixes.py
"""

import sys
from types import SimpleNamespace

from continuous_monitoring import MonitoringPolicy, build_continuous_monitoring
from covenants import CovenantPolicy, build_covenants
from collateral_assessment import CollateralInputs, build_collateral_assessment
from financial_analysis import MetricRow

_FAILURES = []


def _ok(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        _FAILURES.append(msg)


# ============================================================
# Fix 1 - Continuous Monitoring overall-status aggregation
# ============================================================

_MON_POLICY = MonitoringPolicy(
    revenue_decline_threshold=-0.10,
    total_debt_increase_threshold=0.15,
    dscr_deterioration_threshold=-0.35,
    external_rating_note="No Change",
    adverse_news_note="None",
)


def test_continuous_monitoring_overall_status():
    print("=" * 100)
    print("FIX 1 - CONTINUOUS MONITORING: overall status reflects FAIL/WATCH LIST")
    print("=" * 100)

    # All clean (revenue up, debt flat, DSCR up, no covenant breach).
    result = build_continuous_monitoring(_MON_POLICY, 0.05, 0.02, 0.10, covenant_rag_breach=False)
    _ok(result.overall_status == "PASS", f"All triggers clean -> overall PASS, got {result.overall_status!r}")

    # Revenue collapses (-20%, breaches the -10% floor) -> a real FAIL
    # must now surface at the overall level (the bug: it never could).
    result = build_continuous_monitoring(_MON_POLICY, -0.20, 0.02, 0.10, covenant_rag_breach=False)
    _ok(result.overall_status == "NOT PASS",
        f"Revenue decline trigger FAILs -> overall NOT PASS, got {result.overall_status!r}")

    # No FAIL, but a covenant is Amber/Red -> WATCH LIST, distinct from a
    # full breach and distinct from PASS.
    result = build_continuous_monitoring(_MON_POLICY, 0.05, 0.02, 0.10, covenant_rag_breach=True)
    _ok(result.overall_status == "WATCH LIST",
        f"Covenant breach only (no FAIL) -> overall WATCH LIST, got {result.overall_status!r}")

    # Both a FAIL and a WATCH LIST present -> FAIL takes precedence.
    result = build_continuous_monitoring(_MON_POLICY, -0.20, 0.02, 0.10, covenant_rag_breach=True)
    _ok(result.overall_status == "NOT PASS",
        f"FAIL + WATCH LIST together -> overall NOT PASS (FAIL wins), got {result.overall_status!r}")

    # DSCR %-change genuinely unavailable (this document doesn't disclose
    # DSCR) - must not silently read as PASS just because the OTHER
    # triggers are clean.
    result = build_continuous_monitoring(_MON_POLICY, 0.05, 0.02, None, covenant_rag_breach=False)
    _ok(result.overall_status == "Not Available / Requires Review",
        f"DSCR trigger unavailable, others clean -> overall NOT AVAILABLE, got {result.overall_status!r}")

    # NOT MONITORED triggers (external rating, adverse news) never affect
    # the overall status by themselves - confirmed by the "all clean"
    # case above already resolving to PASS despite both always being
    # NOT MONITORED.
    result = build_continuous_monitoring(_MON_POLICY, 0.05, 0.02, 0.10, covenant_rag_breach=False)
    statuses = {t.label: t.status for t in result.triggers}
    _ok(statuses["External rating downgrade"] == "NOT MONITORED" and statuses["Adverse news / qualitative disclosure"] == "NOT MONITORED",
        "External rating / adverse news triggers are their own distinct NOT MONITORED state")


# ============================================================
# Fix 2 - Collateral coverage ratio unit mismatch
# ============================================================

_COLLATERAL_BANDS = [
    (">125", "Strong coverage", "Strong"),
    ("100-125", "Adequate coverage", "Adequate"),
    ("<100", "Weak coverage", "Weak"),
]


def _collateral_inputs(book_value):
    return CollateralInputs(
        assets_raw=[("Plant & Machinery", book_value, 0.0, "Medium", "First charge", "Reviewed", "Primary")],
        rating_bands=_COLLATERAL_BANDS,
        typical_haircuts=[],
    )


def test_collateral_coverage_rating():
    print()
    print("=" * 100)
    print("FIX 2 - COLLATERAL ASSESSMENT: coverage rating uses a consistent unit")
    print("=" * 100)

    # ~205% coverage (book value 205 vs debt 100, 0% haircut) - the exact
    # real case (Tata Steel's own filing) that used to always show
    # "Weak"/"High collateral risk" regardless of actual strength.
    result = build_collateral_assessment(_collateral_inputs(205.0), debt_exposure=100.0)
    _ok(abs(result.coverage_ratio - 2.05) < 1e-9, f"Coverage ratio is still the raw multiple (2.05), got {result.coverage_ratio}")
    _ok(result.rating == "Strong", f"~205% coverage rates 'Strong', not 'Weak' - got {result.rating!r}")

    # ~110% coverage - the middle band.
    result = build_collateral_assessment(_collateral_inputs(110.0), debt_exposure=100.0)
    _ok(result.rating == "Adequate", f"~110% coverage rates 'Adequate', got {result.rating!r}")

    # ~80% coverage - genuinely weak, still correctly bottom-banded.
    result = build_collateral_assessment(_collateral_inputs(80.0), debt_exposure=100.0)
    _ok(result.rating == "Weak", f"~80% coverage rates 'Weak', got {result.rating!r}")

    # Boundary cases, in the same unit as coverage_ratio (1.25 / 1.00).
    result = build_collateral_assessment(_collateral_inputs(125.0), debt_exposure=100.0)
    _ok(result.rating == "Adequate", f"Exactly 125% coverage is NOT > 1.25x, so 'Adequate' (not 'Strong') - got {result.rating!r}")
    result = build_collateral_assessment(_collateral_inputs(100.0), debt_exposure=100.0)
    _ok(result.rating == "Adequate", f"Exactly 100% coverage is >= 1.00x, so 'Adequate' - got {result.rating!r}")


# ============================================================
# Fix 3 - Covenant Monitoring partial-availability gate
# ============================================================

def _ratio_row(key, current):
    return MetricRow(key=key, label=key.upper(), current=current, previous=current,
                      pct_change=0.0, threshold=None, status=None, source="test")


_COV_POLICY = CovenantPolicy(dscr_minimum=1.5, iscr_minimum=3.0)


def test_covenant_partial_availability():
    print()
    print("=" * 100)
    print("FIX 3 - COVENANT MONITORING: a missing required covenant cannot yield a clean PASS")
    print("=" * 100)

    # Both available and comfortably within limits -> clean PASS.
    fa = SimpleNamespace(ratio_rows=[_ratio_row("dscr", 2.0), _ratio_row("iscr", 4.0)])
    result = build_covenants(fa, _COV_POLICY)
    _ok(result.overall_status == "PASS", f"DSCR and ISCR both available and healthy -> PASS, got {result.overall_status!r}")

    # ISCR missing entirely (not every filing discloses it), DSCR healthy
    # - the exact reported bug: this used to still show "PASS".
    fa = SimpleNamespace(ratio_rows=[_ratio_row("dscr", 2.0), _ratio_row("iscr", None)])
    result = build_covenants(fa, _COV_POLICY)
    _ok(result.overall_status == "Not Available / Requires Review",
        f"DSCR healthy but ISCR not disclosed -> NOT AVAILABLE, not a bare PASS - got {result.overall_status!r}")
    _ok(result.overall_status != "PASS", "Confirms the specific reported bug (missing ISCR silently reading as PASS) is gone")

    # Both missing -> unchanged existing behaviour (NOT AVAILABLE).
    fa = SimpleNamespace(ratio_rows=[_ratio_row("dscr", None), _ratio_row("iscr", None)])
    result = build_covenants(fa, _COV_POLICY)
    _ok(result.overall_status == "Not Available / Requires Review",
        f"Both covenants missing -> NOT AVAILABLE (unchanged), got {result.overall_status!r}")

    # DSCR breaches (below policy minimum) while ISCR is also missing -
    # the breach must still surface as NOT PASS, not be masked by the gap.
    fa = SimpleNamespace(ratio_rows=[_ratio_row("dscr", 1.0), _ratio_row("iscr", None)])
    result = build_covenants(fa, _COV_POLICY)
    _ok(result.overall_status == "NOT PASS",
        f"DSCR breach + ISCR missing -> NOT PASS (breach takes precedence over the gap), got {result.overall_status!r}")


def main():
    test_continuous_monitoring_overall_status()
    test_collateral_coverage_rating()
    test_covenant_partial_availability()

    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} FAILURE(S):")
        for f in _FAILURES:
            print(" -", f)
        return 1
    print("ALL CREDIT MODULE FIX TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
