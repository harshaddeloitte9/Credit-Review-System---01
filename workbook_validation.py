"""
Pre-flight structural validation for an uploaded Credit Review workbook, run
BEFORE any calculation module sees the file.

Two severities:
  - errors:   the workbook cannot be analyzed at all - missing '1-Input
              Data' (or its own required cells/policy cells, checked via
              reference_data.load_reference_bundle's own defensive
              validation), or missing the '4-Covenants' / '6-Limit
              Setting' / '7-Continuous Monitoring' policy cells those
              modules need to run. Upload is rejected outright with a
              plain-language list of what's missing.
  - warnings: the workbook is analyzable, but '5-Collateral Assessment',
              '8-Portfolio Monitoring' and/or '9 - Credit Memo' lack their
              own entity-specific data - those modules will show
              "unavailable" rather than being derived or backfilled from
              the reference (Tata Steel) workbook.

Nothing here performs a financial calculation. It only confirms the cells
the existing loaders (reference_data.py, covenants.py, limit_setting.py,
continuous_monitoring.py, collateral_assessment.py, portfolio_monitoring.py,
credit_memo.py) are about to read actually exist and are the right shape,
so a bad upload produces one clear message instead of a raw Python
traceback reaching the UI.
"""

from dataclasses import dataclass, field

from openpyxl import load_workbook

from reference_data import load_reference_bundle, ReferenceDataError

COVENANTS_SHEET = "4-Covenants"
COLLATERAL_SHEET = "5-Collateral Assessment"
LIMIT_SETTING_SHEET = "6-Limit Setting"
CONTINUOUS_MONITORING_SHEET = "7-Continuous Monitoring"
PORTFOLIO_MONITORING_SHEET = "8-Portfolio Monitoring"
CREDIT_MEMO_SHEET = "9 - Credit Memo"

# Cell lists mirror exactly what each module's own load_* function reads
# (covenants.py, limit_setting.py, continuous_monitoring.py,
# collateral_assessment.py, portfolio_monitoring.py) - kept in sync with
# those loaders, not re-deriving the business logic itself.
_COVENANTS_CELLS = ["B11", "B12"]
_LIMIT_SETTING_CELLS = ["B4", "B5", "B6", "B24"]
_MONITORING_CELLS = ["B4", "B5", "B6"]
_COLLATERAL_ASSET_ROWS = [15, 16, 17, 18, 19]
_PORTFOLIO_NUMERIC_CELLS = ["C8", "C9", "C11", "C13"]


@dataclass(frozen=True)
class ValidationReport:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def is_valid(self):
        return not self.errors


def _numeric_cells_missing(ws, coords):
    missing = []
    for coord in coords:
        value = ws[coord].value
        if value is None or (isinstance(value, str) and value.strip().startswith("=")):
            missing.append(coord)
            continue
        try:
            float(value)
        except (TypeError, ValueError):
            missing.append(coord)
    return missing


def validate_workbook(path) -> ValidationReport:
    errors = []
    warnings = []

    try:
        wb = load_workbook(path, data_only=True)
    except Exception as exc:
        return ValidationReport(errors=[f"Could not open this file as an Excel workbook ({exc})."])

    # --- Hard requirement: '1-Input Data' + '2-Financial Analysis' /
    # '3-Risk Scoring' policy cells, via reference_data.py's own defensive
    # checks - if a bundle can't be built, nothing downstream can run.
    try:
        load_reference_bundle(path=path)
    except ReferenceDataError as exc:
        errors.append(str(exc))
        return ValidationReport(errors=errors, warnings=warnings)

    # --- Hard requirement: modules 4, 6, 7's own policy cells ---
    if COVENANTS_SHEET not in wb.sheetnames:
        errors.append(f"Missing required sheet '{COVENANTS_SHEET}' (needed for the Covenants module).")
    else:
        missing = _numeric_cells_missing(wb[COVENANTS_SHEET], _COVENANTS_CELLS)
        if missing:
            errors.append(f"'{COVENANTS_SHEET}' is missing policy cells: {', '.join(missing)} (DSCR/ISCR minimums).")

    if LIMIT_SETTING_SHEET not in wb.sheetnames:
        errors.append(f"Missing required sheet '{LIMIT_SETTING_SHEET}' (needed for the Limit Setting module).")
    else:
        missing = _numeric_cells_missing(wb[LIMIT_SETTING_SHEET], _LIMIT_SETTING_CELLS)
        if missing:
            errors.append(f"'{LIMIT_SETTING_SHEET}' is missing deal-parameter cells: {', '.join(missing)}.")

    if CONTINUOUS_MONITORING_SHEET not in wb.sheetnames:
        errors.append(f"Missing required sheet '{CONTINUOUS_MONITORING_SHEET}' (needed for Continuous Monitoring).")
    else:
        missing = _numeric_cells_missing(wb[CONTINUOUS_MONITORING_SHEET], _MONITORING_CELLS)
        if missing:
            errors.append(f"'{CONTINUOUS_MONITORING_SHEET}' is missing threshold cells: {', '.join(missing)}.")

    # --- Soft/optional: module 5 (Collateral) ---
    if COLLATERAL_SHEET not in wb.sheetnames:
        warnings.append(f"'{COLLATERAL_SHEET}' not found - Collateral Assessment will show 'unavailable'.")
    else:
        coords = [f"{col}{row}" for row in _COLLATERAL_ASSET_ROWS for col in ("B", "C")]
        missing = _numeric_cells_missing(wb[COLLATERAL_SHEET], coords)
        if missing:
            warnings.append(
                f"'{COLLATERAL_SHEET}' is missing book value / haircut cells: {', '.join(missing)} - "
                f"Collateral Assessment will show 'unavailable'."
            )

    # --- Soft/optional: module 8 (Portfolio Monitoring) ---
    if PORTFOLIO_MONITORING_SHEET not in wb.sheetnames:
        warnings.append(f"'{PORTFOLIO_MONITORING_SHEET}' not found - Portfolio Monitoring will show 'unavailable'.")
    else:
        ws = wb[PORTFOLIO_MONITORING_SHEET]
        missing = _numeric_cells_missing(ws, _PORTFOLIO_NUMERIC_CELLS)
        if ws["C7"].value in (None, ""):
            missing = ["C7"] + missing
        if missing:
            warnings.append(
                f"'{PORTFOLIO_MONITORING_SHEET}' is missing input cells: {', '.join(missing)} - "
                f"Portfolio Monitoring will show 'unavailable'."
            )

    # --- Soft/optional: module 9 narrative ---
    if CREDIT_MEMO_SHEET not in wb.sheetnames:
        warnings.append(
            f"'{CREDIT_MEMO_SHEET}' not found - Credit Memo will show live computed statuses "
            f"only, with no reference narrative alongside them."
        )

    return ValidationReport(errors=errors, warnings=warnings)
