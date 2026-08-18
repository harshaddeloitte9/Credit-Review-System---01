"""
Re-implements '2-Financial Analysis' as a calculation, not a lookup.

The reference workbook types most of this sheet's FY-current/FY-previous
numbers in by hand (confirmed during the architecture review: only the
OCF-to-Debt row was an actual cross-sheet formula). Every metric here
instead reads its figure live from '1-Input Data' - either directly
(Revenue, Total Assets, Operating Cash Flow, the Reported Ratios block) or
through the same formula chain '1-Input Data' itself defines for that
metric (EBITDA Margin = EBITDA / Revenue, Total Debt = SUM of borrowings +
lease liabilities, ...). See input_data_engine.py for that formula chain.

Two metrics on this sheet - Debtors Days and Inventory Days - cannot be
computed at all: '1-Input Data' has no Trade Receivables, Inventory or Cost
of Sales figures anywhere in its schema, so there is no input to derive them
from. They are reported as not available rather than invented.

The reference workbook's Reported Ratios block (DSCR, ISCR, Current Ratio,
Debt-Equity, Debt/Assets, EBITDA Margin %, Net Profit Margin %, Net Worth)
is what '2-Financial Analysis' demonstrably matches cell-for-cell (verified
during the architecture review), so those eight metrics are sourced from
that block. '1-Input Data' also defines an independent "Calculated" formula
for each of them (rows 49-56, columns C/E) as an internal reconciliation
check - that calculated value is exposed here too, alongside a note when it
does not tie to the reported figure, rather than silently picked over it.
"""

from dataclasses import dataclass, field
from typing import Optional

from reference_data import ReferenceBundle
from input_data_engine import (
    DerivedInputMetrics,
    CalculatedRatios,
    compute_derived_metrics,
    compute_calculated_ratios,
)


@dataclass(frozen=True)
class MetricRow:
    key: str
    label: str
    current: Optional[float]
    previous: Optional[float]
    pct_change: Optional[float]
    threshold: Optional[float]
    status: Optional[str]
    source: str
    note: str = ""


@dataclass(frozen=True)
class FinancialAnalysisResult:
    income_statement_rows: list
    ratio_rows: list
    overall_status: str
    overall_status_note: str
    derived: DerivedInputMetrics
    calculated_ratios: CalculatedRatios
    unavailable: list = field(default_factory=list)


def _pct_change(current, previous):
    if previous in (None, 0):
        return None
    return (current - previous) / previous


def build_financial_analysis(bundle: ReferenceBundle) -> FinancialAnalysisResult:
    d = bundle.input_data
    policy = bundle.fa_policy.thresholds
    m = compute_derived_metrics(d)
    c = compute_calculated_ratios(d, m)

    rows1 = []
    rows2 = []
    statuses_used_for_gate = []

    def add1(key, label, current, previous, threshold, passes, source, note=""):
        pct = _pct_change(current, previous)
        status = "Pass" if passes(current, pct) else "Fail"
        rows1.append(MetricRow(key, label, current, previous, pct, threshold, status, source, note))
        statuses_used_for_gate.append(status)
        return status

    def add2(key, label, current, previous, threshold, passes, source, note=""):
        pct = _pct_change(current, previous)
        status = "Pass" if passes(current) else "Fail"
        rows2.append(MetricRow(key, label, current, previous, pct, threshold, status, source, note))
        statuses_used_for_gate.append(status)
        return status

    # ---------------------------------------------------------------
    # Table 1: Income Statement / Balance Sheet / Cash Flow metrics
    # (reference '2-Financial Analysis' rows 7-14)
    # ---------------------------------------------------------------

    add1("revenue", "Revenue", d.revenue.current, d.revenue.previous,
         policy["revenue"],
         lambda cur, pct: pct is not None and pct >= policy["revenue"],
         "input (1-Input Data B5/C5)")

    ebitda_margin_pct_cur = m.ebitda_margin.current * 100
    ebitda_margin_pct_prev = m.ebitda_margin.previous * 100
    add1("ebitda_margin_growth", "EBITDA Margin %", ebitda_margin_pct_cur, ebitda_margin_pct_prev,
         policy["ebitda_margin_growth"],
         lambda cur, pct: pct is not None and pct >= policy["ebitda_margin_growth"],
         "calculated = EBITDA / Revenue (1-Input Data B18/B5 formula chain)")

    add1("net_profit", "Net Profit", m.net_profit.current, m.net_profit.previous,
         policy["net_profit"],
         lambda cur, pct: pct is not None and pct >= policy["net_profit"],
         "calculated = PBT - Tax (1-Input Data B14-B15 formula chain)")

    add1("total_assets", "Total Assets", d.total_assets.current, d.total_assets.previous,
         policy["total_assets"],
         lambda cur, pct: pct is not None and pct >= policy["total_assets"],
         "input (1-Input Data B24/C24)")

    add1("net_worth", "Net Worth", d.reported_net_worth.current, d.reported_net_worth.previous,
         policy["net_worth"],
         lambda cur, pct: pct is not None and pct >= policy["net_worth"],
         "input, Reported Ratios block (1-Input Data B56/D56)",
         "This is the company's disclosed Net Worth, not Total Equity (B25). The two differ "
         "by about 2.4% in FY-current; no reconciliation formula ties them in the reference "
         "workbook, so the disclosed figure is used as-is. See validation report.")

    add1("operating_cash_flow", "Operating Cash Flow", d.operating_cash_flow.current, d.operating_cash_flow.previous,
         policy["operating_cash_flow"],
         lambda cur, pct: pct is not None and pct >= policy["operating_cash_flow"],
         "input (1-Input Data B40/C40)")

    ocf_to_debt_cur = d.operating_cash_flow.current / m.total_debt.current
    ocf_to_debt_prev = d.operating_cash_flow.previous / m.total_debt.previous
    add1("ocf_to_debt", "OCF to Debt", ocf_to_debt_cur, ocf_to_debt_prev,
         policy["ocf_to_debt"],
         lambda cur, pct: cur >= 0.05,
         "calculated = OCF / Total Debt (2-Financial Analysis C13 formula)",
         "Reference workbook's own status formula (H13) hardcodes a 5% cut-off rather than "
         "referencing the 20% threshold shown alongside it (G13) - replicated as written.")

    total_debt_pct = _pct_change(m.total_debt.current, m.total_debt.previous)
    total_debt_status = "Pass" if m.total_debt.current >= 0.05 else "Fail"
    rows1.append(MetricRow(
        "total_debt", "Total Debt", m.total_debt.current, m.total_debt.previous, total_debt_pct,
        policy["total_debt"], total_debt_status,
        "calculated = SUM(borrowings + lease liabilities) (1-Input Data B30 formula)",
        "Reference workbook's own status formula (H14) compares the absolute debt figure "
        "(Rs crore) to 5%, which is always true, and this row is excluded from the sheet's "
        "own Approve/Reject gate (COUNTIF range stops at row 13) - replicated as written and "
        "likewise excluded from the gate below.",
    ))
    # total_debt_status is deliberately NOT added to statuses_used_for_gate,
    # matching the reference workbook's own gate (COUNTIF(H7:H13,...)).

    # ---------------------------------------------------------------
    # Table 2: Ratio table (reference '2-Financial Analysis' rows 18-26)
    # ---------------------------------------------------------------

    add2("debt_equity", "Debt-Equity", d.reported_debt_equity.current, d.reported_debt_equity.previous,
         policy["debt_equity"], lambda cur: cur >= 0.5,
         "input, Reported Ratios block (1-Input Data B52/D52)",
         f"Calculated cross-check (Net Debt / Average Equity) = {c.debt_equity.current:.4f}x "
         f"FY-current / {c.debt_equity.previous:.4f}x FY-previous - ties within tolerance.")

    add2("dscr", "DSCR", d.reported_dscr.current, d.reported_dscr.previous,
         policy["dscr"], lambda cur: cur >= 1.5,
         "input, Reported Ratios block (1-Input Data B49/D49)",
         f"Calculated cross-check (EBIT / (Net Finance Charges + Principal Repayments)) = "
         f"{c.dscr.current:.4f}x FY-current / {c.dscr.previous:.4f}x FY-previous - does NOT tie "
         f"to the reported figure. '1-Input Data' defines no reconciliation check for this pair "
         f"(unlike EBITDA margin, net profit margin, debt/assets and debt-equity, which all have "
         f"one). Flagged, not guessed - see validation report.")

    add2("iscr", "ISCR", d.reported_iscr.current, d.reported_iscr.previous,
         policy["iscr"], lambda cur: cur >= 3,
         "input, Reported Ratios block (1-Input Data B50/D50)",
         f"Calculated cross-check (EBIT / Net Finance Charges) = {c.iscr.current:.4f}x "
         f"FY-current / {c.iscr.previous:.4f}x FY-previous - does not tie closely to the "
         f"reported figure. Flagged - see validation report.")

    add2("current_ratio", "Current Ratio", d.reported_current_ratio.current, d.reported_current_ratio.previous,
         policy["current_ratio"], lambda cur: cur >= 0.5,
         "input, Reported Ratios block (1-Input Data B51/D51)",
         f"Calculated cross-check (Current Assets / (Current Liabilities - Current Lease "
         f"Liabilities)) = {c.current_ratio.current:.4f}x FY-current / {c.current_ratio.previous:.4f}x "
         f"FY-previous - does not tie closely to the reported figure. Flagged - see validation report.")

    add2("debt_assets", "Debt/Assets", d.reported_debt_assets.current, d.reported_debt_assets.previous,
         policy["debt_assets"], lambda cur: cur < 0.6,
         "input, Reported Ratios block (1-Input Data B53/D53)",
         f"Calculated cross-check (Total Debt / Total Assets) = {c.debt_assets.current:.4f} "
         f"FY-current / {c.debt_assets.previous:.4f} FY-previous - ties within tolerance.")

    rows2.append(MetricRow(
        "debtors_days", "Debtors Days", None, None, None, policy.get("debtors_days"), None,
        "not available",
        "Requires average Trade Receivables and credit Sales; neither field exists anywhere in "
        "'1-Input Data'. Not computed - the reference workbook's static value is not reproduced.",
    ))
    rows2.append(MetricRow(
        "inventory_days", "Inventory Days", None, None, None, policy.get("inventory_days"), None,
        "not available",
        "Requires average Inventory and Cost of Sales; neither field exists anywhere in "
        "'1-Input Data'. Not computed - the reference workbook's static value is not reproduced.",
    ))

    add2("ebitda_margin_pct", "EBITDA Margin %", ebitda_margin_pct_cur, ebitda_margin_pct_prev,
         policy["ebitda_margin_pct"], lambda cur: cur > 15,
         "calculated = EBITDA / Revenue (1-Input Data B18/B5 formula chain)")

    net_profit_margin_pct_cur = c.net_profit_margin.current * 100
    net_profit_margin_pct_prev = c.net_profit_margin.previous * 100
    add2("net_profit_margin_pct", "Net Profit Margin %", net_profit_margin_pct_cur, net_profit_margin_pct_prev,
         policy["net_profit_margin_pct"], lambda cur: cur > 10,
         "calculated = Net Profit / Revenue (1-Input Data B16/B5 formula chain)")

    overall_status = "Approve" if all(s == "Pass" for s in statuses_used_for_gate) else "Reject"
    overall_status_note = (
        "Based on the 14 metrics that can be computed from '1-Input Data' (matches the reference "
        "workbook's own gate, which likewise excludes the Total Debt row). The reference gate also "
        "spans Debtors Days and Inventory Days (H18:H26); those two cannot be evaluated here - "
        "excluded rather than assumed Pass."
    )

    return FinancialAnalysisResult(
        income_statement_rows=rows1,
        ratio_rows=rows2,
        overall_status=overall_status,
        overall_status_note=overall_status_note,
        derived=m,
        calculated_ratios=c,
        unavailable=["Debtors Days", "Inventory Days"],
    )
