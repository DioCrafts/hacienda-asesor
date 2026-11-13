// Pure helpers of the «Optimizar mis impuestos» screen: labels, error wording,
// and how a plan maps onto the simulator's figures. No React here, so the
// component file only exports components.

import type { CausaNoDisponible, Naturaleza, PalancaElegida, PlanOptimo, ResultadoHogar, TipoActivo } from "../apiOptimizador";
import { factLabel } from "./presentation";

/** "1.500,50", "1500.5", "1.500 €" → 1500.5; empty or unreadable → null. */
export function parseAmount(raw: string | undefined): number | null {
  const cleaned = (raw ?? "").trim().replace(/[€\s]/g, "").replace(/\.(?=\d{3}(\D|$))/g, "").replace(",", ".");
  if (!cleaned || cleaned === "-") return null;
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : null;
}

/** 1500.5 → "1500,5": how the simulator's boxes show a figure. */
export function amountText(n: number): string {
  return String(Math.round(n * 100) / 100).replace(".", ",");
}

/** "1082.46" → "1.082,46 €": always with the thousands point, like the plan's own sentences. */
export function eur(value: string | number): string {
  const n = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(n)) return String(value);
  const [whole, cents] = Math.abs(n).toFixed(2).split(".");
  const sign = n < 0 && Math.abs(n) >= 0.005 ? "-" : "";
  return `${sign}${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ".")},${cents}\u00a0€`;
}

/** "A", "A y B", "A, B y C". */
export function joinList(items: string[]): string {
  return items.length <= 1 ? (items[0] ?? "") : `${items.slice(0, -1).join(", ")} y ${items[items.length - 1]}`;
}

/** Today's date in Spain (the server's default for the plan), as AAAA-MM-DD. */
export function todayInSpain(): string {
  try {
    return new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Madrid", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  } catch {
    return new Date().toISOString().slice(0, 10);
  }
}

export const NATURALEZA: Record<Naturaleza, { label: string; cls: string }> = {
  ahorro_definitivo: { label: "Ahorro definitivo", cls: "border-grounding-cited-bd bg-grounding-cited-bg text-grounding-cited-fg" },
  diferimiento: { label: "Diferimiento", cls: "border-grounding-uncited-bd bg-grounding-uncited-bg text-grounding-uncited-fg" },
  requiere_gastar: { label: "Requiere gastar", cls: "border-grounding-abstain-bd bg-grounding-abstain-bg text-grounding-abstain-fg" },
};

/** What the nature of a lever means for the user, in words (no figures: those come from the plan). */
export function naturalezaTexto(p: Pick<PalancaElegida, "id" | "naturaleza">): string {
  if (p.naturaleza === "ahorro_definitivo") return "Es un ahorro definitivo: no lo pagarás más adelante.";
  if (p.naturaleza === "requiere_gastar") return "Exige gastar dinero: el ahorro solo compensa una parte de lo que das.";
  if (p.id.startsWith("plan_pensiones")) {
    return "No es un ahorro definitivo, es un diferimiento: baja el impuesto de este año, pero lo que rescates del plan tributará entonces como rendimiento del trabajo.";
  }
  if (p.id.startsWith("perdidas")) {
    return "No es un ahorro definitivo, es un diferimiento: usas ahora una pérdida que ya tenías; si vuelves a invertir, tu ganancia futura será mayor.";
  }
  return "No es un ahorro definitivo, es un diferimiento: pagarás este impuesto más adelante.";
}

/** What the amount committed by a lever is, by kind of lever. */
export function compromisoLabel(p: Pick<PalancaElegida, "id">): string {
  if (p.id.startsWith("plan_pensiones")) return "Dinero que inmovilizas";
  if (p.id.startsWith("perdidas")) return "Pérdida que realizas";
  if (p.id === "donativo") return "Dinero que donas";
  return "Importe comprometido";
}

export const CAUSA_LABEL: Record<CausaNoDisponible, string> = {
  fecha: "Por la fecha",
  restricciones: "Por lo que has indicado",
  faltan_datos: "Faltan datos",
  sin_margen: "No queda margen",
  no_calculable: "No se puede calcular",
};

export const TIPO_ACTIVO: Array<{ value: TipoActivo; label: string }> = [
  { value: "valores_cotizados", label: "Sí (acciones, fondos, ETF)" },
  { value: "valores_no_cotizados", label: "No, son valores no cotizados" },
  { value: "otro_elemento", label: "No es un valor (un inmueble…)" },
];

/**
 * The repurchase rule that keeps a realised loss from counting yet, by kind of asset. Worded after
 * the literal quotes of art. 33.5 e), f) and g) LIRPF that the optimiser itself cites in its plan.
 */
export function reglaRecompra(tipo: TipoActivo): string {
  if (tipo === "valores_cotizados") {
    return "Regla de los dos meses: si compras valores homogéneos en los dos meses anteriores o posteriores a la venta, la pérdida no cuenta todavía; se integra cuando vendas esos valores (art. 33.5.f LIRPF).";
  }
  if (tipo === "valores_no_cotizados") {
    return "Si compras valores homogéneos en el año anterior o posterior a la venta, la pérdida no cuenta todavía (art. 33.5.g LIRPF).";
  }
  return "Si vuelves a comprar el mismo bien dentro del año siguiente a la venta, la pérdida no cuenta hasta que lo vendas de nuevo (art. 33.5.e LIRPF).";
}

const MISSING_LABEL: Record<string, string> = {
  conyuge: "Cifras de tu cónyuge",
  perdidas_latentes: "Inversiones con pérdidas latentes",
  estado_civil: "Estado civil",
  retenciones: "IRPF ya retenido",
  rendimiento_trabajo_bruto: "Salario bruto anual",
};

export function missingLabel(name: string): string {
  return MISSING_LABEL[name] ?? factLabel(name);
}

/** "1.791,79 € a pagar" / "120,00 € a devolver" from a result (positive: to pay). */
export function resultadoTexto(amount: string | number): string {
  const n = Number(amount);
  if (!Number.isFinite(n) || n === 0) return `${eur(0)} (ni a pagar ni a devolver)`;
  return `${eur(Math.abs(n))} ${n > 0 ? "a pagar" : "a devolver"}`;
}

export function tributacionTexto(hogar: Pick<ResultadoHogar, "tributacion" | "declaraciones">): string {
  if (hogar.tributacion === "conjunta_biparental") return "declaración conjunta";
  if (hogar.tributacion === "conjunta_monoparental") return "declaración conjunta monoparental";
  return hogar.declaraciones.length > 1 ? "declaraciones individuales" : "declaración individual";
}

export function titularTexto(titular: string): string {
  if (titular === "conyuge") return "Tu cónyuge";
  if (titular === "conjunta") return "Declaración conjunta";
  if (titular === "hogar") return "El hogar";
  return "Tú";
}

/** The consolidated text of a BOE norm, for the legal quotes the plan carries. */
export function boeUrl(boeId: string): string | null {
  return /^BOE-[A-Z]-\d{4}-\d+$/.test(boeId) ? `https://www.boe.es/buscar/act.php?id=${boeId}` : null;
}

/** Split a text on «…» so literal quotes of the law can be set apart. */
export function splitQuotes(text: string): Array<{ text: string; quote: boolean }> {
  const out: Array<{ text: string; quote: boolean }> = [];
  const re = /«([^»]*)»/g;
  let last = 0;
  for (let m = re.exec(text); m !== null; m = re.exec(text)) {
    if (m.index > last) out.push({ text: text.slice(last, m.index), quote: false });
    out.push({ text: m[1], quote: true });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last), quote: false });
  return out;
}

// ---- 422 messages ------------------------------------------------------------

const RESTRICCION_LABEL: Record<string, string> = {
  aportacion_maxima_pensiones: "Dinero disponible para tu plan de pensiones",
  aportacion_maxima_pensiones_conyuge: "Dinero disponible para el plan de tu cónyuge",
  acepta_iliquidez: "Inmovilizar el dinero hasta la jubilación",
  donativo_previsto: "Donativo que vas a hacer",
  presupuesto_donativos: "Presupuesto para donativos",
  donativo_recurrente_importe_anterior: "Donativo del año pasado a la misma entidad",
  perdidas_latentes: "Inversiones con pérdidas",
  ganancias_latentes: "Inversiones con ganancias",
};
const POSICION_CAMPO: Record<string, string> = { importe: "importe", descripcion: "descripción", tipo: "tipo de bien", titular: "titular" };
const PLAIN_FIELDS = new Set(["edad", "fecha", "ejercicio", "retenciones", "donativos"]);

function pathLabel(path: string): string | null {
  const parts = path.split(".");
  if (parts[0] === "restricciones" && parts.length > 1) {
    const name = parts[1];
    const label = RESTRICCION_LABEL[name];
    if (!label) return null;
    if (parts.length >= 4 && /^\d+$/.test(parts[2])) {
      const kind = name === "ganancias_latentes" ? "Inversión con ganancias" : "Inversión con pérdidas";
      return `${kind} n.º ${Number(parts[2]) + 1} (${POSICION_CAMPO[parts[3]] ?? parts[3]})`;
    }
    return label;
  }
  if (parts[0] === "descendientes" && parts.length === 3) return `Detalle de los hijos, ${parts[1]} (${factLabel(parts[2]).toLowerCase()})`;
  if (parts.length === 1 && (path.includes("_") || PLAIN_FIELDS.has(path))) {
    if (path === "fecha") return "Fecha del plan";
    return factLabel(path);
  }
  return null;
}

/**
 * A 422 from /optimize as a sentence for the user: each «campo: problema» of the validator gets the
 * label this screen uses, and the validator's English asides are dropped.
 */
export function humanizeOptimizeError(message: string, status: number | null): string {
  if (status === 404) return "No se encuentra tu expediente en tu cuenta: abre una consulta nueva o calcula con tus cifras sin expediente.";
  const clean = message.replace(/\s*\((?:Input|String|Value|Field|Decimal|Extra|Unable)[^)]*\)/g, "").trim();
  const match = /^Datos no válidos(?: para el cálculo)?:\s*([\s\S]*?)\.?$/.exec(clean);
  if (!match) return clean;
  const parts = match[1].split("; ").map((part) => {
    const at = part.indexOf(": ");
    if (at < 0) return part;
    const path = part.slice(0, at);
    const problem = part.slice(at + 2);
    if (path === "restricciones") return problem;
    const label = pathLabel(path);
    return label ? `${label}: ${problem}` : part;
  });
  // Bounds come as bare digits ("100000000"): written with thousands points.
  const readable = parts.map((part) => part.replace(/\b\d{5,}\b/g, (digits) => digits.replace(/\B(?=(\d{3})+(?!\d))/g, ".")));
  return `Revisa estos datos: ${readable.join("; ")}.`;
}

// ---- The plan in the simulator ------------------------------------------------

const PARTES_DONATIVOS_AUTONOMICOS = new Set([
  "donativos_medio_ambiente",
  "donativos_cultura_patrimonio",
  "donativos_investigacion",
  "donativos_cooperacion_fines_sociales",
  "donativos_lengua_propia",
  "donativos_fundaciones_autonomicas",
]);

export type PlanEnSimulador =
  | { ok: true; cambios: Record<string, number>; tributacion: string | null }
  | { ok: false; motivo: string };

/**
 * The plan's changes as the simulator's figures (one return): the pension contribution and the
 * donation added to what is already there, the realised loss taken off the year's gains.
 * `figure` returns the figure the plan started from (0 when not given).
 */
export function planEnSimulador(plan: PlanOptimo, figure: (name: string) => number): PlanEnSimulador {
  if (plan.con_conyuge) {
    return { ok: false, motivo: "El simulador calcula una sola declaración: el plan de un matrimonio, con las cifras de los dos, no se puede trasladar tal cual." };
  }
  const cambios: Record<string, number> = {};
  const add = (name: string, delta: number) => {
    cambios[name] = Math.round(((cambios[name] ?? figure(name)) + delta) * 100) / 100;
  };
  let tributacion: string | null = null;
  for (const p of plan.palancas) {
    const importe = Number(p.importe ?? 0);
    if (p.id === "tributacion") tributacion = p.opcion;
    else if (p.id === "plan_pensiones_contribuyente") add("aportacion_plan_pensiones", importe);
    else if (p.id === "perdidas_contribuyente") add("ganancias_patrimoniales", -importe);
    else if (p.id === "donativo") {
      if (p.opcion === "ley_49_2002_recurrente") {
        return { ok: false, motivo: "El simulador no distingue el donativo recurrente a la misma entidad: el plan no se puede trasladar tal cual." };
      }
      add("donativos", importe);
      if (p.opcion?.startsWith("autonomica:")) {
        const campo = p.opcion.slice("autonomica:".length);
        add(campo, importe);
        if (PARTES_DONATIVOS_AUTONOMICOS.has(campo) && figure("donativos_autonomicos") > 0) add("donativos_autonomicos", importe);
      }
    } else {
      return { ok: false, motivo: `«${p.nombre}» no tiene equivalente en el simulador.` };
    }
  }
  return { ok: true, cambios, tributacion };
}

/** First letter in lower case for use mid-sentence, unless the word is an acronym («IRPF», «IVA»). */
export function enMinuscula(texto: string): string {
  const primera = texto.split(/\s/, 1)[0] ?? "";
  if (primera.length > 1 && primera === primera.toUpperCase() && /[A-ZÁÉÍÓÚÑ]/.test(primera)) return texto;
  return texto.charAt(0).toLowerCase() + texto.slice(1);
}
