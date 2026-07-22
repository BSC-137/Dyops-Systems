#!/usr/bin/env python3
"""Compare the historical evaluator's former serial loop with policy batch."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from pathlib import Path

from historical_eval.data import load_dataset
from historical_eval.detectors import DyopsDetector


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticks", type=int, default=100_000)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    fixture = (
        Path(__file__).resolve().parent
        / "historical_eval/fixtures/synthetic_reference.csv"
    )
    source = load_dataset(fixture).for_instrument("synthetic-usd")
    rows = tuple(
        replace(
            source[index % len(source)],
            timestamp=float(index)
            * source[index % len(source)].sampling_interval_sec,
        )
        for index in range(args.ticks)
    )

    serial = DyopsDetector()
    serial.reset()
    start = time.perf_counter_ns()
    serial_ticks = [serial.step(index, row) for index, row in enumerate(rows)]
    serial_ns = time.perf_counter_ns() - start

    batch = DyopsDetector()
    start = time.perf_counter_ns()
    batch_ticks = batch.run(rows)
    batch_ns = time.perf_counter_ns() - start
    if serial_ticks != batch_ticks:
        raise RuntimeError("historical batch output differs from serial output")

    payload = {
        "ticks": args.ticks,
        "serial_ns_per_tick": serial_ns / args.ticks,
        "batch_ns_per_tick": batch_ns / args.ticks,
        "serial_ticks_per_sec": args.ticks * 1_000_000_000 / serial_ns,
        "batch_ticks_per_sec": args.ticks * 1_000_000_000 / batch_ns,
        "speedup": serial_ns / batch_ns,
        "exact_tick_parity": True,
    }
    encoded = json.dumps(payload, indent=2)
    print(encoded)
    if args.json_output is not None:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(encoded + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
