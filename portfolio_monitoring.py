"""
Area 8 - Portfolio Monitoring.

Unlike every other module, '8-Portfolio Monitoring' has NO cross-sheet
formula references anywhere in the reference workbook - confirmed by
tracing every formula on the sheet. Every input (Portfolio Segment, Total
Portfolio Exposure, Number of Accounts, Proposed Loan Amount, Gross NPL) is
the reference workbook's own "Input value" cell: lender-portfolio-level
data that has nothing to do with Tata Steel's own financial statements, so
there is nothing in '1-Input Data' / financial_analysis.py / risk_scoring.py
this module could consume even if the dependency existed. This module is
therefore fully self-contained, reading only from '8-Portfolio Monitoring'
itself.

Note: C11 (Proposed Loan Amount, 5000) numerically matches
'6-Limit Setting'!B4 (also 5000) in this worked example, but there is no
formula linking them in the reference workbook - each sheet carries its own
independent hardcoded figure. Per the reference-workbook parity rule this
is reproduced as two independent reference inputs, not auto-linked.

C10 (Average Exposure per Account) is stored as a hardcoded 400 rather than
a formula, but its own row label states the exact arithmetic ("Portfolio
Exposure / Accounts") and it ties exactly (100,000 / 250 = 400) - formularized
here, same treatment as similar cases in limit_setting.py / covenants.py.

Formulas reproduced exactly as found in '8-Portfolio Monitoring':
    Average Exposure per Account   = Portfolio Exposure / Accounts        (C10)
    Borrower Concentration %       = Proposed Loan / Portfolio Exposure   (C12)
    Gross NPL Ratio %              = Gross NPL / Portfolio Exposure       (C14)
    Portfolio Exposure Post Sanction = Portfolio Exposure + Proposed Loan (C15)
    Single Borrower Exposure %     = Proposed Loan / Post-Sanction Exposure (C16)
    NPL risk band     : <2% Low, 2-5% Moderate, >5% High                  (C20)
    Concentration band: <5% Low, 5-10% Moderate, >10% High                (C21, C22)
    Overall status = Approve if at least 2 of the 3 bands are Low/Moderate Risk (B4)

Note: row 22 is labelled "Manufacturing Sector Exposure" but its formula
(B22=C16) reuses the same post-sanction Single Borrower Exposure % as row
16/21, not a distinct sector-level calculation - reproduced exactly as
written, flagged here as a naming/overlap observation rather than treated
as an error to fix.
"""

from dataclasses import dataclass

from openpyxl import load_workbook

PORTFOLIO_MONITORING_SHEET = "8-Portfolio Monitoring"


@dataclass(frozen=True)
class PortfolioInputs:
    segment: str
    total_portfolio_exposure: float
    number_of_accounts: int
    proposed_loan_amount: float
    gross_npl: float


@dataclass(frozen=True)
class PortfolioCriterion:
    label: str
    value: float
    band: str
    band_scale: str


@dataclass(frozen=True)
class PortfolioMonitoringResult:
    average_exposure_per_account: float
    borrower_concentration_pct: float
    npl_ratio_pct: float
    portfolio_exposure_post_sanction: float
    single_borrower_exposure_pct: float
    criteria: list
    overall_status: str


def load_portfolio_inputs(workbook_path) -> PortfolioInputs:
    ws = load_workbook(workbook_path, data_only=True)[PORTFOLIO_MONITORING_SHEET]
    return PortfolioInputs(
        segment=str(ws["C7"].value),
        total_portfolio_exposure=float(ws["C8"].value),
        number_of_accounts=int(ws["C9"].value),
        proposed_loan_amount=float(ws["C11"].value),
        gross_npl=float(ws["C13"].value),
    )


def _npl_band(pct):
    # '8-Portfolio Monitoring'!C20: IF(B20<=2%,"Low Risk",IF(B20<5%,"Moderate Risk","High Risk"))
    if pct <= 0.02:
        return "Low Risk"
    if pct < 0.05:
        return "Moderate Risk"
    return "High Risk"


def _concentration_band(pct):
    # '8-Portfolio Monitoring'!C21 / C22: IF(B<5%,"Low Risk",IF(B<=10%,"Moderate Risk","High Risk"))
    if pct < 0.05:
        return "Low Risk"
    if pct <= 0.10:
        return "Moderate Risk"
    return "High Risk"


def build_portfolio_monitoring(inputs: PortfolioInputs) -> PortfolioMonitoringResult:
    average_exposure = inputs.total_portfolio_exposure / inputs.number_of_accounts
    borrower_concentration = inputs.proposed_loan_amount / inputs.total_portfolio_exposure
    npl_ratio = inputs.gross_npl / inputs.total_portfolio_exposure
    post_sanction_exposure = inputs.total_portfolio_exposure + inputs.proposed_loan_amount
    single_borrower_exposure = inputs.proposed_loan_amount / post_sanction_exposure

    npl_band = _npl_band(npl_ratio)
    concentration_band = _concentration_band(borrower_concentration)
    sector_band = _concentration_band(single_borrower_exposure)

    criteria = [
        PortfolioCriterion("Non Performing Loan %", npl_ratio, npl_band, "<2% Low; 2-5% Moderate; >5% High"),
        PortfolioCriterion("Borrower Concentration Risk", borrower_concentration, concentration_band, "<5% Low; 5-10% Moderate; >10% High"),
        PortfolioCriterion("Manufacturing Sector Exposure", single_borrower_exposure, sector_band, "<5% Low; 5-10% Moderate; >10% High"),
    ]

    # '8-Portfolio Monitoring'!B4
    low_or_moderate = sum(1 for c in criteria if c.band in ("Low Risk", "Moderate Risk"))
    overall_status = "Approve" if low_or_moderate >= 2 else "Reject"

    return PortfolioMonitoringResult(
        average_exposure_per_account=average_exposure,
        borrower_concentration_pct=borrower_concentration,
        npl_ratio_pct=npl_ratio,
        portfolio_exposure_post_sanction=post_sanction_exposure,
        single_borrower_exposure_pct=single_borrower_exposure,
        criteria=criteria,
        overall_status=overall_status,
    )
