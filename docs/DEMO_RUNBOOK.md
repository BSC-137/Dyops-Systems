# Dyops 8–10 Minute Technical Partner Demo

This runbook is designed for a technically literate investor, product leader, or
integration partner. The deterministic classification and forensic trace do not
depend on Gemini.

## Prerequisites and preflight

- Docker Engine/Desktop with the Compose plugin
- `curl`
- Ports `8000` and `8080` available
- Optional: Python 3 for the local webhook receiver

From the repository root:

```bash
docker compose version
curl --version
docker compose config >/dev/null
```

Pre-build before the meeting:

```bash
docker compose build
```

For a repeatable first run, remove previous demo history:

```bash
docker compose down -v
```

## Start paths

### Live market path

```bash
./scripts/demo.sh live
```

This attempts the Binance public feed. Open:

- UI: `http://localhost:8080`
- OpenAPI: `http://localhost:8000/docs`
- Status: `http://localhost:8000/api/status`

Expect the source badge to read **MARKET · LIVE** when ticks are current. The stable
preset uses a notional USD reference and the Binance USDC/USDT token price, so normal
movement is intentionally quiet.

### Fully offline deterministic fallback

```bash
docker compose down -v
./scripts/demo.sh offline
```

No external market or Gemini access is needed. The API emits a deterministic healthy
stream every 250 ms. Expect **SIMULATED · OFFLINE** and a current freshness age. This
path exercises the same observer, sentinel, persistence, REST, and WebSocket pipeline.

## Presenter script

### Opening — 0:00–0:45

“Dyops is an embeddable monitoring layer for the price relationship between a
reference asset and its tokenized representation. The core product is deterministic:
Rust state estimation, explicit escalation rules, SQLite replay, REST, WebSockets, and
webhooks. Gemini is optional narrative enrichment.”

Point to the source badge, current instrument, freshness age, and Gemini
**OPTIONAL · NOT CONFIGURED** state.

### Act I: normal tracking — 0:45–3:00

Stay on **Live**.

1. Point to **System pulse**, source, instrument, and event count.
2. Explain:
   - **Filtered basis:** estimated log price relationship.
   - **Innovation:** new residual versus the predicted relationship.
   - **Mahalanobis:** residual normalized by model uncertainty; not a probability.
3. Show `/api/pulse` or `/docs` briefly to establish the integration surface.

Expected visuals: MONITORING, low criticality, measured and filtered lines tracking,
Mahalanobis generally below `3.0`.

### Act II: controlled dislocation — 3:00–5:30

Click **Demo: inject sudden depeg** and enter the printed secret
(`dyops-local-demo` by default), or run:

```bash
curl -X POST \
  -H 'X-Dyops-Demo-Secret: dyops-local-demo' \
  'http://localhost:8000/api/demo/inject_scenario?name=sudden_depeg&seed=13'
```

Expected timing and visuals:

- `202 Accepted` immediately; 240 deterministic ticks are queued.
- Roughly 0–2 seconds: stable warm-up drains.
- Roughly 2–7 seconds: two-percent step becomes visible.
- Source changes to **SIMULATED · SUDDEN_DEPEG**.
- Mahalanobis crosses the `3.0` line and classification moves through BREACH/AUDIT.
- Criticality rises; the Structural Drift Audit summary refreshes automatically.

Say: “This classification is produced by the deterministic observer and policy. The
scenario is visibly labeled and is not market evidence.”

### Act III: reconstruction and integration — 5:30–8:30

Click **Review incidents** or the **Incidents** tab.

1. Select the newest incident (automatically selected after refresh).
2. Show the source badge, time window, peak Mahalanobis, and criticality.
3. Scroll the per-tick deterministic reasoning.
4. Click **Export JSON**. Compare its shape with
   `examples/incident-export.json`.
5. Open `http://localhost:8000/docs` and point to history, trace, status, pulse, and
   demo routes.
6. If the webhook receiver is configured, show the terminal payload.

Closing: “The UI is a reference client. Partners can consume the same classifications
through REST, WebSockets, or escalation webhooks.”

## Webhook proof

Terminal A:

```bash
python3 scripts/webhook_receiver.py 9999
```

Terminal B, explicit demo-only webhook opt-in:

```bash
docker compose down -v
DYOPS_DEMO_WEBHOOKS=1 \
DYOPS_WEBHOOK_URLS=http://host.docker.internal:9999/dyops \
./scripts/demo.sh offline
```

Inject the scenario. The receiver prints the JSON escalation. Demo webhooks remain
disabled unless `DYOPS_DEMO_WEBHOOKS=1`; this prevents accidental partner callbacks.

## Reset, second run, stop

Reset one instrument without restarting:

```bash
curl -X POST \
  -H 'X-Dyops-Demo-Secret: dyops-local-demo' \
  'http://localhost:8000/api/demo/reset'
```

Wait for the offline/market heartbeat to return, then inject again. A full clean reset:

```bash
docker compose down -v
./scripts/demo.sh offline
```

Stop while preserving SQLite:

```bash
docker compose down
```

## Recovery and fallback plan

| Failure | What the audience sees | Presenter action |
|---|---|---|
| Binance unavailable | MARKET · STALE after 12 seconds | Switch to `docker compose down -v && ./scripts/demo.sh offline`; state that source is simulated |
| Gemini unavailable | OPTIONAL · NOT CONFIGURED | Continue; pulse, classification, trace, incidents, and exports are deterministic |
| Telemetry WebSocket drops | Reconnecting banner; REST pulse continues | Wait for capped reconnect or reload once; show `/api/pulse` meanwhile |
| Audit WebSocket drops | Optional narrative tail stops | Deterministic trace and incident reconstruction remain available |
| Injection returns 401 | No scenario starts | Re-enter the secret printed by `demo.sh` or use curl |
| Injection returns 409 | Existing scenario is active | Wait about 7 seconds, or call `/api/demo/reset` |
| Incident does not appear promptly | Live chart moved, trace still settling | Wait up to 2 seconds; the UI retries persisted history/trace refresh three times |
| Ports occupied | Containers fail to bind | Stop the conflicting service or edit Compose port mappings before the meeting |

## Claims boundary

- The robustness report is deterministic synthetic regression evidence.
- The historical harness currently uses a legal synthetic fixture.
- Slow drift is a documented current limitation.
- Mahalanobis is model-relative normalized surprise, not default probability.
- Incident export is an unsigned forensic JSON bundle, not a regulatory report.
