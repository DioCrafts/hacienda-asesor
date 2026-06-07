// Pure helpers for mapping backend fields to presentation values.
// Kept separate from components so they can be unit-tested in isolation
// and reused (e.g. for screenshots / fixtures).

import type { AnswerMode, Citation, RiskLevel } from "../api";

export type SourceKind = "boe" | "manual" | "dgt" | "teac" | "other";

/** Pick an icon family + label for a citation, based on its locator. */
export function classifySource(c: Citation): SourceKind {
  const loc = (c.locator || "").toLowerCase();
  const title = (c.title || "").toLowerCase();
  if (loc.startsWith("boe://") || loc.includes("boe.es")) return "boe";
  if (loc.startsWith("aeat://") || loc.includes("agenciatributaria")) return "manual";
  if (loc.startsWith("dgt://") || loc.includes("petete.tributos") || /\bv\d{4}-\d{2}\b/i.test(title))
    return "dgt";
  if (loc.startsWith("teac://") || title.includes("teac")) return "teac";
  return "other";
}

/** Shorten a long locator while keeping the discriminating end. */
export function compactLocator(loc: string, maxLen = 56): string {
  if (loc.length <= maxLen) return loc;
  return loc.slice(0, maxLen - 1).trimEnd() + "…";
}

/** Map RiskLevel -> visual bucket. The mockups define 3 colours
 *  (high/medium/low); CRITICAL collapses onto HIGH. */
export function riskBucket(r: RiskLevel): "high" | "medium" | "low" {
  if (r === "critical" || r === "high") return "high";
  if (r === "medium") return "medium";
  return "low";
}

export const RISK_LABEL: Record<"high" | "medium" | "low", string> = {
  high: "Riesgo alto",
  medium: "Riesgo medio",
  low: "Riesgo bajo",
};

export interface TrustCopy {
  cls: "cited" | "uncited" | "abstain";
  label: string;
  short: string;
}
export const TRUST: Record<AnswerMode, TrustCopy> = {
  cited: {
    cls: "cited",
    label: "Respuesta con citas verificadas",
    short: "Cada afirmación se apoya en las fuentes listadas.",
  },
  uncited: {
    cls: "uncited",
    label: "Orientativo · sin citas verificadas",
    short: "Trátalo como una orientación y verifícalo.",
  },
  abstained: {
    cls: "abstain",
    label: "Abstención · sin base normativa",
    short: "Prefiero no responder antes que arriesgar un dato sin fuente.",
  },
};

/** Render a list of missing fact names as a friendly question. */
export function missingFactsToQuestion(missing: string[]): string {
  if (missing.length === 0) return "";
  if (missing.length === 1) {
    const m = missing[0].replace(/_/g, " ");
    return `¿Puedes confirmarme «${m}»?`;
  }
  return `Aún me faltan: ${missing.map((m) => m.replace(/_/g, " ")).join(", ")}`;
}
