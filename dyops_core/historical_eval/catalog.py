"""Versioned historical-event catalog loading and split enforcement."""

from __future__ import annotations

import json
from pathlib import Path

from .models import (
    CATALOG_SCHEMA_VERSION,
    CatalogEvent,
    Dataset,
    EventCatalog,
)


def load_catalog(path: Path | str) -> EventCatalog:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    version = int(raw.get("schema_version", 0))
    if version != CATALOG_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported catalog schema version {version}; "
            f"expected {CATALOG_SCHEMA_VERSION}"
        )
    events = tuple(CatalogEvent(**event) for event in raw.get("events", []))
    if not events:
        raise ValueError("Event catalog must contain at least one event")
    ids = [event.event_id for event in events]
    if len(ids) != len(set(ids)):
        raise ValueError("Event catalog contains duplicate event_id values")
    for event in events:
        if event.end_timestamp < event.start_timestamp:
            raise ValueError(f"Event {event.event_id} ends before it starts")
        if event.uncertainty_sec < 0:
            raise ValueError(f"Event {event.event_id} has negative uncertainty")
        if not event.provenance.strip():
            raise ValueError(f"Event {event.event_id} is missing label provenance")
    return EventCatalog(
        schema_version=version,
        catalog_id=str(raw["catalog_id"]),
        dataset_id=str(raw["dataset_id"]),
        events=events,
        limitations=tuple(str(item) for item in raw.get("limitations", [])),
    )


def validate_catalog(catalog: EventCatalog, dataset: Dataset) -> None:
    if catalog.dataset_id != dataset.dataset_id:
        raise ValueError(
            f"Catalog dataset_id {catalog.dataset_id!r} does not match "
            f"dataset {dataset.dataset_id!r}"
        )
    instruments = set(dataset.instruments)
    timestamps = {
        instrument: [row.timestamp for row in dataset.for_instrument(instrument)]
        for instrument in instruments
    }
    for event in catalog.events:
        if event.instrument_id not in instruments:
            raise ValueError(
                f"Event {event.event_id} references unknown instrument "
                f"{event.instrument_id!r}"
            )
        available = timestamps[event.instrument_id]
        if event.end_timestamp < available[0] or event.start_timestamp > available[-1]:
            raise ValueError(f"Event {event.event_id} lies outside the dataset range")


def tuning_catalog(catalog: EventCatalog) -> EventCatalog:
    """Return only tuning labels; held-out labels cannot reach calibration."""
    events = catalog.split_events("tuning")
    if not events:
        raise ValueError("Calibration requires at least one tuning event")
    return EventCatalog(
        schema_version=catalog.schema_version,
        catalog_id=f"{catalog.catalog_id}:tuning",
        dataset_id=catalog.dataset_id,
        events=events,
        limitations=catalog.limitations,
    )
