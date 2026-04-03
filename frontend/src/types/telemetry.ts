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
  basis: number
  innovation: number
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
