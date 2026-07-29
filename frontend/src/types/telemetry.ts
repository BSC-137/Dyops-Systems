export type SentinelLevel = "MONITORING" | "BREACH" | "AUDIT"

export type PegHealthBand = "Healthy" | "Watch" | "Breach" | "Audit"

export interface PegHealthTransition {
  from_band: PegHealthBand | string
  to_band: PegHealthBand | string
  at: number
}

/** GET /api/peg_health and additive /ws/telemetry.peg_health */
export interface PegHealth {
  schema_version: string
  instrument_id: string
  timestamp: number
  band: PegHealthBand | string
  basis: number | null
  filtered_basis: number
  mahalanobis: number
  criticality: number
  freshness: {
    live: boolean
    age_sec: number | null
    stale_cutoff_sec: number
  }
  regime_tag: string
  summary: string
  explainability: string
  last_transition: PegHealthTransition | null
  measurement_valid: boolean
  level: SentinelLevel | string
}

export interface TelemetryPayload {
  instrument_id?: string
  level: SentinelLevel
  level_value: number
  timestamp: number
  physical_price: number
  token_price: number
  session_event_index?: number
  ingestion_source: "live" | "offline" | "demo"
  demo_scenario?: string
  health: {
    filtered_basis: number
    innovation: number
    mahalanobis_distance: number
    measurement_valid: boolean
    breach: boolean
  }
  peg_health?: PegHealth
  snapshot: Record<string, unknown> | null
  criticality_recent_pct: number
}

export interface ChartPoint {
  t: number
  measured_basis: number
  filtered_basis: number
  innovation: number
  mahalanobis: number
}

/** GET /api/pulse */
export interface PulseResponse {
  instrument_id?: string
  live: boolean
  last_tick_age_sec: number | null
  events_session: number
  events_total_sqlite: number
  summary?: string
  explainability?: string
  ingestion_source: "live" | "offline" | "demo" | "none"
}

/** GET /api/status */
export interface StatusResponse {
  gemini_configured: boolean
  gemini_ready: boolean
  gemini_last_error: string | null
  webhook_configured?: boolean
  binance_feed: string
  audits_dir: string
  db_path: string
  global_events_total_sqlite: number
  mahalanobis_breach_threshold: number
  peg_health_watch_threshold?: number
  peg_health_schema_version?: string
  regime_mode?: "shadow" | "active"
  regime_active_enabled?: boolean
  criticality_window_events: number
  criticality_audit_pct: number
  audit_cooldown_ticks: number
  demo_inject_enabled: boolean
  demo_injection_active: boolean
  telemetry_queue_depth: number
  demo_queue_depth: number
  persistence_queue_depth: number
  persistence_healthy: boolean
  persistence_last_error: string | null
  dropped_tick_count: number
  processing_error_count: number
  stale_cutoff_sec: number
  replay_window_events: number
  offline_mode: boolean
  feed_source: "binance_market" | "offline_deterministic" | "feed_disabled"
  demo_webhooks_enabled: boolean
  feed_disabled: boolean
  software_version: string
}

/** GET /api/history/trace */
export interface HistoryTraceBundle {
  summary: string
  explainability: string
  points: HistoryTracePoint[]
}

export interface HistoryTracePoint {
  instrument_id?: string
  ingestion_source: "live" | "offline" | "demo"
  scenario?: string | null
  t: number
  measured_basis: number
  filtered_basis: number
  innovation: number
  mahalanobis: number
  valid: boolean
  reasoning: string
  regime_tag?: string | null
  regime_level?: string | null
  regime_reasoning?: string | null
}

export interface AuditRow {
  id: number
  instrument_id?: string
  timestamp: number
  event_id: number | null
  report: {
    gemini?: {
      risk_score?: number | string
      cause?: string
      mitigation_strategy?: string
      executive_summary?: string
    }
    model?: string
    snapshot?: {
      generated_at_utc?: string
      [key: string]: unknown
    }
  }
}

export type IncidentKind = "BREACH" | "AUDIT"

export interface IncidentWindow {
  id: string
  kind: IncidentKind
  startIndex: number
  endIndex: number
  startT: number
  endT: number
  tickCount: number
  peakMahalanobis: number
  criticalityPeakPct: number
  audits: AuditRow[]
}

export interface InstrumentInfo {
  id: string
  label: string
  feed_mode: string
  physical_symbol: string
  token_symbol: string
  synthetic: boolean
  live: boolean
  level: SentinelLevel
  last_mahalanobis: number | null
  criticality_recent_pct: number
  events_session: number
  events_total_sqlite: number
  last_tick_age_sec: number | null
  ingestion_source: "live" | "offline" | "demo" | "none"
}
