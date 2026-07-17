"""Extended measurements and threshold gates for scenario runs."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import numpy as np

from .base import Scenario

DEFAULT_REPLAY_MAX_ABS_ERROR = 1e-12
BREACH = "BREACH"
AUDIT = "AUDIT"
MONITORING = "MONITORING"


class TickLike(Protocol):
    tick: int
    measurement_valid: bool
    level: str
    mahalanobis: float | None


def _anomaly_window(
    scenario: Scenario,
) -> tuple[int, int, bool]:
    labels = scenario.expected_outcomes
    explicit = labels.get("anomaly_window")
    if explicit is not None:
        start, end = int(explicit[0]), int(explicit[1])
        return max(0, start), min(scenario.tick_count - 1, end), True
    if "drift_start_tick" in labels:
        return int(labels["drift_start_tick"]), scenario.tick_count - 1, True
    if "shock_tick" in labels:
        return int(labels["shock_tick"]), scenario.tick_count - 1, True
    return 0, scenario.tick_count - 1, False


def _first_at_or_after(
    ticks: Sequence[TickLike],
    level: str,
    start: int,
) -> int | None:
    return next(
        (tick.tick for tick in ticks if tick.tick >= start and tick.level == level),
        None,
    )


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _replay_max_abs_error(
    scenario: Scenario,
    ticks: Sequence[TickLike],
    observer_factory: Callable[..., Any],
) -> float:
    observer = observer_factory(
        name=f"dyops-scenario-{scenario.name}-batch-replay",
        theta=1.0,
        ring_buffer_capacity=1000,
    )
    replay = observer.update_batch(
        np.ascontiguousarray(scenario.timestamps, dtype=np.float64),
        np.ascontiguousarray(scenario.physical_price, dtype=np.float64),
        np.ascontiguousarray(scenario.token_price, dtype=np.float64),
    )
    batch_mahalanobis = replay["mahalanobis_distance"]
    errors: list[float] = []
    for serial_tick, batch_value in zip(ticks, batch_mahalanobis):
        serial_value = serial_tick.mahalanobis
        batch_float = float(batch_value)
        if serial_value is None or not math.isfinite(batch_float):
            if serial_value is None and not math.isfinite(batch_float):
                errors.append(0.0)
            else:
                return float("inf")
        else:
            errors.append(abs(serial_value - batch_float))
    return max(errors, default=0.0)


def compute_extended_metrics(
    scenario: Scenario,
    ticks: Sequence[TickLike],
    *,
    observer_factory: Callable[..., Any],
    processing_elapsed_ms: float,
    first_audit_snapshot_size_bytes: int | None,
    return_to_monitoring_tick: int | None,
    replay_max_abs_error_threshold: float = DEFAULT_REPLAY_MAX_ABS_ERROR,
) -> dict[str, Any]:
    """Compute detection, quality, stability, and operational measurements."""

    anomaly_start, anomaly_end, has_anomaly_label = _anomaly_window(scenario)
    breach_ticks = [tick.tick for tick in ticks if tick.level == BREACH]
    audit_ticks = [tick.tick for tick in ticks if tick.level == AUDIT]
    first_breach = _first_at_or_after(ticks, BREACH, anomaly_start)
    first_audit = _first_at_or_after(ticks, AUDIT, anomaly_start)
    window_mahalanobis = [
        float(tick.mahalanobis)
        for tick in ticks
        if anomaly_start <= tick.tick <= anomaly_end
        and tick.measurement_valid
        and tick.mahalanobis is not None
    ]

    pre_anomaly_valid = sum(
        tick.measurement_valid and tick.tick < anomaly_start for tick in ticks
    )
    pre_anomaly_breaches = sum(
        tick.level == BREACH and tick.tick < anomaly_start for tick in ticks
    )
    breaches_in_window = sum(
        anomaly_start <= tick.tick <= anomaly_end for tick in ticks if tick.level == BREACH
    )
    invalid_ticks = [tick for tick in ticks if not tick.measurement_valid]
    escalations_on_invalid = sum(
        tick.level in {BREACH, AUDIT} for tick in invalid_ticks
    )

    thresholds = scenario.expected_outcomes.get("thresholds", {})
    allowed_latency = scenario.expected_outcomes.get(
        "max_allowed_latency_ticks",
        thresholds.get("max_time_to_first_breach_ticks"),
    )
    if has_anomaly_label and allowed_latency is not None:
        detection_recall: bool | None = (
            first_breach is not None
            and first_breach - anomaly_start <= int(allowed_latency)
        )
    else:
        detection_recall = None

    replay_error = _replay_max_abs_error(scenario, ticks, observer_factory)
    tick_count = len(ticks)
    return {
        "time_to_first_breach_ticks": (
            first_breach - anomaly_start if first_breach is not None else None
        ),
        "time_to_first_audit_ticks": (
            first_audit - anomaly_start if first_audit is not None else None
        ),
        "breach_count": len(breach_ticks),
        "audit_count": len(audit_ticks),
        "audit_pct": 100.0 * len(audit_ticks) / tick_count if tick_count else 0.0,
        "max_mahalanobis": max(window_mahalanobis, default=None),
        "p95_mahalanobis": _percentile(window_mahalanobis, 0.95),
        "drift_start_tick": scenario.expected_outcomes.get("drift_start_tick"),
        "shock_tick": scenario.expected_outcomes.get("shock_tick"),
        "anomaly_window": (
            [anomaly_start, anomaly_end] if has_anomaly_label else None
        ),
        "false_positive_rate": (
            pre_anomaly_breaches / pre_anomaly_valid
            if has_anomaly_label and pre_anomaly_valid
            else None
        ),
        "detection_recall": detection_recall,
        "precision_at_breach": (
            breaches_in_window / len(breach_ticks)
            if has_anomaly_label and breach_ticks
            else None
        ),
        "replay_consistency": replay_error <= replay_max_abs_error_threshold,
        "replay_max_abs_error": replay_error,
        "replay_max_abs_error_threshold": replay_max_abs_error_threshold,
        "invalid_tick_count": len(invalid_ticks),
        "escalations_on_invalid": escalations_on_invalid,
        "invalid_tick_handling": escalations_on_invalid == 0,
        "processing_ms_per_1k_ticks": (
            processing_elapsed_ms * 1000.0 / tick_count if tick_count else 0.0
        ),
        "snapshot_size_bytes": first_audit_snapshot_size_bytes,
        "return_to_monitoring_tick": return_to_monitoring_tick,
    }


def evaluate_thresholds(
    scenario: Scenario,
    metrics: dict[str, Any],
    ticks: Sequence[TickLike],
) -> list[str]:
    """Return human-readable failures for configured scenario thresholds."""

    del ticks  # Reserved for future tick-level threshold predicates.
    thresholds = scenario.expected_outcomes.get("thresholds", {})
    failures: list[str] = []

    def fail(key: str, actual: Any, relation: str, expected: Any) -> None:
        failures.append(f"{key}: got {actual!r}, expected {relation} {expected!r}")

    breach_count = int(metrics["breach_count"])
    if "max_breaches" in thresholds and breach_count > int(thresholds["max_breaches"]):
        fail("breach_count", breach_count, "<=", thresholds["max_breaches"])
    if "min_breaches" in thresholds and breach_count < int(thresholds["min_breaches"]):
        fail("breach_count", breach_count, ">=", thresholds["min_breaches"])

    if "max_time_to_first_breach_ticks" in thresholds:
        actual = metrics["time_to_first_breach_ticks"]
        maximum = int(thresholds["max_time_to_first_breach_ticks"])
        if actual is None or actual > maximum:
            fail("time_to_first_breach_ticks", actual, "<=", maximum)

    if "max_false_positive_rate" in thresholds:
        actual = metrics["false_positive_rate"]
        maximum = float(thresholds["max_false_positive_rate"])
        if actual is None or float(actual) > maximum:
            fail("false_positive_rate", actual, "<=", maximum)

    if "max_audit_pct" in thresholds:
        actual = float(metrics["audit_pct"])
        maximum = float(thresholds["max_audit_pct"])
        if actual > maximum:
            fail("audit_pct", actual, "<=", maximum)

    replay_limit = float(
        thresholds.get(
            "replay_max_abs_error",
            DEFAULT_REPLAY_MAX_ABS_ERROR,
        )
    )
    replay_error = float(metrics["replay_max_abs_error"])
    if replay_error > replay_limit:
        fail("replay_max_abs_error", replay_error, "<=", replay_limit)

    invalid_escalations = int(metrics["escalations_on_invalid"])
    invalid_handling_failed = invalid_escalations > 0
    if invalid_handling_failed:
        fail("escalations_on_invalid", invalid_escalations, "==", 0)

    if "expected_invalid_measurements" in thresholds:
        actual = int(metrics["invalid_tick_count"])
        expected = int(thresholds["expected_invalid_measurements"])
        if actual != expected:
            fail("invalid_tick_count", actual, "==", expected)

    if "max_escalations_on_invalid" in thresholds:
        actual = invalid_escalations
        maximum = int(thresholds["max_escalations_on_invalid"])
        if actual > maximum and not invalid_handling_failed:
            fail("escalations_on_invalid", actual, "<=", maximum)

    if thresholds.get("require_return_to_monitoring"):
        actual = metrics["return_to_monitoring_tick"]
        if actual is None:
            fail("return_to_monitoring_tick", actual, "to be", "an integer")

    return failures
