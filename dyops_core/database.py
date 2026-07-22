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
_WRITE_BATCH_MAX = 256
_WRITE_BATCH_WAIT_SEC = 0.002


class PersistenceManager:
    """
    Queue-backed sqlite writer: ``events`` (telemetry) and ``audits`` (full JSON reports).
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._q: queue.SimpleQueue[tuple[str, Any] | object] = (
            queue.SimpleQueue()
        )
        self._ready = threading.Event()
        self._state_lock = threading.Lock()
        self._closed = False
        self._init_error: BaseException | None = None
        self._last_write_error: str | None = None
        self._latest_event_id_lock = threading.Lock()
        self._latest_event_ids: dict[str, int] = {}
        self._event_counts_lock = threading.Lock()
        self._event_counts: dict[str, int] = {}
        self._global_event_count = 0
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
                mahalanobis_distance REAL,
                ingestion_source TEXT NOT NULL DEFAULT 'live',
                scenario TEXT
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
        if "ingestion_source" not in event_columns:
            conn.execute(
                "ALTER TABLE events ADD COLUMN ingestion_source TEXT NOT NULL DEFAULT 'live'"
            )
        if "scenario" not in event_columns:
            conn.execute("ALTER TABLE events ADD COLUMN scenario TEXT")
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
            count_rows = conn.execute(
                """
                SELECT instrument_id, COUNT(*), MAX(id)
                FROM events
                GROUP BY instrument_id
                """
            ).fetchall()
            with self._event_counts_lock:
                self._event_counts = {
                    str(instrument_id): int(count)
                    for instrument_id, count, _ in count_rows
                }
                self._global_event_count = sum(self._event_counts.values())
            with self._latest_event_id_lock:
                self._latest_event_ids = {
                    str(instrument_id): int(latest_id)
                    for instrument_id, _, latest_id in count_rows
                    if latest_id is not None
                }
        except Exception as exc:  # noqa: BLE001
            self._init_error = exc
            logger.exception("Persistence init failed: {}", exc)
            self._ready.set()
            return
        self._ready.set()

        closing = False
        while not closing:
            batch = [self._q.get()]
            batch_deadline = time.monotonic() + _WRITE_BATCH_WAIT_SEC
            while len(batch) < _WRITE_BATCH_MAX and batch[-1] is not _CLOSE:
                timeout = batch_deadline - time.monotonic()
                if timeout <= 0.0:
                    break
                try:
                    batch.append(self._q.get(timeout=timeout))
                except queue.Empty:
                    break
                if batch[-1] is _CLOSE:
                    break
            writes = [item for item in batch if item is not _CLOSE]
            closing = any(item is _CLOSE for item in batch)
            with self._event_counts_lock:
                next_counts = dict(self._event_counts)
            with self._latest_event_id_lock:
                next_latest_ids = dict(self._latest_event_ids)
            try:
                conn.execute("BEGIN")
                index = 0
                while index < len(writes):
                    kind, payload = writes[index]
                    if kind == "event":
                        event_payloads: list[tuple[Any, ...]] = []
                        while index < len(writes) and writes[index][0] == "event":
                            event_payloads.append(writes[index][1])
                            index += 1
                        conn.executemany(
                            """
                            INSERT INTO events
                            (instrument_id, timestamp, physical_price, token_price,
                             innovation, mahalanobis_distance, ingestion_source, scenario)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            event_payloads,
                        )
                        last_id = int(
                            conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                        )
                        first_id = last_id - len(event_payloads) + 1
                        for offset, event in enumerate(event_payloads):
                            instrument_id = str(event[0])
                            next_latest_ids[instrument_id] = first_id + offset
                            next_counts[instrument_id] = (
                                next_counts.get(instrument_id, 0) + 1
                            )
                        continue
                    if kind == "audit":
                        timestamp = float(payload.get("timestamp") or time.time())
                        instrument_id = str(
                            payload.get("instrument_id") or "default"
                        )
                        report = payload["report_json"]
                        if not isinstance(report, str):
                            report = json.dumps(report, allow_nan=False)
                        event_id = payload.get("event_id")
                        if event_id is None:
                            event_id = next_latest_ids.get(instrument_id)
                        conn.execute(
                            """
                            INSERT INTO audits
                            (instrument_id, timestamp, event_id, report_json)
                            VALUES (?, ?, ?, ?)
                            """,
                            (instrument_id, timestamp, event_id, report),
                        )
                    elif kind == "reset":
                        instrument_id = str(payload["instrument_id"])
                        conn.execute(
                            "DELETE FROM audits WHERE instrument_id = ?",
                            (instrument_id,),
                        )
                        conn.execute(
                            "DELETE FROM events WHERE instrument_id = ?",
                            (instrument_id,),
                        )
                        next_latest_ids.pop(instrument_id, None)
                        next_counts.pop(instrument_id, None)
                    index += 1
                conn.commit()
                with self._event_counts_lock:
                    self._event_counts = next_counts
                    self._global_event_count = sum(next_counts.values())
                with self._latest_event_id_lock:
                    self._latest_event_ids = next_latest_ids
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                self._last_write_error = str(exc)
                for item in writes:
                    kind, payload = item
                    if kind == "reset":
                        payload["error"] = str(exc)
                logger.exception("Persistence write failed: {}", exc)
            finally:
                for item in batch:
                    if item is not _CLOSE:
                        _, payload = item
                        if isinstance(payload, dict):
                            completion = payload.get("completion")
                            if isinstance(completion, threading.Event):
                                completion.set()

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
        ingestion_source: str = "live",
        scenario: str | None = None,
    ) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("PersistenceManager is closed")
            self._q.put(
                (
                    "event",
                    (
                        instrument_id,
                        float(timestamp),
                        float(physical_price),
                        float(token_price),
                        innovation,
                        mahalanobis_distance,
                        ingestion_source,
                        scenario,
                    ),
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
                           mahalanobis_distance, ingestion_source, scenario
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
                           mahalanobis_distance, ingestion_source, scenario
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
                "ingestion_source": r[6],
                "scenario": r[7],
            }
            for r in rows
        ]

    def reset_instrument(self, instrument_id: str, timeout: float = 5.0) -> None:
        """Delete one instrument's accepted history after earlier queued writes drain."""
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        completion = threading.Event()
        payload: dict[str, Any] = {
            "instrument_id": instrument_id,
            "completion": completion,
        }
        with self._state_lock:
            if self._closed:
                raise RuntimeError("PersistenceManager is closed")
            self._q.put(
                (
                    "reset",
                    payload,
                )
            )
        if not completion.wait(timeout):
            raise TimeoutError(
                f"Persistence reset did not complete within {timeout:.3f} seconds"
            )
        if "error" in payload:
            raise RuntimeError(f"Persistence reset failed: {payload['error']}")

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
        """Return the writer-reconciled committed event count without SQL."""
        with self._event_counts_lock:
            if instrument_id is None:
                return self._global_event_count
            return self._event_counts.get(instrument_id, 0)

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
