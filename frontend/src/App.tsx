import { Activity, BrainCircuit, Radio } from "lucide-react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts"
import { Badge } from "@/components/ui/badge"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import type { AuditRow, ChartPoint, TelemetryPayload } from "@/types/telemetry"

const MAX_POINTS = 500

function wsUrl(path: string) {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:"
  return `${proto}//${window.location.host}${path}`
}

export default function App() {
  const [chartData, setChartData] = useState<ChartPoint[]>([])
  const [audits, setAudits] = useState<AuditRow[]>([])
  const [pulseLive, setPulseLive] = useState(false)
  const [eventsTotal, setEventsTotal] = useState<number>(0)
  const [geminiOk, setGeminiOk] = useState(false)
  const [feedMode, setFeedMode] = useState<string>("—")
  const chartDataRef = useRef<ChartPoint[]>([])
  const auditsRef = useRef<Map<number, AuditRow>>(new Map())

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
        const rows: { t: number; basis: number; innovation: number }[] = await r.json()
        if (cancelled) return
        const initial: ChartPoint[] = rows.map((x) => ({
          t: x.t,
          basis: x.basis,
          innovation: x.innovation,
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
    const ws = new WebSocket(wsUrl("/ws/telemetry"))
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
          basis: p.health.filtered_basis,
          innovation: p.health.innovation,
        })
      } catch {
        /* ignore */
      }
    }
    return () => ws.close()
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
    let t: ReturnType<typeof setInterval>
    const tick = async () => {
      try {
        const [pulseR, statusR] = await Promise.all([
          fetch("/api/pulse"),
          fetch("/api/status"),
        ])
        if (pulseR.ok) {
          const p = await pulseR.json()
          setPulseLive(!!p.live)
          setEventsTotal(Number(p.events_total_sqlite ?? 0))
        }
        if (statusR.ok) {
          const s = await statusR.json()
          setGeminiOk(!!s.gemini_configured)
          setFeedMode(String(s.binance_feed ?? "—"))
        }
      } catch {
        /* ignore */
      }
    }
    tick()
    t = setInterval(tick, 2000)
    return () => clearInterval(t)
  }, [])

  const chartContent = useMemo(() => chartData, [chartData])

  const formatX = (v: number) =>
    new Date(v * 1000).toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    })

  return (
    <div className="flex min-h-full flex-col bg-[#09090b] text-zinc-100">
      <header className="flex shrink-0 items-center justify-between gap-4 border-b border-zinc-800 px-5 py-3">
        <div className="flex items-center gap-2 text-sm font-semibold tracking-tight text-zinc-200">
          <span className="text-[var(--color-gold)]">DYOPS</span>
          <span className="text-zinc-600">|</span>
          <span className="font-normal text-zinc-500">Basis Guard</span>
        </div>

        <div className="flex flex-wrap items-center justify-end gap-4">
          <div className="flex items-center gap-2 text-xs text-zinc-500">
            <Radio className="size-3.5 text-zinc-500" aria-hidden />
            <span className="uppercase tracking-wide">System pulse</span>
            <span
              className={`font-mono-nums text-xs font-medium ${pulseLive ? "text-emerald-400" : "text-zinc-500"}`}
            >
              {pulseLive ? "LIVE" : "STALE"}
            </span>
            <span
              className={`size-2 rounded-full ${pulseLive ? "animate-pulse bg-emerald-400 shadow-[0_0_10px_rgba(52,211,153,0.7)]" : "bg-zinc-600"}`}
            />
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
            <span className="font-mono-nums font-semibold text-[var(--color-gold)] tabular-nums">
              {eventsTotal.toLocaleString()}
            </span>
          </div>

          <Badge variant="outline" className="font-mono-nums text-[10px]">
            {feedMode}
          </Badge>
        </div>
      </header>

      <main className="flex min-h-0 flex-1 gap-4 p-4">
        <section className="flex min-h-0 min-w-0 w-[70%] flex-col">
          <Card className="flex min-h-0 flex-1 flex-col border-zinc-800 bg-transparent shadow-none">
            <CardHeader className="pb-2">
              <CardTitle className="text-xs font-medium uppercase tracking-widest text-zinc-500">
                Filtered basis vs innovation
              </CardTitle>
            </CardHeader>
            <CardContent className="min-h-0 flex-1 px-2 pb-2 pt-0">
              <div className="h-full min-h-[420px] w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart
                    data={chartContent}
                    margin={{ top: 8, right: 16, left: 0, bottom: 8 }}
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
                      tickFormatter={formatX}
                      stroke="#3f3f46"
                    />
                    <YAxis
                      yAxisId="l"
                      tick={{ fill: "#a1a1aa", fontSize: 10 }}
                      stroke="#3f3f46"
                      width={52}
                    />
                    <YAxis
                      yAxisId="r"
                      orientation="right"
                      tick={{ fill: "#67e8f9", fontSize: 10 }}
                      stroke="#3f3f46"
                      width={52}
                    />
                    <Tooltip
                      contentStyle={{
                        background: "#18181b",
                        border: "1px solid #27272a",
                        borderRadius: 8,
                        fontSize: 12,
                        fontFamily: "JetBrains Mono, monospace",
                      }}
                      labelFormatter={(v) =>
                        typeof v === "number" ? formatX(v) : String(v)
                      }
                    />
                    <Legend
                      wrapperStyle={{ fontSize: 11, fontFamily: "JetBrains Mono, monospace" }}
                    />
                    <Line
                      yAxisId="l"
                      type="monotone"
                      dataKey="basis"
                      name="Filtered basis"
                      stroke="#e4c465"
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                      connectNulls
                    />
                    <Line
                      yAxisId="r"
                      type="monotone"
                      dataKey="innovation"
                      name="Innovation"
                      stroke="#22d3ee"
                      strokeWidth={1.5}
                      dot={false}
                      isAnimationActive={false}
                      connectNulls
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            </CardContent>
          </Card>
        </section>

        <section className="flex w-[30%] min-w-[280px] flex-col border-l border-zinc-800 pl-4">
          <Card className="flex min-h-0 flex-1 flex-col border-zinc-800 bg-[#0c0c0f] shadow-none">
            <CardHeader className="pb-2">
              <CardTitle className="text-xs font-medium uppercase tracking-widest text-zinc-500">
                Live audit log
              </CardTitle>
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
              <div className="border-t border-zinc-800 px-4 py-3">
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
