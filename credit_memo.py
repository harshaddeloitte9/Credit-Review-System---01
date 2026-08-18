"""
Area 9 - Credit Memo: Summary.

Terminal aggregator - '9 - Credit Memo' has no calculations of its own; it
only pulls a single status cell out of each of the other 7 areas:
    B4  = '2-Financial Analysis'!B3    (Overall status: Approve/Reject)
    B8  = '3-Risk Scoring'!B10         (Overall status: risk grade text)
    B12 = '4-Covenants'!E14            (Overall Status: PASS/NOT PASS)
    B16 = '5-Collateral Assessment'!B12 (= D24, Collateral Rating text)
    B20 = '6-Limit Setting'!B40        (Conclusion: PASS/false)
    B24 = '7-Continuous Monitoring'!D10 (Overall Status: PASS/NOT PASS)
    B28 = '8-Portfolio Monitoring'!B4  (Overall status: Approve/Reject)

This module therefore does nothing but collect the already-computed
results of financial_analysis.py / risk_scoring.py (frozen) and
covenants.py / collateral_assessment.py / limit_setting.py /
continuous_monitoring.py / portfolio_monitoring.py (built earlier in this
same pass) - it re-derives none of them.

The reference workbook's A-column narrative paragraphs (A4, A8, A12, A16,
A20, A24, A28) are hand-written analyst prose - editorial judgement
("Key concern remains weak current ratio", "cyclical steel industry
exposure"), not a formula or anything derivable from the data. These are
read and shown as the reference workbook's own commentary, clearly
labelled as such, rather than regenerated - never fabricated as if this
application had produced its own written analysis.
"""

from dataclasses import dataclass

from openpyxl import load_workbook

CREDIT_MEMO_SHEET = "9 - Credit Memo"


@dataclass(frozen=True)
class CreditMemoArea:
    area_label: str
    status: object
    reference_commentary: str


@dataclass(frozen=True)
class CreditMemoResult:
    areas: list


def load_credit_memo_commentary(workbook_path) -> dict:
    ws = load_workbook(workbook_path, data_only=True)[CREDIT_MEMO_SHEET]
    return {
        "financial_analysis": str(ws["A4"].value),
        "risk_scoring": str(ws["A8"].value),
        "covenants": str(ws["A12"].value),
        "collateral_assessment": str(ws["A16"].value),
        "limit_setting": str(ws["A20"].value),
        "continuous_monitoring": str(ws["A24"].value),
        "portfolio_monitoring": str(ws["A28"].value),
    }


def build_credit_memo(fa, risk, covenants_result, collateral_result, limit_result,
                       monitoring_result, portfolio_result, commentary: dict) -> CreditMemoResult:
    limit_conclusion = limit_result.conclusion if isinstance(limit_result.conclusion, str) else "FALSE"

    areas = [
        CreditMemoArea("Area 2 - Financial Analysis & Interpretation", fa.overall_status, commentary["financial_analysis"]),
        CreditMemoArea("Area 3 - Risk Scoring", risk.overall_status, commentary["risk_scoring"]),
        CreditMemoArea("Area 4 - Covenant Monitoring", covenants_result.overall_status, commentary["covenants"]),
        CreditMemoArea("Area 5 - Collateral Assessment", collateral_result.rating, commentary["collateral_assessment"]),
        CreditMemoArea("Area 6 - Proposed Loan Impact & Limit Recommendation", limit_conclusion, commentary["limit_setting"]),
        CreditMemoArea("Area 7 - Continuous Monitoring", monitoring_result.overall_status, commentary["continuous_monitoring"]),
        CreditMemoArea("Area 8 - Portfolio Monitoring", portfolio_result.overall_status, commentary["portfolio_monitoring"]),
    ]

    return CreditMemoResult(areas=areas)
