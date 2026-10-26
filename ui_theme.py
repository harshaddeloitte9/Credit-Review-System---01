"""
Presentation layer only: CSS theme, KPI cards, badges, HTML tables and
Plotly chart builders for the Credit Review Tool dashboard.

Nothing here performs a financial calculation or reads the reference
workbook. Every function takes plain values (numbers, strings, lists) that
the caller has already pulled from reference_data.py / input_data_engine.py
/ financial_analysis.py / risk_scoring.py, and renders them. Keeping this
split means the validated calculation modules never need to change for a
styling or layout update.

Colors follow the project's validated reference palette: a single sequential
blue for magnitude comparisons, a light/dark blue pair for "prior vs current
period" bars (one hue, two shades - not two unrelated categorical colors),
and a fixed status palette (good/warning/serious/critical/neutral, plus
processing - the existing BLUE accent, for an in-progress pipeline stage)
for badges, risk bands and stage indicators - never re-purposed for
anything else.
"""

import streamlit as st
import plotly.graph_objects as go

FONT_STACK = "-apple-system, 'Segoe UI', system-ui, sans-serif"

INK_PRIMARY = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"
BORDER = "rgba(11,11,11,0.10)"
SURFACE = "#ffffff"
SURFACE_PAGE = "#f7f7f6"
SIDEBAR_BG = "#0f172a"
SIDEBAR_TEXT = "#cbd5e1"
SIDEBAR_MUTED = "#64748b"

BLUE = "#2a78d6"          # sequential accent / "current period"
BLUE_LIGHT = "#9ec5f4"    # same hue, lighter step / "prior period"
BLUE_DARK = "#184f95"
BLUE_WASH = "rgba(42,120,214,0.12)"

STATUS = {
    "good": {"bg": "#0ca30c", "text": "#ffffff", "tint": "#e3f6e3"},
    "warning": {"bg": "#fab219", "text": "#0b0b0b", "tint": "#fef3d9"},
    "serious": {"bg": "#ec835a", "text": "#0b0b0b", "tint": "#fbe8e0"},
    "critical": {"bg": "#d03b3b", "text": "#ffffff", "tint": "#fbe4e4"},
    "neutral": {"bg": "#e1e0d9", "text": "#52514e", "tint": "#f2f1ee"},
    # In-progress pipeline stage (Data Upload status). Reuses the
    # existing BLUE accent (topbar gradient / active-nav highlight) - not
    # a new color introduced to the palette.
    "processing": {"bg": BLUE, "text": "#ffffff", "tint": BLUE_WASH},
}

# Rounded-grade -> status band. Matches the reference workbook's own
# ROUND(score,0) grade lookup (risk_scoring.py) - this only assigns a
# display color to an already-computed grade, it does not change it.
RISK_BAND_STATUS = {5: "good", 4: "good", 3: "warning", 2: "serious", 1: "critical"}

_STATUS_TEXT_KIND = {
    "Pass": "good", "PASS": "good", "PASS - ties": "good", "Approve": "good", "MATCH": "good",
    "GREEN": "good", "Low Risk": "good", "Compliant": "good",
    "Fail": "critical", "FAIL": "critical", "Reject": "critical", "DISCREPANCY": "critical",
    "NOT PASS": "critical", "RED": "critical", "High Risk": "critical", "Breach": "critical",
    "FLAG FOR REVIEW": "warning", "CHECK": "warning", "WATCH LIST": "warning",
    "AMBER": "warning", "Moderate Risk": "warning", "Warning / Review Required": "warning",
    "NOT COMPARABLE": "neutral",
    # AI-assisted validation (Phase 2B) - content-judgment statuses.
    "CONFIRMED": "good", "YEAR_MISMATCH": "warning", "FIELD_MISMATCH": "critical",
    "VALUE_SUSPICIOUS": "warning", "NOT_FOUND_IN_SOURCE": "neutral",
    "INSUFFICIENT_CONTEXT": "neutral",
    # AI-assisted validation (Phase 2B) - technical/execution statuses.
    "AI_COMPLETED": "good", "AI_SKIPPED": "neutral", "AI_UNAVAILABLE": "neutral",
    "AI_ERROR": "critical",
    # AI correction proposals (Phase 3A) - content statuses.
    "PROPOSAL_AVAILABLE": "warning", "PROPOSAL_UNVERIFIED": "neutral",
    "PROPOSAL_REJECTED_BY_EVIDENCE": "critical", "NO_PROPOSAL": "neutral",
    # Deterministic extraction statuses, shown on the Data Review page.
    "OK": "good", "NOT_FOUND": "neutral", "REVIEW_REQUIRED": "warning",
    "Review Required": "warning", "Not Found": "neutral",
    "HUMAN_APPROVED_CORRECTION": "good",
    # AI dynamic field mapping (this phase) - content statuses.
    "AI_MAPPED": "warning", "MAPPED_AND_VERIFIED": "warning",
    "MAPPED_BUT_REJECTED": "critical", "NO_CANDIDATE_FOUND": "neutral",
    "AI_ROW_SELECTED_VALUES_UNRESOLVED": "neutral",
}


def inject_theme():
    st.markdown(
        f"""
        <style>
        html, body, [class*="css"] {{
            font-family: {FONT_STACK};
        }}
        .stApp {{
            background: {SURFACE_PAGE};
        }}

        /* ---------------- Sidebar shell ---------------- */
        [data-testid="stSidebar"] {{
            background: {SIDEBAR_BG};
        }}
        [data-testid="stSidebar"] * {{
            color: {SIDEBAR_TEXT};
        }}
        [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {{
            gap: 0.35rem;
        }}

        .cr-brand {{
            display: flex;
            align-items: center;
            gap: 0.65rem;
            margin: 0.2rem 0 1.1rem 0;
        }}
        .cr-brand-badge {{
            width: 34px;
            height: 34px;
            border-radius: 9px;
            background: {BLUE};
            color: #ffffff;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 800;
            font-size: 0.92rem;
            flex-shrink: 0;
        }}
        .cr-brand-name {{
            color: #ffffff;
            font-weight: 700;
            font-size: 1.0rem;
            line-height: 1.2;
        }}
        .cr-brand-tagline {{
            color: {SIDEBAR_MUTED};
            font-size: 0.72rem;
        }}

        .cr-sidebar-status {{
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.10);
            border-radius: 10px;
            padding: 0.8rem 0.9rem;
            margin-bottom: 1.1rem;
        }}
        .cr-sidebar-status-eyebrow {{
            color: {SIDEBAR_MUTED};
            font-size: 0.64rem;
            font-weight: 700;
            letter-spacing: 0.09em;
            text-transform: uppercase;
        }}
        .cr-sidebar-status-name {{
            color: #ffffff;
            font-weight: 700;
            font-size: 0.94rem;
            margin-top: 0.3rem;
            line-height: 1.3;
        }}
        .cr-sidebar-status-meta {{
            color: {SIDEBAR_MUTED};
            font-size: 0.76rem;
            margin-top: 0.1rem;
        }}
        .cr-sidebar-status-row {{
            display: flex;
            align-items: center;
            gap: 0.55rem;
            margin-top: 0.6rem;
        }}
        .cr-sidebar-status-score {{
            color: #ffffff;
            font-weight: 700;
            font-size: 0.86rem;
        }}

        /* ---------------- Sidebar nav (single radio, CSS-grouped) ---------------- */
        [data-testid="stSidebar"] div[role="radiogroup"] {{
            gap: 0.08rem;
        }}
        [data-testid="stSidebar"] div[role="radiogroup"] label {{
            border-radius: 8px;
            padding: 0.42rem 0.6rem;
            width: 100%;
            transition: background 0.12s ease;
        }}
        [data-testid="stSidebar"] div[role="radiogroup"] label:hover {{
            background: rgba(255,255,255,0.07);
        }}
        [data-testid="stSidebar"] div[role="radiogroup"] label > div:first-child {{
            display: none;
        }}
        [data-testid="stSidebar"] div[role="radiogroup"] label p {{
            font-size: 0.89rem;
            color: {SIDEBAR_TEXT};
        }}
        [data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) {{
            background: {BLUE};
        }}
        [data-testid="stSidebar"] div[role="radiogroup"] label:has(input:checked) p {{
            color: #ffffff;
            font-weight: 600;
        }}
        [data-testid="stSidebar"] div[role="radiogroup"] label:nth-of-type(3) {{
            margin-top: 0.6rem;
        }}
        [data-testid="stSidebar"] div[role="radiogroup"] label:nth-of-type(3)::before {{
            content: "Credit Analysis";
            display: block;
            font-size: 0.66rem;
            font-weight: 700;
            letter-spacing: 0.09em;
            text-transform: uppercase;
            color: {SIDEBAR_MUTED};
            margin: 0.4rem 0 0.45rem 0.55rem;
        }}
        [data-testid="stSidebar"] div[role="radiogroup"] label:nth-of-type(7) {{
            margin-top: 1rem;
            padding-top: 1rem;
            border-top: 1px solid rgba(255,255,255,0.10);
        }}
        [data-testid="stSidebar"] div[role="radiogroup"] label:nth-of-type(7)::before {{
            content: "Decisioning & Monitoring";
            display: block;
            font-size: 0.66rem;
            font-weight: 700;
            letter-spacing: 0.09em;
            text-transform: uppercase;
            color: {SIDEBAR_MUTED};
            margin: 0 0 0.45rem 0.55rem;
        }}
        [data-testid="stSidebar"] div[role="radiogroup"] label:nth-of-type(10) {{
            margin-top: 1rem;
            padding-top: 1rem;
            border-top: 1px solid rgba(255,255,255,0.10);
        }}
        [data-testid="stSidebar"] div[role="radiogroup"] label:nth-of-type(10)::before {{
            content: "Additional Features";
            display: block;
            font-size: 0.66rem;
            font-weight: 700;
            letter-spacing: 0.09em;
            text-transform: uppercase;
            color: {SIDEBAR_MUTED};
            margin: 0 0 0.45rem 0.55rem;
        }}

        /* ---------------- Page chrome ---------------- */
        .cr-topbar {{
            height: 4px;
            width: 100%;
            background: linear-gradient(90deg, {BLUE_DARK} 0%, {BLUE} 55%, {BLUE_LIGHT} 100%);
            border-radius: 999px;
            margin-bottom: 1.35rem;
        }}
        .cr-header {{
            border-bottom: 1px solid {BORDER};
            padding-bottom: 1rem;
            margin-bottom: 1.4rem;
        }}
        .cr-header-crumb {{
            font-size: 0.8rem;
            color: {INK_MUTED};
            font-weight: 600;
            letter-spacing: 0.01em;
            margin-bottom: 0.3rem;
        }}
        .cr-header-title {{
            font-size: 1.7rem;
            font-weight: 800;
            color: {INK_PRIMARY};
            line-height: 1.2;
            letter-spacing: -0.01em;
        }}
        .cr-header-subtitle {{
            font-size: 0.94rem;
            color: {INK_SECONDARY};
            margin-top: 0.3rem;
        }}
        .cr-section-title {{
            font-size: 1.08rem;
            font-weight: 700;
            color: {INK_PRIMARY};
            margin: 1.5rem 0 0.2rem 0;
        }}
        .cr-section-sub {{
            font-size: 0.85rem;
            color: {INK_MUTED};
            margin-bottom: 0.65rem;
        }}
        .cr-chart-caption {{
            font-size: 0.84rem;
            font-weight: 700;
            color: {INK_SECONDARY};
            margin-bottom: 0.3rem;
        }}

        /* ---------------- KPI cards ---------------- */
        .cr-kpi {{
            background: {SURFACE};
            border: 1px solid {BORDER};
            border-radius: 12px;
            padding: 0.95rem 1.05rem;
            box-shadow: 0 1px 2px rgba(11,11,11,0.04);
            height: 100%;
        }}
        .cr-kpi-top {{
            display: flex;
            align-items: center;
            gap: 0.45rem;
        }}
        .cr-kpi-icon {{
            width: 19px;
            height: 19px;
            border-radius: 999px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 0.66rem;
            font-weight: 800;
            flex-shrink: 0;
        }}
        .cr-kpi-label {{
            font-size: 0.76rem;
            color: {INK_MUTED};
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }}
        .cr-kpi-value {{
            font-size: 1.55rem;
            font-weight: 800;
            color: {INK_PRIMARY};
            margin-top: 0.2rem;
            line-height: 1.15;
            letter-spacing: -0.01em;
        }}
        .cr-kpi-delta {{
            font-size: 0.82rem;
            font-weight: 700;
            margin-top: 0.35rem;
        }}
        .cr-kpi-delta-good {{ color: #006300; }}
        .cr-kpi-delta-bad {{ color: #b02a2a; }}
        .cr-kpi-delta-flat {{ color: {INK_MUTED}; }}
        .cr-kpi-sub {{
            font-size: 0.76rem;
            color: {INK_MUTED};
            margin-top: 0.2rem;
        }}

        /* ---------------- Generic cards / badges ---------------- */
        .cr-card {{
            background: {SURFACE};
            border: 1px solid {BORDER};
            border-radius: 12px;
            padding: 1.1rem 1.25rem;
            box-shadow: 0 1px 2px rgba(11,11,11,0.04);
        }}
        .cr-badge {{
            display: inline-block;
            padding: 0.16rem 0.62rem;
            border-radius: 999px;
            font-size: 0.76rem;
            font-weight: 700;
            white-space: nowrap;
        }}

        /* ---------------- Tables ---------------- */
        .cr-table-wrap {{
            overflow-x: auto;
            border: 1px solid {BORDER};
            border-radius: 10px;
            margin-bottom: 1rem;
        }}
        table.cr-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.85rem;
        }}
        table.cr-table th {{
            text-align: left;
            background: {SURFACE_PAGE};
            color: {INK_SECONDARY};
            font-weight: 700;
            font-size: 0.74rem;
            text-transform: uppercase;
            letter-spacing: 0.03em;
            padding: 0.6rem 0.8rem;
            border-bottom: 1px solid {BORDER};
            white-space: normal;
        }}
        table.cr-table td {{
            padding: 0.55rem 0.8rem;
            border-bottom: 1px solid {GRIDLINE};
            color: {INK_PRIMARY};
            vertical-align: middle;
            max-width: 280px;
            word-break: break-word;
        }}
        table.cr-table tr:last-child td {{ border-bottom: none; }}
        table.cr-table tr:hover td {{ background: {SURFACE_PAGE}; }}
        table.cr-table td.cr-num, table.cr-table th.cr-num {{
            text-align: right;
            font-variant-numeric: tabular-nums;
        }}

        /* ---------------- Risk grade card + scale ---------------- */
        .cr-grade-card {{
            border-radius: 14px;
            padding: 1.5rem 1.6rem;
            text-align: center;
            height: 100%;
            display: flex;
            flex-direction: column;
            justify-content: center;
        }}
        .cr-grade-label {{
            font-size: 0.86rem;
            font-weight: 800;
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }}
        .cr-grade-score {{
            font-size: 2.5rem;
            font-weight: 800;
            color: {INK_PRIMARY};
            margin-top: 0.25rem;
            letter-spacing: -0.02em;
        }}
        .cr-scale-row {{
            display: flex;
            gap: 0.6rem;
            flex-wrap: wrap;
        }}
        .cr-scale-card {{
            flex: 1 1 150px;
            border-radius: 10px;
            padding: 0.85rem 0.7rem;
            text-align: center;
        }}
        .cr-scale-score {{
            font-size: 1.35rem;
            font-weight: 800;
        }}
        .cr-scale-definition {{
            font-size: 0.82rem;
            font-weight: 700;
            color: {INK_PRIMARY};
            margin-top: 0.15rem;
        }}
        .cr-scale-interpretation {{
            font-size: 0.72rem;
            color: {INK_MUTED};
            margin-top: 0.15rem;
        }}
        .cr-scale-marker {{
            font-size: 0.64rem;
            font-weight: 800;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            margin-top: 0.4rem;
        }}

        /* ---------------- Financial health summary ---------------- */
        .cr-health-row {{
            display: flex;
            gap: 0.65rem;
            align-items: flex-start;
            padding: 0.6rem 0;
            border-bottom: 1px solid {GRIDLINE};
        }}
        .cr-health-row:last-child {{ border-bottom: none; }}
        .cr-health-dot {{
            width: 8px;
            height: 8px;
            border-radius: 999px;
            margin-top: 0.42rem;
            flex-shrink: 0;
        }}
        .cr-health-text {{
            font-size: 0.92rem;
            color: {INK_PRIMARY};
            line-height: 1.5;
        }}

        /* ---------------- Under-development state ---------------- */
        .cr-underdev {{
            background: {SURFACE};
            border: 1px dashed {BORDER};
            border-radius: 14px;
            padding: 2.75rem 1.5rem;
            text-align: center;
            margin-top: 1rem;
        }}
        .cr-underdev-badge {{
            display: inline-block;
            background: {STATUS["warning"]["tint"]};
            color: #7a5200;
            padding: 0.28rem 0.95rem;
            border-radius: 999px;
            font-weight: 700;
            font-size: 0.76rem;
            letter-spacing: 0.06em;
            text-transform: uppercase;
        }}
        .cr-underdev-title {{
            font-size: 1.2rem;
            font-weight: 700;
            color: {INK_PRIMARY};
            margin-top: 0.75rem;
        }}
        .cr-underdev-sub {{
            font-size: 0.88rem;
            color: {INK_MUTED};
            margin-top: 0.35rem;
            max-width: 520px;
            margin-left: auto;
            margin-right: auto;
        }}

        /* ---------------- Data Upload: document cards & flow ---------------- */
        .cr-doc-icon {{
            width: 44px;
            height: 44px;
            border-radius: 10px;
            background: {SURFACE_PAGE};
            border: 1px solid {BORDER};
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: 800;
            font-size: 0.68rem;
            color: {INK_SECONDARY};
            flex-shrink: 0;
        }}
        /* Wide screens: one aligned horizontal row, every step the same
           width (flex-basis 0 -> grow equally regardless of label
           length), arrows as fixed-width connectors so spacing stays
           consistent. Narrow screens (see media query below): stacks
           vertically with the connector rotated into a down-arrow,
           rather than wrapping mid-row (which would otherwise strand an
           arrow at the end of one line, disconnected from its step). */
        .cr-flow-row {{
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 0;
            flex-wrap: nowrap;
            background: {SURFACE};
            border: 1px solid {BORDER};
            border-radius: 12px;
            padding: 0.85rem 1.1rem;
            margin-bottom: 0.6rem;
        }}
        .cr-flow-step {{
            flex: 1 1 0;
            min-width: 0;
            text-align: center;
            padding: 0 0.5rem;
        }}
        .cr-flow-dot {{
            width: 10px;
            height: 10px;
            border-radius: 999px;
            margin: 0 auto 0.4rem auto;
        }}
        .cr-flow-title {{
            font-weight: 700;
            font-size: 0.88rem;
            color: {INK_PRIMARY};
            line-height: 1.25;
        }}
        .cr-flow-sub {{
            font-size: 0.76rem;
            color: {INK_MUTED};
            margin-top: 0.15rem;
            line-height: 1.25;
        }}
        .cr-flow-arrow {{
            font-size: 1.3rem;
            color: {INK_MUTED};
            flex: 0 0 auto;
            padding: 0 0.5rem;
        }}
        @media (max-width: 820px) {{
            .cr-flow-row {{
                flex-direction: column;
                align-items: stretch;
                padding: 1rem 1.2rem;
            }}
            .cr-flow-step {{
                width: 100%;
                padding: 0.3rem 0;
            }}
            .cr-flow-arrow {{
                transform: rotate(90deg);
                padding: 0.2rem 0;
            }}
        }}

        div[data-testid="stExpander"] {{
            border: 1px solid {BORDER};
            border-radius: 10px;
        }}

        /* ---------------- Guided-workflow stage buttons ---------------- */
        .stButton > button {{
            border-radius: 8px;
            font-weight: 700;
            font-size: 0.88rem;
            padding: 0.55rem 1.1rem;
            transition: background 0.12s ease, border-color 0.12s ease, color 0.12s ease;
        }}
        .stButton > button[kind="primary"] {{
            background: {BLUE};
            border: 1px solid {BLUE};
            color: #ffffff;
        }}
        .stButton > button[kind="primary"]:hover {{
            background: {BLUE_DARK};
            border-color: {BLUE_DARK};
            color: #ffffff;
        }}
        .stButton > button[kind="primary"]:active {{
            background: {BLUE_DARK};
            border-color: {BLUE_DARK};
        }}
        .stButton > button[kind="primary"]:disabled {{
            background: {GRIDLINE};
            border-color: {GRIDLINE};
            color: {INK_MUTED};
        }}
        .stButton > button[kind="secondary"] {{
            background: {SURFACE};
            border: 1px solid {BORDER};
            color: {INK_SECONDARY};
        }}
        .stButton > button[kind="secondary"]:hover {{
            background: {SURFACE_PAGE};
            border-color: {INK_MUTED};
            color: {INK_PRIMARY};
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def status_kind(status_text):
    return _STATUS_TEXT_KIND.get(status_text, "neutral")


def badge_html(text, kind=None, title=None):
    """title, if given, becomes a native HTML tooltip (no JS) - used to
    keep a compact/relabelled badge's own precise underlying status
    reachable on hover/focus rather than only the shortened label."""
    if text is None:
        text = "—"
    kind = kind or status_kind(text)
    colors = STATUS.get(kind, STATUS["neutral"])
    title_attr = f' title="{title}"' if title else ""
    return f'<span class="cr-badge" style="background:{colors["bg"]};color:{colors["text"]}"{title_attr}>{text}</span>'


def render_topbar():
    st.markdown('<div class="cr-topbar"></div>', unsafe_allow_html=True)


def render_sidebar_brand(initials, name, tagline):
    st.sidebar.markdown(
        f"""
        <div class="cr-brand">
            <div class="cr-brand-badge">{initials}</div>
            <div>
                <div class="cr-brand-name">{name}</div>
                <div class="cr-brand-tagline">{tagline}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_sidebar_status(name, meta, status_text, kind, score_text):
    accent = STATUS[kind]["bg"]
    st.sidebar.markdown(
        f"""
        <div class="cr-sidebar-status">
            <div class="cr-sidebar-status-eyebrow">Active Borrower</div>
            <div class="cr-sidebar-status-name">{name}</div>
            <div class="cr-sidebar-status-meta">{meta}</div>
            <div class="cr-sidebar-status-row">
                <span class="cr-badge" style="background:{accent};color:#ffffff;">{status_text}</span>
                <span class="cr-sidebar-status-score">{score_text}</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_page_header(title, crumb, subtitle=None):
    sub_html = f'<div class="cr-header-subtitle">{subtitle}</div>' if subtitle else ""
    st.markdown(
        f"""
        <div class="cr-header">
            <div class="cr-header-crumb">{crumb}</div>
            <div class="cr-header-title">{title}</div>
            {sub_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_section(title, subtitle=None):
    sub_html = f'<div class="cr-section-sub">{subtitle}</div>' if subtitle else ""
    st.markdown(f'<div class="cr-section-title">{title}</div>{sub_html}', unsafe_allow_html=True)


def chart_caption(text):
    st.markdown(f'<div class="cr-chart-caption">{text}</div>', unsafe_allow_html=True)


def kpi_card_html(label, value, delta_text=None, delta_kind="flat", sublabel=None, icon=None, icon_kind="neutral"):
    icon_html = ""
    top_label = f'<div class="cr-kpi-label">{label}</div>'
    if icon:
        colors = STATUS.get(icon_kind, STATUS["neutral"])
        icon_html = f'<span class="cr-kpi-icon" style="background:{colors["bg"]};color:{colors["text"]};">{icon}</span>'
        top_label = f'<div class="cr-kpi-top">{icon_html}{top_label}</div>'
    delta_html = ""
    if delta_text:
        delta_html = f'<div class="cr-kpi-delta cr-kpi-delta-{delta_kind}">{delta_text}</div>'
    sub_html = f'<div class="cr-kpi-sub">{sublabel}</div>' if sublabel else ""
    return (
        f'<div class="cr-kpi">'
        f"{top_label}"
        f'<div class="cr-kpi-value">{value}</div>'
        f"{delta_html}{sub_html}"
        f"</div>"
    )


def render_kpi_row(cards, columns_per_row=3):
    for start in range(0, len(cards), columns_per_row):
        row = cards[start:start + columns_per_row]
        cols = st.columns(columns_per_row)
        for col, card in zip(cols, row):
            with col:
                st.markdown(card, unsafe_allow_html=True)


def render_html_table(rows, columns, badge_col=None, numeric_cols=None, col_widths=None):
    """col_widths: optional {column_name: css_width} (e.g. "26%" or
    "220px") - a per-column width HINT for browsers under the table's
    normal 'auto' layout, so a couple of columns (e.g. one holding a
    longer description) can be allocated more room than compact count/
    status columns without forcing every other project table to adopt
    a rigid fixed layout."""
    numeric_cols = numeric_cols or []
    col_widths = col_widths or {}
    thead = "".join(
        f'<th class="{"cr-num" if c in numeric_cols else ""}"'
        f'{f" style=\"width:{col_widths[c]}\"" if c in col_widths else ""}>{c}</th>'
        for c in columns
    )
    body = []
    for row in rows:
        cells = []
        for c in columns:
            val = row.get(c, "")
            width_attr = f' style="width:{col_widths[c]}"' if c in col_widths else ""
            if c == badge_col:
                cells.append(f"<td{width_attr}>{badge_html(val)}</td>")
            else:
                cls = "cr-num" if c in numeric_cols else ""
                cells.append(f'<td class="{cls}"{width_attr}>{val}</td>')
        body.append(f"<tr>{''.join(cells)}</tr>")
    html = (
        '<div class="cr-table-wrap"><table class="cr-table">'
        f"<thead><tr>{thead}</tr></thead><tbody>{''.join(body)}</tbody>"
        "</table></div>"
    )
    st.markdown(html, unsafe_allow_html=True)


def progress_bar_html(value, max_value=5, color=None):
    color = color or BLUE
    pct = max(0.0, min(100.0, value / max_value * 100))
    return (
        f'<div style="background:{GRIDLINE};border-radius:6px;height:8px;width:100%;min-width:80px;">'
        f'<div style="background:{color};border-radius:6px;height:8px;width:{pct:.1f}%;"></div>'
        f"</div>"
    )


def render_grade_card(grade_label, score_text, kind):
    tint = STATUS[kind]["tint"]
    accent = STATUS[kind]["bg"]
    st.markdown(
        f"""
        <div class="cr-grade-card" style="background:{tint};border:1px solid {accent}55;">
            <span class="cr-grade-label" style="color:{accent};">{grade_label}</span>
            <div class="cr-grade-score">{score_text}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_grade_scale_cards(scale, current_score):
    cards_html = []
    for g in scale:
        kind = RISK_BAND_STATUS.get(g["score"], "neutral")
        colors = STATUS[kind]
        is_current = g["score"] == current_score
        border = f'2px solid {colors["bg"]}' if is_current else f"1px solid {BORDER}"
        bg = colors["tint"] if is_current else SURFACE
        marker = (
            f'<div class="cr-scale-marker" style="color:{colors["bg"]};">◄ Current</div>'
            if is_current else ""
        )
        cards_html.append(
            f'<div class="cr-scale-card" style="border:{border};background:{bg};">'
            f'<div class="cr-scale-score" style="color:{colors["bg"]};">{g["score"]}</div>'
            f'<div class="cr-scale-definition">{g["definition"]}</div>'
            f'<div class="cr-scale-interpretation">{g["risk_interpretation"]}</div>'
            f"{marker}"
            f"</div>"
        )
    st.markdown(f'<div class="cr-scale-row">{"".join(cards_html)}</div>', unsafe_allow_html=True)


def render_health_summary(items):
    """items: list of (text, sentiment) tuples, sentiment in good/warning/critical/neutral."""
    rows_html = []
    for text, sentiment in items:
        colors = STATUS.get(sentiment, STATUS["neutral"])
        rows_html.append(
            '<div class="cr-health-row">'
            f'<div class="cr-health-dot" style="background:{colors["bg"]};"></div>'
            f'<div class="cr-health-text">{text}</div>'
            "</div>"
        )
    st.markdown(f'<div class="cr-card">{"".join(rows_html)}</div>', unsafe_allow_html=True)


def render_validation_summary(rows):
    """Aggregates already-computed validation.ValidationRow.status values into
    three headline counts (matched / discrepancies / not comparable) shown as
    KPI cards above the detailed table. Pure display aggregation - the counts
    themselves are read straight off validation.py's own output, nothing here
    re-derives a match/mismatch decision."""
    total = len(rows)
    discrepancies = sum(1 for r in rows if r.status == "DISCREPANCY")
    not_comparable = sum(1 for r in rows if r.status.startswith("NOT COMPARABLE"))
    matches = total - discrepancies - not_comparable

    cards = [
        kpi_card_html(
            "Matched", f"{matches} / {total}",
            icon="✓" if discrepancies == 0 else "!",
            icon_kind="good" if discrepancies == 0 else "critical",
        ),
        kpi_card_html(
            "Discrepancies", str(discrepancies),
            icon="✓" if discrepancies == 0 else "!",
            icon_kind="good" if discrepancies == 0 else "critical",
        ),
    ]
    if not_comparable:
        cards.append(kpi_card_html(
            "Not Comparable", str(not_comparable),
            icon="–", icon_kind="neutral",
            sublabel="No source data in Input Data - not a discrepancy",
        ))
    render_kpi_row(cards, columns_per_row=3)
    return matches, discrepancies, not_comparable


def render_flow_steps(steps):
    """steps: list of (title, subtitle, status_kind) tuples, rendered as a
    compact horizontal flow with arrow connectors - e.g. Source Document
    -> Extraction -> Extracted Data. A status strip, not a chart."""
    parts = []
    for i, (title, subtitle, kind) in enumerate(steps):
        colors = STATUS.get(kind, STATUS["neutral"])
        if i > 0:
            parts.append('<div class="cr-flow-arrow">→</div>')
        parts.append(
            '<div class="cr-flow-step">'
            f'<div class="cr-flow-dot" style="background:{colors["bg"]};"></div>'
            f'<div class="cr-flow-title">{title}</div>'
            f'<div class="cr-flow-sub">{subtitle}</div>'
            "</div>"
        )
    st.markdown(f'<div class="cr-flow-row">{"".join(parts)}</div>', unsafe_allow_html=True)


def render_underdev(area_label, sheet_hint):
    st.markdown(
        f"""
        <div class="cr-underdev">
            <span class="cr-underdev-badge">Under Development</span>
            <div class="cr-underdev-title">{area_label}</div>
            <div class="cr-underdev-sub">
                This module maps to '{sheet_hint}' in the reference workbook and is not yet
                implemented in this application. No placeholder figures, charts or scores are
                shown here.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _base_layout(fig, height, legend=True):
    fig.update_layout(
        height=height,
        plot_bgcolor=SURFACE,
        paper_bgcolor=SURFACE,
        font=dict(family=FONT_STACK, color=INK_SECONDARY, size=12.5),
        margin=dict(l=8, r=8, t=44 if legend else 22, b=8),
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, font=dict(size=12)),
        bargap=0.35,
        bargroupgap=0.18,
        hoverlabel=dict(bgcolor=SURFACE, bordercolor=BORDER, font=dict(family=FONT_STACK, color=INK_PRIMARY, size=12)),
    )
    fig.update_xaxes(showline=False, ticks="", tickfont=dict(color=INK_MUTED, size=12))
    fig.update_yaxes(showline=False, ticks="", tickfont=dict(color=INK_MUTED, size=12))
    return fig


def _safe_fmt(value_fmt, v):
    # A metric can be legitimately unavailable (e.g. an OCR extraction
    # that could not reliably read a source figure) rather than always
    # populated the way the reference workbook's own static data always
    # is. None is passed through as the bar's y-value unchanged - Plotly
    # renders that as a genuinely absent bar, not a fabricated zero -
    # only the printed text label needs its own safe formatting.
    return value_fmt.format(v) if v is not None else "N/A"


def fy_comparison_bar(categories, current_values, previous_values, current_label, previous_label,
                       value_fmt="{:,.1f}", height=320, y_tickformat=None):
    fig = go.Figure()
    fig.add_trace(go.Bar(
        name=previous_label, x=categories, y=previous_values,
        marker_color=BLUE_LIGHT,
        text=[_safe_fmt(value_fmt, v) for v in previous_values],
        textposition="outside",
        textfont=dict(color=INK_SECONDARY, size=11),
        cliponaxis=False,
    ))
    fig.add_trace(go.Bar(
        name=current_label, x=categories, y=current_values,
        marker_color=BLUE,
        text=[_safe_fmt(value_fmt, v) for v in current_values],
        textposition="outside",
        textfont=dict(color=INK_PRIMARY, size=11),
        cliponaxis=False,
    ))
    fig.update_layout(barmode="group")
    fig.update_yaxes(gridcolor=GRIDLINE, zeroline=True, zerolinecolor=GRIDLINE, zerolinewidth=1,
                      tickformat=y_tickformat)
    fig.update_xaxes(showgrid=False)
    return _base_layout(fig, height)


def magnitude_bar(categories, values, value_fmt="{:,.2f}", height=320, sort_desc=True):
    pairs = list(zip(categories, values))
    if sort_desc:
        pairs.sort(key=lambda p: (p[1] is not None, p[1]), reverse=True)
    cats = [p[0] for p in pairs]
    vals = [p[1] for p in pairs]
    fig = go.Figure(go.Bar(
        x=vals, y=cats, orientation="h",
        marker_color=BLUE,
        text=[_safe_fmt(value_fmt, v) for v in vals],
        textposition="outside",
        textfont=dict(color=INK_PRIMARY, size=11.5),
        cliponaxis=False,
    ))
    fig.update_yaxes(autorange="reversed", showgrid=False)
    fig.update_xaxes(gridcolor=GRIDLINE, zeroline=True, zerolinecolor=GRIDLINE, zerolinewidth=1)
    return _base_layout(fig, height, legend=False)


def capital_structure_bar(debt, equity, debt_label="Total Debt", equity_label="Total Equity", height=150):
    fig = go.Figure()
    total = debt + equity
    fig.add_trace(go.Bar(
        y=["FY current"], x=[debt], orientation="h", name=debt_label,
        marker_color=BLUE,
        text=[f"{debt_label} {debt / total * 100:.0f}%"], textposition="inside",
        insidetextanchor="middle", textfont=dict(color="#ffffff", size=12),
    ))
    fig.add_trace(go.Bar(
        y=["FY current"], x=[equity], orientation="h", name=equity_label,
        marker_color=BLUE_LIGHT,
        text=[f"{equity_label} {equity / total * 100:.0f}%"], textposition="inside",
        insidetextanchor="middle", textfont=dict(color=INK_PRIMARY, size=12),
    ))
    fig.update_layout(barmode="stack", showlegend=False)
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return _base_layout(fig, height, legend=False)


def radar_chart(categories, values, max_value=5, height=380):
    cats = categories + [categories[0]]
    vals = values + [values[0]]
    fig = go.Figure(go.Scatterpolar(
        r=vals, theta=cats, fill="toself",
        fillcolor=BLUE_WASH,
        line=dict(color=BLUE, width=2),
        marker=dict(size=6, color=BLUE),
    ))
    fig.update_layout(
        polar=dict(
            radialaxis=dict(visible=True, range=[0, max_value], gridcolor=GRIDLINE, tickfont=dict(size=10, color=INK_MUTED)),
            angularaxis=dict(gridcolor=GRIDLINE, tickfont=dict(size=11, color=INK_SECONDARY)),
            bgcolor=SURFACE,
        ),
    )
    return _base_layout(fig, height, legend=False)


def score_meter(score, min_value=1, max_value=5, band_edges=(1.5, 2.5, 3.5, 4.5), height=130):
    """Bullet-style meter: a same-scale banded track with the current score
    marked - the 'polished horizontal score/band visualization' fallback to
    a circular gauge, per choosing-a-form.md ('a single ratio against a
    limit -> Meter, same-ramp track')."""
    lo, hi = min_value, max_value
    edges = [lo] + list(band_edges) + [hi]
    band_kinds = ["critical", "serious", "warning", "good"]
    steps = [
        {"range": [edges[i], edges[i + 1]], "color": STATUS[band_kinds[i]]["bg"]}
        for i in range(len(band_kinds))
    ]
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=score,
        number={"valueformat": ".2f", "font": {"size": 24, "color": INK_PRIMARY}, "suffix": " / 5.00"},
        gauge={
            "shape": "bullet",
            "axis": {"range": [lo, hi], "tickvals": [lo, *band_edges, hi], "tickfont": {"size": 10, "color": INK_MUTED}},
            "bar": {"color": INK_PRIMARY, "thickness": 0.5},
            "steps": steps,
            "threshold": {
                "line": {"color": INK_PRIMARY, "width": 3},
                "thickness": 0.9,
                "value": score,
            },
        },
        domain={"x": [0.02, 0.98], "y": [0.15, 0.85]},
    ))
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=8, b=8),
        paper_bgcolor=SURFACE,
        font=dict(family=FONT_STACK, color=INK_SECONDARY, size=12),
    )
    return fig
