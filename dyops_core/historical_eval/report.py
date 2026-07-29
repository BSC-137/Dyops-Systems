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
        "dyops_regime",
        "dyops_regime_defaults",
        "dyops_calibrated_global",
        "dyops_calibrated_per_instrument",
    ]
    for name in order:
        if name not in detectors:
            continue
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
            "## Deployment posture (regime layer)",
            "",
            _deployment_posture(result),
            "",
            "## Ablations included",
            "",
            "- Dyops observer-only versus observer plus rolling criticality.",
            "- Production parameters versus globally and per-instrument calibrated parameters.",
            "- Replay warm-up sizes of 0, 10, 20, and 40 events.",
            "- Current Dyops policy versus an explicit slow-drift detector.",
            "- Current Dyops policy versus the deterministic Peg Health regime layer.",
            "- Sampling strides 2/3 and deterministic 10% missing-observation sensitivity.",
            "",
            "## Data and label limitations",
            "",
            *limitations,
            *catalog_limits,
            "",
        ]
    )


def _deployment_posture(result: dict[str, Any]) -> str:
    """Honest shadow-vs-active guidance from held-out recommendations."""
    recs = result.get("recommendations") or {}
    regime_keys = [
        key
        for key in (
            "dyops_regime_vs_absolute_basis",
            "dyops_regime_vs_rolling_z",
            "dyops_regime_vs_ewma_z",
        )
        if key in recs
    ]
    if not regime_keys:
        return (
            "Regime layer not present in this result. Live default remains "
            "**shadow** (`DYOPS_REGIME_ACTIVE` unset)."
        )
    beats = sum(
        1
        for key in regime_keys
        if "beats_baseline" in recs[key].get("verdict", "")
    )
    vs_current = recs.get("dyops_regime_vs_dyops_current", {})
    if beats == len(regime_keys):
        return (
            f"Regime layer beats absolute_basis / rolling_z / ewma_z on this fixture "
            f"({beats}/{len(regime_keys)}). Operators may consider "
            "`DYOPS_REGIME_ACTIVE=1` after partner validation. "
            f"Vs dyops_current: `{vs_current.get('verdict', 'n/a')}`."
        )
    return (
        f"Regime layer does **not** dominate absolute_basis / rolling_z / ewma_z on "
        f"this fixture ({beats}/{len(regime_keys)} baseline wins). "
        "Keep live default in **shadow** mode (emit `regime_tag` / reasoning; do not "
        "elevate Peg Health band) until a stronger held-out story exists. "
        f"Vs dyops_current: `{vs_current.get('verdict', 'n/a')}` — "
        f"{vs_current.get('basis', '')}. Scenario suite still requires "
        "`slow_peg_erosion` on `slow_drift` while sentinel `max_breaches` stays 0."
    )
