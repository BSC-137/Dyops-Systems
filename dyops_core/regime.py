"""
Deterministic regime layer for issuer-relevant peg failure modes.

Differentiates:
1. sudden_dislocation — abrupt peg break (Mahalanobis / innovation persistence)
2. slow_peg_erosion — creeping basis drift the Kalman policy alone often absorbs
3. feed_oracle_fault — stale/invalid/oscillating oracle vs a true peg break

Shadow mode (default): emit regime_tag / level / reasoning for forensics without
changing sentinel escalation or Peg Health band.
Active mode (DYOPS_REGIME_ACTIVE=1): regime level may elevate Peg Health band.

No deep learning. No LLM. Gemini never alters regime decisions.
"""

from __future__ import annotations

import math
import os
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Mapping

# --- Tunable defaults (also exposed to historical_eval calibration grids) ---

SLOW_SHORT_WINDOW = 8
SLOW_LONG_WINDOW = 40
SLOW_THRESHOLD_BPS = 8.0
SLOW_PERSIST_TICKS = 4

DISLOCATION_SAME_SIGN_TICKS = 2
MAHALANOBIS_BREACH_DEFAULT = 3.0

ORACLE_SIGN_WINDOW = 12
ORACLE_MIN_SIGN_FLIPS = 5
ORACLE_MAX_ABS_MEAN_BASIS_BPS = 15.0

INVALID_STREAK_FAULT = 2

REGIME_NOMINAL = "nominal"
REGIME_SUDDEN = "sudden_dislocation"
REGIME_SLOW = "slow_peg_erosion"
REGIME_FEED = "feed_oracle_fault"
REGIME_STALE = "stale"
REGIME_WITHHELD = "measurement_withheld"

_REASON_MAX = 280


def regime_active_enabled() -> bool:
    """True when operators explicitly opt into active regime escalation."""
    return os.environ.get("DYOPS_REGIME_ACTIVE") == "1"


def _clip(text: str, max_len: int = _REASON_MAX) -> str:
    t = text.strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1].rstrip() + "…"


def _sign(value: float) -> int:
    if value > 0.0:
        return 1
    if value < 0.0:
        return -1
    return 0


@dataclass(frozen=True)
class RegimeDecision:
    """Per-tick deterministic regime classification."""

    regime_tag: str
    level: str  # MONITORING | BREACH | AUDIT — regime recommendation
    reasoning: str
    mode: str  # shadow | active
    score_bps: float | None = None
    details: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if out.get("details") is None:
            out.pop("details", None)
        return out


class RegimeEngine:
    """
    Causal multi-tick regime classifier.

    Consumes observer outputs already computed by the sentinel path; does not
    mutate the Kalman state. Designed so partners can read reasoning without
    reading filter internals.
    """

    def __init__(
        self,
        *,
        slow_short_window: int = SLOW_SHORT_WINDOW,
        slow_long_window: int = SLOW_LONG_WINDOW,
        slow_threshold_bps: float = SLOW_THRESHOLD_BPS,
        slow_persist_ticks: int = SLOW_PERSIST_TICKS,
        dislocation_same_sign_ticks: int = DISLOCATION_SAME_SIGN_TICKS,
        mahalanobis_breach: float = MAHALANOBIS_BREACH_DEFAULT,
        oracle_sign_window: int = ORACLE_SIGN_WINDOW,
        oracle_min_sign_flips: int = ORACLE_MIN_SIGN_FLIPS,
        oracle_max_abs_mean_basis_bps: float = ORACLE_MAX_ABS_MEAN_BASIS_BPS,
        invalid_streak_fault: int = INVALID_STREAK_FAULT,
        mode: str | None = None,
    ) -> None:
        if slow_short_window < 2 or slow_long_window <= slow_short_window:
            raise ValueError("slow windows must satisfy 2 <= short < long")
        self.slow_short_window = int(slow_short_window)
        self.slow_long_window = int(slow_long_window)
        self.slow_threshold_bps = float(slow_threshold_bps)
        self.slow_persist_ticks = int(slow_persist_ticks)
        self.dislocation_same_sign_ticks = int(dislocation_same_sign_ticks)
        self.mahalanobis_breach = float(mahalanobis_breach)
        self.oracle_sign_window = int(oracle_sign_window)
        self.oracle_min_sign_flips = int(oracle_min_sign_flips)
        self.oracle_max_abs_mean_basis_bps = float(oracle_max_abs_mean_basis_bps)
        self.invalid_streak_fault = int(invalid_streak_fault)
        self._mode = mode  # None → resolve at step from env

        self._basis_hist: deque[float] = deque(maxlen=self.slow_long_window)
        self._innov_signs: deque[int] = deque(maxlen=max(self.oracle_sign_window, 4))
        self._slow_streak = 0
        self._same_sign_streak = 0
        self._last_innov_sign = 0
        self._invalid_streak = 0

    def reset(self) -> None:
        self._basis_hist.clear()
        self._innov_signs.clear()
        self._slow_streak = 0
        self._same_sign_streak = 0
        self._last_innov_sign = 0
        self._invalid_streak = 0

    def parameters(self) -> dict[str, Any]:
        return {
            "slow_short_window": self.slow_short_window,
            "slow_long_window": self.slow_long_window,
            "slow_threshold_bps": self.slow_threshold_bps,
            "slow_persist_ticks": self.slow_persist_ticks,
            "dislocation_same_sign_ticks": self.dislocation_same_sign_ticks,
            "mahalanobis_breach": self.mahalanobis_breach,
            "oracle_sign_window": self.oracle_sign_window,
            "oracle_min_sign_flips": self.oracle_min_sign_flips,
            "oracle_max_abs_mean_basis_bps": self.oracle_max_abs_mean_basis_bps,
            "invalid_streak_fault": self.invalid_streak_fault,
            "mode": self._mode or ("active" if regime_active_enabled() else "shadow"),
        }

    def _resolved_mode(self) -> str:
        if self._mode in {"shadow", "active"}:
            return self._mode
        return "active" if regime_active_enabled() else "shadow"

    def _slow_score_bps(self) -> float | None:
        """
        Contrast recent short-window mean vs the oldest short window inside the
        long buffer. For linear creep this separates endpoints; plain short-vs-long
        means nearly cancel on a gradual ramp.
        """
        if len(self._basis_hist) < self.slow_long_window:
            return None
        values = list(self._basis_hist)
        oldest = values[: self.slow_short_window]
        recent = values[-self.slow_short_window :]
        oldest_mean = sum(oldest) / self.slow_short_window
        recent_mean = sum(recent) / self.slow_short_window
        return abs(recent_mean - oldest_mean) * 10_000.0

    def _sign_flips(self) -> int:
        signs = [s for s in self._innov_signs if s != 0]
        if len(signs) < 2:
            return 0
        return sum(1 for a, b in zip(signs, signs[1:]) if a != b)

    def _abs_mean_basis_bps(self) -> float | None:
        if len(self._basis_hist) < self.slow_short_window:
            return None
        values = list(self._basis_hist)[-self.slow_short_window :]
        return abs(sum(values) / len(values)) * 10_000.0

    def step(
        self,
        *,
        measurement_valid: bool,
        basis: float | None,
        innovation: float | None,
        mahalanobis: float | None,
        sentinel_level: str = "MONITORING",
        live: bool = True,
        age_sec: float | None = None,
        stale_cutoff_sec: float = 12.0,
    ) -> RegimeDecision:
        mode = self._resolved_mode()
        level_sentinel = (sentinel_level or "MONITORING").upper()

        if not live:
            return RegimeDecision(
                regime_tag=REGIME_STALE,
                level="MONITORING",
                reasoning=_clip(
                    "Regime: stale feed — wall-clock freshness outside the live cutoff; "
                    f"age={age_sec if age_sec is not None else 'n/a'}s "
                    f"(cutoff {stale_cutoff_sec:g}s). Peg classification deferred."
                ),
                mode=mode,
                details={"age_sec": age_sec, "stale_cutoff_sec": stale_cutoff_sec},
            )

        if not measurement_valid or basis is None:
            self._invalid_streak += 1
            self._same_sign_streak = 0
            self._last_innov_sign = 0
            if self._invalid_streak >= self.invalid_streak_fault:
                return RegimeDecision(
                    regime_tag=REGIME_FEED,
                    level="BREACH",
                    reasoning=_clip(
                        f"Regime: feed/oracle fault — {self._invalid_streak} consecutive "
                        "invalid or non-positive prices. Treat as operational data quality, "
                        "not a confirmed peg break."
                    ),
                    mode=mode,
                    details={"invalid_streak": self._invalid_streak},
                )
            return RegimeDecision(
                regime_tag=REGIME_WITHHELD,
                level="MONITORING",
                reasoning=_clip(
                    "Regime: measurement withheld — invalid prices this tick; "
                    "observer did not update. Awaiting valid feed."
                ),
                mode=mode,
                details={"invalid_streak": self._invalid_streak},
            )

        self._invalid_streak = 0
        self._basis_hist.append(float(basis))
        innov = float(innovation) if innovation is not None and math.isfinite(innovation) else 0.0
        m = (
            float(mahalanobis)
            if mahalanobis is not None and math.isfinite(mahalanobis)
            else 0.0
        )
        sign = _sign(innov)
        self._innov_signs.append(sign)

        if sign != 0 and sign == self._last_innov_sign and m > self.mahalanobis_breach:
            self._same_sign_streak += 1
        elif sign != 0 and m > self.mahalanobis_breach:
            self._same_sign_streak = 1
        else:
            self._same_sign_streak = 0
        if sign != 0:
            self._last_innov_sign = sign

        slow_bps = self._slow_score_bps()
        if slow_bps is not None and slow_bps > self.slow_threshold_bps:
            self._slow_streak += 1
        else:
            self._slow_streak = 0

        flips = self._sign_flips()
        mean_bps = self._abs_mean_basis_bps()
        mahal_breach = m > self.mahalanobis_breach

        # 1) Feed/oracle fault: oscillating surprise without persistent peg offset.
        oracle_like = (
            mahal_breach
            and flips >= self.oracle_min_sign_flips
            and mean_bps is not None
            and mean_bps <= self.oracle_max_abs_mean_basis_bps
        )
        if oracle_like:
            return RegimeDecision(
                regime_tag=REGIME_FEED,
                level="BREACH",
                reasoning=_clip(
                    f"Regime: feed/oracle fault — Mahalanobis {m:.2f} with "
                    f"{flips} innovation sign flips in {len(self._innov_signs)} ticks "
                    f"while |short-mean basis| {mean_bps:.1f} bps ≤ "
                    f"{self.oracle_max_abs_mean_basis_bps:g} bps. Pattern fits lagging/"
                    "noisy oracle, not a sustained peg break."
                ),
                mode=mode,
                score_bps=mean_bps,
                details={
                    "mahalanobis": m,
                    "sign_flips": flips,
                    "abs_mean_basis_bps": mean_bps,
                },
            )

        # 2) Sudden dislocation: breach + same-sign innovation persistence.
        if (
            mahal_breach
            and self._same_sign_streak >= self.dislocation_same_sign_ticks
        ):
            return RegimeDecision(
                regime_tag=REGIME_SUDDEN,
                level="AUDIT" if level_sentinel == "AUDIT" else "BREACH",
                reasoning=_clip(
                    f"Regime: sudden dislocation — Mahalanobis {m:.2f} above "
                    f"{self.mahalanobis_breach:g} with {self._same_sign_streak} "
                    "same-sign innovation ticks. Persistent directional surprise "
                    "consistent with an abrupt peg break."
                ),
                mode=mode,
                score_bps=(abs(basis) * 10_000.0),
                details={
                    "mahalanobis": m,
                    "same_sign_streak": self._same_sign_streak,
                    "innovation": innov,
                },
            )

        # Also treat single-tick large breach with elevated |basis| as dislocation
        # when oracle pattern does not apply (keeps sudden_depeg strength).
        if mahal_breach and mean_bps is not None and mean_bps > self.oracle_max_abs_mean_basis_bps:
            return RegimeDecision(
                regime_tag=REGIME_SUDDEN,
                level="AUDIT" if level_sentinel == "AUDIT" else "BREACH",
                reasoning=_clip(
                    f"Regime: sudden dislocation — Mahalanobis {m:.2f} with "
                    f"|short-mean basis| {mean_bps:.1f} bps. Directional peg offset "
                    "distinguishes this from oscillating oracle fault."
                ),
                mode=mode,
                score_bps=mean_bps,
                details={"mahalanobis": m, "abs_mean_basis_bps": mean_bps},
            )

        # 3) Slow / creeping peg erosion (Kalman often silent here).
        if self._slow_streak >= self.slow_persist_ticks and slow_bps is not None:
            return RegimeDecision(
                regime_tag=REGIME_SLOW,
                level="BREACH",
                reasoning=_clip(
                    f"Regime: slow peg erosion — recent short({self.slow_short_window}) "
                    f"vs oldest short inside long({self.slow_long_window}) basis means "
                    f"differ by {slow_bps:.1f} bps for {self._slow_streak} ticks "
                    f"(threshold {self.slow_threshold_bps:g} bps, persist "
                    f"{self.slow_persist_ticks}). Creeping drift the static "
                    "Mahalanobis breach gate often absorbs."
                ),
                mode=mode,
                score_bps=slow_bps,
                details={
                    "slow_score_bps": slow_bps,
                    "slow_streak": self._slow_streak,
                },
            )

        # 4) Nominal / pass-through sentinel elevation without a named regime.
        if level_sentinel == "AUDIT":
            tag = "escalated_review"
            level = "AUDIT"
            reason = (
                "Regime: escalated review — rolling criticality policy elevated the "
                "sentinel to AUDIT without a named dislocation/erosion/feed regime."
            )
        elif level_sentinel == "BREACH" or mahal_breach:
            tag = "elevated_surprise"
            level = "BREACH"
            reason = (
                f"Regime: elevated surprise — Mahalanobis {m:.2f} crossed the breach "
                "gate without matching slow-erosion or oracle-fault patterns yet."
            )
        else:
            tag = REGIME_NOMINAL
            level = "MONITORING"
            slow_note = (
                f"slow score {slow_bps:.1f} bps"
                if slow_bps is not None
                else "slow score warming up"
            )
            reason = (
                f"Regime: nominal — Mahalanobis {m:.2f} within band; {slow_note}. "
                "No issuer-relevant dislocation, erosion, or feed-fault pattern."
            )

        return RegimeDecision(
            regime_tag=tag,
            level=level,
            reasoning=_clip(reason),
            mode=mode,
            score_bps=slow_bps,
            details={"mahalanobis": m, "slow_score_bps": slow_bps},
        )


def regime_implies_band(decision: RegimeDecision) -> str | None:
    """
    Map an active regime decision to a Peg Health band elevation.

    Returns None when shadow mode or nominal — caller keeps sentinel band.
    """
    if decision.mode != "active":
        return None
    if decision.regime_tag == REGIME_SUDDEN:
        return "Breach" if decision.level != "AUDIT" else "Audit"
    if decision.regime_tag == REGIME_SLOW:
        return "Watch" if decision.level == "MONITORING" else "Breach"
    if decision.regime_tag == REGIME_FEED:
        return "Watch"
    return None
