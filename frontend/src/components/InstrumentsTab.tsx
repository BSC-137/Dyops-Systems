import { Badge } from "@/components/ui/badge"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import type { InstrumentInfo, SentinelLevel } from "@/types/telemetry"

type InstrumentsTabProps = {
  instruments: InstrumentInfo[]
  onSelect: (instrumentId: string) => void
}

function levelVariant(
  level: SentinelLevel,
): "success" | "warning" | "destructive" {
  if (level === "AUDIT") return "destructive"
  if (level === "BREACH") return "warning"
  return "success"
}

export function InstrumentsTab({
  instruments,
  onSelect,
}: InstrumentsTabProps) {
  return (
    <main className="flex min-h-0 flex-1 p-4">
      <section className="w-full overflow-hidden rounded-lg border border-[var(--color-border)]">
        <div className="border-b border-[var(--color-border)] px-4 py-3">
          <h1 className="text-xs font-medium uppercase tracking-widest text-zinc-500">
            Instruments
          </h1>
          <p className="mt-1 font-mono-nums text-[11px] text-zinc-600">
            Select a row to focus its live telemetry.
          </p>
        </div>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>ID</TableHead>
              <TableHead>Label</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Level</TableHead>
              <TableHead>Last Mahalanobis</TableHead>
              <TableHead className="text-right">Events</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {instruments.map((instrument) => (
              <TableRow
                key={instrument.id}
                tabIndex={0}
                role="button"
                onClick={() => onSelect(instrument.id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault()
                    onSelect(instrument.id)
                  }
                }}
                className="cursor-pointer focus-visible:bg-zinc-900 focus-visible:outline-none"
              >
                <TableCell className="font-mono-nums text-xs text-stone-300">
                  {instrument.id}
                </TableCell>
                <TableCell className="text-xs">{instrument.label}</TableCell>
                <TableCell>
                  <Badge
                    variant={instrument.live ? "success" : "outline"}
                    className="text-[10px]"
                  >
                    {instrument.live ? "LIVE" : "STALE"}
                  </Badge>
                </TableCell>
                <TableCell>
                  <Badge
                    variant={levelVariant(instrument.level)}
                    className="text-[10px]"
                  >
                    {instrument.level}
                  </Badge>
                </TableCell>
                <TableCell className="font-mono-nums text-xs">
                  {instrument.last_mahalanobis === null
                    ? "—"
                    : instrument.last_mahalanobis.toFixed(6)}
                </TableCell>
                <TableCell className="text-right font-mono-nums text-xs">
                  {instrument.events_total_sqlite.toLocaleString()}
                </TableCell>
              </TableRow>
            ))}
            {instruments.length === 0 ? (
              <TableRow>
                <TableCell
                  colSpan={6}
                  className="py-8 text-center font-mono-nums text-xs text-zinc-600"
                >
                  No configured instruments.
                </TableCell>
              </TableRow>
            ) : null}
          </TableBody>
        </Table>
      </section>
    </main>
  )
}
