// Source card: title + icon + locator chip + snippet quote + optional
// outbound link. Renders one per Citation returned by /qa.

import { Book, ExternalLink, Gavel, Landmark, Library, ScrollText } from "lucide-react";

import type { Citation } from "../api";
import { classifySource } from "../lib/presentation";

// Landmark (pillared building) reads as "law" more clearly than the
// generic office Building2; Gavel for tribunal decisions, ScrollText
// for administrative criteria, Book for handbooks, Library as the
// catch-all for "we have it but it's not categorised".
const SOURCE_ICON = {
  boe: Landmark,     // BOE consolidated law
  manual: Book,      // AEAT manual / práctica
  dgt: ScrollText,   // DGT consulta vinculante
  teac: Gavel,       // TEAC criterio
  other: Library,
} as const;

interface SourceCardProps {
  citation: Citation;
}

export function SourceCard({ citation }: SourceCardProps) {
  const kind = classifySource(citation);
  const Icon = SOURCE_ICON[kind];
  const isLink =
    citation.locator?.startsWith("http://") ||
    citation.locator?.startsWith("https://");
  return (
    <div className="flex gap-4 rounded-xl border border-line bg-surface p-4 shadow-card">
      <div className="flex h-11 w-11 flex-shrink-0 items-center justify-center rounded-lg bg-primary-soft text-primary-dark">
        <Icon size={22} aria-hidden />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-start justify-between gap-3">
          <h4 className="font-semibold text-ink leading-snug">
            {citation.title}
          </h4>
          {isLink && (
            <a
              href={citation.locator}
              target="_blank"
              rel="noopener noreferrer"
              className="flex-shrink-0 text-muted hover:text-primary-dark"
              aria-label="Abrir fuente original"
            >
              <ExternalLink size={16} />
            </a>
          )}
        </div>
        {citation.locator && (
          <code className="mt-1 inline-block font-mono text-[12px] text-ink-soft break-all">
            {citation.locator}
            {citation.section && <span className="text-muted"> · {citation.section}</span>}
          </code>
        )}
        {citation.snippet && (
          <p className="mt-2 text-[13px] italic text-ink-soft">
            «{citation.snippet}»
          </p>
        )}
      </div>
    </div>
  );
}

interface SourceCardListProps {
  citations: Citation[];
}

export function SourceCardList({ citations }: SourceCardListProps) {
  if (citations.length === 0) return null;
  return (
    <div className="mt-4">
      <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">
        Fuentes normativas · {citations.length}
      </h3>
      <div className="flex flex-col gap-3">
        {citations.map((c, i) => (
          <SourceCard key={`${c.locator}-${i}`} citation={c} />
        ))}
      </div>
    </div>
  );
}
