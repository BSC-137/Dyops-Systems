"""Unit tests for the deterministic Peg Health product object."""

from __future__ import annotations

import math
import unittest

from peg_health import (
    PEG_HEALTH_SCHEMA_VERSION,
    WATCH_MAHALANOBIS_FRACTION,
    apply_freshness,
    band_changed,
    build_peg_health,
    classify_band,
    empty_peg_health,
    measured_basis,
    replay_peg_health,
    watch_threshold,
)


class PegHealthClassifyTests(unittest.TestCase):
    def test_band_precedence_and_watch_threshold(self) -> None:
        breach = 3.0
        self.assertEqual(watch_threshold(breach), breach * WATCH_MAHALANOBIS_FRACTION)
        self.assertEqual(
            classify_band(
                level_name="MONITORING",
                mahalanobis=0.2,
                measurement_valid=True,
                breach_threshold=breach,
            ),
            "Healthy",
        )
        self.assertEqual(
            classify_band(
                level_name="MONITORING",
                mahalanobis=2.0,
                measurement_valid=True,
                breach_threshold=breach,
            ),
            "Watch",
        )
        self.assertEqual(
            classify_band(
                level_name="BREACH",
                mahalanobis=4.0,
                measurement_valid=True,
                breach_threshold=breach,
            ),
            "Breach",
        )
        self.assertEqual(
            classify_band(
                level_name="AUDIT",
                mahalanobis=0.1,
                measurement_valid=True,
                breach_threshold=breach,
            ),
            "Audit",
        )
        self.assertEqual(
            classify_band(
                level_name="MONITORING",
                mahalanobis=9.0,
                measurement_valid=True,
                breach_threshold=breach,
            ),
            "Breach",
        )

    def test_build_tracks_last_transition(self) -> None:
        first = build_peg_health(
            instrument_id="default",
            timestamp=1.0,
            physical_price=100.0,
            token_price=100.0,
            filtered_basis=0.0,
            mahalanobis=0.1,
            criticality=0.0,
            measurement_valid=True,
            level_name="MONITORING",
            breach_threshold=3.0,
            live=True,
            age_sec=0.1,
            stale_cutoff_sec=12.0,
        )
        self.assertEqual(first["schema_version"], PEG_HEALTH_SCHEMA_VERSION)
        self.assertEqual(first["band"], "Healthy")
        self.assertIsNone(first["last_transition"])
        self.assertAlmostEqual(first["basis"] or 0.0, 0.0, places=9)

        second = build_peg_health(
            instrument_id="default",
            timestamp=2.0,
            physical_price=100.0,
            token_price=90.0,
            filtered_basis=0.01,
            mahalanobis=12.0,
            criticality=5.0,
            measurement_valid=True,
            level_name="BREACH",
            breach_threshold=3.0,
            live=True,
            age_sec=0.0,
            stale_cutoff_sec=12.0,
            previous_band=first["band"],
            previous_transition=first["last_transition"],
        )
        self.assertEqual(second["band"], "Breach")
        self.assertEqual(
            second["last_transition"],
            {"from_band": "Healthy", "to_band": "Breach", "at": 2.0},
        )
        self.assertTrue(band_changed(first, second))
        self.assertFalse(band_changed(second, second))
        self.assertIn("Peg Health Breach", second["summary"])
        self.assertIn("not a default probability", second["explainability"].lower())
        self.assertNotIn("attestation", second["explainability"].lower())

    def test_apply_freshness_marks_stale(self) -> None:
        peg = empty_peg_health(
            instrument_id="default",
            stale_cutoff_sec=12.0,
            breach_threshold=3.0,
        )
        refreshed = apply_freshness(
            peg,
            live=False,
            age_sec=30.0,
            stale_cutoff_sec=12.0,
            breach_threshold=3.0,
        )
        self.assertFalse(refreshed["freshness"]["live"])
        self.assertEqual(refreshed["regime_tag"], "stale")
        self.assertIn("stale", refreshed["summary"].lower())

    def test_measured_basis(self) -> None:
        self.assertIsNone(measured_basis(0.0, 1.0))
        self.assertTrue(math.isclose(measured_basis(100.0, 100.0) or 0.0, 0.0))


class PegHealthReplayTests(unittest.TestCase):
    def test_replay_matches_live_build_path(self) -> None:
        rows = [
            {
                "timestamp": float(i),
                "physical_price": 100.0,
                "token_price": 100.0,
                "ingestion_source": "live",
                "scenario": None,
            }
            for i in range(20)
        ]
        rows.append(
            {
                "timestamp": 20.0,
                "physical_price": 100.0,
                "token_price": 90.0,
                "ingestion_source": "demo",
                "scenario": "sudden_depeg",
            }
        )
        peg = replay_peg_health(
            rows,
            instrument_id="default",
            breach_threshold=3.0,
            stale_cutoff_sec=12.0,
            live=True,
            age_sec=0.0,
        )
        self.assertEqual(peg["instrument_id"], "default")
        self.assertEqual(peg["band"], "Breach")
        self.assertGreater(peg["mahalanobis"], 3.0)
        self.assertEqual(peg["last_transition"]["to_band"], "Breach")
        self.assertEqual(peg["schema_version"], PEG_HEALTH_SCHEMA_VERSION)

    def test_empty_replay(self) -> None:
        peg = replay_peg_health(
            [],
            instrument_id="usdc",
            breach_threshold=3.0,
            stale_cutoff_sec=12.0,
        )
        self.assertEqual(peg["instrument_id"], "usdc")
        self.assertEqual(peg["band"], "Healthy")
        self.assertFalse(peg["freshness"]["live"])


if __name__ == "__main__":
    unittest.main()
