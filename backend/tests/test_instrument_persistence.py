from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

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
