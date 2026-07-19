"""Named instrument registry loaded from environment configuration."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from binance_feed import FeedMode, resolve_feed_mode


@dataclass(frozen=True)
class InstrumentConfig:
    id: str
    label: str
    feed_mode: FeedMode
    physical_symbol: str
    token_symbol: str
    synthetic: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_PRESETS: dict[str, InstrumentConfig] = {
    "stable": InstrumentConfig(
        id="default",
        label="USDC / USDT",
        feed_mode="stable",
        physical_symbol="USD",
        token_symbol="USDCUSDT",
        synthetic=True,
    ),
    "lst": InstrumentConfig(
        id="lst",
        label="ETH / stETH",
        feed_mode="lst",
        physical_symbol="ETHUSDT",
        token_symbol="STETHUSDT",
    ),
}


def _default_config() -> InstrumentConfig:
    mode = resolve_feed_mode()
    preset = _PRESETS[mode]
    return InstrumentConfig(
        id=os.environ.get("DYOPS_INSTRUMENT_ID", "default").strip() or "default",
        label=os.environ.get("DYOPS_INSTRUMENT_LABEL", preset.label).strip()
        or preset.label,
        feed_mode=mode,
        physical_symbol=preset.physical_symbol,
        token_symbol=preset.token_symbol,
        synthetic=preset.synthetic,
    )


def _from_mapping(raw: dict[str, Any]) -> InstrumentConfig:
    mode_raw = str(raw.get("feed_mode", raw.get("feed", "stable"))).lower()
    if mode_raw not in _PRESETS:
        raise ValueError(f"Unsupported instrument feed mode: {mode_raw}")
    mode: FeedMode = "lst" if mode_raw == "lst" else "stable"
    preset = _PRESETS[mode]
    instrument_id = str(raw.get("id", "")).strip()
    if not instrument_id:
        raise ValueError("Instrument id must not be empty")
    return InstrumentConfig(
        id=instrument_id,
        label=str(raw.get("label", instrument_id)).strip() or instrument_id,
        feed_mode=mode,
        physical_symbol=str(
            raw.get("physical_symbol", preset.physical_symbol)
        ).strip(),
        token_symbol=str(raw.get("token_symbol", preset.token_symbol)).strip(),
        synthetic=bool(raw.get("synthetic", preset.synthetic)),
    )


def load_instruments() -> tuple[InstrumentConfig, ...]:
    """Load ``DYOPS_INSTRUMENTS`` JSON or preset names; preserve single-feed defaults."""
    value = os.environ.get("DYOPS_INSTRUMENTS", "").strip()
    if not value:
        return (_default_config(),)

    if value.startswith("["):
        decoded = json.loads(value)
        if not isinstance(decoded, list):
            raise ValueError("DYOPS_INSTRUMENTS JSON must be an array")
        configs = tuple(_from_mapping(item) for item in decoded)
    else:
        names = [name.strip().lower() for name in value.split(",") if name.strip()]
        try:
            configs = tuple(_PRESETS[name] for name in names)
        except KeyError as exc:
            raise ValueError(f"Unknown instrument preset: {exc.args[0]}") from exc

    if not configs:
        raise ValueError("At least one instrument must be configured")
    ids = [config.id for config in configs]
    if len(ids) != len(set(ids)):
        raise ValueError("Instrument ids must be unique")
    return configs
