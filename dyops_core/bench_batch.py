#!/usr/bin/env python3
"""Benchmark: Python for-loop `.update()` vs native `.update_batch()` on 1M ticks."""

from __future__ import annotations

import time

import numpy as np

try:
    import dyops_core
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "dyops_core not importable. From this directory run: maturin develop --release\n"
        f"Import error: {e}"
    ) from e


def main() -> None:
    n = 1_000_000
    rng = np.random.default_rng(42)

    timestamps = np.cumsum(rng.exponential(scale=1e-3, size=n)).astype(np.float64)
    physical = np.exp(rng.normal(0.0, 0.01, size=n)).astype(np.float64) * 100.0
    token = np.exp(rng.normal(0.0, 0.01, size=n)).astype(np.float64) * 100.0

    theta = 1.0
    # Serial path
    obs_lo = dyops_core.BasisObserver("serial", theta, ring_buffer_capacity=0)
    t0 = time.perf_counter()
    for i in range(n):
        obs_lo.update(
            float(timestamps[i]),
            float(physical[i]),
            float(token[i]),
        )
    t_loop = time.perf_counter() - t0

    # Batch path (fresh observer for apples-to-apples filter state progression)
    obs_hi = dyops_core.BasisObserver("batch", theta, ring_buffer_capacity=0)
    t0 = time.perf_counter()
    out = obs_hi.update_batch(timestamps, physical, token)
    t_batch = time.perf_counter() - t0

    speedup = t_loop / t_batch if t_batch > 0 else float("inf")

    print(f"ticks:              {n:,}")
    print(f"loop update:        {t_loop:.3f} s")
    print(f"update_batch:       {t_batch:.3f} s")
    print(f"speedup (loop/batch): {speedup:.1f}x")
    print(
        "(Speedup is mostly PyO3 call overhead removed; typical is a few×–10×+ on "
        "release builds. Use `maturin develop --release`.)"
    )
    print(
        "outputs:",
        out["filtered_basis"].shape,
        out["innovation"].shape,
        out["mahalanobis_distance"].shape,
    )


if __name__ == "__main__":
    main()
