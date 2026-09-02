"""
PDF -> OCR (LLMWhisperer) -> Financial Data Extraction -> Excel pipeline.

This IS the live PDF-upload pipeline: app.py's Data Upload page calls into
this module via ocr_upload.process_uploaded_pdf(), which runs
run_llmwhisperer() (or the curl-transport fallback), then
extract_financial_values() and populate_workbook() from here, against
Credit_Review_Tool_Input_Data_Template.xlsx (the blank OCR-output
template - a different workbook from Credit Review Automation Example.xlsx,
which supplies only the policy/threshold sheets, '2-Financial Analysis'
through '9 - Credit Memo'). run_llmwhisperer() is the only function that
calls the LLMWhisperer API, and only when a caller (ocr_upload.py)
explicitly invokes it with a PDF path and a configured API key - nothing
here runs on import.
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
        # Singular form - some filings label this line item "Exceptional
        # item" (no trailing "s"), e.g. "4 Exceptional item (note 3(a))".
        # NOT the bare word "exceptional item" - that also occurs inside
        # "Profit before exceptional item and tax" (a different field's own
        # row label in the very same statement) and would tie with it.
        "exceptional item (note",
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
        # Alternate phrasing seen in other filings' indirect-method cash
        # flow statements ("generated from"/"generated from/(used in)"
        # rather than "from") for the same line - the final net operating
        # total, not the earlier "Cash generated from operationS"
        # subtotal further up the same statement (deliberately distinct:
        # "operating activities", not "operations").
        "net cash flows generated from operating activities",
        "net cash flow generated from operating activities",
        "net cash generated from operating activities",
        "net cash flows generated from/(used in) operating activities",
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
        # Alternate phrasing for the same cash-flow line ("repayment
        # towards" rather than "principal payment of").
        "repayment towards lease liabilities",
        "repayment of lease liabilities",
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

    # "(in days)" is required, not optional: this field's row/template
    # cell is specifically the days-of-turnover figure, and at least one
    # real filing (Tata Chemicals) discloses the SAME-NAMED ratio in a
    # fundamentally different unit instead - "Inventory turnover ratio
    # (no. of times)" - a genuine turnover MULTIPLE, not a day-count (the
    # two are related by ~365/x but are not interchangeable numbers). A
    # bare "inventory/debtors turnover ratio" fallback would silently
    # match that "(no. of times)" row and write its multiple into a field
    # the rest of this application treats as a day-count - exactly the
    # "field exists but means something else" mismatch the extraction
    # audit requires this pipeline to catch rather than accept.
    "Debtors turnover": [
        "debtors turnover ratio (in days)",
        "trade receivables turnover ratio (in days)",
    ],

    "Inventory turnover": [
        "inventory turnover ratio (in days)",
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

# Every alternative below is anchored on \b before "statement"/"balance"/
# etc. - without it, "financial results for the" and (worse)
# "statement of...financial results" match INSIDE ordinary auditor's-report
# prose that happens to contain the word "misstatement" ("...risks of
# material misstatement of the standalone annual financial results...").
# That real, confirmed defect (found via the Tata Chemicals filing, whose
# audit opinion repeats that sentence for both Consolidated and Standalone)
# makes find_statement_sections() anchor a "PNL section" on a paragraph of
# pure boilerplate with zero data rows, starting BEFORE the genuine
# "Statement of Standalone Audited Financial Results..." heading - so
# pick_section()'s "first standalone match" picks the boilerplate instead
# of the real table, and every P&L field in it reports NOT FOUND even
# though the data is sitting right there a few lines later. "statement" is
# a real word-boundary-safe token ("misstatement" has no boundary between
# "mis" and "statement"), so \bstatement\b fixes this with no loss of any
# genuine heading match.
#
# The RATIO pattern's bare "regulation\s*52" alternative has the same
# shape of bug: an auditor's-report sentence merely citing "Regulation 33
# and Regulation 52(4) read with Regulation 63 of the Listing Regulations"
# (routine boilerplate, appearing several times before any real data table)
# matches it just as well as the genuine disclosure heading
# ("...pursuant to Regulation 52(4) of the Securities and Exchange Board of
# India..."). Requiring "pursuant to" immediately before "regulation 52"
# rules out that boilerplate shape, but is not enough on its own: the P&L
# page's own closing footer line ("See annexed segment information,
# balance sheet, cash flows, additional information pursuant to Regulation
# 52(4) AND ACCOMPANYING NOTES to the standalone audited financial
# results") also says "pursuant to Regulation 52(4)" - a mere cross-
# reference, not the disclosure table's own heading, and (being the P&L
# page's own footer) sorts BEFORE the genuine table too. What every real
# heading actually says next, confirmed word-for-word in both the Tata
# Steel and Tata Chemicals filings, is "...Regulation 52(4) OF [the]
# Securities [and] Exchange Board of India..." - the footer cross-
# reference says "and accompanying notes" instead. Requiring "of" right
# after "52(4)" keeps the genuine heading while excluding both false
# shapes.
SECTION_HEADING_PATTERNS = {
    "PNL": re.compile(
        r"financial results for the|\bstatement of profit and loss\b|"
        r"\bstatement of\b.*\bfinancial results\b",
        re.I,
    ),
    "BS": re.compile(
        r"\bstatement of assets and liabilities\b|\bbalance sheet as at\b",
        re.I,
    ),
    "CF": re.compile(
        r"\bstatement of cash flows?\b|\bcash flow statement\b",
        re.I,
    ),
    "RATIO": re.compile(
        r"pursuant to regulation\s*52\s*(?:\(4\))?\s+of\b|\breported ratios\b|\bdisclosures pursuant\b",
        re.I,
    ),
    # Not one of this pipeline's own four tracked statement types - no
    # field's FIELD_SECTION is ever "SEGMENT", so pick_section() is never
    # asked for one and this heading is never itself chosen as a field's
    # search section. Its only job is to end whichever PNL section
    # precedes it: the segment-wise breakdown that follows a Reg 52(4)
    # filing's headline P&L has no section heading of its own that this
    # pipeline's other patterns recognise, so without this entry
    # _merge_adjacent_same_heading() silently absorbs the entire
    # segment-wise table into the same PNL section as the real P&L (its
    # own heading only reads "...Revenue, Results, Assets and
    # Liabilities", matching none of the PNL/BS/CF/RATIO patterns above).
    # In this filing the segment-wise "Total revenue from operations" /
    # "Total profit before tax" happen to tie to the headline P&L figures
    # (segments reconcile to the same total by definition) so today's
    # extraction isn't wrong - but relying on that coincidence for every
    # field a future filing might name similarly is exactly the
    # cross-table contamination risk the extraction audit warns against.
    "SEGMENT": re.compile(
        r"segment\s*-?\s*wise\s+revenue",
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

    # Carries the last EXPLICITLY marked scope forward to the next heading
    # whose own +-1-line window mentions neither word - a real, confirmed
    # shape in these filings: a Reg 52(4) "Additional information pursuant
    # to Regulation 52(4)..." block that continues the Consolidated report
    # several pages after the "Consolidated Statement of Cash Flows"
    # heading, without repeating the word "Consolidated" in its own
    # heading line. Defaulting an unmarked heading straight to "standalone"
    # (the old behaviour) mislabels that block as standalone, risking a
    # Consolidated figure being read into a field the template asks for on
    # a Standalone basis. Only the very first heading in the document, if
    # it precedes any explicit marker at all, falls back to "standalone" -
    # there is no prior scope to inherit at that point.
    running_scope = None

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
                    running_scope = "consolidated"
                elif (
                    STANDALONE_MARK.search(window)
                    and not CONSOLIDATED_MARK.search(window)
                ):
                    running_scope = "standalone"
                # else: window mentions neither (or ambiguously mentions
                # both) - keep whatever scope was last explicitly seen
                # instead of guessing.

                scope = running_scope or "standalone"

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


def _merge_adjacent_same_heading(sections, chosen):
    """A real table can contain its own internal sub-heading that also
    matches SECTION_HEADING_PATTERNS - e.g. a Profit & Loss statement's own
    Other Comprehensive Income note re-mentions "...the Statement of
    Profit and Loss", opening a second PNL-type heading immediately after
    the main Income statement block (confirmed in the Tata Chemicals
    filing: "Total comprehensive income for the period" sits in this
    second, OCI-continuation chunk). find_statement_sections() correctly
    reports these as two separate same-type/same-scope sections back to
    back, but they are really one continuous table - without this,
    pick_section() would return only the first chunk and any field whose
    row happens to fall in the second (like Total comprehensive income
    here) would wrongly report NOT FOUND despite being a few lines away.
    sections tile the document with no gaps (each one's "end" is exactly
    the next heading's "start"), so "immediately following in the list"
    already means "immediately following in the document" - extending
    forward through every following section that shares this one's type
    and scope reunites the table without touching a genuinely distinct,
    non-adjacent recurrence of the same heading text much later on."""
    end = chosen["end"]
    idx = sections.index(chosen)
    for nxt in sections[idx + 1:]:
        if nxt["type"] == chosen["type"] and nxt["scope"] == chosen["scope"] and nxt["start"] == end:
            end = nxt["end"]
        else:
            break
    if end == chosen["end"]:
        return chosen
    merged = dict(chosen)
    merged["end"] = end
    return merged


def pick_section(sections, section_type):

    matches = [
        s for s in sections if s["type"] == section_type
    ]

    if not matches:
        return None

    standalone_matches = [
        s for s in matches if s["scope"] == "standalone"
    ]

    chosen = standalone_matches[0] if standalone_matches else matches[0]

    return _merge_adjacent_same_heading(sections, chosen)


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


# A line consisting only of plausible 4-digit calendar years (e.g.
# "2026    2025    2025    2026    2025"), used as a fallback header
# detector below when no single line carries a full "31.03.2026"-style
# date token - some filings (confirmed: Tata Chemicals) print the header's
# day/month/year on three separate lines ("31" / "March," / "2026")
# instead of Tata Steel's single-line "31.03.2026" style, so
# DATE_TOKEN_RE never matches anything in that header at all. Restricted
# to a realistic year range so this can't misfire on an unrelated line
# that happens to contain a 4-digit money figure.
_BARE_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _find_bare_year_header(lines, start, window_end):
    """Fallback for a header whose day/month/year are split across
    separate lines: finds a line within the window whose numeric tokens
    are ALL plausible years (so it can only be a header/date row, never a
    row of financial figures), and returns each year with its character
    offset in left-to-right order. Returns None if no such line exists in
    the window."""

    for i in range(start, window_end):

        line = lines[i]
        year_matches = list(_BARE_YEAR_RE.finditer(line))

        if len(year_matches) < 2:
            continue

        # Every OTHER number-like token on the line must itself be one of
        # the matched years - a line mixing a real year with an unrelated
        # figure (e.g. a money amount that happens to start with 20) is
        # not a pure year-header row and is skipped rather than guessed.
        all_tokens = list(re.finditer(r"\d[\d,]*", line))
        year_spans = {(m.start(), m.end()) for m in year_matches}
        if not all(
            (m.start(), m.end()) in year_spans for m in all_tokens
        ):
            continue

        return i, year_matches

    return None


def detect_header_periods(lines, start, end):

    window_end = min(end, start + 20)

    # Every offset this function returns must live in the SAME coordinate
    # system calibrate_columns_from_data() / locate_field() use for data
    # rows - i.e. AFTER the OCR line-number prefix ("0x48D: ") is
    # stripped, not before. Skipping this here would both misalign every
    # header-based column offset against the data-row ones by exactly the
    # prefix's length, and (for _find_bare_year_header below) let the
    # prefix's own digits ("0", "48" out of "0x48D:") count as "other
    # numeric tokens" on the line, which always fails that function's
    # all-tokens-are-years check and made it never fire at all.
    stripped = [strip_line_number_prefix_layout(lines[i]) for i in range(start, window_end)]

    # A Reg 52(4)-style filing's header routinely splits its quarterly and
    # annual date columns across TWO physical lines (confirmed, real case:
    # Tata Steel's own P&L header prints "on 31.03.2026  on 31.12.2025  on
    # 31.03.2025" on one line and the two annual "31.03.2026  31.03.2025"
    # dates on the very next one) - taking only the single line with the
    # MOST matches (the old behaviour) silently drops whichever line has
    # fewer, which is exactly the annual pair resolve_year_columns() needs
    # to match against the template's target years. Collecting every
    # qualifying line's dates - each keeping its OWN line's character
    # offset, since both lines share the same left-hand label-column width
    # and only differ in which columns are printed - reunites them without
    # requiring the two halves to already be on one line.
    first_line_idx = None
    dates = []

    for i in range(start, window_end):

        matches = list(DATE_TOKEN_RE.finditer(stripped[i - start]))

        if len(matches) < 2:
            continue

        if first_line_idx is None:
            first_line_idx = i

        for m in matches:

            day, month, year = m.groups()
            year = int(year)

            if year < 100:
                year += 2000

            dates.append(
                {
                    "day": int(day),
                    "month": int(month),
                    "year": year,
                    "offset": m.start(),
                }
            )

    if dates:

        return {
            "line_idx": first_line_idx,
            "dates": dates,
            "count": len(dates),
        }

    # Fallback: a header whose day/month/year are printed on separate
    # lines (no single line matches DATE_TOKEN_RE at all).
    bare = _find_bare_year_header(stripped, 0, len(stripped))

    if not bare:
        return None

    line_idx, year_matches = bare
    line_idx += start

    dates = [
        {"day": None, "month": None, "year": int(m.group()), "offset": m.start()}
        for m in year_matches
    ]

    return {
        "line_idx": line_idx,
        "dates": dates,
        "count": len(dates),
    }


# ============================================================
# YEAR-COLUMN ALIGNMENT
#
# Deciding which of a data row's numbers is the FY-current figure and
# which is FY-previous must not rest on "the last token is always
# FY-current, the second-to-last is always FY-previous" as a bare
# positional assumption - the very failure mode Step 4 of the extraction
# audit calls out. These two functions instead calibrate each section's
# actual current-year/previous-year column positions (character offset
# in the layout-preserving OCR text) so a row can be checked against - or,
# when it carries only one visible figure, resolved by - where its own
# numbers actually sit, not by array position alone.
# ============================================================

def resolve_year_columns(header_info, target_current_year, target_previous_year):
    """Matches the header's own detected year tokens (detect_header_periods)
    against the two fiscal years the template itself asks for
    (get_target_fy), and returns (current_x, previous_x) - the character
    offsets of the FY-current and FY-previous columns - or (None, None) if
    the header wasn't found or doesn't contain both target years.

    A header can repeat each year more than once (the standard Reg 52(4)
    layout prints Quarter-current/Quarter-prior-quarter/Quarter-year-ago/
    Year-current/Year-previous, so e.g. 2026 appears twice and 2025 three
    times) - the LAST (rightmost) occurrence of each target year is used,
    since the annual "Year ended"/"FY" columns are always the rightmost
    pair in every filing inspected (Tata Steel, Tata Chemicals), never an
    earlier quarterly column that happens to share the same calendar
    year."""

    if not header_info or target_current_year is None or target_previous_year is None:
        return None, None

    current_x = None
    previous_x = None

    for d in header_info["dates"]:
        if d["year"] == target_current_year:
            current_x = d["offset"]
        if d["year"] == target_previous_year:
            previous_x = d["offset"]

    if current_x is None or previous_x is None:
        return None, None

    return current_x, previous_x


def calibrate_columns_from_data(layout_lines, section):
    """Fallback column calibration for when the header itself can't be
    parsed (resolve_year_columns returned (None, None)): scans every line
    in the section and takes the median character offset of the last two
    number tokens across every line carrying 2+ tokens. Real filings
    right-align each column's figures to a consistent character position
    throughout a table (confirmed: Tata Steel and Tata Chemicals both hold
    their two columns within a few characters of each other across every
    row), so the median of many rows is a robust stand-in for a header
    this pipeline couldn't otherwise read.

    `layout_lines` must be the layout-PRESERVING lines (see
    _normalize_preserve_layout / _layout_number_tokens) - the ordinary,
    whitespace-collapsed `lines` would put every token near character
    offset 0, destroying the very column alignment this calibrates from.

    Assumes left=FY-current/right=FY-previous - the convention observed in
    every document seen so far, and the same convention select_fy_values()
    already assumed unconditionally. Requires at least MIN_CALIBRATION_ROWS
    qualifying rows before trusting the result, so a sparse/short section
    never calibrates off 1-2 rows' worth of noise; returns (None, None,
    False) when it can't, so callers fall back to the old, purely
    positional behaviour rather than a shaky calibration."""

    MIN_CALIBRATION_ROWS = 5

    firsts = []
    lasts = []

    for idx in range(section["start"], section["end"]):

        tokens = _layout_number_tokens(layout_lines[idx])
        filtered = filter_footnote_tokens(tokens)

        if len(filtered) >= 2:
            firsts.append(filtered[-2]["start"])
            lasts.append(filtered[-1]["start"])

    if len(firsts) < MIN_CALIBRATION_ROWS:
        return None, None, False

    firsts.sort()
    lasts.sort()
    current_x = firsts[len(firsts) // 2]
    previous_x = lasts[len(lasts) // 2]

    return current_x, previous_x, True



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


def _normalize_preserve_layout(text):
    """Same character-level substitutions as normalize_text() (non-
    breaking space, currency symbol, curly quotes, dash variants), but
    WITHOUT collapsing whitespace runs to a single space or stripping the
    line. normalize_text()'s final "\\s+ -> ' '" collapse is exactly what
    destroys LLMWhisperer's layout-preserving column alignment - a data
    row's two (or five) figures are right-aligned to consistent character
    positions across a whole table ONLY in the original, uncollapsed
    text. Used exclusively for column-position calibration (see
    "YEAR-COLUMN ALIGNMENT" below); every other use of OCR text in this
    module keeps using normalize_text() exactly as before."""

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

    return text


def strip_line_number_prefix_layout(line):
    """Mirrors strip_line_prefixes() below exactly - same four prefix
    shapes stripped (OCR line number, Schedule III serial number,
    alphabetic sub-item marker) - so a line yields the SAME set of
    numeric tokens either way (needed for _with_layout_offsets() to be
    able to zip the two token lists 1:1). The only difference is how much
    trailing whitespace each strip consumes.

    strip_line_prefixes() consumes the ENTIRE run of whitespace after
    each prefix (\\s*/\\s+, unbounded), which is exactly right for VALUE
    parsing (a token's position within the line is irrelevant there) but
    wrong for column-POSITION purposes: on a value-only Layout-B line
    (the row's figures on their own physical line, right-aligned with a
    wide gap before them - see _layout_number_tokens), that unbounded
    match eats the entire gap and collapses the row's first token to
    character offset ~0, destroying the alignment column-calibration
    depends on. Capping each strip at a few characters keeps the prefix
    itself out of the count while leaving a genuine column-alignment gap
    (which runs far longer than any real prefix separator ever does)
    intact."""
    line = re.sub(r"^\s*0[xX][0-9A-Fa-f]{1,8}\s*[|:] ?", "", line)
    line = re.sub(r"^\s*\d{1,5}\s*[|:] ?", "", line)
    line = re.sub(r"^\s*\d{1,2}\)?\s{1,3}", "", line)
    line = re.sub(r"^\s*[a-zA-Z]\)\s{0,3}", "", line)
    return line


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

# Bounded (\s{1,2}, not \s+) counterparts of the two defect regexes above,
# for use in _layout_number_tokens() only: both are already narrowly-
# shaped enough (a dangling trailing comma, or 2 digits immediately
# followed by a closing paren) that neither could plausibly straddle a
# genuine inter-column gap - real figures never end in a bare comma or
# sit immediately before an unrelated ")" - but capping the span costs
# nothing and removes any doubt when computing column positions from
# otherwise-uncollapsed, wide-gapped text.
_STRAY_SPACE_IN_GROUPED_NUMBER_LAYOUT_RE = re.compile(
    r"(\d{1,2}(?:,\d{2,3})*,)\s{1,2}(\d{2,3}(?:\.\d+)?)"
)
_SPACE_AS_DECIMAL_BEFORE_PAREN_LAYOUT_RE = re.compile(
    r"(\d(?:,\d{2,3})+)\s{1,2}(\d{2})\)"
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

        value, decimal_repaired = repair_ocr_decimal_defect(raw, value)

        tokens.append(
            {
                "raw": raw,
                "value": value,
                "start": match.start(),
                "decimal_repaired": decimal_repaired,
            }
        )

    return tokens


def _layout_number_tokens(raw_line):
    """Character offsets of this line's numeric tokens in the ORIGINAL,
    layout-preserving OCR text (before normalize_text()'s whitespace
    collapse), for column-position calibration only - see "YEAR-COLUMN
    ALIGNMENT" below. Uses the bounded (\\s{1,2}) counterparts of the two
    OCR-defect regexes that fix a stray space inside one number - the
    ordinary, unbounded versions are for VALUE parsing, where a genuine
    inter-column gap is never mistaken for the defect anyway (both require
    a dangling trailing comma, or 2 digits immediately before a closing
    paren, neither of which a real column boundary produces - see their
    own docstrings) - but capping the span here removes any doubt when the
    surrounding whitespace hasn't been collapsed. Value parsing always
    still goes through the ordinary extract_number_tokens() on the
    normalized line, unchanged - this function only ever supplies a
    position, never a value."""

    if not raw_line:
        return []

    line = _normalize_preserve_layout(raw_line)
    line = strip_line_number_prefix_layout(line)
    line = UNDERSCORE_DECIMAL_RE.sub(".", line)
    line = _STRAY_SPACE_IN_GROUPED_NUMBER_LAYOUT_RE.sub(r"\1\2", line)
    line = _SPACE_AS_DECIMAL_BEFORE_PAREN_LAYOUT_RE.sub(r"\1.\2)", line)
    line = _reconstruct_indian_multidot_numbers(line)
    line = re.sub(r"[(\[]\s*\d+(?:\s*[+\-]\s*\d+)+\s*[)\]]", "", line)

    tokens = []

    for match in NUMBER_TOKEN_RE.finditer(line):

        raw = match.group()

        if clean_number(raw) is None:
            continue

        tokens.append({"raw": raw, "start": match.start()})

    return tokens


def _with_layout_offsets(tokens, raw_line):
    """Replaces each token's "start" with its true column position from
    the original layout-preserving line, when the layout-based re-parse
    of that same line finds exactly the same number of tokens (in which
    case they correspond 1:1, in order, to `tokens`). If the counts
    differ - e.g. a stray-space defect that only the full, whitespace-
    collapsed parse in extract_number_tokens() knows how to repair -
    `tokens` is returned unchanged (still valid, just without a reliable
    layout offset for this particular row) rather than mismatching them."""

    if raw_line is None:
        return tokens

    layout_tokens = _layout_number_tokens(raw_line)

    if len(layout_tokens) != len(tokens):
        return tokens

    return [
        {**t, "start": layout["start"]}
        for t, layout in zip(tokens, layout_tokens)
    ]


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

# A line that opens its own enumerated sub-item - "(i) ...", "(ii) ...",
# "(a) ...", "a) ..." - immediately after start-of-line (or a "|"/":"
# delimiter). Deliberately loose (a bare character-class match, not real
# roman-numeral validation) - this is only ever used to recognize "this
# line visibly belongs to some OTHER named row", never to parse the
# marker's actual value.
_ENUMERATED_ITEM_MARKER_RE = re.compile(r"(?:^|[|:])\s*\(?(?:[ivxIVX]{1,4}|[a-hA-H])[.)]\s*\S")


def _line_is_independent_item(line, aliases):
    """True if `line` visibly opens its own enumerated sub-item (its own
    "(i)"/"(ii)"/"(a)"/"(b)"/... marker) for something OTHER than the
    field currently being searched for. Schedule III/Ind-AS statements
    routinely place several *different* line items - many with no
    tracked field of their own at all (Trade Receivables, Bank Balances,
    Other Current Liabilities, ...) - immediately next to a field's own
    label, in filings whose table layout puts a row's numbers on a
    different physical line from its label. line_belongs_to_other_field()
    only recognizes this app's own tracked fields; without this
    additional check, an untracked neighbour's numbers could be silently
    borrowed for a field they do not belong to. A line matching the
    CURRENT field's own aliases is never excluded this way - the same
    field's row is always fair game no matter how it is marked."""
    if not _ENUMERATED_ITEM_MARKER_RE.search(line):
        return False
    normalized = _collapse_label_punctuation(normalize_text(line).lower())
    return not any(
        _alias_present(normalized, _collapse_label_punctuation(normalize_text(alias).lower()))
        for alias in aliases
    )


def locate_field(lines, section, aliases, layout_lines=None):

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

    # A field's alias can tie across more than one line in standard
    # Schedule III/Ind-AS filings - a lettered sub-component ("a) Sales/
    # income from operations"), a same-named sub-line ("(b) Other
    # current liabilities"), or a bare subheading - against the explicit
    # "Total <item>" line that is the authoritative summary figure, not
    # any one sub-component or subheading. Whenever candidates tie and
    # at least one of them is explicitly marked "Total", prefer that one
    # - regardless of whether the losing candidates are lettered items,
    # already carry their own inline numbers, or not. Every candidate
    # here already passed a genuine, field-specific alias match, so this
    # only reorders among already-relevant lines - it never introduces a
    # new, unrelated match.
    if len(candidates) > 1:

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

    if len(tokens) == 0:

        # The matched line carries only the label - either a long item
        # name wrapped across OCR lines, a ratio name followed by its
        # formula description before the figures, or (in filings whose
        # table layout prints a row's numbers on the physical line
        # ABOVE its label rather than beside/after it) the label of a
        # row whose own numbers already went by. Look at the
        # surrounding lines for the row's actual numbers, but never
        # cross into a line that is itself another field's labelled
        # row, or another row's own labelled data entirely (see
        # _line_is_independent_item).
        #
        # Deliberately NOT triggered when the matched line already
        # carries ONE number of its own (0 < len(tokens) < 2): that is
        # a single column genuinely present on this exact row (the
        # other column blank/absent in the source), not a signal to go
        # looking elsewhere - borrowing a nearby line's numbers in that
        # case would silently substitute a DIFFERENT row's figures
        # rather than correctly reporting the second year as
        # unavailable. See select_fy_values's own single-token handling.
        #
        # -1 (the immediately preceding line) is tried FIRST, ahead of
        # the forward offsets: in a "numbers-line before label-line"
        # table (see above), the row's own genuine figures are always
        # its immediate predecessor, one line away - whereas a forward
        # match found first could easily be a *different*, closer
        # untracked row's own numbers (nothing marks them as foreign
        # the way an enumerated item is marked - see
        # _line_is_independent_item). The forward-scanning ratio-table
        # case (formula description lines, each with fewer than 2
        # tokens, interposed before the real figures) is unaffected: -1
        # there is either excluded outright or simply lacks 2 tokens of
        # its own, so the loop falls through to the forward offsets
        # exactly as before.
        offsets = [-1] + list(range(1, MAX_LOOKAHEAD_LINES + 1))

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

            if _line_is_independent_item(lines[candidate_idx], aliases):
                continue

            candidate_tokens = extract_number_tokens(
                lines[candidate_idx]
            )

            if len(candidate_tokens) >= 2:
                tokens = candidate_tokens
                numeric_idx = candidate_idx
                break

    if layout_lines is not None:
        # Re-expresses each token's "start" in terms of the ORIGINAL,
        # layout-preserving OCR line (see YEAR-COLUMN ALIGNMENT) instead
        # of the whitespace-collapsed one tokens was computed from - the
        # only thing select_fy_values()'s column-position resolution can
        # meaningfully compare against a section's calibrated column
        # offsets. A count mismatch (rare - see _with_layout_offsets)
        # leaves tokens exactly as already computed.
        tokens = _with_layout_offsets(tokens, layout_lines[numeric_idx])

    return {
        "label_line_idx": best_idx,
        "label_line_text": lines[best_idx],
        "numeric_line_idx": numeric_idx,
        "numeric_line_text": lines[numeric_idx],
        "tokens": tokens,
    }


# How far a single lone number's own character offset must sit from the
# FURTHER of the two calibrated column positions, as a fraction of the
# gap between them, before it's trusted as belonging to the NEARER one
# (rather than left REVIEW_REQUIRED). 0.25 means the token must fall
# within the quarter of the gap closest to one side - comfortably clear
# of the midpoint, so a token sitting near the middle (which could
# plausibly be an OCR-shifted reading from either column) is never
# silently assigned to either year.
_COLUMN_MARGIN_FRACTION = 0.25


def _closer_column(offset, current_x, previous_x):
    """Which of the two calibrated column positions `offset` sits nearer
    to - "current", "previous", or None if it isn't clearly closer to
    either (within _COLUMN_MARGIN_FRACTION of the midpoint). Never used to
    invent a value, only to decide which year an ALREADY-EXTRACTED number
    belongs to."""

    if current_x is None or previous_x is None or current_x == previous_x:
        return None

    gap = abs(previous_x - current_x)
    margin = gap * _COLUMN_MARGIN_FRACTION

    dist_current = abs(offset - current_x)
    dist_previous = abs(offset - previous_x)

    if dist_current < dist_previous and dist_previous - dist_current >= margin:
        return "current"
    if dist_previous < dist_current and dist_current - dist_previous >= margin:
        return "previous"
    return None


def select_fy_values(tokens, expected_count, current_x=None, previous_x=None):

    filtered = filter_footnote_tokens(tokens)
    have_columns = current_x is not None and previous_x is not None

    if len(filtered) >= 2:

        fy_current = filtered[-2]["value"]
        fy_previous = filtered[-1]["value"]
        validation_status = "OK"

        if expected_count and len(filtered) == expected_count:
            confidence = "high"
        elif expected_count:
            confidence = "medium"
        else:
            confidence = "low"

        # Cross-check the array-position assumption (last-two-tokens =
        # [current, previous]) against where these two tokens actually
        # sit, when a column calibration is available. This never
        # overrides a clean match (the last two tokens landing nearer
        # their expected column each) - it only catches the row genuinely
        # printing them in the OPPOSITE order (a mismatch this pipeline
        # must not silently paper over per the year-alignment audit).
        if have_columns:
            side_a = _closer_column(filtered[-2]["start"], current_x, previous_x)
            side_b = _closer_column(filtered[-1]["start"], current_x, previous_x)
            if side_a == "previous" and side_b == "current":
                # Positions say the array order is reversed for this row.
                fy_current, fy_previous = fy_previous, fy_current
                validation_status = "OK"
            elif side_a is not None and side_b is not None and side_a == side_b:
                # Both of the last two tokens sit nearer the SAME column -
                # they cannot both be that year's figure, so which one is
                # FY-current cannot be read off this row with confidence.
                validation_status = "REVIEW_REQUIRED"

    elif len(filtered) == 1:

        offset = filtered[0]["start"]
        side = _closer_column(offset, current_x, previous_x) if have_columns else None

        if side == "current":
            fy_current = filtered[0]["value"]
            fy_previous = None
            confidence = "medium"
            validation_status = "OK"
        elif side == "previous":
            # The row's one visible figure sits in the FY-previous column
            # position - e.g. the FY-current cell was genuinely blank/nil
            # in the source (confirmed real case: Tata Chemicals' Cash and
            # cash equivalents row prints only one number, aligned to the
            # FY-previous column, with the FY-current column empty).
            # Assigning it to "current" merely because it's the only token
            # present would silently shift a real prior-year figure into
            # the wrong year - exactly what the year-alignment audit
            # requires this pipeline not to do.
            fy_current = None
            fy_previous = filtered[0]["value"]
            confidence = "medium"
            validation_status = "OK"
        elif have_columns:
            # A column calibration exists but this token isn't clearly
            # closer to either side - genuinely ambiguous, not guessed.
            fy_current = None
            fy_previous = None
            confidence = "low"
            validation_status = "REVIEW_REQUIRED"
        else:
            # No column calibration available for this section at all
            # (header unparseable AND too few data rows to calibrate from)
            # - falls back to the pipeline's original behaviour (treat the
            # lone figure as FY-current) rather than blocking every such
            # row across a whole section on a missing calibration.
            fy_current = filtered[0]["value"]
            fy_previous = None
            confidence = "low"
            validation_status = "OK"

    else:

        fy_current = None
        fy_previous = None
        confidence = "none"
        validation_status = "OK"

    return fy_current, fy_previous, confidence, filtered, validation_status


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
def select_fy_values_net_worth(tokens, expected_count, current_x=None, previous_x=None):

    filtered = filter_footnote_tokens(tokens)

    # Fewer than 3 tokens means this filing's Net worth line was not
    # printed as the standard 5-column quarterly-plus-annual
    # disclosure (e.g. a plain 2-column balance-sheet-style row) -
    # falls back to the ordinary last-two-tokens selection rather than
    # indexing into columns that were never established to exist.
    if len(filtered) < 3:
        return select_fy_values(tokens, expected_count, current_x, previous_x)

    fy_current = filtered[0]["value"]
    fy_previous = filtered[2]["value"]

    confidence = (
        "high" if expected_count and len(filtered) == expected_count
        else "medium"
    )

    return fy_current, fy_previous, confidence, filtered, "OK"


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
# OCR DECIMAL-POINT-DEFECT REPAIR
#
# A real, confirmed OCR defect in these filings' comma-grouped numbers:
# the decimal point is dropped and its 2 fractional digits merge into the
# preceding comma group, e.g. "1,03269" for what the source actually
# prints as "1,032.69", or "6,06714" for "6,067.14" (both found, byte for
# byte, in the Tata Steel filing's own raw OCR text - "Other balances with
# banks" and "Interest paid" respectively).
#
# This used to be "repaired" by comparing a value against its own
# sibling (the same field's other reported year) and guessing a /100
# correction whenever the two looked "implausibly" far apart - but
# "implausibly far apart" is not a safe signal on its own: real financial
# figures legitimately swing by 40x+ year-over-year (confirmed, real case:
# Tata Chemicals' Current Borrowings genuinely falls from Rs 481 crore to
# Rs 12 crore), and that sibling-comparison heuristic mangled the
# genuine 481 into 4.81 purely because it looked "too different" from 12 -
# exactly the "silent unit-scaling guess" the extraction audit forbids.
#
# The fix below instead reads the defect directly off ONE token's own
# text shape - never compares it to any other field, year, or sibling -
# so a clean, un-grouped, genuinely large number like "481" (no comma at
# all) can never be mistaken for this defect, while "1,03269" and
# "6,06714" (which DO carry the tell-tale malformed 5-digit trailing
# comma-group) are still corrected exactly as before.
# ============================================================

# A comma-grouped number whose FINAL (rightmost) group has more than the
# 3 digits every valid thousands-grouping convention requires. Restricted
# to exactly 5 trailing digits (3 valid grouping digits + 2 merged
# decimal digits) - the one shape actually confirmed in these filings;
# a 4- or 6+-digit trailing group has no single unambiguous
# interpretation and is deliberately left unrepaired rather than guessed.
_MALFORMED_TRAILING_GROUP_RE = re.compile(r"^-?\d{1,2}(?:,\d{2,3})*,(\d{4,})$")

# The opposite-shaped defect: OCR misreads the decimal point itself as a
# comma (e.g. "0,25" for "0.25" - confirmed, real case: Tata Steel's own
# "Total debts to total assets ratio" row). A comma's rightmost group must
# have exactly 3 digits under every real thousands-grouping convention
# (international, or Indian for the group closest to the decimal point) -
# a group of exactly 1-2 digits sitting at the very END of the number is
# never a genuine thousands separator (nobody writes "1,25" to mean
# "1,250"; if the value were under 1000 it would never be comma-grouped
# at all), so this shape is just as unambiguous as the one above, and
# purely a property of this one token's own text - never a sibling
# comparison.
_COMMA_AS_DECIMAL_RE = re.compile(r"^-?\d{1,2}(?:,\d{2,3})*,(\d{1,2})$")


def repair_ocr_decimal_defect(raw, value):
    """Per-token, text-shape-only repair for the dropped-decimal defect
    described above. `raw` is the token's own original matched text (e.g.
    "(6,06714)"); `value` is its already-parsed float (sign and any %/x
    suffix already applied by clean_number()). Returns (possibly-corrected
    value, True) when the defect's exact signature is found, else
    (value, False) unchanged - never touches a token with no comma, or
    whose trailing group is already a valid 2- or 3-digit group."""

    if value is None:
        return value, False

    core = raw.strip()
    if core.startswith("(") and core.endswith(")"):
        core = core[1:-1]
    core = core.rstrip("%xX").strip()

    match = _MALFORMED_TRAILING_GROUP_RE.match(core)
    if match and len(match.group(1)) == 5:
        digits = core.replace(",", "")
        corrected_magnitude = float(digits[:-2] + "." + digits[-2:])
        return (-corrected_magnitude if value < 0 else corrected_magnitude), True

    match = _COMMA_AS_DECIMAL_RE.match(core)
    if match:
        last_comma = core.rindex(",")
        whole_part = core[:last_comma].replace(",", "").replace("-", "") or "0"
        frac_part = core[last_comma + 1:]
        corrected_magnitude = float(whole_part + "." + frac_part)
        return (-corrected_magnitude if value < 0 else corrected_magnitude), True

    return value, False


# ============================================================
# EXTRACT FINANCIAL VALUES
# ============================================================

def extract_financial_values(ocr_text, template_path):

    # `lines` (whitespace-collapsed) drives every text-content match in
    # this function - alias matching, section headings, subsection
    # headings - exactly as before. `layout_lines` keeps the SAME lines
    # (same filtering, so the two stay index-aligned) with their original
    # LLMWhisperer column spacing intact, for column-position calibration
    # only (see "YEAR-COLUMN ALIGNMENT" above) - normalize_text()'s
    # whitespace collapse would otherwise put every token near character
    # offset 0, destroying the alignment a column calibration depends on.
    raw_and_normalized = [
        (raw, normalize_text(raw)) for raw in ocr_text.splitlines()
    ]
    raw_and_normalized = [
        (raw, norm) for raw, norm in raw_and_normalized if norm
    ]
    lines = [norm for _raw, norm in raw_and_normalized]
    layout_lines = [
        _normalize_preserve_layout(raw) for raw, _norm in raw_and_normalized
    ]

    target_current_year, target_previous_year, current_label, previous_label = (
        get_target_fy(template_path)
    )

    sections = find_statement_sections(lines)

    section_by_type = {}
    header_by_type = {}
    columns_by_type = {}

    for section_type in ("PNL", "BS", "CF", "RATIO"):

        section = pick_section(sections, section_type)
        section_by_type[section_type] = section

        if not section:
            header_by_type[section_type] = None
            columns_by_type[section_type] = (None, None, "NO_SECTION")
            continue

        header_info = detect_header_periods(
            layout_lines, section["start"], section["end"]
        )
        header_by_type[section_type] = header_info

        # Year-column calibration for this whole section (see
        # "YEAR-COLUMN ALIGNMENT" above): prefer the header's own detected
        # year positions, matched against the two fiscal years the
        # template itself asks for; fall back to the median position of
        # every 2-token data row in the section when the header can't be
        # read (e.g. a split-across-lines date header this pipeline still
        # doesn't recognise). Computed ONCE per section and reused for
        # every field in it (and every one of its BS subsections, which
        # share the same table and therefore the same columns) rather
        # than recalibrated per field.
        current_x, previous_x = resolve_year_columns(
            header_info, target_current_year, target_previous_year
        )
        column_source = "header"

        if current_x is None or previous_x is None:
            current_x, previous_x, confident = calibrate_columns_from_data(
                layout_lines, section
            )
            column_source = "data_median" if confident else "unavailable"

        columns_by_type[section_type] = (current_x, previous_x, column_source)

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
        current_x, previous_x, column_source = columns_by_type[section_type]

        aliases = ALIASES[field]

        loc = locate_field(lines, section, aliases, layout_lines)

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
                "validation_status": "NOT_FOUND",
                "extraction_method": None,
                "source_line_number": None,
                "source_text": None,
                "confidence": "none",
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
                    "Validation Status": "NOT_FOUND",
                }
            )

            continue

        selector = (
            select_fy_values_net_worth if field == "Net worth"
            else select_fy_values
        )

        fy_current, fy_previous, confidence, filtered_tokens, validation_status = (
            selector(loc["tokens"], expected_count, current_x, previous_x)
        )

        # A dropped-decimal OCR defect (see repair_ocr_decimal_defect) was
        # already corrected per-token inside extract_number_tokens(),
        # purely from that token's own text shape - never by comparing it
        # to a sibling value. "normalized" here is only a traceability
        # flag recording that this row's own figures needed that
        # correction, for the debug report - it does not change any value.
        normalized = any(t.get("decimal_repaired") for t in filtered_tokens)

        # "ambiguous" now means the column/year alignment for this row
        # could not be confidently resolved (validation_status ==
        # REVIEW_REQUIRED, set by select_fy_values() above when a row's
        # own number(s) don't clearly align with either calibrated
        # column) - NOT a magnitude-ratio guess. A field flagged this way
        # is intentionally withheld by populate_workbook() rather than
        # writing a value this pipeline cannot confidently attribute to
        # the right fiscal year.
        ambiguous = validation_status == "REVIEW_REQUIRED"

        extraction_method = (
            "same_line" if loc["numeric_line_idx"] == loc["label_line_idx"]
            else "adjacent_line"
        )

        # extracted[field] keeps the actual parsed figures plus enough
        # metadata (source line, method, column-calibration source,
        # validation status) to answer "why this value, from where, how
        # confident" without cluttering the normal UI - see the extraction
        # audit's traceability requirement. populate_workbook() is the
        # single place that decides, from the "ambiguous" flag, whether
        # the figures are safe to write. debug_rows mirrors the same
        # parsed figures + flags so the diagnostic can report *what was
        # found* alongside *why it was suppressed*, rather than looking
        # like nothing was found at all.
        extracted[field] = {
            "fy_current": fy_current,
            "fy_previous": fy_previous,
            "normalized": normalized,
            "ambiguous": ambiguous,
            "validation_status": validation_status,
            "extraction_method": extraction_method,
            "source_line_number": loc["numeric_line_idx"],
            "source_text": loc["numeric_line_text"],
            "confidence": confidence,
            "column_source": column_source,
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
                "Validation Status": validation_status,
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

        # A field flagged ambiguous (its column/year alignment could not
        # be confidently resolved - validation_status == REVIEW_REQUIRED,
        # set by select_fy_values()'s column-position check) is never
        # written. The cell is actively cleared rather than
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
        "Normalized",
        "Ambiguous",
        "Validation Status",
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
