"""Shared fixtures for backend API contract tests."""

from __future__ import annotations

import asyncio
import queue
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import main as api
from database import PersistenceManager
from sentinel import DyopsSentinel


def drain_telemetry_queue() -> None:
    for telemetry_queue in (api._telemetry_queue, api._demo_telemetry_queue):
        while True:
            try:
                telemetry_queue.get_nowait()
            except queue.Empty:
                break


def wait_for_persisted_events(
    client: TestClient,
    expected: int,
    timeout: float = 3.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get("/api/status")
        response.raise_for_status()
        if response.json()["global_events_total_sqlite"] == expected:
            return
        time.sleep(0.01)
    pytest.fail(f"Timed out waiting for {expected} persisted events")


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Run the API with local persistence and its pump, but no external services."""

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("DYOPS_DEMO_INJECT", raising=False)
    monkeypatch.delenv("DYOPS_DEMO_SECRET", raising=False)
    monkeypatch.delenv("DYOPS_DEMO_WEBHOOKS", raising=False)
    monkeypatch.delenv("DYOPS_OFFLINE_MODE", raising=False)
    monkeypatch.delenv("DYOPS_FEED_DISABLED", raising=False)
    monkeypatch.delenv("DYOPS_WEBHOOK_URLS", raising=False)
    monkeypatch.delenv("DYOPS_INSTRUMENT_ID", raising=False)
    monkeypatch.delenv("DYOPS_INSTRUMENTS", raising=False)

    @asynccontextmanager
    async def test_lifespan(_: FastAPI) -> AsyncIterator[None]:
        drain_telemetry_queue()
        api._persistence = PersistenceManager(tmp_path / "api-contract.db")
        config = api.InstrumentConfig(
            id="default",
            label="Default stable",
            feed_mode="stable",
            physical_symbol="USD",
            token_symbol="USDCUSDT",
            synthetic=True,
        )
        api._sentinel = DyopsSentinel(
            api.dyops_core.BasisObserver(
                name="api-contract-test",
                theta=1.0,
                ring_buffer_capacity=1000,
            ),
            auditor=None,
            persistence=api._persistence,
            instrument_id=config.id,
        )
        api._instrument_configs = (config,)
        api._primary_instrument_id = config.id
        api._instrument_runtimes = {
            config.id: api.InstrumentRuntime(config, api._sentinel)
        }
        api._session_event_count = 0
        api._last_tick_monotonic = 0.0
        api._dropped_tick_count = 0
        api._processing_error_count = 0
        api._last_ingest_log_at.clear()
        api._demo_injection_active = False
        api._gemini_auditor = None
        api._gemini_last_error = None

        pump = asyncio.create_task(api._telemetry_pump())
        try:
            yield
        finally:
            pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass
            api._persistence.close()
            api._persistence = None
            api._sentinel = None
            api._instrument_configs = ()
            api._instrument_runtimes = {}
            api._primary_instrument_id = "default"
            api._session_event_count = 0
            api._last_tick_monotonic = 0.0
            api._demo_injection_active = False
            api._gemini_auditor = None
            api._gemini_last_error = None
            drain_telemetry_queue()

    monkeypatch.setattr(api.app.router, "lifespan_context", test_lifespan)
    with TestClient(api.app) as test_client:
        yield test_client
