import { incidentSummary } from "@/lib/incidents"
import type {
  AuditRow,
  HistoryTracePoint,
  IncidentWindow,
} from "@/types/telemetry"

export const FORENSIC_NON_CLAIM =
  "Operational forensic export — not a regulatory attestation or signed compliance report."

export function stableStringify(value: unknown): string {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value) ?? "null"
  }
  if (Array.isArray(value)) {
    return `[${value.map(stableStringify).join(",")}]`
  }
  const record = value as Record<string, unknown>
  return `{${Object.keys(record)
    .sort()
    .map(
      (key) =>
        `${JSON.stringify(key)}:${stableStringify(record[key])}`,
    )
    .join(",")}}`
}

async function sha256Hex(value: string): Promise<string | null> {
  if (!globalThis.crypto?.subtle) return null
  const digest = await globalThis.crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  )
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("")
}

type IncidentExportBody = {
  artifact_type: "dyops_incident_export"
  schema_version: "2.0"
  exported_at: string
  instrument_id: string
  software: {
    name: "dyops"
    version: string
  }
  classification: {
    unsigned: true
    non_claim: string
    assembled_by: "dyops_frontend"
    exported_at_source: "client_browser_clock"
  }
  data_sources: HistoryTracePoint["ingestion_source"][]
  scenarios: string[]
  incident: {
    id: string
    kind: IncidentWindow["kind"]
    start_timestamp: number
    end_timestamp: number
    tick_count: number
    peak_mahalanobis: number
    criticality_peak_pct: number
  }
  deterministic_evidence: {
    source: "server_deterministic_replay"
    mahalanobis_breach_threshold: number
    summary: string
    points: HistoryTracePoint[]
  }
  optional_llm_evidence: {
    source: "gemini_when_present"
    present: boolean
    audits: AuditRow[]
  }
  integrity_notice: string
}

export type IncidentExportArtifact = IncidentExportBody & {
  content_sha256: string | null
}

export async function buildIncidentExport(
  instrumentId: string,
  softwareVersion: string,
  incident: IncidentWindow,
  points: HistoryTracePoint[],
  breachThreshold: number,
): Promise<IncidentExportArtifact> {
  const audits = incident.audits
  const body: IncidentExportBody = {
    artifact_type: "dyops_incident_export",
    schema_version: "2.0",
    exported_at: new Date().toISOString(),
    instrument_id: instrumentId,
    software: {
      name: "dyops",
      version: softwareVersion,
    },
    classification: {
      unsigned: true,
      non_claim: FORENSIC_NON_CLAIM,
      assembled_by: "dyops_frontend",
      exported_at_source: "client_browser_clock",
    },
    data_sources: Array.from(
      new Set(points.map((point) => point.ingestion_source)),
    ),
    scenarios: Array.from(
      new Set(
        points
          .map((point) => point.scenario)
          .filter((scenario): scenario is string => Boolean(scenario)),
      ),
    ),
    incident: {
      id: incident.id,
      kind: incident.kind,
      start_timestamp: incident.startT,
      end_timestamp: incident.endT,
      tick_count: incident.tickCount,
      peak_mahalanobis: incident.peakMahalanobis,
      criticality_peak_pct: incident.criticalityPeakPct,
    },
    deterministic_evidence: {
      source: "server_deterministic_replay",
      mahalanobis_breach_threshold: breachThreshold,
      summary: incidentSummary(incident, points, breachThreshold),
      points,
    },
    optional_llm_evidence: {
      source: "gemini_when_present",
      present: audits.some((audit) => Boolean(audit.report?.gemini)),
      audits,
    },
    integrity_notice:
      "SHA-256 covers canonical JSON without content_sha256. It is tamper-evidence for comparison, not a digital signature or legal seal.",
  }
  return {
    ...body,
    content_sha256: await sha256Hex(stableStringify(body)),
  }
}

export function downloadIncidentExport(artifact: IncidentExportArtifact): void {
  const blob = new Blob([JSON.stringify(artifact, null, 2)], {
    type: "application/json",
  })
  const url = URL.createObjectURL(blob)
  const link = document.createElement("a")
  link.href = url
  link.download = `dyops-${artifact.incident.kind.toLowerCase()}-${Math.round(
    artifact.incident.start_timestamp,
  )}.json`
  link.click()
  URL.revokeObjectURL(url)
}
