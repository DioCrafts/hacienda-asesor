// Trust strip: the grounding-verdict pill rendered above every answer.
// Matches the mockup: a centred pill with a filled status icon (check /
// alert / shield) and a single short label. The auxiliary "reason"
// copy (e.g. why we abstained) is rendered as a small caption below
// the pill so it does not crowd the headline verdict.

import { AlertCircle, CheckCircle2, ShieldAlert } from "lucide-react";
import clsx from "clsx";

import type { AnswerMode } from "../api";
import { TRUST } from "../lib/presentation";

const ICON_BY_MODE = {
  cited: CheckCircle2,
  uncited: AlertCircle,
  abstain: ShieldAlert,
} as const;

interface TrustStripProps {
  mode: AnswerMode;
  reason?: string | null;
}

export function TrustStrip({ mode, reason }: TrustStripProps) {
  const copy = TRUST[mode];
  const Icon = ICON_BY_MODE[copy.cls];
  return (
    <div className="my-4 flex flex-col items-center gap-1.5">
      <div
        className={clsx(
          "inline-flex items-center gap-2 rounded-pill border px-5 py-2.5 text-[15px] font-bold",
          copy.cls === "cited" &&
            "border-grounding-cited-bd bg-grounding-cited-bg text-grounding-cited-fg",
          copy.cls === "uncited" &&
            "border-grounding-uncited-bd bg-grounding-uncited-bg text-grounding-uncited-fg",
          copy.cls === "abstain" &&
            "border-grounding-abstain-bd bg-grounding-abstain-bg text-grounding-abstain-fg",
        )}
      >
        <Icon size={18} aria-hidden strokeWidth={2.5} />
        {copy.label}
      </div>
      {reason && (
        <p className="text-center text-[12px] text-muted px-4">{reason}</p>
      )}
    </div>
  );
}
