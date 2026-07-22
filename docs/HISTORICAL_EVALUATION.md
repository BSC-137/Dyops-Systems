# Historical Evaluation Harness

This harness asks whether Dyops adds operational value beyond transparent basis and
rolling-anomaly rules. It provides measurement infrastructure; it does **not** change
production defaults or turn synthetic scenarios into market validation.

## Dataset contract (schema version 1)

CSV and Parquet inputs use one row per observation:

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | Must be `1` |
| `dataset_id` | string | Stable identifier shared by all rows |
| `timestamp` | number | Unix seconds; strictly increasing per instrument |
| `instrument_id` | string | Vendor-neutral paired-instrument identifier |
| `physical_price` | number | Physical/reference price |
| `token_price` | number | Token price |
| `source` | string | Dataset/source lineage |
| `sampling_interval_sec` | number | Expected cadence at this row |
| `event_label` | optional string | Coarse row label; catalog windows remain authoritative |

Validation reports duplicate/non-monotonic timestamps, missing samples, source changes
or gaps, invalid cadence, and non-positive/non-finite prices. CSV requires no optional
dependency. Parquet support is enabled when `pyarrow` is installed; the detector engine
does not depend on a vendor SDK or pandas.

Licensed downloads belong under `dyops_core/historical_eval/datasets/raw/`, which is
gitignored. Do not commit data unless its license and redistribution terms allow it.
Private label manifests can remain under the gitignored `manifests/private/` path.

## Event catalog

The versioned JSON manifest records stable periods, dislocations, oracle/data failures,
and recoveries. Every label includes:

- tuning or held-out split;
- provenance;
- uncertainty in seconds;
- confidence and notes.

Approximate windows remain approximate. Metrics expand a window by its recorded
uncertainty rather than manufacturing a precise onset. Calibration receives a
tuning-only catalog and observations no later than the last tuning window. Held-out
labels and post-cutoff rows cannot influence parameter selection.

## Comparable detectors

All detectors consume the same causal observation sequence and emit `DetectionTick`
plus merged `AlertEvent` records:

- fixed absolute log-basis threshold;
- causal rolling z-score;
- causal EWMA z-score;
- rolling median/MAD robust score;
- two-sided standardized CUSUM;
- explicit short/long-window slow-drift detector;
- Dyops observer-only;
- Dyops observer plus rolling criticality (current policy).

Invalid measurements never update detector state. Dyops preserves an already-active
AUDIT state through invalid ticks, matching production policy; invalid data does not
independently create a new breach.

## Metrics and ablations

Primary metrics are event-level recall, labelled-window precision, false alert events
per instrument-day, detection latency in seconds/ticks, basis at detection in basis
points, recovery latency, and alert duration. Per-tick imbalance is not used as the
sole accuracy measure.

The result also includes sampling-stride and deterministic missing-data sensitivity,
production observer-only versus criticality, global versus per-instrument calibration,
replay warm-up sizes, and current policy versus the slow-drift detector. Runtime and
peak `tracemalloc` memory are supporting diagnostics only.

Calibration performs a small deterministic grid over interpretable baseline parameters
and Dyops `theta`, process-noise scale (`Q`), measurement noise (`R`), Mahalanobis and
criticality thresholds. It maximizes tuning-event recall, then minimizes tuning false
alerts and latency. Production defaults remain untouched. Held-out event bootstrap
ranges use a fixed seed and do not re-tune.

## Reproducible commands

From `dyops_core/` with the PyO3 extension installed:

```bash
python -m historical_eval.cli validate \
  --dataset historical_eval/fixtures/synthetic_reference.csv \
  --catalog historical_eval/manifests/synthetic_reference.events.json
```

Calibration-only artifact:

```bash
python -m historical_eval.cli calibrate \
  --dataset historical_eval/fixtures/synthetic_reference.csv \
  --catalog historical_eval/manifests/synthetic_reference.events.json \
  --json-output ../reports/historical_calibration.json
```

Full comparison:

```bash
python -m historical_eval.cli evaluate \
  --dataset historical_eval/fixtures/synthetic_reference.csv \
  --catalog historical_eval/manifests/synthetic_reference.events.json \
  --json-output ../reports/historical_evaluation.json \
  --markdown-output ../reports/historical_evaluation.md
```

Replace the fixture and manifest paths with licensed historical inputs; do not alter
the held-out split after viewing results without versioning a new catalog.

## Evidence boundary

`scenarios/` and the committed `synthetic_reference.csv` are deterministic regression
evidence. They prove replay, metric, calibration-isolation, and detector-comparison
behavior. They do not prove performance on real dislocations.

A defensible historical recommendation requires representative licensed data, multiple
instruments and market regimes, provenance-backed labels, enough independent held-out
events for useful bootstrap ranges, and review of source-specific gaps. The generated
report states this limitation and confines each baseline verdict to the evaluated
fixture.
