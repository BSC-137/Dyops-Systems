# Dyops Validation Methodology

Dyops is a monitoring and early-warning system for the price relationship between a
physical or reference asset and its tokenized representation. This document explains
the deterministic measurements and escalation rules used by the observer, sentinel,
and robustness scenario suite.

## Measurements

For each timestamp, Dyops receives a physical/reference price and a token price. Prices
must be finite and positive. Invalid measurements are marked as invalid and do not
advance the filter state, filter timestamp, or valid-observation diagnostics ring.
Invalid ticks are therefore tracked by measurement-validity and ingestion counters,
not inserted as zero-surprise samples. They neither increase nor dilute rolling
criticality; an AUDIT window remains elevated through a prolonged invalid-data gap
until enough subsequent valid observations change the window.

The observer transforms valid prices into **log-basis**:

`log_basis = ln(physical_price / token_price)`

Log-basis is approximately proportional to percentage dislocation for small price
differences and treats equivalent relative moves consistently across price levels. A
positive value means the physical price is above the token price; a negative value
means the reverse.

The Rust `BasisObserver` estimates a state containing filtered basis, basis velocity,
and a mean level. Its transition model is a critically damped, mean-reverting process.
The production scenario configuration uses `theta=1.0` and the core's default process
and measurement noise.

The **innovation** is the current observed log-basis minus the model's predicted basis.
It is a residual: large values indicate that the new observation was not well explained
by the immediately preceding state and uncertainty.

The **Mahalanobis distance** scales the innovation by its predicted standard deviation.
It answers how statistically surprising the residual is under the configured model,
rather than reporting price distance alone. Dyops uses a breach threshold of `3.0`.
This is a model-relative signal and should not be interpreted as an unconditional
probability of default, loss, fraud, or depeg.

## Sentinel escalation rules

Every scenario tick is processed through `DyopsSentinel.process_event`, the same policy
path used by live ingestion:

- **MONITORING** is the normal state when the valid tick does not breach the
  Mahalanobis threshold and rolling criticality remains below the audit threshold.
- **BREACH** is emitted for a valid measurement whose Mahalanobis distance is strictly
  greater than `3.0`.
- **AUDIT** is emitted when the percentage of critical observations in the most recent
  100 events is greater than `15%`. AUDIT is the higher-priority level and therefore
  replaces BREACH as the reported level when both conditions apply.

Invalid ticks cannot independently produce BREACH or AUDIT. The scenario suite checks
this property explicitly. AUDIT snapshots include recent innovations, window
statistics, filtered state, criticality, and the most recent breach health where
available.

AUDIT severity and AUDIT side effects are deliberately separate. A sustained critical
window remains at level AUDIT on every tick, preserving the state operators and APIs
see, but snapshots, warning logs, callbacks, and optional Gemini dispatch are emitted
at most once every 25 ticks. A new transition into AUDIT always emits immediately.
This cooldown avoids duplicate evidence and repeated external work without masking the
duration of the elevated state. It is not a claim that the underlying condition has
recovered, and it does not alter BREACH or AUDIT counts.

## Replay-window policy

Startup restoration and forensic history APIs use the same bounded window: the most
recent **1,000 persisted events per instrument**, replayed oldest-first. API callers
may request a smaller forensic suffix, but cannot request more than 1,000 events.
The bound matches the observer diagnostics-ring capacity, avoids unbounded startup,
and makes restored live observer state agree with forensic replay for the same window.
Transient side-effect state such as an in-flight webhook task is not replayed.

## Demo telemetry boundary

Scenario injection is synthetic, disabled unless `DYOPS_DEMO_INJECT=1`, and protected
by `DYOPS_DEMO_SECRET`. Injected WebSocket telemetry is additively labeled with
`ingestion_source: "demo"` and `demo_scenario`; live events use
`ingestion_source: "live"`. The network-free deterministic heartbeat uses
`ingestion_source: "offline"`. Source and scenario labels are persisted into forensic
history. Simulated demo and offline events do not dispatch partner escalation webhooks
unless the operator also sets `DYOPS_DEMO_WEBHOOKS=1`; that explicit option exists
only for integration demonstrations.
The deterministic scenario report remains separate from optional Gemini output and
must not be presented as observed market evidence.

## Scenario evidence

The catalog uses fixed random seeds and labeled synthetic regimes: stable tracking,
slow drift, sudden and combined depegs, oracle lag, stale data, recovery, and heavy-tail
stress. Each scenario defines its own acceptance thresholds. The runner records every
tick, computes detection and quality metrics, and checks serial sentinel output against
the Rust observer's batch replay. The default permitted maximum Mahalanobis replay
error is `1e-12`.

Detection latency is measured from the labeled drift, shock, or anomaly-window start.
False-positive rate uses valid pre-anomaly ticks. Precision at breach is the fraction of
reported BREACH ticks inside the labeled anomaly window. Runtime per 1,000 ticks is
reported as operational context, but is host-dependent and is not a fixed service-level
guarantee.

The current threshold set intentionally records two limitations. `slow_drift` does not
breach under the present production observer settings and remains gated to zero
breaches. **TODO (Option B):** require a breach if the product policy is changed to
promise slow-drift alarms; no such change is made by the current thresholds.

`oracle_lag` permits transient breaches, requires the first post-lag breach within 20
ticks, and caps full-run AUDIT occupancy at `90%`. The cap is intentionally close to
the current deterministic result because operational lag is audit-heavy; it establishes
a regression bound rather than claiming that the behavior is already optimal.
`fat_tail_noise` requires at least one breach from tick 92 onward and zero pre-window
false-positive rate. Together these gates distinguish tolerated operational lag from
the required response to labeled heavy-tail stress.

## Deterministic and optional components

The following evidence-pack components are deterministic for the same code, parameters,
and scenario seed:

- synthetic price streams and anomaly labels;
- Rust observer state updates and batch replay;
- Mahalanobis breach and rolling criticality rules;
- threshold evaluation and all metrics except wall-clock processing time;
- audit snapshot construction.

Gemini is optional and is not invoked by the robustness suite. In production, the
`AgenticAuditor` may send an AUDIT snapshot to Gemini to produce a narrative,
structured risk assessment. That model output can vary and is not part of scenario
pass/fail status. The deterministic MONITORING, BREACH, and AUDIT classification does
not depend on Gemini.

## Explicit non-claims

This evidence pack is engineering validation against synthetic scenarios. It is not:

- a regulatory attestation, audit opinion, certification, or signed compliance report;
- proof of SOC 2, ISO, prudential, market-risk, or financial-reporting compliance;
- a guarantee that all depegs, data failures, manipulation, or tail events will be
  detected;
- a substitute for independent model validation, production load testing, legal
  review, or a partner's own risk controls;
- investment, legal, accounting, or regulatory advice.

Partners should calibrate thresholds with representative market data, define response
procedures, and independently validate integration behavior before relying on Dyops in
production.

## Reproducing the evidence

With the project virtual environment active and the PyO3 extension built, run from the
repository root:

```bash
python scripts/generate_robustness_report.py
```

This writes `reports/robustness_report.json` and
`reports/robustness_report.md`. Run the threshold-gated catalog directly with:

```bash
cd dyops_core
python -m scenarios.run --all
```
