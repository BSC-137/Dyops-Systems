"""CSV/Parquet loading and data-quality validation."""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .models import (
    DATASET_SCHEMA_VERSION,
    Dataset,
    Observation,
    ValidationIssue,
    ValidationReport,
)

REQUIRED_COLUMNS = {
    "schema_version",
    "dataset_id",
    "timestamp",
    "instrument_id",
    "physical_price",
    "token_price",
    "source",
    "sampling_interval_sec",
}


def _optional_label(value: Any) -> str | None:
    text = "" if value is None else str(value).strip()
    return text or None


def _row_to_observation(row: dict[str, Any]) -> Observation:
    return Observation(
        timestamp=float(row["timestamp"]),
        instrument_id=str(row["instrument_id"]).strip(),
        physical_price=float(row["physical_price"]),
        token_price=float(row["token_price"]),
        source=str(row["source"]).strip(),
        sampling_interval_sec=float(row["sampling_interval_sec"]),
        event_label=_optional_label(row.get("event_label")),
    )


def _load_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or ())
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise ValueError(f"Dataset is missing required columns: {sorted(missing)}")
        return list(reader)


def _load_parquet(path: Path) -> list[dict[str, Any]]:
    try:
        import pyarrow.parquet as pq  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(
            "Parquet support is optional; install pyarrow or convert the fixture to CSV"
        ) from exc
    table = pq.read_table(path)
    missing = REQUIRED_COLUMNS - set(table.column_names)
    if missing:
        raise ValueError(f"Dataset is missing required columns: {sorted(missing)}")
    return table.to_pylist()


def load_dataset(path: Path | str) -> Dataset:
    """Load a schema-v1 CSV or Parquet file without vendor-specific assumptions."""
    source_path = Path(path)
    suffix = source_path.suffix.lower()
    if suffix == ".csv":
        raw_rows = _load_csv(source_path)
    elif suffix in {".parquet", ".pq"}:
        raw_rows = _load_parquet(source_path)
    else:
        raise ValueError("Historical datasets must be CSV or Parquet")
    if not raw_rows:
        raise ValueError("Historical dataset is empty")

    versions = {int(row["schema_version"]) for row in raw_rows}
    if versions != {DATASET_SCHEMA_VERSION}:
        raise ValueError(
            f"Unsupported dataset schema versions {sorted(versions)}; "
            f"expected {DATASET_SCHEMA_VERSION}"
        )
    dataset_ids = {str(row["dataset_id"]).strip() for row in raw_rows}
    if len(dataset_ids) != 1 or not next(iter(dataset_ids)):
        raise ValueError("All rows must contain one non-empty dataset_id")
    observations = tuple(_row_to_observation(row) for row in raw_rows)
    return Dataset(
        schema_version=DATASET_SCHEMA_VERSION,
        dataset_id=next(iter(dataset_ids)),
        observations=observations,
        path=str(source_path),
    )


def validate_dataset(dataset: Dataset) -> ValidationReport:
    """Return explicit quality findings; callers decide whether warnings are acceptable."""
    issues: list[ValidationIssue] = []
    by_instrument: dict[str, list[Observation]] = defaultdict(list)
    for row in dataset.observations:
        by_instrument[row.instrument_id].append(row)
        if not row.instrument_id:
            issues.append(
                ValidationIssue("missing_instrument", "error", "instrument_id is empty")
            )
        if not row.source:
            issues.append(
                ValidationIssue(
                    "source_gap",
                    "error",
                    "source is empty",
                    row.instrument_id or None,
                    row.timestamp,
                )
            )
        if not math.isfinite(row.timestamp):
            issues.append(
                ValidationIssue(
                    "invalid_timestamp",
                    "error",
                    "timestamp must be finite",
                    row.instrument_id,
                    row.timestamp,
                )
            )
        if not math.isfinite(row.sampling_interval_sec) or row.sampling_interval_sec <= 0:
            issues.append(
                ValidationIssue(
                    "invalid_sampling_interval",
                    "error",
                    "sampling_interval_sec must be finite and positive",
                    row.instrument_id,
                    row.timestamp,
                )
            )
        if (
            not math.isfinite(row.physical_price)
            or not math.isfinite(row.token_price)
            or row.physical_price <= 0
            or row.token_price <= 0
        ):
            issues.append(
                ValidationIssue(
                    "non_positive_or_non_finite_price",
                    "error",
                    "prices must be finite and positive",
                    row.instrument_id,
                    row.timestamp,
                )
            )

    for instrument_id, rows in by_instrument.items():
        seen: set[float] = set()
        previous: Observation | None = None
        for row in rows:
            if row.timestamp in seen:
                issues.append(
                    ValidationIssue(
                        "duplicate_timestamp",
                        "error",
                        "duplicate timestamp within instrument",
                        instrument_id,
                        row.timestamp,
                    )
                )
            seen.add(row.timestamp)
            if previous is not None:
                delta = row.timestamp - previous.timestamp
                if delta <= 0:
                    issues.append(
                        ValidationIssue(
                            "non_monotonic_timestamp",
                            "error",
                            "timestamps must be strictly increasing per instrument",
                            instrument_id,
                            row.timestamp,
                        )
                    )
                expected = previous.sampling_interval_sec
                if delta > expected * 1.5:
                    missing = max(1, round(delta / expected) - 1)
                    issues.append(
                        ValidationIssue(
                            "missing_samples",
                            "warning",
                            f"approximately {missing} sample(s) missing before this row",
                            instrument_id,
                            row.timestamp,
                        )
                    )
                if row.source != previous.source:
                    issues.append(
                        ValidationIssue(
                            "source_change",
                            "warning",
                            f"source changed from {previous.source!r} to {row.source!r}",
                            instrument_id,
                            row.timestamp,
                        )
                    )
            previous = row
    return ValidationReport(
        issues=tuple(issues),
        rows=len(dataset.observations),
        instruments=len(by_instrument),
    )


def write_csv_fixture(path: Path | str, rows: Iterable[dict[str, Any]]) -> None:
    """Small-fixture helper used by tests; raw licensed downloads should stay external."""
    destination = Path(path)
    materialized = list(rows)
    if not materialized:
        raise ValueError("Cannot write an empty fixture")
    columns = [
        "schema_version",
        "dataset_id",
        "timestamp",
        "instrument_id",
        "physical_price",
        "token_price",
        "source",
        "sampling_interval_sec",
        "event_label",
    ]
    with destination.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(materialized)
