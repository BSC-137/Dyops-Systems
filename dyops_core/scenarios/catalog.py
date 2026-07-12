"""Deterministic catalog of market and data-quality stress scenarios."""

from __future__ import annotations

import math
import random
from collections.abc import Callable

from .base import Scenario

DEFAULT_TICKS = 240
WARMUP_TICKS = 80
BASE_PRICE = 100.0


def _timestamps(n: int) -> list[float]:
    return [float(i) for i in range(n)]


def _tracking_prices(
    rng: random.Random,
    n: int,
    *,
    physical_volatility: float = 0.015,
    tracking_noise: float = 0.008,
) -> tuple[list[float], list[float]]:
    physical: list[float] = []
    price = BASE_PRICE
    for i in range(n):
        price += rng.gauss(0.0, physical_volatility)
        physical.append(price + 0.04 * math.sin(i / 23.0))
    token = [price + rng.gauss(0.0, tracking_noise) for price in physical]
    return physical, token


def stable_tracking(seed: int = 7) -> Scenario:
    rng = random.Random(seed)
    physical, token = _tracking_prices(rng, DEFAULT_TICKS)
    return Scenario(
        name="stable_tracking",
        description="Healthy one-to-one tracking with small Gaussian observation noise.",
        timestamps=_timestamps(DEFAULT_TICKS),
        physical_price=physical,
        token_price=token,
        expected_outcomes={
            "stress_type": "baseline",
            "expected_terminal_level": "MONITORING",
            "expected_audit": False,
        },
    )


def slow_drift(seed: int = 11) -> Scenario:
    rng = random.Random(seed)
    physical, token = _tracking_prices(rng, DEFAULT_TICKS)
    drift_ticks = DEFAULT_TICKS - WARMUP_TICKS
    for i in range(WARMUP_TICKS, DEFAULT_TICKS):
        progress = (i - WARMUP_TICKS + 1) / drift_ticks
        token[i] *= 1.0 - 0.005 * progress  # 50 bps total.
    return Scenario(
        name="slow_drift",
        description="Token drifts 50 bps below the physical asset after a stable warm-up.",
        timestamps=_timestamps(DEFAULT_TICKS),
        physical_price=physical,
        token_price=token,
        expected_outcomes={
            "stress_type": "fundamental",
            "drift_bps": 50,
            "drift_ticks": drift_ticks,
            "expected_terminal_level": "MONITORING",
            "expected_escalation": False,
        },
    )


def sudden_depeg(seed: int = 13) -> Scenario:
    rng = random.Random(seed)
    physical, token = _tracking_prices(rng, DEFAULT_TICKS)
    shock_tick = 120
    for i in range(shock_tick, DEFAULT_TICKS):
        token[i] *= 0.98
    return Scenario(
        name="sudden_depeg",
        description="A persistent two-percent step depeg after normal tracking.",
        timestamps=_timestamps(DEFAULT_TICKS),
        physical_price=physical,
        token_price=token,
        expected_outcomes={
            "stress_type": "fundamental",
            "shock_tick": shock_tick,
            "shock_pct": -2.0,
            "expected_escalation": True,
        },
    )


def gradual_then_break(seed: int = 17) -> Scenario:
    rng = random.Random(seed)
    physical, token = _tracking_prices(rng, DEFAULT_TICKS)
    drift_start, break_tick = 70, 170
    for i in range(drift_start, break_tick):
        progress = (i - drift_start + 1) / (break_tick - drift_start)
        token[i] *= 1.0 - 0.005 * progress
    for i in range(break_tick, DEFAULT_TICKS):
        token[i] *= 0.98
    return Scenario(
        name="gradual_then_break",
        description="A 50 bps deterioration culminates in a persistent two-percent break.",
        timestamps=_timestamps(DEFAULT_TICKS),
        physical_price=physical,
        token_price=token,
        expected_outcomes={
            "stress_type": "fundamental",
            "drift_start_tick": drift_start,
            "break_tick": break_tick,
            "expected_escalation": True,
        },
    )


def oracle_lag(seed: int = 19) -> Scenario:
    rng = random.Random(seed)
    n = DEFAULT_TICKS
    lag_ticks = 5
    physical = [
        BASE_PRICE
        + 0.75 * math.sin(i / 8.0)
        + 0.35 * math.sin(i / 19.0)
        + rng.gauss(0.0, 0.01)
        for i in range(n)
    ]
    token = [
        physical[max(0, i - lag_ticks)] + rng.gauss(0.0, 0.006)
        for i in range(n)
    ]
    return Scenario(
        name="oracle_lag",
        description="Token oracle follows a moving physical asset with a five-tick delay.",
        timestamps=_timestamps(n),
        physical_price=physical,
        token_price=token,
        expected_outcomes={
            "stress_type": "operational",
            "lag_ticks": lag_ticks,
            "expected_transient_breaches": True,
        },
    )


def stale_feed(seed: int = 23) -> Scenario:
    rng = random.Random(seed)
    physical, token = _tracking_prices(rng, DEFAULT_TICKS)
    invalid_ticks = [95, 96, 97, 150, 151, 200]
    for tick in invalid_ticks[:3]:
        token[tick] = float("nan")
    for tick in invalid_ticks[3:5]:
        physical[tick] = 0.0
    token[invalid_ticks[-1]] = float("inf")
    return Scenario(
        name="stale_feed",
        description="A healthy stream interrupted by missing, zero, and infinite feed values.",
        timestamps=_timestamps(DEFAULT_TICKS),
        physical_price=physical,
        token_price=token,
        expected_outcomes={
            "stress_type": "operational",
            "invalid_ticks": invalid_ticks,
            "expected_invalid_measurements": len(invalid_ticks),
        },
    )


def recovery_after_shock(seed: int = 29) -> Scenario:
    rng = random.Random(seed)
    n = 420
    physical, token = _tracking_prices(rng, n)
    shock_tick, recovery_start, recovery_ticks = 100, 140, 60
    for i in range(shock_tick, recovery_start):
        token[i] *= 0.98
    for i in range(recovery_start, recovery_start + recovery_ticks):
        remaining = 1.0 - (i - recovery_start + 1) / recovery_ticks
        token[i] *= 1.0 - 0.02 * remaining
    return Scenario(
        name="recovery_after_shock",
        description="A two-percent depeg holds briefly, then smoothly returns to the peg.",
        timestamps=_timestamps(n),
        physical_price=physical,
        token_price=token,
        expected_outcomes={
            "stress_type": "fundamental_then_recovery",
            "shock_tick": shock_tick,
            "recovery_start_tick": recovery_start,
            "expected_return_to_monitoring": True,
        },
    )


def fat_tail_noise(seed: int = 31) -> Scenario:
    rng = random.Random(seed)
    physical, token = _tracking_prices(rng, DEFAULT_TICKS, tracking_noise=0.004)
    operational_spikes = [92, 111, 129]
    for i, tick in enumerate(operational_spikes):
        token[tick] *= 1.0 + (0.012 if i % 2 == 0 else -0.012)
    fundamental_start = 170
    for i in range(fundamental_start, DEFAULT_TICKS):
        # Student-t-like heavy tails from a Gaussian divided by sqrt(chi-square / df).
        df = 3
        denominator = math.sqrt(sum(rng.gauss(0.0, 1.0) ** 2 for _ in range(df)) / df)
        heavy_tail = rng.gauss(0.0, 0.0025) / max(denominator, 0.1)
        token[i] *= 0.992 + heavy_tail
    return Scenario(
        name="fat_tail_noise",
        description=(
            "Isolated operational outliers precede sustained heavy-tailed fundamental stress."
        ),
        timestamps=_timestamps(DEFAULT_TICKS),
        physical_price=physical,
        token_price=token,
        expected_outcomes={
            "stress_type": "operational_then_fundamental",
            "operational_spike_ticks": operational_spikes,
            "fundamental_start_tick": fundamental_start,
            "expected_high_kurtosis": True,
        },
    )


_SCENARIO_FACTORIES: dict[str, Callable[[int], Scenario]] = {
    "stable_tracking": stable_tracking,
    "slow_drift": slow_drift,
    "sudden_depeg": sudden_depeg,
    "gradual_then_break": gradual_then_break,
    "oracle_lag": oracle_lag,
    "stale_feed": stale_feed,
    "recovery_after_shock": recovery_after_shock,
    "fat_tail_noise": fat_tail_noise,
}


def list_scenarios() -> list[str]:
    """Return scenario names in stable catalog order."""

    return list(_SCENARIO_FACTORIES)


def get_scenario(name: str, *, seed: int | None = None) -> Scenario:
    """Build a fresh scenario by name."""

    try:
        factory = _SCENARIO_FACTORIES[name]
    except KeyError as exc:
        choices = ", ".join(list_scenarios())
        raise KeyError(f"unknown scenario {name!r}; choose from: {choices}") from exc
    return factory(seed) if seed is not None else factory()


def get_catalog() -> dict[str, Scenario]:
    """Build every catalog scenario."""

    return {name: factory() for name, factory in _SCENARIO_FACTORIES.items()}
