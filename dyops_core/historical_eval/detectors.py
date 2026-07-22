"""Comparable causal detectors producing a common tick and alert-event format."""

from __future__ import annotations

import math
from collections import deque
from statistics import median
from typing import Any

import numpy as np

import dyops_core

from .models import AlertEvent, DetectionTick, Observation


def _basis(row: Observation) -> float | None:
    if not row.valid_price:
        return None
    value = math.log(row.physical_price / row.token_price)
    return value if math.isfinite(value) else None


class Detector:
    name = "detector"

    def __init__(self, *, warmup_events: int = 0) -> None:
        self.warmup_events = warmup_events
        self._valid_seen = 0

    def reset(self) -> None:
        self._valid_seen = 0

    def parameters(self) -> dict[str, Any]:
        return {"warmup_events": self.warmup_events}

    def _level(self, alert: bool) -> str:
        return "BREACH" if alert and self._valid_seen > self.warmup_events else "MONITORING"

    def step(
        self, tick: int, row: Observation
    ) -> DetectionTick:  # pragma: no cover - protocol guard
        raise NotImplementedError

    def run(self, rows: tuple[Observation, ...]) -> list[DetectionTick]:
        self.reset()
        return [self.step(tick, row) for tick, row in enumerate(rows)]

    def _invalid(self, tick: int, row: Observation) -> DetectionTick:
        return DetectionTick(
            self.name,
            row.instrument_id,
            tick,
            row.timestamp,
            False,
            None,
            None,
            "MONITORING",
        )


class AbsoluteBasisDetector(Detector):
    name = "absolute_basis"

    def __init__(
        self,
        threshold_bps: float = 50.0,
        anchor_basis: float = 0.0,
        *,
        warmup_events: int = 0,
    ) -> None:
        super().__init__(warmup_events=warmup_events)
        self.threshold_bps = threshold_bps
        self.anchor_basis = anchor_basis

    def parameters(self) -> dict[str, Any]:
        return {
            **super().parameters(),
            "threshold_bps": self.threshold_bps,
            "anchor_basis": self.anchor_basis,
        }

    def step(self, tick: int, row: Observation) -> DetectionTick:
        basis = _basis(row)
        if basis is None:
            return self._invalid(tick, row)
        self._valid_seen += 1
        score = abs(basis - self.anchor_basis) * 10_000.0
        return DetectionTick(
            self.name,
            row.instrument_id,
            tick,
            row.timestamp,
            True,
            basis,
            score,
            self._level(score > self.threshold_bps),
        )


class RollingZDetector(Detector):
    name = "rolling_z"

    def __init__(
        self,
        window: int = 20,
        threshold: float = 3.0,
        min_periods: int | None = None,
        *,
        warmup_events: int = 0,
    ) -> None:
        super().__init__(warmup_events=warmup_events)
        self.window = window
        self.threshold = threshold
        self.min_periods = min_periods or max(3, window // 2)
        self._history: deque[float] = deque(maxlen=window)

    def reset(self) -> None:
        super().reset()
        self._history = deque(maxlen=self.window)

    def parameters(self) -> dict[str, Any]:
        return {
            **super().parameters(),
            "window": self.window,
            "threshold": self.threshold,
            "min_periods": self.min_periods,
        }

    def step(self, tick: int, row: Observation) -> DetectionTick:
        basis = _basis(row)
        if basis is None:
            return self._invalid(tick, row)
        score: float | None = None
        if len(self._history) >= self.min_periods:
            values = np.asarray(self._history, dtype=np.float64)
            std = float(values.std(ddof=1))
            score = abs(basis - float(values.mean())) / max(std, 1e-12)
        self._history.append(basis)
        self._valid_seen += 1
        return DetectionTick(
            self.name,
            row.instrument_id,
            tick,
            row.timestamp,
            True,
            basis,
            score,
            self._level(score is not None and score > self.threshold),
        )


class EWMADetector(Detector):
    name = "ewma_z"

    def __init__(
        self,
        alpha: float = 0.1,
        threshold: float = 3.0,
        min_periods: int = 10,
        *,
        warmup_events: int = 0,
    ) -> None:
        super().__init__(warmup_events=warmup_events)
        self.alpha = alpha
        self.threshold = threshold
        self.min_periods = min_periods
        self._mean: float | None = None
        self._variance = 0.0

    def reset(self) -> None:
        super().reset()
        self._mean = None
        self._variance = 0.0

    def parameters(self) -> dict[str, Any]:
        return {
            **super().parameters(),
            "alpha": self.alpha,
            "threshold": self.threshold,
            "min_periods": self.min_periods,
        }

    def step(self, tick: int, row: Observation) -> DetectionTick:
        basis = _basis(row)
        if basis is None:
            return self._invalid(tick, row)
        score: float | None = None
        if self._mean is None:
            self._mean = basis
        else:
            delta = basis - self._mean
            if self._valid_seen >= self.min_periods:
                score = abs(delta) / max(math.sqrt(self._variance), 1e-12)
            self._mean += self.alpha * delta
            self._variance = (
                (1.0 - self.alpha)
                * (self._variance + self.alpha * delta * delta)
            )
        self._valid_seen += 1
        return DetectionTick(
            self.name,
            row.instrument_id,
            tick,
            row.timestamp,
            True,
            basis,
            score,
            self._level(score is not None and score > self.threshold),
        )


class RollingMADDetector(Detector):
    name = "rolling_mad"

    def __init__(
        self,
        window: int = 20,
        threshold: float = 3.5,
        min_periods: int | None = None,
        *,
        warmup_events: int = 0,
    ) -> None:
        super().__init__(warmup_events=warmup_events)
        self.window = window
        self.threshold = threshold
        self.min_periods = min_periods or max(3, window // 2)
        self._history: deque[float] = deque(maxlen=window)

    def reset(self) -> None:
        super().reset()
        self._history = deque(maxlen=self.window)

    def parameters(self) -> dict[str, Any]:
        return {
            **super().parameters(),
            "window": self.window,
            "threshold": self.threshold,
            "min_periods": self.min_periods,
        }

    def step(self, tick: int, row: Observation) -> DetectionTick:
        basis = _basis(row)
        if basis is None:
            return self._invalid(tick, row)
        score: float | None = None
        if len(self._history) >= self.min_periods:
            center = median(self._history)
            mad = median(abs(value - center) for value in self._history)
            score = abs(basis - center) / max(1.4826 * mad, 1e-12)
        self._history.append(basis)
        self._valid_seen += 1
        return DetectionTick(
            self.name,
            row.instrument_id,
            tick,
            row.timestamp,
            True,
            basis,
            score,
            self._level(score is not None and score > self.threshold),
        )


class CUSUMDetector(Detector):
    name = "cusum"

    def __init__(
        self,
        center: float = 0.0,
        scale: float = 0.001,
        allowance: float = 0.5,
        threshold: float = 5.0,
        *,
        warmup_events: int = 0,
    ) -> None:
        super().__init__(warmup_events=warmup_events)
        self.center = center
        self.scale = scale
        self.allowance = allowance
        self.threshold = threshold
        self._positive = 0.0
        self._negative = 0.0

    def reset(self) -> None:
        super().reset()
        self._positive = 0.0
        self._negative = 0.0

    def parameters(self) -> dict[str, Any]:
        return {
            **super().parameters(),
            "center": self.center,
            "scale": self.scale,
            "allowance": self.allowance,
            "threshold": self.threshold,
        }

    def step(self, tick: int, row: Observation) -> DetectionTick:
        basis = _basis(row)
        if basis is None:
            return self._invalid(tick, row)
        standardized = (basis - self.center) / max(self.scale, 1e-12)
        self._positive = max(0.0, self._positive + standardized - self.allowance)
        self._negative = max(0.0, self._negative - standardized - self.allowance)
        score = max(self._positive, self._negative)
        self._valid_seen += 1
        return DetectionTick(
            self.name,
            row.instrument_id,
            tick,
            row.timestamp,
            True,
            basis,
            score,
            self._level(score > self.threshold),
            {"positive": self._positive, "negative": self._negative},
        )


class SlowDriftDetector(Detector):
    name = "slow_drift"

    def __init__(
        self,
        short_window: int = 8,
        long_window: int = 30,
        threshold_bps: float = 8.0,
        *,
        warmup_events: int = 0,
    ) -> None:
        super().__init__(warmup_events=warmup_events)
        self.short_window = short_window
        self.long_window = long_window
        self.threshold_bps = threshold_bps
        self._history: deque[float] = deque(maxlen=long_window)

    def reset(self) -> None:
        super().reset()
        self._history = deque(maxlen=self.long_window)

    def parameters(self) -> dict[str, Any]:
        return {
            **super().parameters(),
            "short_window": self.short_window,
            "long_window": self.long_window,
            "threshold_bps": self.threshold_bps,
        }

    def step(self, tick: int, row: Observation) -> DetectionTick:
        basis = _basis(row)
        if basis is None:
            return self._invalid(tick, row)
        score: float | None = None
        if len(self._history) >= self.long_window:
            values = list(self._history)
            short = sum(values[-self.short_window :]) / self.short_window
            long = sum(values) / self.long_window
            score = abs(short - long) * 10_000.0
        self._history.append(basis)
        self._valid_seen += 1
        return DetectionTick(
            self.name,
            row.instrument_id,
            tick,
            row.timestamp,
            True,
            basis,
            score,
            self._level(score is not None and score > self.threshold_bps),
        )


class DyopsDetector(Detector):
    """Production observer with selectable observer-only or criticality policy."""

    def __init__(
        self,
        *,
        theta: float = 1.0,
        process_noise_scale: float = 1.0,
        measurement_noise: float = 1e-6,
        mahalanobis_threshold: float = 3.0,
        criticality_window: int = 100,
        criticality_audit_pct: float = 15.0,
        observer_only: bool = False,
        warmup_events: int = 0,
    ) -> None:
        super().__init__(warmup_events=warmup_events)
        self.theta = theta
        self.process_noise_scale = process_noise_scale
        self.measurement_noise = measurement_noise
        self.mahalanobis_threshold = mahalanobis_threshold
        self.criticality_window = criticality_window
        self.criticality_audit_pct = criticality_audit_pct
        self.observer_only = observer_only
        self.name = "dyops_observer_only" if observer_only else "dyops_current"
        self._observer: Any = None
        self._critical: deque[bool] = deque(maxlen=criticality_window)

    def reset(self) -> None:
        super().reset()
        scale = self.process_noise_scale
        q = [
            1e-8 * scale,
            0.0,
            0.0,
            0.0,
            1e-6 * scale,
            0.0,
            0.0,
            0.0,
            1e-10 * scale,
        ]
        self._observer = dyops_core.BasisObserver(
            name=f"historical-eval-{self.name}",
            theta=self.theta,
            process_noise=q,
            measurement_noise=self.measurement_noise,
            ring_buffer_capacity=1000,
        )
        self._critical = deque(maxlen=self.criticality_window)

    def parameters(self) -> dict[str, Any]:
        return {
            **super().parameters(),
            "theta": self.theta,
            "process_noise_scale": self.process_noise_scale,
            "measurement_noise": self.measurement_noise,
            "mahalanobis_threshold": self.mahalanobis_threshold,
            "criticality_window": self.criticality_window,
            "criticality_audit_pct": self.criticality_audit_pct,
            "observer_only": self.observer_only,
        }

    def run(self, rows: tuple[Observation, ...]) -> list[DetectionTick]:
        production_policy = (
            not self.observer_only
            and self.theta == 1.0
            and self.process_noise_scale == 1.0
            and self.measurement_noise == 1e-6
            and self.mahalanobis_threshold == float(dyops_core.MAHALANOBIS_BREACH)
            and self.criticality_window == int(dyops_core.CRITICALITY_WINDOW)
            and self.criticality_audit_pct
            == float(dyops_core.CRITICALITY_AUDIT_PCT)
            and self.warmup_events == 0
        )
        if not production_policy or not rows:
            return super().run(rows)

        self.reset()
        core = dyops_core.DyopsSentinelCore(
            self._observer,
            criticality_window=self.criticality_window,
            audit_criticality_pct=self.criticality_audit_pct,
        )
        batch = core.process_batch(
            np.ascontiguousarray([row.timestamp for row in rows], dtype=np.float64),
            np.ascontiguousarray(
                [row.physical_price for row in rows],
                dtype=np.float64,
            ),
            np.ascontiguousarray([row.token_price for row in rows], dtype=np.float64),
        )
        levels = ("MONITORING", "BREACH", "AUDIT")
        ticks: list[DetectionTick] = []
        for tick, row in enumerate(rows):
            valid = bool(batch["measurement_valid"][tick])
            score = (
                float(batch["mahalanobis_distance"][tick]) if valid else None
            )
            metadata = {
                "criticality_recent_pct": float(
                    batch["criticality_recent_pct"][tick]
                ),
            }
            if valid:
                metadata.update(
                    {
                        "innovation": float(batch["innovation"][tick]),
                        "filtered_basis": float(batch["filtered_basis"][tick]),
                    }
                )
            ticks.append(
                DetectionTick(
                    self.name,
                    row.instrument_id,
                    tick,
                    row.timestamp,
                    valid,
                    _basis(row) if valid else None,
                    score,
                    levels[int(batch["level"][tick])],
                    metadata,
                )
            )
        return ticks

    def step(self, tick: int, row: Observation) -> DetectionTick:
        health = self._observer.update(
            row.timestamp,
            row.physical_price,
            row.token_price,
        )
        basis = _basis(row)
        if not health.measurement_valid:
            criticality = (
                100.0 * sum(self._critical) / len(self._critical)
                if self._critical
                else 0.0
            )
            level = "MONITORING"
            if (
                not self.observer_only
                and self._valid_seen > self.warmup_events
                and criticality > self.criticality_audit_pct
            ):
                level = "AUDIT"
            return DetectionTick(
                self.name,
                row.instrument_id,
                tick,
                row.timestamp,
                False,
                None,
                None,
                level,
                {"criticality_recent_pct": criticality},
            )
        self._valid_seen += 1
        breach = health.mahalanobis_distance > self.mahalanobis_threshold
        self._critical.append(breach)
        criticality = (
            100.0 * sum(self._critical) / len(self._critical)
            if self._critical
            else 0.0
        )
        level = self._level(breach)
        if (
            not self.observer_only
            and self._valid_seen > self.warmup_events
            and criticality > self.criticality_audit_pct
        ):
            level = "AUDIT"
        return DetectionTick(
            self.name,
            row.instrument_id,
            tick,
            row.timestamp,
            True,
            basis,
            float(health.mahalanobis_distance),
            level,
            {
                "innovation": float(health.innovation),
                "filtered_basis": float(health.filtered_basis),
                "criticality_recent_pct": criticality,
            },
        )


def ticks_to_alerts(
    ticks: list[DetectionTick],
    *,
    merge_gap_ticks: int = 0,
) -> list[AlertEvent]:
    elevated = [tick for tick in ticks if tick.level != "MONITORING"]
    if not elevated:
        return []
    groups: list[list[DetectionTick]] = [[elevated[0]]]
    for tick in elevated[1:]:
        previous = groups[-1][-1]
        if tick.tick - previous.tick <= merge_gap_ticks + 1:
            groups[-1].append(tick)
        else:
            groups.append([tick])
    level_rank = {"MONITORING": 0, "BREACH": 1, "AUDIT": 2}
    return [
        AlertEvent(
            detector=group[0].detector,
            instrument_id=group[0].instrument_id,
            start_tick=group[0].tick,
            end_tick=group[-1].tick,
            start_timestamp=group[0].timestamp,
            end_timestamp=group[-1].timestamp,
            peak_score=max(
                (tick.score for tick in group if tick.score is not None),
                default=None,
            ),
            max_level=max(group, key=lambda tick: level_rank[tick.level]).level,
        )
        for group in groups
    ]
