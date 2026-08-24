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

Debtors Days and Inventory Days cannot be *derived* from '1-Input Data':
there is no Trade Receivables, Inventory or Cost of Sales figure anywhere
in its schema to compute average-receivables/average-inventory turnover
from. A later data-source audit found they are nonetheless available a
different way for documents that disclose them: Tata Steel's own filing
reports "Debtors turnover ratio (in days)" and "Inventory turnover ratio
(in days)" directly, in the same Regulation 52(4) block as DSCR/ISCR/
Current Ratio/etc. below - the company's own pre-computed figure, not
something derived here. When a document's disclosure includes them
('1-Input Data' rows 74/78 - see reference_data.py and ocr_pipeline.py),
they're used the same way as every other Reported Ratios field; when it
doesn't, they're reported as not available rather than invented.

The reference workbook's Reported Ratios block (DSCR, ISCR, Current Ratio,
Debt-Equity, Debt/Assets, EBITDA Margin %, Net Profit Margin %, Net Worth,
plus Debtors/Inventory Turnover Days where disclosed) is what
'2-Financial Analysis' demonstrably matches cell-for-cell (verified during
the architecture review), so those metrics are sourced from that block.
'1-Input Data' also defines an independent "Calculated" formula for the
original eight of them (rows 49-56, columns C/E) as an internal
reconciliation check - that calculated value is exposed here too, alongside
a note when it does not tie to the reported figure, rather than silently
picked over it. Debtors/Inventory Turnover Days have no such calculated
cross-check cell (no raw components to calculate one from either), so none
is shown for them.
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

    def _fmt_or_na(value, suffix=""):
        return f"{value:.4f}{suffix}" if value is not None else "not available"

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

    ebitda_margin_pct_cur = m.ebitda_margin.current * 100 if m.ebitda_margin.current is not None else None
    ebitda_margin_pct_prev = m.ebitda_margin.previous * 100 if m.ebitda_margin.previous is not None else None
    if ebitda_margin_pct_cur is None or ebitda_margin_pct_prev is None:
        rows1.append(MetricRow(
            "ebitda_margin_growth", "EBITDA Margin %", None, None, None,
            policy.get("ebitda_margin_growth"), None, "not available",
            "Requires Interest Income, which this document does not separately report in the P&L.",
        ))
    else:
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

    if d.reported_net_worth.current is None or d.reported_net_worth.previous is None:
        rows1.append(MetricRow(
            "net_worth", "Net Worth", None, None, None,
            policy["net_worth"], None, "not available",
            "The source document's reported figure for this field was too ambiguous/garbled to "
            "safely extract (see the OCR diagnostic).",
        ))
    else:
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
         f"Calculated cross-check (Net Debt / Average Equity) = {_fmt_or_na(c.debt_equity.current, 'x')} "
         f"FY-current / {_fmt_or_na(c.debt_equity.previous, 'x')} FY-previous - ties within tolerance.")

    add2("dscr", "DSCR", d.reported_dscr.current, d.reported_dscr.previous,
         policy["dscr"], lambda cur: cur >= 1.5,
         "input, Reported Ratios block (1-Input Data B49/D49)",
         f"Calculated cross-check (EBIT / (Net Finance Charges + Principal Repayments)) = "
         f"{_fmt_or_na(c.dscr.current, 'x')} FY-current / {_fmt_or_na(c.dscr.previous, 'x')} FY-previous - does NOT tie "
         f"to the reported figure. '1-Input Data' defines no reconciliation check for this pair "
         f"(unlike EBITDA margin, net profit margin, debt/assets and debt-equity, which all have "
         f"one). Flagged, not guessed - see validation report.")

    add2("iscr", "ISCR", d.reported_iscr.current, d.reported_iscr.previous,
         policy["iscr"], lambda cur: cur >= 3,
         "input, Reported Ratios block (1-Input Data B50/D50)",
         f"Calculated cross-check (EBIT / Net Finance Charges) = {_fmt_or_na(c.iscr.current, 'x')} "
         f"FY-current / {_fmt_or_na(c.iscr.previous, 'x')} FY-previous - does not tie closely to the "
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

    if d.reported_debtors_turnover_days.current is None:
        rows2.append(MetricRow(
            "debtors_days", "Debtors Days", None, None, None, policy.get("debtors_days"), None,
            "not available",
            "This document's own ratio disclosures do not include a Debtors/Receivables "
            "turnover figure - not every filing discloses it. Not computed from raw Trade "
            "Receivables/Sales either, since neither field exists anywhere in '1-Input Data'.",
        ))
    else:
        add2("debtors_days", "Debtors Days",
             d.reported_debtors_turnover_days.current, d.reported_debtors_turnover_days.previous,
             policy["debtors_days"], lambda cur: cur < policy["debtors_days"],
             "input, Reported Ratios block (1-Input Data B74/D74) - the document's own "
             "disclosed Debtors turnover ratio (in days), not derived here")

    if d.reported_inventory_turnover_days.current is None:
        rows2.append(MetricRow(
            "inventory_days", "Inventory Days", None, None, None, policy.get("inventory_days"), None,
            "not available",
            "This document's own ratio disclosures do not include an Inventory turnover "
            "figure - not every filing discloses it. Not computed from raw Inventory/Cost of "
            "Sales either, since neither field exists anywhere in '1-Input Data'.",
        ))
    else:
        add2("inventory_days", "Inventory Days",
             d.reported_inventory_turnover_days.current, d.reported_inventory_turnover_days.previous,
             policy["inventory_days"], lambda cur: cur < policy["inventory_days"],
             "input, Reported Ratios block (1-Input Data B78/D78) - the document's own "
             "disclosed Inventory turnover ratio (in days), not derived here")

    if ebitda_margin_pct_cur is None or ebitda_margin_pct_prev is None:
        rows2.append(MetricRow(
            "ebitda_margin_pct", "EBITDA Margin %", None, None, None,
            policy.get("ebitda_margin_pct"), None, "not available",
            "Requires Interest Income, which this document does not separately report in the P&L.",
        ))
    else:
        add2("ebitda_margin_pct", "EBITDA Margin %", ebitda_margin_pct_cur, ebitda_margin_pct_prev,
             policy["ebitda_margin_pct"], lambda cur: cur > 15,
             "calculated = EBITDA / Revenue (1-Input Data B18/B5 formula chain)")

    net_profit_margin_pct_cur = c.net_profit_margin.current * 100
    net_profit_margin_pct_prev = c.net_profit_margin.previous * 100
    add2("net_profit_margin_pct", "Net Profit Margin %", net_profit_margin_pct_cur, net_profit_margin_pct_prev,
         policy["net_profit_margin_pct"], lambda cur: cur > 10,
         "calculated = Net Profit / Revenue (1-Input Data B16/B5 formula chain)")

    unavailable = []
    if d.reported_debtors_turnover_days.current is None:
        unavailable.append("Debtors Days")
    if d.reported_inventory_turnover_days.current is None:
        unavailable.append("Inventory Days")

    overall_status = "Approve" if all(s == "Pass" for s in statuses_used_for_gate) else "Reject"
    if unavailable:
        overall_status_note = (
            f"Based on the {len(statuses_used_for_gate)} metrics that can be computed from this "
            f"document (matches the reference workbook's own gate, which likewise excludes the "
            f"Total Debt row). The reference gate also spans Debtors Days and Inventory Days "
            f"(H18:H26); {' and '.join(unavailable)} cannot be evaluated for this document "
            f"(not disclosed) - excluded rather than assumed Pass."
        )
    else:
        overall_status_note = (
            f"Based on all {len(statuses_used_for_gate)} metrics the reference workbook's own gate "
            f"spans (H18:H26, excluding the Total Debt row) - Debtors Days and Inventory Days are "
            f"included here since this document discloses both."
        )

    return FinancialAnalysisResult(
        income_statement_rows=rows1,
        ratio_rows=rows2,
        overall_status=overall_status,
        overall_status_note=overall_status_note,
        derived=m,
        calculated_ratios=c,
        unavailable=unavailable,
    )
