from __future__ import annotations

import unittest

import dyops_core
from loguru import logger

from sentinel import DyopsSentinel, SentinelLevel

logger.disable("sentinel")


def _observer() -> dyops_core.BasisObserver:
    return dyops_core.BasisObserver(
        name="sentinel-policy-test",
        theta=1.0,
        ring_buffer_capacity=1000,
    )


class SentinelAuditPolicyTests(unittest.TestCase):
    def test_sustained_audit_snapshots_respect_cooldown(self) -> None:
        captured: list[dict[str, object]] = []
        sentinel = DyopsSentinel(
            _observer(),
            audit_criticality_pct=-1.0,
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
                _observer(),
                audit_cooldown_ticks=-1,
            )


if __name__ == "__main__":
    unittest.main()
