"""
Re-implements the formulas that live inside the '1-Input Data' sheet itself:
Total income, PBT, Net profit, EBITDA, EBIT, Total debt, Net debt, Average
equity, the Reported-vs-Calculated ratio reconciliation, and the sheet's own
automated validation checks (rows 62-73 in the reference workbook).

This is the layer the architecture review found missing between
'1-Input Data' and '2-Financial Analysis': every function here takes only
raw input-data figures (reference_data.ReferenceInputData) and derives a
result - nothing here is a constant copied from the reference workbook.
"""

from dataclasses import dataclass

from reference_data import ReferenceInputData, YearFigure

# Reconciliation tolerances, taken from the reference workbook's own
# validation-check formulas (rows 63-73 of '1-Input Data').
PNL_TIE_TOLERANCE = 1.0          # B63 / B64: abs(derived - reported) <= 1 (Rs crore)
BALANCE_SHEET_TOLERANCE_PCT = 0.01   # B65: abs(Assets - (Equity+Liabilities)) <= 1% of Assets
MARGIN_TIE_TOLERANCE = 0.0005    # B69 / B70 / B71: abs(calculated - reported) <= 0.0005
RATIO_TIE_TOLERANCE = 0.005      # B72 / B73: abs(calculated - reported) <= 0.005


@dataclass(frozen=True)
class DerivedInputMetrics:
    total_income: YearFigure                  # B7  = B5+B6
    pbt_before_exceptional: YearFigure         # B12 = B7-B8
    pbt: YearFigure                            # B14 = B12+B13
    net_profit: YearFigure                     # B16 = B14-B15
    net_finance_charges: YearFigure            # B17 = B9-B11
    ebitda: YearFigure                         # B18 = B12+B17+B10
    ebitda_margin: YearFigure                  # B19 = B18/B5   (fraction)
    ebit: YearFigure                           # B20 = B12+B17
    total_debt: YearFigure                     # B30 = SUM(B26:B29)
    net_debt: YearFigure                       # B34 = B30-B31-B32-B33
    average_equity: YearFigure                 # B59 / C59


@dataclass(frozen=True)
class CalculatedRatios:
    """Mirrors '1-Input Data'!C49:C56 (current year) and E49:E56 (previous
    year) - the sheet's own internal cross-check of the Reported Ratios
    block. This is NOT what '2-Financial Analysis' displays (see
    financial_analysis.py); it exists to reconcile the Reported figures
    against the raw P&L/BS/CF lines."""
    dscr: YearFigure
    iscr: YearFigure
    current_ratio: YearFigure
    debt_equity: YearFigure
    debt_assets: YearFigure
    ebitda_margin: YearFigure
    net_profit_margin: YearFigure
    net_worth: YearFigure


@dataclass(frozen=True)
class ValidationCheck:
    cell: str
    label: str
    result: str


def compute_derived_metrics(d: ReferenceInputData) -> DerivedInputMetrics:
    total_income = YearFigure(
        d.revenue.current + d.other_income.current,
        d.revenue.previous + d.other_income.previous,
    )
    pbt_before_exceptional = YearFigure(
        total_income.current - d.total_expenses.current,
        total_income.previous - d.total_expenses.previous,
    )
    pbt = YearFigure(
        pbt_before_exceptional.current + d.exceptional_items.current,
        pbt_before_exceptional.previous + d.exceptional_items.previous,
    )
    net_profit = YearFigure(
        pbt.current - d.total_tax_expense.current,
        pbt.previous - d.total_tax_expense.previous,
    )
    def _sub_or_none(a, b):
        return a - b if a is not None and b is not None else None

    net_finance_charges = YearFigure(
        _sub_or_none(d.finance_costs.current, d.interest_income.current),
        _sub_or_none(d.finance_costs.previous, d.interest_income.previous),
    )
    ebitda = YearFigure(
        pbt_before_exceptional.current + net_finance_charges.current + d.depreciation.current
        if net_finance_charges.current is not None else None,
        pbt_before_exceptional.previous + net_finance_charges.previous + d.depreciation.previous
        if net_finance_charges.previous is not None else None,
    )
    ebitda_margin = YearFigure(
        ebitda.current / d.revenue.current if ebitda.current is not None else None,
        ebitda.previous / d.revenue.previous if ebitda.previous is not None else None,
    )
    # Reference workbook row 20 is labelled "EBITDA (company definition...)"
    # but its formula (B20=B12+B17, i.e. PBT-before-exceptional + Net
    # finance charges, with depreciation NOT added back) computes EBIT, not
    # EBITDA - and DSCR/ISCR downstream are explicitly documented as
    # EBIT-based. Replicated per the formula, not the row label.
    ebit = YearFigure(
        pbt_before_exceptional.current + net_finance_charges.current
        if net_finance_charges.current is not None else None,
        pbt_before_exceptional.previous + net_finance_charges.previous
        if net_finance_charges.previous is not None else None,
    )
    total_debt = YearFigure(
        d.borrowings_non_current.current + d.borrowings_current.current
        + d.lease_liabilities_non_current.current + d.lease_liabilities_current.current,
        d.borrowings_non_current.previous + d.borrowings_current.previous
        + d.lease_liabilities_non_current.previous + d.lease_liabilities_current.previous,
    )
    net_debt = YearFigure(
        total_debt.current - d.current_investments.current - d.cash.current - d.other_bank_balances.current,
        total_debt.previous - d.current_investments.previous - d.cash.previous - d.other_bank_balances.previous,
    )
    average_equity_current = (d.total_equity.current + d.total_equity.previous) / 2
    # C59 in the reference reconstructs the prior-prior year's closing
    # equity from the FY-previous roll-forward (Equity_open = Equity_close -
    # TCI + Dividends), since only two years of balance-sheet data are
    # captured in Input Data.
    reconstructed_prior_prior_equity = (
        d.total_equity.previous + d.dividend_paid.previous - d.total_comprehensive_income.previous
        if d.total_comprehensive_income.previous is not None else None
    )
    average_equity_previous = (
        (d.total_equity.previous + reconstructed_prior_prior_equity) / 2
        if reconstructed_prior_prior_equity is not None else None
    )
    average_equity = YearFigure(average_equity_current, average_equity_previous)

    return DerivedInputMetrics(
        total_income=total_income,
        pbt_before_exceptional=pbt_before_exceptional,
        pbt=pbt,
        net_profit=net_profit,
        net_finance_charges=net_finance_charges,
        ebitda=ebitda,
        ebitda_margin=ebitda_margin,
        ebit=ebit,
        total_debt=total_debt,
        net_debt=net_debt,
        average_equity=average_equity,
    )


def compute_calculated_ratios(d: ReferenceInputData, m: DerivedInputMetrics) -> CalculatedRatios:
    dscr = YearFigure(
        m.ebit.current / (m.net_finance_charges.current + d.repayment_borrowings.current + d.lease_payment.current)
        if m.ebit.current is not None and m.net_finance_charges.current is not None else None,
        m.ebit.previous / (m.net_finance_charges.previous + d.repayment_borrowings.previous + d.lease_payment.previous)
        if m.ebit.previous is not None and m.net_finance_charges.previous is not None else None,
    )
    iscr = YearFigure(
        m.ebit.current / m.net_finance_charges.current
        if m.ebit.current is not None and m.net_finance_charges.current is not None else None,
        m.ebit.previous / m.net_finance_charges.previous
        if m.ebit.previous is not None and m.net_finance_charges.previous is not None else None,
    )
    current_ratio = YearFigure(
        d.current_assets.current / (d.current_liabilities.current - d.lease_liabilities_current.current),
        d.current_assets.previous / (d.current_liabilities.previous - d.lease_liabilities_current.previous),
    )
    debt_equity = YearFigure(
        m.net_debt.current / m.average_equity.current if m.average_equity.current is not None else None,
        m.net_debt.previous / m.average_equity.previous if m.average_equity.previous is not None else None,
    )
    debt_assets = YearFigure(
        m.total_debt.current / d.total_assets.current,
        m.total_debt.previous / d.total_assets.previous,
    )
    net_profit_margin = YearFigure(
        m.net_profit.current / d.revenue.current,
        m.net_profit.previous / d.revenue.previous,
    )
    net_worth = YearFigure(d.total_equity.current, d.total_equity.previous)

    return CalculatedRatios(
        dscr=dscr,
        iscr=iscr,
        current_ratio=current_ratio,
        debt_equity=debt_equity,
        debt_assets=debt_assets,
        ebitda_margin=m.ebitda_margin,
        net_profit_margin=net_profit_margin,
        net_worth=net_worth,
    )


def _pass_fail(cell, label, ok):
    return ValidationCheck(cell, label, "PASS" if ok else "FAIL")


def run_validation_checks(d: ReferenceInputData, m: DerivedInputMetrics) -> list:
    """Replicates '1-Input Data' rows 62-73 exactly - the nine checks that
    exist in the reference workbook. No additional checks are invented."""
    checks = []

    checks.append(_pass_fail(
        "B63", "P&L internal consistency: derived PBT-before-exceptional vs reported",
        abs(m.pbt_before_exceptional.current - d.reported_pbt_before_exceptional_current) <= PNL_TIE_TOLERANCE,
    ))
    checks.append(_pass_fail(
        "B64", "P&L internal consistency: derived net profit vs reported",
        abs(m.net_profit.current - d.reported_net_profit_current) <= PNL_TIE_TOLERANCE,
    ))
    checks.append(_pass_fail(
        "B65", "Balance sheet identity: Assets = Equity + Total liabilities (±1%)",
        abs(d.total_assets.current - (d.total_equity.current + d.total_liabilities.current))
        <= BALANCE_SHEET_TOLERANCE_PCT * d.total_assets.current,
    ))
    ocf_ok = d.operating_cash_flow.current > 0 and d.operating_cash_flow.previous > 0
    checks.append(ValidationCheck(
        "B66", "Operating cash flow positive in both years",
        "PASS" if ocf_ok else "FLAG FOR REVIEW",
    ))

    c = compute_calculated_ratios(d, m)

    def tie(cell, label, calc, reported, tol):
        if calc is None:
            checks.append(ValidationCheck(cell, label, "N/A - required input not available"))
            return
        checks.append(ValidationCheck(
            cell, label,
            "PASS - ties" if abs(calc - reported) <= tol else "CHECK",
        ))

    tie("B69", "Computed EBITDA margin = reported operating EBITDA margin (FY current)",
        m.ebitda_margin.current, d.reported_ebitda_margin.current, MARGIN_TIE_TOLERANCE)
    tie("B70", "Computed EBITDA margin = reported operating EBITDA margin (FY previous)",
        m.ebitda_margin.previous, d.reported_ebitda_margin.previous, MARGIN_TIE_TOLERANCE)
    tie("B71", "Computed net profit margin = reported net profit margin (FY current)",
        c.net_profit_margin.current, d.reported_net_profit_margin.current, MARGIN_TIE_TOLERANCE)
    tie("B72", "Computed total debts / total assets = reported (FY current)",
        c.debt_assets.current, d.reported_debt_assets.current, RATIO_TIE_TOLERANCE)
    tie("B73", "Computed net debt / average equity = reported (FY current)",
        c.debt_equity.current, d.reported_debt_equity.current, RATIO_TIE_TOLERANCE)

    return checks
