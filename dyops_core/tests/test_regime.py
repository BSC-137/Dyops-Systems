"""Unit tests for the deterministic issuer regime layer."""

from __future__ import annotations

import unittest

from regime import (
    REGIME_FEED,
    REGIME_NOMINAL,
    REGIME_SLOW,
    REGIME_SUDDEN,
    RegimeEngine,
    regime_implies_band,
)
from scenarios.catalog import get_scenario
from scenarios.runner import run_scenario


class RegimeEngineUnitTests(unittest.TestCase):
    def test_slow_erosion_on_linear_creep(self) -> None:
        engine = RegimeEngine(mode="shadow")
        # Warm long window with flat basis, then creep ~0.5 bps/tick.
        seen_slow = False
        last = None
        for i in range(80):
            basis = 0.0 if i < 40 else (i - 39) * 0.00005
            last = engine.step(
                measurement_valid=True,
                basis=basis,
                innovation=0.0,
                mahalanobis=0.1,
                sentinel_level="MONITORING",
                live=True,
            )
            if last.regime_tag == REGIME_SLOW:
                seen_slow = True
        self.assertTrue(seen_slow)
        assert last is not None
        self.assertTrue(last.reasoning)
        self.assertEqual(last.mode, "shadow")

    def test_sudden_dislocation_keeps_strength(self) -> None:
        engine = RegimeEngine(mode="shadow")
        for i in range(20):
            engine.step(
                measurement_valid=True,
                basis=0.0,
                innovation=0.0,
                mahalanobis=0.1,
                live=True,
            )
        first = engine.step(
            measurement_valid=True,
            basis=0.02,
            innovation=0.02,
            mahalanobis=18.0,
            sentinel_level="BREACH",
            live=True,
        )
        second = engine.step(
            measurement_valid=True,
            basis=0.02,
            innovation=0.015,
            mahalanobis=12.0,
            sentinel_level="BREACH",
            live=True,
        )
        self.assertEqual(second.regime_tag, REGIME_SUDDEN)
        self.assertEqual(second.level, "BREACH")
        self.assertIn("sudden dislocation", second.reasoning.lower())
        self.assertIsNone(regime_implies_band(first))  # shadow

    def test_active_mode_elevates_band(self) -> None:
        engine = RegimeEngine(mode="active")
        for _ in range(20):
            engine.step(
                measurement_valid=True,
                basis=0.0,
                innovation=0.0,
                mahalanobis=0.1,
                live=True,
            )
        d = engine.step(
            measurement_valid=True,
            basis=0.02,
            innovation=0.02,
            mahalanobis=18.0,
            sentinel_level="BREACH",
            live=True,
        )
        self.assertEqual(regime_implies_band(d), "Breach")

    def test_invalid_streak_is_feed_fault(self) -> None:
        engine = RegimeEngine(mode="shadow", invalid_streak_fault=2)
        engine.step(
            measurement_valid=False,
            basis=None,
            innovation=None,
            mahalanobis=None,
            live=True,
        )
        d = engine.step(
            measurement_valid=False,
            basis=None,
            innovation=None,
            mahalanobis=None,
            live=True,
        )
        self.assertEqual(d.regime_tag, REGIME_FEED)
        self.assertIn("feed/oracle fault", d.reasoning.lower())


class RegimeScenarioTests(unittest.TestCase):
    def test_key_scenarios_emit_expected_regimes(self) -> None:
        slow = run_scenario(get_scenario("slow_drift"))
        self.assertTrue(slow.passed, slow.failures)
        self.assertGreaterEqual(
            slow.extended_metrics["regime_slow_erosion_ticks"], 1
        )
        self.assertEqual(slow.extended_metrics["breach_count"], 0)

        sudden = run_scenario(get_scenario("sudden_depeg"))
        self.assertTrue(sudden.passed, sudden.failures)
        self.assertGreaterEqual(
            sudden.extended_metrics["regime_sudden_dislocation_ticks"], 1
        )

        recovery = run_scenario(get_scenario("recovery_after_shock"))
        self.assertTrue(recovery.passed, recovery.failures)

        oracle = run_scenario(get_scenario("oracle_lag"))
        self.assertTrue(oracle.passed, oracle.failures)
        self.assertGreaterEqual(
            oracle.extended_metrics["regime_elevated_ticks"], 1
        )

        stable = run_scenario(get_scenario("stable_tracking"))
        self.assertTrue(stable.passed, stable.failures)
        self.assertEqual(stable.extended_metrics["regime_elevated_ticks"], 0)
        self.assertTrue(
            all(t.regime_tag == REGIME_NOMINAL for t in stable.ticks)
        )


if __name__ == "__main__":
    unittest.main()
