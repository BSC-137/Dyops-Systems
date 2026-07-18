import { Download } from "lucide-react"
import { useMemo, useState } from "react"
import { Badge } from "@/components/ui/badge"
import { ScrollArea } from "@/components/ui/scroll-area"
import { deriveIncidentWindows, incidentSummary } from "@/lib/incidents"
import type {
  AuditRow,
  HistoryTraceBundle,
  IncidentWindow,
} from "@/types/telemetry"

type IncidentsTabProps = {
  trace: HistoryTraceBundle | null
  audits: AuditRow[]
  breachThreshold: number
  criticalityWindowEvents: number
  criticalityAuditPct: number
}

function formatTime(timestamp: number, includeDate = false): string {
  const date = new Date(timestamp * 1000)
  return includeDate
    ? date.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : date.toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
}

function exportIncident(
  incident: IncidentWindow,
  points: HistoryTraceBundle["points"],
  breachThreshold: number,
) {
  const payload = {
    exported_at: new Date().toISOString(),
    incident: {
      id: incident.id,
      kind: incident.kind,
      start_timestamp: incident.startT,
      end_timestamp: incident.endT,
      tick_count: incident.tickCount,
      peak_mahalanobis: incident.peakMahalanobis,
      criticality_peak_pct: incident.criticalityPeakPct,
    },
    summary: incidentSummary(incident, points, breachThreshold),
    audits: incident.audits,
    points,
  }
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json",
  })
  const url = URL.createObjectURL(blob)
  const link = document.createElement("a")
  link.href = url
  link.download = `dyops-${incident.kind.toLowerCase()}-${Math.round(incident.startT)}.json`
  link.click()
  URL.revokeObjectURL(url)
}

export function IncidentsTab({
  trace,
  audits,
  breachThreshold,
  criticalityWindowEvents,
  criticalityAuditPct,
}: IncidentsTabProps) {
  const incidents = useMemo(
    () =>
      deriveIncidentWindows(trace?.points ?? [], audits, {
        breachThreshold,
        criticalityWindowEvents,
        criticalityAuditPct,
      }),
    [
      audits,
      breachThreshold,
      criticalityAuditPct,
      criticalityWindowEvents,
      trace?.points,
    ],
  )
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const effectiveSelectedId = incidents.some(
    (incident) => incident.id === selectedId,
  )
    ? selectedId
    : (incidents[0]?.id ?? null)
  const selected =
    incidents.find((incident) => incident.id === effectiveSelectedId) ?? null
  const selectedPoints =
    selected && trace
      ? trace.points.slice(selected.startIndex, selected.endIndex + 1)
      : []

  return (
    <main className="flex min-h-0 flex-1 p-4">
      <section className="flex min-h-[560px] w-full min-w-0 flex-col overflow-hidden rounded-lg border border-[var(--color-border)] bg-transparent lg:flex-row">
        <div className="flex min-h-0 w-full shrink-0 flex-col border-b border-[var(--color-border)] lg:w-[330px] lg:border-b-0 lg:border-r">
          <div className="border-b border-[var(--color-border)] px-4 py-3">
            <h1 className="text-xs font-medium uppercase tracking-widest text-zinc-500">
              Incident windows
            </h1>
            <p className="mt-1 font-mono-nums text-[11px] leading-relaxed text-zinc-600">
              BREACH and AUDIT runs reconstructed from the latest trace and audit tail.
            </p>
          </div>
          <ScrollArea className="min-h-[180px] flex-1">
            <div className="divide-y divide-zinc-800/70">
              {incidents.length === 0 ? (
                <p className="px-4 py-6 font-mono-nums text-xs text-zinc-600">
                  No incidents in recent history.
                </p>
              ) : null}
              {incidents.map((incident) => (
                <button
                  key={incident.id}
                  type="button"
                  onClick={() => setSelectedId(incident.id)}
                  className={`w-full px-4 py-3 text-left transition-colors ${
                    selected?.id === incident.id
                      ? "bg-zinc-900/80"
                      : "hover:bg-zinc-900/40"
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <Badge
                      variant={
                        incident.kind === "AUDIT" ? "destructive" : "warning"
                      }
                      className="text-[10px]"
                    >
                      {incident.kind}
                    </Badge>
                    <span className="font-mono-nums text-[10px] text-zinc-600">
                      {incident.tickCount} ticks
                    </span>
                  </div>
                  <p className="mt-2 font-mono-nums text-[11px] text-zinc-400">
                    {formatTime(incident.startT)} — {formatTime(incident.endT)}
                  </p>
                  <p className="mt-1 font-mono-nums text-[10px] text-zinc-600">
                    peak M {incident.peakMahalanobis.toFixed(3)} · crit{" "}
                    {incident.criticalityPeakPct.toFixed(1)}%
                  </p>
                </button>
              ))}
            </div>
          </ScrollArea>
        </div>

        <div className="flex min-h-0 min-w-0 flex-1 flex-col">
          {selected ? (
            <>
              <div className="flex flex-wrap items-start justify-between gap-3 border-b border-[var(--color-border)] px-5 py-4">
                <div>
                  <div className="flex items-center gap-2">
                    <Badge
                      variant={
                        selected.kind === "AUDIT" ? "destructive" : "warning"
                      }
                    >
                      {selected.kind}
                    </Badge>
                    <span className="font-mono-nums text-xs text-zinc-500">
                      {formatTime(selected.startT, true)} —{" "}
                      {formatTime(selected.endT, true)}
                    </span>
                  </div>
                  <p className="mt-2 max-w-3xl font-mono-nums text-[11px] leading-relaxed text-zinc-500">
                    {incidentSummary(selected, selectedPoints, breachThreshold)}
                  </p>
                </div>
                <button
                  type="button"
                  onClick={() =>
                    exportIncident(selected, selectedPoints, breachThreshold)
                  }
                  className="inline-flex items-center gap-1.5 rounded-md border border-zinc-700 px-2.5 py-1.5 font-mono-nums text-[10px] uppercase tracking-wide text-zinc-400 transition-colors hover:border-zinc-600 hover:text-zinc-200"
                >
                  <Download className="size-3" aria-hidden />
                  Export JSON
                </button>
              </div>

              {selected.audits.length > 0 ? (
                <div className="border-b border-[var(--color-border)] px-5 py-3">
                  <p className="mb-2 text-[10px] font-medium uppercase tracking-widest text-zinc-600">
                    Audit narrative
                  </p>
                  <div className="space-y-2">
                    {selected.audits.map((audit) => {
                      const report = audit.report?.gemini
                      return (
                        <div
                          key={audit.id}
                          className="border-l border-zinc-700 pl-3"
                        >
                          <p className="font-mono-nums text-[11px] leading-relaxed text-zinc-400">
                            {report?.executive_summary ||
                              report?.mitigation_strategy ||
                              report?.cause ||
                              "Audit snapshot recorded without a narrative."}
                          </p>
                          <p className="mt-1 font-mono-nums text-[10px] text-zinc-600">
                            audit #{audit.id} · risk{" "}
                            {String(report?.risk_score ?? "—")} ·{" "}
                            {String(audit.report?.model ?? "—")}
                          </p>
                        </div>
                      )
                    })}
                  </div>
                </div>
              ) : null}

              <ScrollArea className="min-h-[320px] flex-1">
                <div className="min-w-[720px]">
                  <div className="sticky top-0 grid grid-cols-[150px_110px_90px_1fr] gap-3 border-b border-zinc-800 bg-[var(--color-terminal)] px-5 py-2 text-[10px] font-medium uppercase tracking-wide text-zinc-600">
                    <span>Timestamp</span>
                    <span>Mahalanobis</span>
                    <span>Valid</span>
                    <span>Per-tick reasoning</span>
                  </div>
                  {selectedPoints.map((point, index) => (
                    <div
                      key={`${point.t}-${index}`}
                      className="grid grid-cols-[150px_110px_90px_1fr] gap-3 border-b border-zinc-900 px-5 py-2.5 font-mono-nums text-[11px]"
                    >
                      <span className="text-zinc-500">
                        {formatTime(point.t, true)}
                      </span>
                      <span
                        className={
                          point.mahalanobis > breachThreshold
                            ? "text-amber-300/80"
                            : "text-stone-400"
                        }
                      >
                        {point.mahalanobis.toFixed(6)}
                      </span>
                      <span className="text-zinc-500">
                        {point.valid ? "yes" : "no"}
                      </span>
                      <span className="leading-relaxed text-zinc-400">
                        {point.reasoning}
                      </span>
                    </div>
                  ))}
                </div>
              </ScrollArea>
            </>
          ) : (
            <div className="flex flex-1 items-center justify-center p-8 font-mono-nums text-xs text-zinc-600">
              Select an incident window to inspect its trace.
            </div>
          )}
        </div>
      </section>
    </main>
  )
}
