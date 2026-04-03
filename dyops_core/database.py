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


class PersistenceManager:
    """
    Queue-backed sqlite writer: ``events`` (telemetry) and ``audits`` (full JSON reports).
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self._q: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._latest_event_id_lock = threading.Lock()
        self._latest_event_id: int = 0
        self._thread = threading.Thread(target=self._writer_loop, name="dyops-sqlite", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5.0)

    @property
    def db_path(self) -> Path:
        return self._path

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
                timestamp REAL NOT NULL,
                physical_price REAL NOT NULL,
                token_price REAL NOT NULL,
                innovation REAL,
                mahalanobis_distance REAL
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events (timestamp DESC);

            CREATE TABLE IF NOT EXISTS audits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                event_id INTEGER,
                report_json TEXT NOT NULL,
                FOREIGN KEY (event_id) REFERENCES events (id)
            );
            CREATE INDEX IF NOT EXISTS idx_audits_ts ON audits (timestamp DESC);
            """
        )
        conn.commit()

    def _writer_loop(self) -> None:
        try:
            conn = self._connect()
            self._init_schema(conn)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Persistence init failed: {}", exc)
            self._ready.set()
            return
        self._ready.set()

        while not self._stop.is_set():
            try:
                kind, payload = self._q.get(timeout=0.35)
            except queue.Empty:
                continue
            try:
                if kind == "event":
                    cur = conn.execute(
                        """
                        INSERT INTO events
                        (timestamp, physical_price, token_price, innovation, mahalanobis_distance)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            float(payload["timestamp"]),
                            float(payload["physical_price"]),
                            float(payload["token_price"]),
                            payload.get("innovation"),
                            payload.get("mahalanobis_distance"),
                        ),
                    )
                    eid = int(cur.lastrowid)
                    with self._latest_event_id_lock:
                        self._latest_event_id = eid
                elif kind == "audit":
                    ts = float(payload.get("timestamp") or time.time())
                    report = payload["report_json"]
                    if not isinstance(report, str):
                        report = json.dumps(report, allow_nan=False)
                    eid = payload.get("event_id")
                    if eid is None:
                        with self._latest_event_id_lock:
                            eid = self._latest_event_id
                    conn.execute(
                        """
                        INSERT INTO audits (timestamp, event_id, report_json)
                        VALUES (?, ?, ?)
                        """,
                        (ts, eid, report),
                    )
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Persistence write failed: {}", exc)

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
        innovation: float | None,
        mahalanobis_distance: float | None,
    ) -> None:
        self._q.put(
            (
                "event",
                {
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
    ) -> None:
        self._q.put(
            (
                "audit",
                {
                    "timestamp": timestamp,
                    "report_json": report_json,
                    "event_id": event_id,
                },
            )
        )

    def load_recent_events(self, limit: int = 500) -> list[dict[str, Any]]:
        """Return up to ``limit`` most recent rows, oldest-first (for Kalman replay)."""
        conn = self._connect()
        try:
            self._init_schema(conn)
            cur = conn.execute(
                """
                SELECT timestamp, physical_price, token_price, innovation, mahalanobis_distance
                FROM events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        rows.reverse()
        return [
            {
                "timestamp": r[0],
                "physical_price": r[1],
                "token_price": r[2],
                "innovation": r[3],
                "mahalanobis_distance": r[4],
            }
            for r in rows
        ]

    def load_recent_audits(self, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent audit rows, newest-first (``report`` is parsed JSON)."""
        conn = self._connect()
        try:
            self._init_schema(conn)
            cur = conn.execute(
                """
                SELECT id, timestamp, event_id, report_json
                FROM audits
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                report = json.loads(r[3]) if isinstance(r[3], str) else r[3]
            except json.JSONDecodeError:
                report = {"raw": r[3]}
            out.append(
                {
                    "id": int(r[0]),
                    "timestamp": float(r[1]),
                    "event_id": r[2],
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
                SELECT id, timestamp, event_id, report_json
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
                report = json.loads(r[3]) if isinstance(r[3], str) else r[3]
            except json.JSONDecodeError:
                report = {"raw": r[3]}
            out.append(
                {
                    "id": int(r[0]),
                    "timestamp": float(r[1]),
                    "event_id": r[2],
                    "report": report,
                }
            )
        return out

    def count_events(self) -> int:
        conn = self._connect()
        try:
            self._init_schema(conn)
            cur = conn.execute("SELECT COUNT(*) FROM events")
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

    def close(self) -> None:
        self._stop.set()
