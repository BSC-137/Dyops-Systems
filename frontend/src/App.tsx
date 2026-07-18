import { Activity, BrainCircuit, FlaskConical, Radio } from "lucide-react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import type {
  AuditRow,
  ChartPoint,
  HistoryTraceBundle,
  PulseResponse,
  SentinelLevel,
  StatusResponse,
  TelemetryPayload,
} from "@/types/telemetry"

const MAX_POINTS = 500

/**
 * Tail slice passed to LineChart only. Older points stay in chartDataRef for buffering
 * but would dominate ymin/ymax and wash out intra-minute stable-feed motion.
 */
const CHART_VISIBLE_POINTS = 120

const BASIS_DOMAIN_PADDING_RATIO = 0.08
/** Robust range below this ⇒ expand centered span for perceptible drift on quiet feeds */
const BASIS_TINY_SPAN = 1e-8
/** Minimum vertical extent (log-ratio units) after padding when span is negligible */
const BASIS_MIN_DISPLAY_SPAN = 1e-6

function quantileSorted(sorted: readonly number[], q: number): number {
  const n = sorted.length
  if (n === 0) return NaN
  if (n === 1) return sorted[0]!
  const pos = q * (n - 1)
  const lo = Math.floor(pos)
  const hi = Math.ceil(pos)
  if (lo === hi) return sorted[lo]!
  return sorted[lo]! + (sorted[hi]! - sorted[lo]!) * (pos - lo)
}

function collectBasisSampleValues(points: ChartPoint[]): number[] {
  const out: number[] = []
  for (const p of points) {
    for (const v of [p.measured_basis, p.filtered_basis, p.innovation]) {
      if (Number.isFinite(v)) out.push(v)
    }
  }
  return out
}

function basisDomainFromValues(values: number[]): [number, number] {
  if (values.length === 0) return [-BASIS_MIN_DISPLAY_SPAN, BASIS_MIN_DISPLAY_SPAN]
  const sorted = [...values].sort((a, b) => a - b)
  let low: number
  let high: number
  if (sorted.length >= 30) {
    low = quantileSorted(sorted, 0.01)
    high = quantileSorted(sorted, 0.99)
    low = Math.min(low, sorted[0]!)
    high = Math.max(high, sorted[sorted.length - 1]!)
  } else {
    low = sorted[0]!
    high = sorted[sorted.length - 1]!
  }
  let span = high - low
  let mid = (low + high) / 2
  if (!(span >= 0) || !Number.isFinite(span)) {
    span = BASIS_MIN_DISPLAY_SPAN
    mid = Number.isFinite(low) ? low : 0
  }
  const pad =
    span < BASIS_TINY_SPAN ? BASIS_MIN_DISPLAY_SPAN / 2 : span * BASIS_DOMAIN_PADDING_RATIO
  let ymin = low - pad
  let ymax = high + pad
  if (ymax - ymin < BASIS_MIN_DISPLAY_SPAN) {
    ymin = mid - BASIS_MIN_DISPLAY_SPAN / 2
    ymax = mid + BASIS_MIN_DISPLAY_SPAN / 2
  }
  return [ymin, ymax]
}

/** Mirrors `observer.update`: measurement z = ln(physical / token). */
function measuredBasisFromPrices(physical: number, token: number): number {
  if (
    physical > 0 &&
    token > 0 &&
    Number.isFinite(physical) &&
    Number.isFinite(token)
  )
    return Math.log(physical / token)
  return NaN
}

function wsUrl(path: string) {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:"
  return `${proto}//${window.location.host}${path}`
}

function levelBadgeVariant(
  level: SentinelLevel,
): "success" | "warning" | "destructive" {
  if (level === "AUDIT") return "destructive"
  if (level === "BREACH") return "warning"
  return "success"
}

type HistoryApiRow = {
  t: number
  measured_basis: number
  filtered_basis: number
  innovation: number
  mahalanobis: number
  valid?: boolean
}

export default function App() {
  const [chartData, setChartData] = useState<ChartPoint[]>([])
  const [audits, setAudits] = useState<AuditRow[]>([])
  const [pulseLive, setPulseLive] = useState(false)
  const [eventsTotal, setEventsTotal] = useState<number>(0)
  const [geminiOk, setGeminiOk] = useState(false)
  const [feedMode, setFeedMode] = useState<string>("—")
  const [mahalanobisBreachThreshold, setMahalanobisBreachThreshold] =
    useState<number>(3.0)
  const [sentinelLevel, setSentinelLevel] =
    useState<SentinelLevel>("MONITORING")
  const [criticalityRecentPct, setCriticalityRecentPct] = useState(0)
  const [criticalityWindowEvents, setCriticalityWindowEvents] = useState(0)
  const [criticalityAuditPct, setCriticalityAuditPct] = useState(0)
  const [auditCooldownTicks, setAuditCooldownTicks] = useState(0)
  const [demoInjectEnabled, setDemoInjectEnabled] = useState(false)
  const [demoInjectRunning, setDemoInjectRunning] = useState(false)
  const [snapshotHighlighted, setSnapshotHighlighted] = useState(false)
  const [traceMeta, setTraceMeta] = useState<Pick<
    HistoryTraceBundle,
    "summary" | "explainability"
  > | null>(null)
  const [pulseSummaryLine, setPulseSummaryLine] = useState("")
  const [telemetryStreamPaused, setTelemetryStreamPaused] = useState(false)
  const chartDataRef = useRef<ChartPoint[]>([])
  const auditsRef = useRef<Map<number, AuditRow>>(new Map())
  const snapshotHighlightTimerRef =
    useRef<ReturnType<typeof setTimeout> | null>(null)
  const demoResetTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const mergeChart = useCallback((point: ChartPoint) => {
    chartDataRef.current = [...chartDataRef.current, point].slice(-MAX_POINTS)
    setChartData(chartDataRef.current)
  }, [])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const r = await fetch("/api/history?limit=500")
        if (!r.ok) throw new Error(String(r.status))
        const rows: HistoryApiRow[] = await r.json()
        if (cancelled) return
        const initial: ChartPoint[] = rows.map((x) => ({
          t: x.t,
          measured_basis: x.measured_basis,
          filtered_basis: x.filtered_basis,
          innovation: x.innovation,
          mahalanobis: x.mahalanobis,
        }))
        chartDataRef.current = initial.slice(-MAX_POINTS)
        setChartData(chartDataRef.current)
      } catch {
        if (!cancelled) chartDataRef.current = []
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const r = await fetch("/api/history/trace?limit=500")
        if (!r.ok) throw new Error(String(r.status))
        const bundle: HistoryTraceBundle = await r.json()
        if (cancelled) return
        setTraceMeta({
          summary: bundle.summary,
          explainability: bundle.explainability,
        })
      } catch {
        if (!cancelled) setTraceMeta(null)
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    let ws: WebSocket | null = null
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined
    let attempt = 0

    const clearReconnect = () => {
      if (reconnectTimer !== undefined) {
        clearTimeout(reconnectTimer)
        reconnectTimer = undefined
      }
    }

    const connect = () => {
      if (cancelled) return
      clearReconnect()
      ws = new WebSocket(wsUrl("/ws/telemetry"))
      ws.onopen = () => {
        attempt = 0
        setTelemetryStreamPaused(false)
      }
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data as string) as {
            type: string
            payload: TelemetryPayload
          }
          if (msg.type !== "telemetry") return
          const p = msg.payload
          mergeChart({
            t: p.timestamp,
            measured_basis: measuredBasisFromPrices(
              p.physical_price,
              p.token_price,
            ),
            filtered_basis: p.health.filtered_basis,
            innovation: p.health.innovation,
            mahalanobis: p.health.mahalanobis_distance,
          })
          setSentinelLevel(p.level)
          setCriticalityRecentPct(p.criticality_recent_pct)
          if (p.snapshot !== null) {
            setSnapshotHighlighted(true)
            if (snapshotHighlightTimerRef.current !== null) {
              clearTimeout(snapshotHighlightTimerRef.current)
            }
            snapshotHighlightTimerRef.current = setTimeout(() => {
              setSnapshotHighlighted(false)
              snapshotHighlightTimerRef.current = null
            }, 2000)
          }
        } catch {
          /* ignore */
        }
      }
      ws.onerror = () => {}
      ws.onclose = () => {
        if (cancelled) return
        setTelemetryStreamPaused(true)
        const delay = Math.min(30_000, 1000 * 2 ** Math.min(attempt, 5))
        attempt++
        reconnectTimer = setTimeout(connect, delay)
      }
    }

    connect()

    return () => {
      cancelled = true
      clearReconnect()
      ws?.close()
      if (snapshotHighlightTimerRef.current !== null) {
        clearTimeout(snapshotHighlightTimerRef.current)
        snapshotHighlightTimerRef.current = null
      }
    }
  }, [mergeChart])

  const upsertAudit = useCallback((row: AuditRow) => {
    const m = auditsRef.current
    if (m.has(row.id)) return
    m.set(row.id, row)
    setAudits(
      Array.from(m.values())
        .sort((a, b) => a.id - b.id)
        .slice(-80),
    )
  }, [])

  useEffect(() => {
    const ws = new WebSocket(wsUrl("/ws/audits"))
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data as string) as { type: string; payload: AuditRow }
        if (msg.type !== "audit") return
        upsertAudit(msg.payload)
      } catch {
        /* ignore */
      }
    }
    return () => ws.close()
  }, [upsertAudit])

  useEffect(() => {
    const tick = async () => {
      try {
        const [pulseR, statusR] = await Promise.all([
          fetch("/api/pulse"),
          fetch("/api/status"),
        ])
        if (pulseR.ok) {
          const p = (await pulseR.json()) as PulseResponse
          setPulseLive(!!p.live)
          setEventsTotal(Number(p.events_total_sqlite ?? 0))
          const summary = (p.summary ?? "").trim()
          const explain = (p.explainability ?? "").trim()
          setPulseSummaryLine(
            summary && explain
              ? `${summary} · ${explain}`
              : summary || explain || "",
          )
        }
        if (statusR.ok) {
          const s = (await statusR.json()) as StatusResponse
          setGeminiOk(s.gemini_configured)
          setFeedMode(s.binance_feed)
          setMahalanobisBreachThreshold(s.mahalanobis_breach_threshold)
          setCriticalityWindowEvents(s.criticality_window_events)
          setCriticalityAuditPct(s.criticality_audit_pct)
          setAuditCooldownTicks(s.audit_cooldown_ticks)
          setDemoInjectEnabled(s.demo_inject_enabled)
        }
      } catch {
        /* ignore */
      }
    }
    tick()
    const t = setInterval(tick, 2000)
    return () => clearInterval(t)
  }, [])

  const injectSuddenDepeg = useCallback(async () => {
    setDemoInjectRunning(true)
    try {
      const response = await fetch(
        "/api/demo/inject_scenario?name=sudden_depeg",
        { method: "POST" },
      )
      if (!response.ok) throw new Error(String(response.status))
      demoResetTimerRef.current = setTimeout(() => {
        setDemoInjectRunning(false)
        demoResetTimerRef.current = null
      }, 7000)
    } catch {
      setDemoInjectRunning(false)
    }
  }, [])

  useEffect(
    () => () => {
      if (demoResetTimerRef.current !== null) {
        clearTimeout(demoResetTimerRef.current)
      }
    },
    [],
  )

  const chartScaled = useMemo(() => {
    const visible =
      chartData.length <= CHART_VISIBLE_POINTS
        ? chartData
        : chartData.slice(-CHART_VISIBLE_POINTS)

    const basisDomain = basisDomainFromValues(collectBasisSampleValues(visible))

    const mahalVals = visible
      .map((p) => p.mahalanobis)
      .filter((v) => Number.isFinite(v) && v >= 0)
    const maxMahal = mahalVals.length > 0 ? Math.max(...mahalVals) : 0
    const mahalUpper = Math.max(
      mahalanobisBreachThreshold,
      maxMahal * 1.15,
    )

    const times = visible.map((p) => p.t).filter(Number.isFinite)
    let crossesMidnightBoundary = false
    if (times.length >= 2) {
      const day0 = new Date(times[0]! * 1000).toDateString()
      for (let i = 1; i < times.length; i++) {
        if (new Date(times[i]! * 1000).toDateString() !== day0) {
          crossesMidnightBoundary = true
          break
        }
      }
    }

    const formatX = (v: number) => {
      const d = new Date(v * 1000)
      const clock = d.toLocaleTimeString(undefined, {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
      if (!crossesMidnightBoundary) return clock
      return `${d.toLocaleDateString(undefined, { month: "short", day: "numeric" })} · ${clock}`
    }

    return {
      chartVisibleData: visible,
      basisDomain,
      mahalUpper,
      formatX,
    }
  }, [chartData, mahalanobisBreachThreshold])

  const chartTooltipStyle = useMemo(
    () => ({
      background: "#18181b",
      border: "1px solid #27272a",
      borderRadius: 8,
      fontSize: 12,
      fontFamily: "JetBrains Mono, monospace",
      color: "#e4e4e7",
    }),
    [],
  )

  const methodologyHover =
    "Kalman-Filtered State Tracking vs. Static Thresholds"

  return (
    <div className="flex min-h-full flex-col bg-[var(--color-terminal)] text-zinc-100">
      <header className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-[var(--color-border)] px-5 py-3">
        <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <span className="shrink-0 text-sm font-semibold tracking-tight text-stone-200">
              DYOPS
            </span>
            <span className="text-zinc-600">|</span>
            <span className="min-w-0 text-xs font-normal leading-snug text-zinc-500 sm:text-sm">
              Dyops: State-Space Intelligence Layer.
            </span>
          </div>
          <Badge
            variant="outline"
            title={methodologyHover}
            className="w-fit cursor-help border-stone-600/70 text-[10px] font-normal uppercase tracking-wide text-stone-500"
          >
            <FlaskConical className="mr-1 size-3 opacity-70" aria-hidden />
            Methodology
          </Badge>
          {demoInjectEnabled ? (
            <button
              type="button"
              disabled={demoInjectRunning}
              onClick={injectSuddenDepeg}
              className="w-fit rounded-md border border-amber-900/70 bg-amber-950/20 px-2 py-1 font-mono-nums text-[10px] text-amber-300/80 transition-colors hover:border-amber-800 hover:text-amber-200 disabled:cursor-wait disabled:border-zinc-800 disabled:text-zinc-600"
            >
              {demoInjectRunning
                ? "Demo: sudden depeg running…"
                : "Demo: inject sudden depeg"}
            </button>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center justify-end gap-4">
          <div className="flex items-center gap-2 text-xs text-zinc-500">
            <Radio className="size-3.5 text-zinc-500" aria-hidden />
            <span className="uppercase tracking-wide">System pulse</span>
            <span
              className={`font-mono-nums text-xs font-medium ${pulseLive ? "text-emerald-600" : "text-zinc-500"}`}
            >
              {pulseLive ? "LIVE" : "STALE"}
            </span>
            <span
              className={`size-2 rounded-full ${pulseLive ? "bg-emerald-600" : "bg-zinc-600"}`}
            />
            <Badge
              variant={levelBadgeVariant(sentinelLevel)}
              className="text-[10px]"
            >
              {sentinelLevel}
            </Badge>
            <Badge
              variant="outline"
              className="border-zinc-700 text-[10px] text-stone-400"
              title={
                criticalityWindowEvents > 0
                  ? `Recent criticality over ${criticalityWindowEvents} ticks; audit at ${criticalityAuditPct.toFixed(1)}%`
                  : undefined
              }
            >
              crit {criticalityRecentPct.toFixed(1)}%
            </Badge>
          </div>

          <div className="flex items-center gap-2 text-xs text-zinc-500">
            <BrainCircuit className="size-3.5" aria-hidden />
            <span className="uppercase tracking-wide">Gemini</span>
            <Badge variant={geminiOk ? "success" : "outline"}>
              {geminiOk ? "CONFIGURED" : "OFFLINE"}
            </Badge>
          </div>

          <div className="flex items-center gap-2 text-xs text-zinc-500">
            <Activity className="size-3.5" aria-hidden />
            <span className="uppercase tracking-wide">Global events</span>
            <span className="font-mono-nums font-semibold text-stone-400 tabular-nums">
              {eventsTotal.toLocaleString()}
            </span>
          </div>

          <Badge variant="outline" className="font-mono-nums text-[10px]">
            {feedMode}
          </Badge>
          {auditCooldownTicks > 0 ? (
            <Badge
              variant="outline"
              className="border-zinc-700 font-mono-nums text-[10px] font-normal text-zinc-500"
            >
              Audit cooldown {auditCooldownTicks} ticks
            </Badge>
          ) : null}
        </div>
      </header>

      {telemetryStreamPaused ? (
        <div
          className="border-b border-zinc-700/70 bg-zinc-900/40 px-5 py-2 text-center font-mono-nums text-[11px] leading-snug tracking-wide text-zinc-500"
          role="status"
        >
          Live Stream Paused — Reconnecting to State-Space Engine…
        </div>
      ) : null}

      <main className="flex min-h-0 flex-1 gap-4 p-4">
        <section className="flex min-h-0 min-w-0 w-[70%] flex-col">
          <Card className="flex min-h-0 flex-1 flex-col border-[var(--color-border)] bg-transparent shadow-none">
            <CardHeader className="pb-2">
              <CardTitle className="text-xs font-medium uppercase tracking-widest text-zinc-500">
                Real-Time Telemetry
              </CardTitle>
              {pulseSummaryLine ? (
                <p
                  className="mt-1 line-clamp-2 font-mono-nums text-[11px] leading-relaxed text-zinc-600"
                  title={pulseSummaryLine}
                >
                  {pulseSummaryLine}
                </p>
              ) : null}
            </CardHeader>
            <CardContent className="min-h-0 flex-1 px-2 pb-2 pt-0">
              <div className="h-full min-h-[420px] w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart
                    data={chartScaled.chartVisibleData}
                    margin={{ top: 8, right: 12, left: 0, bottom: 8 }}
                  >
                    <CartesianGrid
                      strokeDasharray="3 3"
                      stroke="#27272a"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="t"
                      type="number"
                      domain={["dataMin", "dataMax"]}
                      tick={{ fill: "#71717a", fontSize: 10 }}
                      tickFormatter={chartScaled.formatX}
                      stroke="#3f3f46"
                    />
                    <YAxis
                      yAxisId="basis"
                      domain={chartScaled.basisDomain}
                      allowDataOverflow={false}
                      tick={{ fill: "#a1a1aa", fontSize: 10 }}
                      stroke="#3f3f46"
                      width={52}
                    />
                    <YAxis
                      yAxisId="mahal"
                      orientation="right"
                      domain={[0, chartScaled.mahalUpper]}
                      allowDataOverflow={false}
                      tick={{ fill: "#78716c", fontSize: 10 }}
                      stroke="#3f3f46"
                      width={44}
                    />
                    <Tooltip
                      contentStyle={chartTooltipStyle}
                      labelFormatter={(v) =>
                        typeof v === "number"
                          ? chartScaled.formatX(v)
                          : String(v)
                      }
                      formatter={(value, name) => {
                        if (value === undefined || value === null)
                          return ["—", String(name)]
                        const n =
                          typeof value === "number" ? value : Number(value)
                        const s = Number.isFinite(n) ? n.toFixed(6) : "—"
                        return [s, String(name)]
                      }}
                    />
                    <Legend
                      wrapperStyle={{
                        fontSize: 11,
                        fontFamily: "JetBrains Mono, monospace",
                        color: "#a1a1aa",
                      }}
                    />
                    <Line
                      yAxisId="basis"
                      type="monotone"
                      dataKey="measured_basis"
                      name="Measured basis"
                      stroke="var(--color-chart-slate-measured)"
                      strokeWidth={1.25}
                      dot={false}
                      isAnimationActive={false}
                      connectNulls
                    />
                    <Line
                      yAxisId="basis"
                      type="monotone"
                      dataKey="filtered_basis"
                      name="Filtered state"
                      stroke="var(--color-signal-emerald)"
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                      connectNulls
                    />
                    <Line
                      yAxisId="basis"
                      type="monotone"
                      dataKey="innovation"
                      name="Innovation (residual)"
                      stroke="var(--color-stone-soft)"
                      strokeWidth={1.5}
                      dot={false}
                      isAnimationActive={false}
                      connectNulls
                    />
                    <Line
                      yAxisId="mahal"
                      type="monotone"
                      dataKey="mahalanobis"
                      name="Mahalanobis distance"
                      stroke="var(--color-chart-mahalanobis)"
                      strokeWidth={1.7}
                      dot={false}
                      isAnimationActive={false}
                      connectNulls
                    />
                    <ReferenceLine
                      yAxisId="mahal"
                      y={mahalanobisBreachThreshold}
                      stroke="var(--color-chart-threshold)"
                      strokeDasharray="5 5"
                      strokeWidth={1}
                      ifOverflow="extendDomain"
                      label={{
                        value: `Mahalanobis breach threshold (${mahalanobisBreachThreshold})`,
                        position: "insideTopRight",
                        fill: "#a8a29e",
                        fontSize: 10,
                        fontFamily: "JetBrains Mono, monospace",
                      }}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </CardContent>
          </Card>
        </section>

        <section className="flex w-[30%] min-w-[280px] flex-col border-l border-[var(--color-border)] pl-4">
          <Card className="flex min-h-0 flex-1 flex-col border-[var(--color-border)] bg-[var(--color-panel)] shadow-none">
            <CardHeader
              className={`border-b pb-2 transition-colors duration-300 ${
                snapshotHighlighted
                  ? "border-amber-800/60 bg-amber-950/10"
                  : "border-transparent"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <CardTitle className="text-xs font-medium uppercase tracking-widest text-zinc-500">
                  Structural Drift Audit
                </CardTitle>
                <span className="font-mono-nums text-[10px] text-zinc-600">
                  crit {criticalityRecentPct.toFixed(1)}%
                </span>
              </div>
              {traceMeta ? (
                <div className="mt-1 space-y-1 border-l border-zinc-700 pl-2">
                  <p
                    className="font-mono-nums text-[11px] leading-relaxed text-zinc-600"
                    title={traceMeta.summary}
                  >
                    {traceMeta.summary}
                  </p>
                  <p
                    className="line-clamp-3 font-mono-nums text-[11px] leading-relaxed text-zinc-600/90"
                    title={traceMeta.explainability}
                  >
                    {traceMeta.explainability}
                  </p>
                </div>
              ) : null}
            </CardHeader>
            <CardContent className="flex min-h-0 flex-1 flex-col p-0">
              <ScrollArea className="min-h-0 flex-1 px-4 pb-4">
                <div className="space-y-3 pr-2">
                  {audits.length === 0 && (
                    <p className="font-mono-nums text-xs text-zinc-600">No Gemini audits yet.</p>
                  )}
                  {audits.map((a) => {
                    const g = a.report?.gemini
                    const risk = g?.risk_score ?? "—"
                    return (
                      <div
                        key={a.id}
                        className="rounded-md border border-zinc-800 bg-zinc-950/40 p-3"
                      >
                        <div className="mb-2 flex items-center justify-between gap-2">
                          <Badge
                            variant={
                              Number(risk) >= 60
                                ? "destructive"
                                : "default"
                            }
                            className="text-[10px]"
                          >
                            RISK {String(risk)}
                          </Badge>
                          <span className="font-mono-nums text-[10px] text-zinc-500">
                            #{a.id}
                          </span>
                        </div>
                        <p className="font-mono-nums text-xs leading-relaxed text-zinc-400">
                          {g?.executive_summary ||
                            g?.mitigation_strategy ||
                            g?.cause ||
                            "—"}
                        </p>
                      </div>
                    )
                  })}
                </div>
              </ScrollArea>
              <div className="border-t border-[var(--color-border)] px-4 py-3">
                <p className="mb-2 font-mono-nums text-[10px] uppercase tracking-wide text-zinc-600">
                  Recent audit index
                </p>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>ID</TableHead>
                      <TableHead>Risk</TableHead>
                      <TableHead>Model</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {audits
                      .slice(-8)
                      .reverse()
                      .map((a) => (
                        <TableRow key={`t-${a.id}`}>
                          <TableCell className="font-mono-nums text-xs">
                            {a.id}
                          </TableCell>
                          <TableCell className="font-mono-nums text-xs">
                            {String(a.report?.gemini?.risk_score ?? "—")}
                          </TableCell>
                          <TableCell className="max-w-[100px] truncate font-mono-nums text-xs text-zinc-500">
                            {String(a.report?.model ?? "—")}
                          </TableCell>
                        </TableRow>
                      ))}
                  </TableBody>
                </Table>
              </div>
            </CardContent>
          </Card>
        </section>
      </main>
    </div>
  )
}
