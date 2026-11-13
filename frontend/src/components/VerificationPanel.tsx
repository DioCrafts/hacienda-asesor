// Claim-by-claim verification: what the independent checker concluded about
// each normative statement in the answer, in plain language. Problems are
// open by default; a fully verified answer keeps the detail one click away.

import { AlertTriangle, CheckCircle2, ChevronDown, XCircle } from "lucide-react";
import clsx from "clsx";

import type { ClaimCheck, ClaimVerdict } from "../api";
import { VERDICT_LABEL } from "../lib/presentation";

const VERDICT_STYLE: Record<ClaimVerdict, { icon: typeof CheckCircle2; tone: string }> = {
  respaldada: { icon: CheckCircle2, tone: "text-grounding-cited-fg" },
  parcial: { icon: AlertTriangle, tone: "text-grounding-uncited-fg" },
  no_respaldada: { icon: XCircle, tone: "text-risk-high-fg" },
};

interface VerificationPanelProps {
  claims: ClaimCheck[];
  // Warnings that are not about a specific claim (e.g. verifier unavailable).
  otherWarnings: string[];
  onCite: (fragmentId: string) => void;
}

export function VerificationPanel({ claims, otherWarnings, onCite }: VerificationPanelProps) {
  if (claims.length === 0 && otherWarnings.length === 0) return null;
  const problems = claims.filter((c) => c.verdict !== "respaldada");
  // Problems first: that is what the reader must act on.
  const ordered = [...problems, ...claims.filter((c) => c.verdict === "respaldada")];

  return (
    <div className="mt-3 space-y-2">
      {otherWarnings.map((warning, i) => (
        <p
          key={`w-${i}`}
          className="flex gap-2 rounded-lg border border-grounding-uncited-bd bg-grounding-uncited-bg px-3 py-2 text-[13px] text-ink-soft"
        >
          <AlertTriangle size={15} aria-hidden className="mt-0.5 flex-shrink-0 text-grounding-uncited-fg" />
          {warning}
        </p>
      ))}
      {claims.length > 0 && (
        <details open={problems.length > 0} className="group rounded-xl border border-line bg-surface">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-4 py-2.5 text-[13px] font-semibold text-ink-soft [&::-webkit-details-marker]:hidden">
            <span>
              Comprobación afirmación por afirmación
              {problems.length > 0 && (
                <span className="ml-2 rounded-pill bg-risk-high-bg px-2 py-0.5 text-[11px] font-bold text-risk-high-fg">
                  {problems.length === 1 ? "1 a revisar" : `${problems.length} a revisar`}
                </span>
              )}
            </span>
            <ChevronDown size={16} aria-hidden className="flex-shrink-0 text-muted transition group-open:rotate-180" />
          </summary>
          <ul className="divide-y divide-line border-t border-line">
            {ordered.map((claim, i) => {
              const style = VERDICT_STYLE[claim.verdict];
              const Icon = style.icon;
              return (
                <li key={i} className="flex gap-3 px-4 py-3">
                  <Icon size={17} aria-hidden className={clsx("mt-0.5 flex-shrink-0", style.tone)} />
                  <div className="min-w-0 text-[13.5px] leading-snug">
                    <p className="text-ink">{claim.text}</p>
                    <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px]">
                      <span className={clsx("font-semibold", style.tone)}>{VERDICT_LABEL[claim.verdict]}</span>
                      {claim.fragment_ids.map((id) => (
                        <button
                          key={id}
                          type="button"
                          onClick={() => onCite(id)}
                          aria-label={`Ver la fuente ${id}`}
                          className="rounded bg-primary-soft px-1.5 py-px font-mono text-[11px] font-semibold text-primary-dark transition hover:bg-primary hover:text-white"
                        >
                          {id}
                        </button>
                      ))}
                    </p>
                    {claim.verdict !== "respaldada" && claim.reason && (
                      <p className="mt-1 text-[12.5px] text-ink-soft">{claim.reason}</p>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </details>
      )}
    </div>
  );
}
