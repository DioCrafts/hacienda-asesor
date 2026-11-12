// The verdict banner above every answer: how far this answer can be trusted,
// in one line plus a plain-language explanation.

import { AlertCircle, CheckCircle2, HelpCircle, ShieldAlert } from "lucide-react";
import clsx from "clsx";

import type { AnswerMode } from "../api";
import { TRUST } from "../lib/presentation";

const ICON_BY_CLS = { cited: CheckCircle2, uncited: AlertCircle, abstain: ShieldAlert } as const;

interface TrustStripProps {
  mode: AnswerMode;
  // e.g. "4 de 5 afirmaciones verificadas"; replaces the generic explanation.
  detail?: string | null;
}

export function TrustStrip({ mode, detail }: TrustStripProps) {
  const copy = TRUST[mode];
  const Icon = mode === "clarification" ? HelpCircle : ICON_BY_CLS[copy.cls];
  return (
    <div
      role="status"
      className={clsx(
        "mt-4 flex items-start gap-3 rounded-xl border px-4 py-3",
        copy.cls === "cited" && "border-grounding-cited-bd bg-grounding-cited-bg",
        copy.cls === "uncited" && "border-grounding-uncited-bd bg-grounding-uncited-bg",
        copy.cls === "abstain" && "border-grounding-abstain-bd bg-grounding-abstain-bg",
      )}
    >
      <Icon
        size={20}
        aria-hidden
        strokeWidth={2.25}
        className={clsx(
          "mt-0.5 flex-shrink-0",
          copy.cls === "cited" && "text-grounding-cited-fg",
          copy.cls === "uncited" && "text-grounding-uncited-fg",
          copy.cls === "abstain" && "text-grounding-abstain-fg",
        )}
      />
      <div className="min-w-0">
        <p
          className={clsx(
            "text-[14.5px] font-bold leading-snug",
            copy.cls === "cited" && "text-grounding-cited-fg",
            copy.cls === "uncited" && "text-grounding-uncited-fg",
            copy.cls === "abstain" && "text-grounding-abstain-fg",
          )}
        >
          {copy.label}
          {detail && <span className="font-medium"> · {detail}</span>}
        </p>
        <p className="mt-0.5 text-[13px] leading-snug text-ink-soft">{copy.short}</p>
      </div>
    </div>
  );
}
