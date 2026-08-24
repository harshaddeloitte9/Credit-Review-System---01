import os
import tempfile

import streamlit as st
from openpyxl import load_workbook

from reference_data import load_reference_bundle, ReferenceDataError
from workbook_validation import validate_workbook
import ocr_upload
from input_data_engine import compute_derived_metrics, run_validation_checks
from financial_analysis import build_financial_analysis
from risk_scoring import build_risk_scoring
from covenants import load_covenant_policy, build_covenants
from collateral_assessment import load_collateral_inputs, build_collateral_assessment
from limit_setting import (
    load_limit_setting_policy, build_limit_setting, LimitSettingResult,
    FundBasedInputs, build_fund_based_limit,
    NonFundBasedInputs, build_non_fund_based_limit,
)
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

# Debtors Days / Inventory Days used to be shown here as a read-only echo
# of the *policy* workbook's own '2-Financial Analysis'!C23:D24 (Tata
# Steel's static figures), since financial_analysis.py could not compute
# them for any borrower. That mechanism was removed once financial_analysis.py
# gained a real, per-borrower source for these two fields (Reported Ratios
# rows 74/78 - see that module's docstring): the policy sheet is copied
# unchanged into every borrower's merged workbook, so the old echo would
# have kept showing Tata Steel's figures for every borrower even after
# real data started flowing through fa.ratio_rows, silently undoing the
# fix. fa.ratio_rows is now the only source for these two rows, exactly
# like every other metric on this page.


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


PDF_SESSION_KEYS = [
    ("pdf_name", None),
    ("pdf_size", None),
    ("_last_pdf_upload_id", None),
    ("pdf_processing_error", None),
    ("raw_ocr_path", None),
    ("extracted_workbook_path", None),
    ("extracted_workbook_name", None),
    ("merged_workbook_path", None),
    ("extraction_status_counts", None),
    ("extraction_total_fields", None),
    ("ocr_reused", False),
    ("pdf_processing_failed_stage", None),
]

# Fund-Based / Non-Fund-Based Limit Setting - analyst-entered assumptions
# (Area 7). Unlike every other figure in this app, none of these have a
# source cell anywhere in the reference workbook or the OCR-extracted
# Input Data (no Inventory/Receivable/Payable Days, Sanctioned Limits,
# Utilisation, LC or BG fields exist anywhere in this application's
# schema) - so they are analyst-entered per review, not workbook- or
# PDF-derived. Reset alongside the other per-borrower keys below so a
# new upload never inherits a previous borrower's typed figures.
_FB_NFB_VALUE_KEYS = [
    ("fb_annual_sales", None),
    ("fb_inventory_days", None),
    ("fb_receivable_days", None),
    ("fb_payable_days", None),
    ("fb_contribution_pct", 25.0),
    ("nfb_annual_imports", None),
    ("nfb_peak_lc", None),
    ("nfb_contractual_obligations", None),
    ("nfb_peak_bg", None),
]

# Each field's actual st.number_input widget is bound to a separate
# "_..._widget" key (see _persistent_number_input), never to the value
# key itself. IMPORTANT: these must NOT be pre-registered by the
# unconditional key-init loop the way every other session key is -
# st.number_input ignores its `value=` seed once its `key` already exists
# in session_state (even if that pre-existing value is just an untouched
# init-loop default), which would defeat _persistent_number_input's
# "re-seed from the stable value key" mechanism on every first render.
# They are only ever deleted (never assigned a value) on a reset, via
# _FB_NFB_WIDGET_KEYS below - letting the next widget instantiation seed
# freshly from its (also just-reset) stable value key.
_FB_NFB_WIDGET_KEYS = [f"_{key}_widget" for key, _default in _FB_NFB_VALUE_KEYS]

FB_NFB_SESSION_KEYS = _FB_NFB_VALUE_KEYS + [
    # Tracks which upload each auto-populated field was last prefilled for
    # (see the prefill block below) - NOT which value it holds. Keying
    # re-fill on upload identity rather than "is the value currently None"
    # makes the prefill self-healing: Streamlit never clears session_state
    # on a code hot-reload, so a stale non-None value from an earlier
    # iteration of this feature could otherwise never self-correct.
    ("_fb_annual_sales_prefilled_for", None),
    # Inventory/Receivable Days: auto-populated from this document's own
    # disclosed Debtors/Inventory turnover ratio when it discloses one
    # (see financial_analysis.py / reference_data.py) - same mechanism,
    # added after a data-source audit found this real source.
    ("_fb_inventory_days_prefilled_for", None),
    ("_fb_receivable_days_prefilled_for", None),
]


def _reset_fb_nfb_session_state():
    for _key, _default in FB_NFB_SESSION_KEYS:
        st.session_state[_key] = _default
    for _widget_key in _FB_NFB_WIDGET_KEYS:
        st.session_state.pop(_widget_key, None)


def _cleanup_pdf_artifacts():
    """Removes the temp files a previous PDF upload generated (raw OCR
    text, extracted workbook). The merged workbook is not removed here -
    demo_source.build_demo_workbook() overwrites the same fixed path on
    every call, matching its own existing convention, so there is
    nothing stale left behind for it to clean up."""
    for _key in ("raw_ocr_path", "extracted_workbook_path"):
        _path = st.session_state.get(_key)
        if _path and os.path.exists(_path):
            try:
                os.remove(_path)
            except OSError:
                pass


def _reset_to_no_data():
    """Clears every session_state key either upload path uses, back to
    an empty "no active borrower" state - used both when the user
    explicitly starts a new upload and when a workbook fails to load,
    so a failed load is never silently retried forever."""
    _cleanup_uploaded_workbook()
    _cleanup_pdf_artifacts()
    for _key, _default in [
        ("source_mode", "none"),
        ("uploaded_workbook_path", None),
        ("uploaded_workbook_name", None),
        ("uploaded_workbook_warnings", []),
        ("_last_upload_id", None),
        ("selected_area", AREAS[0]),
    ] + PDF_SESSION_KEYS:
        st.session_state[_key] = _default
    _reset_fb_nfb_session_state()


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
    "4. Covenant Monitoring",
    "5. Collateral Assessment",
    "6. Limit Setting",
    "7. Portfolio Monitoring",
    "8. Credit Memo",
    # Risk Scoring is presented last, in its own "Additional Features"
    # section, at the director's explicit request for demos - shown as
    # an additional capability after the main credit-review workflow,
    # not part of the primary sequence above. See ui_theme.py's sidebar
    # CSS for the matching section heading.
    "9. Risk Scoring",
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
    ("source_mode", "none"),
    ("uploaded_workbook_path", None),
    ("uploaded_workbook_name", None),
    ("uploaded_workbook_warnings", []),
    ("_last_upload_id", None),
    ("uploader_key_version", 0),
] + PDF_SESSION_KEYS + FB_NFB_SESSION_KEYS:
    if _key not in st.session_state:
        st.session_state[_key] = _default
# _FB_NFB_WIDGET_KEYS are deliberately NOT initialized here - see the
# comment above their definition. They come into existence only via
# their own st.number_input's first instantiation (correctly seeded from
# the value keys just initialized above) or not at all until then.

_using_upload = st.session_state["source_mode"] == "uploaded" and st.session_state["uploaded_workbook_path"]
_using_pdf = st.session_state["source_mode"] == "pdf" and st.session_state["merged_workbook_path"]
_has_active_data = bool(_using_upload or _using_pdf)

if _using_upload:
    source_label = f"Uploaded workbook: {st.session_state['uploaded_workbook_name']}"
elif _using_pdf:
    source_label = f"Uploaded PDF: {st.session_state['pdf_name']}"
else:
    source_label = "No financial statement uploaded"


# ============================================================
# LOAD DATA FROM THE ACTIVE SOURCE (+ the two validated
# calculation results, computed once here so the sidebar status
# card and every page below reuse the same numbers instead of
# recomputing them)
# ============================================================
#
# No demo/default dataset is loaded automatically - d/bundle/fa/risk
# stay None until a PDF has been uploaded and processed (or, in
# Advanced mode, a complete replacement workbook). Every page except
# Data Upload shows a "no data" state instead of rendering while that
# is true (see the _has_active_data guard just above the page-routing
# chain below). PDF mode: '1-Input Data' comes from OCR-extracting the
# just-uploaded PDF (see ocr_upload.py), merged into a copy of the
# policy workbook (Credit Review Automation Example.xlsx) for the
# '2-Financial Analysis' through '9 - Credit Memo' policy/threshold
# cells that only that workbook carries - never for borrower figures.
# Uploaded-workbook mode (Advanced, hidden by default) is unchanged:
# the user's own complete workbook, used as-is.

bundle = None
d = None
fa = None
risk = None

if _has_active_data:
    active_workbook_path = (
        st.session_state["uploaded_workbook_path"] if _using_upload
        else st.session_state["merged_workbook_path"]
    )

    try:
        bundle = load_reference_bundle(path=active_workbook_path)
        d = bundle.input_data
        fa = build_financial_analysis(bundle)
        risk = build_risk_scoring(bundle)
    except ReferenceDataError as exc:
        st.error(f"Could not load '{source_label}'.")
        st.code(str(exc))
        st.info("Upload a financial statement on the Data Upload page to continue.")
        _reset_to_no_data()
        _has_active_data = False
        bundle = None

# Fund-Based Limit Setting's Annual Sales / Inventory Days / Receivable
# Days fields default to this borrower's own Revenue / disclosed Inventory
# turnover / disclosed Debtors turnover (each editable) the first time
# data is available after a reset - done once here, centrally, rather
# than inside the Limit Setting page, so Credit Memo reads the same
# pre-filled values even if the analyst never visits the Limit Setting
# page directly.
_fb_upload_id = (
    st.session_state.get("_last_pdf_upload_id") if st.session_state.get("source_mode") == "pdf"
    else st.session_state.get("_last_upload_id")
)
_fb_prefill_identity = f"{st.session_state.get('source_mode')}:{_fb_upload_id}"
if d is not None and st.session_state.get("_fb_annual_sales_prefilled_for") != _fb_prefill_identity:
    st.session_state["fb_annual_sales"] = d.revenue.current
    st.session_state["_fb_annual_sales_prefilled_for"] = _fb_prefill_identity
if d is not None and st.session_state.get("_fb_inventory_days_prefilled_for") != _fb_prefill_identity:
    st.session_state["fb_inventory_days"] = d.reported_inventory_turnover_days.current
    st.session_state["_fb_inventory_days_prefilled_for"] = _fb_prefill_identity
if d is not None and st.session_state.get("_fb_receivable_days_prefilled_for") != _fb_prefill_identity:
    st.session_state["fb_receivable_days"] = d.reported_debtors_turnover_days.current
    st.session_state["_fb_receivable_days_prefilled_for"] = _fb_prefill_identity

if d is not None:
    crumb = (
        f"{d.company_name} · {d.statement_scope} · FY{d.fy_current_year} · {d.currency_unit.title()} "
        f"· Source: {source_label}"
    )
else:
    crumb = "No financial statement uploaded yet"

if fa is not None and risk is not None:
    render_sidebar_status(
        d.company_name,
        f"{d.statement_scope} · FY{d.fy_current_year}",
        fa.overall_status,
        "good" if fa.overall_status == "Approve" else "critical",
        f"{risk.total_weighted_score:.2f} / 5.00",
    )
else:
    st.sidebar.markdown(
        '<div class="cr-sidebar-status">'
        '<div class="cr-sidebar-status-eyebrow">Active Borrower</div>'
        '<div class="cr-sidebar-status-name">No data uploaded</div>'
        '<div class="cr-sidebar-status-meta">Upload a financial statement PDF to begin</div>'
        "</div>",
        unsafe_allow_html=True,
    )

# key="selected_area" is the single source of truth for the current page -
# the sidebar radio and the bottom-of-page workflow buttons (render_stage_nav,
# below) both read/write this same session_state entry, so a button click
# and a sidebar click move the SAME state rather than running two
# independent navigation systems. st.radio defaults to its first option
# (AREAS[0], Data Upload) the first time this key doesn't exist yet - no
# separate init needed.
selected_area = st.sidebar.radio("Go to", AREAS, key="selected_area", label_visibility="collapsed")

st.sidebar.markdown("---")
_sidebar_source_verb = "Manage" if _advanced_mode_enabled() else "View"
st.sidebar.caption(
    f"Active dataset: **{source_label}**. {_sidebar_source_verb} the data source on the Data Upload page."
)
st.sidebar.caption(
    "8 credit-analysis modules are implemented, each validated against the reference workbook. "
    "Borrower data is extracted live from an uploaded PDF via OCR (`ocr_pipeline.py` / "
    "`ocr_upload.py`); policy/threshold sheets continue to come from the reference workbook."
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

FY_CURRENT_SHORT = f"FY{d.fy_current_year}" if d is not None else "FY-current"
FY_PREVIOUS_SHORT = f"FY{d.fy_previous_year}" if d is not None else "FY-previous"


def fmt_money(value):
    return "—" if value is None else f"{value:,.2f}"


def fmt_money_cr(value):
    return "—" if value is None else f"₹{value:,.0f} Cr"


def fmt_money_cr_or_review(value):
    # Used for analyst-entered Fund-Based/Non-Fund-Based figures (Limit
    # Setting, Credit Memo) where a missing input must read as an explicit
    # call to action, not the generic "—" used elsewhere for figures this
    # application can never source for a given document.
    return "Not Available / Requires Review" if value is None else f"₹{value:,.0f} Cr"


def fmt_days_or_review(value):
    return "Not Available / Requires Review" if value is None else f"{value:,.0f} days"


def _persistent_number_input(label, session_key, **kwargs):
    """st.number_input wrapper for the FB/NFB analyst-entry fields (Limit
    Setting). A plain assignment to a widget-bound session_state key made
    from a run where that widget isn't instantiated (e.g. a different
    page) can be silently discarded once the widget is instantiated
    again - verified empirically: navigating away from Limit Setting and
    back reverted a manually-typed value back to its much earlier state,
    with no code of this application's own having touched it in between.
    To keep analyst-entered figures durable across navigation, the actual
    widget uses a private "_..._widget" key, re-seeded every render from
    - and immediately written back to - session_key, which is never
    itself bound to a widget and therefore persists reliably."""
    widget_key = f"_{session_key}_widget"
    value = st.number_input(label, key=widget_key, value=st.session_state.get(session_key), **kwargs)
    st.session_state[session_key] = value
    return value


def _nav_set_selected_area(area_label):
    # Streamlit forbids writing to a widget-bound session_state key (here,
    # "selected_area", owned by the sidebar radio) from ordinary script
    # code that runs AFTER that widget has already been instantiated in
    # the same run - which a bottom-of-page button always is, since the
    # sidebar radio renders long before any page body. on_click callbacks
    # are Streamlit's own mechanism for exactly this: they run in a
    # separate phase BEFORE the next script run (and its widgets) begin,
    # so the write is applied safely, then Streamlit reruns automatically
    # because the button's own value changed - no manual st.rerun() needed.
    st.session_state["selected_area"] = area_label


def render_stage_nav(current_index, primary_label, primary_disabled=False, primary_help=None,
                      primary_target_index=None):
    """Bottom-of-page guided-workflow controls: '<- Previous Step' (when
    there is a previous stage) and a primary continue button. Both write
    the SAME "selected_area" session_state key the sidebar radio itself
    is bound to (see its own key="selected_area" above) via on_click - so
    there is no separate/second navigation state, a button click and a
    sidebar click move the identical value. Pure navigation only: nothing
    here reads, writes, or otherwise touches any borrower financial data,
    calculation, or session_state key belonging to any module - "Accept"
    is a stage-confirmation label, not an action that can fabricate or
    change anything.

    primary_target_index: which AREAS index the primary button navigates
    to. Defaults to current_index + 1 (the next stage); pass an explicit
    index (e.g. 0, for Risk Scoring's "Finish" button returning to Data
    Upload) to send it somewhere else in the workflow instead. Pass -1
    (or leave both un-navigable) only if there is truly nothing to
    advance to.
    """
    if primary_target_index is None:
        primary_target_index = current_index + 1
    has_target = 0 <= primary_target_index < len(AREAS)

    st.divider()
    col_prev, _spacer, col_next = st.columns([2, 5, 2])

    with col_prev:
        if current_index > 0:
            st.button(
                "← Previous Step", key=f"nav_prev_{current_index}",
                type="secondary", width="stretch",
                on_click=_nav_set_selected_area, args=(AREAS[current_index - 1],),
            )

    with col_next:
        st.button(
            primary_label, key=f"nav_next_{current_index}", type="primary",
            disabled=primary_disabled, help=primary_help, width="stretch",
            on_click=_nav_set_selected_area if has_target else None,
            args=(AREAS[primary_target_index],) if has_target else None,
        )


# ============================================================
# DATA UPLOAD - live processing-stage pipeline. Reflects the real stages
# ocr_upload.process_uploaded_pdf() reports via its status_callback
# (see ocr_upload.py) - never a simulated/timed progress indicator.
# ============================================================

UPLOAD_STAGES = [
    ("source", "Source Document"),
    ("ocr", "OCR Extraction"),
    ("extraction", "Financial Data Extraction"),
    ("population", "Data Population"),
]

_UPLOAD_STAGE_DISPLAY = {
    "complete": ("✓ Complete", "good"),
    "reused": ("✓ Previously Extracted", "good"),
    "processing": ("⟳ Processing", "processing"),
    "failed": ("✕ Failed", "critical"),
}


def _render_upload_pipeline(placeholder, stage_states):
    steps = []
    for key, title in UPLOAD_STAGES:
        subtitle, kind = _UPLOAD_STAGE_DISPLAY.get(stage_states.get(key), ("○ Waiting", "neutral"))
        steps.append((title, subtitle, kind))
    with placeholder.container():
        render_flow_steps(steps)


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
# Real PDF upload flow: uploaded PDF -> ocr_upload.process_uploaded_pdf()
# (LLMWhisperer OCR -> ocr_pipeline.extract_financial_values() ->
# ocr_pipeline.populate_workbook(), a fresh copy of
# Credit_Review_Tool_Input_Data_Template.xlsx every time) -> merged with
# the policy workbook (Credit Review Automation Example.xlsx, for
# policy/threshold sheets only - never a borrower-data source) ->
# activated as the dataset every other page reads. No demo/default
# dataset is loaded automatically; until a PDF is uploaded here, every
# other page shows a "no data" state (see the _has_active_data guard
# just below the page-routing chain).

if selected_area == AREAS[0]:

    render_page_header(
        "Data Upload", crumb,
        "Upload a financial statement PDF to extract borrower data via OCR and activate it "
        "for this credit review.",
    )

    render_section("Upload Financial Statement", "PDF")

    _uploaded_pdf = st.file_uploader(
        "Upload a financial statement (PDF)",
        type=["pdf"],
        key=f"pdf_uploader_{st.session_state['uploader_key_version']}",
    )

    if _uploaded_pdf is not None:
        _pdf_upload_id = getattr(_uploaded_pdf, "file_id", None) or f"{_uploaded_pdf.name}:{_uploaded_pdf.size}"
        if _pdf_upload_id != st.session_state["_last_pdf_upload_id"]:
            st.session_state["_last_pdf_upload_id"] = _pdf_upload_id
            _cleanup_pdf_artifacts()
            st.session_state["pdf_processing_error"] = None
            st.session_state["pdf_processing_failed_stage"] = None
            _reset_fb_nfb_session_state()

            os.makedirs(ocr_upload.UPLOAD_WORK_DIR, exist_ok=True)
            _safe_id = _pdf_upload_id.replace(":", "_").replace("/", "_").replace("\\", "_")
            _pdf_temp_path = os.path.join(ocr_upload.UPLOAD_WORK_DIR, f"upload_{_safe_id}.pdf")
            with open(_pdf_temp_path, "wb") as f:
                f.write(_uploaded_pdf.getbuffer())

            with st.status("Processing uploaded financial statement...", expanded=True) as _status:
                st.write(f"Uploading **{_uploaded_pdf.name}**...")
                _stage_states = {"source": "complete"}
                _pipeline_ph = st.empty()
                _render_upload_pipeline(_pipeline_ph, _stage_states)
                _stage_titles = dict(UPLOAD_STAGES)

                def _on_stage(stage, state, detail=None):
                    _stage_states[stage] = state
                    _render_upload_pipeline(_pipeline_ph, _stage_states)
                    if state == "failed":
                        st.session_state["pdf_processing_failed_stage"] = _stage_titles.get(stage, stage)
                        if detail:
                            st.error(f"**{_stage_titles.get(stage, stage)} failed:** {detail}")

                try:
                    _result = ocr_upload.process_uploaded_pdf(
                        _pdf_temp_path, _uploaded_pdf.name, status_callback=_on_stage,
                    )
                    st.session_state["pdf_name"] = _uploaded_pdf.name
                    st.session_state["pdf_size"] = _uploaded_pdf.size
                    st.session_state["raw_ocr_path"] = _result["raw_ocr_path"]
                    st.session_state["extracted_workbook_path"] = _result["extracted_workbook_path"]
                    st.session_state["extracted_workbook_name"] = _result["extracted_workbook_name"]
                    st.session_state["merged_workbook_path"] = _result["merged_workbook_path"]
                    st.session_state["extraction_status_counts"] = _result["status_counts"]
                    st.session_state["extraction_total_fields"] = _result["total_fields"]
                    st.session_state["ocr_reused"] = _result["ocr_reused"]
                    st.session_state["source_mode"] = "pdf"
                    _status.update(label="Processing complete", state="complete")
                except Exception as exc:
                    st.session_state["pdf_processing_error"] = str(exc)
                    _status.update(label="Processing failed", state="error")
            st.rerun()

    if st.session_state.get("pdf_processing_error"):
        _failed_stage = st.session_state.get("pdf_processing_failed_stage")
        st.error(
            f"Could not process the uploaded PDF - **{_failed_stage}** failed."
            if _failed_stage else "Could not process the uploaded PDF."
        )
        st.code(st.session_state["pdf_processing_error"])

    if not _using_pdf:
        if not st.session_state.get("pdf_processing_error"):
            st.info("No financial statement uploaded yet. Upload a PDF above to begin.")

    else:
        # ---- Source Document -----------------------------------------
        render_section("Source Document")
        _pdf_size_mb = (st.session_state["pdf_size"] or 0) / (1024 * 1024)
        st.markdown(
            '<div class="cr-card">'
            '<div style="display:flex;align-items:center;gap:0.9rem;">'
            '<div class="cr-doc-icon">PDF</div>'
            '<div>'
            f'<div class="cr-kpi-value" style="font-size:1rem;">{st.session_state["pdf_name"]}</div>'
            f'<div class="cr-kpi-sub">PDF · {_pdf_size_mb:.1f} MB</div>'
            f'<div style="margin-top:0.45rem;">{badge_html("✓ Uploaded", "good")}</div>'
            "</div>"
            "</div>"
            "</div>",
            unsafe_allow_html=True,
        )

        # ---- Document Extraction ---------------------------------------
        render_section("Document Extraction")
        render_flow_steps([
            ("Source Document", st.session_state["pdf_name"], "good"),
            ("OCR Extraction", "✓ Previously Extracted" if st.session_state.get("ocr_reused") else "✓ Completed", "good"),
            ("Financial Data Extraction", "Completed", "good"),
            ("Data Population", st.session_state["extracted_workbook_name"], "good"),
        ])
        if st.session_state.get("ocr_reused"):
            st.caption(
                "OCR was not re-run for this document - a saved OCR result for this exact PDF "
                "content already existed and was reused, so no new call was made to the OCR "
                "provider."
            )
        _counts = st.session_state["extraction_status_counts"] or {}
        _total = st.session_state["extraction_total_fields"] or 0
        st.caption(
            f"Of {_total} mapped fields: {_counts.get('CORRECT', 0)} extracted directly, "
            f"{_counts.get('NORMALIZED_FROM_OCR', 0)} normalized from OCR formatting defects, "
            f"{_counts.get('NOT_FOUND_IN_OCR', 0)} not present in the source document, "
            f"{_counts.get('AMBIGUOUS', 0)} withheld as ambiguous (see Input & Validation)."
        )

        # ---- Extracted Financial Data -----------------------------------
        render_section("Extracted Financial Data")
        _extracted_path = st.session_state["extracted_workbook_path"]
        if _extracted_path and os.path.exists(_extracted_path):
            _extracted_size_kb = os.path.getsize(_extracted_path) / 1024
            doc_col, dl_col = st.columns([4, 1])
            with doc_col:
                st.markdown(
                    '<div class="cr-card">'
                    '<div style="display:flex;align-items:center;gap:0.9rem;">'
                    '<div class="cr-doc-icon">XLS</div>'
                    '<div>'
                    f'<div class="cr-kpi-value" style="font-size:1rem;">{st.session_state["extracted_workbook_name"]}</div>'
                    f'<div class="cr-kpi-sub">Excel · {_extracted_size_kb:.0f} KB · generated from '
                    f'Credit_Review_Tool_Input_Data_Template.xlsx</div>'
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
                    data=_read_file_bytes(_extracted_path),
                    file_name=st.session_state["extracted_workbook_name"],
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    width="stretch",
                )
            st.caption("The '1-Input Data' sheet generated from this upload's own OCR text.")

        # ---- Raw OCR Output ---------------------------------------------
        render_section("Raw OCR Output")
        _raw_ocr_path = st.session_state["raw_ocr_path"]
        if _raw_ocr_path and os.path.exists(_raw_ocr_path):
            _raw_ocr_name = os.path.basename(_raw_ocr_path)
            _raw_ocr_size_kb = os.path.getsize(_raw_ocr_path) / 1024
            doc_col, dl_col = st.columns([4, 1])
            with doc_col:
                st.markdown(
                    '<div class="cr-card">'
                    '<div style="display:flex;align-items:center;gap:0.9rem;">'
                    '<div class="cr-doc-icon">TXT</div>'
                    '<div>'
                    f'<div class="cr-kpi-value" style="font-size:1rem;">{_raw_ocr_name}</div>'
                    f'<div class="cr-kpi-sub">Text · {_raw_ocr_size_kb:.0f} KB</div>'
                    f'<div style="margin-top:0.45rem;">{badge_html("✓ Ready", "good")}</div>'
                    "</div>"
                    "</div>"
                    "</div>",
                    unsafe_allow_html=True,
                )
            with dl_col:
                st.markdown("<div style='margin-top:0.9rem;'></div>", unsafe_allow_html=True)
                st.download_button(
                    "Download Raw OCR",
                    data=_read_file_bytes(_raw_ocr_path),
                    file_name=_raw_ocr_name,
                    mime="text/plain",
                    width="stretch",
                )
            st.caption("The exact LLMWhisperer result_text for this upload, unmodified.")

        # ---- Active Data Source -------------------------------------------
        render_section("Active Data Source")
        _borrower_label = d.company_name if d is not None else "Unknown"
        st.markdown(
            '<div class="cr-card">'
            '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem;">'
            f'<div><div class="cr-kpi-label">Borrower</div><div class="cr-kpi-value" style="font-size:1rem;">{_borrower_label}</div></div>'
            f'<div><div class="cr-kpi-label">Uploaded document</div><div class="cr-kpi-value" style="font-size:1rem;">{st.session_state["pdf_name"]}</div></div>'
            f'<div><div class="cr-kpi-label">Status</div><div style="margin-top:0.25rem;">{badge_html("● Active", "good")}</div></div>'
            "</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        st.caption(
            "Company name is not automatically detected from the source document in this build - "
            "the uploaded filename above is the reliable identifier for this borrower. All "
            "downstream modules (Input & Validation through Credit Memo) read from this dataset."
        )

        if st.button("↺ Upload a different financial statement"):
            _reset_to_no_data()
            st.session_state["uploader_key_version"] += 1
            st.rerun()

    # ---- Advanced: the existing dynamic-upload feature ------------------
    # Not deleted - only hidden from the default consultant-facing view.
    # See _advanced_mode_enabled() above for the two (non-UI) ways to
    # reveal it. Bypasses OCR entirely: a complete replacement workbook
    # supplied as-is, kept for testing/QA - never the source for a real
    # PDF upload, which always goes through the flow above instead.
    if _advanced_mode_enabled():
        with st.expander("Advanced: load a different Credit Review workbook (for testing)"):
            st.caption(
                "This uploads a complete replacement workbook (all sheets, not just borrower "
                "data) and switches every module's data source to it directly, bypassing OCR "
                "entirely - separate from the PDF upload above."
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
                if st.button("↺ Clear this workbook"):
                    _reset_to_no_data()
                    st.session_state["uploader_key_version"] += 1
                    st.rerun()
            else:
                st.caption("No replacement workbook loaded.")

    # Guided-workflow continue button - only enabled once a document has
    # actually finished the real pipeline (Source Document -> OCR ->
    # Financial Extraction -> Data Population -> Complete) with no error,
    # reusing the exact same _has_active_data / pdf_processing_error state
    # every other page already gates on - never a separate/fake check.
    # Disabled (not hidden) while nothing has completed yet, so the
    # analyst always sees the workflow's next step without it falsely
    # implying processing succeeded.
    _data_upload_ready = _has_active_data and not st.session_state.get("pdf_processing_error")
    render_stage_nav(
        0, "Continue to Validation →",
        primary_disabled=not _data_upload_ready,
        primary_help=None if _data_upload_ready else "Upload and process a financial statement (OCR, extraction, data population all complete) before continuing.",
    )


# ============================================================
# NO ACTIVE DATA - every page below this point (Input & Validation
# through Credit Memo) requires an active borrower dataset. Rather
# than each page guarding itself, this single branch intercepts all
# of them at once whenever nothing has been uploaded yet (or a
# previous upload failed to load) - showing the same message a
# consultant would see is a stronger signal than ten different pages
# quietly rendering nothing.
# ============================================================

elif not _has_active_data:

    _page_title = selected_area.split(". ", 1)[1] if ". " in selected_area else selected_area
    render_page_header(_page_title, crumb, "No financial statement uploaded yet.")
    st.info(
        "No data uploaded. Go to **Data Upload** and upload a financial statement PDF to "
        "activate this page."
    )


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

    render_stage_nav(1, "Accept & Continue →")


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
    ebitda_margin_value = f"{r.current:.2f}%" if r.current is not None else "Not available"
    ebitda_margin_card = kpi_card_html("EBITDA Margin", ebitda_margin_value, delta, kind, "EBITDA / Revenue")

    r = rows_by_key["net_profit"]
    delta, kind = delta_text_kind(r.current, r.previous)
    net_profit_card = kpi_card_html("Net Profit", fmt_money_cr(r.current), delta, kind, "PBT − Tax")

    r = rows_by_key["net_profit_margin_pct"]
    delta, kind = delta_text_kind(r.current, r.previous, as_points=True)
    net_profit_margin_value = f"{r.current:.2f}%" if r.current is not None else "Not available"
    net_profit_margin_card = kpi_card_html("Net Profit Margin", net_profit_margin_value, delta, kind, "Net Profit / Revenue")

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
            "disclosure-basis Net Worth card above, which is drawn from the reported figures."
        )

    # ---- 4. Liquidity & Leverage ----------------------------------
    render_section("Liquidity & Leverage", "Reported Ratios basis")
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
            label, f"{rr.current:.2f}x" if rr.current is not None else "Not available",
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

    # Each bullet needs both years of its own metric to be available -
    # narrated only when that is true, matching the same "not available"
    # convention used everywhere else rather than crashing on a None a
    # genuine OCR extraction can legitimately produce (e.g. EBITDA margin,
    # which depends on Interest Income - not always separately reported).
    health_items = []

    if r_rev.current is not None and r_rev.previous is not None:
        health_items.append((
            f"Revenue {_direction(r_rev.current, r_rev.previous)} "
            f"{abs(r_rev.pct_change) * 100:.1f}% YoY to ₹{r_rev.current:,.0f} cr in {d.fy_current_label}.",
            "good" if r_rev.current >= r_rev.previous else "warning",
        ))

    if r_em.current is not None and r_em.previous is not None:
        health_items.append((
            f"EBITDA margin {_direction(r_em.current, r_em.previous, 'improved', 'declined')} from "
            f"{r_em.previous:.2f}% to {r_em.current:.2f}%.",
            "good" if r_em.current >= r_em.previous else "warning",
        ))
    else:
        health_items.append((
            "EBITDA margin change is not available - requires Interest Income, which this "
            "document does not separately report in the P&L.",
            "warning",
        ))

    if r_td.current is not None and r_td.previous is not None:
        health_items.append((
            f"Total debt {_direction(r_td.current, r_td.previous, 'increased', 'decreased')} "
            f"{abs(r_td.pct_change) * 100:.1f}% YoY to ₹{r_td.current:,.0f} cr.",
            "good" if r_td.current <= r_td.previous else "warning",
        ))

    if r_cr.current is not None and r_cr.threshold is not None:
        health_items.append((
            f"Current ratio stands at {r_cr.current:.2f}x, "
            f"{'meeting' if r_cr.status == 'Pass' else 'below'} the {r_cr.threshold:.2f}x policy "
            f"minimum - a {'comfortable' if r_cr.current >= 1 else 'constrained'} short-term liquidity position.",
            "good" if r_cr.status == "Pass" else "critical",
        ))

    if r_dscr.current is not None and r_dscr.threshold is not None:
        health_items.append((
            f"DSCR of {r_dscr.current:.2f}x is {'above' if r_dscr.status == 'Pass' else 'below'} the "
            f"{r_dscr.threshold:.2f}x policy minimum, indicating "
            f"{'adequate' if r_dscr.status == 'Pass' else 'constrained'} debt-servicing capacity on a reported basis.",
            "good" if r_dscr.status == "Pass" else "critical",
        ))

    if r_ocf.current is not None and r_ocf.previous is not None:
        health_items.append((
            f"Operating cash flow {_direction(r_ocf.current, r_ocf.previous)} "
            f"{abs(r_ocf.pct_change) * 100:.1f}% YoY to ₹{r_ocf.current:,.0f} cr.",
            "good" if r_ocf.current >= r_ocf.previous else "warning",
        ))

    render_health_summary(health_items)

    # ---- Detail tables & notes --------------------------------------
    render_section("Income Statement / Balance Sheet / Cash Flow — detail")
    metric_rows_html_table(fa.income_statement_rows, d.fy_current_label, d.fy_previous_label)

    render_section(
        "Key Ratios — detail",
        "Debtors Days and Inventory Days are this document's own disclosed turnover ratios "
        "(Reported Ratios block) when it discloses them, and included in the Overall Status "
        "gate above like every other metric here - shown as not available for a document "
        "whose own disclosures don't include them.",
    )
    metric_rows_html_table(fa.ratio_rows, d.fy_current_label, d.fy_previous_label)

    with st.expander("Flagged discrepancies & notes"):
        for row in fa.income_statement_rows + fa.ratio_rows:
            if row.note:
                st.markdown(f"**{row.label}** — {row.note}")

    render_stage_nav(2, "Accept Analysis & Continue →")



# ============================================================
# 4. COVENANT MONITORING
# ============================================================

elif selected_area == AREAS[3]:

    render_page_header(
        "Covenant Monitoring", crumb,
        "Financial and non-financial covenant status, plus continuous / early-warning "
        "monitoring - all in one place. Financial covenant headroom and RAG status are "
        "computed from Financial Analysis's reported DSCR/ISCR against the reference "
        "workbook's own policy minimums.",
    )

    covenant_policy = load_covenant_policy(bundle.workbook_path)
    covenants = build_covenants(fa, covenant_policy)
    covenant_breach = any(r.rag in ("AMBER", "RED") for r in covenants.rows)

    render_section("Financial Covenants")

    overall_kind = "good" if covenants.overall_status == "PASS" else "critical"
    st.markdown(
        badge_html(covenants.overall_status, overall_kind)
        + f'&nbsp;&nbsp;<span style="color:{INK_MUTED};font-size:0.85rem;">'
          f'PASS unless any covenant is AMBER or RED.</span>',
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

    st.markdown('<div class="cr-section-sub">Covenant Headroom — Headroom % = (Actual − Threshold) / Threshold. RAG: &gt; 20% Green, 5-20% Amber, &lt; 5% Red.</div>', unsafe_allow_html=True)

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

    render_section(
        "Non-Financial Covenants",
        "Illustrative covenant types this area is designed to extend to. No field in the "
        "uploaded financial statement or the reference workbook captures any of these today, "
        "so status is shown as Not Available / Requires Review for every borrower rather than "
        "inferred or fabricated.",
    )
    non_financial_covenants = [
        {
            "Covenant": "Additional Borrowing Restrictions",
            "Description": "Restriction on taking additional debt without lender consent.",
            "Status": "Not Available / Requires Review",
        },
        {
            "Covenant": "Negative Pledge / Lien Restrictions",
            "Description": "Restriction on creating new security interests over assets.",
            "Status": "Not Available / Requires Review",
        },
        {
            "Covenant": "Change of Control",
            "Description": "Lender notification or consent required if ownership/control changes.",
            "Status": "Not Available / Requires Review",
        },
        {
            "Covenant": "Asset Disposal Restrictions",
            "Description": "Restriction on material sale or transfer of assets without approval.",
            "Status": "Not Available / Requires Review",
        },
        {
            "Covenant": "Regulatory / Environmental Compliance",
            "Description": "Requirement to remain compliant with applicable regulatory, "
                            "environmental and operating requirements.",
            "Status": "Not Available / Requires Review",
        },
    ]
    render_html_table(
        non_financial_covenants,
        columns=["Covenant", "Description", "Status"],
        badge_col="Status",
    )
    st.caption(
        "These five are examples of the borrower obligations this area is designed to track, "
        "not a result computed from this borrower's data - no non-financial covenant status "
        "shown here is ever inferred from the uploaded financial statement."
    )

    render_section(
        "Continuous / Early-Warning Monitoring",
        "Early-warning triggers on Revenue, Total Debt and DSCR movement (from Financial "
        "Analysis), plus the covenant breach status above.",
    )

    monitoring_policy = load_monitoring_policy(bundle.workbook_path)
    cm_rows_by_key = {r.key: r for r in fa.income_statement_rows + fa.ratio_rows}

    monitoring = build_continuous_monitoring(
        monitoring_policy,
        cm_rows_by_key["revenue"].pct_change,
        cm_rows_by_key["total_debt"].pct_change,
        cm_rows_by_key["dscr"].pct_change,
        covenant_breach,
    )

    overall_kind = status_kind(monitoring.overall_status)
    st.markdown(
        badge_html(monitoring.overall_status, overall_kind)
        + f'&nbsp;&nbsp;<span style="color:{INK_MUTED};font-size:0.85rem;">Overall Monitoring Status</span>',
        unsafe_allow_html=True,
    )
    st.markdown("<br>", unsafe_allow_html=True)

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

    render_stage_nav(3, "Accept Assessment & Continue →")



# ============================================================
# 5. COLLATERAL ASSESSMENT
# ============================================================

elif selected_area == AREAS[4]:

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

    render_stage_nav(4, "Accept & Continue →")



# ============================================================
# 6. LIMIT SETTING
# ============================================================

elif selected_area == AREAS[5]:

    render_page_header(
        "Limit Setting", crumb,
        "Proposed facility impact and recommended lending limit, using EBIT and reported DSCR "
        "from Input Data directly (the reference workbook bypasses Financial Analysis and Risk "
        "Scoring for this module).",
    )

    limit_policy = load_limit_setting_policy(bundle.workbook_path)

    # build_limit_setting() (frozen) divides by both EBIT and reported
    # DSCR with no None-guard - correct, since the reference workbook it
    # replicates never has a missing input. A genuine OCR extraction can:
    # EBIT is derived from Interest Income (see input_data_engine.py),
    # which this document may not separately report. Checked here,
    # before the call, rather than inside the frozen module - this
    # entire page's math is one interconnected chain with no meaningful
    # partial result once EBIT is unavailable.
    if fa.derived.ebit.current is None or d.reported_dscr.current is None:
        st.info(
            "Limit Setting is not available for this document: it requires EBIT (derived from "
            "Interest Income, which this document does not separately report in the P&L) and "
            "the reported DSCR."
        )
    else:
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

    # FB/NFB assessment is independent of the term-loan/DSCR engine above
    # (it needs none of EBIT/reported DSCR), so it renders whether or not
    # that section was available for this document.
    render_section(
        "Fund-Based (FB) Limit Assessment",
        "Primarily driven by working-capital funding requirement. These figures are "
        "analyst-entered - no working-capital field (Inventory/Receivable/Payable Days) "
        "exists anywhere in the uploaded financial statement or the reference workbook.",
    )
    _ANALYST_ENTERED_CAPTION = "Analyst-entered — no source field in the uploaded financial statement or reference workbook."

    fb_c1, fb_c2, fb_c3, fb_c4, fb_c5 = st.columns(5)
    with fb_c1:
        _persistent_number_input("Annual Sales (₹ Cr)", "fb_annual_sales", min_value=0.0, step=10.0,
                                  help="Defaults to Revenue from Financial Analysis when available - editable.")
        if d is not None and d.revenue.current is not None:
            st.caption("Auto-populated from Revenue (Financial Analysis) — editable.")
        else:
            st.caption("Revenue not available from the uploaded document — enter Annual Sales manually.")
    with fb_c2:
        _persistent_number_input("Inventory Days", "fb_inventory_days", min_value=0.0, step=1.0,
                                  help="Defaults to this document's own disclosed Inventory turnover ratio when available - editable.")
        if d is not None and d.reported_inventory_turnover_days.current is not None:
            st.caption("Auto-populated from Inventory Turnover Ratio (Financial Analysis) — editable.")
        else:
            st.caption(_ANALYST_ENTERED_CAPTION)
    with fb_c3:
        _persistent_number_input("Receivable Days", "fb_receivable_days", min_value=0.0, step=1.0,
                                  help="Defaults to this document's own disclosed Debtors turnover ratio when available - editable.")
        if d is not None and d.reported_debtors_turnover_days.current is not None:
            st.caption("Auto-populated from Debtors Turnover Ratio (Financial Analysis) — editable.")
        else:
            st.caption(_ANALYST_ENTERED_CAPTION)
    with fb_c4:
        _persistent_number_input("Payable Days", "fb_payable_days", min_value=0.0, step=1.0)
        st.caption(_ANALYST_ENTERED_CAPTION)
    with fb_c5:
        _persistent_number_input("Borrower Contribution %", "fb_contribution_pct", min_value=0.0, max_value=100.0, step=1.0)
        st.caption("Analyst-entered — 25% is a starting default, not a computed or policy-sourced value.")

    fb_inputs = FundBasedInputs(
        annual_sales=st.session_state["fb_annual_sales"],
        inventory_days=st.session_state["fb_inventory_days"],
        receivable_days=st.session_state["fb_receivable_days"],
        payable_days=st.session_state["fb_payable_days"],
        contribution_pct=st.session_state["fb_contribution_pct"],
    )
    fb_result = build_fund_based_limit(fb_inputs)
    render_kpi_row([
        kpi_card_html("Net Working-Capital Cycle", fmt_days_or_review(fb_result.nwc_cycle_days), sublabel="Inventory + Receivable − Payable Days"),
        kpi_card_html("Working-Capital Requirement", fmt_money_cr_or_review(fb_result.wc_requirement), sublabel="Annual Sales × Cycle / 365"),
        kpi_card_html("Borrower Contribution", fmt_money_cr_or_review(fb_result.borrower_contribution_amount)),
        kpi_card_html("FB Requirement", fmt_money_cr_or_review(fb_result.fb_requirement), sublabel="WC Requirement − Borrower Contribution"),
    ], columns_per_row=4)

    render_section(
        "Non-Fund-Based (NFB) Limit Assessment",
        "Primarily driven by underlying trade/contractual obligations and expected peak "
        "utilisation. These figures are analyst-entered - no LC/BG or trade-obligation "
        "field exists anywhere in this application's data sources.",
    )
    nfb_c1, nfb_c2, nfb_c3, nfb_c4 = st.columns(4)
    with nfb_c1:
        _persistent_number_input("Annual Imports / Purchases (₹ Cr)", "nfb_annual_imports", min_value=0.0, step=10.0,
                                  help="Context only - not used in the assessed NFB limit below.")
        st.caption(_ANALYST_ENTERED_CAPTION)
    with nfb_c2:
        _persistent_number_input("Peak LC Requirement (₹ Cr)", "nfb_peak_lc", min_value=0.0, step=10.0)
        st.caption(_ANALYST_ENTERED_CAPTION)
    with nfb_c3:
        _persistent_number_input("Contractual / Project Obligations (₹ Cr)", "nfb_contractual_obligations", min_value=0.0, step=10.0,
                                  help="Context only - not used in the assessed NFB limit below.")
        st.caption(_ANALYST_ENTERED_CAPTION)
    with nfb_c4:
        _persistent_number_input("Peak BG Requirement (₹ Cr)", "nfb_peak_bg", min_value=0.0, step=10.0)
        st.caption(_ANALYST_ENTERED_CAPTION)

    nfb_inputs = NonFundBasedInputs(
        annual_imports=st.session_state["nfb_annual_imports"],
        peak_lc_requirement=st.session_state["nfb_peak_lc"],
        contractual_obligations=st.session_state["nfb_contractual_obligations"],
        peak_bg_requirement=st.session_state["nfb_peak_bg"],
    )
    nfb_result = build_non_fund_based_limit(nfb_inputs)
    render_kpi_row([
        kpi_card_html("Letters of Credit (LC)", fmt_money_cr_or_review(st.session_state["nfb_peak_lc"]), sublabel="Peak LC requirement"),
        kpi_card_html("Bank Guarantees (BG)", fmt_money_cr_or_review(st.session_state["nfb_peak_bg"]), sublabel="Peak BG requirement"),
        kpi_card_html("Assessed NFB Limit", fmt_money_cr_or_review(nfb_result.assessed_nfb_limit), sublabel="Peak LC + Peak BG"),
    ], columns_per_row=3)

    render_section("NFB Benchmarking")
    st.markdown(badge_html("Not Available / Requires Review", "neutral"), unsafe_allow_html=True)
    st.caption(
        "Benchmarking the proposed NFB limit against business volumes, existing sanctioned "
        "limits, margin/collateral requirements and utilisation would require sanctioned-"
        "limit and utilisation data this application does not have a source for today - no "
        "score is estimated in its place."
    )

    render_stage_nav(5, "Accept Limits & Continue →")



# ============================================================
# 7. PORTFOLIO MONITORING
# ============================================================

elif selected_area == AREAS[6]:

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

    render_stage_nav(6, "Accept & Continue →")



# ============================================================
# 8. CREDIT MEMO
# ============================================================

elif selected_area == AREAS[7]:

    render_page_header(
        "Credit Memo", crumb,
        "Consolidated status across the review's 6 areas. This module performs no "
        "calculations of its own - every status below is collected from the module that "
        "computed it.",
    )

    cm9_cov_policy = load_covenant_policy(bundle.workbook_path)
    cm9_covenants = build_covenants(fa, cm9_cov_policy)

    cm9_coll_inputs = load_collateral_inputs(bundle.workbook_path)
    cm9_collateral = (
        build_collateral_assessment(cm9_coll_inputs, fa.derived.total_debt.current)
        if cm9_coll_inputs else None
    )

    cm9_limit_policy = load_limit_setting_policy(bundle.workbook_path)
    # Same EBIT-availability guard as the Limit Setting page itself (see
    # AREAS[6] above) - build_limit_setting() (frozen) has no None-guard
    # of its own, and the reference workbook it replicates never has a
    # missing input. A minimal stand-in result (a "Not available" status,
    # matching the collateral_result/portfolio_result None-tolerant
    # pattern already used a few lines above) lets build_credit_memo()
    # (also frozen) read a real string from .conclusion rather than
    # crashing on None, with no change to either frozen module.
    if fa.derived.ebit.current is None or d.reported_dscr.current is None:
        cm9_limit = LimitSettingResult(
            annual_instalment=None, schedule=[], implied_existing_debt_service=None,
            total_proforma_debt_service=None, ebit=None, proforma_dscr=None,
            proforma_dscr_status="Not available", max_serviceable_debt_service=None,
            available_additional_debt_service=None, capacity_cap=None,
            recommended_limit=None, conclusion="Not available",
        )
    else:
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

    # Same analyst-entered FB/NFB inputs as the Limit Setting page (see
    # AREAS[6] above) - read from the same session_state keys so Credit
    # Memo reflects whatever the analyst has entered there, without
    # duplicating or re-deriving any figure.
    cm9_fb_inputs = FundBasedInputs(
        annual_sales=st.session_state["fb_annual_sales"],
        inventory_days=st.session_state["fb_inventory_days"],
        receivable_days=st.session_state["fb_receivable_days"],
        payable_days=st.session_state["fb_payable_days"],
        contribution_pct=st.session_state["fb_contribution_pct"],
    )
    cm9_fb_result = build_fund_based_limit(cm9_fb_inputs)
    cm9_nfb_inputs = NonFundBasedInputs(
        annual_imports=st.session_state["nfb_annual_imports"],
        peak_lc_requirement=st.session_state["nfb_peak_lc"],
        contractual_obligations=st.session_state["nfb_contractual_obligations"],
        peak_bg_requirement=st.session_state["nfb_peak_bg"],
    )
    cm9_nfb_result = build_non_fund_based_limit(cm9_nfb_inputs)

    commentary = load_credit_memo_commentary(bundle.workbook_path)
    memo = build_credit_memo(
        fa, risk, cm9_covenants, cm9_collateral, cm9_limit, cm9_monitoring, cm9_portfolio, commentary,
        fb_result=cm9_fb_result, nfb_result=cm9_nfb_result,
    )

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

    render_stage_nav(7, "Complete Review →")



# ============================================================
# 9. RISK SCORING
#
# Presented last, in its own "Additional Features" section - at the
# director's explicit request for demos, so the main credit-review
# workflow (Data Upload through Credit Memo) can be shown first, with
# Risk Scoring introduced afterward as an additional capability. Moved
# verbatim from its earlier nav position - no calculation, layout, or
# UI inside this block changed, only its own elif index and this
# section comment.
# ============================================================

elif selected_area == AREAS[8]:

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

    # Risk Scoring is the last item in AREAS (index 8, "Additional
    # Features") - nothing in the main workflow comes after it, so its
    # primary button finishes the review and returns to the homepage
    # (Data Upload, index 0) rather than advancing to a non-existent next
    # stage. "<- Previous Step" still returns to Credit Memo, same as any
    # other page.
    render_stage_nav(8, "Finish →", primary_target_index=0)

