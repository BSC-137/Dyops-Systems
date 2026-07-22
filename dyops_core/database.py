"""
SQLite time-series persistence for Dyops operational telemetry and AI audits.

All writes go through a background writer thread so ingestion never blocks.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from loguru import logger


DEFAULT_DB_PATH = Path(__file__).resolve().parent / "dyops_ts.db"
REPLAY_WINDOW_EVENTS = 1000
_CLOSE = object()


class PersistenceManager:
    """
    Queue-backed sqlite writer: ``events`` (telemetry) and ``audits`` (full JSON reports).
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._q: queue.Queue[tuple[str, dict[str, Any]] | object] = queue.Queue()
        self._ready = threading.Event()
        self._state_lock = threading.Lock()
        self._closed = False
        self._init_error: BaseException | None = None
        self._last_write_error: str | None = None
        self._latest_event_id_lock = threading.Lock()
        self._latest_event_ids: dict[str, int] = {}
        self._thread = threading.Thread(target=self._writer_loop, name="dyops-sqlite", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            raise TimeoutError("Persistence writer initialization timed out")
        if self._init_error is not None:
            raise RuntimeError(
                f"Persistence writer initialization failed: {self._init_error}"
            ) from self._init_error

    @property
    def db_path(self) -> Path:
        return self._path

    @property
    def queue_depth(self) -> int:
        """Number of accepted writes still waiting for the writer."""
        return self._q.qsize()

    @property
    def healthy(self) -> bool:
        """Whether initialization succeeded and no asynchronous write has failed."""
        return (
            self._ready.is_set()
            and self._init_error is None
            and self._last_write_error is None
            and (self._thread.is_alive() or self._closed)
        )

    @property
    def last_error(self) -> str | None:
        return (
            str(self._init_error)
            if self._init_error is not None
            else self._last_write_error
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument_id TEXT NOT NULL DEFAULT 'default',
                timestamp REAL NOT NULL,
                physical_price REAL NOT NULL,
                token_price REAL NOT NULL,
                innovation REAL,
                mahalanobis_distance REAL
            );

            CREATE TABLE IF NOT EXISTS audits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instrument_id TEXT NOT NULL DEFAULT 'default',
                timestamp REAL NOT NULL,
                event_id INTEGER,
                report_json TEXT NOT NULL,
                FOREIGN KEY (event_id) REFERENCES events (id)
            );
            """
        )
        event_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(events)").fetchall()
        }
        if "instrument_id" not in event_columns:
            conn.execute(
                "ALTER TABLE events ADD COLUMN instrument_id TEXT NOT NULL DEFAULT 'default'"
            )
        audit_columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(audits)").fetchall()
        }
        if "instrument_id" not in audit_columns:
            conn.execute(
                "ALTER TABLE audits ADD COLUMN instrument_id TEXT NOT NULL DEFAULT 'default'"
            )
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events (timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_events_instrument
            ON events (instrument_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_audits_ts ON audits (timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_audits_instrument
            ON audits (instrument_id, id DESC);
            """
        )
        conn.commit()

    def _writer_loop(self) -> None:
        try:
            conn = self._connect()
            self._init_schema(conn)
        except Exception as exc:  # noqa: BLE001
            self._init_error = exc
            logger.exception("Persistence init failed: {}", exc)
            self._ready.set()
            return
        self._ready.set()

        while True:
            item = self._q.get()
            if item is _CLOSE:
                self._q.task_done()
                break
            kind, payload = item
            try:
                if kind == "event":
                    cur = conn.execute(
                        """
                        INSERT INTO events
                        (instrument_id, timestamp, physical_price, token_price, innovation,
                         mahalanobis_distance)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            payload["instrument_id"],
                            float(payload["timestamp"]),
                            float(payload["physical_price"]),
                            float(payload["token_price"]),
                            payload.get("innovation"),
                            payload.get("mahalanobis_distance"),
                        ),
                    )
                    eid = int(cur.lastrowid)
                    with self._latest_event_id_lock:
                        self._latest_event_ids[payload["instrument_id"]] = eid
                elif kind == "audit":
                    ts = float(payload.get("timestamp") or time.time())
                    instrument_id = str(payload.get("instrument_id") or "default")
                    report = payload["report_json"]
                    if not isinstance(report, str):
                        report = json.dumps(report, allow_nan=False)
                    eid = payload.get("event_id")
                    if eid is None:
                        with self._latest_event_id_lock:
                            eid = self._latest_event_ids.get(instrument_id)
                    conn.execute(
                        """
                        INSERT INTO audits (instrument_id, timestamp, event_id, report_json)
                        VALUES (?, ?, ?, ?)
                        """,
                        (instrument_id, ts, eid, report),
                    )
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                self._last_write_error = str(exc)
                logger.exception("Persistence write failed: {}", exc)
            finally:
                self._q.task_done()

        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    def schedule_event(
        self,
        timestamp: float,
        physical_price: float,
        token_price: float,
        *,
        instrument_id: str = "default",
        innovation: float | None,
        mahalanobis_distance: float | None,
    ) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("PersistenceManager is closed")
            self._q.put(
                (
                    "event",
                    {
                        "instrument_id": instrument_id,
                        "timestamp": timestamp,
                        "physical_price": physical_price,
                        "token_price": token_price,
                        "innovation": innovation,
                        "mahalanobis_distance": mahalanobis_distance,
                    },
                )
            )

    def schedule_audit(
        self,
        report_json: str | dict[str, Any],
        *,
        timestamp: float | None = None,
        event_id: int | None = None,
        instrument_id: str = "default",
    ) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("PersistenceManager is closed")
            self._q.put(
                (
                    "audit",
                    {
                        "instrument_id": instrument_id,
                        "timestamp": timestamp,
                        "report_json": report_json,
                        "event_id": event_id,
                    },
                )
            )

    def load_recent_events(
        self,
        limit: int = 500,
        *,
        instrument_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return up to ``limit`` most recent rows, oldest-first (for Kalman replay)."""
        conn = self._connect()
        try:
            self._init_schema(conn)
            if instrument_id is None:
                cur = conn.execute(
                    """
                    SELECT instrument_id, timestamp, physical_price, token_price, innovation,
                           mahalanobis_distance
                    FROM events
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT instrument_id, timestamp, physical_price, token_price, innovation,
                           mahalanobis_distance
                    FROM events
                    WHERE instrument_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (instrument_id, limit),
                )
            rows = cur.fetchall()
        finally:
            conn.close()
        rows.reverse()
        return [
            {
                "instrument_id": r[0],
                "timestamp": r[1],
                "physical_price": r[2],
                "token_price": r[3],
                "innovation": r[4],
                "mahalanobis_distance": r[5],
            }
            for r in rows
        ]

    def load_recent_audits(
        self,
        limit: int = 50,
        *,
        instrument_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Most recent audit rows, newest-first (``report`` is parsed JSON)."""
        conn = self._connect()
        try:
            self._init_schema(conn)
            if instrument_id is None:
                cur = conn.execute(
                    """
                    SELECT id, instrument_id, timestamp, event_id, report_json
                    FROM audits
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT id, instrument_id, timestamp, event_id, report_json
                    FROM audits
                    WHERE instrument_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (instrument_id, limit),
                )
            rows = cur.fetchall()
        finally:
            conn.close()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                report = json.loads(r[4]) if isinstance(r[4], str) else r[4]
            except json.JSONDecodeError:
                report = {"raw": r[4]}
            out.append(
                {
                    "id": int(r[0]),
                    "instrument_id": r[1],
                    "timestamp": float(r[2]),
                    "event_id": r[3],
                    "report": report,
                }
            )
        return out

    def load_audits_after(self, after_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """Audit rows with id > ``after_id``, ascending id (for live tail)."""
        conn = self._connect()
        try:
            self._init_schema(conn)
            cur = conn.execute(
                """
                SELECT id, instrument_id, timestamp, event_id, report_json
                FROM audits
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (after_id, limit),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                report = json.loads(r[4]) if isinstance(r[4], str) else r[4]
            except json.JSONDecodeError:
                report = {"raw": r[4]}
            out.append(
                {
                    "id": int(r[0]),
                    "instrument_id": r[1],
                    "timestamp": float(r[2]),
                    "event_id": r[3],
                    "report": report,
                }
            )
        return out

    def count_events(self, instrument_id: str | None = None) -> int:
        conn = self._connect()
        try:
            self._init_schema(conn)
            if instrument_id is None:
                cur = conn.execute("SELECT COUNT(*) FROM events")
            else:
                cur = conn.execute(
                    "SELECT COUNT(*) FROM events WHERE instrument_id = ?",
                    (instrument_id,),
                )
            row = cur.fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def get_max_audit_id(self) -> int:
        conn = self._connect()
        try:
            self._init_schema(conn)
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM audits").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def close(self, timeout: float = 5.0) -> None:
        """Drain accepted writes and stop the writer within ``timeout`` seconds."""
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        with self._state_lock:
            if not self._closed:
                self._closed = True
                self._q.put(_CLOSE)
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            raise TimeoutError(
                f"Persistence writer did not stop within {timeout:.3f} seconds"
            )
