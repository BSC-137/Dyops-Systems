#!/usr/bin/env python3
"""Release-only Python/PyO3 sentinel and persistence microbenchmarks."""

from __future__ import annotations

import argparse
import gc
import json
import platform
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import dyops_core
from loguru import logger

from database import PersistenceManager
from sentinel import DyopsSentinel


def prices(tick: int) -> tuple[float, float]:
    offset = tick % 17 - 8
    return 100.0 + offset * 1e-5, 100.0


def sentinel(*, persistence: PersistenceManager | None = None) -> DyopsSentinel:
    observer = dyops_core.BasisObserver(
        "python-perf",
        1.0,
        ring_buffer_capacity=1000,
    )
    return DyopsSentinel(observer, persistence=persistence)


def run_ticks(
    target: DyopsSentinel,
    *,
    start_tick: int,
    count: int,
) -> tuple[int, float]:
    checksum = 0.0
    start = time.perf_counter_ns()
    for tick in range(start_tick, start_tick + count):
        physical, token = prices(tick)
        result = target.process_event(
            tick * 0.001,
            physical,
            token,
            schedule_background_audit=False,
        )
        checksum += result.health.filtered_basis + result.criticality_recent_pct
    return time.perf_counter_ns() - start, checksum


def result(
    name: str,
    ticks: int,
    elapsed_ns: int,
    checksum: float,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "case": name,
        "ticks": ticks,
        "elapsed_ns": elapsed_ns,
        "ns_per_tick": elapsed_ns / ticks,
        "ticks_per_sec": ticks * 1_000_000_000 / elapsed_ns,
        "checksum": checksum,
        **extra,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticks", type=int, default=200_000)
    parser.add_argument("--persistence-ticks", type=int, default=20_000)
    parser.add_argument("--warmup", type=int, default=10_000)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    if min(args.ticks, args.persistence_ticks, args.warmup) < 1:
        raise SystemExit("tick counts must be positive")

    logger.disable("sentinel")
    gc.disable()
    rows: list[dict[str, Any]] = []
    try:
        without_persistence = sentinel()
        run_ticks(without_persistence, start_tick=0, count=args.warmup)
        elapsed, checksum = run_ticks(
            without_persistence,
            start_tick=args.warmup,
            count=args.ticks,
        )
        rows.append(
            result(
                "python_sentinel_monitoring_persistence_off",
                args.ticks,
                elapsed,
                checksum,
            )
        )

        with tempfile.TemporaryDirectory(prefix="dyops-perf-") as tmp:
            persistence = PersistenceManager(Path(tmp) / "perf.db")
            with_persistence = sentinel(persistence=persistence)
            total_start = time.perf_counter_ns()
            enqueue_elapsed, checksum = run_ticks(
                with_persistence,
                start_tick=0,
                count=args.persistence_ticks,
            )
            persistence.close(timeout=60.0)
            end_to_end_elapsed = time.perf_counter_ns() - total_start
            rows.append(
                result(
                    "python_sentinel_monitoring_persistence_enqueue",
                    args.persistence_ticks,
                    enqueue_elapsed,
                    checksum,
                    end_to_end_ns=end_to_end_elapsed,
                    end_to_end_ns_per_tick=end_to_end_elapsed
                    / args.persistence_ticks,
                )
            )
    finally:
        gc.enable()
        logger.enable("sentinel")

    payload = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "release_build_required": True,
        "results": rows,
    }
    print(json.dumps(payload, indent=2))
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()
