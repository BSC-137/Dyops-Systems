"""
Binance Spot WebSocket tick feed for basis telemetry (USDC/USDT or LST ETH–stETH).

Reconnection uses exponential backoff with jitter (circuit breaker style).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import queue
import random
import threading
import time
from collections.abc import Iterable
from typing import Literal

import websockets
from loguru import logger

FeedMode = Literal["stable", "lst"]

BINANCE_WS_SINGLE = "wss://stream.binance.com:9443/ws/{}"
BINANCE_WS_COMBINED = "wss://stream.binance.com:9443/stream?streams={}"

BACKOFF_INITIAL = 1.0
BACKOFF_MAX = 60.0
BACKOFF_MULTIPLIER = 2.0


def _jitter(base: float) -> float:
    return base + random.uniform(0.0, min(1.0, base * 0.25))


def _parse_trade(raw: dict) -> tuple[float, float] | None:
    """Return (timestamp_sec, price) from a Binance trade event."""
    if raw.get("e") != "trade":
        return None
    p = raw.get("p")
    t = raw.get("T") or raw.get("E")
    if p is None or t is None:
        return None
    return float(t) / 1000.0, float(p)


def _unwrap_message(text: str) -> list[dict]:
    data = json.loads(text)
    if isinstance(data, dict) and "data" in data and "stream" in data:
        inner = data["data"]
        return [inner] if isinstance(inner, dict) else []
    if isinstance(data, dict):
        return [data]
    return []


async def _consume_stable(
    out_q: "queue.Queue",
    stop: threading.Event,
    instrument_id: str | None = None,
    token_symbol: str = "USDCUSDT",
) -> None:
    """USDC/USDT stablecoin basis: physical peg = 1.0 USDT notionally, token = USDC in USDT."""
    uri = BINANCE_WS_SINGLE.format(f"{token_symbol.lower()}@trade")
    backoff = BACKOFF_INITIAL
    while not stop.is_set():
        try:
            async with websockets.connect(
                uri,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            ) as ws:
                backoff = BACKOFF_INITIAL
                logger.info("Binance WebSocket connected: {}", uri)
                while not stop.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=45.0)
                    except asyncio.TimeoutError:
                        continue
                    for raw in _unwrap_message(msg):
                        parsed = _parse_trade(raw)
                        if parsed is None:
                            continue
                        ts_wall, price = parsed
                        if not math.isfinite(price) or price <= 0:
                            continue
                        tick = (time.time(), 1.0, price)
                        out_q.put(
                            (instrument_id, *tick) if instrument_id is not None else tick
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if stop.is_set():
                break
            sleep_s = _jitter(backoff)
            logger.warning(
                "Binance WS stable feed error (retry in {:.1f}s): {}",
                sleep_s,
                exc,
            )
            await asyncio.sleep(sleep_s)
            backoff = min(backoff * BACKOFF_MULTIPLIER, BACKOFF_MAX)


async def _consume_lst(
    out_q: "queue.Queue",
    stop: threading.Event,
    instrument_id: str | None = None,
    physical_symbol: str = "ETHUSDT",
    token_symbol: str = "STETHUSDT",
) -> None:
    """LST basis: physical = ETH/USDT, token = stETH/USDT (log-ratio tracks discount)."""
    streams = f"{physical_symbol.lower()}@trade/{token_symbol.lower()}@trade"
    uri = BINANCE_WS_COMBINED.format(streams)
    last_eth: float | None = None
    last_steth: float | None = None
    backoff = BACKOFF_INITIAL
    while not stop.is_set():
        try:
            async with websockets.connect(
                uri,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
            ) as ws:
                backoff = BACKOFF_INITIAL
                last_eth = None
                last_steth = None
                logger.info("Binance WebSocket connected (LST): {}", uri)
                while not stop.is_set():
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=45.0)
                    except asyncio.TimeoutError:
                        continue
                    for raw in _unwrap_message(msg):
                        parsed = _parse_trade(raw)
                        if parsed is None:
                            continue
                        _, price = parsed
                        sym = str(raw.get("s", "")).upper()
                        if not math.isfinite(price) or price <= 0:
                            continue
                        if sym == physical_symbol.upper():
                            last_eth = price
                        elif sym == token_symbol.upper():
                            last_steth = price
                        if last_eth is not None and last_steth is not None:
                            tick = (time.time(), last_eth, last_steth)
                            out_q.put(
                                (instrument_id, *tick)
                                if instrument_id is not None
                                else tick
                            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if stop.is_set():
                break
            sleep_s = _jitter(backoff)
            logger.warning(
                "Binance WS LST feed error (retry in {:.1f}s): {}",
                sleep_s,
                exc,
            )
            await asyncio.sleep(sleep_s)
            backoff = min(backoff * BACKOFF_MULTIPLIER, BACKOFF_MAX)


async def _async_main(
    out_q: "queue.Queue",
    stop: threading.Event,
    mode: FeedMode,
    instrument_id: str | None = None,
    physical_symbol: str = "",
    token_symbol: str = "",
) -> None:
    if mode == "stable":
        await _consume_stable(
            out_q,
            stop,
            instrument_id,
            token_symbol or "USDCUSDT",
        )
    else:
        await _consume_lst(
            out_q,
            stop,
            instrument_id,
            physical_symbol or "ETHUSDT",
            token_symbol or "STETHUSDT",
        )


def _thread_target(
    out_q: "queue.Queue",
    stop: threading.Event,
    mode: FeedMode,
    instrument_id: str | None = None,
    physical_symbol: str = "",
    token_symbol: str = "",
) -> None:
    asyncio.run(
        _async_main(
            out_q,
            stop,
            mode,
            instrument_id,
            physical_symbol,
            token_symbol,
        )
    )


def start_binance_feed_thread(
    out_q: "queue.Queue[tuple[float, float, float]]",
    stop: threading.Event,
    *,
    mode: FeedMode | None = None,
) -> threading.Thread:
    """Spawn a daemon thread running the asyncio Binance consumer."""
    m: FeedMode
    if mode is not None:
        m = mode
    else:
        raw = os.environ.get("DYOPS_BINANCE_FEED", "stable").strip().lower()
        m = "lst" if raw in ("lst", "steth", "eth", "steth/eth") else "stable"
    t = threading.Thread(
        target=_thread_target,
        args=(out_q, stop, m),
        name="dyops-binance-ws",
        daemon=True,
    )
    t.start()
    return t


def start_instrument_feed_threads(
    out_q: "queue.Queue",
    stop: threading.Event,
    instruments: Iterable[tuple[str, FeedMode, str, str]],
) -> list[threading.Thread]:
    """Start one tagged Binance consumer thread per configured instrument."""
    threads: list[threading.Thread] = []
    for instrument_id, mode, physical_symbol, token_symbol in instruments:
        thread = threading.Thread(
            target=_thread_target,
            args=(
                out_q,
                stop,
                mode,
                instrument_id,
                physical_symbol,
                token_symbol,
            ),
            name=f"dyops-binance-{instrument_id}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    return threads


def _offline_thread_target(
    out_q: "queue.Queue",
    stop: threading.Event,
    instrument_id: str,
    mode: FeedMode,
    interval_sec: float,
) -> None:
    """Emit deterministic healthy telemetry without network access."""
    tick = 0
    phase = sum(ord(char) for char in instrument_id) % 17
    while not stop.is_set():
        angle = (tick + phase) * 0.17
        if mode == "stable":
            physical = 1.0
            token = 1.0 + 0.00003 * math.sin(angle)
        else:
            physical = 2000.0 + 2.0 * math.sin(angle)
            token = physical * (0.9995 + 0.00002 * math.cos(angle))
        out_q.put_nowait(
            (
                instrument_id,
                time.time(),
                physical,
                token,
                "offline",
            )
        )
        tick += 1
        stop.wait(interval_sec)


def start_offline_feed_threads(
    out_q: "queue.Queue",
    stop: threading.Event,
    instruments: Iterable[tuple[str, FeedMode, str, str]],
    *,
    interval_sec: float = 0.25,
) -> list[threading.Thread]:
    """Start deterministic, continuously current demo feeds with no external I/O."""
    threads: list[threading.Thread] = []
    for instrument_id, mode, _, _ in instruments:
        thread = threading.Thread(
            target=_offline_thread_target,
            args=(out_q, stop, instrument_id, mode, interval_sec),
            name=f"dyops-offline-{instrument_id}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    return threads


def resolve_feed_mode() -> FeedMode:
    raw = os.environ.get("DYOPS_BINANCE_FEED", "stable").strip().lower()
    return "lst" if raw in ("lst", "steth", "eth", "steth/eth") else "stable"
