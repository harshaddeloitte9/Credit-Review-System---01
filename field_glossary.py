"""
Centralized glossary of plain-English definitions for the Credit Review
Tool's template fields - the exact field names ocr_pipeline.py's
ROW_MAPPING/EXTRA_FIELDS use throughout the rest of this app (the same
strings already shown in the "Field" column of the Data Review page's
Field-Level Review table).

Purely a reference lookup for the UI - never touches extraction,
aliases, or any calculation. get_definition() never guesses: a field
with no configured entry gets a neutral fallback, not an invented
definition.
"""

FIELD_DEFINITIONS = {
    # ---- Profit & Loss --------------------------------------------
    "Revenue from operations": "Income earned from the company's principal business activities.",
    "Other income": "Income from activities outside the company's principal operations.",
    "Total income": "Total income from operations and other income, according to the applicable statement definition.",
    "Total expenses": "Total expenses recognized in the relevant income statement.",
    "Finance costs": "Interest and other financing costs associated with borrowings.",
    "Depreciation": "Allocation of the cost of tangible and intangible assets over their useful lives.",
    "Exceptional items": "Significant items presented separately because of their nature or impact.",
    "Total tax expense": "Tax expense recognized for the relevant reporting period.",
    "Total comprehensive income": "Net profit plus other comprehensive income/loss for the period.",
    "Profit before exceptional items": "Profit before tax and before any separately-disclosed exceptional items.",
    "Profit before tax": "Profit for the period before deducting tax expense.",
    "Net profit": "Profit for the period after tax, attributable to the reporting entity.",

    # ---- Balance Sheet ----------------------------------------------
    "Property, plant": "Property, Plant & Equipment - tangible long-term assets used in business operations.",
    "Total assets": "Total assets reported on the balance sheet.",
    "Total equity": "Equity attributable to the relevant reporting entity, according to the statement definition.",
    "Borrowings non current": "Long-term borrowings due for repayment beyond the next 12 months.",
    "Borrowings current": "Borrowings due for repayment within the next 12 months.",
    "Lease liabilities non current": "Long-term outstanding obligations arising from lease arrangements.",
    "Lease liabilities current": "Lease obligations due for repayment within the next 12 months.",
    "Current investments": "Short-term investments expected to be realized within 12 months.",
    "Cash": "Cash and cash equivalents held by the company.",
    "Other balances": "Other bank balances not classified as cash and cash equivalents.",
    "Current assets": "Assets classified as current under the applicable accounting presentation.",
    "Current liabilities": "Liabilities classified as current under the applicable accounting presentation.",
    "Total liabilities": "Total liabilities reported on the balance sheet.",

    # ---- Cash Flow ----------------------------------------------------
    "Interest income": "Income earned from interest-bearing deposits or investments.",
    "Operating cash flow": "Net cash generated or used by operating activities.",
    "Income taxes paid": "Cash taxes paid during the reporting period.",
    "Interest paid": "Cash interest paid during the reporting period.",
    "Repayment borrowings": "Cash repayments of long-term borrowing principal during the period.",
    "Lease payment": "Cash payments made against lease liabilities during the period.",
    "Capex": "Capital Expenditure - expenditure on acquiring or improving long-term assets.",
    "Dividend": "Cash dividends paid during the reporting period.",

    # ---- Ratios ---------------------------------------------------------
    "DSCR": "Debt Service Coverage Ratio - a measure of the ability to cover debt-service obligations (principal and interest).",
    "ISCR": "Interest Service Coverage Ratio - a measure of the ability to cover interest obligations.",
    "Current ratio": "A measure comparing current assets with current liabilities.",
    "Debt equity": "Debt-to-Equity Ratio - a ratio comparing total debt with the applicable measure of equity.",
    "Debt assets": "A ratio comparing total debt with total assets.",
    "Operating EBITDA margin": "EBITDA expressed as a proportion of the applicable revenue measure.",
    "Net profit margin": "Net profit expressed as a proportion of the applicable revenue measure.",
    "Net worth": "The applicable equity or net-worth measure defined by the reporting source and existing calculation methodology.",
    "Debtors turnover": "A measure of how quickly the company collects its receivables, relative to sales.",
    "Inventory turnover": "A measure of how quickly inventory is sold and replaced.",
}

_FALLBACK_DEFINITION = "Definition not configured"


def get_definition(field):
    """Looks up a plain-English definition for a template field name.
    Tries an exact match first, then a normalized (trimmed, case-
    insensitive) match - never invents a definition. Returns the
    neutral fallback string for any field with no configured entry."""
    if not field:
        return _FALLBACK_DEFINITION
    if field in FIELD_DEFINITIONS:
        return FIELD_DEFINITIONS[field]
    normalized = field.strip().lower()
    for key, definition in FIELD_DEFINITIONS.items():
        if key.strip().lower() == normalized:
            return definition
    return _FALLBACK_DEFINITION
