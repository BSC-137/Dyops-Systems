"""Contract tests for Peg Health REST, WebSocket additive field, and SQLite replay."""

from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from backend import main as api
from backend.tests.conftest import wait_for_persisted_events
import sentinel
from peg_health import PEG_HEALTH_SCHEMA_VERSION, replay_peg_health


def test_peg_health_empty_then_live_and_openapi(client: TestClient) -> None:
    empty = client.get("/api/peg_health")
    assert empty.status_code == 200
    body = empty.json()
    assert body["schema_version"] == PEG_HEALTH_SCHEMA_VERSION
    assert body["band"] == "Healthy"
    assert body["instrument_id"] == "default"
    assert body["freshness"]["live"] is False
    assert {
        "band",
        "basis",
        "filtered_basis",
        "mahalanobis",
        "criticality",
        "freshness",
        "regime_tag",
        "summary",
        "explainability",
        "last_transition",
        "measurement_valid",
        "level",
        "timestamp",
        "schema_version",
        "instrument_id",
    } == set(body.keys())
    assert "attestation" not in body["explainability"].lower()
    assert "Peg Health" in body["summary"]

    schema = client.get("/openapi.json").json()
    assert "PegHealthResponse" in schema["components"]["schemas"]
    assert "/api/peg_health" in schema["paths"]

    status = client.get("/api/status").json()
    assert status["peg_health_schema_version"] == PEG_HEALTH_SCHEMA_VERSION
    assert status["peg_health_watch_threshold"] == (
        sentinel.MAHALANOBIS_BREACH * 0.5
    )


def test_peg_health_ws_additive_and_replay_parity(client: TestClient) -> None:
    prices = [(float(tick), 100.0, 100.0) for tick in range(25)]
    prices.append((25.0, 100.0, 90.0))

    with client.websocket_connect("/ws/telemetry") as websocket:
        deadline = time.monotonic() + 1.0
        while not api.hub._telemetry and time.monotonic() < deadline:
            time.sleep(0.005)
        assert api.hub._telemetry

        for tick in prices:
            api._telemetry_queue.put(tick)

        # Coalesced fan-out collapses intermediate frames; wait for Breach Peg Health.
        saw_peg = False
        breached = False
        end = time.monotonic() + 3.0
        while time.monotonic() < end and not breached:
            message = websocket.receive_json()
            payload = message["payload"]
            assert "peg_health" in payload
            peg = payload["peg_health"]
            assert peg["schema_version"] == PEG_HEALTH_SCHEMA_VERSION
            assert peg["band"] in {"Healthy", "Watch", "Breach", "Audit"}
            saw_peg = True
            if peg["band"] == "Breach":
                breached = True

    assert saw_peg
    assert breached
    wait_for_persisted_events(client, len(prices))

    live = client.get("/api/peg_health").json()
    assert live["band"] == "Breach"
    assert live["freshness"]["live"] is True
    assert live["last_transition"]["to_band"] == "Breach"
    assert "not a default probability" in live["explainability"].lower()

    assert api._persistence is not None
    rows = api._persistence.load_recent_events(
        api.REPLAY_WINDOW_EVENTS,
        instrument_id="default",
    )
    reconstructed = replay_peg_health(
        rows,
        instrument_id="default",
        breach_threshold=float(sentinel.MAHALANOBIS_BREACH),
        stale_cutoff_sec=api.STALE_CUTOFF_SEC,
        live=True,
        age_sec=0.0,
    )
    assert reconstructed["band"] == live["band"]
    assert abs(reconstructed["mahalanobis"] - live["mahalanobis"]) < 1e-9
    assert abs(reconstructed["criticality"] - live["criticality"]) < 1e-9
    assert reconstructed["last_transition"]["to_band"] == "Breach"


def test_band_transition_webhook_includes_issuer_peg_health(
    client: TestClient,
    monkeypatch,
) -> None:
    import threading

    import httpx
    from backend import webhooks

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
    monkeypatch.setenv("DYOPS_WEBHOOK_URLS", "https://issuer.example/hooks/peg-health")

    prices = [(float(tick), 100.0, 100.0) for tick in range(1, 31)]
    prices.append((31.0, 100.0, 90.0))
    for tick in prices:
        api._telemetry_queue.put(tick)
    wait_for_persisted_events(client, len(prices))
    assert webhook_received.wait(timeout=1.0)

    # Band-transition escalation: one webhook when entering Breach (not every BREACH tick).
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["band"] == "Breach"
    assert payload["level"] == "BREACH"
    assert "peg_health" in payload
    assert payload["peg_health"]["band"] == "Breach"
    assert payload["band_transition"]["to_band"] == "Breach"
    assert payload["peg_health"]["schema_version"] == PEG_HEALTH_SCHEMA_VERSION
