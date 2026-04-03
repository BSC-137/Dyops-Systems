"""
Dyops BASIS GUARD — Streamlit operations dashboard (institutional glass terminal).

Run from this directory:
  streamlit run dashboard.py
"""

from __future__ import annotations

import html
import json
import os
import queue
import threading
import time
from collections import deque
from datetime import timedelta
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st
from loguru import logger

import dyops_core
from binance_feed import resolve_feed_mode, start_binance_feed_thread
from database import PersistenceManager
from sentinel import AUDITS_DIR, DyopsSentinel, MAHALANOBIS_BREACH

# ---------------------------------------------------------------------------
# Glass / institutional terminal theme
# ---------------------------------------------------------------------------
TERMINAL_CSS = """
<style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap');
    html, body, .stApp, [data-testid="stAppViewContainer"], .stMarkdown, .stText,
    input, button, label, [data-baseweb] {
        font-family: 'JetBrains Mono', 'SF Mono', 'Consolas', monospace !important;
    }
    .stApp {
        background: linear-gradient(135deg, #020617 0%, #0f172a 100%) !important;
        color: #f1f5f9;
    }
    [data-testid="stHeader"] { background-color: transparent !important; }
    [data-testid="stToolbar"] { visibility: hidden; height: 0; }
    [data-testid="stAppViewContainer"] > .main { background: transparent; }
    section.main > div { background: transparent; }
    [data-testid="stSidebar"] > div:first-child {
        background: rgba(255, 255, 255, 0.03) !important;
        backdrop-filter: blur(20px);
        -webkit-backdrop-filter: blur(20px);
        border-right: 1px solid rgba(255, 255, 255, 0.1) !important;
    }
    [data-testid="stSidebar"] .block-container { padding-top: 1.25rem; }
    .dyops-sidebar-meta {
        font-size: 0.78rem;
        line-height: 1.45;
        color: #94a3b8;
        padding: 0.65rem 0.85rem;
        margin-bottom: 0.85rem;
        background: rgba(255, 255, 255, 0.05);
        backdrop-filter: blur(14px);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 12px;
    }
    .dyops-sidebar-meta code { font-size: 0.68rem; color: #a5f3fc; word-break: break-all; }
    .dyops-sidebar-meta .k {
        color: #67e8f9; font-size: 0.62rem; letter-spacing: 0.14em; text-transform: uppercase;
    }
    /* High-density neon metric cards */
    .dyops-metric-card {
        background: rgba(255, 255, 255, 0.05);
        backdrop-filter: blur(18px);
        -webkit-backdrop-filter: blur(18px);
        border-radius: 14px;
        border: 1px solid rgba(34, 211, 238, 0.28);
        padding: 0.9rem 1rem;
        margin-bottom: 0.75rem;
        box-shadow:
            0 0 24px rgba(34, 211, 238, 0.12),
            0 8px 32px rgba(0, 0, 0, 0.4);
        transition: border-color 0.35s ease, box-shadow 0.35s ease;
    }
    .dyops-metric-card.dyops-metric-breach {
        border-color: rgba(248, 113, 113, 0.55);
        box-shadow:
            0 0 28px rgba(248, 113, 113, 0.35),
            0 0 48px rgba(239, 68, 68, 0.12),
            0 8px 32px rgba(0, 0, 0, 0.45);
        animation: metric-breach-pulse 2.2s ease-in-out infinite;
    }
    @keyframes metric-breach-pulse {
        0%, 100% { box-shadow: 0 0 22px rgba(248, 113, 113, 0.32), 0 8px 32px rgba(0,0,0,0.45); }
        50% { box-shadow: 0 0 38px rgba(248, 113, 113, 0.48), 0 0 64px rgba(239, 68, 68, 0.15), 0 8px 32px rgba(0,0,0,0.5); }
    }
    .dyops-metric-card .dyops-metric-label {
        font-size: 0.7rem;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: #7dd3fc;
        margin-bottom: 0.35rem;
    }
    .dyops-metric-card .dyops-metric-value {
        font-size: 1.35rem;
        font-weight: 700;
        color: #ffe066;
        text-shadow: 0 0 18px rgba(255, 215, 0, 0.25);
    }
    .dyops-metric-card .dyops-metric-help {
        font-size: 0.68rem;
        color: #64748b;
        margin-top: 0.35rem;
        line-height: 1.35;
    }
    .stMetric {
        background: rgba(255, 255, 255, 0.06);
        backdrop-filter: blur(16px);
        -webkit-backdrop-filter: blur(16px);
        border-radius: 15px;
        border: 1px solid rgba(255, 255, 255, 0.12);
        padding: 15px;
        margin-bottom: 0.65rem;
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.35);
    }
    .stMetric label,
    [data-testid="stMetric"] label {
        color: #7dd3fc !important;
        white-space: normal !important;
        line-height: 1.25 !important;
        font-size: 0.78rem !important;
    }
    .stMetric [data-testid="stMetricValue"],
    [data-testid="stMetric"] [data-testid="stMetricValue"] {
        color: #ffd700 !important;
    }
    .dyops-metric-foot {
        font-size: 0.76rem;
        color: #94a3b8;
        text-align: center;
        margin-top: 0.25rem;
    }
    /* Plotly chart shell */
    [data-testid="stPlotlyChart"] {
        background: rgba(255, 255, 255, 0.06);
        backdrop-filter: blur(18px);
        -webkit-backdrop-filter: blur(18px);
        border: 1px solid rgba(255, 255, 255, 0.12);
        border-radius: 16px;
        box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
        padding: 0.6rem;
        margin-top: 0.35rem;
    }
    /* Header row: title + pulse aligned */
    .dyops-top-bar {
        display: flex;
        flex-direction: row;
        align-items: center;
        justify-content: space-between;
        gap: 1.25rem;
        width: 100%;
        margin-bottom: 0.85rem;
    }
    .dyops-title-block {
        flex: 1 1 auto;
        min-width: 0;
    }
    .dyops-top-bar .pulse-wrap {
        flex: 0 0 auto;
        margin: 0;
    }
    .dyops-header {
        font-family: 'JetBrains Mono', 'Fira Code', 'IBM Plex Mono', monospace;
        font-size: 1.75rem;
        font-weight: 700;
        color: #ffec99;
        letter-spacing: 0.1em;
        text-shadow: 0 0 20px rgba(255, 215, 0, 0.45);
        margin: 0;
        padding: 0.75rem 1rem;
        background: rgba(255, 255, 255, 0.05);
        backdrop-filter: blur(15px);
        border: 1px solid rgba(255, 255, 255, 0.1);
        border-radius: 16px;
        box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
    }
    .dyops-sub {
        color: #67e8f9;
        font-size: 0.72rem;
        letter-spacing: 0.22em;
        margin-top: 0.5rem;
        opacity: 0.95;
    }
    .pulse-wrap {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        gap: 0.65rem;
        color: #e2e8f0;
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.82rem;
        padding: 0.85rem 1.15rem;
        min-height: 52px;
        box-sizing: border-box;
        background: rgba(255, 255, 255, 0.06);
        backdrop-filter: blur(16px);
        -webkit-backdrop-filter: blur(16px);
        border: 1px solid rgba(255, 255, 255, 0.12);
        border-radius: 16px;
        box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
    }
    .pulse-label-live {
        color: #6ee7b7;
        font-weight: 600;
        letter-spacing: 0.18em;
        animation: live-breath 2.4s ease-in-out infinite;
    }
    @keyframes live-breath {
        0%, 100% { opacity: 1; text-shadow: 0 0 10px rgba(110, 231, 183, 0.45); }
        50% { opacity: 0.82; text-shadow: 0 0 22px rgba(52, 211, 153, 0.65), 0 0 32px rgba(16, 185, 129, 0.35); }
    }
    .pulse-dot {
        width: 14px;
        height: 14px;
        border-radius: 50%;
        background: #22c55e;
        animation: breath 2.2s ease-in-out infinite;
    }
    .pulse-dot.stale {
        background: #64748b;
        animation: none;
        box-shadow: none;
    }
    @keyframes breath {
        0%, 100% {
            box-shadow: 0 0 6px #22c55e, 0 0 14px rgba(34, 197, 94, 0.55);
            transform: scale(1);
            opacity: 1;
        }
        50% {
            box-shadow: 0 0 14px #4ade80, 0 0 28px rgba(74, 222, 128, 0.65);
            transform: scale(1.08);
            opacity: 0.92;
        }
    }
    /* Frosted audit notifications */
    .audit-card {
        background: rgba(255, 255, 255, 0.05);
        backdrop-filter: blur(15px);
        -webkit-backdrop-filter: blur(15px);
        border-radius: 16px;
        box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.37);
        padding: 0.9rem 1rem;
        margin-bottom: 0.85rem;
    }
    .audit-card-new { animation: card-in 0.45s ease-out; }
    @keyframes card-in {
        from { opacity: 0; transform: translateY(6px); }
        to { opacity: 1; transform: translateY(0); }
    }
    .audit-risk-low {
        border: 1px solid rgba(34, 211, 238, 0.45);
    }
    .audit-risk-high {
        border: 1px solid rgba(248, 113, 113, 0.55);
        animation: risk-pulse 2.4s ease-in-out infinite;
    }
    @keyframes risk-pulse {
        0%, 100% { box-shadow: 0 0 0 0 rgba(248, 113, 113, 0.35); }
        50% { box-shadow: 0 0 18px 2px rgba(248, 113, 113, 0.25); }
    }
    .risk-pill {
        display: inline-block;
        background: linear-gradient(135deg, #ffd700, #f59e0b);
        color: #020617;
        font-weight: 800;
        padding: 0.2rem 0.55rem;
        border-radius: 8px;
        font-size: 0.8rem;
    }
    .exec-summary {
        color: #cbd5e1;
        font-size: 0.8rem;
        line-height: 1.5;
        margin-top: 0.55rem;
    }
    /* Info / buttons */
    [data-testid="stSidebar"] [data-testid="stAlert"] {
        background: rgba(255, 255, 255, 0.06);
        border: 1px solid rgba(255, 255, 255, 0.12);
        border-radius: 16px;
    }
</style>
"""


def _init_session_state() -> None:
    if st.session_state.get("_dyops_ready"):
        return
    st.session_state._dyops_ready = True
    st.session_state.telemetry_queue: queue.Queue[tuple[float, float, float]] = queue.Queue()
    st.session_state.stop_feed = threading.Event()
    st.session_state.seen_audit_files: set[str] = set()
    st.session_state.pulse_ts = 0.0
    st.session_state.events_processed = 0
    st.session_state.in_breach = False

    db_path = os.environ.get("DYOPS_SQLITE_PATH")
    st.session_state.persistence = PersistenceManager(db_path)

    rows = st.session_state.persistence.load_recent_events(500)
    observer = dyops_core.BasisObserver(
        name="basis-guard-ui",
        theta=1.0,
        ring_buffer_capacity=1000,
    )
    hist: deque[dict[str, float | bool]] = deque(maxlen=500)
    for row in rows:
        h = observer.update(
            float(row["timestamp"]),
            float(row["physical_price"]),
            float(row["token_price"]),
        )
        hist.append(
            {
                "t": float(row["timestamp"]),
                "basis": h.filtered_basis,
                "innovation": h.innovation,
                "valid": h.measurement_valid,
            }
        )
    st.session_state.history = hist
    st.session_state.sentinel = DyopsSentinel(
        observer,
        auditor=None,
        persistence=st.session_state.persistence,
    )
    st.session_state.feed_thread = None


def _ensure_binance_feed() -> None:
    t = st.session_state.feed_thread
    if t is not None and t.is_alive():
        return
    st.session_state.stop_feed.clear()
    st.session_state.feed_thread = start_binance_feed_thread(
        st.session_state.telemetry_queue,
        st.session_state.stop_feed,
        mode=resolve_feed_mode(),
    )


def _drain_telemetry(max_pull: int = 80) -> int:
    q = st.session_state.telemetry_queue
    sentinel: DyopsSentinel = st.session_state.sentinel
    n = 0
    while n < max_pull:
        try:
            ts, phys, tok = q.get_nowait()
        except queue.Empty:
            break
        res = sentinel.process_event(ts, phys, tok)
        h = res.health
        st.session_state.in_breach = bool(
            h.measurement_valid and h.mahalanobis_distance > MAHALANOBIS_BREACH
        )
        st.session_state.history.append(
            {
                "t": ts,
                "basis": h.filtered_basis,
                "innovation": h.innovation,
                "valid": h.measurement_valid,
            }
        )
        st.session_state.events_processed += 1
        n += 1
    if n:
        st.session_state.pulse_ts = time.time()
    return n


def _parse_risk_level(risk: object) -> str:
    try:
        if isinstance(risk, (int, float)):
            v = int(risk)
        else:
            v = int(float(str(risk).strip()))
    except (ValueError, TypeError):
        return "low"
    return "high" if v >= 60 else "low"


def _build_figure() -> go.Figure:
    hist = list(st.session_state.history)
    if not hist:
        fig = go.Figure()
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#e2e8f0"),
            title=dict(
                text="Waiting for telemetry events…",
                font=dict(color="#67e8f9", size=14),
            ),
            height=520,
        )
        return fig

    t_axis = [row["t"] for row in hist]
    basis = [row["basis"] for row in hist]
    innov = [row["innovation"] for row in hist]

    fig = go.Figure()
    # Filtered basis — layered filament glow (gold)
    for width, alpha in ((14, 0.07), (10, 0.11), (6, 0.18), (3, 0.28)):
        fig.add_trace(
            go.Scatter(
                x=t_axis,
                y=basis,
                mode="lines",
                name="_glow_basis",
                line=dict(color=f"rgba(255, 215, 0, {alpha})", width=width),
                hoverinfo="skip",
                showlegend=False,
            )
        )
    fig.add_trace(
        go.Scatter(
            x=t_axis,
            y=basis,
            mode="lines",
            name="Filtered basis",
            line=dict(color="#ffec99", width=2.2),
            hovertemplate="time=%{x:.3f}<br>filtered basis=%{y:.5f}<extra></extra>",
        )
    )
    for width, alpha in ((8, 0.12), (4, 0.22)):
        fig.add_trace(
            go.Scatter(
                x=t_axis,
                y=innov,
                mode="lines",
                name="_glow_innov",
                line=dict(color=f"rgba(34, 211, 238, {alpha})", width=width),
                hoverinfo="skip",
                showlegend=False,
                yaxis="y2",
            )
        )
    fig.add_trace(
        go.Scatter(
            x=t_axis,
            y=innov,
            mode="lines",
            name="Innovation",
            line=dict(color="#67e8f9", width=2, dash="dash"),
            yaxis="y2",
            hovertemplate="time=%{x:.3f}<br>innov=%{y:.6f}<extra></extra>",
        )
    )
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="JetBrains Mono, monospace", color="#f1f5f9", size=11),
        title=dict(
            text="Last 500 events — Filtered basis vs innovation (Binance feed)",
            font=dict(color="#ffec99", size=15),
            x=0.0,
            xanchor="left",
            y=0.97,
            yanchor="top",
        ),
        margin=dict(l=58, r=58, t=56, b=76),
        hovermode="x unified",
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.2,
            xanchor="center",
            x=0.5,
            font=dict(color="#e2e8f0", size=11),
            bgcolor="rgba(0,0,0,0)",
        ),
        xaxis=dict(
            gridcolor="rgba(255,255,255,0.1)",
            zerolinecolor="rgba(255,255,255,0.22)",
            title=dict(text="Unix time (s)", font=dict(color="#94a3b8")),
        ),
        yaxis=dict(
            gridcolor="rgba(255,215,0,0.11)",
            zerolinecolor="rgba(255,255,255,0.2)",
            title=dict(text="Filtered basis (log-ratio)", font=dict(color="#ffec99")),
        ),
        yaxis2=dict(
            overlaying="y",
            side="right",
            showgrid=False,
            title=dict(text="Innovation", font=dict(color="#22d3ee")),
        ),
        height=520,
    )
    return fig


def _load_audit_files() -> list[tuple[Path, float]]:
    audit_dir = Path(AUDITS_DIR)
    if not audit_dir.is_dir():
        return []
    files = sorted(
        audit_dir.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [(p, p.stat().st_mtime) for p in files[:20]]


def _audit_cards_html() -> str:
    rows = _load_audit_files()
    if not rows:
        return (
            '<p style="color:#94a3b8;font-size:0.85rem;padding:0.5rem;">'
            "No audit JSON yet.</p>"
        )

    seen: set[str] = st.session_state.seen_audit_files
    new_names: list[str] = []
    chunks: list[str] = []

    for path, _mtime in rows:
        key = path.name
        is_new = key not in seen
        if is_new:
            new_names.append(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            chunks.append(
                '<div class="audit-card audit-risk-high"><span style="color:#f87171;">'
                f"{html.escape(path.name)} (invalid JSON)</span></div>"
            )
            continue

        g = data.get("gemini") or {}
        risk = g.get("risk_score", "—")
        risk_tier = _parse_risk_level(risk)
        risk_class = "audit-risk-high" if risk_tier == "high" else "audit-risk-low"
        raw_summary = (
            g.get("executive_summary")
            or g.get("mitigation_strategy")
            or g.get("cause")
            or "—"
        )
        summary = html.escape(str(raw_summary))
        cause_raw = g.get("cause", "")
        cause_esc = html.escape(str(cause_raw)) if cause_raw else ""
        new_cls = " audit-card-new" if is_new else ""
        card_cls = f"audit-card {risk_class}{new_cls}"
        cause_html = (
            f"<div class='exec-summary' style='opacity:0.85'>Cause: {cause_esc}</div>"
            if cause_esc
            else ""
        )
        fname = html.escape(path.name)
        chunks.append(
            f'<div class="{card_cls}">'
            f'<div style="color:#67e8f9;font-size:0.68rem;letter-spacing:0.08em;">'
            f"GEMINI AUDIT · {fname}</div>"
            f'<div style="margin-top:0.4rem;"><span class="risk-pill">'
            f"RISK {html.escape(str(risk))}</span></div>"
            f'<div class="exec-summary"><b style="color:#ffec99;">Executive summary</b><br/>{summary}</div>'
            f"{cause_html}</div>"
        )

    for k in new_names:
        seen.add(k)

    return "\n".join(chunks)


@st.fragment(run_every=timedelta(seconds=1))
def _live() -> None:
    """Sidebar intelligence feed only — must be invoked inside ``with st.sidebar:``."""
    st.markdown(_audit_cards_html(), unsafe_allow_html=True)


@st.fragment(run_every=timedelta(seconds=1))
def _main_dashboard_fragment() -> None:
    """Cockpit: header, pulse, chart + metrics columns."""
    _ensure_binance_feed()
    drained = _drain_telemetry()

    alive = time.time() - st.session_state.pulse_ts < 12.0
    dot_class = "pulse-dot" if alive else "pulse-dot stale"
    pulse_label_html = (
        '<span class="pulse-label-live">LIVE</span>'
        if alive
        else '<span>STALE</span>'
    )

    st.markdown(
        '<div class="dyops-top-bar">'
        '<div class="dyops-title-block">'
        '<p class="dyops-header">DYOPS SYSTEMS | BASIS GUARD V1.0</p>'
        '<p class="dyops-sub">INSTITUTIONAL TERMINAL · OPERATIONAL BETA</p>'
        '</div>'
        f'<div class="pulse-wrap"><span>Telemetry heartbeat</span>'
        f'<span class="{dot_class}"></span>{pulse_label_html}</div>'
        "</div>",
        unsafe_allow_html=True,
    )

    breach_cls = (
        "dyops-metric-card dyops-metric-breach"
        if st.session_state.get("in_breach")
        else "dyops-metric-card"
    )

    chart_col, metrics_col = st.columns([3.2, 0.95], gap="large")
    with chart_col:
        st.plotly_chart(_build_figure(), width="stretch")
    with metrics_col:
        st.markdown(
            f'<div class="{breach_cls}">'
            '<div class="dyops-metric-label">Global telemetry</div>'
            f'<div class="dyops-metric-value">{len(st.session_state.history):,}</div>'
            '<div class="dyops-metric-help">Rolling chart depth (SQLite replay on load '
            f"+ live feed; {st.session_state.events_processed:,} ticks this session)."
            "</div></div>",
            unsafe_allow_html=True,
        )
        st.markdown(
            f'<div class="{breach_cls}">'
            '<div class="dyops-metric-label">Ingress queue</div>'
            f'<div class="dyops-metric-value">{st.session_state.telemetry_queue.qsize():,}</div>'
            '<div class="dyops-metric-help">WebSocket → dashboard buffer '
            "(non-blocking).</div></div>",
            unsafe_allow_html=True,
        )
        foot = (
            f"+{drained} ticks / refresh"
            if drained
            else "—"
        )
        st.markdown(
            f'<p class="dyops-metric-foot">{html.escape(foot)}</p>',
            unsafe_allow_html=True,
        )


def _clear_history() -> None:
    st.session_state.history.clear()
    st.session_state.events_processed = 0
    st.session_state.in_breach = False
    observer = dyops_core.BasisObserver(
        name="basis-guard-ui",
        theta=1.0,
        ring_buffer_capacity=1000,
    )
    st.session_state.sentinel = DyopsSentinel(
        observer,
        auditor=None,
        persistence=st.session_state.persistence,
    )


def run_dashboard() -> None:
    logger.remove()
    logger.add(lambda _msg: None, level="DEBUG")

    st.set_page_config(
        page_title="Dyops | Basis Guard",
        page_icon="◆",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _init_session_state()
    st.markdown(TERMINAL_CSS, unsafe_allow_html=True)

    with st.sidebar:
        mode = resolve_feed_mode()
        feed_label = "USDC/USDT (stable basis)" if mode == "stable" else "ETH · stETH (LST basis)"
        dbp = html.escape(str(st.session_state.persistence.db_path.resolve()))
        p = html.escape(str(Path(AUDITS_DIR).resolve()))
        st.markdown(
            f'<div class="dyops-sidebar-meta"><span class="k">Binance feed</span><br/>'
            f"<code>{html.escape(feed_label)}</code><br/>"
            f'<span class="k" style="display:inline-block;margin-top:0.5rem;">SQLite</span><br/>'
            f"<code>{dbp}</code><br/>"
            f'<span class="k" style="display:inline-block;margin-top:0.5rem;">Audit directory</span><br/>'
            f"<code>{p}</code></div>",
            unsafe_allow_html=True,
        )
        if st.button("Clear History", width="stretch", key="btn_clear"):
            _clear_history()
        if st.button(
            "Export Compliance Report",
            width="stretch",
            type="primary",
            key="btn_export",
        ):
            st.session_state["_export_placeholder"] = True
        if st.session_state.pop("_export_placeholder", False):
            st.info(
                "**Export Compliance Report** — _placeholder_. "
                "Pipeline: SOX-style PDF / signed bundle + regulator schema (Q2)."
            )
        st.divider()
        st.markdown("### Intelligence feed")
        _live()

    _main_dashboard_fragment()


if __name__ == "__main__":
    run_dashboard()
