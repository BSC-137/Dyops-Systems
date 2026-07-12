"""Command-line entry point for the Dyops scenario catalog."""

from __future__ import annotations

import argparse
import json

from .catalog import get_scenario, list_scenarios


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a Dyops sentinel scenario")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--list", action="store_true", help="list available scenarios")
    selection.add_argument(
        "--scenario",
        choices=list_scenarios(),
        help="catalog scenario to execute",
    )
    selection.add_argument("--all", action="store_true", help="run the entire catalog")
    parser.add_argument("--seed", type=int, help="override the deterministic random seed")
    parser.add_argument("--json", action="store_true", help="emit the full result as JSON")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 on threshold failure (implicitly enabled by --all)",
    )
    return parser


def _print_result(result: object) -> None:
    metrics = result.metrics
    status = "PASS" if result.passed else "FAIL"
    print(f"[{status}] Scenario: {result.scenario}")
    print(result.description)
    print(f"Ticks: {metrics.total_ticks} ({metrics.invalid_ticks} invalid)")
    print(
        "Levels: "
        + ", ".join(
            f"{level}={count}" for level, count in metrics.level_counts.items()
        )
    )
    print(
        "Detection: "
        f"breach={result.extended_metrics['time_to_first_breach_ticks']} ticks, "
        f"audit={result.extended_metrics['time_to_first_audit_ticks']} ticks"
    )
    print(
        "Replay max error: "
        f"{result.extended_metrics['replay_max_abs_error']}"
    )
    if result.failures:
        for failure in result.failures:
            print(f"  - {failure}")
    print("Expected: " + json.dumps(result.expected_outcomes, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)

    if args.list:
        for name in list_scenarios():
            scenario = get_scenario(name)
            print(f"{name}: {scenario.description}")
        return 0

    if not args.scenario and not args.all:
        parser.error("one of --list, --scenario, or --all is required")

    from .runner import run_scenario

    names = list_scenarios() if args.all else [args.scenario]
    results = [
        run_scenario(get_scenario(name, seed=args.seed))
        for name in names
    ]
    all_passed = all(result.passed for result in results)

    if args.json:
        if args.all:
            print(
                json.dumps(
                    {
                        "passed": all_passed,
                        "results": [result.to_dict() for result in results],
                    },
                    indent=2,
                    allow_nan=False,
                )
            )
        else:
            print(results[0].to_json(indent=2))
    else:
        for index, result in enumerate(results):
            if index:
                print()
            _print_result(result)

    strict = args.strict or args.all
    return 1 if strict and not all_passed else 0


if __name__ == "__main__":
    raise SystemExit(main())
