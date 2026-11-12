// Source card: one per fragment the answer cites. Shows who issued it, when,
// whether it binds the Administration, and the literal passage — and is the
// jump target of the [F#] chips in the answer.

import { Book, Calculator, ExternalLink, Gavel, Landmark, Library, ScrollText } from "lucide-react";
import clsx from "clsx";

import type { Citation } from "../api";
import { authorityLabel, classifySource, formatDate, hostLabel, isHttpUrl, sourceAnchorId } from "../lib/presentation";

const SOURCE_ICON = {
  boe: Landmark, // legislation
  manual: Book, // AEAT handbook / informative page
  dgt: ScrollText, // DGT binding consulta
  court: Gavel, // TEAC / TS / TC
  calc: Calculator, // deterministic IRPF computation
  other: Library,
} as const;

// Only statuses that should make the reader cautious get a badge.
const VIGENCIA_BADGE: Record<string, { label: string; cls: string }> = {
  superada: { label: "Superada", cls: "bg-risk-high-bg text-risk-high-fg" },
  discutida: { label: "Criterio discutido", cls: "bg-grounding-uncited-bg text-grounding-uncited-fg" },
  matizada: { label: "Matizada", cls: "bg-grounding-uncited-bg text-grounding-uncited-fg" },
};

interface SourceCardProps {
  citation: Citation;
  scope: string;
  highlighted: boolean;
}

function SourceCard({ citation, scope, highlighted }: SourceCardProps) {
  const Icon = SOURCE_ICON[classifySource(citation)];
  const issuer = authorityLabel(citation.authority);
  const date = formatDate(citation.date);
  const meta = [issuer, citation.reference, date].filter(Boolean).join(" · ");
  const section = citation.section && citation.section !== "general" && citation.section !== citation.title;

  return (
    <article
      id={citation.fragment_id ? sourceAnchorId(scope, citation.fragment_id) : undefined}
      className={clsx(
        "flex scroll-mt-24 gap-3 rounded-xl border bg-surface p-4 shadow-card transition-colors sm:gap-4",
        highlighted ? "animate-flash border-primary" : "border-line",
      )}
    >
      <div className="flex h-10 w-10 flex-shrink-0 items-center justify-center rounded-lg bg-primary-soft text-primary-dark">
        <Icon size={20} aria-hidden />
      </div>
      <div className="min-w-0 flex-1">
        <h4 className="font-semibold leading-snug text-ink">
          {citation.fragment_id && (
            <span className="mr-2 rounded bg-primary-soft px-1.5 py-0.5 font-mono text-[12px] text-primary-dark">
              {citation.fragment_id}
            </span>
          )}
          {citation.title}
          {section && <span className="font-normal text-ink-soft"> · {citation.section}</span>}
        </h4>
        <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12.5px] text-muted">
          {meta && <span>{meta}</span>}
          {citation.binding === true && (
            <span className="rounded-pill bg-grounding-cited-bg px-2 py-px text-[11px] font-semibold text-grounding-cited-fg">
              Vinculante
            </span>
          )}
          {citation.vigencia && VIGENCIA_BADGE[citation.vigencia] && (
            <span
              title={citation.vigencia_resumen ?? undefined}
              className={clsx("rounded-pill px-2 py-px text-[11px] font-semibold", VIGENCIA_BADGE[citation.vigencia].cls)}
            >
              {VIGENCIA_BADGE[citation.vigencia].label}
            </span>
          )}
          {citation.binding === false && (
            <span className="rounded-pill bg-grounding-abstain-bg px-2 py-px text-[11px] font-semibold text-grounding-abstain-fg">
              No vinculante
            </span>
          )}
        </p>
        {citation.vigencia && VIGENCIA_BADGE[citation.vigencia] && citation.vigencia_resumen && (
          <p className="mt-1 text-[12.5px] text-ink-soft">{citation.vigencia_resumen}</p>
        )}
        {citation.snippet && (
          <blockquote className="mt-2 border-l-2 border-line pl-3 text-[13px] italic leading-relaxed text-ink-soft">
            {citation.snippet}
          </blockquote>
        )}
        {isHttpUrl(citation.locator) && (
          <a
            href={citation.locator}
            target="_blank"
            rel="noopener noreferrer"
            className="mt-2 inline-flex items-center gap-1 text-[12.5px] font-semibold text-primary-dark hover:underline"
          >
            Abrir en {hostLabel(citation.locator)}
            <ExternalLink size={13} aria-hidden />
          </a>
        )}
      </div>
    </article>
  );
}

interface SourceCardListProps {
  citations: Citation[];
  scope: string;
  highlightedId: string | null;
}

export function SourceCardList({ citations, scope, highlightedId }: SourceCardListProps) {
  if (citations.length === 0) return null;
  return (
    <section className="mt-5" aria-label="Fuentes citadas">
      <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">
        Fuentes citadas · {citations.length}
      </h3>
      <div className="flex flex-col gap-2.5">
        {citations.map((c, i) => (
          <SourceCard
            key={c.fragment_id ?? `${c.locator}-${i}`}
            citation={c}
            scope={scope}
            highlighted={!!c.fragment_id && highlightedId === sourceAnchorId(scope, c.fragment_id)}
          />
        ))}
      </div>
    </section>
  );
}
