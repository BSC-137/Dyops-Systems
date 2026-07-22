# Dyops Hot-Path Performance Budget

Status: Phase A baseline captured before algorithm or hot-path behavior changes.

## Measurement contract

- Host: WSL2, Linux 5.15, 12th Gen Intel Core i7-12700H
- Revision: `a3dc8b4` plus the measurement harness only
- Rust: `cargo bench` optimized profile with release LTO
- Python: CPython 3.10.12 and `maturin develop --release`
- Inputs: deterministic valid price pairs, constant 1 ms timestamp spacing
- Warm-up: 10,000 ticks
- Rust monitoring cases: 1,000,000 measured ticks
- Rust audit snapshot case: 100,000 measured ticks
- Python no-persistence case: 200,000 measured ticks
- Python persistence case: 20,000 measured ticks, including close/drain timing

These are engineering budgets, not cross-host SLAs. WSL scheduling, CPU frequency, and
thermal state are uncontrolled. Compare before/after on the same host and release build.

## Baseline and target

| Stage | Baseline | Initial target | Budget rationale |
|---|---:|---:|---|
| Rust observer update, ring disabled | 63.782 ns/tick | ≤50 ns | Fixed 3×3 math only |
| Rust observer update, ring 1,000 | 67.446 ns/tick | ≤55 ns | O(1) diagnostics append |
| Rust sentinel MONITORING | 86.926 ns/tick | ≤65 ns | No allocation; O(1) criticality |
| Rust sentinel AUDIT snapshot tick | 2,474.076 ns/tick | ≤1,600 ns | Snapshot-only diagnostics pass |
| Python `DyopsSentinel`, persistence off | 1,415.928 ns/tick | ≤1,000 ns | Compact PyO3 ABI and less wrapper work |
| Python persistence enqueue | 4,002.821 ns/tick | ≤2,500 ns | Queue-only producer work |
| Python persistence enqueue + durable close/drain | 75,170.069 ns/tick | ≤15,000 ns | Batched SQLite transaction drain |

The baseline is one deterministic harness run. After values use three runs and report
the median; future comparisons should capture three baseline runs as well.

## After optimization

After values are medians of three release runs on the same host.

| Stage | Before | After | Delta | Target |
|---|---:|---:|---:|---:|
| Rust observer, ring disabled | 63.782 ns | 40.911 ns | -35.9% | pass |
| Rust observer, ring 1,000 | 67.446 ns | 42.474 ns | -37.0% | pass |
| Rust sentinel MONITORING | 86.926 ns | 50.634 ns | -41.8% | pass |
| Rust sentinel AUDIT snapshot | 2,474.076 ns | 1,134.728 ns | -54.1% | pass |
| Python sentinel, persistence off | 1,415.928 ns | 909.658 ns | -35.8% | pass |
| Python persistence enqueue | 4,002.821 ns | 1,588.886 ns | -60.3% | pass |
| Python durable close/drain | 75,170.069 ns | 6,495.648 ns | -91.4% | pass |

The allocation counter reports zero allocations across 1,000,000 measured Rust
MONITORING ticks, both with and without the diagnostics ring. An AUDIT snapshot tick
performs one allocation for its exported innovation vector.

Final median throughput:

| Stage | Throughput |
|---|---:|
| Rust observer, ring off | 24.44 million ticks/s |
| Rust observer, ring on | 23.54 million ticks/s |
| Rust sentinel MONITORING | 19.75 million ticks/s |
| Rust AUDIT snapshot every tick | 0.881 million ticks/s |
| Python sentinel, persistence off | 1.10 million ticks/s |
| Python persistence enqueue | 0.629 million ticks/s |
| Python durable drain | 153.95 thousand ticks/s |

The 100,000-tick historical evaluator comparison preserved exact `DetectionTick`
parity and improved from 3,517.35 ns/tick serial to 3,440.22 ns/tick batched (1.022×).
Object construction dominates that evaluator after the Rust batch crossing. The
observer-only 1,000,000-tick benchmark measured 0.403 s serial versus 0.050 s batch
(8.1×).

## Throughput equivalents

| Stage | Baseline throughput |
|---|---:|
| Rust observer, ring off | 15.68 million ticks/s |
| Rust observer, ring on | 14.83 million ticks/s |
| Rust sentinel MONITORING | 11.50 million ticks/s |
| Rust AUDIT snapshot every tick | 0.404 million ticks/s |
| Python sentinel, persistence off | 0.706 million ticks/s |
| Python persistence enqueue | 0.250 million ticks/s |
| Python durable drain | 13.30 thousand ticks/s |

## Sampling profile

`py-spy` sampled a 1,000,000-tick Python replay at 1,000 Hz. The speedscope artifact is
`reports/perf/baseline_replay.speedscope.json`; the flamegraph is
`reports/perf/baseline_replay.svg`.

Top visible Python leaf frames:

1. `DyopsSentinel.process_event` at the PyO3 call: 18.47%
2. `EventResult` construction/return: 7.88%
3. benchmark result field access: 7.84%
4. `SentinelLevel[...]` enum lookup: 3.84%
5. non-snapshot branch handling: 2.88%

Native Rust execution is attributed to the calling Python frame by `py-spy`. Source
inspection identifies the Rust MONITORING delta as the 100-sample
`criticality_recent` scan; AUDIT snapshot ticks additionally scan the 1,000-sample ring
for statistics/full criticality and allocate the innovation vector.

The optimized 1M-tick profile (`after_replay.speedscope.json`, 2,296 samples, zero
errors) attributes 9.51% to the compact PyO3 call and 9.93% to Python result
construction/return. No diagnostic ring scan remains on MONITORING ticks.

## Product budgets

- Rust MONITORING must remain heap-allocation-free after observer construction.
- Valid-tick diagnostics and rolling criticality must be O(1).
- Window statistics and kurtosis must not run on MONITORING ticks.
- The persistence producer must remain non-blocking; durable batching must preserve
  FIFO order and `close()` drain semantics.
- API status/pulse must not execute SQL `COUNT(*)` in their request path.
- Frontend chart rendering is capped at 20 paints/s and must not call React state
  setters for every WebSocket packet.
- Detection thresholds, strict comparisons, invalid-tick behavior, replay parity, and
  audit cooldown semantics are outside the performance budget and may not be weakened.

## Reproduce on WSL without Docker

```bash
cd dyops_core
.venv/bin/maturin develop --release
cargo bench --bench tick_hotpath
.venv/bin/python bench_sentinel_tick.py \
  --ticks 200000 \
  --persistence-ticks 20000 \
  --json-output ../reports/perf/python_latest.json
.venv/bin/python bench_historical_eval.py \
  --ticks 100000 \
  --json-output ../reports/perf/historical_eval_latest.json
```

Optional 1M-tick sampling profile:

```bash
cd dyops_core
.venv/bin/pip install py-spy
.venv/bin/py-spy record --format speedscope --rate 1000 \
  --output ../reports/perf/replay_latest.speedscope.json -- \
  .venv/bin/python bench_sentinel_tick.py \
  --ticks 1000000 --persistence-ticks 1 --warmup 10000
```

`py-spy 0.4.2` can return exit code 1 under WSL after successfully writing the profile
because its child has already exited (`No child process`). Accept the run only when the
artifact is non-empty and the profiler reports zero sampling errors.

Correctness and end-to-end verification:

```bash
cd dyops_core
cargo test --release --locked
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m scenarios.run --all --quiet --strict
.venv/bin/python -m historical_eval.cli evaluate \
  --dataset historical_eval/fixtures/synthetic_reference.csv \
  --catalog historical_eval/manifests/synthetic_reference.events.json

cd ../backend
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  ../dyops_core/.venv/bin/python -m pytest tests -q

cd ../frontend
npm run lint
npm run build
```

## Machine-readable artifacts

- `reports/perf/baseline_rust.csv`
- `reports/perf/baseline_python.json`
- `reports/perf/baseline_profile_summary.json`
- `reports/perf/baseline_replay.speedscope.json`
- `reports/perf/baseline_replay.svg`
- `reports/perf/after_rust_runs.txt`
- `reports/perf/after_python_run{1,2,3}.json`
- `reports/perf/after_replay.speedscope.json`
- `reports/perf/historical_eval_after.json`

## Remaining bottlenecks

1. Python still creates one `SystemHealth` object and one slotted `EventResult` per live
   tick. This is now larger than the 50.6 ns Rust policy cost.
2. AUDIT export intentionally allocates one innovation vector and serializes a larger
   WebSocket/forensic payload.
3. Historical evaluation spends most time creating rich Python `DetectionTick`
   objects, so the new policy batch produces only a 2.2% end-to-end gain there.
4. Recharts and history/trace reconstruction remain more expensive than the 10 Hz
   frontend paint cadence, but they are outside the ingestion hot path.

## What not to optimize next

- Do not replace Joseph covariance, relax strict threshold comparisons, or reduce
  diagnostic windows for benchmark gains.
- Do not add SIMD crates, Arrow/Polars, a message bus, another database, or a Tokio
  ingestion rewrite.
- Do not optimize the 4 Hz feed thread or the now-cached status counters without a new
  profile showing they are material.
- Do not eliminate the AUDIT innovation export; it is cold-path evidence and now costs
  about 1.1 µs.
