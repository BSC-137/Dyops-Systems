# Dyops Integration Examples

## Peg Health (issuer primary object)

Peg Health is the adopt-able product noun for stablecoin / RWA / token issuers.
Wire against this surface — you do not need Kalman / observer internals.

Interactive OpenAPI:

```text
http://localhost:8000/docs
```

Poll current Peg Health:

```bash
curl -sS 'http://localhost:8000/api/peg_health?instrument=default' | python3 -m json.tool
```

Representative Peg Health object (`schema_version: "1.0"`):

```json
{
  "schema_version": "1.0",
  "instrument_id": "default",
  "timestamp": 1753185600.0,
  "band": "Breach",
  "basis": 0.0202027,
  "filtered_basis": 0.0029,
  "mahalanobis": 18.2793,
  "criticality": 12.0,
  "freshness": {
    "live": true,
    "age_sec": 0.2,
    "stale_cutoff_sec": 12.0
  },
  "regime_tag": "correlation_fracture",
  "summary": "Peg Health Breach · Mahalanobis 18.28 above 3 · crit 12.0%.",
  "explainability": "Normalized innovation exceeded the sentinel breach threshold — operational correlation fracture signal, not a default probability.",
  "last_transition": {
    "from_band": "Healthy",
    "to_band": "Breach",
    "at": 1753185600.0
  },
  "measurement_valid": true,
  "level": "BREACH"
}
```

Bands: `Healthy` → `Watch` → `Breach` → `Audit`. Gemini never alters Peg Health.
Bands are operational classification only — not attestation or default probability.

Subscribe (additive `peg_health` on every telemetry frame):

```bash
websocat 'ws://localhost:8000/ws/telemetry?instrument=default'
```

Escalate on **band transitions** via webhook (see below).

## OpenAPI and REST

Preflight:

```bash
curl -sS http://localhost:8000/api/status | python3 -m json.tool
curl -sS http://localhost:8000/api/instruments | python3 -m json.tool
curl -sS http://localhost:8000/api/pulse | python3 -m json.tool
curl -sS http://localhost:8000/api/peg_health | python3 -m json.tool
curl -sS 'http://localhost:8000/api/history/trace?limit=5' | python3 -m json.tool
```

Status distinguishes `feed_source` (`binance_market` or
`offline_deterministic`; `feed_disabled` when no producer threads start) and reports
whether demo injection, webhooks, persistence, and Gemini key/local initialization
are available. `gemini_ready` is not an endpoint reachability probe.
`peg_health_schema_version` and `peg_health_watch_threshold` document the Peg Health
contract.

## Telemetry WebSocket

Using `websocat`:

```bash
websocat ws://localhost:8000/ws/telemetry
```

Instrument-scoped:

```bash
websocat 'ws://localhost:8000/ws/telemetry?instrument=default'
```

Representative payload (additive `peg_health`; legacy `health` retained):

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
    "peg_health": {
      "schema_version": "1.0",
      "instrument_id": "default",
      "timestamp": 1753185600.0,
      "band": "Breach",
      "basis": 0.0202027,
      "filtered_basis": 0.0029,
      "mahalanobis": 18.2793,
      "criticality": 12.0,
      "freshness": {
        "live": true,
        "age_sec": 0.0,
        "stale_cutoff_sec": 12.0
      },
      "regime_tag": "correlation_fracture",
      "summary": "Peg Health Breach · Mahalanobis 18.28 above 3 · crit 12.0%.",
      "explainability": "Normalized innovation exceeded the sentinel breach threshold — operational correlation fracture signal, not a default probability.",
      "last_transition": {
        "from_band": "Healthy",
        "to_band": "Breach",
        "at": 1753185600.0
      },
      "measurement_valid": true,
      "level": "BREACH"
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
`GET /api/history/trace` and does not depend on this socket. Peg Health is never
populated by Gemini.

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

## Webhook receiver (issuer-style Peg Health escalation)

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

Webhooks fire on **Peg Health band transitions** (not every BREACH tick). Demo
webhooks additionally require `DYOPS_DEMO_WEBHOOKS=1`.

Issuer-style webhook payload:

```json
{
  "timestamp": 1753185600.0,
  "level": "BREACH",
  "band": "Breach",
  "band_transition": {
    "from_band": "Healthy",
    "to_band": "Breach",
    "at": 1753185600.0
  },
  "mahalanobis": 18.2793,
  "innovation": 0.0201,
  "criticality_recent_pct": 12.0,
  "instrument_id": "default",
  "ingestion_source": "demo",
  "summary": "Peg Health Breach · Mahalanobis 18.28 above 3 · crit 12.0%.",
  "explainability": "Normalized innovation exceeded the sentinel breach threshold — operational correlation fracture signal, not a default probability.",
  "peg_health": {
    "schema_version": "1.0",
    "instrument_id": "default",
    "timestamp": 1753185600.0,
    "band": "Breach",
    "basis": 0.0202027,
    "filtered_basis": 0.0029,
    "mahalanobis": 18.2793,
    "criticality": 12.0,
    "freshness": {
      "live": true,
      "age_sec": 0.0,
      "stale_cutoff_sec": 12.0
    },
    "regime_tag": "correlation_fracture",
    "summary": "Peg Health Breach · Mahalanobis 18.28 above 3 · crit 12.0%.",
    "explainability": "Normalized innovation exceeded the sentinel breach threshold — operational correlation fracture signal, not a default probability.",
    "last_transition": {
      "from_band": "Healthy",
      "to_band": "Breach",
      "at": 1753185600.0
    },
    "measurement_valid": true,
    "level": "BREACH"
  }
}
```

Partner pattern: poll `GET /api/peg_health` on a timer **or** subscribe to
`/ws/telemetry` and escalate when `peg_health.band` changes (or when the webhook
arrives with `band_transition`).

## Incident export

The React **Export JSON** action produces an unsigned forensic bundle containing a
**Peg Health snapshot**, source labels, scenario names, software/schema version,
deterministic trace points, and a separate optional-LLM section. Schema `2.1`.
`content_sha256` covers canonical JSON without the hash field when Web Crypto is
available. It is comparison integrity, not a digital signature or legal seal.
See [`../examples/incident-export.json`](../examples/incident-export.json).
