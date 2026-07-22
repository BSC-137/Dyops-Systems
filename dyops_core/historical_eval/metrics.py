"""Event-centric metrics and deterministic bootstrap ranges."""

from __future__ import annotations

import math
from statistics import mean, median
from typing import Any

import numpy as np

from .detectors import ticks_to_alerts
from .models import AlertEvent, CatalogEvent, DetectionTick


def _overlaps(alert: AlertEvent, event: CatalogEvent) -> bool:
    start = event.start_timestamp - event.uncertainty_sec
    end = event.end_timestamp + event.uncertainty_sec
    return alert.end_timestamp >= start and alert.start_timestamp <= end


def _tick_at_or_after(
    ticks: list[DetectionTick], timestamp: float
) -> DetectionTick | None:
    return next((tick for tick in ticks if tick.timestamp >= timestamp), None)


def evaluate_metrics(
    ticks: list[DetectionTick],
    events: tuple[CatalogEvent, ...],
    *,
    merge_gap_ticks: int = 0,
) -> dict[str, Any]:
    alerts = ticks_to_alerts(ticks, merge_gap_ticks=merge_gap_ticks)
    anomalies = tuple(event for event in events if event.is_anomaly)
    matched_alert_ids: set[int] = set()
    per_event: list[dict[str, Any]] = []
    detected = 0

    for event in anomalies:
        candidates = [
            (index, alert)
            for index, alert in enumerate(alerts)
            if _overlaps(alert, event)
        ]
        if not candidates:
            per_event.append(
                {
                    "event_id": event.event_id,
                    "detected": False,
                    "latency_sec": None,
                    "latency_ticks": None,
                    "basis_at_detection_bps": None,
                    "recovery_latency_sec": None,
                }
            )
            continue
        detected += 1
        matched_alert_ids.update(index for index, _ in candidates)
        first_index, first_alert = min(candidates, key=lambda item: item[1].start_timestamp)
        del first_index
        first_tick = _tick_at_or_after(ticks, first_alert.start_timestamp)
        event_start_tick = _tick_at_or_after(ticks, event.start_timestamp)
        latency_sec = max(0.0, first_alert.start_timestamp - event.start_timestamp)
        latency_ticks = (
            max(0, first_tick.tick - event_start_tick.tick)
            if first_tick is not None and event_start_tick is not None
            else None
        )
        basis_bps = (
            abs(first_tick.basis) * 10_000.0
            if first_tick is not None and first_tick.basis is not None
            else None
        )
        last_overlap = max((alert for _, alert in candidates), key=lambda a: a.end_timestamp)
        recovery_latency = max(0.0, last_overlap.end_timestamp - event.end_timestamp)
        per_event.append(
            {
                "event_id": event.event_id,
                "detected": True,
                "latency_sec": latency_sec,
                "latency_ticks": latency_ticks,
                "basis_at_detection_bps": basis_bps,
                "recovery_latency_sec": recovery_latency,
            }
        )

    duration_sec = (
        ticks[-1].timestamp - ticks[0].timestamp if len(ticks) >= 2 else 0.0
    )
    instrument_days = max(duration_sec / 86_400.0, 1.0 / 86_400.0)
    false_alerts = len(alerts) - len(matched_alert_ids)
    intervals = [
        current.timestamp - previous.timestamp
        for previous, current in zip(ticks, ticks[1:])
        if current.timestamp > previous.timestamp
    ]
    representative_interval = median(intervals) if intervals else 0.0
    durations_sec = [
        alert.duration_sec + representative_interval for alert in alerts
    ]
    durations_ticks = [alert.duration_ticks for alert in alerts]
    valid_ticks = sum(tick.measurement_valid for tick in ticks)
    invalid_escalations = sum(
        not tick.measurement_valid and tick.level != "MONITORING" for tick in ticks
    )
    return {
        "labelled_anomaly_events": len(anomalies),
        "detected_anomaly_events": detected,
        "event_recall": detected / len(anomalies) if anomalies else None,
        "alert_events": len(alerts),
        "matched_alert_events": len(matched_alert_ids),
        "precision_labelled_windows": (
            len(matched_alert_ids) / len(alerts) if alerts else None
        ),
        "false_alerts": false_alerts,
        "false_alerts_per_instrument_day": false_alerts / instrument_days,
        "mean_detection_latency_sec": _mean_present(
            item["latency_sec"] for item in per_event
        ),
        "mean_detection_latency_ticks": _mean_present(
            item["latency_ticks"] for item in per_event
        ),
        "mean_basis_at_detection_bps": _mean_present(
            item["basis_at_detection_bps"] for item in per_event
        ),
        "mean_recovery_latency_sec": _mean_present(
            item["recovery_latency_sec"] for item in per_event
        ),
        "mean_alert_duration_sec": mean(durations_sec) if durations_sec else None,
        "mean_alert_duration_ticks": mean(durations_ticks) if durations_ticks else None,
        "valid_ticks": valid_ticks,
        "invalid_ticks": len(ticks) - valid_ticks,
        "escalations_on_invalid_ticks": invalid_escalations,
        "per_event": per_event,
    }


def _mean_present(values: Any) -> float | None:
    present = [float(value) for value in values if value is not None]
    return mean(present) if present else None


def bootstrap_ranges(
    metrics: dict[str, Any],
    *,
    seed: int = 20260722,
    replicates: int = 500,
) -> dict[str, dict[str, float] | None]:
    """Bootstrap labelled events with replacement; labels are never re-tuned."""
    per_event = metrics["per_event"]
    if not per_event:
        return {
            "event_recall": None,
            "detection_latency_sec": None,
            "recovery_latency_sec": None,
        }
    rng = np.random.default_rng(np.random.PCG64(seed))
    recall_samples: list[float] = []
    latency_samples: list[float] = []
    recovery_samples: list[float] = []
    n = len(per_event)
    for _ in range(replicates):
        sample = [per_event[int(index)] for index in rng.integers(0, n, size=n)]
        recall_samples.append(sum(item["detected"] for item in sample) / n)
        latencies = [
            item["latency_sec"] for item in sample if item["latency_sec"] is not None
        ]
        recoveries = [
            item["recovery_latency_sec"]
            for item in sample
            if item["recovery_latency_sec"] is not None
        ]
        if latencies:
            latency_samples.append(mean(latencies))
        if recoveries:
            recovery_samples.append(mean(recoveries))
    return {
        "event_recall": _range(recall_samples),
        "detection_latency_sec": _range(latency_samples),
        "recovery_latency_sec": _range(recovery_samples),
    }


def _range(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    array = np.asarray(values, dtype=np.float64)
    return {
        "low": float(np.quantile(array, 0.025)),
        "median": float(np.quantile(array, 0.5)),
        "high": float(np.quantile(array, 0.975)),
    }


def json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value
