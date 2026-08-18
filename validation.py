"""
Compares Python-calculated output against the values actually stored in the
reference workbook (as last saved by Excel, read with data_only=True).
Never adjusts a calculated value to force a match - every row below reports
what was calculated, what the reference workbook shows, and the difference.
"""

from dataclasses import dataclass
from typing import Optional

from openpyxl import load_workbook

from reference_data import INPUT_SHEET, FINANCIAL_ANALYSIS_SHEET, RISK_SCORING_SHEET


@dataclass(frozen=True)
class ValidationRow:
    metric: str
    calculated: object
    reference: object
    difference: Optional[object]
    status: str


def _compare(metric, calculated, reference, tolerance):
    if calculated is None or reference is None:
        return ValidationRow(metric, calculated, reference, None, "NOT COMPARABLE")
    if isinstance(calculated, str) or isinstance(reference, str):
        status = "MATCH" if str(calculated).strip() == str(reference).strip() else "DISCREPANCY"
        return ValidationRow(metric, calculated, reference, None, status)
    diff = calculated - reference
    status = "MATCH" if abs(diff) <= tolerance else "DISCREPANCY"
    return ValidationRow(metric, calculated, reference, diff, status)


def validate_input_data_engine(d, m, c, workbook_path):
    """Cross-checks input_data_engine's derived metrics against '1-Input
    Data''s own cached formula results - proves the formula transcription
    (Total income, PBT, Net profit, EBITDA, EBIT, Total debt, Net debt,
    Average equity, Calculated ratios) is bit-exact before it feeds
    anything downstream."""
    wb = load_workbook(workbook_path, data_only=True)
    ws = wb[INPUT_SHEET]
    rows = [
        _compare("Total income (B7)", m.total_income.current, ws["B7"].value, 0.01),
        _compare("Total income (C7)", m.total_income.previous, ws["C7"].value, 0.01),
        _compare("PBT before exceptional (B12)", m.pbt_before_exceptional.current, ws["B12"].value, 0.01),
        _compare("PBT before exceptional (C12)", m.pbt_before_exceptional.previous, ws["C12"].value, 0.01),
        _compare("PBT (B14)", m.pbt.current, ws["B14"].value, 0.01),
        _compare("PBT (C14)", m.pbt.previous, ws["C14"].value, 0.01),
        _compare("Net profit (B16)", m.net_profit.current, ws["B16"].value, 0.01),
        _compare("Net profit (C16)", m.net_profit.previous, ws["C16"].value, 0.01),
        _compare("Net finance charges (B17)", m.net_finance_charges.current, ws["B17"].value, 0.01),
        _compare("Net finance charges (C17)", m.net_finance_charges.previous, ws["C17"].value, 0.01),
        _compare("EBITDA (B18)", m.ebitda.current, ws["B18"].value, 0.01),
        _compare("EBITDA (C18)", m.ebitda.previous, ws["C18"].value, 0.01),
        _compare("EBITDA margin, calculated (B19)", m.ebitda_margin.current, ws["B19"].value, 1e-6),
        _compare("EBITDA margin, calculated (C19)", m.ebitda_margin.previous, ws["C19"].value, 1e-6),
        _compare("EBIT (B20)", m.ebit.current, ws["B20"].value, 0.01),
        _compare("EBIT (C20)", m.ebit.previous, ws["C20"].value, 0.01),
        _compare("Total debt (B30)", m.total_debt.current, ws["B30"].value, 0.01),
        _compare("Total debt (C30)", m.total_debt.previous, ws["C30"].value, 0.01),
        _compare("Net debt (B34)", m.net_debt.current, ws["B34"].value, 0.01),
        _compare("Net debt (C34)", m.net_debt.previous, ws["C34"].value, 0.01),
        _compare("Average equity (B59)", m.average_equity.current, ws["B59"].value, 0.01),
        _compare("Average equity (C59)", m.average_equity.previous, ws["C59"].value, 0.01),
        _compare("Calculated DSCR (C49)", c.dscr.current, ws["C49"].value, 1e-6),
        _compare("Calculated DSCR (E49)", c.dscr.previous, ws["E49"].value, 1e-6),
        _compare("Calculated ISCR (C50)", c.iscr.current, ws["C50"].value, 1e-6),
        _compare("Calculated ISCR (E50)", c.iscr.previous, ws["E50"].value, 1e-6),
        _compare("Calculated Current Ratio (C51)", c.current_ratio.current, ws["C51"].value, 1e-6),
        _compare("Calculated Current Ratio (E51)", c.current_ratio.previous, ws["E51"].value, 1e-6),
        _compare("Calculated Debt-Equity (C52)", c.debt_equity.current, ws["C52"].value, 1e-6),
        _compare("Calculated Debt-Equity (E52)", c.debt_equity.previous, ws["E52"].value, 1e-6),
        _compare("Calculated Debt/Assets (C53)", c.debt_assets.current, ws["C53"].value, 1e-6),
        _compare("Calculated Debt/Assets (E53)", c.debt_assets.previous, ws["E53"].value, 1e-6),
        _compare("Calculated Net Worth (C56)", c.net_worth.current, ws["C56"].value, 0.01),
        _compare("Calculated Net Worth (E56)", c.net_worth.previous, ws["E56"].value, 0.01),
    ]
    return rows


def validate_input_checks(checks, workbook_path):
    """Cross-checks input_data_engine.run_validation_checks() results
    against '1-Input Data''s own cached check-cell text (B63-B73)."""
    wb = load_workbook(workbook_path, data_only=True)
    ws = wb[INPUT_SHEET]
    rows = []
    for check in checks:
        reference_value = ws[check.cell].value
        rows.append(_compare(f"{check.label} ({check.cell})", check.result, reference_value, 0))
    return rows


# (row key, reference cell for FY-current, reference cell for FY-previous, tolerance)
_FA_CELL_MAP = {
    "revenue": ("C7", "D7", 0.01),
    "ebitda_margin_growth": ("C8", "D8", 0.01),
    "net_profit": ("C9", "D9", 0.01),
    "total_assets": ("C10", "D10", 0.01),
    "net_worth": ("C11", "D11", 0.01),
    "operating_cash_flow": ("C12", "D12", 0.01),
    "ocf_to_debt": ("C13", "D13", 0.0005),
    "total_debt": ("C14", "D14", 0.01),
    "debt_equity": ("C18", "D18", 0.005),
    "dscr": ("C19", "D19", 0.005),
    "iscr": ("C20", "D20", 0.005),
    "current_ratio": ("C21", "D21", 0.005),
    "debt_assets": ("C22", "D22", 0.005),
    "ebitda_margin_pct": ("C25", "D25", 0.05),
    "net_profit_margin_pct": ("C26", "D26", 0.05),
}


def validate_financial_analysis(fa_result, workbook_path):
    wb = load_workbook(workbook_path, data_only=True)
    ws = wb[FINANCIAL_ANALYSIS_SHEET]

    rows_by_key = {r.key: r for r in fa_result.income_statement_rows + fa_result.ratio_rows}
    results = []

    for key, (cur_cell, prev_cell, tol) in _FA_CELL_MAP.items():
        row = rows_by_key[key]
        results.append(_compare(f"{row.label} (FY-current, {cur_cell})", row.current, ws[cur_cell].value, tol))
        results.append(_compare(f"{row.label} (FY-previous, {prev_cell})", row.previous, ws[prev_cell].value, tol))

    for key, cell in (("debtors_days", "C23"), ("inventory_days", "C24")):
        row = rows_by_key[key]
        results.append(ValidationRow(
            f"{row.label} (FY-current, {cell})", None, ws[cell].value, None,
            "NOT COMPARABLE - required source data not present in 1-Input Data",
        ))

    results.append(_compare("Overall status (B3)", fa_result.overall_status, ws["B3"].value, 0))
    return results


_FS_BAND_CELLS = {
    "dscr": ("D30", "F30"),
    "iscr": ("D31", "F31"),
    "current_ratio": ("D32", "F32"),
    "debt_equity": ("D33", "F33"),
}

_COMPONENT_WEIGHTED_CELLS = {
    "financial_strength": "D13",
    "industry_risk": "D14",
    "management_quality": "D15",
    "leverage": "D16",
    "liquidity": "D17",
    "debt_service_capacity": "D18",
    "collateral_asset_coverage": "D19",
    "business_position": "D20",
}


def validate_risk_scoring(risk_result, workbook_path):
    wb = load_workbook(workbook_path, data_only=True)
    ws = wb[RISK_SCORING_SHEET]
    results = []

    for fs in risk_result.financial_strength_inputs:
        score_cell, weighted_cell = _FS_BAND_CELLS[fs.key]
        results.append(_compare(f"{fs.label} band score ({score_cell})", fs.score, ws[score_cell].value, 0))
        results.append(_compare(
            f"{fs.label} weighted contribution ({weighted_cell})",
            fs.score * fs.weight, ws[weighted_cell].value, 0.001,
        ))

    results.append(_compare("Financial Strength score (F35)", risk_result.financial_strength_score, ws["F35"].value, 0.01))

    for comp in risk_result.components:
        cell = _COMPONENT_WEIGHTED_CELLS[comp.key]
        results.append(_compare(f"{comp.label} weighted score ({cell})", comp.weighted_score, ws[cell].value, 0.01))

    results.append(_compare("Total Weighted Score (D23)", risk_result.total_weighted_score, ws["D23"].value, 0.01))
    results.append(_compare("Risk Grade (D24)", risk_result.risk_grade, ws["D24"].value, 0))
    results.append(_compare("Overall status (B10)", risk_result.overall_status, ws["B10"].value, 0))
    return results
