"""
Area 6 - Proposed Loan Impact & Limit Recommendation.

Reference workbook dependency (verified from actual formulas):
    '6-Limit Setting'!B19 = '1-Input Data'!B20 / '1-Input Data'!B49   (EBIT / reported DSCR)
    '6-Limit Setting'!B22 = '1-Input Data'!B20                        (EBIT)

Both are DIRECT Input Data references - the reference workbook's own
'6-Limit Setting' sheet does not go through '2-Financial Analysis' or
'3-Risk Scoring' for these figures. This module consumes
input_data_engine.py's EBIT (frozen) and reference_data.py's reported DSCR
(frozen) rather than recomputing either.

Deal parameters (loan amount, interest rate, tenor) and the policy minimum
DSCR are the reference workbook's own "Yellow fill = Assumption / policy
parameter" cells (B4, B5, B6, B24, B30) - read live, not hardcoded here.

Formulas reproduced exactly as found in '6-Limit Setting':
    Annual instalment (annuity)            = -PMT(rate, tenor, loan amount)      (B7)
    Amortization schedule (5 years)        = standard opening/interest/principal/
                                              closing balance roll-forward         (B11:E15)
    Implied existing debt-service denom.   = EBIT / reported DSCR                 (B19)
    Total pro-forma debt service           = existing denom. + new instalment     (B21)
    Pro-forma DSCR                         = EBIT / total pro-forma debt service  (B23)
    Max serviceable total debt service     = EBIT / policy minimum DSCR           (B31)
    Available additional annual debt svc.  = max serviceable - existing denom.    (B33)
    Capacity cap (PV annuity factor)       = available svc. * (1-(1+r)^-n)/r      (B34)
    Recommended limit                      = MIN(requested amount, capacity cap)  (B39)
    Conclusion                             = "PASS" if recommended == requested,
                                              otherwise the reference workbook's
                                              own IF has no false-branch (Excel
                                              would show FALSE) - reproduced as
                                              Python False, not invented as "FAIL"  (B40)

B25 ("Pro-forma DSCR status (>= 1.5x)") is a hardcoded "PASS" string in the
reference workbook rather than a formula, but its own row label states the
rule unambiguously and it is the same >=-minimum pattern used everywhere
else in this workbook - reproduced here as a computed comparison
(pro-forma DSCR >= policy minimum), not left as an untraceable constant.
"""

from dataclasses import dataclass
from typing import Optional

from openpyxl import load_workbook

LIMIT_SETTING_SHEET = "6-Limit Setting"


@dataclass(frozen=True)
class LimitSettingPolicy:
    requested_amount: float
    interest_rate: float
    tenor_years: int
    policy_minimum_dscr: float


@dataclass(frozen=True)
class AmortizationYear:
    year: int
    opening_balance: float
    interest: float
    principal: float
    closing_balance: float


@dataclass(frozen=True)
class LimitSettingResult:
    annual_instalment: float
    schedule: list
    implied_existing_debt_service: float
    total_proforma_debt_service: float
    ebit: float
    proforma_dscr: float
    proforma_dscr_status: str
    max_serviceable_debt_service: float
    available_additional_debt_service: float
    capacity_cap: float
    recommended_limit: float
    conclusion: object


def load_limit_setting_policy(workbook_path) -> LimitSettingPolicy:
    ws = load_workbook(workbook_path, data_only=True)[LIMIT_SETTING_SHEET]
    return LimitSettingPolicy(
        requested_amount=float(ws["B4"].value),
        interest_rate=float(ws["B5"].value),
        tenor_years=int(ws["B6"].value),
        policy_minimum_dscr=float(ws["B24"].value),
    )


def _annuity_payment(rate, nper, pv):
    # '6-Limit Setting'!B7: =-PMT(B5,B6,B4)
    return pv * rate * (1 + rate) ** nper / ((1 + rate) ** nper - 1)


def _amortization_schedule(loan_amount, rate, nper, instalment):
    schedule = []
    opening = loan_amount
    for year in range(1, nper + 1):
        interest = opening * rate
        principal = instalment - interest
        closing = opening - principal
        schedule.append(AmortizationYear(year, opening, interest, principal, closing))
        opening = closing
    return schedule


def build_limit_setting(policy: LimitSettingPolicy, ebit: float, reported_dscr: float) -> LimitSettingResult:
    instalment = _annuity_payment(policy.interest_rate, policy.tenor_years, policy.requested_amount)
    schedule = _amortization_schedule(policy.requested_amount, policy.interest_rate, policy.tenor_years, instalment)

    implied_existing = ebit / reported_dscr
    total_proforma = implied_existing + instalment
    proforma_dscr = ebit / total_proforma
    proforma_status = "PASS" if proforma_dscr >= policy.policy_minimum_dscr else "FAIL"

    max_serviceable = ebit / policy.policy_minimum_dscr
    available_additional = max_serviceable - implied_existing
    annuity_factor = (1 - (1 + policy.interest_rate) ** -policy.tenor_years) / policy.interest_rate
    capacity_cap = available_additional * annuity_factor

    recommended_limit = min(policy.requested_amount, capacity_cap)
    # '6-Limit Setting'!B40: =IF(B39=B4,"PASS")  -  no false-branch in the
    # reference workbook itself; Excel returns boolean FALSE in that case.
    conclusion = "PASS" if recommended_limit == policy.requested_amount else False

    return LimitSettingResult(
        annual_instalment=instalment,
        schedule=schedule,
        implied_existing_debt_service=implied_existing,
        total_proforma_debt_service=total_proforma,
        ebit=ebit,
        proforma_dscr=proforma_dscr,
        proforma_dscr_status=proforma_status,
        max_serviceable_debt_service=max_serviceable,
        available_additional_debt_service=available_additional,
        capacity_cap=capacity_cap,
        recommended_limit=recommended_limit,
        conclusion=conclusion,
    )


# ============================================================
# Fund-Based (FB) / Non-Fund-Based (NFB) Limit Assessment
#
# Added alongside (not replacing) the term-loan/DSCR engine above, at
# stakeholder request. Unlike everything else in this module, none of
# these inputs have a source cell in the reference workbook - it has no
# working-capital or trade-finance concept anywhere in its 11 sheets -
# and the OCR-extracted Input Data schema has no Inventory/Receivable/
# Payable Days, Sanctioned Limit, Utilisation, LC or BG fields either.
# So, unlike every other module in this application, these inputs are
# analyst-entered per review (see app.py's Limit Setting and Credit
# Memo pages) rather than read from a workbook or an uploaded PDF.
#
# Formulas are exactly the arithmetic from the stakeholder's worked
# example, generalized to whatever the analyst enters for this
# borrower - nothing more elaborate:
#   NWC cycle days              = Inventory Days + Receivable Days - Payable Days
#   Working-capital requirement = Annual Sales * NWC cycle days / 365
#   Borrower contribution       = Working-capital requirement * Contribution % / 100
#   FB requirement               = Working-capital requirement - Borrower contribution
#   Assessed NFB limit          = Peak LC Requirement + Peak BG Requirement
#
# A field that depends on a missing (None) input is None, never a
# fabricated number - app.py renders None as "Not Available / Requires
# Review" rather than treating a blank entry as zero.
# ============================================================


@dataclass(frozen=True)
class FundBasedInputs:
    annual_sales: Optional[float]
    inventory_days: Optional[float]
    receivable_days: Optional[float]
    payable_days: Optional[float]
    contribution_pct: Optional[float]


@dataclass(frozen=True)
class FundBasedResult:
    nwc_cycle_days: Optional[float]
    wc_requirement: Optional[float]
    borrower_contribution_amount: Optional[float]
    fb_requirement: Optional[float]


@dataclass(frozen=True)
class NonFundBasedInputs:
    annual_imports: Optional[float]
    peak_lc_requirement: Optional[float]
    contractual_obligations: Optional[float]
    peak_bg_requirement: Optional[float]


@dataclass(frozen=True)
class NonFundBasedResult:
    assessed_nfb_limit: Optional[float]


def build_fund_based_limit(inputs: FundBasedInputs) -> FundBasedResult:
    if (
        inputs.inventory_days is None
        or inputs.receivable_days is None
        or inputs.payable_days is None
    ):
        nwc_cycle_days = None
    else:
        nwc_cycle_days = inputs.inventory_days + inputs.receivable_days - inputs.payable_days

    if inputs.annual_sales is None or nwc_cycle_days is None:
        wc_requirement = None
    else:
        wc_requirement = inputs.annual_sales * nwc_cycle_days / 365

    if wc_requirement is None or inputs.contribution_pct is None:
        borrower_contribution_amount = None
    else:
        borrower_contribution_amount = wc_requirement * inputs.contribution_pct / 100

    if wc_requirement is None or borrower_contribution_amount is None:
        fb_requirement = None
    else:
        fb_requirement = wc_requirement - borrower_contribution_amount

    return FundBasedResult(
        nwc_cycle_days=nwc_cycle_days,
        wc_requirement=wc_requirement,
        borrower_contribution_amount=borrower_contribution_amount,
        fb_requirement=fb_requirement,
    )


def build_non_fund_based_limit(inputs: NonFundBasedInputs) -> NonFundBasedResult:
    if inputs.peak_lc_requirement is None or inputs.peak_bg_requirement is None:
        assessed_nfb_limit = None
    else:
        assessed_nfb_limit = inputs.peak_lc_requirement + inputs.peak_bg_requirement

    return NonFundBasedResult(assessed_nfb_limit=assessed_nfb_limit)
