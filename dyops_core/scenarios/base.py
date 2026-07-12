"""Data model for deterministic Dyops monitoring scenarios."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Scenario:
    """A complete physical/token price stream and its expected behavior."""

    name: str
    description: str
    timestamps: list[float]
    physical_price: list[float]
    token_price: list[float]
    expected_outcomes: dict[str, Any]

    def __post_init__(self) -> None:
        lengths = {
            len(self.timestamps),
            len(self.physical_price),
            len(self.token_price),
        }
        if not self.name:
            raise ValueError("scenario name must not be empty")
        if lengths == {0}:
            raise ValueError("scenario must contain at least one tick")
        if len(lengths) != 1:
            raise ValueError("timestamps and price arrays must have equal lengths")
        if any(not math.isfinite(timestamp) for timestamp in self.timestamps):
            raise ValueError("timestamps must be finite")
        if any(
            current <= previous
            for previous, current in zip(self.timestamps, self.timestamps[1:])
        ):
            raise ValueError("timestamps must be strictly increasing")

    @property
    def tick_count(self) -> int:
        return len(self.timestamps)
