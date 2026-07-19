from __future__ import annotations

import json
import math
import unittest

from loguru import logger

from scenarios.catalog import get_catalog, get_scenario, list_scenarios

logger.disable("sentinel")


class ScenarioCatalogTests(unittest.TestCase):
    def test_catalog_contains_required_scenarios(self) -> None:
        self.assertEqual(
            list_scenarios(),
            [
                "stable_tracking",
                "slow_drift",
                "sudden_depeg",
                "gradual_then_break",
                "oracle_lag",
                "stale_feed",
                "recovery_after_shock",
                "fat_tail_noise",
            ],
        )
        for scenario in get_catalog().values():
            self.assertGreater(scenario.tick_count, 0)
            self.assertEqual(scenario.tick_count, len(scenario.physical_price))
            self.assertEqual(scenario.tick_count, len(scenario.token_price))

    def test_catalog_is_deterministic(self) -> None:
        first = get_scenario("slow_drift")
        second = get_scenario("slow_drift")
        self.assertEqual(first, second)


class ScenarioRunnerTests(unittest.TestCase):
    def test_runner_records_ticks_and_emits_strict_json(self) -> None:
        from scenarios.runner import run_scenario

        result = run_scenario(get_scenario("stale_feed"))
        payload = json.loads(result.to_json())

        self.assertEqual(payload["scenario"], "stale_feed")
        self.assertEqual(payload["instrument_id"], "default")
        self.assertEqual(len(payload["ticks"]), result.metrics.total_ticks)
        self.assertEqual(result.metrics.invalid_ticks, 6)
        self.assertTrue(
            all(
                tick["level"] in {"MONITORING", "BREACH", "AUDIT"}
                for tick in payload["ticks"]
            )
        )
        self.assertFalse(
            any(
                isinstance(value, float) and not math.isfinite(value)
                for tick in payload["ticks"]
                for value in tick.values()
            )
        )

    def test_runner_accepts_instrument_id_override(self) -> None:
        from scenarios.runner import run_scenario

        result = run_scenario(
            get_scenario("stable_tracking"),
            instrument_id="stable",
        )

        self.assertEqual(result.instrument_id, "stable")

    def test_recovery_scenario_returns_to_monitoring(self) -> None:
        from scenarios.runner import run_scenario

        result = run_scenario(get_scenario("recovery_after_shock"))
        self.assertIsNotNone(result.metrics.first_escalation_tick)
        self.assertIsNotNone(result.metrics.return_to_monitoring_tick)
        self.assertEqual(result.metrics.final_criticality_recent_pct, 0.0)


if __name__ == "__main__":
    unittest.main()
