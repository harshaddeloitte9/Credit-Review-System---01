"""
Re-implements '3-Risk Scoring' from the reference workbook.

Financial Strength (reference rows 28-35) is the one component the
reference workbook itself derives from ratios - DSCR, ISCR, Current Ratio
and Debt-Equity, each banded 1-5 and weighted 35/25/15/25 - and that part is
computed here exactly as banded in the reference (see the four `_band`
functions below, each annotated with its source cell).

The other seven components (Industry Risk, Management Quality, Leverage,
Liquidity, Debt Service Capacity, Collateral/Asset Coverage, Business
Position) are analyst-assigned qualitative scores in the reference workbook
with no financial-statement formula behind them - the reference workbook's
own 'Sheet1' helper tab says as much ("qualitative factors ... left with the
analyst. The system covers only the quantitative layer."). Those scores are
read live from the reference workbook's policy cells (reference_data.py),
not computed, and are labelled as qualitative policy inputs throughout.

What this module computes programmatically, per the brief, is the
weighting: raw score x weight for all eight components, the total, and the
risk-grade lookup - none of that is hardcoded.
"""

from dataclasses import dataclass

from reference_data import ReferenceBundle


@dataclass(frozen=True)
class FinancialStrengthInput:
    key: str
    label: str
    value: float
    band_rule: str
    score: int
    weight: float


@dataclass(frozen=True)
class RiskComponent:
    key: str
    label: str
    weight_pct: float
    raw_score: float
    weighted_score: float
    basis: str
    source: str


@dataclass(frozen=True)
class RiskScoringResult:
    financial_strength_inputs: list
    financial_strength_score: float
    components: list
    total_weighted_score: float
    risk_grade: str
    risk_interpretation: str
    overall_status: str


def _dscr_band(x):
    # '3-Risk Scoring'!D30: >=2.0x->5, 1.5-2.0x->4, 1.25-1.5x->3, 1.0-1.25x->2, <1.0x->1
    if x >= 2:
        return 5
    if x >= 1.5:
        return 4
    if x >= 1.25:
        return 3
    if x >= 1:
        return 2
    return 1


def _iscr_band(x):
    # '3-Risk Scoring'!D31: >=3x->5, 2-3x->3, 1-2x->2, <1x->1 (no "4" band)
    if x >= 3:
        return 5
    if x >= 2:
        return 3
    if x >= 1:
        return 2
    return 1


def _current_ratio_band(x):
    # '3-Risk Scoring'!D32: >=1.5->5, 1.2-1.5->3, 1.0-1.2->2, <1.0->1 (no "4" band)
    if x >= 1.5:
        return 5
    if x >= 1.2:
        return 3
    if x >= 1:
        return 2
    return 1


def _debt_equity_band(x):
    # '3-Risk Scoring'!D33: <=1.0x->4, 1-2x->3, 2-3x->2, >3x->1 (max score is 4, not 5)
    if x <= 1:
        return 4
    if x <= 2:
        return 3
    if x <= 3:
        return 2
    return 1


_FINANCIAL_STRENGTH_INPUTS = [
    ("dscr", "DSCR (reported)", ">=2.0x->5, 1.5-2.0x->4, 1.25-1.5x->3, 1.0-1.25x->2, <1.0x->1", _dscr_band),
    ("iscr", "ISCR (reported)", ">=3x->5, 2-3x->3, 1-2x->2, <1x->1", _iscr_band),
    ("current_ratio", "Current Ratio (reported)", ">=1.5->5, 1.2-1.5->3, 1.0-1.2->2, <1.0->1", _current_ratio_band),
    ("debt_equity", "Debt-to-Equity (reported basis)", "<=1.0x->4, 1-2x->3, 2-3x->2, >3x->1", _debt_equity_band),
]

_COMPONENT_ORDER = [
    ("financial_strength", "Financial Strength"),
    ("industry_risk", "Industry Risk"),
    ("management_quality", "Management Quality"),
    ("leverage", "Leverage"),
    ("liquidity", "Liquidity"),
    ("debt_service_capacity", "Debt Service Capacity"),
    ("collateral_asset_coverage", "Collateral/Asset Coverage"),
    ("business_position", "Business Position"),
]


def build_risk_scoring(bundle: ReferenceBundle) -> RiskScoringResult:
    d = bundle.input_data
    policy = bundle.risk_policy
    sw = policy.financial_strength_subweights

    ratio_values = {
        "dscr": d.reported_dscr.current,
        "iscr": d.reported_iscr.current,
        "current_ratio": d.reported_current_ratio.current,
        "debt_equity": d.reported_debt_equity.current,
    }

    fs_inputs = []
    for key, label, band_rule, band_fn in _FINANCIAL_STRENGTH_INPUTS:
        value = ratio_values[key]
        score = band_fn(value)
        weight = sw[key]
        fs_inputs.append(FinancialStrengthInput(key, label, value, band_rule, score, weight))

    financial_strength_score = sum(f.score * f.weight for f in fs_inputs)

    components = []
    for key, label in _COMPONENT_ORDER:
        weight_pct = policy.component_weights[key]
        if key == "financial_strength":
            raw_score = financial_strength_score
            basis = "Computed: weighted DSCR/ISCR/Current Ratio/Debt-Equity bands (see above)"
            source = "computed from Input Data"
        else:
            raw_score = policy.qualitative_scores[key]
            basis = "Analyst-assigned qualitative score, read from the reference workbook"
            source = "policy input (qualitative - not derived from Input Data)"
        weighted_score = raw_score * weight_pct / 100
        components.append(RiskComponent(key, label, weight_pct, raw_score, weighted_score, basis, source))

    total_weighted_score = sum(comp.weighted_score for comp in components)

    rounded = round(total_weighted_score)
    grade_row = next((g for g in policy.grade_scale if g["score"] == rounded), None)
    if grade_row is None:
        raise ValueError(
            f"Total weighted score {total_weighted_score:.4f} rounds to {rounded}, which "
            f"falls outside the reference workbook's 1-5 grade scale - cannot look up a Risk "
            f"Grade. This should not happen when component weights sum to 100 and every raw "
            f"score is in 1-5; reporting rather than guessing a grade."
        )

    return RiskScoringResult(
        financial_strength_inputs=fs_inputs,
        financial_strength_score=financial_strength_score,
        components=components,
        total_weighted_score=total_weighted_score,
        risk_grade=grade_row["risk_interpretation"],
        risk_interpretation=grade_row["risk_interpretation"],
        overall_status=grade_row["risk_interpretation"],
    )
