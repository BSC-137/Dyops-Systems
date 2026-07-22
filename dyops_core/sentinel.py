"""
Dyops 24/7 monitoring layer: Basis observer + breach / audit escalation + optional Gemini auditor.

Gemini integration uses the **Google Gen AI unified SDK** (`google-genai`).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

try:
    from google import genai as google_genai
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover
    google_genai = None  # type: ignore[assignment]
    genai_types = None  # type: ignore[assignment]

import dyops_core
from database import PersistenceManager

DEFAULT_GEMINI_MODEL = os.environ.get("DYOPS_GEMINI_MODEL", "gemini-3-flash")
AUDITS_DIR = Path(__file__).resolve().parent / "audits"
MAHALANOBIS_BREACH = float(dyops_core.MAHALANOBIS_BREACH)
CRITICALITY_WINDOW_EVENTS = int(dyops_core.CRITICALITY_WINDOW)
CRITICALITY_WINDOW_TICKS = CRITICALITY_WINDOW_EVENTS  # deprecated alias
CRITICALITY_AUDIT_PCT = float(dyops_core.CRITICALITY_AUDIT_PCT)
AUDIT_COOLDOWN_TICKS = int(dyops_core.AUDIT_COOLDOWN_TICKS)


def _json_safe_float(x: float) -> float | None:
    if x != x or x in (float("inf"), float("-inf")):
        return None
    return x


class SentinelLevel(IntEnum):
    """Escalation ladder for basis monitoring."""

    MONITORING = 1
    BREACH = 2
    AUDIT = 3


@dataclass
class EventResult:
    """Outcome of a single telemetry event (`process_event` / `process_event_async`)."""

    level: SentinelLevel
    health: dyops_core.SystemHealth
    snapshot: Optional[dict[str, Any]]
    criticality_recent_pct: float


# Backward compatibility
TickResult = EventResult


class DyopsSentinel:
    """
    Thin Python integration wrapper around the Rust `DyopsSentinelCore`.

    Rust owns observer updates, breach/audit policy, and snapshot diagnostics.
    Python retains persistence, logging, callbacks, and optional Gemini dispatch.
    """

    def __init__(
        self,
        observer: dyops_core.BasisObserver,
        *,
        auditor: Optional["AgenticAuditor"] = None,
        criticality_window: int = CRITICALITY_WINDOW_EVENTS,
        audit_criticality_pct: float = CRITICALITY_AUDIT_PCT,
        audit_cooldown_ticks: int = AUDIT_COOLDOWN_TICKS,
        audits_path: Path | str = AUDITS_DIR,
        on_audit: Optional[Callable[[dict[str, Any]], None]] = None,
        persistence: Optional[PersistenceManager] = None,
        instrument_id: str = "default",
    ) -> None:
        self._core = dyops_core.DyopsSentinelCore(
            observer,
            criticality_window=criticality_window,
            audit_criticality_pct=audit_criticality_pct,
            audit_cooldown_ticks=audit_cooldown_ticks,
        )
        self.observer = self._core
        self.auditor = auditor
        self.criticality_window = self._core.criticality_window
        self.audit_criticality_pct = self._core.audit_criticality_pct
        self.audit_cooldown_ticks = self._core.audit_cooldown_ticks
        self.audits_path = Path(audits_path)
        self.on_audit = on_audit
        self.persistence = persistence
        self.instrument_id = instrument_id

    def _maybe_schedule_audit(self, snapshot: dict[str, Any]) -> None:
        """Fire-and-forget audit when a running asyncio loop exists (sync `process_event`)."""
        if self.auditor is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "Audit triggered but no running event loop; use `process_event_async` "
                "or run a background asyncio loop."
            )
            return
        loop.create_task(self._audit_task(snapshot))

    async def _audit_task(self, snapshot: dict[str, Any]) -> None:
        try:
            report = await self.auditor.audit_snapshot(snapshot)  # type: ignore[union-attr]
            await self.auditor.save_audit_record(snapshot, report)  # type: ignore[union-attr]
        except Exception as exc:  # noqa: BLE001
            logger.exception("Gemini audit failed: {}", exc)

    async def _await_audit_if_needed(self, result: EventResult) -> None:
        if result.level == SentinelLevel.AUDIT and result.snapshot is not None and self.auditor:
            try:
                report = await self.auditor.audit_snapshot(result.snapshot)
                await self.auditor.save_audit_record(result.snapshot, report)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Gemini audit failed: {}", exc)

    def process_event(
        self,
        timestamp: float,
        physical_price: float,
        token_price: float,
        *,
        schedule_background_audit: bool = True,
        ingestion_source: str = "live",
        scenario: str | None = None,
    ) -> EventResult:
        """
        Delegate one telemetry packet to Rust, then run Python-only integrations.
        """
        core_result = self._core.process_event(
            timestamp,
            physical_price,
            token_price,
        )
        health = core_result["health"]
        crit_recent = float(core_result["criticality_recent_pct"])
        level = SentinelLevel[core_result["level"]]
        snapshot = core_result["snapshot"]

        if core_result["breach"]:
            logger.opt(colors=True).info(
                "<red>🔴 BREACH DETECTED</red> | mahalanobis={:.4f} | innovation={:.6f}",
                health.mahalanobis_distance,
                health.innovation,
            )

        if snapshot is not None:
            snapshot.update(
                {
                    "schema_version": 1,
                    "reason": "criticality_window",
                    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                    "instrument_id": self.instrument_id,
                }
            )
            logger.warning(
                "<yellow>🟠 AUDIT SNAPSHOT</yellow> | last {} telemetry packets "
                "criticality {:.2f}% (> {:.1f}%) | cooldown {} ticks",
                self.criticality_window,
                crit_recent,
                self.audit_criticality_pct,
                self.audit_cooldown_ticks,
            )
            if self.on_audit:
                self.on_audit(snapshot)
            if schedule_background_audit:
                self._maybe_schedule_audit(snapshot)
        elif level == SentinelLevel.BREACH:
            logger.debug(
                "Breach captured | basis={:.6f} | innovation={:.6f}",
                health.filtered_basis,
                health.innovation,
            )

        if self.persistence is not None:
            self.persistence.schedule_event(
                timestamp,
                physical_price,
                token_price,
                instrument_id=self.instrument_id,
                innovation=_json_safe_float(health.innovation),
                mahalanobis_distance=_json_safe_float(health.mahalanobis_distance),
                ingestion_source=ingestion_source,
                scenario=scenario,
            )

        return EventResult(
            level=level,
            health=health,
            snapshot=snapshot,
            criticality_recent_pct=crit_recent,
        )

    def process_tick(
        self,
        timestamp: float,
        physical_price: float,
        token_price: float,
        *,
        schedule_background_audit: bool = True,
    ) -> EventResult:
        """Deprecated alias for :meth:`process_event`."""
        return self.process_event(
            timestamp,
            physical_price,
            token_price,
            schedule_background_audit=schedule_background_audit,
        )

    async def process_event_async(
        self,
        timestamp: float,
        physical_price: float,
        token_price: float,
    ) -> EventResult:
        """
        Async path: same as :meth:`process_event`, but awaits Gemini audit (via thread pool) inline.
        """
        result = self.process_event(
            timestamp,
            physical_price,
            token_price,
            schedule_background_audit=False,
        )
        await self._await_audit_if_needed(result)
        return result

    async def process_tick_async(
        self,
        timestamp: float,
        physical_price: float,
        token_price: float,
    ) -> EventResult:
        """Deprecated alias for :meth:`process_event_async` (same implementation)."""
        result = self.process_event(
            timestamp,
            physical_price,
            token_price,
            schedule_background_audit=False,
        )
        await self._await_audit_if_needed(result)
        return result


AUDITOR_SYSTEM_PROMPT = """You are the Dyops Risk Auditor. You are analyzing a potential de-peg event in a tokenized asset basis.

Input: Analyze the innovation kurtosis and the Mahalanobis distance.
Context: High Kurtosis (>5.0) indicates a "Fat Tail" or black swan event.

Goal: Categorize the event as OPERATIONAL (Oracle lag/Data noise) or FUNDAMENTAL (Liquidity shock/Contract failure).

Return a JSON object ONLY (no markdown) with exactly these keys:
{"cause": "<string>", "risk_score": <integer 0-100>, "mitigation_strategy": "<string>"}
"""


class AgenticAuditor:
    """
    Gemini-backed risk auditor using the unified ``google-genai`` client.
    Blocking ``generate_content`` runs in ``asyncio.to_thread`` so ingestion stays responsive.
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        model_name: str = DEFAULT_GEMINI_MODEL,
        audits_dir: Path | str = AUDITS_DIR,
        persistence: Optional[PersistenceManager] = None,
    ) -> None:
        if google_genai is None or genai_types is None:
            raise ImportError(
                "google-genai is required for AgenticAuditor. "
                "pip install -U google-genai"
            )
        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise ValueError(
                "Set GEMINI_API_KEY or GOOGLE_API_KEY, or pass api_key= to AgenticAuditor."
            )
        self._client = google_genai.Client(api_key=key)
        self.model_name = model_name
        self.audits_dir = Path(audits_dir)
        self.audits_dir.mkdir(parents=True, exist_ok=True)
        self.persistence = persistence

    def _generate_sync(self, snapshot: dict[str, Any]) -> str:
        payload = json.dumps(snapshot, indent=2, allow_nan=False)
        user_block = (
            "Analyze the following snapshot JSON from the Dyops Sentinel.\n\n" + payload
        )
        config = genai_types.GenerateContentConfig(
            system_instruction=AUDITOR_SYSTEM_PROMPT,
            temperature=0.2,
            max_output_tokens=1024,
            response_mime_type="application/json",
        )
        resp = self._client.models.generate_content(
            model=self.model_name,
            contents=user_block,
            config=config,
        )
        text = getattr(resp, "text", None) or ""
        if not text.strip():
            raise RuntimeError("Empty response from Gemini")
        return text.strip()

    async def audit_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        raw = await asyncio.to_thread(self._generate_sync, snapshot)
        return self._parse_auditor_json(raw)

    @staticmethod
    def _parse_auditor_json(text: str) -> dict[str, Any]:
        cleaned = text.strip()
        fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned)
        if fence:
            cleaned = fence.group(1).strip()
        return json.loads(cleaned)

    async def save_audit_record(
        self,
        snapshot: dict[str, Any],
        gemini_report: dict[str, Any],
    ) -> Path:
        record = {
            "snapshot": snapshot,
            "gemini": gemini_report,
            "model": self.model_name,
        }

        if self.persistence is not None:
            self.persistence.schedule_audit(
                record,
                timestamp=datetime.now(timezone.utc).timestamp(),
                instrument_id=str(snapshot.get("instrument_id") or "default"),
            )

        def _write() -> Path:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%Hmmss")
            uid = uuid.uuid4().hex[:8]
            path = self.audits_dir / f"audit_{stamp}_{uid}.json"
            path.write_text(json.dumps(record, indent=2, allow_nan=False), encoding="utf-8")
            logger.info("Audit saved to {}", path)
            return path

        return await asyncio.to_thread(_write)
