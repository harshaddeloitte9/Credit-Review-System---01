"""
Area-spanning: Requested Amount, Recommended Limit, Sanctioned Amount,
Proposed Funded Exposure, NFB Commitment and Total Proposed Commitment -
the single, explicitly-named set of exposure figures that Limit Setting,
Portfolio Monitoring, Collateral Assessment and Credit Memo now all share,
rather than each independently reading (or silently duplicating) its own
copy of "the loan amount".

Deliberately NOT one single aggregate. FB Requirement (Fund-Based, an
on-balance-sheet working-capital facility) and the NFB Assessed Limit
(Non-Fund-Based - LCs/BGs, a contingent, off-balance-sheet commitment)
are genuinely different risk types, so they are never silently merged
into one undifferentiated number:

  Requested Amount            - analyst's ask for the term facility, this deal
  Recommended Limit           - MIN(Requested Amount, Capacity Cap)
                                 (limit_setting.py's own formula, unchanged)
  Sanctioned Amount           - optional analyst/committee override;
                                 defaults to Recommended Limit when not set
  Term Facility Exposure      - = Sanctioned Amount (the actual funded
                                 term-loan commitment being proposed)
  Proposed Funded Exposure    - Term Facility Exposure + FB Requirement
  NFB Commitment              - the NFB Assessed Limit, kept separate -
                                 never added into "funded" exposure
  Total Proposed Commitment   - Proposed Funded Exposure + NFB Commitment

This module does no mode-detection (PDF-upload vs Advanced/uploaded-
workbook) and reads no workbook itself - app.py resolves, per
source_mode, what `requested_amount` and `sanctioned_override` actually
are (analyst session-state input for PDF mode; the active workbook's own
cell, unchanged, for Advanced mode) and passes them in already resolved.
Every field here is None (never 0, never guessed) the moment an input it
depends on is unavailable - the same convention input_data_engine.py uses
throughout for exactly the same reason.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ProposedExposure:
    requested_amount: Optional[float]
    recommended_limit: Optional[float]
    sanctioned_amount: Optional[float]
    term_facility_exposure: Optional[float]
    fb_requirement: Optional[float]
    proposed_funded_exposure: Optional[float]
    nfb_commitment: Optional[float]
    total_proposed_commitment: Optional[float]


def build_proposed_exposure(
    requested_amount: Optional[float],
    recommended_limit: Optional[float],
    sanctioned_override: Optional[float],
    fb_requirement: Optional[float],
    nfb_commitment: Optional[float],
) -> ProposedExposure:
    """Pure aggregation - no workbook/session-state access. `recommended_limit`
    is None when Limit Setting's own term-loan engine couldn't run for this
    document (EBIT/reported DSCR unavailable - see limit_setting.py/app.py's
    existing guard); `sanctioned_override` is None whenever the analyst has
    not entered one (the normal case - Sanctioned Amount then simply IS the
    Recommended Limit, not a separate fabricated figure)."""

    sanctioned_amount = (
        sanctioned_override if sanctioned_override is not None else recommended_limit
    )
    term_facility_exposure = sanctioned_amount

    proposed_funded_exposure = (
        term_facility_exposure + fb_requirement
        if term_facility_exposure is not None and fb_requirement is not None
        else None
    )

    total_proposed_commitment = (
        proposed_funded_exposure + nfb_commitment
        if proposed_funded_exposure is not None and nfb_commitment is not None
        else None
    )

    return ProposedExposure(
        requested_amount=requested_amount,
        recommended_limit=recommended_limit,
        sanctioned_amount=sanctioned_amount,
        term_facility_exposure=term_facility_exposure,
        fb_requirement=fb_requirement,
        proposed_funded_exposure=proposed_funded_exposure,
        nfb_commitment=nfb_commitment,
        total_proposed_commitment=total_proposed_commitment,
    )


@dataclass(frozen=True)
class ExposureReconciliation:
    status: str    # "Reconciled" / "Not Reconciled" / "Not Available"
    note: str


# Float-equality safety margin only (not a real materiality threshold) -
# the two figures being compared should either be IDENTICAL by
# construction (PDF mode, post-wiring) or come from genuinely independent
# sources (Advanced mode) where any real difference is worth surfacing.
_RECONCILIATION_TOLERANCE = 0.01


def check_exposure_reconciliation(
    exposure: ProposedExposure, portfolio_proposed_loan_amount: Optional[float]
) -> ExposureReconciliation:
    """Compares Limit Setting's own Total Proposed Commitment against
    whatever figure Portfolio Monitoring is actually using as its
    "proposed loan amount" for this same borrower.

    In PDF-upload mode these are wired to be the identical value by
    construction (see app.py) - this reconciles trivially there, which is
    itself worth asserting in tests as a regression guard. In Advanced/
    uploaded-workbook mode, Portfolio Monitoring keeps reading its own
    workbook cell unchanged - a genuinely independent figure - so this is
    where a real mismatch can surface. Always informational ("something to
    verify"), never treated as an error that blocks anything - an
    uploaded workbook's own figures are never overridden based on this
    check."""

    if exposure.total_proposed_commitment is None or portfolio_proposed_loan_amount is None:
        return ExposureReconciliation(
            "Not Available",
            "Cannot reconcile - Total Proposed Commitment and/or Portfolio Monitoring's proposed "
            "loan amount is not available for this borrower.",
        )

    diff = exposure.total_proposed_commitment - portfolio_proposed_loan_amount

    if abs(diff) <= _RECONCILIATION_TOLERANCE:
        return ExposureReconciliation(
            "Reconciled",
            f"Limit Setting's Total Proposed Commitment (₹{exposure.total_proposed_commitment:,.0f} Cr) "
            f"matches Portfolio Monitoring's proposed loan amount.",
        )

    return ExposureReconciliation(
        "Not Reconciled",
        f"Limit Setting's Total Proposed Commitment (₹{exposure.total_proposed_commitment:,.0f} Cr) differs "
        f"from Portfolio Monitoring's proposed loan amount (₹{portfolio_proposed_loan_amount:,.0f} Cr) - "
        f"review which figure is authoritative for this analysis.",
    )
