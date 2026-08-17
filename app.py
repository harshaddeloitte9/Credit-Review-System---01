import csv
import os
import re
import tempfile
from copy import copy
from datetime import datetime
from io import BytesIO

import streamlit as st
from openpyxl import load_workbook
from openpyxl.styles import Font

from unstract.llmwhisperer import LLMWhispererClientV2
from unstract.llmwhisperer.client_v2 import LLMWhispererClientException


# ============================================================
# CONFIGURATION
# ============================================================

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
# STREAMLIT PAGE
# ============================================================

st.set_page_config(
    page_title="Credit Review Tool — Input Data",
    page_icon="📄",
    layout="wide",
)

st.title("Credit Review Tool — Input Data Extraction")

st.caption(
    "Scanned Financial Statement PDF → OCR → Financial Data Extraction → Excel"
)

st.info(
    "Current scope: only the '1-Input Data' sheet is populated. "
    "Downstream sheets are not modified."
)


# ============================================================
# CHECK EXCEL TEMPLATE
# ============================================================

if not os.path.exists(TEMPLATE_FILE):

    st.error(
        "Excel template is not available."
    )

    st.caption(
        "Make sure Credit_Review_Tool_Input_Data_Template.xlsx "
        "is present in the same folder as app.py."
    )

    st.stop()


# ============================================================
# PDF UPLOAD
# ============================================================

st.subheader("Upload Financial Statement")

pdf_file = st.file_uploader(
    "Upload scanned Financial Statement PDF",
    type=["pdf"],
)


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
# Rows that hold template formulas (Total income, Net finance
# charges, EBITDA, Total debt, Net debt, Average equity, all
# validation/reconciliation rows) are deliberately absent here -
# those cells already carry the correct formula in the template
# and must never be overwritten.
ROW_MAPPING = {

    # Profit & Loss
    "Revenue from operations": 5,
    "Other income": 6,
    "Total expenses": 8,
    "Finance costs": 9,
    "Depreciation": 10,
    "Interest income": 11,
    "Profit before exceptional items": 12,
    "Exceptional items": 13,
    "Profit before tax": 14,
    "Total tax expense": 15,
    "Net profit": 16,

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

    # Reported Ratios
    "DSCR": 49,
    "ISCR": 50,
    "Current ratio": 51,
    "Debt equity": 52,
    "Debt assets": 53,
    "Operating EBITDA margin": 54,
    "Net profit margin": 55,
    "Net worth": 56,
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
MAGNITUDE_ROWS = {41, 42, 43, 44, 45, 46}

# "Total income" is not written to B7/C7 (that is a template
# formula, =B5+B6). It is extracted independently only so that
# D7 - the "Reported (validation)" column - can show the figure
# as actually printed in the statement, giving a real tie-out
# against the computed B5+B6 instead of a self-referencing echo.
EXTRA_FIELDS = ["Total income"]

FIELD_SECTION = {
    "Revenue from operations": "PNL",
    "Other income": "PNL",
    "Total income": "PNL",
    "Total expenses": "PNL",
    "Finance costs": "PNL",
    "Depreciation": "PNL",
    "Interest income": "PNL",
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

    "DSCR": "RATIO",
    "ISCR": "RATIO",
    "Current ratio": "RATIO",
    "Debt equity": "RATIO",
    "Debt assets": "RATIO",
    "Operating EBITDA margin": "RATIO",
    "Net profit margin": "RATIO",
    "Net worth": "RATIO",
}

ALIASES = {

    # --------------------------------------------------------
    # PROFIT & LOSS
    # --------------------------------------------------------

    "Revenue from operations": [
        "revenue from operations",
        "revenue from operation",
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
    ],

    "Borrowings current": [
        "borrowings - current",
        "borrowings current",
        "current borrowings",
    ],

    "Lease liabilities non current": [
        "lease liabilities - non-current",
        "lease liabilities non-current",
        "non-current lease liabilities",
        "non current lease liabilities",
    ],

    "Lease liabilities current": [
        "lease liabilities - current",
        "lease liabilities current",
        "current lease liabilities",
    ],

    "Current investments": [
        "current investments",
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
    ],

    "Net profit margin": [
        "net profit margin",
    ],

    "Net worth": [
        "net worth",
    ],
}

# Flattened, lower-cased list of every alias across every field.
# Used to detect whether a candidate "neighbour" line (see
# locate_field below) is actually somebody else's labelled row,
# so a wrapped label never steals another field's numbers.
ALL_ALIASES_FLAT = sorted(
    {
        alias.lower()
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

    text = text.replace(" ", " ")
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
    # OCR line number (e.g. "1234 | text" / "1234: text"). That is
    # not part of the statement and must not be read as a value.
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


def extract_number_tokens(line):

    if not line:
        return []

    line = normalize_text(line)
    line = strip_line_prefixes(line)

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


def alias_score(line, aliases):

    normalized = normalize_text(line).lower()

    score = 0

    for alias in aliases:

        alias_lower = normalize_text(alias).lower()

        if alias_lower in normalized:
            score = max(score, len(alias_lower))

    return score


def line_matches_any_alias(line):

    normalized = normalize_text(line).lower()

    for alias in ALL_ALIASES_FLAT:

        if alias in normalized:
            return True

    return False


# ============================================================
# LOCATE A FIELD'S DATA WITHIN A SECTION
# ============================================================

def locate_field(lines, section, aliases):

    if section:
        search_range = range(section["start"], section["end"])
    else:
        search_range = range(len(lines))

    best_idx = None
    best_score = 0

    for idx in search_range:

        score = alias_score(lines[idx], aliases)

        if score > best_score:
            best_score = score
            best_idx = idx

    if best_idx is None:
        return None

    numeric_idx = best_idx
    tokens = extract_number_tokens(lines[best_idx])

    if len(tokens) < 2:

        # The matched line only carries the label (a long item
        # name wrapped across OCR lines). Look at the immediately
        # surrounding lines for the row's actual numbers, but
        # never cross into a line that is itself another field's
        # labelled row.
        for offset in (1, 2, -1):

            candidate_idx = best_idx + offset

            if candidate_idx < 0 or candidate_idx >= len(lines):
                continue

            if section and not (
                section["start"] <= candidate_idx < section["end"]
            ):
                continue

            if line_matches_any_alias(lines[candidate_idx]):
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

    extracted = {}
    debug_rows = []

    all_fields = list(ROW_MAPPING.keys()) + EXTRA_FIELDS

    for field in all_fields:

        section_type = FIELD_SECTION[field]
        section = section_by_type[section_type]
        header_info = header_by_type[section_type]
        expected_count = header_info["count"] if header_info else None

        aliases = ALIASES[field]

        loc = locate_field(lines, section, aliases)

        row = ROW_MAPPING.get(field)
        excel_cell = (
            f"B{row}, C{row}" if row else "D7"
        )

        if loc is None:

            extracted[field] = {
                "fy_current": None,
                "fy_previous": None,
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
                }
            )

            continue

        fy_current, fy_previous, confidence, filtered_tokens = (
            select_fy_values(loc["tokens"], expected_count)
        )

        extracted[field] = {
            "fy_current": fy_current,
            "fy_previous": fy_previous,
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

        # FY2025 = Column C
        set_extracted_value(
            ws.cell(row=row, column=3),
            fy_previous
        )

    # --------------------------------------------------------
    # D7 - "Reported (validation)" column
    #
    # D12/D14/D16 stay as template-supplied ties to their own
    # extraction row (there is no independent second source for
    # those figures). D7 is different: "Total income" is also
    # printed as its own line in the statement, independently of
    # the B5+B6 formula in B7, so it is used here as a genuine
    # cross-check rather than a self-referencing echo.
    # --------------------------------------------------------

    total_income = extracted.get("Total income", {})
    reported_total_income = total_income.get("fy_current")

    if reported_total_income is not None:
        set_extracted_value(ws["D7"], reported_total_income)
    else:
        ws["D7"] = "=B7"

    ws["D12"] = "=B12"
    ws["D14"] = "=B14"
    ws["D16"] = "=B16"

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


# ============================================================
# MAIN PROCESS
# ============================================================

if st.button(
    "🚀 Extract & Generate Input Data",
    type="primary",
    use_container_width=True,
):

    # --------------------------------------------------------
    # API KEY CHECK
    # --------------------------------------------------------

    if not LLMWHISPERER_API_KEY:

        st.error(
            "Document extraction is currently unavailable."
        )

        st.stop()

    # --------------------------------------------------------
    # PDF CHECK
    # --------------------------------------------------------

    if not pdf_file:

        st.warning(
            "Please upload the scanned Financial Statement PDF."
        )

        st.stop()

    pdf_path = None

    try:

        # ====================================================
        # SAVE PDF
        # ====================================================

        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".pdf"
        ) as tmp_pdf:

            tmp_pdf.write(
                pdf_file.getbuffer()
            )

            pdf_path = tmp_pdf.name

        # ====================================================
        # STEP 1 — OCR
        # ====================================================

        st.subheader(
            "1. Document OCR"
        )

        progress = st.progress(0)

        progress.progress(10)

        with st.spinner(
            "Extracting document data..."
        ):

            ocr_text, whisper_response = (
                run_llmwhisperer(
                    pdf_path
                )
            )

        progress.progress(50)

        st.success(
            "✓ Document extraction completed."
        )

        # ====================================================
        # OCR PREVIEW
        # ====================================================

        with st.expander(
            "View extracted document text"
        ):

            st.text_area(
                "OCR output",
                ocr_text,
                height=400
            )

        # ====================================================
        # STEP 2 — FINANCIAL EXTRACTION
        # ====================================================

        st.subheader(
            "2. Financial Data Extraction"
        )

        with st.spinner(
            "Mapping extracted data to the Input Data structure..."
        ):

            extracted, debug_rows, meta = (
                extract_financial_values(
                    ocr_text,
                    TEMPLATE_FILE,
                )
            )

        progress.progress(70)

        # ====================================================
        # EXTRACTION STATUS
        # ====================================================

        found = sum(
            1
            for item in debug_rows
            if item["OCR Source Line"] != "NOT FOUND"
        )

        not_found = (
            len(debug_rows)
            - found
        )

        col1, col2 = st.columns(2)

        col1.metric(
            "Fields detected",
            found
        )

        col2.metric(
            "Fields not detected",
            not_found
        )

        # ====================================================
        # DEBUG DETAILS
        # ====================================================

        with st.expander(
            "View extraction details"
        ):

            st.dataframe(
                debug_rows,
                use_container_width=True
            )

        if DEBUG_MODE:

            debug_paths = save_debug_artifacts(
                ocr_text, debug_rows
            )

            with st.expander(
                "DEBUG - Validation Report"
            ):

                st.caption(
                    f"Target FY: {meta['target_current_label']} "
                    f"vs {meta['target_previous_label']}"
                )

                st.dataframe(
                    debug_rows,
                    use_container_width=True
                )

                if debug_paths:

                    st.caption(
                        f"Raw OCR saved to {debug_paths['raw_path']}"
                    )

                    st.caption(
                        f"Validation report saved to "
                        f"{debug_paths['report_path']}"
                    )

        # ====================================================
        # STEP 3 — GENERATE EXCEL
        # ====================================================

        st.subheader(
            "3. Generate Input Data Excel"
        )

        with st.spinner(
            "Populating the Input Data sheet..."
        ):

            wb = populate_workbook(
                TEMPLATE_FILE,
                extracted
            )

            output = BytesIO()

            wb.save(
                output
            )

            output.seek(0)

        progress.progress(100)

        st.success(
            "✓ Input Data Excel generated successfully."
        )

        # ====================================================
        # DOWNLOAD
        # ====================================================

        st.download_button(
            label="⬇️ Download Credit Review Tool Input Data Excel",
            data=output.getvalue(),
            file_name=OUTPUT_FILE,
            mime=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            use_container_width=True
        )

    # ========================================================
    # LLMWHISPERER ERROR
    # ========================================================

    except LLMWhispererClientException as e:

        st.error(
            "Document extraction failed."
        )

        st.code(
            str(e)
        )

    # ========================================================
    # GENERAL ERROR
    # ========================================================

    except Exception as e:

        st.error(
            "Something went wrong while processing the document."
        )

        st.exception(e)

    # ========================================================
    # CLEANUP
    # ========================================================

    finally:

        if (
            pdf_path
            and os.path.exists(pdf_path)
        ):

            try:
                os.remove(
                    pdf_path
                )
            except Exception:
                pass
