"""CLI for validating, calibrating, and evaluating historical fixtures."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import load_catalog, validate_catalog
from .data import load_dataset, validate_dataset
from .report import render_markdown
from .runner import evaluate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "calibrate", "evaluate"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--dataset", type=Path, required=True)
        sub.add_argument("--catalog", type=Path, required=True)
        if command in {"calibrate", "evaluate"}:
            sub.add_argument("--json-output", type=Path)
        if command == "evaluate":
            sub.add_argument("--markdown-output", type=Path)
            sub.add_argument("--warmup-events", type=int, default=20)
    return parser


def _load(paths: argparse.Namespace) -> tuple[Any, Any, Any]:
    dataset = load_dataset(paths.dataset)
    catalog = load_catalog(paths.catalog)
    validate_catalog(catalog, dataset)
    validation = validate_dataset(dataset)
    return dataset, catalog, validation


def _write_json(path: Path | None, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, indent=2, allow_nan=False, sort_keys=True) + "\n"
    if path is None:
        print(raw, end="")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    print(f"Wrote {path}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    dataset, catalog, validation = _load(args)
    if args.command == "validate":
        payload = {
            "dataset_id": dataset.dataset_id,
            "catalog_id": catalog.catalog_id,
            **validation.to_dict(),
        }
        print(json.dumps(payload, indent=2, allow_nan=False, sort_keys=True))
        return 0 if validation.valid else 1
    if not validation.valid:
        print(json.dumps(validation.to_dict(), indent=2, allow_nan=False))
        return 1

    result = evaluate(
        dataset,
        catalog,
        warmup_events=getattr(args, "warmup_events", 20),
    )
    result["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    if args.command == "calibrate":
        _write_json(
            args.json_output,
            {
                "schema_version": result["schema_version"],
                "generated_at_utc": result["generated_at_utc"],
                "dataset_id": dataset.dataset_id,
                "tuning_event_ids": result["catalog"]["tuning_event_ids"],
                "held_out_labels_accessed": False,
                "calibration": result["calibration"],
            },
        )
        return 0

    _write_json(args.json_output, result)
    markdown = render_markdown(result)
    if args.markdown_output is None:
        print(markdown)
    else:
        args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_output.write_text(markdown, encoding="utf-8")
        print(f"Wrote {args.markdown_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
