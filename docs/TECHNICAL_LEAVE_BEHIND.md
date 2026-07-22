# Dyops Technical Leave-Behind

## Business problem

Tokenized assets, stable pairs, wrapped assets, and LSTs can separate from their
reference relationship because of liquidity stress, oracle/data faults, or fundamental
events. Product and risk teams need a continuous signal and reconstructable evidence,
not only a raw percentage threshold or an occasional report.

## Technical approach

```text
Market or deterministic feed
        │
        ▼
Thread-safe ingestion queue ──► Rust BasisObserver ──► Sentinel policy
                                      │                    │
                                      ▼                    ▼
                              filtered basis         BREACH / AUDIT
                                      │                    │
                                      └────► SQLite ◄──────┘
                                                │
                                      REST / WebSockets / webhooks
```

- **Basis:** `ln(reference_price / token_price)`.
- **Filtered basis:** state-space estimate of the current relationship.
- **Innovation:** residual between the new observation and prediction.
- **Mahalanobis distance:** innovation normalized by predicted model uncertainty.
- **BREACH:** valid measurement with Mahalanobis strictly above `3.0`.
- **AUDIT:** more than `15%` critical observations in the most recent `100` valid
  observations; snapshot side effects are cooldown-gated.

Mahalanobis is a model-relative statistic, not an unconditional probability of default,
loss, fraud, or depeg.

## Differentiation

- Rust hot path with Joseph-form covariance update and deterministic replay.
- Invalid measurements do not mutate filter state or dilute rolling criticality.
- Current classification does not require Gemini.
- SQLite-backed history and per-tick reasoning support incident reconstruction.
- Source labels distinguish Binance market telemetry, deterministic offline telemetry,
  and injected scenarios.
- Multi-instrument REST/WebSocket surface with optional outbound escalation webhooks.

## Integration

| Surface | Purpose |
|---|---|
| `GET /api/status` | Readiness, thresholds, queues, persistence and source mode |
| `GET /api/instruments` | Instrument metadata, freshness and current level |
| `GET /api/pulse` | Current freshness and concise deterministic explanation |
| `GET /api/history` | Chart-compatible replay |
| `GET /api/history/trace` | Per-tick deterministic forensic reasoning |
| `/ws/telemetry` | Live classifications and source labels |
| `/ws/audits` | Optional stored narrative audit tail |
| `DYOPS_WEBHOOK_URLS` | BREACH/AUDIT escalation JSON |

OpenAPI is served at `/docs`. WebSocket and webhook examples are in
[`INTEGRATION_EXAMPLES.md`](INTEGRATION_EXAMPLES.md).

## Evidence

- 8/8 deterministic scenario gates currently pass.
- Serial and batch replay are checked to `1e-12` maximum absolute error.
- Rust, Python, API contracts, frontend build/lint, and Compose smoke are CI gates.
- A vendor-neutral comparison harness covers transparent baseline detectors,
  calibration leakage controls, event metrics, and ablations.

This evidence is primarily synthetic regression evidence. The committed historical
fixture is synthetic and does not establish real-market superiority.

## Known limitations

- Current production settings do not promise slow-drift detection.
- Oracle lag can sustain AUDIT occupancy.
- Sentinel cooldown phase is not persisted across process restart.
- Demo and offline streams are simulated and visibly labeled.
- Gemini output is optional, variable narrative—not classification evidence.
- Incident export is unsigned JSON, not certification or regulatory attestation.
- Representative licensed market history and independent model validation are still
  required before production reliance.

## Fast evaluation

```bash
docker compose build
docker compose down -v
./scripts/demo.sh offline
```

Open `http://localhost:8080`, inject the sudden-depeg scenario, review **Incidents**,
and export JSON. See [`DEMO_RUNBOOK.md`](DEMO_RUNBOOK.md) for the complete 8–10 minute
presenter sequence.
