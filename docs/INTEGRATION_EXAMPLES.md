# Dyops Integration Examples

## OpenAPI and REST

Interactive OpenAPI:

```text
http://localhost:8000/docs
```

Preflight:

```bash
curl -sS http://localhost:8000/api/status | python3 -m json.tool
curl -sS http://localhost:8000/api/instruments | python3 -m json.tool
curl -sS http://localhost:8000/api/pulse | python3 -m json.tool
curl -sS 'http://localhost:8000/api/history/trace?limit=5' | python3 -m json.tool
```

Status distinguishes `feed_source` (`binance_market` or
`offline_deterministic`) and reports whether demo injection, webhooks, persistence,
and Gemini configuration are available.

## Telemetry WebSocket

Using `websocat`:

```bash
websocat ws://localhost:8000/ws/telemetry
```

Instrument-scoped:

```bash
websocat 'ws://localhost:8000/ws/telemetry?instrument=default'
```

Representative payload:

```json
{
  "type": "telemetry",
  "payload": {
    "instrument_id": "default",
    "ingestion_source": "demo",
    "demo_scenario": "sudden_depeg",
    "timestamp": 1753185600.0,
    "physical_price": 100.0,
    "token_price": 98.0,
    "level": "BREACH",
    "level_value": 2,
    "criticality_recent_pct": 12.0,
    "health": {
      "filtered_basis": 0.0029,
      "innovation": 0.0201,
      "mahalanobis_distance": 18.2793,
      "measurement_valid": true,
      "breach": true
    },
    "snapshot": null,
    "session_event_index": 121
  }
}
```

`ingestion_source` values:

- `live`: Binance market feed;
- `offline`: deterministic network-free healthy feed;
- `demo`: explicitly injected scenario.

## Audit WebSocket

```bash
websocat ws://localhost:8000/ws/audits
```

This sends up to 50 recent stored audits, then a live tail. Gemini fields are present
only when the optional auditor ran. Deterministic trace reasoning is obtained from
`GET /api/history/trace` and does not depend on this socket.

## Demo control

```bash
curl -X POST \
  -H 'X-Dyops-Demo-Secret: dyops-local-demo' \
  'http://localhost:8000/api/demo/inject_scenario?name=sudden_depeg&seed=13'
```

Reset:

```bash
curl -X POST \
  -H 'X-Dyops-Demo-Secret: dyops-local-demo' \
  http://localhost:8000/api/demo/reset
```

## Webhook receiver

```bash
python3 scripts/webhook_receiver.py 9999
```

Local API:

```bash
DYOPS_WEBHOOK_URLS=http://127.0.0.1:9999/dyops \
uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Docker API with explicit simulated-webhook opt-in:

```bash
DYOPS_DEMO_WEBHOOKS=1 \
DYOPS_WEBHOOK_URLS=http://host.docker.internal:9999/dyops \
./scripts/demo.sh offline
```

Representative webhook:

```json
{
  "timestamp": 1753185600.0,
  "level": "BREACH",
  "mahalanobis": 18.2793,
  "innovation": 0.0201,
  "criticality_recent_pct": 12.0,
  "instrument_id": "default",
  "ingestion_source": "demo",
  "summary": "SIMULATED SCENARIO · session 121 events · 121 persisted in SQLite.",
  "explainability": "Deterministic simulated telemetry exercised the production observer and sentinel path; this payload is not market evidence."
}
```

Demo webhooks are disabled by default. Production/live webhook behavior is unaffected.

## Incident export

The React **Export JSON** action produces an unsigned forensic bundle containing source
labels, scenario names, deterministic trace points, and matching optional audit rows.
See [`../examples/incident-export.json`](../examples/incident-export.json).
