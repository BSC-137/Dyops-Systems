export type SentinelLevel = "MONITORING" | "BREACH" | "AUDIT"

export interface TelemetryPayload {
  instrument_id?: string
  level: SentinelLevel
  level_value: number
  timestamp: number
  physical_price: number
  token_price: number
  session_event_index?: number
  health: {
    filtered_basis: number
    innovation: number
    mahalanobis_distance: number
    measurement_valid: boolean
    breach: boolean
  }
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
}

/** GET /api/status */
export interface StatusResponse {
  gemini_configured: boolean
  webhook_configured?: boolean
  binance_feed: string
  audits_dir: string
  db_path: string
  global_events_total_sqlite: number
  mahalanobis_breach_threshold: number
  criticality_window_events: number
  criticality_audit_pct: number
  audit_cooldown_ticks: number
  demo_inject_enabled: boolean
}

/** GET /api/history/trace */
export interface HistoryTraceBundle {
  summary: string
  explainability: string
  points: HistoryTracePoint[]
}

export interface HistoryTracePoint {
  instrument_id?: string
  t: number
  measured_basis: number
  filtered_basis: number
  innovation: number
  mahalanobis: number
  valid: boolean
  reasoning: string
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
  events_session: number
  events_total_sqlite: number
  last_tick_age_sec: number | null
}
