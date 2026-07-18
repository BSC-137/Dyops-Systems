import type {
  AuditRow,
  HistoryTracePoint,
  IncidentKind,
  IncidentWindow,
  SentinelLevel,
} from "@/types/telemetry"

type IncidentPolicy = {
  breachThreshold: number
  criticalityWindowEvents: number
  criticalityAuditPct: number
}

function auditTimestamp(audit: AuditRow): number {
  const generated = audit.report?.snapshot?.generated_at_utc
  const parsed = generated ? Date.parse(generated) / 1000 : NaN
  return Number.isFinite(parsed) ? parsed : audit.timestamp
}

function distanceToWindow(timestamp: number, incident: IncidentWindow): number {
  if (timestamp < incident.startT) return incident.startT - timestamp
  if (timestamp > incident.endT) return timestamp - incident.endT
  return 0
}

function peakMahalanobis(points: HistoryTracePoint[]): number {
  const finite = points
    .map((point) => point.mahalanobis)
    .filter(Number.isFinite)
  return finite.length > 0 ? Math.max(...finite) : 0
}

export function deriveIncidentWindows(
  points: HistoryTracePoint[],
  audits: AuditRow[],
  policy: IncidentPolicy,
): IncidentWindow[] {
  if (points.length === 0) return []

  const windowSize = Math.max(1, policy.criticalityWindowEvents)
  const levels: SentinelLevel[] = []
  const criticality: number[] = []
  let breachCount = 0

  for (let index = 0; index < points.length; index++) {
    const point = points[index]!
    if (Number.isFinite(point.mahalanobis) && point.mahalanobis > policy.breachThreshold) {
      breachCount++
    }
    const expired = points[index - windowSize]
    if (
      expired &&
      Number.isFinite(expired.mahalanobis) &&
      expired.mahalanobis > policy.breachThreshold
    ) {
      breachCount--
    }
    const sampleCount = Math.min(index + 1, windowSize)
    const recentPct = (100 * breachCount) / sampleCount
    criticality.push(recentPct)
    levels.push(
      recentPct > policy.criticalityAuditPct
        ? "AUDIT"
        : point.valid && point.mahalanobis > policy.breachThreshold
          ? "BREACH"
          : "MONITORING",
    )
  }

  const incidents: IncidentWindow[] = []
  let startIndex = 0
  while (startIndex < points.length) {
    const kind = levels[startIndex]!
    if (kind === "MONITORING") {
      startIndex++
      continue
    }
    let endIndex = startIndex
    while (endIndex + 1 < points.length && levels[endIndex + 1] === kind) {
      endIndex++
    }
    const slice = points.slice(startIndex, endIndex + 1)
    incidents.push({
      id: `${kind.toLowerCase()}-${points[startIndex]!.t}-${points[endIndex]!.t}`,
      kind: kind as IncidentKind,
      startIndex,
      endIndex,
      startT: points[startIndex]!.t,
      endT: points[endIndex]!.t,
      tickCount: slice.length,
      peakMahalanobis: peakMahalanobis(slice),
      criticalityPeakPct: Math.max(...criticality.slice(startIndex, endIndex + 1)),
      audits: [],
    })
    startIndex = endIndex + 1
  }

  for (const audit of audits) {
    const timestamp = auditTimestamp(audit)
    if (
      timestamp < points[0]!.t - 30 ||
      timestamp > points[points.length - 1]!.t + 30
    ) {
      continue
    }
    const auditIncidents = incidents.filter((incident) => incident.kind === "AUDIT")
    const nearest = auditIncidents.reduce<IncidentWindow | null>((best, incident) => {
      if (!best) return incident
      return distanceToWindow(timestamp, incident) < distanceToWindow(timestamp, best)
        ? incident
        : best
    }, null)
    if (nearest) {
      nearest.audits.push(audit)
      continue
    }

    let anchorIndex = 0
    let bestDistance = Number.POSITIVE_INFINITY
    points.forEach((point, index) => {
      const distance = Math.abs(point.t - timestamp)
      if (distance < bestDistance) {
        bestDistance = distance
        anchorIndex = index
      }
    })
    const auditStart = Math.max(0, anchorIndex - windowSize + 1)
    const slice = points.slice(auditStart, anchorIndex + 1)
    incidents.push({
      id: `audit-record-${audit.id}`,
      kind: "AUDIT",
      startIndex: auditStart,
      endIndex: anchorIndex,
      startT: points[auditStart]!.t,
      endT: points[anchorIndex]!.t,
      tickCount: slice.length,
      peakMahalanobis: peakMahalanobis(slice),
      criticalityPeakPct: Math.max(...criticality.slice(auditStart, anchorIndex + 1)),
      audits: [audit],
    })
  }

  return incidents.sort((a, b) => b.endT - a.endT)
}

export function incidentSummary(
  incident: IncidentWindow,
  points: HistoryTracePoint[],
  breachThreshold: number,
): string {
  const breachMoments = points.filter(
    (point) => point.valid && point.mahalanobis > breachThreshold,
  ).length
  return `${incident.kind} window · ${points.length} ticks · ${breachMoments} breach moments · peak Mahalanobis ${incident.peakMahalanobis.toFixed(3)}`
}
