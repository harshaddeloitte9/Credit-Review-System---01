"""
PDF -> OCR (LLMWhisperer) -> Financial Data Extraction -> Excel pipeline.

ISOLATED FROM THE CURRENT DEMO FLOW. The application currently loads its
Tata Steel dataset directly from the business/reference workbook
(reference_data.py) instead of running this pipeline - see app.py. Nothing
in app.py imports this module, and nothing here is called during the
current Financial Analysis / Risk Scoring flow.

This module is kept intact, unmodified in behaviour from the original
single-file app, for future re-integration once OCR extraction quality is
revisited. It targets a different workbook (Credit_Review_Tool_Input_Data_
Template.xlsx, the blank OCR-output template) than the reference workbook
the rest of the app now reads from (Credit Review Automation Example.xlsx).

Nothing in this module is invoked automatically; run_llmwhisperer() is the
only function that calls the LLMWhisperer API, and only when a caller
explicitly invokes it with a PDF path and a configured API key.
"""

import csv
import os
import re
from copy import copy
from datetime import datetime

import streamlit as st
from openpyxl import load_workbook
from openpyxl.styles import Font

from unstract.llmwhisperer import LLMWhispererClientV2
from unstract.llmwhisperer.client_v2 import LLMWhispererClientException  # noqa: F401  (re-exported for callers)


# ============================================================
# LLMWHISPERER CONFIGURATION
#
# The API key is never hardcoded in source. It is loaded from
# (in order of preference):
#   1. Streamlit secrets (.streamlit/secrets.toml -> LLMWHISPERER_API_KEY)
#   2. The LLMWHISPERER_API_KEY environment variable
# It is intentionally never shown in the Streamlit UI, and never
# written to logs or DEBUG_MODE output.
# ============================================================

def _load_llmwhisperer_api_key():

    try:
        if "LLMWHISPERER_API_KEY" in st.secrets:
            return st.secrets["LLMWHISPERER_API_KEY"]
    except Exception:
        # No secrets.toml configured - fall back to the environment.
        pass

    return os.environ.get("LLMWHISPERER_API_KEY", "")


LLMWHISPERER_API_KEY = _load_llmwhisperer_api_key()

# LLMWhisperer V2 API URL

LLMWHISPERER_BASE_URL = (
    "https://llmwhisperer-api.us-central.unstract.com/api/v2"
)

# OCR configuration
OCR_MODE = "high_quality"
OCR_OUTPUT_MODE = "layout_preserving"
OCR_LANGUAGE = "eng"
OCR_WAIT_TIMEOUT = 300


# ============================================================
# DEBUG CONFIGURATION
# ============================================================
#
# Code-level switch only. There is intentionally no Streamlit
# control for this - flip the constant and restart the app.
#
# When enabled, every run writes the raw OCR text and a
# field-by-field validation report (matched source line,
# extracted numbers, selected FY2026/FY2025 values, target
# Excel cell) to DEBUG_OUTPUT_DIR, and shows the same report
# inside an expander in the app. Never includes the API key.

DEBUG_MODE = False

DEBUG_OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "debug_output",
)


# ============================================================
# EXCEL CONFIGURATION
# ============================================================

# Automatically locate the template in the same folder
# as app.py.
APP_DIRECTORY = os.path.dirname(
    os.path.abspath(__file__)
)

TEMPLATE_FILE = os.path.join(
    APP_DIRECTORY,
    "Credit_Review_Tool_Input_Data_Template.xlsx"
)

TARGET_SHEET = "1-Input Data"

OUTPUT_FILE = "Credit_Review_Tool_Input_Data_Extracted.xlsx"


# ============================================================
# FIELD ALIASES
#
# Each field also carries the statement it belongs to, so a
# field can only be matched against OCR lines that fall inside
# that statement's own section of the document. This prevents
# a Balance Sheet line from being matched by a Cash Flow alias
# (or vice versa) purely because of coincidental text overlap.
# ============================================================

# Row on the "1-Input Data" sheet that each field is written to.
# Rows that hold template formulas (Total income, Profit before
# exceptional items, Profit before tax, Net finance charges, EBITDA,
# Total debt, Net debt, Average equity, all validation/reconciliation
# rows) are deliberately absent here - those cells already carry the
# correct formula in the template (e.g. row 12 "=B7-B8", row 14
# "=B12+B13", row 16 "=B14-B15") and must never be overwritten. Each
# is still extracted (see EXTRA_FIELDS) so its reported figure can
# populate the "Reported (validation)" D-column cross-check.
ROW_MAPPING = {

    # Profit & Loss
    "Revenue from operations": 5,
    "Other income": 6,
    "Total expenses": 8,
    "Finance costs": 9,
    "Depreciation": 10,
    "Interest income": 11,
    "Exceptional items": 13,
    "Total tax expense": 15,

    # Balance Sheet
    "Property, plant": 23,
    "Total assets": 24,
    "Total equity": 25,
    "Borrowings non current": 26,
    "Borrowings current": 27,
    "Lease liabilities non current": 28,
    "Lease liabilities current": 29,
    "Current investments": 31,
    "Cash": 32,
    "Other balances": 33,
    "Current assets": 35,
    "Current liabilities": 36,
    "Total liabilities": 37,

    # Cash Flow
    "Operating cash flow": 40,
    "Income taxes paid": 41,
    "Interest paid": 42,
    "Repayment borrowings": 43,
    "Lease payment": 44,
    "Capex": 45,
    "Dividend": 46,
    "Total comprehensive income": 47,

    # Reported Ratios
    "DSCR": 49,
    "ISCR": 50,
    "Current ratio": 51,
    "Debt equity": 52,
    "Debt assets": 53,
    "Operating EBITDA margin": 54,
    "Net profit margin": 55,
    "Net worth": 56,

    # Reported Ratios (continued) - added rows 74 and 78, appended after
    # the sheet's original last row (73) rather than inserted into the
    # block above, so no other module's hardcoded cell reference shifts.
    # Row 78, not 75: the *policy* workbook's own '1-Input Data' sheet has
    # a pre-existing merged footer range at A75:D77 (confirmed by testing
    # the full extraction->merge pipeline, not assumed) - row 75 would
    # have silently failed to merge into any borrower's active dataset.
    # Same Regulation 52(4) disclosure table as DSCR/ISCR/etc. above
    # (items #9 and #10 in that table) - the company's own pre-computed
    # ratio, not something derived here from raw Trade Receivables/
    # Inventory figures (which this template still has no rows for).
    "Debtors turnover": 74,
    "Inventory turnover": 78,
}

# Rows whose Excel number format is a native percentage ("0.00%").
# Excel stores percentages as a fraction of 1 (0.2364, not 23.64),
# so values landing in these rows must be divided by 100 after
# parsing - clean_number() only strips the "%" glyph, it does not
# do the unit conversion.
PERCENTAGE_ROWS = {54, 55}

# Cash outflow lines (taxes paid, interest paid, debt repayment,
# lease principal, capex, dividends) are printed in the statement's
# own Cash Flow schedule as negative/parenthesised outflows, but the
# template stores them as positive magnitudes - downstream ratio
# formulas elsewhere in the workbook (e.g. DSCR = EBIT / (Interest +
# Principal)) depend on these being positive denominators. Net cash
# from operating activities (row 40) is left signed, since a
# genuinely negative operating cash flow is meaningful and is what
# the row 66 validation check ("positive in both years") is testing.
# Interest income (row 11) is the same shape of defect from the
# opposite direction: the indirect-method Cash Flow schedule prints
# it parenthesised as a deduction from PBT (since it is reclassified
# elsewhere in the statement), but the template's own native formula
# for row 17 ("=B9-B11", Net finance charges = Finance costs minus
# Interest income) only reduces net finance charges correctly if B11
# is stored as the plain positive amount of income earned - matching
# how every other income line in this template (e.g. row 6, Other
# income) is stored.
MAGNITUDE_ROWS = {11, 41, 42, 43, 44, 45, 46}

# These fields are not written to their row's B/C columns - each is
# a template formula (Total income = B5+B6, Profit before exceptional
# items = B7-B8, Profit before tax = B12+B13, Net profit = B14-B15).
# They are extracted independently only so that D7/D12/D14/D16 - the
# "Reported (validation)" column - can show the figure as actually
# printed in the statement, giving a real tie-out against the
# computed formula instead of a self-referencing echo.
EXTRA_FIELDS = [
    "Total income",
    "Profit before exceptional items",
    "Profit before tax",
    "Net profit",
]

# Where each EXTRA_FIELDS figure lands - the "Reported (validation)"
# D-column cross-check cell for its row (D7/D12/D14/D16 respectively).
EXTRA_FIELD_CELLS = {
    "Total income": "D7",
    "Profit before exceptional items": "D12",
    "Profit before tax": "D14",
    "Net profit": "D16",
}

FIELD_SECTION = {
    "Revenue from operations": "PNL",
    "Other income": "PNL",
    "Total income": "PNL",
    "Total expenses": "PNL",
    "Finance costs": "PNL",
    "Depreciation": "PNL",
    # Per the template's own row-11 label ("Interest income and income
    # from current investments (per cash flow)"), this field is
    # explicitly defined as the Cash Flow statement's own disclosure,
    # not a P&L breakout - the standalone P&L in this filing has no
    # separate Interest Income line at all (confirmed: the word
    # "interest" does not appear anywhere in the P&L section).
    "Interest income": "CF",
    "Profit before exceptional items": "PNL",
    "Exceptional items": "PNL",
    "Profit before tax": "PNL",
    "Total tax expense": "PNL",
    "Net profit": "PNL",

    "Property, plant": "BS",
    "Total assets": "BS",
    "Total equity": "BS",
    "Borrowings non current": "BS",
    "Borrowings current": "BS",
    "Lease liabilities non current": "BS",
    "Lease liabilities current": "BS",
    "Current investments": "BS",
    "Cash": "BS",
    "Other balances": "BS",
    "Current assets": "BS",
    "Current liabilities": "BS",
    "Total liabilities": "BS",

    "Operating cash flow": "CF",
    "Income taxes paid": "CF",
    "Interest paid": "CF",
    "Repayment borrowings": "CF",
    "Lease payment": "CF",
    "Capex": "CF",
    "Dividend": "CF",
    # Positioned in the template right after the Cash Flow rows (row
    # 47), but the source document itself states this as the closing
    # line of the P&L/Other-Comprehensive-Income statement, not the
    # Cash Flow section - confirmed by its actual OCR location.
    "Total comprehensive income": "PNL",

    "DSCR": "RATIO",
    "ISCR": "RATIO",
    "Current ratio": "RATIO",
    "Debt equity": "RATIO",
    "Debt assets": "RATIO",
    "Operating EBITDA margin": "RATIO",
    "Net profit margin": "RATIO",
    "Net worth": "RATIO",
    "Debtors turnover": "RATIO",
    "Inventory turnover": "RATIO",
}

# Fields whose qualifier ("non-current" / "current") is only stated
# once as a Balance Sheet subheading, not repeated on the item's own
# line - restrict these to the matching subsection found by
# find_bs_subsections() instead of searching the whole BS section.
FIELD_SUBSECTION = {
    "Borrowings non current": "non_current_liabilities",
    "Borrowings current": "current_liabilities",
    "Lease liabilities non current": "non_current_liabilities",
    "Lease liabilities current": "current_liabilities",
    "Current investments": "current_assets",
}

ALIASES = {

    # --------------------------------------------------------
    # PROFIT & LOSS
    # --------------------------------------------------------

    "Revenue from operations": [
        "revenue from operations",
        "revenue from operation",
        "total revenue from operations",
        "sales / income from operations",
        "sales income from operations",
        "sales/income from operations",
    ],

    "Other income": [
        "other income",
    ],

    "Total income": [
        "total income [1 + 2]",
        "total income [1+2]",
        "total income (1+2)",
        "total income",
    ],

    "Total expenses": [
        "total expenses",
        "total expense",
    ],

    "Finance costs": [
        "finance costs",
        "finance cost",
    ],

    "Depreciation": [
        "depreciation and amortisation expense",
        "depreciation and amortisation",
        "depreciation & amortisation",
        "depreciation and amortization expense",
        "depreciation and amortization",
    ],

    "Interest income": [
        "interest income and income from current investments",
        "interest income and income from current investment",
        "interest income",
        "income from current investments",
    ],

    "Profit before exceptional items": [
        "profit / (loss) before exceptional items & tax",
        "profit / loss before exceptional items & tax",
        "profit before exceptional items & tax",
        "profit before exceptional items and tax",
        "profit before exceptional items",
    ],

    "Exceptional items": [
        "total exceptional items",
        "exceptional items (net)",
        "exceptional items",
    ],

    "Profit before tax": [
        "profit / (loss) before tax",
        "profit / loss before tax",
        "profit before tax",
    ],

    "Total tax expense": [
        "total tax expense",
        "tax expense",
    ],

    "Net profit": [
        "net profit for the period",
        "profit for the period",
        "net profit",
    ],

    # --------------------------------------------------------
    # BALANCE SHEET
    # --------------------------------------------------------

    "Property, plant": [
        "property, plant and equipment",
        "property plant and equipment",
    ],

    "Total assets": [
        "total assets",
    ],

    "Total equity": [
        "total equity",
        "total shareholders' equity",
    ],

    "Borrowings non current": [
        "borrowings - non-current",
        "borrowings non-current",
        "non-current borrowings",
        "non current borrowings",
        # Bare form: some filings state "Borrowings" as a line item
        # under a "Non-current liabilities" subheading without
        # repeating the qualifier on the item's own line. Safe to
        # include here only because extract_financial_values()
        # restricts this field's search to the non-current-
        # liabilities subsection specifically (see FIELD_SUBSECTION).
        "borrowings",
    ],

    "Borrowings current": [
        "borrowings - current",
        "borrowings current",
        "current borrowings",
        "borrowings",
    ],

    "Lease liabilities non current": [
        "lease liabilities - non-current",
        "lease liabilities non-current",
        "non-current lease liabilities",
        "non current lease liabilities",
        "lease liabilities",
    ],

    "Lease liabilities current": [
        "lease liabilities - current",
        "lease liabilities current",
        "current lease liabilities",
        "lease liabilities",
    ],

    "Current investments": [
        "current investments",
        # Bare form: restricted to the current-assets subsection
        # (see FIELD_SUBSECTION) - the document lists it as a bare
        # "Investments" line under the "Current assets" subheading.
        "investments",
    ],

    "Cash": [
        "cash and cash equivalents",
        "cash & cash equivalents",
    ],

    "Other balances": [
        "other balances with banks",
        "other balances with bank",
    ],

    "Current assets": [
        "current assets",
    ],

    "Current liabilities": [
        "current liabilities",
    ],

    "Total liabilities": [
        "total liabilities",
    ],

    # --------------------------------------------------------
    # CASH FLOW
    # --------------------------------------------------------

    "Operating cash flow": [
        "net cash from operating activities",
        "net cash from/(used in) operating activities",
        "net cash from / (used in) operating activities",
        "cash from operating activities",
    ],

    "Income taxes paid": [
        "income taxes paid",
        "income tax paid",
        "taxes paid",
    ],

    "Interest paid": [
        "interest paid",
    ],

    "Repayment borrowings": [
        "repayment of long-term borrowings",
        "repayment of long term borrowings",
        "repayment of borrowings",
    ],

    "Lease payment": [
        "principal payment of lease liabilities",
        "principal payment of lease liability",
        "principal payment of lease",
    ],

    "Capex": [
        "purchase of capital assets",
        "purchase of capital asset",
        "capital expenditure",
        "capex",
    ],

    "Dividend": [
        "dividend paid",
        "dividends paid",
    ],

    "Total comprehensive income": [
        "total comprehensive income for the period",
        "total comprehensive income",
    ],

    # --------------------------------------------------------
    # RATIOS
    # --------------------------------------------------------

    "DSCR": [
        "debt service coverage ratio (dscr)",
        "debt service coverage ratio",
        "dscr",
    ],

    "ISCR": [
        "interest service coverage ratio (iscr)",
        "interest service coverage ratio",
        "iscr",
    ],

    "Current ratio": [
        "current ratio (company definition",
        "current ratio",
    ],

    "Debt equity": [
        "debt equity ratio (net debt / average equity)",
        "debt equity ratio",
        "debt-to-equity ratio",
    ],

    "Debt assets": [
        "total debts to total assets",
        "total debt to total assets",
        "debts to total assets",
    ],

    "Operating EBITDA margin": [
        "operating ebitda margin",
        "operating ebidta margin",
    ],

    "Net profit margin": [
        "net profit margin",
    ],

    "Net worth": [
        "net worth",
    ],

    "Debtors turnover": [
        "debtors turnover ratio (in days)",
        "debtors turnover ratio",
    ],

    "Inventory turnover": [
        "inventory turnover ratio (in days)",
        "inventory turnover ratio",
    ],
}

# ============================================================
# LABEL PUNCTUATION NORMALIZATION
#
# OCR renders the same underlying label with cosmetic punctuation
# differences that plain substring matching would otherwise treat as
# a non-match:
#   - whitespace around a slash ("profit / (loss)" vs "profit/
#     (loss)" vs "profit/(loss)") all collapse to "profit/(loss)"
#   - a hyphen used as a visual word separator with spaces on both
#     sides ("total - assets") collapses to a plain space ("total
#     assets"), while a hyphenated compound word with no surrounding
#     spaces ("non-current") is left untouched, since that hyphen is
#     part of the word itself, not a separator.
# Applied identically to both the OCR line and the alias text before
# comparison, so it only widens what already-intended matches can
# tolerate - it never changes which distinct words are being compared.
# ============================================================

def _collapse_label_punctuation(text):
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"\s+-\s+", " ", text)
    return text


# Flattened, lower-cased list of every alias across every field.
# Used to detect whether a candidate "neighbour" line (see
# locate_field below) is actually somebody else's labelled row,
# so a wrapped label never steals another field's numbers.
ALL_ALIASES_FLAT = sorted(
    {
        _collapse_label_punctuation(alias.lower())
        for aliases in ALIASES.values()
        for alias in aliases
    },
    key=len,
    reverse=True,
)


# ============================================================
# STATEMENT SECTION DETECTION
#
# Real filings print Standalone and Consolidated results as two
# separate tables, each containing its own Profit & Loss /
# Balance Sheet / Cash Flow / Ratios blocks. Searching the whole
# OCR text for "Revenue from operations" with no further
# qualification risks matching the Consolidated figure instead
# of the Standalone one the template asks for (see A1 title).
# ============================================================

SECTION_HEADING_PATTERNS = {
    "PNL": re.compile(
        r"financial results for the|statement of profit and loss|"
        r"statement of.*financial results",
        re.I,
    ),
    "BS": re.compile(
        r"statement of assets and liabilities|balance sheet as at",
        re.I,
    ),
    "CF": re.compile(
        r"statement of cash flows?|cash flow statement",
        re.I,
    ),
    "RATIO": re.compile(
        r"regulation\s*52|reported ratios|disclosures pursuant",
        re.I,
    ),
}

STANDALONE_MARK = re.compile(r"\bstandalone\b", re.I)
CONSOLIDATED_MARK = re.compile(r"\bconsolidated\b", re.I)

# A header row announcing the reporting period(s), e.g.
# "31.03.2026  31.12.2025  31.03.2025  31.03.2026  31.03.2025".
DATE_TOKEN_RE = re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})")


def find_statement_sections(lines):

    headings = []

    for idx, line in enumerate(lines):

        for section_type, pattern in SECTION_HEADING_PATTERNS.items():

            if pattern.search(line):

                window = " ".join(
                    lines[max(0, idx - 1): idx + 2]
                )

                if (
                    CONSOLIDATED_MARK.search(window)
                    and not STANDALONE_MARK.search(window)
                ):
                    scope = "consolidated"
                else:
                    scope = "standalone"

                headings.append(
                    {
                        "line": idx,
                        "type": section_type,
                        "scope": scope,
                    }
                )

                break

    headings.sort(key=lambda h: h["line"])

    sections = []

    for i, heading in enumerate(headings):

        end = (
            headings[i + 1]["line"]
            if i + 1 < len(headings)
            else len(lines)
        )

        sections.append(
            {
                "start": heading["line"],
                "end": end,
                "type": heading["type"],
                "scope": heading["scope"],
            }
        )

    return sections


def pick_section(sections, section_type):

    matches = [
        s for s in sections if s["type"] == section_type
    ]

    if not matches:
        return None

    standalone_matches = [
        s for s in matches if s["scope"] == "standalone"
    ]

    if standalone_matches:
        return standalone_matches[0]

    return matches[0]


# ============================================================
# BALANCE SHEET SUBSECTIONS
#
# Schedule III balance sheets split ASSETS into "Non-current assets"
# / "Current assets" and EQUITY AND LIABILITIES into "Non-current
# liabilities" / "Current liabilities" subheadings, then list bare
# items ("(i) Borrowings", "(ii) Lease liabilities", "(i)
# Investments") underneath without repeating the qualifier on each
# item's own line. This is standard Ind-AS/Schedule III structure,
# not specific to any one filer, so detecting it generically lets a
# handful of fields (see FIELD_SUBSECTION) resolve their qualifier
# from surrounding context instead of requiring it on the same line.
# ============================================================

BS_SUBHEADING_PATTERNS = {
    "non_current_assets": re.compile(r"non-?\s*current\s+assets\b", re.I),
    "current_assets": re.compile(r"(?<!non-)(?<!non\s)current\s+assets\b", re.I),
    "non_current_liabilities": re.compile(r"non-?\s*current\s+liabilities\b", re.I),
    "current_liabilities": re.compile(r"(?<!non-)(?<!non\s)current\s+liabilities\b", re.I),
}


def find_bs_subsections(lines, section):

    if not section:
        return {}

    first_seen = {}

    for idx in range(section["start"], section["end"]):

        line = lines[idx]

        for name, pattern in BS_SUBHEADING_PATTERNS.items():

            if name in first_seen:
                continue

            if pattern.search(line):
                first_seen[name] = idx

    ordered = sorted(first_seen.items(), key=lambda kv: kv[1])

    subsections = {}

    for i, (name, start_idx) in enumerate(ordered):

        end_idx = (
            ordered[i + 1][1] if i + 1 < len(ordered) else section["end"]
        )

        subsections[name] = {
            "start": start_idx,
            "end": end_idx,
            "type": "BS",
            "scope": section["scope"],
        }

    return subsections


def detect_header_periods(lines, start, end):

    window_end = min(end, start + 20)

    best_line_idx = None
    best_matches = []

    for i in range(start, window_end):

        matches = list(DATE_TOKEN_RE.finditer(lines[i]))

        if len(matches) >= 2 and len(matches) > len(best_matches):
            best_matches = matches
            best_line_idx = i

    if not best_matches:
        return None

    dates = []

    for m in best_matches:

        day, month, year = m.groups()
        year = int(year)

        if year < 100:
            year += 2000

        dates.append(
            {
                "day": int(day),
                "month": int(month),
                "year": year,
            }
        )

    return {
        "line_idx": best_line_idx,
        "dates": dates,
        "count": len(dates),
    }


# ============================================================
# TEXT NORMALIZATION
# ============================================================

def normalize_text(text):

    if not text:
        return ""

    text = str(text)

    text = text.replace(" ", " ")
    text = text.replace("₹", " ")
    text = text.replace("’", "'")
    text = text.replace("‘", "'")
    text = text.replace("–", "-")
    text = text.replace("—", "-")
    text = text.replace("−", "-")

    return re.sub(
        r"\s+",
        " ",
        text
    ).strip()


def strip_line_prefixes(line):

    # LLMWhisperer's add_line_nos prefixes every line with its own
    # OCR line number, either decimal ("1234 | text" / "1234: text")
    # or hexadecimal ("0x11F: text" - seen on some runs). Left
    # unstripped, a hex prefix is misread by the number-token
    # extractor as fake numeric values, which can make a label-only
    # line look like it already carries real data.
    line = re.sub(r"^\s*0[xX][0-9A-Fa-f]{1,8}\s*[|:]\s*", "", line)
    line = re.sub(r"^\s*\d{1,5}\s*[|:]\s*", "", line)

    # Schedule III item serial numbers, e.g. "7  Exceptional items",
    # or a serial number sitting alone in its own column ahead of the
    # row's numbers with no label on that line at all (happens when a
    # long label wraps onto the line above/below, leaving the numbers
    # on their own line prefixed only by the serial number).
    line = re.sub(r"^\s*\d{1,2}\)?\s+", "", line)

    # Alphabetic sub-item prefixes, e.g. "a) Sales / income..."
    line = re.sub(r"^\s*[a-zA-Z]\)\s*", "", line)

    return line


# ============================================================
# NUMBER CLEANING
# ============================================================

def clean_number(value):

    if value is None:
        return None

    value = str(value).strip()

    if value in [
        "",
        "-",
        "—",
        "–",
        "N/A",
        "NA",
    ]:
        return None

    negative = False

    # Accounting-style negative:
    # (1,098.86)
    if (
        value.startswith("(")
        and value.endswith(")")
    ):
        negative = True
        value = value[1:-1]

    value = value.replace(",", "")
    value = value.replace("₹", "")
    value = value.replace("Cr", "")
    value = value.replace("cr", "")
    value = value.replace("CR", "")
    value = value.replace("x", "")
    value = value.replace("X", "")
    value = value.replace("%", "")

    value = value.strip()

    match = re.search(
        r"-?\d+(?:\.\d+)?",
        value
    )

    if not match:
        return None

    number = float(
        match.group()
    )

    if negative:
        number = -number

    return number


# ============================================================
# EXTRACT NUMBER TOKENS FROM AN OCR LINE
#
# Returns rich tokens (raw text + parsed value) rather than bare
# floats, because deciding which numbers are real data and which
# are footnote markers or serial numbers requires looking at the
# shape of the original text, not just the parsed value.
# ============================================================

NUMBER_TOKEN_RE = re.compile(
    r"""
    (?:
        \(\s*-?\d[\d,]*(?:\.\d+)?\s*\)
    )
    |
    (?:
        -?\d[\d,]*(?:\.\d+)?(?:%|x|X)?
    )
    """,
    re.VERBOSE,
)


# A run of 3+ dot-separated digit groups (e.g. "1.32.345.31") is
# almost always a single Indian lakh/crore-grouped figure where OCR
# misread the ',' group separators as '.', not one number with
# several decimal points.
INDIAN_MULTI_DOT_RE = re.compile(r"\d{1,2}(?:\.\d{2,3}){2,}")


def _reconstruct_indian_multidot_numbers(line):
    """Collapse a "1.32.345.31"-shaped run into one normal number
    before the ordinary tokenizer runs, so it isn't fragmented into
    meaningless pieces. Only fires when every group after the first
    is 2-3 digits (consistent with Indian lakh/crore grouping) and
    the last group is exactly 2 digits (a currency-cents convention);
    all but that last '.' are then treated as group separators and
    dropped, leaving the final '.' as the real decimal point. Left
    untouched for any other shape, including an ordinary single-
    decimal number - this never inserts a decimal point that wasn't
    already present in the OCR text, it only reinterprets which of
    several already-present dots is the real one."""

    def _replace(match):

        groups = match.group().split(".")

        if not re.fullmatch(r"\d{1,2}", groups[0]):
            return match.group()

        if not all(re.fullmatch(r"\d{2,3}", g) for g in groups[1:-1]):
            return match.group()

        if not re.fullmatch(r"\d{2}", groups[-1]):
            return match.group()

        return "".join(groups[:-1]) + "." + groups[-1]

    return INDIAN_MULTI_DOT_RE.sub(_replace, line)


# OCR occasionally renders a decimal point as an underscore between
# two digits (e.g. "2_85" for "2.85"). An underscore never appears in
# a genuine financial figure, so any digit-underscore-digit run is
# safely read as a decimal point.
UNDERSCORE_DECIMAL_RE = re.compile(r"(?<=\d)_(?=\d)")

# OCR occasionally inserts a stray space into the middle of an Indian
# comma-grouped number, splitting it into a dangling group ending in a
# comma (e.g. "1,39,") and its continuation (e.g. "720.22"), instead of
# "1,39,720.22" as one figure. A complete, well-formed number never
# ends in a trailing comma, so this shape - a comma-group immediately
# followed by whitespace and 2-3 more digits - is unambiguous: no
# genuinely separate number would start immediately after a dangling
# comma with nothing but whitespace between them.
STRAY_SPACE_IN_GROUPED_NUMBER_RE = re.compile(
    r"(\d{1,2}(?:,\d{2,3})*,)\s+(\d{2,3}(?:\.\d+)?)"
)

# OCR occasionally drops the decimal point of a parenthesised
# (accounting-negative) figure and leaves a plain space in its place
# (e.g. "(2,688 26)" for "(2,688.26)"). A closing parenthesis marks
# the end of that one figure, so a comma-grouped whole number
# immediately followed by whitespace, exactly two more digits, and
# the closing paren cannot be two genuinely separate figures - no
# independent second number would be sandwiched between an
# already-complete comma-grouped figure and the parenthesis that
# closes it.
SPACE_AS_DECIMAL_BEFORE_PAREN_RE = re.compile(
    r"(\d(?:,\d{2,3})+)\s+(\d{2})\)"
)


def extract_number_tokens(line):

    if not line:
        return []

    line = normalize_text(line)
    line = strip_line_prefixes(line)
    line = UNDERSCORE_DECIMAL_RE.sub(".", line)
    line = STRAY_SPACE_IN_GROUPED_NUMBER_RE.sub(r"\1\2", line)
    line = SPACE_AS_DECIMAL_BEFORE_PAREN_RE.sub(r"\1.\2)", line)
    line = _reconstruct_indian_multidot_numbers(line)

    # Formula-style references in item labels, e.g. "Total income
    # (1+2)" or "[1 + 2]" - these are not data columns.
    line = re.sub(r"[(\[]\s*\d+(?:\s*[+\-]\s*\d+)+\s*[)\]]", "", line)

    tokens = []

    for match in NUMBER_TOKEN_RE.finditer(line):

        raw = match.group()
        value = clean_number(raw)

        if value is None:
            continue

        tokens.append(
            {
                "raw": raw,
                "value": value,
                "start": match.start(),
            }
        )

    return tokens


def filter_footnote_tokens(tokens):

    # Drop trailing bare 1-2 digit integers (no comma, no decimal
    # point, not parenthesised) that follow at least one properly
    # formatted money value on the same line - these are almost
    # always footnote/superscript markers picked up by OCR, e.g.
    # "... (1,098.90) (902.00) 1" where the trailing "1" is a
    # footnote reference, not a third data column.
    if len(tokens) <= 2:
        return tokens

    result = list(tokens)

    while len(result) > 2:

        last = result[-1]

        is_bare_short_int = re.fullmatch(
            r"\d{1,2}", last["raw"]
        ) is not None

        has_richer_before = any(
            ("." in t["raw"] or "," in t["raw"] or t["raw"].startswith("("))
            for t in result[:-1]
        )

        if is_bare_short_int and has_richer_before:
            result.pop()
        else:
            break

    return result


def _alias_present(normalized_line, alias_lower):
    """True if `alias_lower` occurs in `normalized_line`, other than
    as part of a "non-<alias>" / "non <alias>" phrase - e.g. "current
    assets" must not match inside "non-current assets", which is a
    different, opposite balance-sheet line. Checks every occurrence,
    since the same alias can legitimately appear again elsewhere on
    the same line."""

    idx = normalized_line.find(alias_lower)

    while idx != -1:

        prefix = normalized_line[:idx]

        if not re.search(r"non[\s-]*$", prefix):
            return True

        idx = normalized_line.find(alias_lower, idx + 1)

    return False


def alias_score(line, aliases):

    normalized = _collapse_label_punctuation(normalize_text(line).lower())

    score = 0

    for alias in aliases:

        alias_lower = _collapse_label_punctuation(normalize_text(alias).lower())

        if _alias_present(normalized, alias_lower):
            score = max(score, len(alias_lower))

    return score


def line_matches_any_alias(line):

    normalized = _collapse_label_punctuation(normalize_text(line).lower())

    for alias in ALL_ALIASES_FLAT:

        if _alias_present(normalized, alias):
            return True

    return False


# Every alias paired with the statement section its own field belongs
# to, longest-alias-first (mirrors ALL_ALIASES_FLAT's priority).
ALIAS_SECTION_LOOKUP = sorted(
    (
        (_collapse_label_punctuation(alias.lower()), FIELD_SECTION[field])
        for field, aliases in ALIASES.items()
        for alias in aliases
    ),
    key=lambda pair: len(pair[0]),
    reverse=True,
)


def line_belongs_to_other_field(line, current_section_type):
    """True if `line` is itself clearly some OTHER field's own
    labelled row *within the same statement section* the current
    field is being searched in - e.g. don't let one Reported Ratios
    row's lookahead steal a neighbouring ratio's row. Deliberately
    does NOT reject on a cross-section name reuse: the Ratios
    section's own formula descriptions routinely reference P&L/BS
    terms by name (e.g. "EBIT: Profit before taxes + Exceptional
    items + Net finance charges", "Net profit after tax / Turnover")
    without being that field's actual row, since those fields live in
    a different section and cannot have their own row here."""

    normalized = _collapse_label_punctuation(normalize_text(line).lower())

    for alias, section_type in ALIAS_SECTION_LOOKUP:

        if section_type != current_section_type:
            continue

        if _alias_present(normalized, alias):
            return True

    return False


# ============================================================
# LOCATE A FIELD'S DATA WITHIN A SECTION
# ============================================================

# How far past a label-only line to look for its data. Ratio-table
# rows in these filings interpose 1-3 lines of formula description
# ("(EBIT / (Net finance charges + ...))") between the ratio's name
# and its actual reported figures; 6 lines is a generous margin above
# the longest such block observed (3 lines).
MAX_LOOKAHEAD_LINES = 6


LETTERED_ITEM_RE = re.compile(r"(?:^|[|:])\s*[a-zA-Z]\)\s")


def locate_field(lines, section, aliases):

    if section:
        search_range = range(section["start"], section["end"])
    else:
        search_range = range(len(lines))

    scored = [
        (idx, alias_score(lines[idx], aliases))
        for idx in search_range
    ]
    scored = [(idx, s) for idx, s in scored if s > 0]

    if not scored:
        return None

    best_score = max(s for _, s in scored)
    candidates = [idx for idx, s in scored if s == best_score]

    # A compound line item's lettered sub-components (e.g. "a) Sales/
    # income from operations") can outscore, by raw alias length, the
    # explicit "Total <item>" line that follows them in standard
    # Schedule III/Ind-AS filings - even though that Total line is the
    # authoritative summary figure, not any one sub-component. If
    # every current best-scoring candidate is such a lettered
    # sub-item and a line explicitly marked "Total" also matched this
    # field's own aliases (at any score), prefer the Total line
    # instead. Every candidate here already passed a genuine, field-
    # specific alias match, so this only reorders among already-
    # relevant lines - it never introduces a new, unrelated match.
    if all(LETTERED_ITEM_RE.search(lines[idx]) for idx in candidates):

        total_marked = [
            idx for idx, _ in scored
            if re.search(r"\btotal\b", lines[idx], re.I)
        ]

        if total_marked:
            candidates = total_marked

    # When the alias matches more than one line at the same score -
    # e.g. a bare "(2) Current assets" subheading and, further down,
    # the "Sub-total - Current assets" line that actually states the
    # figures - prefer whichever candidate already carries real data
    # over a label-only occurrence, rather than always taking the
    # first (usually the subheading).
    data_bearing = [
        idx for idx in candidates
        if len(extract_number_tokens(lines[idx])) >= 2
    ]
    best_idx = data_bearing[0] if data_bearing else candidates[0]

    numeric_idx = best_idx
    tokens = extract_number_tokens(lines[best_idx])

    if len(tokens) < 2:

        # The matched line only carries the label (a long item
        # name wrapped across OCR lines, or a ratio name followed by
        # its formula description before the figures). Look at the
        # surrounding lines for the row's actual numbers, but never
        # cross into a line that is itself another field's labelled
        # row.
        offsets = list(range(1, MAX_LOOKAHEAD_LINES + 1)) + [-1]

        for offset in offsets:

            candidate_idx = best_idx + offset

            if candidate_idx < 0 or candidate_idx >= len(lines):
                continue

            if section and not (
                section["start"] <= candidate_idx < section["end"]
            ):
                continue

            current_section_type = section["type"] if section else None

            if line_belongs_to_other_field(
                lines[candidate_idx], current_section_type
            ):
                continue

            candidate_tokens = extract_number_tokens(
                lines[candidate_idx]
            )

            if len(candidate_tokens) >= 2:
                tokens = candidate_tokens
                numeric_idx = candidate_idx
                break

    return {
        "label_line_idx": best_idx,
        "label_line_text": lines[best_idx],
        "numeric_line_idx": numeric_idx,
        "numeric_line_text": lines[numeric_idx],
        "tokens": tokens,
    }


def select_fy_values(tokens, expected_count):

    filtered = filter_footnote_tokens(tokens)

    if len(filtered) >= 2:

        fy_current = filtered[-2]["value"]
        fy_previous = filtered[-1]["value"]

        if expected_count and len(filtered) == expected_count:
            confidence = "high"
        elif expected_count:
            confidence = "medium"
        else:
            confidence = "low"

    elif len(filtered) == 1:

        fy_current = filtered[0]["value"]
        fy_previous = None
        confidence = "low"

    else:

        fy_current = None
        fy_previous = None
        confidence = "none"

    return fy_current, fy_previous, confidence, filtered


# "Net worth" is the one row in the Regulation 52(4) "Reported Ratios"
# block that is a genuine point-in-time balance-sheet figure - its own
# defining note states it as a static formula ("Equity share capital +
# Other equity - Capital reserve - Amalgamation reserve"), not a ratio
# accumulated over a period the way DSCR/ISCR/the margin rows are.
# That block's disclosure is laid out, per SEBI Regulation 52(4)'s own
# standard format (not a Tata Steel-specific layout), as five columns
# in a fixed order: [Quarter ended <current>, Quarter ended
# <immediately prior>, Quarter ended <year-ago>, Financial year ended
# <current>, Financial year ended <prior>]. For any company's Q4/
# year-end quarter, "quarter ended" and "financial year ended" name
# the identical date - so for this one point-in-time field, the
# Quarter-current and Quarter-year-ago columns (positions 0 and 2) are
# the same true figure as the nominal FY-current/FY-previous columns
# (positions 3 and 4), and are frequently the cleaner OCR read of the
# two since the FY columns sit on the header's second, often
# misaligned line. This is never applied to any other Reported Ratios
# row - a flow ratio's quarter and full-year figures are genuinely
# different numbers (confirmed against this filing's own Debt equity
# ratio row, which even carries a footnote marking a mid-year
# definition change between its quarterly and annual columns), so
# reusing this column-selection logic there would be wrong, not
# merely unnecessary.
def select_fy_values_net_worth(tokens, expected_count):

    filtered = filter_footnote_tokens(tokens)

    # Fewer than 3 tokens means this filing's Net worth line was not
    # printed as the standard 5-column quarterly-plus-annual
    # disclosure (e.g. a plain 2-column balance-sheet-style row) -
    # falls back to the ordinary last-two-tokens selection rather than
    # indexing into columns that were never established to exist.
    if len(filtered) < 3:
        return select_fy_values(tokens, expected_count)

    fy_current = filtered[0]["value"]
    fy_previous = filtered[2]["value"]

    confidence = (
        "high" if expected_count and len(filtered) == expected_count
        else "medium"
    )

    return fy_current, fy_previous, confidence, filtered


# ============================================================
# TARGET FY YEARS (READ FROM THE TEMPLATE ITSELF)
#
# The template's own header cells (B4 = "FY 31.03.2026",
# C4 = "FY 31.03.2025") say which two years are wanted. Reading
# them at runtime - instead of hardcoding "2026"/"2025" - means
# the tool keeps working unmodified once the template is rolled
# forward to next year's reporting dates.
# ============================================================

def get_target_fy(template_path):

    wb = load_workbook(template_path, data_only=False)
    ws = wb[TARGET_SHEET]

    current_label = str(ws["B4"].value or "")
    previous_label = str(ws["C4"].value or "")

    current_match = DATE_TOKEN_RE.search(current_label)
    previous_match = DATE_TOKEN_RE.search(previous_label)

    current_year = (
        int(current_match.group(3)) if current_match else None
    )
    previous_year = (
        int(previous_match.group(3)) if previous_match else None
    )

    return current_year, previous_year, current_label, previous_label


# ============================================================
# LLMWHISPERER OCR
# ============================================================

def run_llmwhisperer(pdf_path):

    if not LLMWHISPERER_API_KEY:

        raise RuntimeError(
            "LLMWhisperer API key is not configured. Set it in "
            ".streamlit/secrets.toml (LLMWHISPERER_API_KEY) or the "
            "LLMWHISPERER_API_KEY environment variable."
        )

    client = LLMWhispererClientV2(
        base_url=LLMWHISPERER_BASE_URL,
        api_key=LLMWHISPERER_API_KEY,
        logging_level="ERROR",
    )

    result = client.whisper(

        file_path=pdf_path,

        mode=OCR_MODE,

        output_mode=OCR_OUTPUT_MODE,

        lang=OCR_LANGUAGE,

        wait_for_completion=True,

        wait_timeout=OCR_WAIT_TIMEOUT,

        add_line_nos=True,
    )

    if not result:

        raise RuntimeError(
            "LLMWhisperer returned an empty response."
        )

    extraction = result.get(
        "extraction",
        {}
    )

    if isinstance(
        extraction,
        dict
    ):

        text = extraction.get(
            "result_text",
            ""
        )

    else:

        text = str(
            extraction
        )

    if not text:

        raise RuntimeError(
            "LLMWhisperer completed, "
            "but no extracted text was returned."
        )

    return text, result


# ============================================================
# SIBLING-MAGNITUDE REPAIR
#
# Two more OCR number defects observed in these filings, both
# producing a "clean" integer where a 2-decimal figure was intended:
# a dropped decimal point ("606714" for what should read "6067.14")
# and a decimal point mis-read as a comma ("0,25", which ordinary
# comma-stripping turns into "25" instead of "0.25"). Neither defect
# is safely detectable from the token in isolation - a large whole
# number is not inherently wrong. What makes it recognisable is its
# own sibling: the same field's other reported year, already parsed.
# When the two are wildly inconsistent (>=20x) purely because of a
# trailing ".XX" and dividing by 100 brings them back into a
# plausible range of each other (<=10x), the /100 reading is used.
# This never inserts a decimal point into a number considered on its
# own, and a genuine large year-over-year swing between two cleanly
# parsed figures is left untouched.
# ============================================================

def _maybe_divide_by_100(value, sibling):

    if value is None or sibling is None:
        return value, False

    if value == 0 or sibling == 0:
        return value, False

    if value != int(value):
        return value, False

    ratio = abs(value) / abs(sibling)

    if ratio < 20:
        return value, False

    candidate = value / 100.0

    candidate_ratio = (
        max(abs(candidate), abs(sibling)) / min(abs(candidate), abs(sibling))
    )

    if candidate_ratio <= 10:
        return candidate, True

    return value, False


def repair_sibling_magnitude(fy_current, fy_previous):

    new_current, current_changed = _maybe_divide_by_100(fy_current, fy_previous)
    new_previous, previous_changed = _maybe_divide_by_100(fy_previous, fy_current)

    return new_current, new_previous, (current_changed or previous_changed)


# Ratio beyond which two of a field's own reported years are too
# magnitude-inconsistent to trust, even after repair_sibling_magnitude()
# above has had a chance to resolve it (that function only fires when
# one side is a "clean" integer - a dropped decimal or a comma-as-
# decimal defect; when BOTH sides already carry a fraction, as with a
# multi-column figure fragmented by badly garbled digit grouping, there
# is nothing for it to repair). A ratio this extreme between the same
# field's own two years almost always means the source OCR text was too
# garbled to recover a genuine trailing figure, not a real swing -
# populate_workbook() must not write either fragment into the workbook
# for a field flagged this way. General to any field, not specific to
# any one filer's data.
AMBIGUOUS_MAGNITUDE_RATIO = 15


def is_magnitude_ambiguous(fy_current, fy_previous):

    if fy_current in (None, 0) or fy_previous in (None, 0):
        return False

    ratio = max(abs(fy_current), abs(fy_previous)) / min(abs(fy_current), abs(fy_previous))

    return ratio >= AMBIGUOUS_MAGNITUDE_RATIO


# ============================================================
# EXTRACT FINANCIAL VALUES
# ============================================================

def extract_financial_values(ocr_text, template_path):

    lines = [
        normalize_text(x)
        for x in ocr_text.splitlines()
        if normalize_text(x)
    ]

    target_current_year, target_previous_year, current_label, previous_label = (
        get_target_fy(template_path)
    )

    sections = find_statement_sections(lines)

    section_by_type = {}
    header_by_type = {}

    for section_type in ("PNL", "BS", "CF", "RATIO"):

        section = pick_section(sections, section_type)
        section_by_type[section_type] = section

        if section:
            header_by_type[section_type] = detect_header_periods(
                lines, section["start"], section["end"]
            )
        else:
            header_by_type[section_type] = None

    bs_subsections = find_bs_subsections(lines, section_by_type["BS"])

    extracted = {}
    debug_rows = []

    all_fields = list(ROW_MAPPING.keys()) + EXTRA_FIELDS

    for field in all_fields:

        section_type = FIELD_SECTION[field]
        subsection_name = FIELD_SUBSECTION.get(field)

        if subsection_name and subsection_name in bs_subsections:
            section = bs_subsections[subsection_name]
        else:
            section = section_by_type[section_type]

        header_info = header_by_type[section_type]
        expected_count = header_info["count"] if header_info else None

        aliases = ALIASES[field]

        loc = locate_field(lines, section, aliases)

        row = ROW_MAPPING.get(field)
        excel_cell = (
            f"B{row}, C{row}" if row else EXTRA_FIELD_CELLS[field]
        )

        if loc is None:

            extracted[field] = {
                "fy_current": None,
                "fy_previous": None,
                "normalized": False,
                "ambiguous": False,
            }

            debug_rows.append(
                {
                    "Field": field,
                    "Section": section_type,
                    "OCR Source Line": "NOT FOUND",
                    "OCR Numbers": [],
                    "FY2026 Selected": None,
                    "FY2025 Selected": None,
                    "Excel Cell": excel_cell,
                    "Confidence": "none",
                    "Normalized": False,
                    "Ambiguous": False,
                }
            )

            continue

        selector = (
            select_fy_values_net_worth if field == "Net worth"
            else select_fy_values
        )

        fy_current, fy_previous, confidence, filtered_tokens = (
            selector(loc["tokens"], expected_count)
        )

        fy_current, fy_previous, normalized = repair_sibling_magnitude(
            fy_current, fy_previous
        )

        ambiguous = is_magnitude_ambiguous(fy_current, fy_previous)

        # extracted[field] keeps the actual parsed figures - populate_
        # workbook() is the single place that decides, from the
        # "ambiguous" flag, whether they are safe to write. debug_rows
        # mirrors the same parsed figures + flag so the diagnostic can
        # report *what was found* alongside *why it was suppressed*,
        # rather than looking like nothing was found at all.
        extracted[field] = {
            "fy_current": fy_current,
            "fy_previous": fy_previous,
            "normalized": normalized,
            "ambiguous": ambiguous,
        }

        source_line = loc["label_line_text"]

        if loc["numeric_line_idx"] != loc["label_line_idx"]:
            source_line = (
                f"{loc['label_line_text']} | "
                f"{loc['numeric_line_text']}"
            )

        debug_rows.append(
            {
                "Field": field,
                "Section": section_type,
                "OCR Source Line": source_line,
                "OCR Numbers": [t["value"] for t in loc["tokens"]],
                "FY2026 Selected": fy_current,
                "FY2025 Selected": fy_previous,
                "Excel Cell": excel_cell,
                "Confidence": confidence,
                "Normalized": normalized,
                "Ambiguous": ambiguous,
            }
        )

    meta = {
        "target_current_year": target_current_year,
        "target_previous_year": target_previous_year,
        "target_current_label": current_label,
        "target_previous_label": previous_label,
        "sections": sections,
    }

    return extracted, debug_rows, meta


# ============================================================
# WRITE EXTRACTED VALUE
# ============================================================

def set_extracted_value(cell, value):

    if value is None:
        return

    cell.value = value

    old_font = copy(
        cell.font
    )

    cell.font = Font(
        name=old_font.name,
        size=old_font.size,
        bold=old_font.bold,
        italic=old_font.italic,
        underline=old_font.underline,
        strike=old_font.strike,
        color="0000FF",
    )


# ============================================================
# POPULATE EXCEL WORKBOOK
#
# The template is the source of truth for structure, formulas
# and formatting: every formula cell (Total income, Net finance
# charges, EBITDA, EBITDA margin, Total debt, Net debt, Average
# equity, all validation/reconciliation checks) already exists
# in the template with the correct references, and every
# extraction cell already carries the correct blue font and
# number format (General / 0.00% / 0.00x). This function only
# ever writes to the extraction cells - it never re-derives a
# formula or touches number formatting, so there is no way for
# it to drift from what the template defines.
# ============================================================

def populate_workbook(template_path, extracted):

    wb = load_workbook(
        template_path
    )

    if TARGET_SHEET not in wb.sheetnames:

        raise ValueError(
            f"Template does not contain "
            f"'{TARGET_SHEET}' sheet."
        )

    ws = wb[
        TARGET_SHEET
    ]

    for field, row in ROW_MAPPING.items():

        if field not in extracted:
            continue

        fy_current = extracted[field]["fy_current"]
        fy_previous = extracted[field]["fy_previous"]

        # The Reported Ratios block (rows 49-56) uses a different
        # column layout from every other extraction row: column C (and
        # E) hold the template's own built-in "Calculated cross-check"
        # formula for current/previous year respectively (e.g. row 49's
        # C49 = "=B20/((B9-B11)+(B43+B44))"), never the previous-year
        # reported figure - that belongs in column D instead. Every
        # other row uses the ordinary B=current/C=previous layout.
        previous_col = 4 if FIELD_SECTION.get(field) == "RATIO" else 3

        # A field flagged ambiguous (its two reported years are too
        # magnitude-inconsistent to trust - see is_magnitude_ambiguous())
        # is never written. The cell is actively cleared rather than
        # simply skipped: several of these extraction rows carry a
        # template-supplied placeholder formula (e.g. row 56's D column
        # defaults to "=C25") that would otherwise surface as soon as
        # this field stops receiving a real value, which would replace
        # one unreliable figure with a different, unrelated one instead
        # of a genuine blank. General to any field this applies to, not
        # specific to any one of them - other rows' own formulas
        # (D7/D12/D14/D16, and any row this mechanism never triggers
        # for) are untouched.
        if extracted[field].get("ambiguous"):
            fy_current = None
            fy_previous = None
            ws.cell(row=row, column=2).value = None
            ws.cell(row=row, column=previous_col).value = None

        if row in PERCENTAGE_ROWS:

            if fy_current is not None:
                fy_current = fy_current / 100

            if fy_previous is not None:
                fy_previous = fy_previous / 100

        if row in MAGNITUDE_ROWS:

            if fy_current is not None:
                fy_current = abs(fy_current)

            if fy_previous is not None:
                fy_previous = abs(fy_previous)

        # FY2026 = Column B
        set_extracted_value(
            ws.cell(row=row, column=2),
            fy_current
        )

        # FY2025 = Column C for ordinary rows, Column D for the
        # Reported Ratios block (see previous_col above) - column C
        # there is a template-supplied formula and is never touched.
        set_extracted_value(
            ws.cell(row=row, column=previous_col),
            fy_previous
        )

    # --------------------------------------------------------
    # D7/D12/D14/D16 - "Reported (validation)" column, current year
    # only (no FY-previous equivalent exists in the sheet - see
    # reference_data.ReferenceInputData's own docstring on these four
    # fields). Each holds that field's own already-extracted current-
    # year figure as a genuine number - never a formula string, since
    # reference_data._num() (frozen) rejects any cell containing one.
    # Left blank (None) when the field itself could not be extracted,
    # rather than echoing a value that was never independently
    # confirmed. D7 is a genuine cross-check ("Total income" is also
    # printed as its own line in the statement, independently of the
    # B5+B6 formula in B7); D12/D14/D16 mirror their own row's B value
    # since no independent second source exists for those figures.
    # --------------------------------------------------------

    for field, cell_ref in EXTRA_FIELD_CELLS.items():
        field_result = extracted.get(field, {})
        value = field_result.get("fy_current")
        if field_result.get("ambiguous"):
            value = None
        if value is not None:
            set_extracted_value(ws[cell_ref], value)
        else:
            ws[cell_ref].value = None

    return wb


# ============================================================
# DEBUG ARTIFACTS
# ============================================================

def save_debug_artifacts(ocr_text, debug_rows):

    if not DEBUG_MODE:
        return None

    os.makedirs(DEBUG_OUTPUT_DIR, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    raw_path = os.path.join(
        DEBUG_OUTPUT_DIR, f"ocr_raw_{stamp}.txt"
    )

    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(ocr_text)

    report_path = os.path.join(
        DEBUG_OUTPUT_DIR, f"validation_report_{stamp}.csv"
    )

    fieldnames = [
        "Field",
        "Section",
        "OCR Source Line",
        "OCR Numbers",
        "FY2026 Selected",
        "FY2025 Selected",
        "Excel Cell",
        "Confidence",
    ]

    with open(report_path, "w", encoding="utf-8", newline="") as f:

        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for row in debug_rows:
            writer.writerow(row)

    return {
        "raw_path": raw_path,
        "report_path": report_path,
    }
