// The structured facts the optimiser screen keeps in the case (JSON text in the fact store, like
// `descendientes`): `datos_conyuge` — the spouse's own figures — and `preferencias_optimizacion` —
// what the user can and will do before 31 December. Reading them back and the one-line summaries
// «Tu expediente» shows. A leaf module: presentation.ts and the optimiser screen both import it.
//
// The server validates and canonicalises both (hacienda_gpt/tax/profile.py `DatosConyuge`,
// hacienda_gpt/tax/optimizador.py `PreferenciasOptimizacion`): amounts come back as decimal
// strings ("12000.5"), defaults are left out.

export const DATOS_CONYUGE = "datos_conyuge";
export const PREFERENCIAS_OPTIMIZACION = "preferencias_optimizacion";

/** Facts edited in their own screen (simulator, optimiser), never with the generic pencil. */
export const STRUCTURED_FACTS: ReadonlySet<string> = new Set(["descendientes", DATOS_CONYUGE, PREFERENCIAS_OPTIMIZACION]);

export interface StoredPosition {
  id?: string;
  descripcion?: string;
  importe: string | number;
  tipo?: string;
  titular?: string;
}

export interface StoredPreferences {
  aportacion_maxima_pensiones?: string | number;
  aportacion_maxima_pensiones_conyuge?: string | number;
  acepta_iliquidez?: boolean;
  donativo_previsto?: string | number;
  presupuesto_donativos?: string | number;
  donativo_recurrente_importe_anterior?: string | number;
  perdidas_latentes?: StoredPosition[];
  ganancias_latentes?: StoredPosition[];
  tributacion_prevista?: "individual" | "conjunta_biparental";
}

/** The JSON object a structured fact holds, or null when it is absent or cannot be read. */
export function parseStoredObject(value: unknown): Record<string, unknown> | null {
  let parsed: unknown = value;
  if (typeof value === "string") {
    try {
      parsed = JSON.parse(value);
    } catch {
      return null;
    }
  }
  return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : null;
}

/** "12000.5" or 12000.5 → 12000.5; anything else → null. */
export function storedNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value !== "string" || !value.trim()) return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** The positions of a stored list, those that can be read. */
export function storedPositions(value: unknown): StoredPosition[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (p): p is StoredPosition => typeof p === "object" && p !== null && storedNumber((p as Record<string, unknown>).importe) !== null,
  );
}

/** 1500 → "1.500 €", 1234.5 → "1.234,50 €": thousands always grouped, cents only when there are any. */
function euros(n: number): string {
  const totalCents = Math.round(Math.abs(n) * 100);
  const sign = n < 0 && totalCents > 0 ? "-" : "";
  const whole = Math.floor(totalCents / 100);
  const cents = totalCents % 100;
  const grouped = String(whole).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  return `${sign}${grouped}${cents ? `,${String(cents).padStart(2, "0")}` : ""}\u00a0€`;
}

const UNREADABLE = "No se puede leer · vuelve a guardarlo desde el optimizador";
const EDITED_IN = "se edita en el optimizador";

// What each stored figure of the spouse reads like, in the order of the server's model.
const SPOUSE_PHRASES: Array<[string, (v: unknown) => string | null]> = [
  ["rendimiento_trabajo_bruto", (v) => money(v, "de salario")],
  ["cotizaciones_seguridad_social", (v) => money(v, "de cotizaciones")],
  ["otros_gastos_deducibles_trabajo", (v) => money(v, "de otros gastos del trabajo")],
  ["retenciones", (v) => money(v, "ya retenidos")],
  ["rendimiento_actividad_neto", (v) => money(v, "de su actividad")],
  ["rendimiento_alquiler_neto", (v) => money(v, "de alquileres")],
  ["rendimiento_capital_mobiliario", (v) => money(v, "de intereses y dividendos")],
  ["ganancias_patrimoniales", (v) => money(v, "de ganancias por ventas")],
  ["rendimientos_negativos_pendientes", (v) => money(v, "de rendimientos negativos pendientes")],
  ["perdidas_patrimoniales_pendientes", (v) => money(v, "de pérdidas pendientes")],
  ["aportacion_plan_pensiones", (v) => money(v, "ya aportados a su plan")],
  ["contribucion_empresa_plan_pensiones", (v) => money(v, "aportados por su empresa")],
  ["donativos", (v) => money(v, "ya donados")],
  ["edad", (v) => (storedNumber(v) === null ? null : `${storedNumber(v)} años`)],
  ["grado_discapacidad", (v) => (storedNumber(v) === null ? null : `discapacidad del ${storedNumber(v)}\u00a0%`)],
  ["necesita_asistencia", (v) => (v === true ? "necesita ayuda de terceros" : null)],
];

function money(value: unknown, what: string): string | null {
  const n = storedNumber(value);
  return n === null ? null : `${euros(n)} ${what}`;
}

/** «12.000 € de salario, 900 € ya retenidos, 44 años · se edita en el optimizador». */
export function spouseSummary(value: unknown): string {
  const stored = parseStoredObject(value);
  if (!stored) return UNREADABLE;
  const parts = SPOUSE_PHRASES.filter(([name]) => name in stored)
    .map(([name, phrase]) => phrase(stored[name]))
    .filter((p): p is string => p !== null);
  return parts.length ? `${parts.join(", ")} · ${EDITED_IN}` : `Sin cifras · ${EDITED_IN}`;
}

function positionsPhrase(value: unknown, singular: string, plural: string): string | null {
  const list = storedPositions(value);
  if (!list.length) return null;
  const total = list.reduce((sum, p) => sum + (storedNumber(p.importe) ?? 0), 0);
  return `${list.length} ${list.length === 1 ? singular : plural} (${euros(total)})`;
}

/** «hasta 1.500 € a tu plan de pensiones, aceptas inmovilizar el dinero, … · se edita en el optimizador». */
export function preferencesSummary(value: unknown): string {
  const stored = parseStoredObject(value) as StoredPreferences | null;
  if (!stored) return UNREADABLE;
  const parts: string[] = [];
  const limit = (raw: unknown, some: string, none: string) => {
    const n = storedNumber(raw);
    if (n !== null) parts.push(n > 0 ? some.replace("{}", euros(n)) : none);
  };
  limit(stored.aportacion_maxima_pensiones, "hasta {} a tu plan de pensiones", "nada más a tu plan de pensiones");
  limit(stored.aportacion_maxima_pensiones_conyuge, "hasta {} al plan de tu cónyuge", "nada más al plan de tu cónyuge");
  if (stored.acepta_iliquidez === true) parts.push("aceptas inmovilizar el dinero");
  if (stored.acepta_iliquidez === false) parts.push("sin inmovilizar dinero");
  limit(stored.donativo_previsto, "donativo previsto de {}", "sin donativo previsto");
  limit(stored.presupuesto_donativos, "hasta {} en donativos", "nada en donativos");
  limit(stored.donativo_recurrente_importe_anterior, "{} donados el año pasado a la misma entidad", "");
  const perdidas = positionsPhrase(stored.perdidas_latentes, "pérdida latente", "pérdidas latentes");
  if (perdidas) parts.push(perdidas);
  const ganancias = positionsPhrase(stored.ganancias_latentes, "ganancia latente", "ganancias latentes");
  if (ganancias) parts.push(ganancias);
  if (stored.tributacion_prevista === "conjunta_biparental") parts.push("pensabais declarar en conjunta");
  if (stored.tributacion_prevista === "individual") parts.push("pensabais declarar por separado");
  const said = parts.filter(Boolean);
  return said.length ? `${said.join(", ")} · ${EDITED_IN}` : `Sin preferencias · ${EDITED_IN}`;
}
