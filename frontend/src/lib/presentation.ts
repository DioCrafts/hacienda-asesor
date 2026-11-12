// Pure helpers for mapping backend fields to presentation values.
// Kept separate from components so they can be unit-tested in isolation.

import type { AnswerMode, Citation, ClaimCheck, ClaimVerdict, Fact, ProgressEvent, RiskLevel } from "../api";

export type SourceKind = "boe" | "manual" | "dgt" | "court" | "other";

/** Pick an icon family for a citation: the agent's explicit authority first,
 *  URL/title sniffing as a fallback for legacy /qa citations. */
export function classifySource(c: Citation): SourceKind {
  const authority = (c.authority || "").toUpperCase();
  if (authority === "DGT") return "dgt";
  if (authority === "TEAC" || authority === "TEAR" || authority.startsWith("TS") || authority === "TC") return "court";
  if (authority === "AEAT") return "manual";
  if (authority === "BOE" || authority.startsWith("BOLETIN")) return "boe";
  const loc = (c.locator || "").toLowerCase();
  const title = (c.title || "").toLowerCase();
  if (loc.startsWith("boe://") || loc.includes("boe.es")) return "boe";
  if (loc.startsWith("aeat://") || loc.includes("agenciatributaria")) return "manual";
  if (loc.startsWith("dgt://") || loc.includes("petete.tributos") || /\bv\d{4}-\d{2}\b/i.test(title)) return "dgt";
  if (loc.startsWith("teac://") || title.includes("teac")) return "court";
  return "other";
}

const AUTHORITY_LABEL: Record<string, string> = {
  BOE: "Boletín Oficial del Estado",
  DGT: "Dirección General de Tributos",
  TEAC: "Tribunal Económico-Administrativo Central",
  TEAR: "Tribunal Económico-Administrativo Regional",
  TC: "Tribunal Constitucional",
  TS: "Tribunal Supremo",
  TS_SALA_TERCERA: "Tribunal Supremo · Sala Tercera",
  AEAT: "Agencia Tributaria",
};

export function authorityLabel(authority: string | null | undefined): string | null {
  if (!authority) return null;
  if (authority.toUpperCase().startsWith("BOLETIN")) return "Boletín autonómico";
  return AUTHORITY_LABEL[authority.toUpperCase()] ?? authority;
}

export function isHttpUrl(value: string | null | undefined): value is string {
  return !!value && (value.startsWith("http://") || value.startsWith("https://"));
}

/** "boe.es" from a full URL, for a compact "Abrir en boe.es" link. */
export function hostLabel(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "la fuente";
  }
}

const DATE_FMT = new Intl.DateTimeFormat("es-ES", { day: "numeric", month: "short", year: "numeric" });

/** "2025-01-20" → "20 ene 2025". Returns the input untouched if it is not a date. */
export function formatDate(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!match) return iso;
  // Build in local time from the parts: `new Date("2026-06-30")` is UTC and can
  // render as the previous day in negative-offset time zones.
  return DATE_FMT.format(new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
}

/** Whole days from today until an ISO date (negative when past). */
export function daysUntil(iso: string): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso);
  if (!match) return null;
  const target = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  return Math.round((target.getTime() - today.getTime()) / 86_400_000);
}

export function relativeTime(timestamp: number): string {
  const minutes = Math.round((Date.now() - timestamp) / 60_000);
  if (minutes < 1) return "ahora";
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `hace ${hours} h`;
  const days = Math.round(hours / 24);
  if (days < 7) return days === 1 ? "ayer" : `hace ${days} días`;
  return DATE_FMT.format(new Date(timestamp));
}

export function formatElapsed(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds} s`;
  return `${Math.floor(seconds / 60)} min ${String(seconds % 60).padStart(2, "0")} s`;
}

/** Map RiskLevel -> visual bucket (CRITICAL collapses onto HIGH). */
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
  verified: {
    cls: "cited",
    label: "Verificada contra sus fuentes",
    short: "Cada afirmación normativa se ha comprobado con el texto de la fuente que cita.",
  },
  partially_verified: {
    cls: "uncited",
    label: "Verificada solo en parte",
    short: "Alguna afirmación no queda respaldada por la fuente citada. Mira el detalle antes de fiarte.",
  },
  unverified: {
    cls: "uncited",
    label: "Orientativa · sin verificar",
    short: "No se ha podido comprobar contra las fuentes; tómala como una orientación general.",
  },
  clarification: {
    cls: "abstain",
    label: "Necesito un dato más",
    short: "La respuesta depende de información que todavía no tengo.",
  },
  cited: {
    cls: "cited",
    label: "Respuesta con fuentes",
    short: "Se apoya en las fuentes listadas.",
  },
  uncited: {
    cls: "uncited",
    label: "Orientativa · sin fuentes",
    short: "Trátala como una orientación y verifícala.",
  },
  abstained: {
    cls: "abstain",
    label: "Sin base normativa suficiente",
    short: "Prefiero no responder antes que darte un dato sin fuente.",
  },
};

export const VERDICT_LABEL: Record<ClaimVerdict, string> = {
  respaldada: "Respaldada por la fuente",
  parcial: "Respaldada solo en parte",
  no_respaldada: "Sin respaldo en la fuente",
};

// ---- Case facts ------------------------------------------------------------

const FACT_LABEL: Record<string, string> = {
  residencia_fiscal: "Residencia fiscal",
  periodo_fiscal: "Ejercicio",
  tipo_renta: "Tipo de renta",
  menciona_ingresos: "Tiene ingresos",
  importe_renta_aproximado: "Ingresos anuales aproximados",
  categoria_contribuyente: "Situación laboral",
  regimen_tributario: "Régimen de tributación",
  volumen_facturacion: "Facturación anual",
  actividad_economica: "Actividad económica",
  plantilla: "Empleados",
  alta_actividad_economica: "De alta en actividad económica",
  fecha_inicio_actividad: "Inicio de la actividad",
  regimen_cotizacion: "Régimen de cotización",
  periodicidad_iva: "Periodicidad del IVA",
  tema_tributario: "Tema de la consulta",
};

// Enum values the backend stores as ASCII slugs.
const VALUE_LABEL: Record<string, string> = {
  actividad_economica: "Actividad económica",
  capital_mobiliario: "Capital mobiliario (intereses, dividendos)",
  capital_inmobiliario: "Capital inmobiliario (alquileres)",
  ganancias_patrimoniales: "Ganancias patrimoniales",
  autonomo: "Autónomo",
  estimacion_directa_normal: "Estimación directa normal",
  estimacion_directa_simplificada: "Estimación directa simplificada",
  estimacion_objetiva: "Estimación objetiva (módulos)",
  modulos: "Estimación objetiva (módulos)",
};

const MONEY_FACTS = new Set(["importe_renta_aproximado", "volumen_facturacion"]);
const EUR = new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });

/** "tipo_renta" → "Tipo de renta"; unknown names are de-snake-cased. */
export function factLabel(name: string): string {
  const known = FACT_LABEL[name];
  if (known) return known;
  const spaced = name.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function factValue(fact: Pick<Fact, "name" | "value">): string {
  const { name, value } = fact;
  if (typeof value === "boolean") return value ? "Sí" : "No";
  if (typeof value === "number") return MONEY_FACTS.has(name) ? EUR.format(value) : String(value);
  if (typeof value === "string") {
    if (name === "residencia_fiscal") return value === "ES" ? "España" : value === "OTHER" ? "Fuera de España" : value;
    const known = VALUE_LABEL[value];
    if (known) return known;
    const spaced = value.replace(/_/g, " ");
    return spaced.charAt(0).toUpperCase() + spaced.slice(1);
  }
  return JSON.stringify(value);
}

export type FactInput =
  | { kind: "choice"; options: Array<{ value: string | boolean; label: string }> }
  | { kind: "money" }
  | { kind: "number" }
  | { kind: "year" }
  | { kind: "text" };

const choice = (values: string[]): FactInput => ({
  kind: "choice",
  options: values.map((value) => ({ value, label: factValue({ name: "", value }) })),
});

const FACT_INPUT: Record<string, FactInput> = {
  residencia_fiscal: {
    kind: "choice",
    options: [
      { value: "ES", label: "España" },
      { value: "OTHER", label: "Fuera de España" },
    ],
  },
  periodo_fiscal: { kind: "year" },
  tipo_renta: choice(["trabajo", "actividad_economica", "capital_mobiliario", "capital_inmobiliario", "ganancias_patrimoniales", "otros"]),
  categoria_contribuyente: choice(["asalariado", "autonomo", "pensionista", "sociedad", "estudiante", "desempleado"]),
  regimen_tributario: choice(["estimacion_directa_normal", "estimacion_directa_simplificada", "estimacion_objetiva"]),
  periodicidad_iva: choice(["trimestral", "mensual"]),
  importe_renta_aproximado: { kind: "money" },
  volumen_facturacion: { kind: "money" },
  plantilla: { kind: "number" },
};

const YES_NO: FactInput = {
  kind: "choice",
  options: [
    { value: true, label: "Sí" },
    { value: false, label: "No" },
  ],
};

/** Which control edits a fact: a fixed list where the values are an enum,
 *  a number box for amounts, free text otherwise. */
export function factInput(fact: Pick<Fact, "name" | "value">): FactInput {
  if (typeof fact.value === "boolean") return YES_NO;
  const known = FACT_INPUT[fact.name];
  if (known) {
    // A stored value outside the list (e.g. a country code) must stay editable as text.
    if (known.kind === "choice" && !known.options.some((o) => o.value === fact.value)) return { kind: "text" };
    return known;
  }
  return typeof fact.value === "number" ? { kind: "number" } : { kind: "text" };
}

/** "Aún me falta: tipo de renta y ejercicio" from raw fact names. */
export function missingFactsSentence(missing: string[]): string {
  if (missing.length === 0) return "";
  const labels = missing.map((m) => factLabel(m).toLowerCase());
  const joined = labels.length === 1 ? labels[0] : `${labels.slice(0, -1).join(", ")} y ${labels[labels.length - 1]}`;
  return `Falta por saber: ${joined}.`;
}

// ---- Agent activity --------------------------------------------------------

const SOURCE_TYPE_LABEL: Record<string, string> = {
  normativa: "leyes y reglamentos",
  jurisprudencia: "jurisprudencia",
  doctrina_administrativa: "doctrina de la DGT y el TEAC",
  aeat: "manuales de la AEAT",
};

/** One persistent row of the "what the advisor did" timeline. */
export interface ActivityRow {
  kind: "search" | "case" | "verify" | "revise";
  text: string;
  detail?: string;
  outcome?: string;
  failed?: boolean;
}

function searchDetail(args: Record<string, unknown>): string | undefined {
  const parts: string[] = [];
  if (typeof args.tipo_fuente === "string") parts.push(SOURCE_TYPE_LABEL[args.tipo_fuente] ?? args.tipo_fuente);
  if (typeof args.tributo === "string") parts.push(args.tributo);
  if (typeof args.ambito === "string") parts.push(args.ambito.replace("autonomico:", "autonómico: "));
  if (args.solo_vinculante === true) parts.push("solo vinculante");
  if (typeof args.desde_fecha === "string") parts.push(`desde ${args.desde_fecha}`);
  return parts.length ? `Solo en ${parts.join(" · ")}` : undefined;
}

/** Fold raw progress events into human-readable rows (tool results attach to
 *  the call they answer; "thinking" is transient and produces no row). */
export function activityRows(events: ProgressEvent[]): ActivityRow[] {
  const rows: ActivityRow[] = [];
  for (const event of events) {
    if (event.type === "tool_call") {
      if (event.tool === "buscar_fuentes") {
        const query = typeof event.arguments.consulta === "string" ? event.arguments.consulta : "…";
        rows.push({ kind: "search", text: query, detail: searchDetail(event.arguments) });
      } else if (event.tool === "registrar_hechos") {
        const facts = Array.isArray(event.arguments.hechos) ? event.arguments.hechos : [];
        const names = facts
          .map((f) => (typeof f === "object" && f !== null && "nombre" in f ? factLabel(String(f.nombre)).toLowerCase() : ""))
          .filter(Boolean);
        rows.push({ kind: "case", text: `Expediente: anotado${names.length ? ` ${names.join(", ")}` : " un dato nuevo"}` });
      } else if (event.tool === "consultar_expediente") {
        rows.push({ kind: "case", text: "Expediente: repaso de lo que ya sé de tu situación" });
      } else if (event.tool === "evaluar_obligaciones") {
        rows.push({ kind: "case", text: "Obligaciones: comprobación de las que te aplican" });
      }
    } else if (event.type === "tool_result") {
      const last = rows[rows.length - 1];
      if (!last || last.outcome !== undefined) continue;
      if (event.is_error) {
        last.outcome = "no se pudo completar";
        last.failed = true;
      } else if (last.kind === "search") {
        const count = /^(\d+) fragmentos/.exec(event.summary);
        last.outcome = count ? (count[1] === "1" ? "1 fragmento" : `${count[1]} fragmentos`) : "sin resultados";
      } else {
        last.outcome = "hecho";
      }
    } else if (event.type === "verifying") {
      rows.push({
        kind: "verify",
        text:
          event.citations > 0
            ? `Verificación de cada afirmación contra ${event.citations === 1 ? "la fuente citada" : `las ${event.citations} fuentes citadas`}`
            : "Revisión de la respuesta",
      });
    } else if (event.type === "revising") {
      rows.push({
        kind: "revise",
        text:
          event.problems === 1
            ? "Corrección de una afirmación que no cuadraba con su fuente"
            : `Corrección de ${event.problems} afirmaciones que no cuadraban con sus fuentes`,
      });
    }
  }
  return rows;
}

/** The transient one-liner shown next to the spinner. */
export function statusLine(events: ProgressEvent[]): string {
  const last = events[events.length - 1];
  if (!last) return "Conectando con el asesor…";
  if (last.type === "text_delta") return "Redactando la respuesta…";
  if (last.type === "thinking") return last.step === 1 ? "Analizando tu consulta…" : "Leyendo lo encontrado y decidiendo el siguiente paso…";
  if (last.type === "tool_call") return last.tool === "buscar_fuentes" ? "Buscando en la normativa…" : "Actualizando tu expediente…";
  if (last.type === "tool_result") return "Leyendo lo encontrado…";
  if (last.type === "verifying") return "Verificando la respuesta contra las fuentes…";
  return "Corrigiendo la respuesta…";
}

// ---- Sources & verification ------------------------------------------------

/** DOM id of a source card, the jump target of the [F#] chips of one turn. */
export function sourceAnchorId(scope: string, fragmentId: string): string {
  return `src-${scope}-${fragmentId}`;
}

/** "4 de 5 afirmaciones comprobadas" for the verdict banner. */
export function verificationSummary(claims: ClaimCheck[]): string | null {
  if (claims.length === 0) return null;
  const ok = claims.filter((c) => c.verdict === "respaldada").length;
  if (ok === claims.length) {
    return claims.length === 1 ? "1 afirmación comprobada" : `${claims.length} afirmaciones comprobadas`;
  }
  return `${ok} de ${claims.length} afirmaciones comprobadas`;
}

