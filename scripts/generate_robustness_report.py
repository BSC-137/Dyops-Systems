#!/usr/bin/env python3
"""Generate partner-facing deterministic robustness evidence for Dyops."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DYOPS_CORE_DIR = ROOT / "dyops_core"
REPORTS_DIR = ROOT / "reports"

# The project keeps Python orchestration modules beside the PyO3 extension.
sys.path.insert(0, str(DYOPS_CORE_DIR))

from loguru import logger  # noqa: E402
from scenarios.catalog import get_catalog  # noqa: E402
from scenarios.runner import ScenarioResult, run_scenario  # noqa: E402
from sentinel import MAHALANOBIS_BREACH  # noqa: E402


def _display(value: Any, *, digits: int = 6) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    return str(value)


def _summary(results: list[ScenarioResult]) -> str:
    passed = sum(result.passed for result in results)
    failed = len(results) - passed
    slow_drift = next(result for result in results if result.scenario == "slow_drift")
    oracle_lag = next(result for result in results if result.scenario == "oracle_lag")
    slow_breaches = slow_drift.extended_metrics["breach_count"]
    oracle_audits = oracle_lag.extended_metrics["audit_count"]
    return (
        f"{passed} of {len(results)} deterministic scenarios passed their configured "
        f"thresholds ({failed} failed). Known limitations: slow_drift is intentionally "
        f"silent under the current policy ({slow_breaches} breach ticks), so this pack "
        "does not claim slow-drift alarming; oracle_lag is audit-heavy "
        f"({oracle_audits} audit ticks), showing that operational lag can sustain "
        "escalation even without a fundamental depeg."
    )


def _markdown(
    generated_at: str,
    results: list[ScenarioResult],
    observer_parameters: dict[str, Any],
) -> str:
    rows = []
    for result in results:
        metrics = result.extended_metrics
        rows.append(
            "| "
            + " | ".join(
                [
                    result.scenario,
                    "PASS" if result.passed else "FAIL",
                    _display(metrics["breach_count"]),
                    _display(metrics["time_to_first_breach_ticks"]),
                    _display(metrics["max_mahalanobis"]),
                    _display(metrics["replay_max_abs_error"]),
                    _display(metrics["processing_ms_per_1k_ticks"]),
                ]
            )
            + " |"
        )

    return "\n".join(
        [
            "# Dyops Robustness Evidence Report",
            "",
            f"Generated at: `{generated_at}`",
            "",
            "## Configuration",
            "",
            f"- Observer mean-reversion parameter (`theta`): `{observer_parameters['theta']}`",
            f"- Mahalanobis breach threshold: `{observer_parameters['mahalanobis_breach']}`",
            (
                "- Ring buffer capacity: "
                f"`{observer_parameters['ring_buffer_capacity']}`"
            ),
            (
                "- AUDIT snapshot cooldown: "
                f"`{observer_parameters['audit_cooldown_ticks']}` ticks"
            ),
            "- Process and measurement noise: production defaults",
            "",
            "## Scenario results",
            "",
            (
                "| scenario | pass/fail | breach_count | time_to_first_breach | "
                "max_mahalanobis | replay_error | processing_ms_per_1k |"
            ),
            "|---|---:|---:|---:|---:|---:|---:|",
            *rows,
            "",
            "## Summary",
            "",
            _summary(results),
            "",
            (
                "These results are deterministic synthetic validation evidence, not "
                "production performance guarantees or regulatory attestation. Runtime "
                "measurements vary by host."
            ),
            "",
        ]
    )


def main() -> int:
    logger.disable("sentinel")
    generated_at = datetime.now(timezone.utc).isoformat()
    results = [run_scenario(scenario) for scenario in get_catalog().values()]
    observer_parameters = {
        **results[0].observer_parameters,
        "mahalanobis_breach": MAHALANOBIS_BREACH,
    }
    payload = {
        "schema_version": 1,
        "generated_at_utc": generated_at,
        "passed": all(result.passed for result in results),
        "scenario_count": len(results),
        "passed_count": sum(result.passed for result in results),
        "observer_parameters": observer_parameters,
        "summary": _summary(results),
        "results": [result.to_dict() for result in results],
    }

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = REPORTS_DIR / "robustness_report.json"
    markdown_path = REPORTS_DIR / "robustness_report.md"
    json_path.write_text(
        json.dumps(payload, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        _markdown(generated_at, results, observer_parameters),
        encoding="utf-8",
    )
    print(f"Wrote {json_path.relative_to(ROOT)}")
    print(f"Wrote {markdown_path.relative_to(ROOT)}")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
