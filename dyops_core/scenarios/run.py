"""Command-line entry point for the Dyops scenario catalog."""

from __future__ import annotations

import argparse
import json

from .catalog import get_scenario, list_scenarios


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a Dyops sentinel scenario")
    parser.add_argument("--list", action="store_true", help="list available scenarios")
    parser.add_argument(
        "--scenario",
        choices=list_scenarios(),
        help="catalog scenario to execute",
    )
    parser.add_argument("--seed", type=int, help="override the deterministic random seed")
    parser.add_argument("--json", action="store_true", help="emit the full result as JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if args.list:
        for name in list_scenarios():
            scenario = get_scenario(name)
            print(f"{name}: {scenario.description}")
        return 0

    if not args.scenario:
        parser.error("one of --list or --scenario is required")

    from .runner import run_scenario

    result = run_scenario(get_scenario(args.scenario, seed=args.seed))
    if args.json:
        print(result.to_json(indent=2))
        return 0

    metrics = result.metrics
    print(f"Scenario: {result.scenario}")
    print(result.description)
    print(f"Ticks: {metrics.total_ticks} ({metrics.invalid_ticks} invalid)")
    print(
        "Levels: "
        + ", ".join(
            f"{level}={count}" for level, count in metrics.level_counts.items()
        )
    )
    print(f"First escalation tick: {metrics.first_escalation_tick}")
    print(f"Max Mahalanobis: {metrics.max_mahalanobis}")
    print(f"Final criticality: {metrics.final_criticality_recent_pct}%")
    print("Expected: " + json.dumps(result.expected_outcomes, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
