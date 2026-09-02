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


# Every formula below takes raw extracted fields, any of which can
# legitimately be None for a document whose OCR text doesn't disclose (or
# this pipeline can't locate) that specific line - a real, common outcome
# for a genuinely different filing, not just a Tata Steel-shaped one. These
# three helpers are the ONLY arithmetic used in this module from here on:
# each returns None (never 0, never a guess) the moment any operand is
# unavailable, instead of letting `None + float` / `None / float` reach
# Python and crash the whole app. A None here always means "not
# derivable from what this document supplied" and must render as
# "Not Available / Requires Review", never a fabricated number.
def _add(*values):
    return sum(values) if all(v is not None for v in values) else None


def _sub(a, b):
    return a - b if a is not None and b is not None else None


def _div(a, b):
    return a / b if a is not None and b not in (None, 0) else None


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
        _add(d.revenue.current, d.other_income.current),
        _add(d.revenue.previous, d.other_income.previous),
    )
    pbt_before_exceptional = YearFigure(
        _sub(total_income.current, d.total_expenses.current),
        _sub(total_income.previous, d.total_expenses.previous),
    )
    pbt = YearFigure(
        _add(pbt_before_exceptional.current, d.exceptional_items.current),
        _add(pbt_before_exceptional.previous, d.exceptional_items.previous),
    )
    net_profit = YearFigure(
        _sub(pbt.current, d.total_tax_expense.current),
        _sub(pbt.previous, d.total_tax_expense.previous),
    )
    net_finance_charges = YearFigure(
        _sub(d.finance_costs.current, d.interest_income.current),
        _sub(d.finance_costs.previous, d.interest_income.previous),
    )
    ebitda = YearFigure(
        _add(pbt_before_exceptional.current, net_finance_charges.current, d.depreciation.current),
        _add(pbt_before_exceptional.previous, net_finance_charges.previous, d.depreciation.previous),
    )
    ebitda_margin = YearFigure(
        _div(ebitda.current, d.revenue.current),
        _div(ebitda.previous, d.revenue.previous),
    )
    # Reference workbook row 20 is labelled "EBITDA (company definition...)"
    # but its formula (B20=B12+B17, i.e. PBT-before-exceptional + Net
    # finance charges, with depreciation NOT added back) computes EBIT, not
    # EBITDA - and DSCR/ISCR downstream are explicitly documented as
    # EBIT-based. Replicated per the formula, not the row label.
    ebit = YearFigure(
        _add(pbt_before_exceptional.current, net_finance_charges.current),
        _add(pbt_before_exceptional.previous, net_finance_charges.previous),
    )
    total_debt = YearFigure(
        _add(d.borrowings_non_current.current, d.borrowings_current.current,
             d.lease_liabilities_non_current.current, d.lease_liabilities_current.current),
        _add(d.borrowings_non_current.previous, d.borrowings_current.previous,
             d.lease_liabilities_non_current.previous, d.lease_liabilities_current.previous),
    )
    net_debt = YearFigure(
        _sub(_sub(_sub(total_debt.current, d.current_investments.current), d.cash.current), d.other_bank_balances.current),
        _sub(_sub(_sub(total_debt.previous, d.current_investments.previous), d.cash.previous), d.other_bank_balances.previous),
    )
    average_equity_current = _div(_add(d.total_equity.current, d.total_equity.previous), 2)
    # C59 in the reference reconstructs the prior-prior year's closing
    # equity from the FY-previous roll-forward (Equity_open = Equity_close -
    # TCI + Dividends), since only two years of balance-sheet data are
    # captured in Input Data.
    reconstructed_prior_prior_equity = _sub(
        _add(d.total_equity.previous, d.dividend_paid.previous), d.total_comprehensive_income.previous
    )
    average_equity_previous = _div(_add(d.total_equity.previous, reconstructed_prior_prior_equity), 2)
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
        _div(m.ebit.current, _add(m.net_finance_charges.current, d.repayment_borrowings.current, d.lease_payment.current)),
        _div(m.ebit.previous, _add(m.net_finance_charges.previous, d.repayment_borrowings.previous, d.lease_payment.previous)),
    )
    iscr = YearFigure(
        _div(m.ebit.current, m.net_finance_charges.current),
        _div(m.ebit.previous, m.net_finance_charges.previous),
    )
    current_ratio = YearFigure(
        _div(d.current_assets.current, _sub(d.current_liabilities.current, d.lease_liabilities_current.current)),
        _div(d.current_assets.previous, _sub(d.current_liabilities.previous, d.lease_liabilities_current.previous)),
    )
    debt_equity = YearFigure(
        _div(m.net_debt.current, m.average_equity.current),
        _div(m.net_debt.previous, m.average_equity.previous),
    )
    debt_assets = YearFigure(
        _div(m.total_debt.current, d.total_assets.current),
        _div(m.total_debt.previous, d.total_assets.previous),
    )
    net_profit_margin = YearFigure(
        _div(m.net_profit.current, d.revenue.current),
        _div(m.net_profit.previous, d.revenue.previous),
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
    # `ok` is None when a required operand was itself None (this
    # document's OCR text didn't disclose/couldn't locate it) - "N/A", not
    # a guessed PASS or FAIL.
    if ok is None:
        return ValidationCheck(cell, label, "N/A - required input not available")
    return ValidationCheck(cell, label, "PASS" if ok else "FAIL")


def _within_tolerance(a, b, tol):
    diff = _sub(a, b)
    return None if diff is None else abs(diff) <= tol


# ============================================================
# DATA COMPLETENESS
#
# The "Input & Validation" page used to show a hardcoded "100% - All
# required fields loaded" KPI regardless of what this document's own OCR
# text actually disclosed - the completeness/false-100% audit's central
# finding. This replaces it with a genuine count: which of this
# application's downstream-critical fields are actually present in `d`
# (from wherever it came - live OCR extraction or the reference
# workbook), split into the two tiers the audit calls for.
#
# CRITICAL_FIELDS are the ones Financial Analysis's own Approve/Reject
# gate (see financial_analysis.py) treats as required for the decision,
# plus the handful of raw Balance Sheet fields every one of this
# application's modules (Financial Analysis, Covenant Monitoring,
# Collateral Assessment, Limit Setting, Risk Scoring) reads directly.
# IMPORTANT_FIELDS still feed real calculations (EBITDA margin, net
# finance charges, net debt) but a document missing one of them still
# yields a usable - if narrower - analysis, not a blocked decision.
# ============================================================

@dataclass(frozen=True)
class FieldCompleteness:
    label: str
    tier: str          # "critical" or "important"
    available: bool


@dataclass(frozen=True)
class DataCompletenessResult:
    fields: list
    critical_available: int
    critical_total: int
    important_available: int
    important_total: int
    overall_pct: float          # weighted: critical fields count double
    missing_critical: list
    missing_important: list


# Weight applied to a critical field's contribution to overall_pct,
# relative to an important field's weight of 1 - a document missing a
# critical figure should visibly cost more completeness than one missing
# a supplementary line, not be averaged away against 13 other present
# fields.
_CRITICAL_WEIGHT = 2


def compute_data_completeness(d: ReferenceInputData, m: DerivedInputMetrics) -> DataCompletenessResult:

    critical_checks = [
        ("Revenue", d.revenue.current is not None),
        ("Total Income", m.total_income.current is not None),
        ("Net Profit", m.net_profit.current is not None),
        ("Total Assets", d.total_assets.current is not None),
        ("Total Equity", d.total_equity.current is not None),
        ("Total Debt / Borrowings", m.total_debt.current is not None),
        ("Current Assets", d.current_assets.current is not None),
        ("Current Liabilities", d.current_liabilities.current is not None),
        ("Operating Cash Flow", d.operating_cash_flow.current is not None),
    ]

    important_checks = [
        ("Finance Costs", d.finance_costs.current is not None),
        ("Depreciation", d.depreciation.current is not None),
        ("Other Income", d.other_income.current is not None),
        ("Cash", d.cash.current is not None),
        ("Current Investments", d.current_investments.current is not None),
    ]

    fields = (
        [FieldCompleteness(label, "critical", ok) for label, ok in critical_checks]
        + [FieldCompleteness(label, "important", ok) for label, ok in important_checks]
    )

    critical_available = sum(1 for _, ok in critical_checks if ok)
    important_available = sum(1 for _, ok in important_checks if ok)

    weighted_available = _CRITICAL_WEIGHT * critical_available + important_available
    weighted_total = _CRITICAL_WEIGHT * len(critical_checks) + len(important_checks)
    overall_pct = (weighted_available / weighted_total) * 100 if weighted_total else 0.0

    return DataCompletenessResult(
        fields=fields,
        critical_available=critical_available,
        critical_total=len(critical_checks),
        important_available=important_available,
        important_total=len(important_checks),
        overall_pct=overall_pct,
        missing_critical=[label for label, ok in critical_checks if not ok],
        missing_important=[label for label, ok in important_checks if not ok],
    )


def run_validation_checks(d: ReferenceInputData, m: DerivedInputMetrics) -> list:
    """Replicates '1-Input Data' rows 62-73 exactly - the nine checks that
    exist in the reference workbook. No additional checks are invented."""
    checks = []

    checks.append(_pass_fail(
        "B63", "P&L internal consistency: derived PBT-before-exceptional vs reported",
        _within_tolerance(m.pbt_before_exceptional.current, d.reported_pbt_before_exceptional_current, PNL_TIE_TOLERANCE),
    ))
    checks.append(_pass_fail(
        "B64", "P&L internal consistency: derived net profit vs reported",
        _within_tolerance(m.net_profit.current, d.reported_net_profit_current, PNL_TIE_TOLERANCE),
    ))
    _bs_identity_diff = _sub(d.total_assets.current, _add(d.total_equity.current, d.total_liabilities.current))
    _bs_identity_ok = (
        None if _bs_identity_diff is None or d.total_assets.current is None
        else abs(_bs_identity_diff) <= BALANCE_SHEET_TOLERANCE_PCT * d.total_assets.current
    )
    checks.append(_pass_fail(
        "B65", "Balance sheet identity: Assets = Equity + Total liabilities (±1%)",
        _bs_identity_ok,
    ))
    if d.operating_cash_flow.current is None or d.operating_cash_flow.previous is None:
        checks.append(ValidationCheck(
            "B66", "Operating cash flow positive in both years",
            "N/A - required input not available",
        ))
    else:
        ocf_ok = d.operating_cash_flow.current > 0 and d.operating_cash_flow.previous > 0
        checks.append(ValidationCheck(
            "B66", "Operating cash flow positive in both years",
            "PASS" if ocf_ok else "FLAG FOR REVIEW",
        ))

    c = compute_calculated_ratios(d, m)

    def tie(cell, label, calc, reported, tol):
        # `reported` is this document's own Reported Ratios disclosure -
        # not every filing includes it (Debt/Assets, Debt-Equity, etc.),
        # same as `calc` being unavailable when a raw component is missing.
        if calc is None or reported is None:
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
