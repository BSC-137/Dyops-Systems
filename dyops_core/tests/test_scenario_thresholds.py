from __future__ import annotations

import copy
import io
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from unittest.mock import patch

from loguru import logger

from scenarios.base import Scenario
from scenarios.catalog import get_catalog
from scenarios.metrics import evaluate_thresholds
from scenarios.runner import ScenarioResult, run_scenario

logger.disable("sentinel")


class ScenarioThresholdTests(unittest.TestCase):
    results: dict[str, ScenarioResult]

    @classmethod
    def setUpClass(cls) -> None:
        cls.results = {
            name: run_scenario(scenario)
            for name, scenario in get_catalog().items()
        }

    def test_current_catalog_passes_thresholds(self) -> None:
        failures = {
            name: result.failures
            for name, result in self.results.items()
            if not result.passed
        }
        self.assertEqual(failures, {})

    def test_detection_metrics_use_anomaly_label(self) -> None:
        metrics = self.results["sudden_depeg"].extended_metrics
        self.assertEqual(metrics["shock_tick"], 120)
        self.assertEqual(metrics["anomaly_window"], [120, 239])
        self.assertEqual(metrics["time_to_first_breach_ticks"], 0)
        self.assertTrue(metrics["detection_recall"])
        self.assertGreater(metrics["p95_mahalanobis"], 0.0)

    def test_stale_feed_has_no_invalid_tick_escalations(self) -> None:
        metrics = self.results["stale_feed"].extended_metrics
        self.assertEqual(metrics["invalid_tick_count"], 6)
        self.assertEqual(metrics["escalations_on_invalid"], 0)
        self.assertTrue(metrics["invalid_tick_handling"])
        self.assertTrue(metrics["replay_consistency"])

    def test_oracle_lag_passes_latency_and_audit_occupancy(self) -> None:
        scenario = get_catalog()["oracle_lag"]
        result = self.results["oracle_lag"]
        thresholds = scenario.expected_outcomes["thresholds"]

        self.assertTrue(result.passed, result.failures)
        self.assertLessEqual(
            result.extended_metrics["time_to_first_breach_ticks"],
            thresholds["max_time_to_first_breach_ticks"],
        )
        self.assertLessEqual(
            result.extended_metrics["audit_pct"],
            thresholds["max_audit_pct"],
        )

    def test_fat_tail_passes_breach_and_false_positive_thresholds(self) -> None:
        scenario = get_catalog()["fat_tail_noise"]
        result = self.results["fat_tail_noise"]
        thresholds = scenario.expected_outcomes["thresholds"]

        self.assertTrue(result.passed, result.failures)
        self.assertGreaterEqual(
            result.extended_metrics["breach_count"],
            thresholds["min_breaches"],
        )
        self.assertLessEqual(
            result.extended_metrics["false_positive_rate"],
            thresholds["max_false_positive_rate"],
        )

    def test_evaluate_thresholds_reports_breach_limit(self) -> None:
        scenario = get_catalog()["sudden_depeg"]
        expected = copy.deepcopy(scenario.expected_outcomes)
        expected["thresholds"] = {"max_breaches": 0}
        strict_scenario = replace(scenario, expected_outcomes=expected)
        result = self.results["sudden_depeg"]

        failures = evaluate_thresholds(
            strict_scenario,
            result.extended_metrics,
            result.ticks,
        )

        self.assertTrue(any("breach_count" in failure for failure in failures))

    def test_recovery_threshold_observes_return_to_monitoring(self) -> None:
        result = self.results["recovery_after_shock"]
        self.assertIsNotNone(
            result.extended_metrics["return_to_monitoring_tick"]
        )
        self.assertIsNotNone(result.extended_metrics["snapshot_size_bytes"])

    def test_all_cli_is_strict_by_default(self) -> None:
        from scenarios import run as cli

        scenario = get_catalog()["stable_tracking"]
        expected = copy.deepcopy(scenario.expected_outcomes)
        expected["thresholds"] = {"min_breaches": 1}
        failing_scenario = replace(scenario, expected_outcomes=expected)

        with (
            patch.object(cli, "list_scenarios", return_value=["stable_tracking"]),
            patch.object(cli, "get_scenario", return_value=failing_scenario),
            redirect_stdout(io.StringIO()),
        ):
            exit_code = cli.main(["--all", "--json"])

        self.assertEqual(exit_code, 1)

    def test_all_summary_is_quiet_and_prints_final_banner(self) -> None:
        from scenarios import run as cli

        def cached_run(scenario: Scenario) -> ScenarioResult:
            return self.results[scenario.name]

        output = io.StringIO()
        with (
            patch("scenarios.runner.run_scenario", side_effect=cached_run),
            patch.object(logger, "disable") as disable_logging,
            redirect_stdout(output),
        ):
            exit_code = cli.main(["--all", "--summary"])

        rendered = output.getvalue()
        self.assertEqual(exit_code, 0)
        disable_logging.assert_called_once_with("sentinel")
        self.assertIn("[PASS] oracle_lag", rendered)
        self.assertIn("8/8 scenarios passed", rendered)
        self.assertNotIn('"ticks":', rendered)


if __name__ == "__main__":
    unittest.main()
