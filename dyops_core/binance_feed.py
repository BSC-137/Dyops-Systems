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
    out_q: "queue.Queue[tuple[float, float, float]]",
    stop: threading.Event,
) -> None:
    """USDC/USDT stablecoin basis: physical peg = 1.0 USDT notionally, token = USDC in USDT."""
    uri = BINANCE_WS_SINGLE.format("usdcusdt@trade")
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
                        out_q.put((time.time(), 1.0, price))
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
    out_q: "queue.Queue[tuple[float, float, float]]",
    stop: threading.Event,
) -> None:
    """LST basis: physical = ETH/USDT, token = stETH/USDT (log-ratio tracks discount)."""
    streams = "ethusdt@trade/stethusdt@trade"
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
                        if sym == "ETHUSDT":
                            last_eth = price
                        elif sym == "STETHUSDT":
                            last_steth = price
                        if last_eth is not None and last_steth is not None:
                            out_q.put((time.time(), last_eth, last_steth))
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
    out_q: "queue.Queue[tuple[float, float, float]]",
    stop: threading.Event,
    mode: FeedMode,
) -> None:
    if mode == "stable":
        await _consume_stable(out_q, stop)
    else:
        await _consume_lst(out_q, stop)


def _thread_target(
    out_q: "queue.Queue[tuple[float, float, float]]",
    stop: threading.Event,
    mode: FeedMode,
) -> None:
    asyncio.run(_async_main(out_q, stop, mode))


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


def resolve_feed_mode() -> FeedMode:
    raw = os.environ.get("DYOPS_BINANCE_FEED", "stable").strip().lower()
    return "lst" if raw in ("lst", "steth", "eth", "steth/eth") else "stable"
