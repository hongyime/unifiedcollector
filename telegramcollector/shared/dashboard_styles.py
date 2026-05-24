"""
shared/dashboard_styles.py

Shared CSS injection for all Streamlit dashboards.
Call inject_global_styles() once per page load, immediately after st.set_page_config().
"""
from __future__ import annotations

_CSS = """
<style>
/* ── Base font size ─────────────────────────────────────────────────────── */
html, body, [class*="css"] {
    font-size: 16px !important;
}

/* ── Headers ────────────────────────────────────────────────────────────── */
h1, .stMarkdown h1 { font-size: 1.75rem !important; font-weight: 700 !important; }
h2, .stMarkdown h2 { font-size: 1.4rem  !important; font-weight: 650 !important; }
h3, .stMarkdown h3 { font-size: 1.15rem !important; font-weight: 600 !important; }

/* ── Metric cards — tighter padding ────────────────────────────────────── */
[data-testid="metric-container"] {
    padding-top: 6px    !important;
    padding-bottom: 6px !important;
}
[data-testid="stMetricValue"] {
    font-size: 1.5rem !important;
    font-weight: 600  !important;
}
[data-testid="stMetricLabel"] {
    font-size: 0.9rem !important;
}

/* ── Reduce top padding on main block ──────────────────────────────────── */
.block-container {
    padding-top: 1.5rem    !important;
    padding-bottom: 1rem   !important;
}

/* ── Tighten vertical gap between elements ──────────────────────────────── */
.element-container {
    margin-bottom: 0.4rem !important;
}

/* ── Sidebar ────────────────────────────────────────────────────────────── */
[data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
    font-size: 0.9rem !important;
}

/* ── Dataframe / table text ─────────────────────────────────────────────── */
.stDataFrame, .stDataFrame td, .stDataFrame th {
    font-size: 0.9rem !important;
}

/* ── Tab labels ─────────────────────────────────────────────────────────── */
button[data-baseweb="tab"] {
    font-size: 0.95rem !important;
    font-weight: 500   !important;
}

/* ── Input labels ───────────────────────────────────────────────────────── */
label[data-testid="stWidgetLabel"] {
    font-size: 0.95rem !important;
    font-weight: 500   !important;
}
</style>
"""


def inject_global_styles() -> None:
    """Inject shared CSS into the current Streamlit page.

    Call this once per page load, immediately after st.set_page_config().
    """
    import streamlit as st
    st.markdown(_CSS, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Cross-dashboard navigation
# ---------------------------------------------------------------------------

_SERVICES = [
    {"name": "Collector",         "port": 8501, "icon": "📡"},
    {"name": "Face Recognition",  "port": 8502, "icon": "👤"},
    {"name": "User Intelligence", "port": 8503, "icon": "🧠"},
    {"name": "Link Discovery",    "port": 8504, "icon": "🔗"},
    {"name": "Bulk Sender",       "port": 8505, "icon": "📤"},
]


def render_service_nav(current_service: str) -> None:
    """Render a 'Jump to service' section at the bottom of the sidebar.

    Call this inside a ``with st.sidebar:`` block (or anywhere in the sidebar)
    after your own sidebar content.

    Args:
        current_service: The name of the current service (e.g. "Collector").
                         That entry will be shown as plain text instead of a link.
    """
    import streamlit as st

    st.sidebar.divider()
    st.sidebar.caption("🗂 **Jump to service**")
    for svc in _SERVICES:
        if svc["name"] == current_service:
            st.sidebar.caption(f"{svc['icon']} **{svc['name']}** ← here")
        else:
            st.sidebar.markdown(
                f"{svc['icon']} [**{svc['name']}**](http://localhost:{svc['port']})",
                unsafe_allow_html=False,
            )
    st.sidebar.divider()
    st.sidebar.markdown(
        "[📊 Index (all services)](http://localhost:8500)",
        unsafe_allow_html=False,
    )
