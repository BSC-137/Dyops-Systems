# Dyops Robustness Evidence Report

Generated at: `2026-07-22T12:27:49.218614+00:00`

## Configuration

- Observer mean-reversion parameter (`theta`): `1.0`
- Mahalanobis breach threshold: `3.0`
- Ring buffer capacity: `1000`
- AUDIT snapshot cooldown: `25` ticks
- Process and measurement noise: production defaults

## Scenario results

| scenario | pass/fail | breach_count | time_to_first_breach | max_mahalanobis | replay_error | processing_ms_per_1k |
|---|---:|---:|---:|---:|---:|---:|
| stable_tracking | PASS | 0 | — | 0.207037 | 0 | 3.395 |
| slow_drift | PASS | 0 | — | 2.07491 | 0 | 3.98495 |
| sudden_depeg | PASS | 15 | 0 | 18.2793 | 0 | 7.77129 |
| gradual_then_break | PASS | 15 | 100 | 16.0067 | 0 | 4.32902 |
| oracle_lag | PASS | 3 | 15 | 4.07722 | 0 | 4.11605 |
| stale_feed | PASS | 0 | — | 0.200326 | 0 | 3.46248 |
| recovery_after_shock | PASS | 15 | 0 | 18.1502 | 0 | 5.03601 |
| fat_tail_noise | PASS | 15 | 0 | 15.6352 | 0 | 4.48051 |

## Summary

8 of 8 deterministic scenarios passed their configured thresholds (0 failed). Known limitations: slow_drift is intentionally silent under the current policy (0 breach ticks), so this pack does not claim slow-drift alarming; oracle_lag is audit-heavy (211 audit ticks), showing that operational lag can sustain escalation even without a fundamental depeg.

These results are deterministic synthetic validation evidence, not production performance guarantees or regulatory attestation. Runtime measurements vary by host.
