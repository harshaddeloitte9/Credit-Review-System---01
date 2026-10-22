"""
Final Credit Decision (Part 6, final integration pass).

A single, explicit, traceable REJECT / REVIEW REQUIRED / CONDITIONAL
APPROVAL / APPROVE verdict for the Credit Memo, built ONLY from results
each module has ALREADY computed on its own (financial_analysis.py,
risk_scoring.py, covenants.py, collateral_assessment.py,
continuous_monitoring.py) plus the Data Review page's own count of
still-unresolved human-review items. No new credit-policy arithmetic is
introduced here - this is a precedence GATE over verdicts that already
exist, so every reason the memo shows traces back to a real, existing
module result.

Precedence (most severe tier wins; every reason at every tier is still
recorded, not just the winning tier's):
  REJECT               - a hard stop: Financial Analysis's own
                         Approve/Reject gate says Reject, a financial
                         covenant is in breach, Continuous Monitoring's
                         aggregate is the FAIL-equivalent ("NOT PASS"),
                         or the overall risk grade is "High Risk".
  REVIEW REQUIRED       - critical data is missing (Financial Analysis,
                         Covenants, or Risk Scoring could not even reach
                         a verdict for this document) or a human
                         reviewer still has an unresolved extraction
                         flag / AI correction proposal on the Data
                         Review page.
  CONDITIONAL APPROVAL  - a real but not disqualifying concern: an
                         early-warning trigger is on WATCH LIST, the
                         overall risk grade is "Elevated Risk", or
                         existing collateral coverage is rated "High
                         collateral risk".
  APPROVE               - none of the above triggered.
"""

from dataclasses import dataclass, field

NOT_AVAILABLE = "Not Available / Requires Review"

DECISION_TIERS = ("REJECT", "REVIEW REQUIRED", "CONDITIONAL APPROVAL", "APPROVE")


@dataclass(frozen=True)
class CreditDecisionResult:
    decision: str
    reject_reasons: list = field(default_factory=list)
    review_reasons: list = field(default_factory=list)
    conditional_reasons: list = field(default_factory=list)


def build_credit_decision(fa, risk, covenants, collateral, monitoring,
                           unresolved_review_count=0) -> CreditDecisionResult:
    """Every argument (other than unresolved_review_count) is an
    already-built module result (or None, when that area has no data
    for this workbook - e.g. collateral/monitoring in some Advanced-mode
    workbooks) - this function never recomputes anything from raw
    Input Data itself."""

    reject_reasons = []
    review_reasons = []
    conditional_reasons = []

    # ---- REJECT ----
    if fa is not None and fa.overall_status == "Reject":
        reject_reasons.append(
            "Financial Analysis: one or more critical metrics fail the policy gate (Reject)."
        )
    if covenants is not None and covenants.breach:
        reject_reasons.append(
            "Covenant Monitoring: a financial covenant is in breach (AMBER/RED)."
        )
    if monitoring is not None and monitoring.overall_status == "NOT PASS":
        reject_reasons.append(
            "Continuous Monitoring: the aggregate early-warning status is NOT PASS (a FAIL-level trigger fired)."
        )
    if risk is not None and risk.overall_status == "High Risk":
        reject_reasons.append(
            "Risk Scoring: the overall risk grade is High Risk."
        )

    # ---- REVIEW REQUIRED ----
    if fa is not None and fa.overall_status == NOT_AVAILABLE:
        review_reasons.append(
            "Financial Analysis: critical data is unavailable for this document - Approve/Reject cannot be soundly determined."
        )
    if covenants is not None and covenants.overall_status == NOT_AVAILABLE:
        review_reasons.append(
            "Covenant Monitoring: a required covenant could not be evaluated for this document."
        )
    if risk is not None and risk.overall_status == NOT_AVAILABLE:
        review_reasons.append(
            "Risk Scoring: could not be computed - one or more Financial Strength inputs are unavailable."
        )
    if unresolved_review_count > 0:
        review_reasons.append(
            f"{unresolved_review_count} field(s) on the Data Review page still have an unresolved "
            f"extraction flag or AI correction proposal awaiting a human decision."
        )

    # ---- CONDITIONAL APPROVAL ----
    if monitoring is not None and monitoring.overall_status == "WATCH LIST":
        conditional_reasons.append(
            "Continuous Monitoring: one or more early-warning triggers are on WATCH LIST."
        )
    if risk is not None and risk.overall_status == "Elevated Risk":
        conditional_reasons.append(
            "Risk Scoring: the overall risk grade is Elevated Risk."
        )
    if collateral is not None and collateral.rating == "High collateral risk":
        conditional_reasons.append(
            "Collateral Assessment: existing coverage is rated High collateral risk."
        )

    if reject_reasons:
        decision = "REJECT"
    elif review_reasons:
        decision = "REVIEW REQUIRED"
    elif conditional_reasons:
        decision = "CONDITIONAL APPROVAL"
    else:
        decision = "APPROVE"

    return CreditDecisionResult(
        decision=decision,
        reject_reasons=reject_reasons,
        review_reasons=review_reasons,
        conditional_reasons=conditional_reasons,
    )
