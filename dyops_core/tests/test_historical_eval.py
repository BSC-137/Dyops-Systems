from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import dyops_core
from loguru import logger

from historical_eval.calibration import calibrate
from historical_eval.catalog import load_catalog
from historical_eval.data import load_dataset, validate_dataset, write_csv_fixture
from historical_eval.detectors import (
    AbsoluteBasisDetector,
    DyopsDetector,
    RollingZDetector,
    ticks_to_alerts,
)
from historical_eval.metrics import bootstrap_ranges, evaluate_metrics
from historical_eval.models import CatalogEvent, Dataset, DetectionTick
from historical_eval.runner import evaluate
from sentinel import DyopsSentinel

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "historical_eval/fixtures/synthetic_reference.csv"
CATALOG = ROOT / "historical_eval/manifests/synthetic_reference.events.json"
logger.disable("sentinel")


class DatasetContractTests(unittest.TestCase):
    def test_fixture_contract_and_known_gap(self) -> None:
        dataset = load_dataset(FIXTURE)
        report = validate_dataset(dataset)

        self.assertTrue(report.valid)
        self.assertEqual(dataset.schema_version, 1)
        self.assertEqual(dataset.instruments, ("synthetic-usd",))
        self.assertEqual([issue.code for issue in report.issues], ["missing_samples"])

    def test_validation_finds_duplicates_nonpositive_and_source_changes(self) -> None:
        rows = [
            {
                "schema_version": 1,
                "dataset_id": "quality-test",
                "timestamp": 0,
                "instrument_id": "x",
                "physical_price": 100,
                "token_price": 100,
                "source": "a",
                "sampling_interval_sec": 60,
                "event_label": "",
            },
            {
                "schema_version": 1,
                "dataset_id": "quality-test",
                "timestamp": 0,
                "instrument_id": "x",
                "physical_price": 100,
                "token_price": 0,
                "source": "b",
                "sampling_interval_sec": 60,
                "event_label": "",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            write_csv_fixture(path, rows)
            report = validate_dataset(load_dataset(path))
        codes = {issue.code for issue in report.issues}
        self.assertFalse(report.valid)
        self.assertIn("duplicate_timestamp", codes)
        self.assertIn("non_monotonic_timestamp", codes)
        self.assertIn("non_positive_or_non_finite_price", codes)
        self.assertIn("source_change", codes)


class DetectorContractTests(unittest.TestCase):
    def test_all_detectors_emit_common_alerts_and_hold_invalid_state(self) -> None:
        dataset = load_dataset(FIXTURE)
        rows = dataset.for_instrument("synthetic-usd")[:15]
        absolute = AbsoluteBasisDetector(threshold_bps=5)
        rolling = RollingZDetector(window=5, threshold=2, min_periods=3)

        for detector in (absolute, rolling):
            ticks = detector.run(rows)
            self.assertEqual(len(ticks), len(rows))
            self.assertTrue(all(tick.detector == detector.name for tick in ticks))
            self.assertTrue(all(tick.instrument_id == "synthetic-usd" for tick in ticks))
            self.assertTrue(all(alert.start_tick <= alert.end_tick for alert in ticks_to_alerts(ticks)))

    def test_current_dyops_adapter_matches_production_sentinel_levels(self) -> None:
        rows = load_dataset(FIXTURE).for_instrument("synthetic-usd")
        adapter_ticks = DyopsDetector().run(rows)
        sentinel = DyopsSentinel(
            dyops_core.BasisObserver(
                name="historical-parity",
                theta=1.0,
                ring_buffer_capacity=1000,
            )
        )
        production_levels = [
            sentinel.process_event(
                row.timestamp,
                row.physical_price,
                row.token_price,
                schedule_background_audit=False,
            ).level.name
            for row in rows
        ]
        self.assertEqual([tick.level for tick in adapter_ticks], production_levels)


class MetricsAndCalibrationTests(unittest.TestCase):
    def test_event_metrics_are_alert_event_based(self) -> None:
        ticks = [
            DetectionTick("d", "x", index, float(index), True, 0.0, 4.0, level)
            for index, level in enumerate(
                ["MONITORING", "BREACH", "BREACH", "MONITORING", "BREACH"]
            )
        ]
        event = CatalogEvent(
            "heldout-event",
            "x",
            1.0,
            2.0,
            "dislocation",
            "held_out",
            "test construction",
            0.0,
            "high",
        )
        result = evaluate_metrics(ticks, (event,))

        self.assertEqual(result["alert_events"], 2)
        self.assertEqual(result["matched_alert_events"], 1)
        self.assertEqual(result["event_recall"], 1.0)
        self.assertEqual(result["precision_labelled_windows"], 0.5)

    def test_bootstrap_ranges_are_deterministic(self) -> None:
        metrics = {
            "per_event": [
                {
                    "detected": True,
                    "latency_sec": 10.0,
                    "recovery_latency_sec": 20.0,
                },
                {
                    "detected": False,
                    "latency_sec": None,
                    "recovery_latency_sec": None,
                },
            ]
        }
        self.assertEqual(
            bootstrap_ranges(metrics, seed=7, replicates=100),
            bootstrap_ranges(metrics, seed=7, replicates=100),
        )

    def test_calibration_cannot_see_post_tuning_rows(self) -> None:
        dataset = load_dataset(FIXTURE)
        catalog = load_catalog(CATALOG)
        candidates = [{"threshold_bps": 5.0}, {"threshold_bps": 50.0}]
        factory = lambda params: AbsoluteBasisDetector(**params)
        first = calibrate("absolute", factory, candidates, dataset, catalog)
        changed = tuple(
            replace(row, token_price=50.0)
            if row.timestamp > first.tuning_cutoff_timestamp
            else row
            for row in dataset.observations
        )
        mutated = Dataset(
            dataset.schema_version,
            dataset.dataset_id,
            changed,
            dataset.path,
        )
        second = calibrate("absolute", factory, candidates, mutated, catalog)

        self.assertEqual(first.parameters, second.parameters)
        self.assertEqual(first.tuning_metrics, second.tuning_metrics)
        self.assertTrue(
            all("heldout" not in event_id for event_id in first.tuning_event_ids)
        )


class EndToEndHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.dataset = load_dataset(FIXTURE)
        cls.catalog = load_catalog(CATALOG)
        cls.result = evaluate(cls.dataset, cls.catalog)

    def test_result_contains_required_comparisons_and_sensitivities(self) -> None:
        result = self.result
        self.assertEqual(result["evidence_class"], "synthetic_regression_fixture")
        self.assertIn("dyops_current", result["detectors"])
        self.assertIn("absolute_basis", result["detectors"])
        self.assertIn("rolling_z", result["detectors"])
        self.assertIn("rolling_mad", result["detectors"])
        self.assertIn("cusum", result["detectors"])
        self.assertIn("observer_only_vs_criticality", result["ablations"])
        self.assertIn("global_vs_per_instrument", result["ablations"])
        self.assertIn("replay_warmup_events", result["ablations"])
        self.assertIn("sampling_stride_2", result["sensitivity"]["dyops_current"])
        self.assertIn("missing_10pct", result["sensitivity"]["dyops_current"])
        json.dumps(result, allow_nan=False)

    def test_report_recommendations_do_not_claim_real_world_validation(self) -> None:
        verdicts = {
            item["verdict"] for item in self.result["recommendations"].values()
        }
        self.assertNotIn("proven_real_world_value", verdicts)
        self.assertTrue(
            any("synthetic" in limitation.lower() for limitation in self.result["limitations"])
        )


if __name__ == "__main__":
    unittest.main()
