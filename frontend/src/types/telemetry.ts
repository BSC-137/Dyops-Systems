export interface TelemetryPayload {
  level: string
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
  live: boolean
  last_tick_age_sec: number | null
  events_session: number
  events_total_sqlite: number
  summary?: string
  explainability?: string
}

/** GET /api/history/trace */
export interface HistoryTraceBundle {
  summary: string
  explainability: string
  points: HistoryTracePoint[]
}

export interface HistoryTracePoint {
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
    snapshot?: unknown
  }
}
