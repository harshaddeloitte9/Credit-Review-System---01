import os
import tempfile

import streamlit as st
from openpyxl import load_workbook

from reference_data import load_reference_bundle, ReferenceDataError
from workbook_validation import validate_workbook
from demo_source import build_demo_workbook, DemoSourceError, EXTRACTED_SHEET_FILE, EXTRACTED_SHEET_PATH
from input_data_engine import compute_derived_metrics, compute_calculated_ratios, run_validation_checks
from financial_analysis import build_financial_analysis
from risk_scoring import build_risk_scoring
from covenants import load_covenant_policy, build_covenants
from collateral_assessment import load_collateral_inputs, build_collateral_assessment
from limit_setting import load_limit_setting_policy, build_limit_setting
from continuous_monitoring import load_monitoring_policy, build_continuous_monitoring
from portfolio_monitoring import load_portfolio_inputs, build_portfolio_monitoring
from credit_memo import load_credit_memo_commentary, build_credit_memo
# validate_* functions (validation.py / validation_extended.py) are
# intentionally NOT imported here anymore - "Validation vs reference
# workbook" is no longer part of the user-facing UI (see the current
# requirements). Both modules are untouched and still fully usable
# internally: run_validation.py and test_dynamic_data.py import them
# directly for development/regression testing.
from ui_theme import (
    inject_theme,
    render_topbar,
    render_sidebar_brand,
    render_sidebar_status,
    render_page_header,
    render_section,
    chart_caption,
    kpi_card_html,
    render_kpi_row,
    render_html_table,
    render_grade_card,
    render_grade_scale_cards,
    render_health_summary,
    render_flow_steps,
    badge_html,
    status_kind,
    progress_bar_html,
    fy_comparison_bar,
    magnitude_bar,
    capital_structure_bar,
    radar_chart,
    score_meter,
    RISK_BAND_STATUS,
    STATUS,
    INK_MUTED,
)

PLOTLY_CONFIG = {"displayModeBar": False}

AREA2_REFERENCE_SOURCE = "Reference workbook, Area 2 (2-Financial Analysis!C23:D24) — not derived from Input Data"


def load_area2_reference_metrics(workbook_path):
    """Debtors Days and Inventory Days cannot be derived from '1-Input Data'
    (no Trade Receivables / Inventory / Cost of Sales fields exist there -
    see financial_analysis.py's own notes on these two rows). For exact
    reference-workbook parity, their figures are read directly from
    '2-Financial Analysis'!C23:D24 (plus the matching %change/threshold/
    status cells) instead of being marked unavailable.

    This is a read-only, isolated lookup against the reference workbook -
    it does not call, wrap, or alter reference_data.py / input_data_engine.py
    / financial_analysis.py / risk_scoring.py / validation.py in any way.
    Nothing here is calculated; every value is read verbatim from the cell
    the reference workbook itself already computed it in.
    """
    ws = load_workbook(workbook_path, data_only=True)["2-Financial Analysis"]
    return {
        "debtors_days": {
            "current": ws["C23"].value, "previous": ws["D23"].value,
            "pct_change": ws["E23"].value, "threshold": ws["G23"].value,
            "status": ws["H23"].value, "source": AREA2_REFERENCE_SOURCE,
        },
        "inventory_days": {
            "current": ws["C24"].value, "previous": ws["D24"].value,
            "pct_change": ws["E24"].value, "threshold": ws["G24"].value,
            "status": ws["H24"].value, "source": AREA2_REFERENCE_SOURCE,
        },
    }


UPLOAD_TEMP_DIR = os.path.join(tempfile.gettempdir(), "credit_review_tool_uploads")


def _save_uploaded_workbook(uploaded_file):
    os.makedirs(UPLOAD_TEMP_DIR, exist_ok=True)
    fd, path = tempfile.mkstemp(suffix=".xlsx", dir=UPLOAD_TEMP_DIR)
    with os.fdopen(fd, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return path


def _cleanup_uploaded_workbook():
    old_path = st.session_state.get("uploaded_workbook_path")
    if old_path and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass


def _read_file_bytes(path):
    """Plain, uncached read. st.cache_data was tried here but its
    pickle-based cache write raised MemoryError on the ~24MB source PDF
    in this environment - reading straight from local disk each time is
    a few tens of milliseconds and avoids that failure mode entirely."""
    with open(path, "rb") as f:
        return f.read()


def _advanced_mode_enabled():
    """The dynamic Excel-upload/reset controls (below) are not deleted -
    the dynamic pipeline still needs to be reachable for testing and for
    a future OCR/PDF integration to swap in a generated workbook without
    touching Modules 2-10 - but they are hidden from the default
    consultant-facing demo view. Enabled by either mechanism, neither of
    which appears anywhere in the UI:
      - env var CREDIT_REVIEW_TOOL_ADVANCED_MODE=1, for scripted/automated
        runs (e.g. test_dynamic_data.py, AppTest) that need the controls
        present without a person clicking anything; or
      - a '?advanced=1' URL query parameter, for a developer to toggle the
        controls on a running app without restarting it.
    """
    if os.environ.get("CREDIT_REVIEW_TOOL_ADVANCED_MODE", "").strip().lower() in ("1", "true", "yes"):
        return True
    return st.query_params.get("advanced", "").strip().lower() in ("1", "true", "yes")


# ============================================================
# STREAMLIT PAGE
# ============================================================

st.set_page_config(
    page_title="Credit Review Tool",
    page_icon="📄",
    layout="wide",
)

inject_theme()


# ============================================================
# NAVIGATION
# ============================================================

AREAS = [
    "1. Data Upload",
    "2. Input & Validation",
    "3. Financial Analysis",
    "4. Risk Scoring",
    "5. Covenants",
    "6. Collateral Assessment",
    "7. Limit Setting",
    "8. Continuous Monitoring",
    "9. Portfolio Monitoring",
    "10. Credit Memo",
]
render_sidebar_brand("CR", "Credit Review Tool", "Reference dataset workflow")


# ============================================================
# DATA SOURCE - resolved from session_state only. The actual
# upload control (and the reset button) now lives on the Data
# Upload page (AREAS[0]), not the sidebar - it still updates the
# same session_state keys read here, calling st.rerun() after a
# successful change so this resolution (which every page,
# including the sidebar status card below, depends on) picks up
# the new source immediately rather than one interaction later.
# ============================================================

for _key, _default in [
    ("source_mode", "demo"),
    ("uploaded_workbook_path", None),
    ("uploaded_workbook_name", None),
    ("uploaded_workbook_warnings", []),
    ("_last_upload_id", None),
    ("uploader_key_version", 0),
]:
    if _key not in st.session_state:
        st.session_state[_key] = _default

_using_upload = st.session_state["source_mode"] == "uploaded" and st.session_state["uploaded_workbook_path"]
source_label = f"Uploaded: {st.session_state['uploaded_workbook_name']}" if _using_upload else "Demo Extracted Sheet"


# ============================================================
# LOAD DATA FROM THE ACTIVE SOURCE (+ the two validated
# calculation results, computed once here so the sidebar status
# card and every page below reuse the same numbers instead of
# recomputing them)
# ============================================================
#
# Demo mode: '1-Input Data' comes from Demo Extracted Sheet.xlsx (the
# current source of truth - see demo_source.py), merged into a copy of
# the policy workbook (Credit Review Automation Example.xlsx) for the
# '2-Financial Analysis' through '9 - Credit Memo' cells that only that
# workbook carries. Uploaded mode is unchanged from the existing
# dynamic-upload feature: the user's own complete workbook, used as-is.

if _using_upload:
    active_workbook_path = st.session_state["uploaded_workbook_path"]
else:
    try:
        active_workbook_path = build_demo_workbook()
    except DemoSourceError as exc:
        st.error("Could not build the current demo dataset.")
        st.code(str(exc))
        st.stop()

try:
    bundle = load_reference_bundle(path=active_workbook_path)
except ReferenceDataError as exc:
    st.error(f"Could not load '{source_label}'.")
    st.code(str(exc))
    if _using_upload:
        st.info("Falling back to the demo dataset - use the sidebar to try another upload.")
        _cleanup_uploaded_workbook()
        for _key, _default in [
            ("source_mode", "demo"), ("uploaded_workbook_path", None),
            ("uploaded_workbook_name", None), ("uploaded_workbook_warnings", []),
            ("_last_upload_id", None),
        ]:
            st.session_state[_key] = _default
        st.rerun()
    st.stop()

d = bundle.input_data
fa = build_financial_analysis(bundle)
risk = build_risk_scoring(bundle)

crumb = (
    f"{d.company_name} · {d.statement_scope} · FY{d.fy_current_year} · {d.currency_unit.title()} "
    f"· Source: {source_label}"
)

render_sidebar_status(
    d.company_name,
    f"{d.statement_scope} · FY{d.fy_current_year}",
    fa.overall_status,
    "good" if fa.overall_status == "Approve" else "critical",
    f"{risk.total_weighted_score:.2f} / 5.00",
)

selected_area = st.sidebar.radio("Go to", AREAS, label_visibility="collapsed")

st.sidebar.markdown("---")
_sidebar_source_verb = "Manage" if _advanced_mode_enabled() else "View"
st.sidebar.caption(
    f"Active dataset: **{source_label}**. {_sidebar_source_verb} the data source on the Data Upload page."
)
st.sidebar.caption(
    "9 credit-analysis modules are implemented, each validated against the reference workbook. "
    "PDF/OCR extraction is not used by this workflow - that pipeline is preserved as-is in "
    "`ocr_pipeline.py` for future re-integration; nothing on this page calls it."
)

render_topbar()


# ============================================================
# DISPLAY FORMATTING HELPERS
# ============================================================

_MONEY_KEYS = {"revenue", "net_profit", "total_assets", "net_worth", "operating_cash_flow", "total_debt"}
_RATIO_KEYS = {"ocf_to_debt", "debt_equity", "dscr", "iscr", "current_ratio", "debt_assets"}
_PCT_LEVEL_KEYS = {"ebitda_margin_growth", "ebitda_margin_pct", "net_profit_margin_pct"}
_DAYS_KEYS = {"debtors_days", "inventory_days"}
_GROWTH_THRESHOLD_KEYS = {"revenue", "ebitda_margin_growth", "net_profit", "total_assets", "net_worth", "operating_cash_flow"}

FY_CURRENT_SHORT = f"FY{d.fy_current_year}"
FY_PREVIOUS_SHORT = f"FY{d.fy_previous_year}"


def fmt_money(value):
    return "—" if value is None else f"{value:,.2f}"


def fmt_money_cr(value):
    return "—" if value is None else f"₹{value:,.0f} Cr"


def fmt_value(key, value):
    if value is None:
        return "—"
    if key in _MONEY_KEYS:
        return f"{value:,.2f}"
    if key in _RATIO_KEYS:
        return f"{value:,.2f}x"
    if key in _PCT_LEVEL_KEYS:
        return f"{value:,.2f}%"
    if key in _DAYS_KEYS:
        return f"{value:,.0f} days"
    return f"{value:,.2f}"


def fmt_pct_change(value):
    return "—" if value is None else f"{value * 100:+.2f}%"


def fmt_threshold(key, value):
    if value is None:
        return "—"
    if key in _GROWTH_THRESHOLD_KEYS:
        return f"≥ {value * 100:.0f}%"
    if key in ("ocf_to_debt", "total_debt"):
        return "see note"
    if key in _DAYS_KEYS:
        return f"< {value:.0f} days"
    if key in _PCT_LEVEL_KEYS:
        return f"> {value:.0f}%"
    if key == "debt_assets":
        return f"< {value:.2f}x"
    return f"≥ {value:.2f}x"


def fmt_status_text(status):
    return "—" if status is None else status


def delta_text_kind(current, previous, higher_is_good=True, as_points=False, unit=""):
    """Presentation-only YoY delta for a KPI card: a plain (a-b)/b or (a-b),
    formatted and colour-classified. Not a financial calculation - the
    values themselves come from financial_analysis.py / input_data_engine.py
    unchanged; this only decides the arrow colour and phrasing."""
    if current is None or previous in (None, 0):
        return None, "flat"
    if as_points:
        change = current - previous
        arrow = "▲" if change > 0 else ("▼" if change < 0 else "▬")
        text = f"{arrow} {'+' if change >= 0 else ''}{change:.2f}{unit} pts YoY"
    else:
        change = (current - previous) / previous
        arrow = "▲" if change > 0 else ("▼" if change < 0 else "▬")
        text = f"{arrow} {'+' if change >= 0 else ''}{change * 100:.2f}% YoY"
    is_good = (change >= 0) if higher_is_good else (change <= 0)
    return text, ("good" if is_good else "bad")


def metric_rows_html_table(rows, current_label, previous_label, overrides=None):
    overrides = overrides or {}
    table_rows = []
    for r in rows:
        o = overrides.get(r.key)
        current = o["current"] if o else r.current
        previous = o["previous"] if o else r.previous
        pct_change = o["pct_change"] if o else r.pct_change
        threshold = o["threshold"] if o else r.threshold
        status = o["status"] if o else r.status
        source = o["source"] if o else r.source
        table_rows.append({
            "Metric": r.label,
            current_label: fmt_value(r.key, current),
            previous_label: fmt_value(r.key, previous),
            "% Change": fmt_pct_change(pct_change),
            "Threshold": fmt_threshold(r.key, threshold),
            "Status": fmt_status_text(status),
            "Source": source,
        })
    render_html_table(
        table_rows,
        columns=["Metric", current_label, previous_label, "% Change", "Threshold", "Status", "Source"],
        badge_col="Status",
        numeric_cols=[current_label, previous_label, "% Change", "Threshold"],
    )


def year_figure_html_table(pairs, current_label, previous_label):
    table_rows = [
        {"Line item": label, current_label: fmt_money(yf.current), previous_label: fmt_money(yf.previous)}
        for label, yf in pairs
    ]
    render_html_table(
        table_rows,
        columns=["Line item", current_label, previous_label],
        numeric_cols=[current_label, previous_label],
    )


# ============================================================
# 1. DATA UPLOAD
# ============================================================
#
# Current stage: shows the source PDF and the already-extracted data as
# preloaded for this credit review - real filename/type/size only, no
# invented metadata (no upload date, user name, OCR accuracy, etc). There
# is no live PDF uploader and no OCR/LLMWhisperer call anywhere on this
# page (ocr_pipeline.py is not imported by app.py at all). The actual
# current data source is Demo Extracted Sheet.xlsx, loaded via
# demo_source.build_demo_workbook() above, unrelated to this page's
# rendering - this page is a status/download view onto files already on
# disk, not part of the calculation pipeline itself.
#
# The one working upload control on this page (the "Advanced" expander
# near the bottom) is the existing dynamic-upload feature, relocated here
# from the sidebar per the current requirements - its logic is otherwise
# byte-for-byte the same as before, including the st.rerun() calls it
# needs so the state resolved at the top of this script (which every page
# depends on) picks up a change immediately.

if selected_area == AREAS[0]:

    render_page_header(
        "Data Upload", crumb,
        "Source document and extracted data status for the current credit review.",
    )

    pdf_filename = "TATA STEEL FS_31March2026 (1).pdf"
    pdf_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), pdf_filename)

    # ---- 1. Data Source (source document) ---------------------------
    render_section("Data Source", "Financial Statement")
    if os.path.exists(pdf_path):
        pdf_size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
        doc_col, dl_col = st.columns([4, 1])
        with doc_col:
            st.markdown(
                '<div class="cr-card">'
                '<div style="display:flex;align-items:center;gap:0.9rem;">'
                '<div class="cr-doc-icon">PDF</div>'
                '<div>'
                f'<div class="cr-kpi-value" style="font-size:1rem;">{pdf_filename}</div>'
                f'<div class="cr-kpi-sub">PDF · {pdf_size_mb:.1f} MB · {d.company_name} · FY{d.fy_current_year}</div>'
                f'<div style="margin-top:0.45rem;">{badge_html("✓ Loaded", "good")}</div>'
                "</div>"
                "</div>"
                "</div>",
                unsafe_allow_html=True,
            )
        with dl_col:
            st.markdown("<div style='margin-top:0.9rem;'></div>", unsafe_allow_html=True)
            st.download_button(
                "Download PDF",
                data=_read_file_bytes(pdf_path),
                file_name=pdf_filename,
                mime="application/pdf",
                width="stretch",
            )
        st.caption(
            "This document is preloaded automatically for the current credit review - no "
            "manual upload is required."
        )
    else:
        st.warning(f"Expected source document '{pdf_filename}' was not found in the application folder.")

    # ---- 2. Extraction status flow -----------------------------------
    render_section("Extraction Status")
    render_flow_steps([
        ("Source Document", pdf_filename, "good"),
        ("Extraction", "Pre-extracted for demo", "neutral"),
        ("Extracted Financial Data", EXTRACTED_SHEET_FILE, "good"),
    ])
    st.caption(
        "OCR is not run live in this build - the steps above represent the intended pipeline. "
        "For the current demo, the extraction step is a pre-extracted, already-validated "
        "dataset rather than a live OCR result."
    )

    # ---- 3. Extracted Financial Data ----------------------------------
    render_section("Extracted Financial Data")
    if os.path.exists(EXTRACTED_SHEET_PATH):
        extracted_size_kb = os.path.getsize(EXTRACTED_SHEET_PATH) / 1024
        doc_col, dl_col = st.columns([4, 1])
        with doc_col:
            st.markdown(
                '<div class="cr-card">'
                '<div style="display:flex;align-items:center;gap:0.9rem;">'
                '<div class="cr-doc-icon">XLS</div>'
                '<div>'
                f'<div class="cr-kpi-value" style="font-size:1rem;">{EXTRACTED_SHEET_FILE}</div>'
                f'<div class="cr-kpi-sub">Excel · {extracted_size_kb:.0f} KB</div>'
                f'<div style="margin-top:0.45rem;">{badge_html("✓ Ready", "good")}</div>'
                "</div>"
                "</div>"
                "</div>",
                unsafe_allow_html=True,
            )
        with dl_col:
            st.markdown("<div style='margin-top:0.9rem;'></div>", unsafe_allow_html=True)
            st.download_button(
                "Download Excel",
                data=_read_file_bytes(EXTRACTED_SHEET_PATH),
                file_name=EXTRACTED_SHEET_FILE,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
            )
        st.caption("Pre-extracted financial data corresponding to the current demo source document.")
    else:
        st.warning(f"Expected extracted data source '{EXTRACTED_SHEET_FILE}' was not found in the application folder.")

    # ---- 4. Active Data Source ------------------------------------------
    render_section("Active Data Source")
    _active_dataset_label = st.session_state["uploaded_workbook_name"] if _using_upload else EXTRACTED_SHEET_FILE
    st.markdown(
        '<div class="cr-card">'
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;">'
        f'<div><div class="cr-kpi-label">Borrower</div><div class="cr-kpi-value" style="font-size:1rem;">{d.company_name}</div></div>'
        f'<div><div class="cr-kpi-label">Dataset</div><div class="cr-kpi-value" style="font-size:1rem;">{_active_dataset_label}</div></div>'
        f'<div><div class="cr-kpi-label">Source</div><div class="cr-kpi-value" style="font-size:1rem;">Tata Steel financial statement</div></div>'
        f'<div><div class="cr-kpi-label">Status</div><div style="margin-top:0.25rem;">{badge_html("● Active", "good")}</div></div>'
        "</div>"
        "</div>",
        unsafe_allow_html=True,
    )
    if _advanced_mode_enabled():
        st.caption(
            "All downstream modules (Input & Validation through Credit Memo) read from this "
            "dataset - see 'Advanced' below for how to point them at a different workbook."
        )
    else:
        st.caption("All downstream modules (Input & Validation through Credit Memo) read from this dataset.")

    # ---- 5. Future Upload (PDF - not yet enabled) ------------------------
    render_section("Future Upload")
    st.markdown(
        '<div class="cr-card" style="text-align:center;padding:1.9rem 1.5rem;'
        'border-style:dashed;opacity:0.8;">'
        '<div style="font-weight:700;color:#52514e;">⬆ Upload Financial Statement</div>'
        '<div style="font-size:0.85rem;color:#898781;margin-top:0.4rem;">'
        "PDF upload will be enabled in a future release."
        "</div>"
        "</div>",
        unsafe_allow_html=True,
    )

    # ---- 6. Advanced: the existing dynamic-upload feature ------------------
    # Not deleted - only hidden from the default consultant-facing view.
    # See _advanced_mode_enabled() above for the two (non-UI) ways to
    # reveal it. Everything inside this branch is unchanged from before.
    if _advanced_mode_enabled():
        with st.expander("Advanced: load a different Credit Review workbook (for testing)"):
            st.caption(
                "This uploads a complete replacement workbook (all sheets, not just the PDF's "
                "extracted data) and switches every module's data source to it - separate from "
                "the PDF placeholder above, which is not yet functional. The demo source "
                f"({EXTRACTED_SHEET_FILE}) is unaffected unless you use this control."
            )

            # Versioned key (workbook_uploader_0, _1, ...) rather than fixed:
            # a plain st.file_uploader keeps showing the last-selected file
            # across reruns even after other session_state is reset -
            # clicking "Reset" would otherwise leave the widget still holding
            # the old upload, which then gets silently reprocessed as a "new"
            # upload on the very next rerun. Bumping uploader_key_version
            # gives the widget a fresh identity, which is Streamlit's actual
            # mechanism for clearing it.
            _uploaded_file = st.file_uploader(
                "Upload a Credit Review workbook (.xlsx)",
                type=["xlsx"],
                key=f"workbook_uploader_{st.session_state['uploader_key_version']}",
            )

            if _uploaded_file is not None:
                _upload_id = getattr(_uploaded_file, "file_id", None) or f"{_uploaded_file.name}:{_uploaded_file.size}"
                if _upload_id != st.session_state["_last_upload_id"]:
                    st.session_state["_last_upload_id"] = _upload_id
                    _temp_path = _save_uploaded_workbook(_uploaded_file)
                    _report = validate_workbook(_temp_path)
                    if not _report.is_valid:
                        st.error(
                            "Upload rejected - this workbook is missing data the app needs:\n\n"
                            + "\n".join(f"- {e}" for e in _report.errors)
                        )
                        os.remove(_temp_path)
                    else:
                        _cleanup_uploaded_workbook()
                        st.session_state["source_mode"] = "uploaded"
                        st.session_state["uploaded_workbook_path"] = _temp_path
                        st.session_state["uploaded_workbook_name"] = _uploaded_file.name
                        st.session_state["uploaded_workbook_warnings"] = _report.warnings
                        st.rerun()

            if _using_upload:
                st.success(f"Currently analyzing uploaded workbook: **{st.session_state['uploaded_workbook_name']}**")
                if st.session_state["uploaded_workbook_warnings"]:
                    st.warning(
                        "Some modules are unavailable for this workbook:\n\n"
                        + "\n".join(f"- {w}" for w in st.session_state["uploaded_workbook_warnings"])
                    )
                if st.button(f"↺ Reset to demo dataset ({EXTRACTED_SHEET_FILE})"):
                    _cleanup_uploaded_workbook()
                    for _key, _default in [
                        ("source_mode", "demo"), ("uploaded_workbook_path", None),
                        ("uploaded_workbook_name", None), ("uploaded_workbook_warnings", []),
                        ("_last_upload_id", None),
                    ]:
                        st.session_state[_key] = _default
                    st.session_state["uploader_key_version"] += 1
                    st.rerun()
            else:
                st.caption(f"Currently analyzing the demo dataset (**{EXTRACTED_SHEET_FILE}**).")


# ============================================================
# 2. INPUT & VALIDATION
# ============================================================

elif selected_area == AREAS[1]:

    render_page_header(
        "Input & Validation", crumb,
        "Reference financial data, loaded automatically and cross-checked against the "
        "reference workbook's own validation formulas.",
    )

    m = compute_derived_metrics(d)
    c_ratios = compute_calculated_ratios(d, m)
    checks = run_validation_checks(d, m)
    checks_by_cell = {c.cell: c for c in checks}

    total_checks = len(checks)
    passed_checks = sum(1 for c in checks if c.result in ("PASS", "PASS - ties"))
    tie_cells = ("B69", "B70", "B71", "B72", "B73")
    ties_passed = sum(1 for cell in tie_cells if checks_by_cell[cell].result == "PASS - ties")

    st.markdown(
        badge_html("Reference Financial Data Loaded", "good")
        + f'&nbsp;&nbsp;<span style="color:{INK_MUTED};font-size:0.85rem;">Source: {bundle.workbook_path}</span>',
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    render_kpi_row([
        kpi_card_html(
            "Data Completeness", "100%",
            sublabel="All required fields loaded from the reference workbook",
            icon="✓", icon_kind="good",
        ),
        kpi_card_html(
            "Validation Checks", f"{passed_checks} / {total_checks}",
            sublabel="1-Input Data automated checks",
            icon="✓" if passed_checks == total_checks else "!",
            icon_kind="good" if passed_checks == total_checks else "critical",
        ),
        kpi_card_html(
            "Reconciliation Ties", f"{ties_passed} / {len(tie_cells)}",
            sublabel="Calculated vs reported ratios",
            icon="✓" if ties_passed == len(tie_cells) else "!",
            icon_kind="good" if ties_passed == len(tie_cells) else "critical",
        ),
        kpi_card_html(
            "Reporting Period", f"{FY_CURRENT_SHORT} vs {FY_PREVIOUS_SHORT}",
            sublabel=f"{d.statement_scope} · {d.currency_unit.title()}",
        ),
    ], columns_per_row=4)

    tabs = st.tabs(["Overview", "P&L Statement", "Balance Sheet", "Cash Flow", "Reported Ratios", "Validation Checks"])

    with tabs[0]:
        st.markdown("##### Borrower")
        render_kpi_row([
            kpi_card_html("Company", d.company_name),
            kpi_card_html("Scope", d.statement_scope),
            kpi_card_html("Currency", d.currency_unit.title()),
        ], columns_per_row=3)
        st.markdown("<br>", unsafe_allow_html=True)
        st.caption(
            "Only the fields present in '1-Input Data' are shown throughout this app - no "
            "borrower profile, facility, or covenant fields beyond that sheet are fabricated."
        )

    with tabs[1]:
        year_figure_html_table([
            ("Revenue from operations", d.revenue),
            ("Other income", d.other_income),
            ("Total income (calculated: Revenue + Other income)", m.total_income),
            ("Total expenses", d.total_expenses),
            ("  of which: Finance costs", d.finance_costs),
            ("  of which: Depreciation & amortisation", d.depreciation),
            ("Interest income", d.interest_income),
            ("Profit before exceptional items (calculated)", m.pbt_before_exceptional),
            ("Exceptional items", d.exceptional_items),
            ("Profit before tax (calculated)", m.pbt),
            ("Total tax expense", d.total_tax_expense),
            ("Net profit (calculated)", m.net_profit),
        ], d.fy_current_label, d.fy_previous_label)

    with tabs[2]:
        year_figure_html_table([
            ("Property, plant & equipment (net)", d.ppe),
            ("Total assets", d.total_assets),
            ("Total equity", d.total_equity),
            ("Borrowings - non-current", d.borrowings_non_current),
            ("Borrowings - current", d.borrowings_current),
            ("Lease liabilities - non-current", d.lease_liabilities_non_current),
            ("Lease liabilities - current", d.lease_liabilities_current),
            ("Total debt (calculated: borrowings + lease liabilities)", m.total_debt),
            ("Current investments", d.current_investments),
            ("Cash & cash equivalents", d.cash),
            ("Other balances with banks", d.other_bank_balances),
            ("Net debt (calculated)", m.net_debt),
            ("Current assets", d.current_assets),
            ("Current liabilities", d.current_liabilities),
            ("Total liabilities", d.total_liabilities),
        ], d.fy_current_label, d.fy_previous_label)

    with tabs[3]:
        year_figure_html_table([
            ("Net cash from operating activities", d.operating_cash_flow),
            ("Income taxes paid (net of refund)", d.income_taxes_paid),
            ("Interest paid", d.interest_paid),
            ("Repayment of long-term borrowings", d.repayment_borrowings),
            ("Principal payment of lease liabilities", d.lease_payment),
            ("Purchase of capital assets (capex)", d.capex),
            ("Dividend paid", d.dividend_paid),
            ("Total Comprehensive Income", d.total_comprehensive_income),
        ], d.fy_current_label, d.fy_previous_label)

    with tabs[4]:
        year_figure_html_table([
            ("DSCR", d.reported_dscr),
            ("ISCR", d.reported_iscr),
            ("Current ratio", d.reported_current_ratio),
            ("Debt-equity ratio (net debt / average equity)", d.reported_debt_equity),
            ("Total debts to total assets", d.reported_debt_assets),
            ("Operating EBITDA margin", d.reported_ebitda_margin),
            ("Net profit margin", d.reported_net_profit_margin),
            ("Net worth", d.reported_net_worth),
        ], d.fy_current_label, d.fy_previous_label)

    with tabs[5]:
        rows = [{"Cell": c.cell, "Check": c.label, "Result": c.result} for c in checks]
        render_html_table(rows, columns=["Cell", "Check", "Result"], badge_col="Result")

    render_section(
        "Reported vs Calculated — reconciliation",
        "Where '1-Input Data' defines both a disclosed (Reported) figure and an independently "
        "derived (Calculated) formula for the same ratio, both are shown here. Four of these "
        "eight have no reconciliation check in the reference workbook itself and are labelled "
        "accordingly rather than judged - see the Financial Analysis notes for the DSCR/ISCR/"
        "Current Ratio/Net Worth gap this surfaces.",
    )
    recon_rows = []
    recon_defs = [
        ("DSCR", d.reported_dscr.current, c_ratios.dscr.current, None),
        ("ISCR", d.reported_iscr.current, c_ratios.iscr.current, None),
        ("Current Ratio", d.reported_current_ratio.current, c_ratios.current_ratio.current, None),
        ("Debt-Equity", d.reported_debt_equity.current, c_ratios.debt_equity.current, "B73"),
        ("Debt/Assets", d.reported_debt_assets.current, c_ratios.debt_assets.current, "B72"),
        ("EBITDA Margin", d.reported_ebitda_margin.current * 100, c_ratios.ebitda_margin.current * 100, "B69"),
        ("Net Profit Margin", d.reported_net_profit_margin.current * 100, c_ratios.net_profit_margin.current * 100, "B71"),
        ("Net Worth", d.reported_net_worth.current, c_ratios.net_worth.current, None),
    ]
    for label, reported, calculated, check_cell in recon_defs:
        if check_cell:
            result_text = checks_by_cell[check_cell].result
        else:
            result_text = "No reference check"
        recon_rows.append({
            "Metric": label,
            "Reported": f"{reported:,.4f}",
            "Calculated": f"{calculated:,.4f}",
            "Difference": f"{reported - calculated:,.4f}",
            "Reference check": result_text,
        })
    render_html_table(
        recon_rows,
        columns=["Metric", "Reported", "Calculated", "Difference", "Reference check"],
        badge_col="Reference check",
        numeric_cols=["Reported", "Calculated", "Difference"],
    )



# ============================================================
# 3. FINANCIAL ANALYSIS
# ============================================================

elif selected_area == AREAS[2]:

    render_page_header(
        "Financial Analysis", crumb,
        "Metrics derived from Input Data - either directly or through the reference "
        "workbook's own formulas - benchmarked against policy thresholds.",
    )

    m = fa.derived
    rows_by_key = {r.key: r for r in fa.income_statement_rows + fa.ratio_rows}
    area2_overrides = load_area2_reference_metrics(bundle.workbook_path)

    overall_kind = "good" if fa.overall_status == "Approve" else "critical"
    st.markdown(
        badge_html(fa.overall_status, overall_kind)
        + f'&nbsp;&nbsp;<span style="color:{INK_MUTED};font-size:0.85rem;">{fa.overall_status_note}</span>',
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    # ---- 1. Financial Overview ---------------------------------
    render_section("Financial Overview", f"{d.fy_current_label} vs {d.fy_previous_label}")

    r = rows_by_key["revenue"]
    delta, kind = delta_text_kind(r.current, r.previous)
    revenue_card = kpi_card_html("Revenue", fmt_money_cr(r.current), delta, kind, d.fy_current_label)

    delta, kind = delta_text_kind(m.ebitda.current, m.ebitda.previous)
    ebitda_card = kpi_card_html("EBITDA", fmt_money_cr(m.ebitda.current), delta, kind, "PBT + Net Finance Charges + Depreciation")

    r = rows_by_key["ebitda_margin_growth"]
    delta, kind = delta_text_kind(r.current, r.previous, as_points=True)
    ebitda_margin_card = kpi_card_html("EBITDA Margin", f"{r.current:.2f}%", delta, kind, "EBITDA / Revenue")

    r = rows_by_key["net_profit"]
    delta, kind = delta_text_kind(r.current, r.previous)
    net_profit_card = kpi_card_html("Net Profit", fmt_money_cr(r.current), delta, kind, "PBT − Tax")

    r = rows_by_key["net_profit_margin_pct"]
    delta, kind = delta_text_kind(r.current, r.previous, as_points=True)
    net_profit_margin_card = kpi_card_html("Net Profit Margin", f"{r.current:.2f}%", delta, kind, "Net Profit / Revenue")

    r = rows_by_key["total_assets"]
    delta, kind = delta_text_kind(r.current, r.previous)
    total_assets_card = kpi_card_html("Total Assets", fmt_money_cr(r.current), delta, kind)

    r = rows_by_key["net_worth"]
    delta, kind = delta_text_kind(r.current, r.previous)
    net_worth_card = kpi_card_html("Net Worth", fmt_money_cr(r.current), delta, kind, "Reported basis")

    r = rows_by_key["total_debt"]
    delta, kind = delta_text_kind(r.current, r.previous, higher_is_good=False)
    total_debt_card = kpi_card_html("Total Debt", fmt_money_cr(r.current), delta, kind)

    r = rows_by_key["operating_cash_flow"]
    delta, kind = delta_text_kind(r.current, r.previous)
    ocf_card = kpi_card_html("Operating Cash Flow", fmt_money_cr(r.current), delta, kind)

    render_kpi_row([
        revenue_card, ebitda_card, ebitda_margin_card,
        net_profit_card, net_profit_margin_card, total_assets_card,
        net_worth_card, total_debt_card, ocf_card,
    ], columns_per_row=3)

    # ---- 2. Revenue & Profitability -----------------------------
    render_section(
        "Revenue & Profitability",
        f"{d.fy_previous_label} vs {d.fy_current_label} — only two fiscal years are available "
        f"in the reference dataset, so comparisons are shown as bars rather than a multi-year trend.",
    )
    col1, col2 = st.columns(2)
    with col1:
        chart_caption("Revenue, EBITDA, Net Profit (₹ crore)")
        r_rev, r_np = rows_by_key["revenue"], rows_by_key["net_profit"]
        fig = fy_comparison_bar(
            ["Revenue", "EBITDA", "Net Profit"],
            [r_rev.current, m.ebitda.current, r_np.current],
            [r_rev.previous, m.ebitda.previous, r_np.previous],
            FY_CURRENT_SHORT, FY_PREVIOUS_SHORT, value_fmt="₹{:,.0f}", y_tickformat=".2~s",
        )
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)
    with col2:
        chart_caption("EBITDA & Net Profit margin")
        r_em, r_npm = rows_by_key["ebitda_margin_growth"], rows_by_key["net_profit_margin_pct"]
        fig = fy_comparison_bar(
            ["EBITDA Margin %", "Net Profit Margin %"],
            [r_em.current, r_npm.current], [r_em.previous, r_npm.previous],
            FY_CURRENT_SHORT, FY_PREVIOUS_SHORT, value_fmt="{:.2f}%",
        )
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)

    # ---- 3. Balance Sheet / Capital Structure --------------------
    render_section("Balance Sheet / Capital Structure", f"{d.fy_previous_label} vs {d.fy_current_label}")
    col1, col2 = st.columns([2, 1])
    with col1:
        chart_caption("Total Assets, Net Worth, Total Debt, Net Debt (₹ crore)")
        r_ta, r_nw, r_td = rows_by_key["total_assets"], rows_by_key["net_worth"], rows_by_key["total_debt"]
        fig = fy_comparison_bar(
            ["Total Assets", "Net Worth", "Total Debt", "Net Debt"],
            [r_ta.current, r_nw.current, r_td.current, m.net_debt.current],
            [r_ta.previous, r_nw.previous, r_td.previous, m.net_debt.previous],
            FY_CURRENT_SHORT, FY_PREVIOUS_SHORT, value_fmt="₹{:,.0f}", height=380, y_tickformat=".2~s",
        )
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)
    with col2:
        chart_caption(f"Capital structure — {FY_CURRENT_SHORT}")
        fig = capital_structure_bar(m.total_debt.current, d.total_equity.current)
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)
        st.caption(
            "Total Debt vs Total Equity (balance-sheet basis). Differs from the "
            "disclosure-basis Net Worth card above - see the Input & Validation reconciliation."
        )

    # ---- 4. Liquidity & Leverage ----------------------------------
    render_section("Liquidity & Leverage", "Reported Ratios basis (see Input & Validation for the reconciliation to calculated values)")
    col1, col2 = st.columns(2)
    with col1:
        chart_caption("Coverage ratios")
        r_dscr, r_iscr = rows_by_key["dscr"], rows_by_key["iscr"]
        fig = fy_comparison_bar(
            ["DSCR", "ISCR"], [r_dscr.current, r_iscr.current], [r_dscr.previous, r_iscr.previous],
            FY_CURRENT_SHORT, FY_PREVIOUS_SHORT, value_fmt="{:.2f}x",
        )
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)
    with col2:
        chart_caption("Leverage & liquidity ratios")
        r_cr, r_de, r_da = rows_by_key["current_ratio"], rows_by_key["debt_equity"], rows_by_key["debt_assets"]
        fig = fy_comparison_bar(
            ["Current Ratio", "Debt/Equity", "Debt/Assets"],
            [r_cr.current, r_de.current, r_da.current], [r_cr.previous, r_de.previous, r_da.previous],
            FY_CURRENT_SHORT, FY_PREVIOUS_SHORT, value_fmt="{:.2f}x",
        )
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)

    ratio_cards = []
    for key, label in [("dscr", "DSCR"), ("iscr", "ISCR"), ("current_ratio", "Current Ratio"),
                        ("debt_equity", "Debt/Equity"), ("debt_assets", "Debt/Assets")]:
        rr = rows_by_key[key]
        ratio_cards.append(kpi_card_html(
            label, f"{rr.current:.2f}x",
            sublabel=badge_html(rr.status, status_kind(rr.status)),
        ))
    render_kpi_row(ratio_cards, columns_per_row=5)

    # ---- 5. Cash Flow -----------------------------------------------
    render_section("Cash Flow", f"{d.fy_previous_label} vs {d.fy_current_label}")
    chart_caption("Cash flow line items (₹ crore)")
    fig = fy_comparison_bar(
        ["Operating Cash Flow", "Income Taxes Paid", "Interest Paid", "Repayment of Borrowings",
         "Lease Payments", "Capex", "Dividend Paid"],
        [d.operating_cash_flow.current, d.income_taxes_paid.current, d.interest_paid.current,
         d.repayment_borrowings.current, d.lease_payment.current, d.capex.current, d.dividend_paid.current],
        [d.operating_cash_flow.previous, d.income_taxes_paid.previous, d.interest_paid.previous,
         d.repayment_borrowings.previous, d.lease_payment.previous, d.capex.previous, d.dividend_paid.previous],
        FY_CURRENT_SHORT, FY_PREVIOUS_SHORT, value_fmt="₹{:,.0f}", height=380, y_tickformat=".2~s",
    )
    st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)

    # ---- 6. Financial Health Summary --------------------------------
    render_section("Financial Health Summary", "Generated from the calculated metrics above - not a separately typed narrative.")

    def _direction(current, previous, good_word="grew", bad_word="declined"):
        return good_word if current >= previous else bad_word

    r_rev = rows_by_key["revenue"]
    r_em = rows_by_key["ebitda_margin_growth"]
    r_td = rows_by_key["total_debt"]
    r_cr = rows_by_key["current_ratio"]
    r_dscr = rows_by_key["dscr"]
    r_ocf = rows_by_key["operating_cash_flow"]

    health_items = [
        (
            f"Revenue {_direction(r_rev.current, r_rev.previous)} "
            f"{abs(r_rev.pct_change) * 100:.1f}% YoY to ₹{r_rev.current:,.0f} cr in {d.fy_current_label}.",
            "good" if r_rev.current >= r_rev.previous else "warning",
        ),
        (
            f"EBITDA margin {_direction(r_em.current, r_em.previous, 'improved', 'declined')} from "
            f"{r_em.previous:.2f}% to {r_em.current:.2f}%.",
            "good" if r_em.current >= r_em.previous else "warning",
        ),
        (
            f"Total debt {_direction(r_td.current, r_td.previous, 'increased', 'decreased')} "
            f"{abs(r_td.pct_change) * 100:.1f}% YoY to ₹{r_td.current:,.0f} cr.",
            "good" if r_td.current <= r_td.previous else "warning",
        ),
        (
            f"Current ratio stands at {r_cr.current:.2f}x, "
            f"{'meeting' if r_cr.status == 'Pass' else 'below'} the {r_cr.threshold:.2f}x policy "
            f"minimum - a {'comfortable' if r_cr.current >= 1 else 'constrained'} short-term liquidity position.",
            "good" if r_cr.status == "Pass" else "critical",
        ),
        (
            f"DSCR of {r_dscr.current:.2f}x is {'above' if r_dscr.status == 'Pass' else 'below'} the "
            f"{r_dscr.threshold:.2f}x policy minimum, indicating "
            f"{'adequate' if r_dscr.status == 'Pass' else 'constrained'} debt-servicing capacity on a reported basis.",
            "good" if r_dscr.status == "Pass" else "critical",
        ),
        (
            f"Operating cash flow {_direction(r_ocf.current, r_ocf.previous)} "
            f"{abs(r_ocf.pct_change) * 100:.1f}% YoY to ₹{r_ocf.current:,.0f} cr.",
            "good" if r_ocf.current >= r_ocf.previous else "warning",
        ),
    ]
    render_health_summary(health_items)

    # ---- Detail tables & notes --------------------------------------
    render_section("Income Statement / Balance Sheet / Cash Flow — detail")
    metric_rows_html_table(fa.income_statement_rows, d.fy_current_label, d.fy_previous_label)

    render_section(
        "Key Ratios — detail",
        "Debtors Days and Inventory Days are shown here as reference-workbook values for "
        "parity (read directly from '2-Financial Analysis', Area 2) - they are not derived "
        "from Input Data by this engine and are excluded from the Overall Status gate above, "
        "same as before.",
    )
    metric_rows_html_table(fa.ratio_rows, d.fy_current_label, d.fy_previous_label, overrides=area2_overrides)

    with st.expander("Flagged discrepancies & notes"):
        for key, o in area2_overrides.items():
            label = "Debtors Days" if key == "debtors_days" else "Inventory Days"
            st.markdown(
                f"**{label}** — Not derivable from '1-Input Data' (no Trade Receivables / "
                f"Inventory / Cost of Sales fields exist there). Shown above for "
                f"reference-workbook parity, sourced from {o['source']}: {o['current']:.0f} "
                f"({d.fy_current_label}) vs {o['previous']:.0f} ({d.fy_previous_label})."
            )
        for row in fa.income_statement_rows + fa.ratio_rows:
            if row.key in area2_overrides:
                continue
            if row.note:
                st.markdown(f"**{row.label}** — {row.note}")



# ============================================================
# 4. RISK SCORING
# ============================================================

elif selected_area == AREAS[3]:

    render_page_header(
        "Risk Scoring", crumb,
        "Weighted composite risk assessment across 8 components, per the reference "
        "workbook's own methodology, weights and scoring bands.",
    )

    band_kind = RISK_BAND_STATUS[round(risk.total_weighted_score)]
    grade_by_score = {g["score"]: g for g in bundle.risk_policy.grade_scale}

    col1, col2 = st.columns([1, 2])
    with col1:
        render_grade_card(risk.risk_grade.upper(), f"{risk.total_weighted_score:.2f} / 5.00", band_kind)
    with col2:
        fig = score_meter(risk.total_weighted_score)
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)
        st.caption(
            "Band shown: 1 (Poor / High Risk) → 5 (Excellent / Very Low Risk), per the reference "
            "workbook's own score-to-grade scale - see the full scale further down this page."
        )

    # ---- Risk Factor Breakdown --------------------------------------
    render_section("Risk Factor Breakdown", "All 8 components, weight × score = weighted contribution.")
    rows = []
    for comp in risk.components:
        comp_rounded = round(comp.raw_score)
        comp_kind = RISK_BAND_STATUS.get(comp_rounded, "neutral")
        comp_grade = grade_by_score.get(comp_rounded)
        grade_cell = badge_html(comp_grade["risk_interpretation"] if comp_grade else "—", comp_kind)
        rows.append({
            "Component": comp.label,
            "Weight": f"{comp.weight_pct:.0f}%",
            "Raw Score": f"{comp.raw_score:.2f}",
            "Score Bar": progress_bar_html(comp.raw_score, color=STATUS[comp_kind]["bg"]),
            "Weighted Score": f"{comp.weighted_score:.3f}",
            "Grade": grade_cell,
            "Source": comp.source,
        })
    render_html_table(
        rows,
        columns=["Component", "Weight", "Raw Score", "Score Bar", "Weighted Score", "Grade", "Source"],
        numeric_cols=["Weight", "Raw Score", "Weighted Score"],
    )

    col1, col2 = st.columns(2)
    with col1:
        chart_caption("Raw score by component")
        fig = magnitude_bar(
            [c.label for c in risk.components], [c.raw_score for c in risk.components],
            value_fmt="{:.2f}",
        )
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)
    with col2:
        chart_caption("Weighted contribution by component")
        fig = magnitude_bar(
            [c.label for c in risk.components], [c.weighted_score for c in risk.components],
            value_fmt="{:.3f}",
        )
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)
        st.caption("Sorted by contribution - shows which factors move the final score the most.")

    render_section("Risk Radar", "Raw score (1-5) across all 8 components.")
    fig = radar_chart([c.label for c in risk.components], [c.raw_score for c in risk.components])
    st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)

    # ---- Financial Strength -----------------------------------------
    render_section(
        "Financial Strength — underlying components",
        "The one component computed from Input Data: DSCR/ISCR/Current Ratio/Debt-Equity, "
        "banded exactly per the reference workbook's scoring rules.",
    )
    fs_rows = []
    for f in risk.financial_strength_inputs:
        fs_rows.append({
            "Metric": f.label,
            "Actual Value": f"{f.value:.2f}x",
            "Band Rule": f.band_rule,
            "Score": f"{f.score}",
            "Weight": f"{f.weight:.0%}",
            "Contribution": f"{f.score * f.weight:.3f}",
        })
    render_html_table(
        fs_rows,
        columns=["Metric", "Actual Value", "Band Rule", "Score", "Weight", "Contribution"],
        numeric_cols=["Actual Value", "Score", "Weight", "Contribution"],
    )
    render_kpi_row([
        kpi_card_html("Financial Strength score", f"{risk.financial_strength_score:.2f} / 5.00",
                      sublabel="Sum of (component score × weight)"),
    ], columns_per_row=1)

    # ---- Risk Grade Scale ---------------------------------------------
    render_section("Risk Grade Scale", "From the reference workbook's own score-to-grade table - not re-derived.")
    render_grade_scale_cards(bundle.risk_policy.grade_scale, round(risk.total_weighted_score))

    # ---- Analyst / Reference Inputs -------------------------------------
    render_section(
        "Analyst / Reference Inputs",
        "These components are qualitative policy inputs read directly from the reference "
        "workbook - not computed from Input Data (see the reference workbook's own 'Sheet1' "
        "notes: qualitative factors are explicitly left to the analyst).",
    )
    qual_rows = [
        {"Component": c.label, "Score (1-5)": f"{c.raw_score:.0f}", "Weight": f"{c.weight_pct:.0f}%"}
        for c in risk.components if c.key != "financial_strength"
    ]
    render_html_table(qual_rows, columns=["Component", "Score (1-5)", "Weight"], numeric_cols=["Score (1-5)", "Weight"])



# ============================================================
# 5. COVENANTS
# ============================================================

elif selected_area == AREAS[4]:

    render_page_header(
        "Covenants", crumb,
        "Financial covenant headroom and RAG status, computed from Financial Analysis's "
        "reported DSCR/ISCR against the reference workbook's own policy minimums.",
    )

    covenant_policy = load_covenant_policy(bundle.workbook_path)
    covenants = build_covenants(fa, covenant_policy)

    overall_kind = "good" if covenants.overall_status == "PASS" else "critical"
    st.markdown(
        badge_html(covenants.overall_status, overall_kind)
        + f'&nbsp;&nbsp;<span style="color:{INK_MUTED};font-size:0.85rem;">'
          f'PASS unless any covenant is AMBER or RED.</span>',
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    render_section("Covenant Headroom", "Headroom % = (Actual − Threshold) / Threshold. RAG: > 20% Green, 5-20% Amber, < 5% Red.")

    cov_cards = []
    for row in covenants.rows:
        cov_cards.append(kpi_card_html(
            row.label.split(",")[0], f"{row.actual:.2f}x",
            sublabel=f"Min {row.threshold:.2f}x · Headroom {row.headroom * 100:+.0f}%",
            icon="✓" if row.rag == "GREEN" else ("!" if row.rag == "RED" else "•"),
            icon_kind=status_kind(row.rag),
        ))
    render_kpi_row(cov_cards, columns_per_row=2)

    table_rows = [
        {
            "Covenant": r.label,
            "Threshold": f"{r.threshold:.2f}x",
            "Actual": f"{r.actual:.2f}x",
            "Headroom": f"{r.headroom * 100:+.1f}%",
            "RAG Status": r.rag,
        }
        for r in covenants.rows
    ]
    render_html_table(
        table_rows,
        columns=["Covenant", "Threshold", "Actual", "Headroom", "RAG Status"],
        badge_col="RAG Status",
        numeric_cols=["Threshold", "Actual", "Headroom"],
    )

    st.caption(
        "Both covenants are read from Financial Analysis's reported DSCR/ISCR (Reported Ratios "
        "basis) - the reference workbook's own '4-Covenants' sheet pulls from the same cells "
        "('2-Financial Analysis'!C19/C20), not from Input Data directly. Threshold minimums "
        "(1.5x DSCR, 2.0x ISCR) are read live from the reference workbook's policy cells."
    )



# ============================================================
# 6. COLLATERAL ASSESSMENT
# ============================================================

elif selected_area == AREAS[5]:

    render_page_header(
        "Collateral Assessment", crumb,
        "Collateral coverage of the credit exposure, applying policy haircuts to each asset "
        "class's book value.",
    )

    collateral_inputs = load_collateral_inputs(bundle.workbook_path)

    if collateral_inputs is None:
        st.markdown(badge_html("Unavailable", "neutral"), unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)
        st.warning(
            "**Collateral data unavailable — analyst input required.** This workbook has no "
            "'5-Collateral Assessment' sheet, or is missing its book value / haircut cells. "
            "This is not derivable from '1-Input Data' (which has no Trade Receivables, "
            "Investments-beyond-current, or Inventory fields), so no figures are shown here - "
            "the reference workbook's (Tata Steel's) collateral values are never substituted "
            "in as a fallback."
        )
    else:
        collateral = build_collateral_assessment(collateral_inputs, fa.derived.total_debt.current)

        rating_kind = status_kind(collateral.rating)
        st.markdown(
            badge_html(collateral.rating, rating_kind)
            + f'&nbsp;&nbsp;<span style="color:{INK_MUTED};font-size:0.85rem;">'
              f'Coverage ratio {collateral.coverage_ratio:.2f}x (~{collateral.coverage_ratio * 100:.0f}%).</span>',
            unsafe_allow_html=True,
        )
        st.markdown("<br>", unsafe_allow_html=True)

        st.warning(
            "**Reference-workbook formula quirk, reproduced faithfully:** '5-Collateral "
            "Assessment'!D24 compares the Collateral Coverage Ratio (a multiple, e.g. 2.05x) "
            "directly against 125 and 100 - as if it were already a percentage number. A realistic "
            "coverage multiple is never that large, so the reference workbook's own rating always "
            "falls in the '<100' band ('High collateral risk'), even at ~205% coverage. This is an "
            "apparent unit-mismatch bug in the source workbook (confirmed against its own cached "
            "D24 value) - reproduced exactly here rather than silently corrected, per the "
            "reference-workbook parity rule. The underlying coverage ratio itself "
            f"({collateral.coverage_ratio:.2f}x) is accurate."
        )

        render_kpi_row([
            kpi_card_html("Total Adjusted Collateral Value", fmt_money_cr(collateral.total_adjusted_value),
                          sublabel="Sum of book value x (1 - haircut%)"),
            kpi_card_html("Assumed Debt Exposure", fmt_money_cr(collateral.debt_exposure),
                          sublabel="= Total Debt, from Input Data (frozen)"),
            kpi_card_html("Collateral Coverage Ratio", f"{collateral.coverage_ratio:.2f}x",
                          sublabel="Total Adjusted / Debt Exposure"),
        ], columns_per_row=3)

        render_section("Collateral Schedule", "Book values and haircuts are entity-specific reference data read directly from the active workbook - not derivable from '1-Input Data'.")
        collateral_rows = [
            {
                "Collateral Type": a.label,
                "Book Value": fmt_money_cr(a.book_value),
                "Haircut %": f"{a.haircut_pct:.0f}%",
                "Adjusted Value": fmt_money_cr(a.adjusted_value),
                "Liquidity": a.liquidity,
                "Security Rank": a.security_rank,
                "Assessment": a.assessment,
            }
            for a in collateral.assets
        ]
        render_html_table(
            collateral_rows,
            columns=["Collateral Type", "Book Value", "Haircut %", "Adjusted Value", "Liquidity", "Security Rank", "Assessment"],
            numeric_cols=["Book Value", "Haircut %", "Adjusted Value"],
        )

        chart_caption("Book value vs adjusted value by collateral type (₹ crore)")
        fig = fy_comparison_bar(
            [a.label for a in collateral.assets],
            [a.adjusted_value for a in collateral.assets],
            [a.book_value for a in collateral.assets],
            "Adjusted value", "Book value",
            value_fmt="₹{:,.0f}", height=360, y_tickformat=".2~s",
        )
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)

        with st.expander("Reference: typical haircut ranges by collateral type (industry context, not used in the calculation)"):
            typical_rows = [{"Collateral Type": t, "Typical Haircut": h} for t, h in collateral_inputs.typical_haircuts]
            render_html_table(typical_rows, columns=["Collateral Type", "Typical Haircut"])

        with st.expander("Reference: collateral coverage rating bands"):
            band_rows = [
                {"Coverage Ratio": label, "Interpretation": interp, "Credit View": view}
                for label, interp, view in collateral_inputs.rating_bands
            ]
            render_html_table(band_rows, columns=["Coverage Ratio", "Interpretation", "Credit View"])



# ============================================================
# 7. LIMIT SETTING
# ============================================================

elif selected_area == AREAS[6]:

    render_page_header(
        "Limit Setting", crumb,
        "Proposed facility impact and recommended lending limit, using EBIT and reported DSCR "
        "from Input Data directly (the reference workbook bypasses Financial Analysis and Risk "
        "Scoring for this module).",
    )

    limit_policy = load_limit_setting_policy(bundle.workbook_path)
    limit_result = build_limit_setting(limit_policy, fa.derived.ebit.current, d.reported_dscr.current)

    conclusion_text = limit_result.conclusion if isinstance(limit_result.conclusion, str) else "FALSE (uncapped case not defined by the reference workbook)"
    conclusion_kind = "good" if limit_result.conclusion == "PASS" else "warning"
    st.markdown(
        badge_html(conclusion_text, conclusion_kind)
        + f'&nbsp;&nbsp;<span style="color:{INK_MUTED};font-size:0.85rem;">'
          f'Recommended limit ₹{limit_result.recommended_limit:,.0f} cr.</span>',
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    render_section("Proposed Facility", "Deal parameters - policy/analyst inputs read from the reference workbook.")
    render_kpi_row([
        kpi_card_html("Requested Amount", fmt_money_cr(limit_policy.requested_amount)),
        kpi_card_html("Interest Rate", f"{limit_policy.interest_rate:.1%} p.a."),
        kpi_card_html("Tenor", f"{limit_policy.tenor_years} years"),
        kpi_card_html("Annual Instalment", fmt_money_cr(limit_result.annual_instalment), sublabel="Annuity: PMT(rate, tenor, amount)"),
    ], columns_per_row=4)

    render_section("Amortization Schedule")
    schedule_rows = [
        {
            "Year": y.year,
            "Opening Balance": fmt_money_cr(y.opening_balance),
            "Interest": fmt_money_cr(y.interest),
            "Principal": fmt_money_cr(y.principal),
            "Closing Balance": fmt_money_cr(y.closing_balance),
        }
        for y in limit_result.schedule
    ]
    render_html_table(
        schedule_rows,
        columns=["Year", "Opening Balance", "Interest", "Principal", "Closing Balance"],
        numeric_cols=["Year", "Opening Balance", "Interest", "Principal", "Closing Balance"],
    )
    chart_caption("Interest vs Principal by year (₹ crore)")
    fig = fy_comparison_bar(
        [f"Year {y.year}" for y in limit_result.schedule],
        [y.principal for y in limit_result.schedule],
        [y.interest for y in limit_result.schedule],
        "Principal", "Interest",
        value_fmt="₹{:,.0f}", height=320,
    )
    st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)

    render_section(
        "Pro-forma Debt Service Position",
        "Position after assuming the proposed loan has been disbursed, on top of existing debt service implied by EBIT / reported DSCR.",
    )
    render_kpi_row([
        kpi_card_html("Implied Existing Debt Service", fmt_money_cr(limit_result.implied_existing_debt_service), sublabel="EBIT / reported DSCR"),
        kpi_card_html("New Loan Instalment", fmt_money_cr(limit_result.annual_instalment)),
        kpi_card_html("Total Pro-forma Debt Service", fmt_money_cr(limit_result.total_proforma_debt_service)),
        kpi_card_html(
            "Pro-forma DSCR", f"{limit_result.proforma_dscr:.2f}x",
            sublabel=badge_html(limit_result.proforma_dscr_status, status_kind(limit_result.proforma_dscr_status)),
        ),
    ], columns_per_row=4)
    st.caption(f"EBIT: {fmt_money_cr(limit_result.ebit)} · Policy minimum DSCR: {limit_policy.policy_minimum_dscr:.2f}x")

    render_section("Debt Service Capacity & Limit Recommendation")
    render_kpi_row([
        kpi_card_html("Max Serviceable Debt Service", fmt_money_cr(limit_result.max_serviceable_debt_service), sublabel="EBIT / policy minimum DSCR"),
        kpi_card_html("Available Additional Debt Service", fmt_money_cr(limit_result.available_additional_debt_service)),
        kpi_card_html("Capacity Cap", fmt_money_cr(limit_result.capacity_cap), sublabel=f"PV annuity factor at {limit_policy.interest_rate:.0%} / {limit_policy.tenor_years}y"),
    ], columns_per_row=3)

    chart_caption("Requested amount vs capacity cap vs recommended limit (₹ crore)")
    fig = magnitude_bar(
        ["(a) Requested amount", "(b) Capacity cap", "Recommended limit = MIN(a, b)"],
        [limit_policy.requested_amount, limit_result.capacity_cap, limit_result.recommended_limit],
        value_fmt="₹{:,.0f}", sort_desc=False,
    )
    st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)



# ============================================================
# 8. CONTINUOUS MONITORING
# ============================================================

elif selected_area == AREAS[7]:

    render_page_header(
        "Continuous Monitoring", crumb,
        "Early-warning triggers on Revenue, Total Debt and DSCR movement (from Financial "
        "Analysis), plus covenant breach status (from Covenants).",
    )

    monitoring_policy = load_monitoring_policy(bundle.workbook_path)
    cm_rows_by_key = {r.key: r for r in fa.income_statement_rows + fa.ratio_rows}
    cm_covenant_policy = load_covenant_policy(bundle.workbook_path)
    cm_covenants = build_covenants(fa, cm_covenant_policy)
    cm_breach = any(r.rag in ("AMBER", "RED") for r in cm_covenants.rows)

    monitoring = build_continuous_monitoring(
        monitoring_policy,
        cm_rows_by_key["revenue"].pct_change,
        cm_rows_by_key["total_debt"].pct_change,
        cm_rows_by_key["dscr"].pct_change,
        cm_breach,
    )

    overall_kind = status_kind(monitoring.overall_status)
    st.markdown(
        badge_html(monitoring.overall_status, overall_kind)
        + f'&nbsp;&nbsp;<span style="color:{INK_MUTED};font-size:0.85rem;">Overall Status</span>',
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    st.warning(
        "**Reference-workbook formula quirk, reproduced faithfully:** '7-Continuous "
        "Monitoring'!D10 only flags Overall Status as 'NOT PASS' if a trigger literally reads "
        "the text 'NOT PASS' - but every individual trigger can only ever read 'PASS', 'FAIL' "
        "or 'WATCH LIST'. So this gate structurally can never fire, regardless of whether an "
        "individual trigger below actually fails. Reproduced exactly as the reference workbook "
        "computes it - **review the individual triggers below, not just the Overall Status "
        "badge.**"
    )

    render_section("Early Warning Triggers")
    trigger_rows = []
    for t in monitoring.triggers:
        value_text = f"{t.computed_value * 100:+.1f}%" if isinstance(t.computed_value, float) else (t.computed_value or "—")
        trigger_rows.append({
            "Trigger": t.label,
            "Threshold": t.threshold_text,
            "Computed Value": value_text,
            "Status": t.status,
        })
    render_html_table(
        trigger_rows,
        columns=["Trigger", "Threshold", "Computed Value", "Status"],
        badge_col="Status",
    )
    for t in monitoring.triggers:
        if t.note:
            st.caption(f"**{t.label}**: {t.note}")

    chart_caption("Trigger value vs threshold (percentage-based triggers)")
    fig = fy_comparison_bar(
        ["Revenue YoY", "Total Debt YoY", "DSCR YoY"],
        [cm_rows_by_key["revenue"].pct_change * 100, cm_rows_by_key["total_debt"].pct_change * 100, cm_rows_by_key["dscr"].pct_change * 100],
        [monitoring_policy.revenue_decline_threshold * 100, monitoring_policy.total_debt_increase_threshold * 100, monitoring_policy.dscr_deterioration_threshold * 100],
        "Actual", "Threshold",
        value_fmt="{:+.1f}%", height=320,
    )
    st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)



# ============================================================
# 9. PORTFOLIO MONITORING
# ============================================================

elif selected_area == AREAS[8]:

    render_page_header(
        "Portfolio Monitoring", crumb,
        "Impact of the proposed exposure on the lender's overall portfolio - concentration, "
        "asset quality and sector exposure. Self-contained: the reference workbook carries no "
        "cross-sheet formula into this module, so every input here is a lender-portfolio-level "
        "reference figure, not derived from Tata Steel's own financials.",
    )

    portfolio_inputs = load_portfolio_inputs(bundle.workbook_path)

    if portfolio_inputs is None:
        st.markdown(badge_html("Unavailable", "neutral"), unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)
        st.warning(
            "**Portfolio data unavailable — analyst input required.** This workbook has no "
            "'8-Portfolio Monitoring' sheet, or is missing its input cells. Portfolio "
            "Monitoring is lender-portfolio-level information (total book exposure, account "
            "count, NPL) with no connection to any borrower's financial statements - it is "
            "intentionally kept as a separately-sourced analyst input area, never derived from "
            "Financial Analysis, and the reference workbook's (Tata Steel's) portfolio figures "
            "are never substituted in for a different workbook."
        )
    else:
        portfolio = build_portfolio_monitoring(portfolio_inputs)

        overall_kind = "good" if portfolio.overall_status == "Approve" else "critical"
        st.markdown(
            badge_html(portfolio.overall_status, overall_kind)
            + f'&nbsp;&nbsp;<span style="color:{INK_MUTED};font-size:0.85rem;">'
              f'Approve if at least 2 of 3 risk bands are Low/Moderate.</span>',
            unsafe_allow_html=True,
        )
        st.markdown("<br>", unsafe_allow_html=True)

        render_section("Portfolio Inputs", "Lender-level reference data, read directly from the active workbook.")
        render_kpi_row([
            kpi_card_html("Portfolio Segment", portfolio_inputs.segment),
            kpi_card_html("Total Portfolio Exposure", fmt_money_cr(portfolio_inputs.total_portfolio_exposure)),
            kpi_card_html("Number of Accounts", f"{portfolio_inputs.number_of_accounts:,}"),
            kpi_card_html("Average Exposure / Account", fmt_money_cr(portfolio.average_exposure_per_account)),
        ], columns_per_row=4)
        render_kpi_row([
            kpi_card_html("Proposed Loan Amount", fmt_money_cr(portfolio_inputs.proposed_loan_amount)),
            kpi_card_html("Gross NPL", fmt_money_cr(portfolio_inputs.gross_npl)),
            kpi_card_html("Portfolio Exposure Post-Sanction", fmt_money_cr(portfolio.portfolio_exposure_post_sanction)),
        ], columns_per_row=3)

        render_section("Risk Criteria")
        criteria_rows = [
            {
                "Criteria": c.label,
                "Value": f"{c.value * 100:.2f}%",
                "Scale": c.band_scale,
                "Credit View": c.band,
            }
            for c in portfolio.criteria
        ]
        render_html_table(
            criteria_rows,
            columns=["Criteria", "Value", "Scale", "Credit View"],
            badge_col="Credit View",
        )
        st.caption(
            "'Manufacturing Sector Exposure' reuses the same post-sanction single-borrower "
            "exposure % as the Borrower Concentration Risk row above (both = Proposed Loan / "
            "Post-Sanction Exposure) - reproduced exactly as the reference workbook computes it, "
            "not a separately modelled sector-level metric."
        )

        chart_caption("Risk criteria vs their band cut-points")
        fig = magnitude_bar(
            [c.label for c in portfolio.criteria],
            [c.value * 100 for c in portfolio.criteria],
            value_fmt="{:.2f}%", sort_desc=False,
        )
        st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG)



# ============================================================
# 10. CREDIT MEMO
# ============================================================

elif selected_area == AREAS[9]:

    render_page_header(
        "Credit Memo", crumb,
        "Consolidated status across all 8 areas. This module performs no calculations of its "
        "own - every status below is collected from the module that computed it.",
    )

    cm9_cov_policy = load_covenant_policy(bundle.workbook_path)
    cm9_covenants = build_covenants(fa, cm9_cov_policy)

    cm9_coll_inputs = load_collateral_inputs(bundle.workbook_path)
    cm9_collateral = (
        build_collateral_assessment(cm9_coll_inputs, fa.derived.total_debt.current)
        if cm9_coll_inputs else None
    )

    cm9_limit_policy = load_limit_setting_policy(bundle.workbook_path)
    cm9_limit = build_limit_setting(cm9_limit_policy, fa.derived.ebit.current, d.reported_dscr.current)

    cm9_mon_policy = load_monitoring_policy(bundle.workbook_path)
    cm9_rows_by_key = {r.key: r for r in fa.income_statement_rows + fa.ratio_rows}
    cm9_breach = any(r.rag in ("AMBER", "RED") for r in cm9_covenants.rows)
    cm9_monitoring = build_continuous_monitoring(
        cm9_mon_policy, cm9_rows_by_key["revenue"].pct_change, cm9_rows_by_key["total_debt"].pct_change,
        cm9_rows_by_key["dscr"].pct_change, cm9_breach,
    )

    cm9_portfolio_inputs = load_portfolio_inputs(bundle.workbook_path)
    cm9_portfolio = build_portfolio_monitoring(cm9_portfolio_inputs) if cm9_portfolio_inputs else None

    commentary = load_credit_memo_commentary(bundle.workbook_path)
    memo = build_credit_memo(fa, risk, cm9_covenants, cm9_collateral, cm9_limit, cm9_monitoring, cm9_portfolio, commentary)

    render_section("Area Status Summary")
    status_rows = [{"Area": a.area_label, "Status": a.status if isinstance(a.status, str) else str(a.status)} for a in memo.areas]
    render_html_table(status_rows, columns=["Area", "Status"], badge_col="Status")

    render_section(
        "Reference Commentary",
        "Reference workbook commentary — not dynamically generated. This is the analyst-"
        "written narrative read verbatim from the active workbook's own '9 - Credit Memo' "
        "sheet, shown for context only; it is never presented as if generated for whichever "
        "borrower is currently loaded. The status badge above each paragraph, in contrast, "
        "is this application's own live computed result for that area.",
    )
    for area in memo.areas:
        status_text = area.status if isinstance(area.status, str) else str(area.status)
        st.markdown(
            f'<div class="cr-card" style="margin-bottom:0.8rem;">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.5rem;">'
            f'<strong>{area.area_label}</strong>{badge_html(status_text, status_kind(status_text))}'
            f'</div>'
            f'<div style="color:{INK_MUTED};font-size:0.88rem;white-space:pre-line;">{area.reference_commentary}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

