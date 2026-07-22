# Dyops Historical Evaluation Harness Report

Generated at: `2026-07-22T12:52:02.431104+00:00`

## Evidence status

**This run uses a legal synthetic fixture for harness regression. It is not historical market validation and cannot answer the primary product question conclusively.** Replace the fixture with licensed/vendor-neutral historical CSV or Parquet data and a provenance-backed catalog before making production threshold changes.

- Dataset: `dyops-synthetic-reference-v1` (79 rows)
- Tuning events: `tuning-stable-01, tuning-oracle-failure-01, tuning-data-gap-01, tuning-dislocation-01, tuning-recovery-01`
- Held-out events: `heldout-stable-01, heldout-slow-drift-01, heldout-dislocation-01, heldout-recovery-01`

## Held-out detector comparison

| detector | event recall | window precision | false alerts / instrument-day | latency sec | latency ticks | basis at detection bps | recovery sec | alert duration sec |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| absolute_basis | 1.000 | 1.000 | 0.000 | 120.0 | 2.0 | 10.0 | 660.0 | 1320.0 |
| rolling_z | 1.000 | 1.000 | 0.000 | 120.0 | 2.0 | 106.0 | 90.0 | 240.0 |
| ewma_z | 1.000 | 1.000 | 0.000 | 300.0 | 5.0 | 202.0 | 60.0 | 120.0 |
| rolling_mad | 1.000 | 0.333 | 48.814 | 0.0 | 0.0 | 2.0 | 300.0 | 580.0 |
| cusum | 1.000 | 1.000 | 0.000 | 0.0 | 0.0 | 0.0 | 900.0 | 2400.0 |
| slow_drift | 1.000 | 1.000 | 0.000 | 0.0 | 0.0 | 5.0 | 900.0 | 1050.0 |
| dyops_observer_only | 1.000 | 0.667 | 24.407 | 0.0 | 0.0 | 101.0 | 420.0 | 600.0 |
| dyops_current | 1.000 | 1.000 | 0.000 | 0.0 | 0.0 | 0.0 | 900.0 | 2400.0 |
| dyops_calibrated_global | 1.000 | 1.000 | 0.000 | 300.0 | 5.0 | 202.0 | 900.0 | 1200.0 |
| dyops_calibrated_per_instrument | 1.000 | 1.000 | 0.000 | 300.0 | 5.0 | 202.0 | 900.0 | 1200.0 |

Event windows include their recorded uncertainty. Runtime and memory are available in the JSON as supporting diagnostics, not primary ranking metrics.

## Recommendation

- **absolute_basis:** `dyops_does_not_beat_baseline_on_this_fixture` — held-out event recall 1.0 vs 1.0; false alerts/instrument-day 0.0 vs 0.0; mean alert duration 2400.0s vs 1320.0s.
- **rolling_z:** `dyops_does_not_beat_baseline_on_this_fixture` — held-out event recall 1.0 vs 1.0; false alerts/instrument-day 0.0 vs 0.0; mean alert duration 2400.0s vs 240.0s.
- **ewma_z:** `dyops_does_not_beat_baseline_on_this_fixture` — held-out event recall 1.0 vs 1.0; false alerts/instrument-day 0.0 vs 0.0; mean alert duration 2400.0s vs 120.0s.
- **rolling_mad:** `no_overall_dyops_advantage_demonstrated` — held-out event recall 1.0 vs 1.0; false alerts/instrument-day 0.0 vs 48.813559322033896; mean alert duration 2400.0s vs 580.0s.
- **cusum:** `no_overall_dyops_advantage_demonstrated` — held-out event recall 1.0 vs 1.0; false alerts/instrument-day 0.0 vs 0.0; mean alert duration 2400.0s vs 2400.0s.
- **slow_drift:** `dyops_does_not_beat_baseline_on_this_fixture` — held-out event recall 1.0 vs 1.0; false alerts/instrument-day 0.0 vs 0.0; mean alert duration 2400.0s vs 1050.0s.

These verdicts apply only to this held-out synthetic fixture. A tie or win here does not establish operational value on market history.

## Ablations included

- Dyops observer-only versus observer plus rolling criticality.
- Production parameters versus globally and per-instrument calibrated parameters.
- Replay warm-up sizes of 0, 10, 20, and 40 events.
- Current Dyops policy versus an explicit slow-drift detector.
- Sampling strides 2/3 and deterministic 10% missing-observation sensitivity.

## Data and label limitations

- This committed fixture is synthetic regression data, not real-world validation.
- Calibration reads tuning events only; held-out labels and post-cutoff rows are excluded.
- Bootstrap ranges resample held-out labelled events without re-tuning.
- Runtime and tracemalloc values are supporting host-dependent diagnostics.
- Approximate labels are expanded by their recorded uncertainty and are not precise ground truth.
- The committed fixture has one instrument, so global and per-instrument calibration are expected to coincide.
- Catalog: All rows and labels are generated synthetic fixtures for legal regression testing.
- Catalog: The slow-drift onset is intentionally approximate and carries a two-minute uncertainty.
- Catalog: The data-gap label identifies an absent sample; no detector can alert during a timestamp that is not observed.
- Catalog: This catalog must not be described as historical market evidence.
