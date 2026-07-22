"""
Dyops API — FastAPI backend: Binance feed → DyopsSentinel → SQLite + WebSocket fan-out.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import queue
import secrets
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from backend import webhooks

# dyops_core package (sentinel, database, binance_feed) lives alongside this repo folder
_DYOPS_PY = Path(__file__).resolve().parent.parent / "dyops_core"
if str(_DYOPS_PY) not in sys.path:
    sys.path.insert(0, str(_DYOPS_PY))

import dyops_core  # noqa: E402
from loguru import logger  # noqa: E402
from binance_feed import (  # noqa: E402
    start_instrument_feed_threads,
    start_offline_feed_threads,
)
from database import PersistenceManager, REPLAY_WINDOW_EVENTS  # noqa: E402
from instruments import InstrumentConfig, load_instruments  # noqa: E402
from scenarios import get_scenario  # noqa: E402
from sentinel import (  # noqa: E402
    AUDIT_COOLDOWN_TICKS,
    AUDITS_DIR,
    AgenticAuditor,
    CRITICALITY_AUDIT_PCT,
    CRITICALITY_WINDOW_EVENTS,
    DyopsSentinel,
    EventResult,
    MAHALANOBIS_BREACH,
)

_HISTORY_SUMMARY_MAX = 200
_HISTORY_EXPLAIN_MAX = 280
_PULSE_SUMMARY_MAX = 200
_PULSE_EXPLAIN_MAX = 280
STALE_CUTOFF_SEC = 12.0
_INGEST_LOG_INTERVAL_SEC = 5.0


def _clip_text(s: str, max_len: int) -> str:
    t = s.strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1].rstrip() + "…"


def _reasoning_row(
    measurement_valid: bool,
    mahalanobis_distance: float,
    breach_threshold: float,
) -> str:
    """Deterministic copy for replay rows; never LLM / Gemini."""
    if not measurement_valid:
        return (
            "Measurement withheld: invalid or non-positive prices; observer did not "
            "apply this tick. Deterministic statistical reasoning defers the update."
        )
    m = mahalanobis_distance
    if not math.isfinite(m):
        return "Mahalanobis distance undefined for this step; no breach assessment applied."
    if m > breach_threshold:
        pct_above = (m - breach_threshold) / breach_threshold * 100.0
        return (
            f"Mahalanobis distance at {m:.2f} normalized units "
            f"({pct_above:.0f}% above sentinel threshold). "
            "Correlation fracture detected."
        )
    if m == 0.0:
        return (
            "Deterministic statistical reasoning: innovation within model band for this step "
            f"(Mahalanobis {m:.2f}; threshold {breach_threshold:.2f})."
        )
    return (
        "Deterministic statistical reasoning: Mahalanobis distance within sentinel norm; "
        f"current {m:.4f} at or below threshold {breach_threshold:.2f}."
    )


def _replay_history_events(
    rows: list[dict[str, Any]],
) -> tuple[list["HistoryPoint"], list["HistoryTracePoint"]]:
    observer = dyops_core.BasisObserver(
        name="dyops-api-replay",
        theta=1.0,
        ring_buffer_capacity=1000,
    )
    plain_out: list[HistoryPoint] = []
    trace_out: list[HistoryTracePoint] = []
    thresh = float(MAHALANOBIS_BREACH)
    for row in rows:
        ts = float(row["timestamp"])
        phys = float(row["physical_price"])
        tok = float(row["token_price"])
        measured = (
            math.log(phys / tok)
            if phys > 0.0 and tok > 0.0 and math.isfinite(phys) and math.isfinite(tok)
            else float("nan")
        )
        h = observer.update(ts, phys, tok)
        reasoning = _reasoning_row(
            h.measurement_valid,
            h.mahalanobis_distance,
            thresh,
        )
        hp = HistoryPoint(
            instrument_id=str(row.get("instrument_id") or "default"),
            ingestion_source=str(row.get("ingestion_source") or "live"),
            scenario=(
                str(row["scenario"])
                if row.get("scenario") is not None
                else None
            ),
            t=ts,
            measured_basis=float(measured),
            filtered_basis=h.filtered_basis,
            innovation=h.innovation,
            mahalanobis=h.mahalanobis_distance,
            valid=h.measurement_valid,
        )
        plain_out.append(hp)
        trace_out.append(
            HistoryTracePoint(**hp.model_dump(), reasoning=reasoning),
        )
    return plain_out, trace_out


def _trace_window_copy(points: list["HistoryTracePoint"]) -> tuple[str, str]:
    if not points:
        summary = "Replay trace: empty window."
        explain = (
            "Load persisted ticks to reproduce the observer path with row-level "
            "deterministic reasoning (Mahalanobis vs sentinel threshold; no LLM)."
        )
        return _clip_text(summary, _HISTORY_SUMMARY_MAX), _clip_text(
            explain, _HISTORY_EXPLAIN_MAX
        )
    n = len(points)
    breaches = sum(
        1
        for p in points
        if p.valid and p.mahalanobis > float(MAHALANOBIS_BREACH)
    )
    summary = (
        f"Replay trace: {n} ticks; {breaches} breach moments "
        f"(valid measurement, Mahalanobis > {MAHALANOBIS_BREACH})."
    )
    explain = (
        "SQLite replay reproduces the Kalman observer; each point includes statistical "
        "reasoning from Mahalanobis distance and measurement validity. Gemini is not used here."
    )
    return _clip_text(summary, _HISTORY_SUMMARY_MAX), _clip_text(
        explain, _HISTORY_EXPLAIN_MAX
    )


def _pulse_narrative(
    *,
    live: bool,
    age_sec: float | None,
    session: int,
    total: int,
) -> tuple[str, str]:
    age_s = (
        f"{age_sec:.1f}s"
        if age_sec is not None and math.isfinite(age_sec)
        else "n/a"
    )
    if live:
        summary = (
            f"LIVE · last tick {age_s} ago · session {session} events · "
            f"{total} persisted in SQLite."
        )
        explain = (
            "Feed is current; streamed Mahalanobis and innovation reflect live "
            "deterministic observer updates for operational monitoring."
        )
    else:
        summary = (
            f"STALE · last tick age {age_s} (cutoff {STALE_CUTOFF_SEC:g}s) · "
            f"session {session} · "
            f"{total} events on record."
        )
        explain = (
            "Inbound data may be interrupted; filter state will not advance until the "
            "feed resumes—treat live indicators as potentially stale."
        )
    return _clip_text(summary, _PULSE_SUMMARY_MAX), _clip_text(
        explain, _PULSE_EXPLAIN_MAX
    )


def _event_result_model(er: EventResult) -> dict[str, Any]:
    h = er.health
    snap = er.snapshot
    return {
        "level": er.level.name,
        "level_value": int(er.level),
        "health": {
            "filtered_basis": h.filtered_basis,
            "innovation": h.innovation,
            "mahalanobis_distance": h.mahalanobis_distance,
            "measurement_valid": h.measurement_valid,
            "breach": bool(
                h.measurement_valid and h.mahalanobis_distance > MAHALANOBIS_BREACH
            ),
        },
        "snapshot": snap,
        "criticality_recent_pct": er.criticality_recent_pct,
    }


class ConnectionHub:
    def __init__(self) -> None:
        self._telemetry: dict[WebSocket, str | None] = {}
        self._audits: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def register_telemetry(
        self,
        ws: WebSocket,
        instrument_id: str | None = None,
    ) -> None:
        async with self._lock:
            self._telemetry[ws] = instrument_id

    async def unregister_telemetry(self, ws: WebSocket) -> None:
        async with self._lock:
            self._telemetry.pop(ws, None)

    async def register_audits(self, ws: WebSocket) -> None:
        async with self._lock:
            self._audits.add(ws)

    async def unregister_audits(self, ws: WebSocket) -> None:
        async with self._lock:
            self._audits.discard(ws)

    async def broadcast_telemetry(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(
            {"type": "telemetry", "payload": payload},
            allow_nan=False,
            default=str,
        )
        async with self._lock:
            dead: list[WebSocket] = []
            for ws, instrument_id in self._telemetry.items():
                if (
                    instrument_id is not None
                    and instrument_id != payload.get("instrument_id")
                ):
                    continue
                try:
                    await ws.send_text(raw)
                except Exception:  # noqa: BLE001
                    dead.append(ws)
            for ws in dead:
                self._telemetry.pop(ws, None)

    async def broadcast_audit(self, payload: dict[str, Any]) -> None:
        raw = json.dumps(
            {"type": "audit", "payload": payload},
            allow_nan=False,
            default=str,
        )
        async with self._lock:
            dead: list[WebSocket] = []
            for ws in self._audits:
                try:
                    await ws.send_text(raw)
                except Exception:  # noqa: BLE001
                    dead.append(ws)
            for ws in dead:
                self._audits.discard(ws)


hub = ConnectionHub()
_stop_binance = threading.Event()
_telemetry_queue: queue.Queue = queue.Queue()
_demo_telemetry_queue: queue.Queue = queue.Queue()
_binance_threads: list[threading.Thread] = []
_persistence: PersistenceManager | None = None
_sentinel: DyopsSentinel | None = None
_session_event_count: int = 0
_last_tick_monotonic: float = 0.0
_dropped_tick_count: int = 0
_processing_error_count: int = 0
_last_ingest_log_at: dict[str, float] = {}
_demo_injection_active = False
_demo_resetting = False
_webhook_tasks: set[asyncio.Task[None]] = set()
_instrument_configs: tuple[InstrumentConfig, ...] = ()
_primary_instrument_id = "default"


@dataclass
class InstrumentRuntime:
    config: InstrumentConfig
    sentinel: DyopsSentinel
    session_event_count: int = 0
    last_tick_monotonic: float = 0.0
    level: str = "MONITORING"
    last_mahalanobis: float | None = None
    criticality_recent_pct: float = 0.0
    ingestion_source: str = "none"


_instrument_runtimes: dict[str, InstrumentRuntime] = {}


def _log_ingestion_issue(kind: str, **context: Any) -> None:
    """Rate-limit noisy feed failures while preserving structured context and counters."""
    now = time.monotonic()
    last = _last_ingest_log_at.get(kind, 0.0)
    if now - last < _INGEST_LOG_INTERVAL_SEC:
        return
    _last_ingest_log_at[kind] = now
    logger.bind(issue=kind, **context).warning(
        "Telemetry ingestion issue: {issue} | {context}",
        issue=kind,
        context=context,
    )


def _replay_observer_state(
    persistence: PersistenceManager,
    instrument_id: str = "default",
) -> dyops_core.BasisObserver:
    observer = dyops_core.BasisObserver(
        name=f"dyops-api-{instrument_id}",
        theta=1.0,
        ring_buffer_capacity=1000,
    )
    rows = persistence.load_recent_events(
        REPLAY_WINDOW_EVENTS,
        instrument_id=instrument_id,
    )
    for row in rows:
        observer.update(
            float(row["timestamp"]),
            float(row["physical_price"]),
            float(row["token_price"]),
        )
    return observer


def _on_startup_sync() -> dict[str, InstrumentRuntime]:
    global _persistence, _sentinel, _session_event_count, _binance_threads
    global _instrument_configs, _instrument_runtimes, _primary_instrument_id
    global _dropped_tick_count, _processing_error_count, _demo_injection_active
    global _demo_resetting
    db_path = os.environ.get("DYOPS_SQLITE_PATH")
    _persistence = PersistenceManager(db_path)
    _instrument_configs = load_instruments()
    _primary_instrument_id = _instrument_configs[0].id
    auditor = _try_create_auditor(_persistence)
    _instrument_runtimes = {}
    for config in _instrument_configs:
        observer = _replay_observer_state(_persistence, config.id)
        sentinel = DyopsSentinel(
            observer,
            auditor=auditor,
            persistence=_persistence,
            instrument_id=config.id,
        )
        _instrument_runtimes[config.id] = InstrumentRuntime(config, sentinel)
    _session_event_count = 0
    _dropped_tick_count = 0
    _processing_error_count = 0
    _last_ingest_log_at.clear()
    _demo_injection_active = False
    _demo_resetting = False
    _sentinel = _instrument_runtimes[_primary_instrument_id].sentinel
    _stop_binance.clear()
    feed_specs = tuple(
        (
            config.id,
            config.feed_mode,
            config.physical_symbol,
            config.token_symbol,
        )
        for config in _instrument_configs
    )
    if os.environ.get("DYOPS_OFFLINE_MODE") == "1":
        interval = float(os.environ.get("DYOPS_OFFLINE_INTERVAL_SEC", "0.25"))
        _binance_threads = start_offline_feed_threads(
            _telemetry_queue,
            _stop_binance,
            feed_specs,
            interval_sec=max(0.05, interval),
        )
    else:
        _binance_threads = start_instrument_feed_threads(
            _telemetry_queue,
            _stop_binance,
            feed_specs,
        )
    return _instrument_runtimes


def _try_create_auditor(persistence: PersistenceManager) -> AgenticAuditor | None:
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        return None
    try:
        return AgenticAuditor(persistence=persistence, audits_dir=AUDITS_DIR)
    except (ImportError, ValueError):
        return None


def _on_shutdown_sync() -> None:
    global _binance_threads
    _stop_binance.set()
    if _persistence is not None:
        _persistence.close()
    _binance_threads = []


async def _send_escalation_webhook(
    model: dict[str, Any],
    *,
    session_event_count: int,
) -> None:
    total = session_event_count
    if _persistence is not None:
        try:
            total = await asyncio.to_thread(
                _persistence.count_events,
                str(model["instrument_id"]),
            )
        except Exception:  # noqa: BLE001
            pass
    ingestion_source = str(model["ingestion_source"])
    if ingestion_source == "live":
        summary, explainability = _pulse_narrative(
            live=True,
            age_sec=0.0,
            session=session_event_count,
            total=total,
        )
    else:
        label = "SCENARIO" if ingestion_source == "demo" else "OFFLINE"
        summary = _clip_text(
            f"SIMULATED {label} · session {session_event_count} events · "
            f"{total} persisted in SQLite.",
            _PULSE_SUMMARY_MAX,
        )
        explainability = _clip_text(
            "Deterministic simulated telemetry exercised the production observer and "
            "sentinel path; this payload is not market evidence.",
            _PULSE_EXPLAIN_MAX,
        )
    health = model["health"]
    payload: dict[str, Any] = {
        "timestamp": model["timestamp"],
        "level": model["level"],
        "mahalanobis": health["mahalanobis_distance"],
        "innovation": health["innovation"],
        "criticality_recent_pct": model["criticality_recent_pct"],
        "instrument_id": model["instrument_id"],
        "ingestion_source": model["ingestion_source"],
        "summary": summary,
        "explainability": explainability,
    }
    if model.get("event_id") is not None:
        payload["event_id"] = model["event_id"]
    await webhooks.send_webhooks(payload)


def _schedule_escalation_webhook(
    model: dict[str, Any],
    *,
    session_event_count: int,
) -> None:
    if not webhooks.configured_urls():
        return
    task = asyncio.create_task(
        _send_escalation_webhook(
            model,
            session_event_count=session_event_count,
        )
    )
    _webhook_tasks.add(task)
    task.add_done_callback(_webhook_tasks.discard)


async def _telemetry_pump() -> None:
    global _last_tick_monotonic, _session_event_count
    global _dropped_tick_count, _processing_error_count, _demo_injection_active
    assert _instrument_runtimes
    while True:
        if _demo_resetting:
            await asyncio.sleep(0.01)
            continue
        is_demo = False
        ingestion_source = "live"
        delay_after = 0.0
        demo_scenario: str | None = None
        demo_last = False
        try:
            demo_item = _demo_telemetry_queue.get_nowait()
            if len(demo_item) == 7:
                (
                    instrument_id,
                    ts,
                    phys,
                    tok,
                    delay_after,
                    demo_scenario,
                    demo_last,
                ) = demo_item
            elif len(demo_item) == 5:
                instrument_id, ts, phys, tok, delay_after = demo_item
            else:
                ts, phys, tok, delay_after = demo_item
                instrument_id = _primary_instrument_id
            is_demo = True
            ingestion_source = "demo"
        except queue.Empty:
            try:
                item = _telemetry_queue.get_nowait()
                if len(item) == 5:
                    instrument_id, ts, phys, tok, ingestion_source = item
                elif len(item) == 4:
                    instrument_id, ts, phys, tok = item
                else:
                    ts, phys, tok = item
                    instrument_id = _primary_instrument_id
            except queue.Empty:
                await asyncio.sleep(0.01)
                continue
        runtime = _instrument_runtimes.get(str(instrument_id))
        if runtime is None:
            _dropped_tick_count += 1
            _log_ingestion_issue(
                "unknown_instrument",
                instrument_id=str(instrument_id),
                dropped_tick_count=_dropped_tick_count,
            )
            if is_demo and demo_last:
                _demo_injection_active = False
            continue
        try:
            result = runtime.sentinel.process_event(
                ts,
                phys,
                tok,
                schedule_background_audit=not is_demo,
                ingestion_source=ingestion_source,
                scenario=demo_scenario,
            )
        except Exception as exc:  # noqa: BLE001
            _processing_error_count += 1
            _log_ingestion_issue(
                "process_event_error",
                instrument_id=runtime.config.id,
                timestamp=ts,
                error=repr(exc),
                processing_error_count=_processing_error_count,
            )
            if is_demo and demo_last:
                _demo_injection_active = False
            await asyncio.sleep(0.001)
            continue
        runtime.last_tick_monotonic = time.monotonic()
        runtime.session_event_count += 1
        runtime.level = result.level.name
        runtime.criticality_recent_pct = result.criticality_recent_pct
        runtime.ingestion_source = ingestion_source
        mahalanobis = float(result.health.mahalanobis_distance)
        runtime.last_mahalanobis = mahalanobis if math.isfinite(mahalanobis) else None
        if runtime.config.id == _primary_instrument_id:
            _last_tick_monotonic = runtime.last_tick_monotonic
            _session_event_count = runtime.session_event_count
        model = _event_result_model(result)
        model["instrument_id"] = runtime.config.id
        model["timestamp"] = ts
        model["physical_price"] = phys
        model["token_price"] = tok
        model["session_event_index"] = runtime.session_event_count
        model["ingestion_source"] = ingestion_source
        if demo_scenario is not None:
            model["demo_scenario"] = demo_scenario
        simulated_webhooks = os.environ.get("DYOPS_DEMO_WEBHOOKS") == "1"
        webhook_allowed = ingestion_source == "live" or simulated_webhooks
        if webhook_allowed and (
            result.level.name == "BREACH" or result.snapshot is not None
        ):
            _schedule_escalation_webhook(
                model,
                session_event_count=runtime.session_event_count,
            )
        await hub.broadcast_telemetry(model)
        if delay_after > 0.0:
            await asyncio.sleep(delay_after)
        if is_demo and demo_last:
            _demo_injection_active = False


async def _audit_poll_loop() -> None:
    assert _persistence is not None
    last_id = _persistence.get_max_audit_id()
    while True:
        await asyncio.sleep(0.35)
        if _persistence is None:
            continue
        batch = _persistence.load_audits_after(last_id, limit=20)
        for row in batch:
            last_id = max(last_id, row["id"])
            await hub.broadcast_audit(row)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _on_startup_sync()
    pump = asyncio.create_task(_telemetry_pump())
    audit_loop = asyncio.create_task(_audit_poll_loop())
    yield
    pump.cancel()
    audit_loop.cancel()
    try:
        await pump
    except asyncio.CancelledError:
        pass
    try:
        await audit_loop
    except asyncio.CancelledError:
        pass
    _on_shutdown_sync()


app = FastAPI(
    title="Dyops API",
    version="1.0.0",
    description=(
        "High-fidelity telemetry API for monitoring digital asset basis risk and peg stability."
    ),
    lifespan=lifespan,
)

_cors_origins = os.environ.get(
    "DYOPS_CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _cors_origins if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class StatusResponse(BaseModel):
    gemini_configured: bool
    webhook_configured: bool
    binance_feed: str
    audits_dir: str
    db_path: str
    global_events_total_sqlite: int
    mahalanobis_breach_threshold: float
    criticality_window_events: int
    criticality_audit_pct: float
    audit_cooldown_ticks: int
    demo_inject_enabled: bool
    demo_injection_active: bool
    telemetry_queue_depth: int
    demo_queue_depth: int
    persistence_queue_depth: int
    persistence_healthy: bool
    persistence_last_error: str | None
    dropped_tick_count: int
    processing_error_count: int
    stale_cutoff_sec: float
    replay_window_events: int
    offline_mode: bool
    feed_source: str
    demo_webhooks_enabled: bool


class InstrumentResponse(BaseModel):
    id: str
    label: str
    feed_mode: str
    physical_symbol: str
    token_symbol: str
    synthetic: bool
    live: bool
    level: str
    last_mahalanobis: float | None
    criticality_recent_pct: float
    events_session: int
    events_total_sqlite: int
    last_tick_age_sec: float | None
    ingestion_source: str


def _instrument_runtime(instrument: str | None) -> InstrumentRuntime:
    instrument_id = instrument or _primary_instrument_id
    runtime = _instrument_runtimes.get(instrument_id)
    if runtime is None:
        raise HTTPException(status_code=404, detail=f"Unknown instrument: {instrument_id}")
    return runtime


@app.get("/api/instruments", response_model=list[InstrumentResponse])
async def api_instruments() -> list[InstrumentResponse]:
    now = time.monotonic()
    out: list[InstrumentResponse] = []
    for config in _instrument_configs:
        runtime = _instrument_runtimes[config.id]
        age = (
            now - runtime.last_tick_monotonic
            if runtime.last_tick_monotonic > 0
            else None
        )
        out.append(
            InstrumentResponse(
                **config.to_dict(),
                live=age is not None and age <= STALE_CUTOFF_SEC,
                level=runtime.level,
                last_mahalanobis=runtime.last_mahalanobis,
                criticality_recent_pct=runtime.criticality_recent_pct,
                events_session=runtime.session_event_count,
                events_total_sqlite=(
                    _persistence.count_events(config.id) if _persistence else 0
                ),
                last_tick_age_sec=age,
                ingestion_source=runtime.ingestion_source,
            )
        )
    return out


@app.get("/api/status", response_model=StatusResponse)
async def api_status() -> StatusResponse:
    assert _persistence is not None
    gem = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    return StatusResponse(
        gemini_configured=gem,
        webhook_configured=bool(webhooks.configured_urls()),
        binance_feed=(
            _instrument_configs[0].feed_mode
            if len(_instrument_configs) == 1
            else "multi"
        ),
        audits_dir=str(AUDITS_DIR.resolve()),
        db_path=str(_persistence.db_path.resolve()),
        global_events_total_sqlite=await asyncio.to_thread(_persistence.count_events),
        mahalanobis_breach_threshold=float(MAHALANOBIS_BREACH),
        criticality_window_events=int(CRITICALITY_WINDOW_EVENTS),
        criticality_audit_pct=float(CRITICALITY_AUDIT_PCT),
        audit_cooldown_ticks=int(AUDIT_COOLDOWN_TICKS),
        demo_inject_enabled=(
            os.environ.get("DYOPS_DEMO_INJECT") == "1"
            and bool(os.environ.get("DYOPS_DEMO_SECRET"))
        ),
        demo_injection_active=_demo_injection_active,
        telemetry_queue_depth=_telemetry_queue.qsize(),
        demo_queue_depth=_demo_telemetry_queue.qsize(),
        persistence_queue_depth=_persistence.queue_depth,
        persistence_healthy=_persistence.healthy,
        persistence_last_error=_persistence.last_error,
        dropped_tick_count=_dropped_tick_count,
        processing_error_count=_processing_error_count,
        stale_cutoff_sec=STALE_CUTOFF_SEC,
        replay_window_events=REPLAY_WINDOW_EVENTS,
        offline_mode=os.environ.get("DYOPS_OFFLINE_MODE") == "1",
        feed_source=(
            "offline_deterministic"
            if os.environ.get("DYOPS_OFFLINE_MODE") == "1"
            else "binance_market"
        ),
        demo_webhooks_enabled=os.environ.get("DYOPS_DEMO_WEBHOOKS") == "1",
    )


@app.post("/api/demo/inject_scenario", status_code=202)
async def inject_demo_scenario(
    name: str = "sudden_depeg",
    seed: int = 13,
    instrument: str | None = None,
    x_dyops_demo_secret: str | None = Header(default=None),
) -> dict[str, int | str]:
    global _demo_injection_active
    _require_demo_access(x_dyops_demo_secret)
    if name != "sudden_depeg":
        raise HTTPException(status_code=400, detail="Only sudden_depeg is available")
    if _demo_injection_active:
        raise HTTPException(status_code=409, detail="A demo injection is already running")
    runtime = _instrument_runtime(instrument)

    scenario = get_scenario(name, seed=seed)
    shock_tick = int(scenario.expected_outcomes.get("shock_tick", 0))
    timestamp = time.time()
    pairs = list(zip(scenario.physical_price, scenario.token_price, strict=True))
    _demo_injection_active = True
    for i, (phys, tok) in enumerate(pairs):
        # Keep injected timestamps effectively current while pacing the visible stress phase.
        ts = timestamp + i * 1e-6
        delay_after = 0.04 if i >= shock_tick else 0.0
        _demo_telemetry_queue.put_nowait(
            (
                runtime.config.id,
                ts,
                float(phys),
                float(tok),
                delay_after,
                name,
                i == len(pairs) - 1,
            )
        )
    return {
        "scenario": name,
        "seed": seed,
        "instrument_id": runtime.config.id,
        "ticks_queued": len(scenario.timestamps),
    }


def _require_demo_access(provided_secret: str | None) -> None:
    if os.environ.get("DYOPS_DEMO_INJECT") != "1":
        raise HTTPException(status_code=404, detail="Not found")
    expected_secret = os.environ.get("DYOPS_DEMO_SECRET", "")
    if not expected_secret:
        raise HTTPException(status_code=503, detail="Demo secret is not configured")
    if provided_secret is None or not secrets.compare_digest(
        provided_secret,
        expected_secret,
    ):
        raise HTTPException(status_code=401, detail="Invalid demo secret")


def _drain_instrument_queue(
    target: queue.Queue,
    instrument_id: str,
    *,
    tagged_lengths: set[int],
) -> None:
    retained: list[Any] = []
    while True:
        try:
            item = target.get_nowait()
        except queue.Empty:
            break
        item_instrument = (
            str(item[0])
            if isinstance(item, tuple) and len(item) in tagged_lengths
            else _primary_instrument_id
        )
        if item_instrument != instrument_id:
            retained.append(item)
    for item in retained:
        target.put_nowait(item)


@app.post("/api/demo/reset")
async def reset_demo(
    instrument: str | None = None,
    x_dyops_demo_secret: str | None = Header(default=None),
) -> dict[str, str]:
    """Reset one demo instrument's persisted history and in-memory policy state."""
    global _demo_injection_active, _demo_resetting, _sentinel
    global _session_event_count, _last_tick_monotonic
    _require_demo_access(x_dyops_demo_secret)
    runtime = _instrument_runtime(instrument)
    assert _persistence is not None
    _demo_resetting = True
    _demo_injection_active = False
    _drain_instrument_queue(
        _demo_telemetry_queue,
        runtime.config.id,
        tagged_lengths={5, 7},
    )
    _drain_instrument_queue(
        _telemetry_queue,
        runtime.config.id,
        tagged_lengths={4, 5},
    )
    try:
        await asyncio.to_thread(
            _persistence.reset_instrument,
            runtime.config.id,
            5.0,
        )
        observer = dyops_core.BasisObserver(
            name=f"dyops-api-{runtime.config.id}",
            theta=1.0,
            ring_buffer_capacity=1000,
        )
        runtime.sentinel = DyopsSentinel(
            observer,
            auditor=runtime.sentinel.auditor,
            persistence=_persistence,
            instrument_id=runtime.config.id,
        )
        runtime.session_event_count = 0
        runtime.last_tick_monotonic = 0.0
        runtime.level = "MONITORING"
        runtime.last_mahalanobis = None
        runtime.criticality_recent_pct = 0.0
        runtime.ingestion_source = "none"
        if runtime.config.id == _primary_instrument_id:
            _sentinel = runtime.sentinel
            _session_event_count = 0
            _last_tick_monotonic = 0.0
    finally:
        _demo_resetting = False
    return {
        "status": "reset",
        "instrument_id": runtime.config.id,
    }


class HistoryPoint(BaseModel):
    instrument_id: str = "default"
    ingestion_source: str = "live"
    scenario: str | None = None
    t: float
    measured_basis: float
    filtered_basis: float
    innovation: float
    mahalanobis: float
    valid: bool


class HistoryTracePoint(HistoryPoint):
    """Replay row plus deterministic statistical reasoning (no Gemini)."""

    reasoning: str


class HistoryTraceBundle(BaseModel):
    """Audit-trail wrapper; default GET /api/history stays a bare array for the chart."""

    summary: str
    explainability: str
    points: list[HistoryTracePoint]


class PulseResponse(BaseModel):
    """Real-time pulse state with short explainability strings for operators."""

    instrument_id: str = "default"
    live: bool
    last_tick_age_sec: float | None
    events_session: int
    events_total_sqlite: int
    summary: str = ""
    explainability: str = ""
    ingestion_source: str = "none"


@app.get("/api/history", response_model=list[HistoryPoint])
async def api_history(
    limit: int = REPLAY_WINDOW_EVENTS,
    instrument: str | None = None,
) -> list[HistoryPoint]:
    assert _persistence is not None
    runtime = _instrument_runtime(instrument)
    rows = _persistence.load_recent_events(
        min(max(limit, 0), REPLAY_WINDOW_EVENTS),
        instrument_id=runtime.config.id,
    )
    plain, _ = _replay_history_events(rows)
    return plain


@app.get("/api/history/trace", response_model=HistoryTraceBundle)
async def api_history_trace(
    limit: int = REPLAY_WINDOW_EVENTS,
    instrument: str | None = None,
) -> HistoryTraceBundle:
    """Replay with per-tick reasoning; chart clients may keep using GET /api/history only."""
    assert _persistence is not None
    runtime = _instrument_runtime(instrument)
    rows = _persistence.load_recent_events(
        min(max(limit, 0), REPLAY_WINDOW_EVENTS),
        instrument_id=runtime.config.id,
    )
    _, trace = _replay_history_events(rows)
    summary, explainability = _trace_window_copy(trace)
    return HistoryTraceBundle(
        summary=summary,
        explainability=explainability,
        points=trace,
    )


@app.get("/api/pulse", response_model=PulseResponse)
async def api_pulse(instrument: str | None = None) -> PulseResponse:
    """Server-side pulse: time since last ingested tick."""
    runtime = _instrument_runtime(instrument)
    stale = runtime.last_tick_monotonic <= 0
    if runtime.last_tick_monotonic > 0:
        stale = (
            time.monotonic() - runtime.last_tick_monotonic
        ) > STALE_CUTOFF_SEC
    total_sqlite = (
        _persistence.count_events(runtime.config.id) if _persistence is not None else 0
    )
    age = (
        time.monotonic() - runtime.last_tick_monotonic
        if runtime.last_tick_monotonic > 0
        else None
    )
    summary, explainability = _pulse_narrative(
        live=not stale,
        age_sec=age,
        session=runtime.session_event_count,
        total=total_sqlite,
    )
    return PulseResponse(
        instrument_id=runtime.config.id,
        live=not stale,
        last_tick_age_sec=age,
        events_session=runtime.session_event_count,
        events_total_sqlite=total_sqlite,
        summary=summary,
        explainability=explainability,
        ingestion_source=runtime.ingestion_source,
    )


@app.websocket("/ws/telemetry")
async def websocket_telemetry(ws: WebSocket) -> None:
    await ws.accept()
    instrument_id = ws.query_params.get("instrument")
    if instrument_id is not None and instrument_id not in _instrument_runtimes:
        await ws.close(code=1008, reason="Unknown instrument")
        return
    await hub.register_telemetry(ws, instrument_id)
    try:
        while True:
            await ws.receive_text()  # optional client pings; discard
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unregister_telemetry(ws)


@app.websocket("/ws/audits")
async def websocket_audits(ws: WebSocket) -> None:
    await ws.accept()
    await hub.register_audits(ws)
    assert _persistence is not None
    # Initial snapshot (newest last for chronological scroll — send oldest-first chunk)
    initial = _persistence.load_recent_audits(50)
    initial_chrono = list(reversed(initial))
    for row in initial_chrono:
        await ws.send_text(
            json.dumps({"type": "audit", "payload": row}, allow_nan=False, default=str)
        )
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unregister_audits(ws)
