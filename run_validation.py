"""
Standalone validation runner: loads the reference dataset, computes every
module (1-Input Data through 9-Credit Memo), and prints every discrepancy
against the reference workbook's own cached values.

Run with: python run_validation.py
"""

from reference_data import load_reference_bundle
from input_data_engine import compute_derived_metrics, compute_calculated_ratios, run_validation_checks
from financial_analysis import build_financial_analysis
from risk_scoring import build_risk_scoring
from validation import (
    validate_input_data_engine,
    validate_input_checks,
    validate_financial_analysis,
    validate_risk_scoring,
)

from covenants import load_covenant_policy, build_covenants
from collateral_assessment import load_collateral_inputs, build_collateral_assessment
from limit_setting import load_limit_setting_policy, build_limit_setting
from continuous_monitoring import load_monitoring_policy, build_continuous_monitoring
from portfolio_monitoring import load_portfolio_inputs, build_portfolio_monitoring
from credit_memo import load_credit_memo_commentary, build_credit_memo
from validation_extended import (
    validate_covenants,
    validate_collateral_assessment,
    validate_limit_setting,
    validate_continuous_monitoring,
    validate_portfolio_monitoring,
    validate_credit_memo,
)


def _print_rows(title, rows):
    print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")
    print(f"{'Metric':<65}{'Calculated':>14}{'Reference':>14}{'Status':>15}")
    discrepancies = 0
    not_comparable = 0
    for r in rows:
        calc = r.calculated if isinstance(r.calculated, str) else (f"{r.calculated:.4f}" if r.calculated is not None else "-")
        ref = r.reference if isinstance(r.reference, str) else (f"{r.reference:.4f}" if r.reference is not None else "-")
        print(f"{r.metric:<65}{str(calc):>14}{str(ref):>14}{r.status:>15}")
        if r.status == "DISCREPANCY":
            discrepancies += 1
        elif r.status.startswith("NOT COMPARABLE"):
            not_comparable += 1
    matches = len(rows) - discrepancies - not_comparable
    print(f"\n{len(rows)} rows checked - {discrepancies} discrepancies, {not_comparable} not comparable, {matches} matches.")
    return discrepancies, not_comparable


def main():
    bundle = load_reference_bundle()
    d = bundle.input_data

    # ---- Modules 1-3 (frozen) --------------------------------------
    m = compute_derived_metrics(d)
    c = compute_calculated_ratios(d, m)
    checks = run_validation_checks(d, m)
    fa = build_financial_analysis(bundle)
    risk = build_risk_scoring(bundle)

    # ---- Modules 4-9 -------------------------------------------------
    cov_policy = load_covenant_policy(bundle.workbook_path)
    covenants = build_covenants(fa, cov_policy)

    coll_inputs = load_collateral_inputs(bundle.workbook_path)
    collateral = build_collateral_assessment(coll_inputs, fa.derived.total_debt.current)

    limit_policy = load_limit_setting_policy(bundle.workbook_path)
    limit = build_limit_setting(limit_policy, fa.derived.ebit.current, d.reported_dscr.current)

    mon_policy = load_monitoring_policy(bundle.workbook_path)
    rows_by_key = {r.key: r for r in fa.income_statement_rows + fa.ratio_rows}
    breach = any(r.rag in ("AMBER", "RED") for r in covenants.rows)
    monitoring = build_continuous_monitoring(
        mon_policy, rows_by_key["revenue"].pct_change, rows_by_key["total_debt"].pct_change,
        rows_by_key["dscr"].pct_change, breach,
    )

    portfolio_inputs = load_portfolio_inputs(bundle.workbook_path)
    portfolio = build_portfolio_monitoring(portfolio_inputs)

    commentary = load_credit_memo_commentary(bundle.workbook_path)
    memo = build_credit_memo(fa, risk, covenants, collateral, limit, monitoring, portfolio, commentary)

    total_discrepancies = 0
    total_not_comparable = 0

    for title, rows in [
        ("1. INPUT DATA ENGINE (formula transcription check)", validate_input_data_engine(d, m, c, bundle.workbook_path)),
        ("1. INPUT DATA VALIDATION CHECKS (rows 62-73)", validate_input_checks(checks, bundle.workbook_path)),
        ("2. FINANCIAL ANALYSIS", validate_financial_analysis(fa, bundle.workbook_path)),
        ("3. RISK SCORING", validate_risk_scoring(risk, bundle.workbook_path)),
        ("4. COVENANTS", validate_covenants(covenants, bundle.workbook_path)),
        ("5. COLLATERAL ASSESSMENT", validate_collateral_assessment(collateral, bundle.workbook_path)),
        ("6. LIMIT SETTING", validate_limit_setting(limit, bundle.workbook_path)),
        ("7. CONTINUOUS MONITORING", validate_continuous_monitoring(monitoring, bundle.workbook_path)),
        ("8. PORTFOLIO MONITORING", validate_portfolio_monitoring(portfolio, bundle.workbook_path)),
        ("9. CREDIT MEMO", validate_credit_memo(memo, bundle.workbook_path)),
    ]:
        disc, nc = _print_rows(title, rows)
        total_discrepancies += disc
        total_not_comparable += nc

    print(f"\n{'#' * 100}")
    print(f"TOTAL: {total_discrepancies} discrepancies, {total_not_comparable} not comparable across all validation layers.")
    print(f"{'#' * 100}")

    print("\n--- Regression spot-check (must match the frozen baseline) ---")
    print(f"Risk Score = {risk.total_weighted_score}")
    print(f"Risk Grade = {risk.risk_grade}")
    print(f"Financial Strength = {risk.financial_strength_score}")
    print(f"Overall Financial Analysis status = {fa.overall_status}")
    check_pass = sum(1 for ck in checks if ck.result in ("PASS", "PASS - ties"))
    print(f"Input Validation checks = {check_pass}/{len(checks)}")


if __name__ == "__main__":
    main()
