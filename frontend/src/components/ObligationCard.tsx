// Obligation card: the colour-coded box that surfaces a candidate
// obligation detected by the decision engine. The visual hierarchy
// matches the brief: thick coloured left border, risk badge top-right,
// big confidence bar, and the missing fact framed as a question.

import clsx from "clsx";

import type { ObligationCandidate } from "../api";
import {
  RISK_LABEL,
  missingFactsToQuestion,
  riskBucket,
} from "../lib/presentation";

interface ObligationCardProps {
  obligation: ObligationCandidate;
}

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

export function ObligationCard({ obligation }: ObligationCardProps) {
  const risk = riskBucket(obligation.risk_level);
  const pct = Math.round(obligation.confidence * 100);
  const missingQuestion = missingFactsToQuestion(obligation.blocking_missing_facts);
  const sourceLine =
    obligation.evidence_refs
      .map((e) => e.title)
      .filter((t): t is string => Boolean(t))
      .join(" · ") || "Sin fuentes asociadas";
  return (
    <div
      className={clsx(
        "rounded-xl border border-line bg-surface px-5 py-4 shadow-card",
        "border-l-[6px]",
        BORDER_BY_RISK[risk],
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <h4 className="text-[17px] font-bold text-ink">{obligation.title}</h4>
        <span
          className={clsx(
            "flex-shrink-0 rounded-pill px-3 py-1 text-[11px] font-bold uppercase tracking-[0.08em]",
            BADGE_BY_RISK[risk],
          )}
        >
          {RISK_LABEL[risk]}
        </span>
      </div>

      <div className="mt-4">
        <div className="flex items-center justify-between text-[13px]">
          <span className="text-ink-soft">Confianza</span>
          <span className="font-bold text-ink">{pct}%</span>
        </div>
        <div className="mt-1.5 h-2.5 w-full overflow-hidden rounded-pill bg-line">
          <div
            className="h-full rounded-pill bg-primary"
            style={{ width: `${pct}%` }}
          />
        </div>
      </div>

      <dl className="mt-4 grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-[13px]">
        {missingQuestion && (
          <>
            <dt className="text-muted">Dato que falta</dt>
            <dd className="text-ink">{missingQuestion}</dd>
          </>
        )}
        <dt className="text-muted">Base normativa</dt>
        <dd className="text-ink-soft">{sourceLine}</dd>
      </dl>
    </div>
  );
}

interface ObligationCardListProps {
  obligations: ObligationCandidate[];
  heading?: string;
}

export function ObligationCardList({
  obligations,
  heading = "Obligación detectada",
}: ObligationCardListProps) {
  if (obligations.length === 0) return null;
  return (
    <div className="mt-6">
      <h3 className="mb-3 font-serif text-[20px] font-bold text-ink">{heading}</h3>
      <div className="flex flex-col gap-3">
        {obligations.map((o) => (
          <ObligationCard key={o.obligation_id} obligation={o} />
        ))}
      </div>
    </div>
  );
}
