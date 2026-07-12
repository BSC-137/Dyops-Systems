# Dyops Robustness Evidence Report

Generated at: `2026-07-12T16:33:40.978737+00:00`

## Configuration

- Observer mean-reversion parameter (`theta`): `1.0`
- Mahalanobis breach threshold: `3.0`
- Ring buffer capacity: `1000`
- Process and measurement noise: production defaults

## Scenario results

| scenario | pass/fail | breach_count | time_to_first_breach | max_mahalanobis | replay_error | processing_ms_per_1k |
|---|---:|---:|---:|---:|---:|---:|
| stable_tracking | PASS | 0 | — | 0.207037 | 0 | 2.86574 |
| slow_drift | PASS | 0 | — | 2.07491 | 0 | 2.77136 |
| sudden_depeg | PASS | 15 | 0 | 18.2793 | 0 | 6.00667 |
| gradual_then_break | PASS | 15 | 100 | 16.0067 | 0 | 7.8517 |
| oracle_lag | PASS | 3 | 15 | 4.07722 | 0 | 8.72593 |
| stale_feed | PASS | 0 | — | 0.200326 | 0 | 2.97812 |
| recovery_after_shock | PASS | 15 | 0 | 18.1502 | 0 | 7.92624 |
| fat_tail_noise | PASS | 15 | 0 | 15.6352 | 0 | 5.39248 |

## Summary

8 of 8 deterministic scenarios passed their configured thresholds (0 failed). Known limitations: slow_drift is intentionally silent under the current policy (0 breach ticks), so this pack does not claim slow-drift alarming; oracle_lag is audit-heavy (211 audit ticks), showing that operational lag can sustain escalation even without a fundamental depeg.

These results are deterministic synthetic validation evidence, not production performance guarantees or regulatory attestation. Runtime measurements vary by host.
