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
sheet - or, for a PDF-uploaded borrower, automatically from the analyst
workbook (ANALYST_WORKBOOK_PATH) when verify_workbook_borrower() confirms
its own '1-Input Data' figures are that borrower's (see
analyst_collateral_for_borrower). See app.py's Collateral Assessment page
and Credit Memo section.

Formulas reproduced exactly as found in '5-Collateral Assessment':
    Adjusted Value              = Book Value * (1 - Haircut% / 100)   (D15:D19)
    Total Adjusted Collateral   = SUM(Adjusted Value)                 (D21)
    Collateral Coverage Ratio   = Total Adjusted / Debt Exposure      (D23)
    Collateral Rating           = >1.25x -> C28 text                   (D24)
                                   1.00x-1.25x -> C29 text
                                   <1.00x -> C30 text
    (the cut-points are embedded in the reference workbook's own IF
    formula as 125/100 - a percentage-shaped literal - not in separate
    policy cells; compared here in the same unit as the ratio itself
    (1.25/1.00) rather than against the raw ratio, see the unit-mismatch
    fix above. The band label TEXT itself is still read live from
    C28:C30, not retyped.)

FIXED UNIT MISMATCH (previously reproduced faithfully from the reference
workbook - now corrected): D23 (Collateral Coverage Ratio) is a raw ratio
(2.05 for ~205% coverage), but D24's IF formula in the reference workbook
compares D23 directly against 125 and 100 as if it were already a
percentage number (i.e. as if D23 were 205, not 2.05). Since a realistic
ratio is never >100, the reference workbook's own D24 always lands in the
"<100" band ("High collateral risk") regardless of how strong coverage
actually is - confirmed against the reference workbook's own cached D24
value, which is "High collateral risk" despite ~205% coverage.

This is a genuine defect, not a borrower-specific quirk, so it is no
longer reproduced: the band comparison below uses the SAME unit as
coverage_ratio itself (a raw multiple - 1.25 / 1.00, not 125 / 100),
never rescaling coverage_ratio and never touching how it's displayed
(still "2.05x", same as before) - only the threshold comparison changes.
"""

import os
from dataclasses import dataclass
from typing import Optional

from openpyxl import load_workbook

# Same cut-points as the reference workbook's own D24 formula (125% /
# 100% coverage), expressed as a raw multiple (1.25 / 1.00) - the same
# unit coverage_ratio is already in - rather than the workbook's
# percentage-shaped 125/100 literals. See build_collateral_assessment().
_COVERAGE_STRONG_THRESHOLD = 1.25
_COVERAGE_ADEQUATE_THRESHOLD = 1.00

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


@dataclass(frozen=True)
class CollateralReference:
    """Generic guidance from '5-Collateral Assessment' that is the same for
    every borrower. Deliberately has NO book values, schedule haircuts or
    assessment text - those cells (B, C, G of rows 15-19) are the reference
    workbook's own (Tata Steel's) schedule and are never read here."""
    asset_classes: list       # [(collateral_type, liquidity, security_rank)]
    rating_bands: list        # [(band_label, interpretation, credit_view)]
    typical_haircuts: list    # [(collateral_type, typical_haircut_range)]


def load_collateral_reference(workbook_path):
    """Reference-only view for a borrower with no collateral data of its
    own (e.g. a PDF upload). Returns None if the sheet or its guidance
    cells are missing."""
    wb = load_workbook(workbook_path, data_only=True)
    if COLLATERAL_SHEET not in wb.sheetnames:
        return None
    ws = wb[COLLATERAL_SHEET]
    asset_classes = [(ws[f"A{row}"].value, ws[f"E{row}"].value, ws[f"F{row}"].value) for row in _ASSET_ROWS]
    rating_bands = [(ws[f"A{row}"].value, ws[f"B{row}"].value, ws[f"C{row}"].value) for row in (28, 29, 30)]
    typical_haircuts = [(ws[f"A{row}"].value, ws[f"B{row}"].value) for row in range(4, 11)]
    if not all(label for label, _l, _r in asset_classes) or not all(b[0] for b in rating_bands):
        return None
    return CollateralReference(asset_classes=asset_classes, rating_bands=rating_bands, typical_haircuts=typical_haircuts)


# Core statement lines used to confirm that an analyst's collateral
# workbook belongs to the borrower whose PDF was uploaded: the workbook's
# own '1-Input Data' current-year figures (column B of the template row,
# see ocr_pipeline.ROW_MAPPING) must equal the figures extracted from that
# PDF. Different borrowers' statements never agree on all of these.
IDENTITY_FIELDS = (
    "Revenue from operations", "Other income", "Total expenses", "Property, plant",
    "Total assets", "Total equity", "Borrowings non current", "Cash",
)
MIN_IDENTITY_MATCHES = 5


def verify_workbook_borrower(workbook_path, extracted):
    """(True, detail) only if the workbook's '1-Input Data' current-year
    figures equal this borrower's PDF-extracted figures on at least
    MIN_IDENTITY_MATCHES core statement lines, with no line disagreeing.
    (False, reason) otherwise. Reads no collateral values."""
    import ocr_pipeline

    wb = load_workbook(workbook_path, data_only=True)
    if "1-Input Data" not in wb.sheetnames or COLLATERAL_SHEET not in wb.sheetnames:
        return False, "the workbook needs both a '1-Input Data' and a '5-Collateral Assessment' sheet"
    ws = wb["1-Input Data"]
    matched, mismatched = [], []
    for field in IDENTITY_FIELDS:
        row = ocr_pipeline.ROW_MAPPING.get(field)
        pdf_value = (extracted or {}).get(field, {}).get("fy_current")
        wb_value = ws[f"B{row}"].value if row else None
        if pdf_value is None or not isinstance(wb_value, (int, float)) or isinstance(wb_value, bool):
            continue
        (matched if abs(wb_value - pdf_value) <= 0.01 + 1e-6 * abs(wb_value) else mismatched).append(field)
    borrower = ws["A1"].value
    if mismatched:
        return False, (f"its financial statement figures do not match this borrower's PDF "
                       f"({len(mismatched)} of {len(matched) + len(mismatched)} core figures differ)")
    if len(matched) < MIN_IDENTITY_MATCHES:
        return False, (f"too few core figures could be compared to confirm it is the same borrower "
                       f"({len(matched)} of the {MIN_IDENTITY_MATCHES} required)")
    return True, f"{len(matched)} of {len(matched)} core statement figures match this borrower's PDF" + (
        f" · workbook: {borrower}" if isinstance(borrower, str) and borrower.strip() else "")


# The analyst-prepared workbook shipped with the app (the same file
# demo_source.py merges policy sheets from). Its own '1-Input Data' sheet
# - not the merged workbook's, which is overwritten with the uploaded
# borrower's figures - identifies which borrower its collateral schedule
# belongs to.
ANALYST_WORKBOOK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Credit Review Automation Example.xlsx")


def analyst_collateral_for_borrower(extracted, workbook_path=None):
    """The analyst workbook's collateral inputs for a PDF-uploaded borrower,
    used automatically - but ONLY when verify_workbook_borrower() confirms
    the workbook's own statement figures are this borrower's. Any other
    borrower gets None (the page then shows its unavailable/reference-only
    view), so the schedule can never appear for a different company."""
    path = workbook_path or ANALYST_WORKBOOK_PATH
    if not path or not os.path.exists(path):
        return None
    ok, _detail = verify_workbook_borrower(path, extracted)
    return load_collateral_inputs(path) if ok else None


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

    # '5-Collateral Assessment'!D24: =IF(D23>125,C28,IF(D23>=100,C29,C30)) -
    # but D23 (coverage_ratio) is a raw multiple (2.05 for ~205% cover),
    # and 125/100 are percentage-shaped literals. Comparing them directly,
    # as the reference workbook's own formula does, means a realistic
    # ratio (never >100) always falls into the "<100" band regardless of
    # actual coverage strength (see the module docstring's fixed-unit-
    # mismatch note - confirmed against Tata Steel's own cached D24, which
    # read "High collateral risk" despite ~205% coverage). Fixed by
    # comparing coverage_ratio against the SAME-unit thresholds
    # (1.25 / 1.00) instead - coverage_ratio itself is untouched, so every
    # existing caller/display (still "2.05x") is unaffected.
    rating = _rate_coverage(coverage_ratio, inputs.rating_bands)

    return CollateralResult(
        assets=assets,
        total_adjusted_value=total_adjusted,
        debt_exposure=debt_exposure,
        coverage_ratio=coverage_ratio,
        rating=rating,
    )


def _rate_coverage(coverage_ratio: float, rating_bands) -> str:
    """Shared band lookup for both the "as-is" coverage rating above and
    compute_post_sanction_coverage() below - same thresholds, same
    workbook-sourced band label text, only the ratio being rated differs."""
    band_by_label = {label: (interpretation, credit_view) for label, interpretation, credit_view in rating_bands}
    if coverage_ratio > _COVERAGE_STRONG_THRESHOLD:
        return band_by_label[">125"][1]
    if coverage_ratio >= _COVERAGE_ADEQUATE_THRESHOLD:
        return band_by_label["100-125"][1]
    return band_by_label["<100"][1]


# Basis label shown alongside Post-Sanction Coverage wherever it's
# rendered - named explicitly so the denominator is never ambiguous (see
# compute_post_sanction_coverage()'s own docstring for why FB/NFB are
# excluded from it).
POST_SANCTION_BASIS_LABEL = "Existing Total Debt + Proposed Term Facility Exposure"


@dataclass(frozen=True)
class PostSanctionCoverage:
    post_sanction_exposure: Optional[float]
    coverage_ratio: Optional[float]
    rating: str
    basis_label: str = POST_SANCTION_BASIS_LABEL


def compute_post_sanction_coverage(
    collateral_result: CollateralResult, term_facility_exposure: Optional[float], rating_bands
) -> PostSanctionCoverage:
    """Pro-forma coverage AFTER the proposed TERM facility (see
    proposed_exposure.py's Term Facility Exposure) is added on top of the
    borrower's existing debt - does this SAME collateral schedule still
    adequately cover the combined post-sanction term-debt load, not just
    what's already on the balance sheet today (collateral_result's own
    "Existing Coverage" above, entirely untouched by this function).

    Deliberately excludes FB Requirement and NFB Commitment from the
    denominator: this collateral schedule (Plant & Machinery, etc.) is
    fixed-asset security, which conventionally secures the TERM facility;
    working-capital (FB) is typically secured separately (hypothecation of
    stock/receivables) and NFB (LCs/BGs) is a contingent, off-balance-
    sheet commitment - both a different collateral pool this application
    does not model. Folding them into this denominator would overstate
    what this specific schedule is actually covering, so the basis is
    named explicitly (basis_label / POST_SANCTION_BASIS_LABEL) rather than
    left for the reader to guess."""

    if collateral_result.debt_exposure is None or term_facility_exposure is None:
        return PostSanctionCoverage(post_sanction_exposure=None, coverage_ratio=None, rating="Not available")

    post_sanction_exposure = collateral_result.debt_exposure + term_facility_exposure

    if post_sanction_exposure == 0:
        return PostSanctionCoverage(post_sanction_exposure=post_sanction_exposure, coverage_ratio=None, rating="Not available")

    coverage_ratio = collateral_result.total_adjusted_value / post_sanction_exposure
    rating = _rate_coverage(coverage_ratio, rating_bands)

    return PostSanctionCoverage(
        post_sanction_exposure=post_sanction_exposure,
        coverage_ratio=coverage_ratio,
        rating=rating,
    )
