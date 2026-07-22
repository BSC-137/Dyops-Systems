"""Small deterministic grid calibration restricted to tuning labels and time ranges."""

from __future__ import annotations

import itertools
import json
import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .catalog import tuning_catalog
from .metrics import evaluate_metrics
from .models import Dataset, EventCatalog, Observation

DetectorFactory = Callable[[dict[str, Any]], Any]


@dataclass(frozen=True)
class CalibrationResult:
    detector: str
    scope: str
    parameters: dict[str, Any]
    tuning_metrics: dict[str, Any]
    candidates_evaluated: int
    tuning_event_ids: tuple[str, ...]
    tuning_cutoff_timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "detector": self.detector,
            "scope": self.scope,
            "parameters": self.parameters,
            "tuning_metrics": self.tuning_metrics,
            "candidates_evaluated": self.candidates_evaluated,
            "tuning_event_ids": list(self.tuning_event_ids),
            "tuning_cutoff_timestamp": self.tuning_cutoff_timestamp,
        }


def parameter_grid(grid: dict[str, Iterable[Any]]) -> list[dict[str, Any]]:
    keys = tuple(sorted(grid))
    return [
        dict(zip(keys, values, strict=True))
        for values in itertools.product(*(tuple(grid[key]) for key in keys))
    ]


def _calibration_rows(
    rows: tuple[Observation, ...],
    tuning_events: tuple[Any, ...],
) -> tuple[Observation, ...]:
    relevant = [event for event in tuning_events if event.instrument_id == rows[0].instrument_id]
    if not relevant:
        return ()
    cutoff = max(event.end_timestamp + event.uncertainty_sec for event in relevant)
    return tuple(row for row in rows if row.timestamp <= cutoff)


def calibrate(
    detector_name: str,
    factory: DetectorFactory,
    candidates: list[dict[str, Any]],
    dataset: Dataset,
    catalog: EventCatalog,
    *,
    scope: str = "global",
) -> CalibrationResult:
    """Choose parameters using only tuning events and observations before their cutoff."""
    if not candidates:
        raise ValueError("Calibration parameter grid is empty")
    tuning = tuning_catalog(catalog)
    cutoff = max(
        event.end_timestamp + event.uncertainty_sec for event in tuning.events
    )
    best: tuple[tuple[float, float, float, str], dict[str, Any], dict[str, Any]] | None = None
    for params in candidates:
        detected = labelled = alerts = matched = false_alerts = 0
        latency_values: list[float] = []
        per_instrument: dict[str, Any] = {}
        for instrument_id in dataset.instruments:
            rows = _calibration_rows(dataset.for_instrument(instrument_id), tuning.events)
            instrument_events = tuple(
                event
                for event in tuning.events
                if event.instrument_id == instrument_id
            )
            if not rows or not instrument_events:
                continue
            ticks = factory(params).run(rows)
            result = evaluate_metrics(ticks, instrument_events)
            per_instrument[instrument_id] = result
            detected += result["detected_anomaly_events"]
            labelled += result["labelled_anomaly_events"]
            alerts += result["alert_events"]
            matched += result["matched_alert_events"]
            false_alerts += result["false_alerts"]
            if result["mean_detection_latency_sec"] is not None:
                latency_values.append(result["mean_detection_latency_sec"])
        recall = detected / labelled if labelled else 0.0
        precision = matched / alerts if alerts else 0.0
        latency = sum(latency_values) / len(latency_values) if latency_values else math.inf
        tie = json.dumps(params, sort_keys=True, separators=(",", ":"))
        objective = (-recall, false_alerts, latency, tie)
        aggregate = {
            "event_recall": recall,
            "precision_labelled_windows": precision,
            "false_alerts": false_alerts,
            "mean_detection_latency_sec": None if math.isinf(latency) else latency,
            "per_instrument": per_instrument,
        }
        if best is None or objective < best[0]:
            best = (objective, dict(params), aggregate)
    assert best is not None
    return CalibrationResult(
        detector=detector_name,
        scope=scope,
        parameters=best[1],
        tuning_metrics=best[2],
        candidates_evaluated=len(candidates),
        tuning_event_ids=tuple(event.event_id for event in tuning.events),
        tuning_cutoff_timestamp=cutoff,
    )


def calibrate_per_instrument(
    detector_name: str,
    factory: DetectorFactory,
    candidates: list[dict[str, Any]],
    dataset: Dataset,
    catalog: EventCatalog,
) -> dict[str, CalibrationResult]:
    out: dict[str, CalibrationResult] = {}
    for instrument_id in dataset.instruments:
        subset = Dataset(
            dataset.schema_version,
            dataset.dataset_id,
            dataset.for_instrument(instrument_id),
            dataset.path,
        )
        events = tuple(
            event for event in catalog.events if event.instrument_id == instrument_id
        )
        if not any(event.split == "tuning" for event in events):
            continue
        subcatalog = EventCatalog(
            catalog.schema_version,
            catalog.catalog_id,
            catalog.dataset_id,
            events,
            catalog.limitations,
        )
        out[instrument_id] = calibrate(
            detector_name,
            factory,
            candidates,
            subset,
            subcatalog,
            scope=f"instrument:{instrument_id}",
        )
    return out
