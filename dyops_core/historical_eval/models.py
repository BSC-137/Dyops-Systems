"""Versioned data and result models for historical evaluation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

DATASET_SCHEMA_VERSION = 1
CATALOG_SCHEMA_VERSION = 1
RESULT_SCHEMA_VERSION = 1
Level = Literal["MONITORING", "BREACH", "AUDIT"]


@dataclass(frozen=True)
class Observation:
    timestamp: float
    instrument_id: str
    physical_price: float
    token_price: float
    source: str
    sampling_interval_sec: float
    event_label: str | None = None

    @property
    def valid_price(self) -> bool:
        return (
            self.physical_price > 0.0
            and self.token_price > 0.0
            and self.physical_price not in (float("inf"), float("-inf"))
            and self.token_price not in (float("inf"), float("-inf"))
        )


@dataclass(frozen=True)
class Dataset:
    schema_version: int
    dataset_id: str
    observations: tuple[Observation, ...]
    path: str

    @property
    def instruments(self) -> tuple[str, ...]:
        return tuple(sorted({row.instrument_id for row in self.observations}))

    def for_instrument(self, instrument_id: str) -> tuple[Observation, ...]:
        return tuple(
            row for row in self.observations if row.instrument_id == instrument_id
        )


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    severity: Literal["warning", "error"]
    message: str
    instrument_id: str | None = None
    timestamp: float | None = None


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...]
    rows: int
    instruments: int

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "rows": self.rows,
            "instruments": self.instruments,
            "issues": [asdict(issue) for issue in self.issues],
        }


@dataclass(frozen=True)
class CatalogEvent:
    event_id: str
    instrument_id: str
    start_timestamp: float
    end_timestamp: float
    category: Literal[
        "dislocation", "stable", "oracle_failure", "data_failure", "recovery"
    ]
    split: Literal["tuning", "held_out"]
    provenance: str
    uncertainty_sec: float
    confidence: Literal["low", "medium", "high"]
    notes: str = ""

    @property
    def is_anomaly(self) -> bool:
        return self.category not in {"stable", "recovery"}


@dataclass(frozen=True)
class EventCatalog:
    schema_version: int
    catalog_id: str
    dataset_id: str
    events: tuple[CatalogEvent, ...]
    limitations: tuple[str, ...] = ()

    def split_events(self, split: str) -> tuple[CatalogEvent, ...]:
        return tuple(event for event in self.events if event.split == split)


@dataclass(frozen=True)
class DetectionTick:
    detector: str
    instrument_id: str
    tick: int
    timestamp: float
    measurement_valid: bool
    basis: float | None
    score: float | None
    level: Level
    details: dict[str, float | str | None] = field(default_factory=dict)


@dataclass(frozen=True)
class AlertEvent:
    detector: str
    instrument_id: str
    start_tick: int
    end_tick: int
    start_timestamp: float
    end_timestamp: float
    peak_score: float | None
    max_level: Level

    @property
    def duration_ticks(self) -> int:
        return self.end_tick - self.start_tick + 1

    @property
    def duration_sec(self) -> float:
        return max(0.0, self.end_timestamp - self.start_timestamp)
