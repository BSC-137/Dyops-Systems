"""End-to-end calibration, held-out evaluation, sensitivity, and ablations."""

from __future__ import annotations

import time
import tracemalloc
from dataclasses import asdict
from statistics import mean
from typing import Any, Callable

import numpy as np

from .calibration import calibrate, calibrate_per_instrument, parameter_grid
from .catalog import validate_catalog
from .data import validate_dataset
from .detectors import (
    AbsoluteBasisDetector,
    CUSUMDetector,
    DyopsDetector,
    EWMADetector,
    RollingMADDetector,
    RollingZDetector,
    SlowDriftDetector,
)
from .metrics import bootstrap_ranges, evaluate_metrics, json_safe
from .models import Dataset, EventCatalog, Observation, RESULT_SCHEMA_VERSION

Factory = Callable[[dict[str, Any], int], Any]
DEFAULT_WARMUP_EVENTS = 20


def _factories() -> dict[str, Factory]:
    return {
        "absolute_basis": lambda p, w: AbsoluteBasisDetector(
            threshold_bps=p["threshold_bps"], warmup_events=w
        ),
        "rolling_z": lambda p, w: RollingZDetector(
            window=int(p["window"]),
            threshold=p["threshold"],
            min_periods=max(3, int(p["window"]) // 2),
            warmup_events=w,
        ),
        "ewma_z": lambda p, w: EWMADetector(
            alpha=p["alpha"],
            threshold=p["threshold"],
            min_periods=8,
            warmup_events=w,
        ),
        "rolling_mad": lambda p, w: RollingMADDetector(
            window=int(p["window"]),
            threshold=p["threshold"],
            min_periods=max(3, int(p["window"]) // 2),
            warmup_events=w,
        ),
        "cusum": lambda p, w: CUSUMDetector(
            center=p["center"],
            scale=p["scale"],
            allowance=p["allowance"],
            threshold=p["threshold"],
            warmup_events=w,
        ),
        "slow_drift": lambda p, w: SlowDriftDetector(
            short_window=int(p["short_window"]),
            long_window=int(p["long_window"]),
            threshold_bps=p["threshold_bps"],
            warmup_events=w,
        ),
        "dyops_calibrated": lambda p, w: DyopsDetector(
            theta=p["theta"],
            process_noise_scale=p["process_noise_scale"],
            measurement_noise=p["measurement_noise"],
            mahalanobis_threshold=p["mahalanobis_threshold"],
            criticality_window=int(p["criticality_window"]),
            criticality_audit_pct=p["criticality_audit_pct"],
            warmup_events=w,
        ),
    }


def _grids() -> dict[str, list[dict[str, Any]]]:
    return {
        "absolute_basis": parameter_grid(
            {"threshold_bps": [5.0, 10.0, 20.0, 50.0]}
        ),
        "rolling_z": parameter_grid(
            {"window": [12, 20], "threshold": [2.5, 3.0, 4.0]}
        ),
        "ewma_z": parameter_grid(
            {"alpha": [0.05, 0.1, 0.2], "threshold": [2.5, 3.0, 4.0]}
        ),
        "rolling_mad": parameter_grid(
            {"window": [12, 20], "threshold": [3.0, 3.5, 5.0]}
        ),
        "cusum": parameter_grid(
            {
                "center": [0.0],
                "scale": [0.0001, 0.0005],
                "allowance": [0.25, 0.5],
                "threshold": [4.0, 6.0, 10.0],
            }
        ),
        "slow_drift": parameter_grid(
            {
                "short_window": [4, 8],
                "long_window": [16, 24],
                "threshold_bps": [3.0, 6.0, 10.0],
            }
        ),
        "dyops_calibrated": parameter_grid(
            {
                "theta": [0.5, 1.0, 2.0],
                "process_noise_scale": [0.5, 1.0, 2.0],
                "measurement_noise": [1e-6, 1e-5],
                "mahalanobis_threshold": [2.5, 3.0, 3.5],
                "criticality_window": [20],
                "criticality_audit_pct": [10.0, 15.0, 20.0],
            }
        ),
    }


def _heldout_rows(
    rows: tuple[Observation, ...],
    events: tuple[Any, ...],
    warmup_events: int,
) -> tuple[tuple[Observation, ...], int]:
    relevant = [event for event in events if event.instrument_id == rows[0].instrument_id]
    if not relevant:
        return (), 0
    evaluation_start = min(event.start_timestamp for event in relevant)
    first = next(
        (index for index, row in enumerate(rows) if row.timestamp >= evaluation_start),
        len(rows),
    )
    start = max(0, first - warmup_events)
    return rows[start:], first - start


def _run_measured(detector: Any, rows: tuple[Observation, ...]) -> tuple[list[Any], float, int]:
    tracemalloc.start()
    started = time.perf_counter()
    ticks = detector.run(rows)
    runtime_ms = (time.perf_counter() - started) * 1000.0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return ticks, runtime_ms, peak


def _aggregate(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not results:
        return {}
    labelled = sum(item["labelled_anomaly_events"] for item in results.values())
    detected = sum(item["detected_anomaly_events"] for item in results.values())
    alerts = sum(item["alert_events"] for item in results.values())
    matched = sum(item["matched_alert_events"] for item in results.values())
    false_alerts = sum(item["false_alerts"] for item in results.values())
    return {
        "labelled_anomaly_events": labelled,
        "detected_anomaly_events": detected,
        "event_recall": detected / labelled if labelled else None,
        "alert_events": alerts,
        "matched_alert_events": matched,
        "precision_labelled_windows": matched / alerts if alerts else None,
        "false_alerts": false_alerts,
        "false_alerts_per_instrument_day": sum(
            item["false_alerts_per_instrument_day"] for item in results.values()
        ),
        "mean_detection_latency_sec": _mean_metric(
            results, "mean_detection_latency_sec"
        ),
        "mean_detection_latency_ticks": _mean_metric(
            results, "mean_detection_latency_ticks"
        ),
        "mean_basis_at_detection_bps": _mean_metric(
            results, "mean_basis_at_detection_bps"
        ),
        "mean_recovery_latency_sec": _mean_metric(
            results, "mean_recovery_latency_sec"
        ),
        "mean_alert_duration_sec": _mean_metric(results, "mean_alert_duration_sec"),
        "mean_alert_duration_ticks": _mean_metric(
            results, "mean_alert_duration_ticks"
        ),
        "escalations_on_invalid_ticks": sum(
            item["escalations_on_invalid_ticks"] for item in results.values()
        ),
    }


def _mean_metric(results: dict[str, dict[str, Any]], key: str) -> float | None:
    values = [item[key] for item in results.values() if item[key] is not None]
    return mean(values) if values else None


def _evaluate_configuration(
    name: str,
    factory: Factory,
    params_by_instrument: dict[str, dict[str, Any]],
    dataset: Dataset,
    heldout_events: tuple[Any, ...],
    *,
    warmup_events: int,
    stride: int = 1,
    missing_fraction: float = 0.0,
    seed: int = 41,
) -> dict[str, Any]:
    instrument_results: dict[str, dict[str, Any]] = {}
    runtime_ms = 0.0
    peak_memory = 0
    all_per_event: list[dict[str, Any]] = []
    for instrument_id in dataset.instruments:
        rows, actual_warmup = _heldout_rows(
            dataset.for_instrument(instrument_id),
            heldout_events,
            warmup_events,
        )
        if not rows:
            continue
        if stride > 1:
            rows = rows[::stride]
            actual_warmup = max(0, actual_warmup // stride)
        if missing_fraction > 0:
            rng = np.random.default_rng(np.random.PCG64(seed))
            removable = np.arange(actual_warmup, len(rows))
            count = min(len(removable), round(len(removable) * missing_fraction))
            dropped = set(
                int(value)
                for value in rng.choice(removable, size=count, replace=False)
            )
            rows = tuple(row for index, row in enumerate(rows) if index not in dropped)
        params = params_by_instrument.get(
            instrument_id, next(iter(params_by_instrument.values()))
        )
        detector = factory(params, actual_warmup)
        detector.name = name
        ticks, elapsed, peak = _run_measured(detector, rows)
        metrics = evaluate_metrics(
            ticks,
            tuple(
                event
                for event in heldout_events
                if event.instrument_id == instrument_id
            ),
        )
        all_per_event.extend(metrics["per_event"])
        instrument_results[instrument_id] = metrics
        runtime_ms += elapsed
        peak_memory = max(peak_memory, peak)
    aggregate = _aggregate(instrument_results)
    bootstrap_input = {**aggregate, "per_event": all_per_event}
    return {
        "parameters_by_instrument": params_by_instrument,
        "metrics": aggregate,
        "bootstrap_95pct": bootstrap_ranges(bootstrap_input),
        "supporting_metrics": {
            "runtime_ms": runtime_ms,
            "peak_tracemalloc_bytes": peak_memory,
        },
        "per_instrument": instrument_results,
    }


def _recommendations(results: dict[str, Any]) -> dict[str, dict[str, str]]:
    dyops = results["dyops_current"]["metrics"]
    out: dict[str, dict[str, str]] = {}
    for baseline in (
        "absolute_basis",
        "rolling_z",
        "ewma_z",
        "rolling_mad",
        "cusum",
        "slow_drift",
    ):
        other = results[baseline]["metrics"]
        dr = dyops.get("event_recall")
        br = other.get("event_recall")
        df = dyops.get("false_alerts_per_instrument_day")
        bf = other.get("false_alerts_per_instrument_day")
        dd = dyops.get("mean_alert_duration_sec")
        bd = other.get("mean_alert_duration_sec")
        dd_cmp = float("inf") if dd is None else dd
        bd_cmp = float("inf") if bd is None else bd
        if dr is None or br is None:
            verdict = "insufficient_labels"
        elif (
            dr >= br
            and df <= bf
            and dd_cmp <= bd_cmp
            and (dr > br or df < bf or dd_cmp < bd_cmp)
        ):
            verdict = "dyops_beats_baseline_on_this_fixture"
        elif (
            dr <= br
            and df >= bf
            and dd_cmp >= bd_cmp
            and (dr < br or df > bf or dd_cmp > bd_cmp)
        ):
            verdict = "dyops_does_not_beat_baseline_on_this_fixture"
        else:
            verdict = "no_overall_dyops_advantage_demonstrated"
        out[baseline] = {
            "verdict": verdict,
            "basis": (
                f"held-out event recall {dr} vs {br}; false alerts/instrument-day "
                f"{df} vs {bf}; mean alert duration {dd}s vs {bd}s"
            ),
        }
    return out


def evaluate(
    dataset: Dataset,
    catalog: EventCatalog,
    *,
    warmup_events: int = DEFAULT_WARMUP_EVENTS,
) -> dict[str, Any]:
    validate_catalog(catalog, dataset)
    validation = validate_dataset(dataset)
    if not validation.valid:
        raise ValueError("Dataset has validation errors; inspect validation report first")
    factories = _factories()
    grids = _grids()
    calibration: dict[str, Any] = {}
    global_params: dict[str, dict[str, Any]] = {}
    per_instrument_dyops: dict[str, dict[str, Any]] = {}
    for name, factory in factories.items():
        adapter = lambda params, f=factory: f(params, 0)
        result = calibrate(name, adapter, grids[name], dataset, catalog)
        calibration[name] = {"global": result.to_dict()}
        global_params[name] = result.parameters
        if name == "dyops_calibrated":
            per = calibrate_per_instrument(name, adapter, grids[name], dataset, catalog)
            calibration[name]["per_instrument"] = {
                instrument: item.to_dict() for instrument, item in per.items()
            }
            per_instrument_dyops = {
                instrument: item.parameters for instrument, item in per.items()
            }

    heldout = catalog.split_events("held_out")
    if not heldout:
        raise ValueError("Evaluation requires held-out catalog events")
    detector_results: dict[str, Any] = {}
    for name in (
        "absolute_basis",
        "rolling_z",
        "ewma_z",
        "rolling_mad",
        "cusum",
        "slow_drift",
    ):
        detector_results[name] = _evaluate_configuration(
            name,
            factories[name],
            {"global": global_params[name]},
            dataset,
            heldout,
            warmup_events=warmup_events,
        )

    current_params = {
        "theta": 1.0,
        "process_noise_scale": 1.0,
        "measurement_noise": 1e-6,
        "mahalanobis_threshold": 3.0,
        "criticality_window": 100,
        "criticality_audit_pct": 15.0,
    }
    current_factory: Factory = lambda p, w: DyopsDetector(
        **p, warmup_events=w
    )
    observer_factory: Factory = lambda p, w: DyopsDetector(
        **p, observer_only=True, warmup_events=w
    )
    detector_results["dyops_observer_only"] = _evaluate_configuration(
        "dyops_observer_only",
        observer_factory,
        {"global": current_params},
        dataset,
        heldout,
        warmup_events=warmup_events,
    )
    detector_results["dyops_current"] = _evaluate_configuration(
        "dyops_current",
        current_factory,
        {"global": current_params},
        dataset,
        heldout,
        warmup_events=warmup_events,
    )
    detector_results["dyops_calibrated_global"] = _evaluate_configuration(
        "dyops_calibrated_global",
        factories["dyops_calibrated"],
        {"global": global_params["dyops_calibrated"]},
        dataset,
        heldout,
        warmup_events=warmup_events,
    )
    detector_results["dyops_calibrated_per_instrument"] = _evaluate_configuration(
        "dyops_calibrated_per_instrument",
        factories["dyops_calibrated"],
        per_instrument_dyops or {"global": global_params["dyops_calibrated"]},
        dataset,
        heldout,
        warmup_events=warmup_events,
    )

    sensitivity: dict[str, Any] = {}
    for name in (
        "absolute_basis",
        "rolling_z",
        "ewma_z",
        "rolling_mad",
        "cusum",
        "slow_drift",
        "dyops_current",
    ):
        if name == "dyops_current":
            factory = current_factory
            params = {"global": current_params}
        else:
            factory = factories[name]
            params = {"global": global_params[name]}
        sensitivity[name] = {
            "sampling_stride_2": _evaluate_configuration(
                name,
                factory,
                params,
                dataset,
                heldout,
                warmup_events=warmup_events,
                stride=2,
            )["metrics"],
            "sampling_stride_3": _evaluate_configuration(
                name,
                factory,
                params,
                dataset,
                heldout,
                warmup_events=warmup_events,
                stride=3,
            )["metrics"],
            "missing_10pct": _evaluate_configuration(
                name,
                factory,
                params,
                dataset,
                heldout,
                warmup_events=warmup_events,
                missing_fraction=0.1,
            )["metrics"],
        }

    warmup_ablation = {
        str(warmup): _evaluate_configuration(
            "dyops_current",
            current_factory,
            {"global": current_params},
            dataset,
            heldout,
            warmup_events=warmup,
        )["metrics"]
        for warmup in (0, 10, 20, 40)
    }
    return json_safe(
        {
            "schema_version": RESULT_SCHEMA_VERSION,
            "evidence_class": "synthetic_regression_fixture",
            "dataset": {
                "dataset_id": dataset.dataset_id,
                "schema_version": dataset.schema_version,
                "path": dataset.path,
                "rows": len(dataset.observations),
                "instruments": list(dataset.instruments),
                "validation": validation.to_dict(),
            },
            "catalog": {
                "catalog_id": catalog.catalog_id,
                "schema_version": catalog.schema_version,
                "tuning_event_ids": [
                    event.event_id for event in catalog.split_events("tuning")
                ],
                "held_out_event_ids": [
                    event.event_id for event in catalog.split_events("held_out")
                ],
                "limitations": list(catalog.limitations),
            },
            "calibration": calibration,
            "detectors": detector_results,
            "ablations": {
                "observer_only_vs_criticality": {
                    "observer_only": detector_results["dyops_observer_only"]["metrics"],
                    "observer_plus_criticality": detector_results["dyops_current"][
                        "metrics"
                    ],
                },
                "global_vs_per_instrument": {
                    "global": detector_results["dyops_calibrated_global"]["metrics"],
                    "per_instrument": detector_results[
                        "dyops_calibrated_per_instrument"
                    ]["metrics"],
                },
                "replay_warmup_events": warmup_ablation,
                "current_vs_slow_drift": {
                    "current": detector_results["dyops_current"]["metrics"],
                    "slow_drift": detector_results["slow_drift"]["metrics"],
                },
            },
            "sensitivity": sensitivity,
            "recommendations": _recommendations(detector_results),
            "limitations": [
                "This committed fixture is synthetic regression data, not real-world validation.",
                "Calibration reads tuning events only; held-out labels and post-cutoff rows are excluded.",
                "Bootstrap ranges resample held-out labelled events without re-tuning.",
                "Runtime and tracemalloc values are supporting host-dependent diagnostics.",
                "Approximate labels are expanded by their recorded uncertainty and are not precise ground truth.",
                "The committed fixture has one instrument, so global and per-instrument calibration are expected to coincide.",
            ],
        }
    )
