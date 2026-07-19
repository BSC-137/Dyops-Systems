"""Headless scenario execution through the production sentinel path."""

from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

import dyops_core
from sentinel import DyopsSentinel, SentinelLevel

from .base import Scenario
from .metrics import compute_extended_metrics, evaluate_thresholds


@dataclass(frozen=True)
class TickResult:
    tick: int
    timestamp: float
    physical_price: float | None
    token_price: float | None
    measurement_valid: bool
    level: str
    mahalanobis: float | None
    innovation: float | None
    criticality_recent_pct: float | None


@dataclass(frozen=True)
class ScenarioMetrics:
    total_ticks: int
    valid_ticks: int
    invalid_ticks: int
    level_counts: dict[str, int]
    level_percentages: dict[str, float]
    escalation_count: int
    first_escalation_tick: int | None
    first_breach_tick: int | None
    first_audit_tick: int | None
    return_to_monitoring_tick: int | None
    max_mahalanobis: float | None
    mean_mahalanobis: float | None
    max_abs_innovation: float | None
    max_criticality_recent_pct: float | None
    final_criticality_recent_pct: float | None


@dataclass(frozen=True)
class ScenarioResult:
    scenario: str
    instrument_id: str
    description: str
    expected_outcomes: dict[str, Any]
    observer_parameters: dict[str, Any]
    ticks: list[TickResult]
    metrics: ScenarioMetrics
    passed: bool
    failures: list[str]
    extended_metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Return a mapping accepted by strict JSON encoders."""

        return _json_safe(asdict(self))

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(self.to_dict(), indent=indent, allow_nan=False)


ObserverFactory = Callable[..., Any]
SentinelFactory = Callable[..., DyopsSentinel]


def _finite_or_none(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return _finite_or_none(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _first_tick(ticks: list[TickResult], levels: set[str]) -> int | None:
    return next((tick.tick for tick in ticks if tick.level in levels), None)


def _return_to_monitoring_tick(ticks: list[TickResult]) -> int | None:
    escalated = [tick.tick for tick in ticks if tick.level != SentinelLevel.MONITORING.name]
    if not escalated:
        return None
    last_escalation = escalated[-1]
    return next(
        (
            tick.tick
            for tick in ticks
            if tick.tick > last_escalation
            and tick.level == SentinelLevel.MONITORING.name
        ),
        None,
    )


def _compute_metrics(ticks: list[TickResult]) -> ScenarioMetrics:
    level_names = [level.name for level in SentinelLevel]
    level_counts = {
        level: sum(tick.level == level for tick in ticks)
        for level in level_names
    }
    total = len(ticks)
    valid = [tick for tick in ticks if tick.measurement_valid]
    mahalanobis = [
        tick.mahalanobis for tick in valid if tick.mahalanobis is not None
    ]
    innovations = [tick.innovation for tick in valid if tick.innovation is not None]
    criticalities = [
        tick.criticality_recent_pct
        for tick in ticks
        if tick.criticality_recent_pct is not None
    ]
    escalated_levels = {SentinelLevel.BREACH.name, SentinelLevel.AUDIT.name}
    return ScenarioMetrics(
        total_ticks=total,
        valid_ticks=len(valid),
        invalid_ticks=total - len(valid),
        level_counts=level_counts,
        level_percentages={
            level: (100.0 * count / total if total else 0.0)
            for level, count in level_counts.items()
        },
        escalation_count=sum(tick.level in escalated_levels for tick in ticks),
        first_escalation_tick=_first_tick(ticks, escalated_levels),
        first_breach_tick=_first_tick(ticks, {SentinelLevel.BREACH.name}),
        first_audit_tick=_first_tick(ticks, {SentinelLevel.AUDIT.name}),
        return_to_monitoring_tick=_return_to_monitoring_tick(ticks),
        max_mahalanobis=max(mahalanobis, default=None),
        mean_mahalanobis=statistics.fmean(mahalanobis) if mahalanobis else None,
        max_abs_innovation=max((abs(value) for value in innovations), default=None),
        max_criticality_recent_pct=max(criticalities, default=None),
        final_criticality_recent_pct=criticalities[-1] if criticalities else None,
    )


def run_scenario(
    scenario: Scenario,
    *,
    instrument_id: str | None = None,
    observer_factory: ObserverFactory = dyops_core.BasisObserver,
    sentinel_factory: SentinelFactory = DyopsSentinel,
) -> ScenarioResult:
    """Execute every tick through ``DyopsSentinel.process_event``.

    Observer and sentinel arguments intentionally mirror backend startup:
    theta=1.0, ring_buffer_capacity=1000, default Q/R, and default sentinel
    criticality policy.
    """

    effective_instrument_id = instrument_id or scenario.instrument_id or "default"
    observer = observer_factory(
        name=f"dyops-scenario-{effective_instrument_id}-{scenario.name}",
        theta=1.0,
        ring_buffer_capacity=1000,
    )
    sentinel = sentinel_factory(observer)
    ticks: list[TickResult] = []
    first_audit_snapshot_size_bytes: int | None = None
    started = time.perf_counter()

    for tick, (timestamp, physical, token) in enumerate(
        zip(scenario.timestamps, scenario.physical_price, scenario.token_price)
    ):
        event = sentinel.process_event(
            timestamp,
            physical,
            token,
            schedule_background_audit=False,
        )
        if first_audit_snapshot_size_bytes is None and event.snapshot is not None:
            first_audit_snapshot_size_bytes = len(
                json.dumps(
                    _json_safe(event.snapshot),
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        ticks.append(
            TickResult(
                tick=tick,
                timestamp=timestamp,
                physical_price=_finite_or_none(physical),
                token_price=_finite_or_none(token),
                measurement_valid=bool(event.health.measurement_valid),
                level=event.level.name,
                mahalanobis=_finite_or_none(event.health.mahalanobis_distance),
                innovation=_finite_or_none(event.health.innovation),
                criticality_recent_pct=_finite_or_none(
                    event.criticality_recent_pct
                ),
            )
        )

    processing_elapsed_ms = (time.perf_counter() - started) * 1000.0
    basic_metrics = _compute_metrics(ticks)
    replay_threshold = float(
        scenario.expected_outcomes.get("thresholds", {}).get(
            "replay_max_abs_error",
            1e-12,
        )
    )
    extended_metrics = compute_extended_metrics(
        scenario,
        ticks,
        observer_factory=observer_factory,
        processing_elapsed_ms=processing_elapsed_ms,
        first_audit_snapshot_size_bytes=first_audit_snapshot_size_bytes,
        return_to_monitoring_tick=basic_metrics.return_to_monitoring_tick,
        replay_max_abs_error_threshold=replay_threshold,
    )
    failures = evaluate_thresholds(scenario, extended_metrics, ticks)

    return ScenarioResult(
        scenario=scenario.name,
        instrument_id=effective_instrument_id,
        description=scenario.description,
        expected_outcomes=scenario.expected_outcomes,
        observer_parameters={
            "theta": 1.0,
            "ring_buffer_capacity": 1000,
            "process_noise": "dyops_core default",
            "measurement_noise": "dyops_core default",
            "criticality_window": sentinel.criticality_window,
            "audit_criticality_pct": sentinel.audit_criticality_pct,
            "audit_cooldown_ticks": sentinel.audit_cooldown_ticks,
        },
        ticks=ticks,
        metrics=basic_metrics,
        passed=not failures,
        failures=failures,
        extended_metrics=extended_metrics,
    )
