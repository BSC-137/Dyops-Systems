from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path

import pytest

from backend import main as api
from backend.main import PersistenceManager, load_instruments


def test_registry_supports_stable_and_lst_presets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DYOPS_INSTRUMENTS", "stable,lst")

    configs = load_instruments()

    assert [config.id for config in configs] == ["default", "lst"]
    assert [config.feed_mode for config in configs] == ["stable", "lst"]


def test_registry_defaults_to_backward_compatible_instrument(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DYOPS_INSTRUMENTS", raising=False)
    monkeypatch.delenv("DYOPS_INSTRUMENT_ID", raising=False)

    assert load_instruments()[0].id == "default"


def test_event_schema_migrates_and_filters_by_instrument(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        """
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            physical_price REAL NOT NULL,
            token_price REAL NOT NULL,
            innovation REAL,
            mahalanobis_distance REAL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE audits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            event_id INTEGER,
            report_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO events
        (timestamp, physical_price, token_price, innovation, mahalanobis_distance)
        VALUES (1, 1, 1, 0, 0)
        """
    )
    connection.commit()
    connection.close()

    persistence = PersistenceManager(db_path)
    persistence.schedule_event(
        2,
        1,
        1,
        instrument_id="default",
        innovation=0,
        mahalanobis_distance=0,
    )
    persistence.schedule_event(
        3,
        2000,
        1999,
        instrument_id="lst",
        innovation=0.1,
        mahalanobis_distance=1.0,
    )

    deadline = time.monotonic() + 2
    while persistence.count_events() < 3 and time.monotonic() < deadline:
        time.sleep(0.01)

    default_rows = persistence.load_recent_events(
        10,
        instrument_id="default",
    )
    lst_rows = persistence.load_recent_events(10, instrument_id="lst")
    persistence.close()

    assert len(default_rows) == 2
    assert {row["instrument_id"] for row in default_rows} == {"default"}
    assert len(lst_rows) == 1
    assert lst_rows[0]["instrument_id"] == "lst"


def test_close_drains_all_accepted_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "drain.db"
    persistence = PersistenceManager(db_path)
    for timestamp in range(750):
        persistence.schedule_event(
            timestamp,
            100,
            100,
            instrument_id="default",
            innovation=0,
            mahalanobis_distance=0,
        )

    persistence.close(timeout=5.0)

    connection = sqlite3.connect(db_path)
    try:
        count = connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        connection.close()
    assert count == 750
    with pytest.raises(RuntimeError, match="closed"):
        persistence.schedule_event(
            751,
            100,
            100,
            innovation=0,
            mahalanobis_distance=0,
        )


def test_initialization_failure_is_raised_to_caller(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="initialization failed"):
        PersistenceManager(tmp_path / "missing" / "cannot-open.db")


def test_startup_replay_matches_full_forensic_window_over_500_events(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "replay.db"
    writer = PersistenceManager(db_path)
    rows: list[tuple[float, float, float]] = []
    for tick in range(650):
        physical = 100.0 + math.sin(tick / 17.0)
        token = 100.0 + math.cos(tick / 23.0) * 0.5
        rows.append((float(tick), physical, token))
        writer.schedule_event(
            float(tick),
            physical,
            token,
            instrument_id="default",
            innovation=None,
            mahalanobis_distance=None,
        )
    writer.close()

    persistence = PersistenceManager(db_path)
    restored = api._replay_observer_state(persistence, "default")
    forensic_rows = persistence.load_recent_events(
        api.REPLAY_WINDOW_EVENTS,
        instrument_id="default",
    )
    persistence.close()
    assert len(forensic_rows) == 650

    continuous = api.dyops_core.BasisObserver(
        name="continuous",
        theta=1.0,
        ring_buffer_capacity=1000,
    )
    for timestamp, physical, token in rows:
        continuous.update(timestamp, physical, token)

    expected = continuous.update(650.0, 101.0, 100.0)
    actual = restored.update(650.0, 101.0, 100.0)
    assert actual.filtered_basis == pytest.approx(expected.filtered_basis, abs=1e-12)
    assert actual.innovation == pytest.approx(expected.innovation, abs=1e-12)
    assert actual.mahalanobis_distance == pytest.approx(
        expected.mahalanobis_distance,
        abs=1e-12,
    )
