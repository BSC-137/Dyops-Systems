from __future__ import annotations

import unittest
from dataclasses import dataclass

from loguru import logger

from sentinel import DyopsSentinel, SentinelLevel

logger.disable("sentinel")


@dataclass
class _Health:
    filtered_basis: float = 0.0
    innovation: float = 0.0
    mahalanobis_distance: float = 0.0
    measurement_valid: bool = True


@dataclass
class _WindowStats:
    mean: float = 0.0
    variance: float = 0.0
    kurtosis: float = 0.0


class _AlwaysCriticalObserver:
    def update(self, timestamp: float, physical: float, token: float) -> _Health:
        return _Health()

    def get_criticality_recent(self, window: int) -> float:
        return 20.0

    def get_window_stats(self) -> _WindowStats:
        return _WindowStats()

    def get_basis_velocity(self) -> tuple[float, float]:
        return 0.0, 0.0

    def get_last_innovations(self, count: int) -> list[float]:
        return [0.0]

    def get_criticality_score(self) -> float:
        return 20.0


class SentinelAuditPolicyTests(unittest.TestCase):
    def test_sustained_audit_snapshots_respect_cooldown(self) -> None:
        captured: list[dict[str, object]] = []
        sentinel = DyopsSentinel(
            _AlwaysCriticalObserver(),
            audit_cooldown_ticks=3,
            on_audit=captured.append,
        )

        results = [
            sentinel.process_event(
                float(tick),
                100.0,
                100.0,
                schedule_background_audit=False,
            )
            for tick in range(7)
        ]

        self.assertTrue(
            all(result.level == SentinelLevel.AUDIT for result in results)
        )
        self.assertEqual(
            [index for index, result in enumerate(results) if result.snapshot],
            [0, 3, 6],
        )
        self.assertEqual(len(captured), 3)

    def test_negative_audit_cooldown_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DyopsSentinel(
                _AlwaysCriticalObserver(),
                audit_cooldown_ticks=-1,
            )


if __name__ == "__main__":
    unittest.main()
