"""
Dyops API — FastAPI backend: Binance feed → DyopsSentinel → SQLite + WebSocket fan-out.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# dyops_core package (sentinel, database, binance_feed) lives alongside this repo folder
_DYOPS_PY = Path(__file__).resolve().parent.parent / "dyops_core"
if str(_DYOPS_PY) not in sys.path:
    sys.path.insert(0, str(_DYOPS_PY))

import dyops_core  # noqa: E402
from binance_feed import resolve_feed_mode, start_binance_feed_thread  # noqa: E402
from database import PersistenceManager  # noqa: E402
from sentinel import (  # noqa: E402
    AUDITS_DIR,
    AgenticAuditor,
    DyopsSentinel,
    EventResult,
    MAHALANOBIS_BREACH,
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
        self._telemetry: set[WebSocket] = set()
        self._audits: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def register_telemetry(self, ws: WebSocket) -> None:
        async with self._lock:
            self._telemetry.add(ws)

    async def unregister_telemetry(self, ws: WebSocket) -> None:
        async with self._lock:
            self._telemetry.discard(ws)

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
            for ws in self._telemetry:
                try:
                    await ws.send_text(raw)
                except Exception:  # noqa: BLE001
                    dead.append(ws)
            for ws in dead:
                self._telemetry.discard(ws)

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
_telemetry_queue: queue.Queue[tuple[float, float, float]] = queue.Queue()
_binance_thread: threading.Thread | None = None
_persistence: PersistenceManager | None = None
_sentinel: DyopsSentinel | None = None
_session_event_count: int = 0
_last_tick_monotonic: float = 0.0


def _replay_observer_state(persistence: PersistenceManager) -> dyops_core.BasisObserver:
    observer = dyops_core.BasisObserver(
        name="dyops-api",
        theta=1.0,
        ring_buffer_capacity=1000,
    )
    rows = persistence.load_recent_events(500)
    for row in rows:
        observer.update(
            float(row["timestamp"]),
            float(row["physical_price"]),
            float(row["token_price"]),
        )
    return observer


def _on_startup_sync() -> DyopsSentinel:
    global _persistence, _sentinel, _session_event_count, _binance_thread
    db_path = os.environ.get("DYOPS_SQLITE_PATH")
    _persistence = PersistenceManager(db_path)
    observer = _replay_observer_state(_persistence)
    _session_event_count = 0
    _sentinel = DyopsSentinel(
        observer,
        auditor=_try_create_auditor(_persistence),
        persistence=_persistence,
    )
    _stop_binance.clear()
    _binance_thread = start_binance_feed_thread(
        _telemetry_queue,
        _stop_binance,
        mode=resolve_feed_mode(),
    )
    return _sentinel


def _try_create_auditor(persistence: PersistenceManager) -> AgenticAuditor | None:
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        return None
    try:
        return AgenticAuditor(persistence=persistence, audits_dir=AUDITS_DIR)
    except (ImportError, ValueError):
        return None


def _on_shutdown_sync() -> None:
    global _binance_thread
    _stop_binance.set()
    if _persistence is not None:
        _persistence.close()
    _binance_thread = None


async def _telemetry_pump() -> None:
    global _last_tick_monotonic, _session_event_count
    assert _sentinel is not None
    while True:
        try:
            ts, phys, tok = _telemetry_queue.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.01)
            continue
        try:
            result = _sentinel.process_event(ts, phys, tok)
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.001)
            continue
        _last_tick_monotonic = time.monotonic()
        _session_event_count += 1
        model = _event_result_model(result)
        model["timestamp"] = ts
        model["physical_price"] = phys
        model["token_price"] = tok
        model["session_event_index"] = _session_event_count
        await hub.broadcast_telemetry(model)


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
    binance_feed: str
    audits_dir: str
    db_path: str
    global_events_total_sqlite: int


@app.get("/api/status", response_model=StatusResponse)
async def api_status() -> StatusResponse:
    assert _persistence is not None
    gem = bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
    return StatusResponse(
        gemini_configured=gem,
        binance_feed=resolve_feed_mode(),
        audits_dir=str(AUDITS_DIR.resolve()),
        db_path=str(_persistence.db_path.resolve()),
        global_events_total_sqlite=_persistence.count_events(),
    )


class HistoryPoint(BaseModel):
    t: float
    basis: float
    innovation: float
    valid: bool


@app.get("/api/history", response_model=list[HistoryPoint])
async def api_history(limit: int = 500) -> list[HistoryPoint]:
    assert _persistence is not None
    rows = _persistence.load_recent_events(min(limit, 2000))
    observer = dyops_core.BasisObserver(
        name="dyops-api-replay",
        theta=1.0,
        ring_buffer_capacity=1000,
    )
    out: list[HistoryPoint] = []
    for row in rows:
        h = observer.update(
            float(row["timestamp"]),
            float(row["physical_price"]),
            float(row["token_price"]),
        )
        out.append(
            HistoryPoint(
                t=float(row["timestamp"]),
                basis=h.filtered_basis,
                innovation=h.innovation,
                valid=h.measurement_valid,
            )
        )
    return out


@app.get("/api/pulse")
async def api_pulse() -> dict[str, Any]:
    """Server-side pulse: time since last ingested tick."""
    stale = True
    if _last_tick_monotonic > 0:
        stale = (time.monotonic() - _last_tick_monotonic) > 12.0
    total_sqlite = _persistence.count_events() if _persistence is not None else 0
    return {
        "live": not stale,
        "last_tick_age_sec": time.monotonic() - _last_tick_monotonic
        if _last_tick_monotonic > 0
        else None,
        "events_session": _session_event_count,
        "events_total_sqlite": total_sqlite,
    }


@app.websocket("/ws/telemetry")
async def websocket_telemetry(ws: WebSocket) -> None:
    await ws.accept()
    await hub.register_telemetry(ws)
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
