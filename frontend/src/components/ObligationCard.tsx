// Obligation card: a tax obligation the rules engine thinks may apply to the
// user, with the thing people actually need first — the deadline.

import { CalendarClock } from "lucide-react";
import clsx from "clsx";

import type { ObligationCandidate } from "../api";
import { RISK_LABEL, daysUntil, formatDate, missingFactsSentence, riskBucket } from "../lib/presentation";

const BORDER_BY_RISK = {
  high: "border-l-risk-high-fg",
  medium: "border-l-risk-medium-fg",
  low: "border-l-risk-low-fg",
} as const;

const BADGE_BY_RISK = {
  high: "bg-risk-high-bg text-risk-high-fg",
  medium: "bg-risk-medium-bg text-risk-medium-fg",
  low: "bg-risk-low-bg text-risk-low-fg",
} as const;

function deadlineCopy(iso: string): { text: string; urgent: boolean } {
  const days = daysUntil(iso);
  const date = formatDate(iso) ?? iso;
  if (days === null) return { text: date, urgent: false };
  if (days < 0) return { text: `${date} · plazo vencido`, urgent: true };
  if (days === 0) return { text: `${date} · vence hoy`, urgent: true };
  if (days <= 30) return { text: `${date} · quedan ${days} ${days === 1 ? "día" : "días"}`, urgent: true };
  return { text: date, urgent: false };
}

export function ObligationCard({ obligation }: { obligation: ObligationCandidate }) {
  const risk = riskBucket(obligation.risk_level);
  const missing = missingFactsSentence(obligation.blocking_missing_facts);
  const sources = obligation.evidence_refs.map((e) => e.title).filter((t): t is string => Boolean(t));
  const deadline = obligation.due_date ? deadlineCopy(obligation.due_date) : null;

  return (
    <article className={clsx("rounded-xl border border-l-[5px] border-line bg-surface px-4 py-4 shadow-card sm:px-5", BORDER_BY_RISK[risk])}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h4 className="text-[16px] font-bold text-ink">{obligation.title}</h4>
        <span className={clsx("flex-shrink-0 rounded-pill px-2.5 py-1 text-[11px] font-bold uppercase tracking-[0.06em]", BADGE_BY_RISK[risk])}>
          {RISK_LABEL[risk]}
        </span>
      </div>
      <p className="mt-1 text-[13.5px] leading-snug text-ink-soft">{obligation.description}</p>

      {deadline && (
        <div className="mt-3">
          <p className={clsx("flex items-center gap-2 text-[13.5px] font-semibold", deadline.urgent ? "text-risk-high-fg" : "text-ink")}>
            <CalendarClock size={16} aria-hidden />
            Fecha límite: {deadline.text}
          </p>
          {obligation.due_date_note && <p className="mt-1 pl-6 text-[12.5px] leading-snug text-muted">{obligation.due_date_note}</p>}
        </div>
      )}

      {(missing || sources.length > 0) && (
        <dl className="mt-3 space-y-1.5 border-t border-line pt-3 text-[13px]">
          {missing && (
            <div>
              <dt className="sr-only">Datos pendientes</dt>
              <dd className="text-ink-soft">{missing}</dd>
            </div>
          )}
          {sources.length > 0 && (
            <div className="flex flex-col gap-0.5 sm:flex-row sm:gap-2">
              <dt className="flex-shrink-0 text-muted">Base normativa</dt>
              <dd className="text-ink-soft">{sources.join(" · ")}</dd>
            </div>
          )}
        </dl>
      )}
      <p className="mt-3 text-[12px] text-muted">
        Indicio detectado a partir de tus datos (certeza estimada {Math.round(obligation.confidence * 100)} %). Confírmalo antes de actuar.
      </p>
    </article>
  );
}

interface ObligationCardListProps {
  obligations: ObligationCandidate[];
}

export function ObligationCardList({ obligations }: ObligationCardListProps) {
  if (obligations.length === 0) return null;
  return (
    <section className="mt-6" aria-label="Obligaciones detectadas">
      <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">
        {obligations.length === 1 ? "Posible obligación" : `Posibles obligaciones · ${obligations.length}`}
      </h3>
      <div className="flex flex-col gap-2.5">
        {obligations.map((o) => (
          <ObligationCard key={o.obligation_id} obligation={o} />
        ))}
      </div>
    </section>
  );
}
