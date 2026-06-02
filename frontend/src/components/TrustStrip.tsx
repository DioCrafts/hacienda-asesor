// Trust strip: a single rounded pill that names the grounding verdict
// of an answer. The colour palette comes from the design tokens.

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
}

export function TrustStrip({ mode }: TrustStripProps) {
  const copy = TRUST[mode];
  const Icon = ICON_BY_MODE[copy.cls];
  return (
    <div className="my-3 flex justify-start">
      <div
        className={clsx(
          "inline-flex items-center gap-2 rounded-pill border px-4 py-2 text-[14px] font-semibold",
          copy.cls === "cited" &&
            "border-grounding-cited-bd bg-grounding-cited-bg text-grounding-cited-fg",
          copy.cls === "uncited" &&
            "border-grounding-uncited-bd bg-grounding-uncited-bg text-grounding-uncited-fg",
          copy.cls === "abstain" &&
            "border-grounding-abstain-bd bg-grounding-abstain-bg text-grounding-abstain-fg",
        )}
      >
        <Icon size={16} aria-hidden />
        {copy.label}
      </div>
    </div>
  );
}
