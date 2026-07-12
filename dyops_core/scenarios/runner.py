"""Headless scenario execution through the production sentinel path."""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import asdict, dataclass
from typing import Any, Callable

import dyops_core
from sentinel import DyopsSentinel, SentinelLevel

from .base import Scenario


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
    description: str
    expected_outcomes: dict[str, Any]
    observer_parameters: dict[str, Any]
    ticks: list[TickResult]
    metrics: ScenarioMetrics

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
    observer_factory: ObserverFactory = dyops_core.BasisObserver,
    sentinel_factory: SentinelFactory = DyopsSentinel,
) -> ScenarioResult:
    """Execute every tick through ``DyopsSentinel.process_event``.

    Observer and sentinel arguments intentionally mirror backend startup:
    theta=1.0, ring_buffer_capacity=1000, default Q/R, and default sentinel
    criticality policy.
    """

    observer = observer_factory(
        name=f"dyops-scenario-{scenario.name}",
        theta=1.0,
        ring_buffer_capacity=1000,
    )
    sentinel = sentinel_factory(observer)
    ticks: list[TickResult] = []

    for tick, (timestamp, physical, token) in enumerate(
        zip(scenario.timestamps, scenario.physical_price, scenario.token_price)
    ):
        event = sentinel.process_event(
            timestamp,
            physical,
            token,
            schedule_background_audit=False,
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

    return ScenarioResult(
        scenario=scenario.name,
        description=scenario.description,
        expected_outcomes=scenario.expected_outcomes,
        observer_parameters={
            "theta": 1.0,
            "ring_buffer_capacity": 1000,
            "process_noise": "dyops_core default",
            "measurement_noise": "dyops_core default",
            "criticality_window": sentinel.criticality_window,
            "audit_criticality_pct": sentinel.audit_criticality_pct,
        },
        ticks=ticks,
        metrics=_compute_metrics(ticks),
    )
