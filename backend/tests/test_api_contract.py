from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import main as api
from backend import webhooks
import sentinel
from database import PersistenceManager
from sentinel import DyopsSentinel


def _drain_telemetry_queue() -> None:
    for telemetry_queue in (api._telemetry_queue, api._demo_telemetry_queue):
        while True:
            try:
                telemetry_queue.get_nowait()
            except queue.Empty:
                break


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """Run the API with local persistence and its pump, but no external services."""

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("DYOPS_DEMO_INJECT", raising=False)
    monkeypatch.delenv("DYOPS_WEBHOOK_URLS", raising=False)
    monkeypatch.delenv("DYOPS_INSTRUMENT_ID", raising=False)

    @asynccontextmanager
    async def test_lifespan(_: FastAPI) -> AsyncIterator[None]:
        _drain_telemetry_queue()
        api._persistence = PersistenceManager(tmp_path / "api-contract.db")
        api._sentinel = DyopsSentinel(
            api.dyops_core.BasisObserver(
                name="api-contract-test",
                theta=1.0,
                ring_buffer_capacity=1000,
            ),
            auditor=None,
            persistence=api._persistence,
        )
        api._session_event_count = 0
        api._last_tick_monotonic = 0.0

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
            api._session_event_count = 0
            api._last_tick_monotonic = 0.0
            _drain_telemetry_queue()

    monkeypatch.setattr(api.app.router, "lifespan_context", test_lifespan)
    with TestClient(api.app) as test_client:
        yield test_client


def _wait_for_persisted_events(
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


def test_status_uses_sentinel_breach_threshold(client: TestClient) -> None:
    response = client.get("/api/status")

    assert response.status_code == 200
    assert (
        response.json()["mahalanobis_breach_threshold"]
        == sentinel.MAHALANOBIS_BREACH
    )


def test_breach_sends_webhook_but_monitoring_does_not(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    webhook_received = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        webhook_received.set()
        return httpx.Response(204)

    real_async_client = httpx.AsyncClient

    def mock_async_client(**kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(**kwargs)

    monkeypatch.setattr(webhooks.httpx, "AsyncClient", mock_async_client)
    monkeypatch.setenv("DYOPS_WEBHOOK_URLS", "https://partner.example/webhooks/dyops")
    monkeypatch.setenv("DYOPS_INSTRUMENT_ID", "usdc-usdt")

    status = client.get("/api/status")
    assert status.json()["webhook_configured"] is True

    api._telemetry_queue.put((0.0, 100.0, 100.0))
    _wait_for_persisted_events(client, 1)
    time.sleep(0.05)
    assert requests == []

    prices = [(float(tick), 100.0, 100.0) for tick in range(1, 31)]
    prices.append((31.0, 100.0, 90.0))
    for tick in prices:
        api._telemetry_queue.put(tick)
    _wait_for_persisted_events(client, 1 + len(prices))
    assert webhook_received.wait(timeout=1.0)

    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["level"] == "BREACH"
    assert payload["mahalanobis"] > sentinel.MAHALANOBIS_BREACH
    assert payload["instrument_id"] == "usdc-usdt"
    assert {
        "timestamp",
        "innovation",
        "criticality_recent_pct",
        "summary",
        "explainability",
    } <= payload.keys()
    assert "event_id" not in payload


def test_history_trace_has_deterministic_breach_reasoning(
    client: TestClient,
) -> None:
    prices = [(float(tick), 100.0, 100.0) for tick in range(30)]
    prices.append((30.0, 100.0, 90.0))
    for tick in prices:
        api._telemetry_queue.put(tick)
    _wait_for_persisted_events(client, len(prices))

    first = client.get("/api/history/trace", params={"limit": len(prices)})
    second = client.get("/api/history/trace", params={"limit": len(prices)})

    assert first.status_code == 200
    assert first.json() == second.json()
    breach = first.json()["points"][-1]
    assert breach["valid"] is True
    assert breach["mahalanobis"] > sentinel.MAHALANOBIS_BREACH
    assert "above sentinel threshold" in breach["reasoning"]
    assert "Correlation fracture detected." in breach["reasoning"]


def test_pulse_is_stale_without_recent_ticks(client: TestClient) -> None:
    response = client.get("/api/pulse")

    assert response.status_code == 200
    assert response.json()["live"] is False
    assert response.json()["last_tick_age_sec"] is None


def test_telemetry_websocket_receives_event_result(client: TestClient) -> None:
    with client.websocket_connect("/ws/telemetry") as websocket:
        deadline = time.monotonic() + 1.0
        while not api.hub._telemetry and time.monotonic() < deadline:
            time.sleep(0.005)
        assert api.hub._telemetry

        api._telemetry_queue.put((1.0, 100.0, 99.0))
        message = websocket.receive_json()

    assert message["type"] == "telemetry"
    payload = message["payload"]
    assert {
        "level",
        "level_value",
        "health",
        "snapshot",
        "criticality_recent_pct",
        "timestamp",
        "physical_price",
        "token_price",
        "session_event_index",
    } <= payload.keys()
    assert {
        "filtered_basis",
        "innovation",
        "mahalanobis_distance",
        "measurement_valid",
        "breach",
    } == payload["health"].keys()
    assert payload["timestamp"] == 1.0
    assert payload["physical_price"] == 100.0
    assert payload["token_price"] == 99.0
    assert payload["session_event_index"] == 1


def test_demo_injection_is_guarded_and_emits_breach(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disabled = client.post(
        "/api/demo/inject_scenario",
        params={"name": "sudden_depeg"},
    )
    assert disabled.status_code == 404

    monkeypatch.setenv("DYOPS_DEMO_INJECT", "1")
    with client.websocket_connect("/ws/telemetry") as websocket:
        response = client.post(
            "/api/demo/inject_scenario",
            params={"name": "sudden_depeg", "seed": 13},
        )
        assert response.status_code == 202

        for _ in range(response.json()["ticks_queued"]):
            message = websocket.receive_json()
            if message["payload"]["level"] == "BREACH":
                break
        else:
            pytest.fail("sudden_depeg injection did not emit a BREACH telemetry event")
