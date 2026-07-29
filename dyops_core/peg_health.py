"""
Peg Health — versioned, deterministic product object for peg / basis monitoring.

Partners integrate against this surface without reading Kalman / observer internals.
Gemini never alters Peg Health. Bands are operational classification only — not
regulatory attestation, default probability, or legal advice.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

# Keep in sync with sentinel / Rust policy defaults; imported lazily where needed
# so this module stays usable for pure unit tests without a built extension.

PEG_HEALTH_SCHEMA_VERSION = "1.0"

# Watch when valid Mahalanobis exceeds this fraction of the breach threshold.
WATCH_MAHALANOBIS_FRACTION = 0.5

_BAND_HEALTHY = "Healthy"
_BAND_WATCH = "Watch"
_BAND_BREACH = "Breach"
_BAND_AUDIT = "Audit"

PEG_BANDS = (_BAND_HEALTHY, _BAND_WATCH, _BAND_BREACH, _BAND_AUDIT)

_SUMMARY_MAX = 200
_EXPLAIN_MAX = 280


def _clip(text: str, max_len: int) -> str:
    t = text.strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1].rstrip() + "…"


def _finite_or_none(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


def measured_basis(physical_price: float, token_price: float) -> float | None:
    """Log-basis ln(physical / token) when both prices are finite and positive."""
    if (
        physical_price > 0.0
        and token_price > 0.0
        and math.isfinite(physical_price)
        and math.isfinite(token_price)
    ):
        return math.log(physical_price / token_price)
    return None


def watch_threshold(breach_threshold: float) -> float:
    return float(breach_threshold) * WATCH_MAHALANOBIS_FRACTION


def classify_band(
    *,
    level_name: str,
    mahalanobis: float,
    measurement_valid: bool,
    breach_threshold: float,
) -> str:
    """
    Map sentinel level + Mahalanobis into a partner-facing Peg Health band.

    Precedence: Audit > Breach > Watch > Healthy.
    """
    level = (level_name or "").upper()
    if level == "AUDIT":
        return _BAND_AUDIT
    if level == "BREACH":
        return _BAND_BREACH
    if (
        measurement_valid
        and math.isfinite(mahalanobis)
        and mahalanobis > float(breach_threshold)
    ):
        return _BAND_BREACH
    if (
        measurement_valid
        and math.isfinite(mahalanobis)
        and mahalanobis > watch_threshold(breach_threshold)
    ):
        return _BAND_WATCH
    return _BAND_HEALTHY


def regime_tag_for(
    band: str,
    *,
    measurement_valid: bool,
    live: bool,
) -> str:
    """Stable regime tags for dashboards and webhooks (no probability language)."""
    if not live:
        return "stale"
    if not measurement_valid:
        return "measurement_withheld"
    if band == _BAND_AUDIT:
        return "escalated_review"
    if band == _BAND_BREACH:
        return "correlation_fracture"
    if band == _BAND_WATCH:
        return "elevated_surprise"
    return "nominal"


def _narrative(
    *,
    band: str,
    mahalanobis: float | None,
    criticality: float,
    live: bool,
    measurement_valid: bool,
    breach_threshold: float,
) -> tuple[str, str]:
    m_s = f"{mahalanobis:.2f}" if mahalanobis is not None else "n/a"
    if not live:
        summary = f"Peg Health {band} · feed stale · M {m_s} · crit {criticality:.1f}%."
        explain = (
            "Freshness is outside the live cutoff; band reflects the last processed "
            "tick. Resume the feed before treating Peg Health as current."
        )
    elif not measurement_valid:
        summary = f"Peg Health {band} · measurement withheld · crit {criticality:.1f}%."
        explain = (
            "Invalid or non-positive prices; observer did not apply this tick. "
            "Peg Health band holds from sentinel policy without a new measurement."
        )
    elif band == _BAND_AUDIT:
        summary = (
            f"Peg Health Audit · rolling criticality {criticality:.1f}% · M {m_s}."
        )
        explain = (
            "Rolling criticality crossed the audit threshold. Deterministic Peg Health "
            "escalated to Audit; optional Gemini narrative never changes this band."
        )
    elif band == _BAND_BREACH:
        summary = (
            f"Peg Health Breach · Mahalanobis {m_s} above {breach_threshold:g} · "
            f"crit {criticality:.1f}%."
        )
        explain = (
            "Normalized innovation exceeded the sentinel breach threshold — operational "
            "correlation fracture signal, not a default probability."
        )
    elif band == _BAND_WATCH:
        wt = watch_threshold(breach_threshold)
        summary = (
            f"Peg Health Watch · Mahalanobis {m_s} above watch {wt:g} · "
            f"crit {criticality:.1f}%."
        )
        explain = (
            "Surprise is elevated relative to the model band but has not crossed the "
            "breach threshold. Escalate on further band transitions."
        )
    else:
        summary = (
            f"Peg Health Healthy · Mahalanobis {m_s} within band · "
            f"crit {criticality:.1f}%."
        )
        explain = (
            "Filtered basis and normalized innovation sit within operational norms. "
            "Poll GET /api/peg_health or subscribe to /ws/telemetry for updates."
        )
    return _clip(summary, _SUMMARY_MAX), _clip(explain, _EXPLAIN_MAX)


def transition_record(
    *,
    previous_band: str | None,
    band: str,
    timestamp: float,
    previous_transition: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return a new last_transition dict when the band changes; else keep prior."""
    if previous_band is None:
        return (
            dict(previous_transition)
            if previous_transition is not None
            else None
        )
    if previous_band == band:
        return (
            dict(previous_transition)
            if previous_transition is not None
            else None
        )
    return {
        "from_band": previous_band,
        "to_band": band,
        "at": float(timestamp),
    }


def build_peg_health(
    *,
    instrument_id: str,
    timestamp: float,
    physical_price: float,
    token_price: float,
    filtered_basis: float,
    mahalanobis: float,
    criticality: float,
    measurement_valid: bool,
    level_name: str,
    breach_threshold: float,
    live: bool,
    age_sec: float | None,
    stale_cutoff_sec: float,
    previous_band: str | None = None,
    previous_transition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a versioned Peg Health snapshot (JSON-serializable)."""
    band = classify_band(
        level_name=level_name,
        mahalanobis=mahalanobis,
        measurement_valid=measurement_valid,
        breach_threshold=breach_threshold,
    )
    m = _finite_or_none(mahalanobis)
    basis = measured_basis(physical_price, token_price)
    fb = _finite_or_none(filtered_basis)
    summary, explainability = _narrative(
        band=band,
        mahalanobis=m,
        criticality=float(criticality),
        live=live,
        measurement_valid=measurement_valid,
        breach_threshold=breach_threshold,
    )
    return {
        "schema_version": PEG_HEALTH_SCHEMA_VERSION,
        "instrument_id": instrument_id,
        "timestamp": float(timestamp),
        "band": band,
        "basis": basis,
        "filtered_basis": fb if fb is not None else 0.0,
        "mahalanobis": m if m is not None else 0.0,
        "criticality": float(criticality),
        "freshness": {
            "live": bool(live),
            "age_sec": age_sec,
            "stale_cutoff_sec": float(stale_cutoff_sec),
        },
        "regime_tag": regime_tag_for(
            band,
            measurement_valid=measurement_valid,
            live=live,
        ),
        "summary": summary,
        "explainability": explainability,
        "last_transition": transition_record(
            previous_band=previous_band,
            band=band,
            timestamp=timestamp,
            previous_transition=previous_transition,
        ),
        "measurement_valid": bool(measurement_valid),
        "level": (level_name or "MONITORING").upper(),
    }


def empty_peg_health(
    *,
    instrument_id: str,
    stale_cutoff_sec: float,
    breach_threshold: float,
) -> dict[str, Any]:
    """Peg Health before any tick has been processed for the instrument."""
    summary, explainability = _narrative(
        band=_BAND_HEALTHY,
        mahalanobis=None,
        criticality=0.0,
        live=False,
        measurement_valid=True,
        breach_threshold=breach_threshold,
    )
    return {
        "schema_version": PEG_HEALTH_SCHEMA_VERSION,
        "instrument_id": instrument_id,
        "timestamp": 0.0,
        "band": _BAND_HEALTHY,
        "basis": None,
        "filtered_basis": 0.0,
        "mahalanobis": 0.0,
        "criticality": 0.0,
        "freshness": {
            "live": False,
            "age_sec": None,
            "stale_cutoff_sec": float(stale_cutoff_sec),
        },
        "regime_tag": "stale",
        "summary": summary,
        "explainability": explainability,
        "last_transition": None,
        "measurement_valid": True,
        "level": "MONITORING",
    }


def apply_freshness(
    peg: Mapping[str, Any],
    *,
    live: bool,
    age_sec: float | None,
    stale_cutoff_sec: float,
    breach_threshold: float,
) -> dict[str, Any]:
    """Return a copy of Peg Health with refreshed freshness / regime / copy."""
    out = dict(peg)
    out["freshness"] = {
        "live": bool(live),
        "age_sec": age_sec,
        "stale_cutoff_sec": float(stale_cutoff_sec),
    }
    measurement_valid = bool(out.get("measurement_valid", True))
    band = str(out["band"])
    out["regime_tag"] = regime_tag_for(
        band,
        measurement_valid=measurement_valid,
        live=live,
    )
    mah = out.get("mahalanobis")
    mah_f = float(mah) if mah is not None and math.isfinite(float(mah)) else None
    summary, explainability = _narrative(
        band=band,
        mahalanobis=mah_f,
        criticality=float(out.get("criticality") or 0.0),
        live=live,
        measurement_valid=measurement_valid,
        breach_threshold=breach_threshold,
    )
    out["summary"] = summary
    out["explainability"] = explainability
    return out


def band_changed(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> bool:
    if previous is None:
        return False
    return previous.get("band") != current.get("band")


def replay_peg_health(
    rows: Sequence[Mapping[str, Any]],
    *,
    instrument_id: str,
    breach_threshold: float,
    stale_cutoff_sec: float,
    live: bool = False,
    age_sec: float | None = None,
) -> dict[str, Any]:
    """
    Reconstruct Peg Health from SQLite event rows (same window policy as history/trace).

    Uses a fresh BasisObserver + DyopsSentinel so band, Mahalanobis, and criticality
    match the live processing path for the given window. Gemini is never involved.
    """
    if not rows:
        return empty_peg_health(
            instrument_id=instrument_id,
            stale_cutoff_sec=stale_cutoff_sec,
            breach_threshold=breach_threshold,
        )

    import dyops_core
    from sentinel import DyopsSentinel

    observer = dyops_core.BasisObserver(
        name=f"peg-health-replay-{instrument_id}",
        theta=1.0,
        ring_buffer_capacity=1000,
    )
    sentinel = DyopsSentinel(
        observer,
        auditor=None,
        persistence=None,
        instrument_id=instrument_id,
    )

    peg: dict[str, Any] | None = None
    previous_band: str | None = None
    previous_transition: Mapping[str, Any] | None = None

    for row in rows:
        ts = float(row["timestamp"])
        phys = float(row["physical_price"])
        tok = float(row["token_price"])
        result = sentinel.process_event(
            ts,
            phys,
            tok,
            schedule_background_audit=False,
            ingestion_source=str(row.get("ingestion_source") or "live"),
            scenario=(
                str(row["scenario"]) if row.get("scenario") is not None else None
            ),
        )
        peg = build_peg_health(
            instrument_id=instrument_id,
            timestamp=ts,
            physical_price=phys,
            token_price=tok,
            filtered_basis=float(result.health.filtered_basis),
            mahalanobis=float(result.health.mahalanobis_distance),
            criticality=float(result.criticality_recent_pct),
            measurement_valid=bool(result.health.measurement_valid),
            level_name=result.level.name,
            breach_threshold=breach_threshold,
            live=live,
            age_sec=age_sec,
            stale_cutoff_sec=stale_cutoff_sec,
            previous_band=previous_band,
            previous_transition=previous_transition,
        )
        previous_band = str(peg["band"])
        previous_transition = peg.get("last_transition")

    assert peg is not None
    return peg
