// What the advisor is doing / did for one answer. Live, it replaces the old
// silent "Consultando…" with the actual searches as they happen; afterwards it
// collapses into a "how I got here" disclosure under the answer.

import { ChevronDown, ClipboardList, FileSearch, Loader2, PencilLine, ShieldCheck } from "lucide-react";
import clsx from "clsx";
import { useEffect, useState } from "react";

import type { ProgressEvent } from "../api";
import { type ActivityRow, activityRows, formatElapsed, statusLine } from "../lib/presentation";

const ROW_ICON = { search: FileSearch, case: ClipboardList, verify: ShieldCheck, revise: PencilLine } as const;

function Rows({ rows }: { rows: ActivityRow[] }) {
  return (
    <ol className="space-y-2">
      {rows.map((row, i) => {
        const Icon = ROW_ICON[row.kind];
        return (
          <li key={i} className="flex gap-2.5 text-[13.5px] leading-snug">
            <Icon size={15} aria-hidden className="mt-0.5 flex-shrink-0 text-primary-dark" />
            <div className="min-w-0">
              <p className="text-ink-soft">
                {row.kind === "search" ? <span>Búsqueda: «{row.text}»</span> : row.text}
                {row.outcome && (
                  <span className={clsx("ml-2 whitespace-nowrap text-[12px]", row.failed ? "text-risk-high-fg" : "text-muted")}>
                    — {row.outcome}
                  </span>
                )}
              </p>
              {row.detail && <p className="text-[12px] text-muted">{row.detail}</p>}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** Live view while the turn is running. */
export function LiveActivity({ events, startedAt }: { events: ProgressEvent[]; startedAt: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  const rows = activityRows(events);

  return (
    <div className="rounded-xl border border-line bg-surface px-4 py-3.5 shadow-card" aria-live="polite">
      <p className="flex items-center gap-2.5 text-[14.5px] font-semibold text-ink">
        <Loader2 size={17} aria-hidden className="animate-spin text-primary" />
        <span className="flex-1">{statusLine(events)}</span>
        <span className="font-mono text-[12px] font-normal tabular-nums text-muted">{formatElapsed(now - startedAt)}</span>
      </p>
      {rows.length > 0 && (
        <div className="mt-3 border-t border-line pt-3">
          <Rows rows={rows} />
        </div>
      )}
      <p className="mt-3 text-[12px] text-muted">
        Busco en la normativa y compruebo cada afirmación antes de responder; puede tardar uno o dos minutos.
      </p>
    </div>
  );
}

/** Collapsed record under a finished answer. */
export function ActivitySummary({ rows, elapsedMs }: { rows: ActivityRow[]; elapsedMs: number }) {
  if (rows.length === 0) return null;
  const searches = rows.filter((r) => r.kind === "search").length;
  const label = [
    searches > 0 ? (searches === 1 ? "1 búsqueda" : `${searches} búsquedas`) : null,
    rows.some((r) => r.kind === "revise") ? "1 corrección" : null,
    formatElapsed(elapsedMs),
  ]
    .filter(Boolean)
    .join(" · ");
  return (
    <details className="group mt-3">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 text-[12.5px] font-medium text-muted hover:text-ink-soft [&::-webkit-details-marker]:hidden">
        <ChevronDown size={14} aria-hidden className="transition group-open:rotate-180" />
        Cómo llegué a esta respuesta · {label}
      </summary>
      <div className="mt-2 rounded-lg border border-line bg-surface px-4 py-3">
        <Rows rows={rows} />
      </div>
    </details>
  );
}
