"""Concise Markdown rendering for machine-readable evaluation results."""

from __future__ import annotations

from typing import Any


def _display(value: Any, digits: int = 3) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(result: dict[str, Any]) -> str:
    detectors = result["detectors"]
    rows: list[str] = []
    order = [
        "absolute_basis",
        "rolling_z",
        "ewma_z",
        "rolling_mad",
        "cusum",
        "slow_drift",
        "dyops_observer_only",
        "dyops_current",
        "dyops_calibrated_global",
        "dyops_calibrated_per_instrument",
    ]
    for name in order:
        metrics = detectors[name]["metrics"]
        rows.append(
            "| "
            + " | ".join(
                [
                    name,
                    _display(metrics.get("event_recall")),
                    _display(metrics.get("precision_labelled_windows")),
                    _display(metrics.get("false_alerts_per_instrument_day")),
                    _display(metrics.get("mean_detection_latency_sec"), 1),
                    _display(metrics.get("mean_detection_latency_ticks"), 1),
                    _display(metrics.get("mean_basis_at_detection_bps"), 1),
                    _display(metrics.get("mean_recovery_latency_sec"), 1),
                    _display(metrics.get("mean_alert_duration_sec"), 1),
                ]
            )
            + " |"
        )
    recommendation_lines = []
    for baseline, recommendation in result["recommendations"].items():
        recommendation_lines.append(
            f"- **{baseline}:** `{recommendation['verdict']}` — "
            f"{recommendation['basis']}."
        )
    limitations = [f"- {item}" for item in result["limitations"]]
    catalog_limits = [
        f"- Catalog: {item}" for item in result["catalog"]["limitations"]
    ]
    return "\n".join(
        [
            "# Dyops Historical Evaluation Harness Report",
            "",
            f"Generated at: `{result['generated_at_utc']}`",
            "",
            "## Evidence status",
            "",
            "**This run uses a legal synthetic fixture for harness regression. It is not "
            "historical market validation and cannot answer the primary product question "
            "conclusively.** Replace the fixture with licensed/vendor-neutral historical "
            "CSV or Parquet data and a provenance-backed catalog before making production "
            "threshold changes.",
            "",
            f"- Dataset: `{result['dataset']['dataset_id']}` "
            f"({result['dataset']['rows']} rows)",
            f"- Tuning events: `{', '.join(result['catalog']['tuning_event_ids'])}`",
            f"- Held-out events: `{', '.join(result['catalog']['held_out_event_ids'])}`",
            "",
            "## Held-out detector comparison",
            "",
            "| detector | event recall | window precision | false alerts / instrument-day "
            "| latency sec | latency ticks | basis at detection bps | recovery sec "
            "| alert duration sec |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            *rows,
            "",
            "Event windows include their recorded uncertainty. Runtime and memory are "
            "available in the JSON as supporting diagnostics, not primary ranking metrics.",
            "",
            "## Recommendation",
            "",
            *recommendation_lines,
            "",
            "These verdicts apply only to this held-out synthetic fixture. A tie or win here "
            "does not establish operational value on market history.",
            "",
            "## Ablations included",
            "",
            "- Dyops observer-only versus observer plus rolling criticality.",
            "- Production parameters versus globally and per-instrument calibrated parameters.",
            "- Replay warm-up sizes of 0, 10, 20, and 40 events.",
            "- Current Dyops policy versus an explicit slow-drift detector.",
            "- Sampling strides 2/3 and deterministic 10% missing-observation sensitivity.",
            "",
            "## Data and label limitations",
            "",
            *limitations,
            *catalog_limits,
            "",
        ]
    )
