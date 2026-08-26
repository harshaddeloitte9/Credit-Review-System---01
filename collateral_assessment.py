"""
Area 5 - Collateral Assessment.

Reference workbook dependency (verified from actual formulas):
    '5-Collateral Assessment'!D22 = '1-Input Data'!B30   (Total Debt)

This is a DIRECT dependency on Input Data - the reference workbook's own
'5-Collateral Assessment' sheet does not go through '2-Financial Analysis'
for this figure, so this module consumes input_data_engine.py's
already-computed Total Debt (the frozen module) rather than Financial
Analysis's.

Per-asset-class book values and haircuts (B15:C19) are hardcoded, entity-
specific numbers in the reference workbook - not formulas, and not
derivable from '1-Input Data' (which has no Trade Receivables, Investments-
beyond-current, or Inventory fields at all, and whose Cash/PPE figures do
not tie exactly to these rounded collateral-schedule numbers either).

These are Tata Steel's OWN collateral schedule, not generic reference/
policy data - unlike '2-Financial Analysis'!G7:G26 (thresholds) or
'3-Risk Scoring' weights, there is no sense in which every borrower should
see the same numbers here. A PDF-uploaded borrower's merged workbook still
carries this sheet unchanged from the policy workbook (demo_source.py only
replaces '1-Input Data'), so app.py deliberately does NOT call
load_collateral_inputs() for a PDF-uploaded borrower (source_mode="pdf") -
only for Advanced/uploaded-workbook mode, where the analyst supplies a
complete, independent workbook that may carry its own genuine collateral
sheet. See app.py's Collateral Assessment page and Credit Memo section.

Formulas reproduced exactly as found in '5-Collateral Assessment':
    Adjusted Value              = Book Value * (1 - Haircut% / 100)   (D15:D19)
    Total Adjusted Collateral   = SUM(Adjusted Value)                 (D21)
    Collateral Coverage Ratio   = Total Adjusted / Debt Exposure      (D23)
    Collateral Rating           = >125 -> C28 text                    (D24)
                                   100-125 -> C29 text
                                   <100 -> C30 text
    (the 125/100 cut-points are embedded in the reference workbook's own
    IF formula, not in separate policy cells - transcribed here with the
    cell reference documented, same treatment as risk_scoring.py's banded
    IF-ladders; the band label TEXT itself is still read live from C28:C30,
    not retyped.)

DISCOVERED REFERENCE-WORKBOOK INCONSISTENCY (reproduced faithfully, not
"fixed" - flagged in the UI): D23 (Collateral Coverage Ratio) is a raw
ratio (2.05 for ~205% coverage), but D24's IF formula compares D23
directly against 125 and 100 as if it were already a percentage number
(i.e. as if D23 were 205, not 2.05). Since a realistic ratio is never
>100, the reference workbook's own D24 always lands in the "<100" band
("High collateral risk") regardless of how strong coverage actually is -
confirmed against the reference workbook's own cached D24 value, which is
"High collateral risk" despite ~205% coverage. This looks like a missing
"D23*100" (or thresholds that should have been 1.25/1.00) in the source
workbook. Reproduced exactly as written for parity.
"""

from dataclasses import dataclass
from typing import Optional

from openpyxl import load_workbook

COLLATERAL_SHEET = "5-Collateral Assessment"

_ASSET_ROWS = [15, 16, 17, 18, 19]


@dataclass(frozen=True)
class CollateralAsset:
    label: str
    book_value: float
    haircut_pct: float
    liquidity: str
    security_rank: str
    assessment: str
    credit_relevance: str
    adjusted_value: float


@dataclass(frozen=True)
class CollateralInputs:
    assets_raw: list          # [(label, book_value, haircut_pct, liquidity, rank, assessment, relevance)]
    rating_bands: list        # [(band_label, interpretation, credit_view)]
    typical_haircuts: list    # [(collateral_type, typical_haircut_range)] - informational only


@dataclass(frozen=True)
class CollateralResult:
    assets: list
    total_adjusted_value: float
    debt_exposure: Optional[float]
    coverage_ratio: Optional[float]
    rating: str


def load_collateral_inputs(workbook_path):
    """Returns None (not a crash, not a Tata Steel fallback) when this
    workbook has no '5-Collateral Assessment' sheet or is missing its
    book-value/haircut cells - the caller (app.py) is expected to show
    "Collateral data unavailable" rather than calling
    build_collateral_assessment() at all in that case."""
    wb = load_workbook(workbook_path, data_only=True)
    if COLLATERAL_SHEET not in wb.sheetnames:
        return None
    ws = wb[COLLATERAL_SHEET]

    assets_raw = []
    for row in _ASSET_ROWS:
        book_value, haircut = ws[f"B{row}"].value, ws[f"C{row}"].value
        if book_value is None or haircut is None:
            return None
        try:
            book_value, haircut = float(book_value), float(haircut)
        except (TypeError, ValueError):
            return None
        assets_raw.append((
            ws[f"A{row}"].value,
            book_value,
            haircut,
            ws[f"E{row}"].value,
            ws[f"F{row}"].value,
            ws[f"G{row}"].value,
            ws[f"H{row}"].value,
        ))

    rating_bands = [
        (ws[f"A{row}"].value, ws[f"B{row}"].value, ws[f"C{row}"].value)
        for row in (28, 29, 30)
    ]

    typical_haircuts = [
        (ws[f"A{row}"].value, ws[f"B{row}"].value)
        for row in range(4, 11)
    ]

    return CollateralInputs(assets_raw=assets_raw, rating_bands=rating_bands, typical_haircuts=typical_haircuts)


def build_collateral_assessment(inputs: CollateralInputs, debt_exposure: Optional[float]) -> CollateralResult:
    assets = []
    total_adjusted = 0.0
    for label, book_value, haircut_pct, liquidity, rank, assessment, relevance in inputs.assets_raw:
        adjusted_value = book_value * (1 - haircut_pct / 100)
        total_adjusted += adjusted_value
        assets.append(CollateralAsset(
            label=label, book_value=book_value, haircut_pct=haircut_pct,
            liquidity=liquidity, security_rank=rank, assessment=assessment,
            credit_relevance=relevance, adjusted_value=adjusted_value,
        ))

    # debt_exposure (fa.derived.total_debt.current) requires Borrowings
    # (current & non-current) and Lease Liabilities (current & non-current)
    # to all be extractable - not guaranteed for every document. Coverage
    # ratio/rating are not derivable without it - "Not available", not a
    # divide-by-None crash or a guessed rating.
    if debt_exposure is None:
        return CollateralResult(
            assets=assets, total_adjusted_value=total_adjusted,
            debt_exposure=None, coverage_ratio=None, rating="Not available",
        )

    coverage_ratio = total_adjusted / debt_exposure

    # '5-Collateral Assessment'!D24: =IF(D23>125,C28,IF(D23>=100,C29,C30)).
    # Compares the raw ratio (e.g. 2.05) against 125/100 exactly as the
    # reference workbook does - see the module docstring for the unit-
    # mismatch this produces. Not "corrected" to *100 or to 1.25/1.00.
    band_by_label = {label: (interpretation, credit_view) for label, interpretation, credit_view in inputs.rating_bands}
    if coverage_ratio > 125:
        rating = band_by_label[">125"][1]
    elif coverage_ratio >= 100:
        rating = band_by_label["100-125"][1]
    else:
        rating = band_by_label["<100"][1]

    return CollateralResult(
        assets=assets,
        total_adjusted_value=total_adjusted,
        debt_exposure=debt_exposure,
        coverage_ratio=coverage_ratio,
        rating=rating,
    )
