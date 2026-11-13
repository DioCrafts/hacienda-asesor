// «Optimizar mis impuestos»: the best COMBINATION of the choices the law leaves to a household
// (filing jointly or not, pension contributions, the donation and the latent losses), from the
// deterministic optimiser behind POST /optimize — no model call. Every figure on this screen is
// the plan's; the screen never adds standalone savings up, and says which savings are deferrals.
//
// Two tabs: «Tus datos» (figures — from the case or typed —, the spouse's figures and the user's
// constraints) and «Tu plan». A plan computed from other inputs than the ones on screen is marked
// as outdated until it is recalculated.
//
// Everything on «Tus datos» starts from the case: the user's own facts, the spouse's figures
// (fact `datos_conyuge`) and the constraints (fact `preferencias_optimizacion`); what the user types
// is layered over them until «Guardar en mi expediente» writes it. The request always carries the
// screen's spouse figures and constraints in full, so the server's stored defaults never add
// something the screen does not show.

import {
  ArrowRight,
  BookOpen,
  Check,
  ChevronDown,
  CircleAlert,
  ExternalLink,
  Info,
  Loader2,
  PiggyBank,
  Plus,
  RotateCcw,
  Save,
  Trash2,
  TriangleAlert,
  X,
} from "lucide-react";
import clsx from "clsx";
import { type FormEvent, type ReactNode, useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";

import type { Fact } from "../api";
import { ApiError } from "../api";
import type { Declaracion, OptimizeRequest, PalancaElegida, PalancaNoDisponible, PlanOptimo, PosicionLatente, Restricciones, ResultadoHogar, TipoActivo, Titular } from "../apiOptimizador";
import { optimizar } from "../apiOptimizador";
import { DATOS_CONYUGE, PREFERENCIAS_OPTIMIZACION, type StoredPosition, type StoredPreferences, parseStoredObject, storedNumber, storedPositions } from "../lib/expediente";
import { newId } from "../lib/id";
import {
  CAUSA_LABEL,
  NATURALEZA,
  TIPO_ACTIVO,
  amountText,
  boeUrl,
  compromisoLabel,
  enMinuscula,
  eur,
  humanizeOptimizeError,
  joinList,
  missingLabel,
  naturalezaTexto,
  parseAmount,
  planEnSimulador,
  reglaRecompra,
  resultadoTexto,
  splitQuotes,
  titularTexto,
  todayInSpain,
  tributacionTexto,
} from "../lib/optimizador";
import { COMUNIDADES, comunidadDeExpediente, factLabel, formatDate } from "../lib/presentation";
import type { SimulatorSeed } from "./SimulatorPanel";

// ---- The form -------------------------------------------------------------------

type Kind = "money" | "signed" | "count" | "age" | "percent" | "flag";
interface FieldDef {
  name: string;
  label: string;
  kind: Kind;
  hint?: string;
}

const INGRESOS: FieldDef[] = [
  { name: "rendimiento_trabajo_bruto", label: "Salario bruto anual", kind: "money" },
  { name: "cotizaciones_seguridad_social", label: "Cotizaciones a la Seguridad Social", kind: "money", hint: "Lo que te descuentan en nómina por Seguridad Social" },
  { name: "retenciones", label: "IRPF ya retenido", kind: "money", hint: "Lo retenido en nómina en el año: decide si te sale a pagar o a devolver" },
  { name: "rendimiento_actividad_neto", label: "Beneficio neto de tu actividad", kind: "signed", hint: "Si eres autónomo (con signo menos si hay pérdidas)" },
  { name: "rendimiento_capital_mobiliario", label: "Intereses y dividendos", kind: "signed" },
  { name: "ganancias_patrimoniales", label: "Ganancias por ventas ya hechas este año", kind: "signed", hint: "Fondos, acciones, inmuebles… ya vendidos. Con signo menos si perdiste" },
];
const HECHO: FieldDef[] = [
  { name: "aportacion_plan_pensiones", label: "Ya aportado a tu plan este año", kind: "money", hint: "Planes de pensiones y otros sistemas de previsión social" },
  { name: "donativos", label: "Ya donado este año", kind: "money", hint: "A entidades acogidas a la Ley 49/2002" },
  { name: "perdidas_patrimoniales_pendientes", label: "Pérdidas de años anteriores sin compensar", kind: "money", hint: "Las que te quedaron pendientes en declaraciones anteriores" },
];
const FAMILIA: FieldDef[] = [
  { name: "edad", label: "Tu edad", kind: "age" },
  { name: "num_descendientes", label: "Hijos a cargo", kind: "count" },
  { name: "num_descendientes_menores_3", label: "De ellos, menores de 3 años", kind: "count" },
];
const MONOPARENTAL: FieldDef = {
  name: "familia_monoparental",
  label: "Familia monoparental: vivo solo o sola con mis hijos",
  kind: "flag",
  hint: "Así se compara también la declaración conjunta con tus hijos.",
};
// Other figures that can move a deduction (offered under «Más datos»; the simulator has them all).
const EXTRA: FieldDef[] = [
  { name: "gastos_guarderia", label: factLabel("gastos_guarderia"), kind: "money" },
  { name: "num_hijos_primer_ciclo_infantil", label: factLabel("num_hijos_primer_ciclo_infantil"), kind: "count" },
  { name: "hijos_nacidos_en_el_ejercicio", label: factLabel("hijos_nacidos_en_el_ejercicio"), kind: "count" },
  { name: "hijos_nacidos_ultimos_3_ejercicios", label: factLabel("hijos_nacidos_ultimos_3_ejercicios"), kind: "count" },
  { name: "gastos_escolaridad", label: factLabel("gastos_escolaridad"), kind: "money" },
  { name: "gastos_libros_texto", label: factLabel("gastos_libros_texto"), kind: "money" },
  { name: "gastos_ensenanza_idiomas", label: factLabel("gastos_ensenanza_idiomas"), kind: "money" },
  { name: "cotizaciones_empleada_hogar", label: factLabel("cotizaciones_empleada_hogar"), kind: "money" },
  { name: "gastos_cuidado_familiares", label: factLabel("gastos_cuidado_familiares"), kind: "money" },
  { name: "alquiler_vivienda_habitual", label: factLabel("alquiler_vivienda_habitual"), kind: "money" },
  { name: "grado_discapacidad", label: factLabel("grado_discapacidad"), kind: "percent" },
  { name: "necesita_asistencia", label: "Necesito ayuda de terceras personas o tengo movilidad reducida", kind: "flag", hint: "Con una discapacidad del 33 % al 64 %, suma los gastos de asistencia al mínimo" },
  { name: "num_descendientes_discapacidad", label: "Hijos con discapacidad del 33 % o más", kind: "count", hint: "Cuentan a cualquier edad si viven contigo" },
  { name: "num_descendientes_discapacidad_65", label: "De ellos, con discapacidad del 65 % o más", kind: "count" },
  { name: "num_descendientes_asistencia", label: "Hijos del 33 % al 64 % que necesitan ayuda de terceros", kind: "count", hint: "O que tienen movilidad reducida (desde el 65 % ya se suma)" },
  { name: "num_ascendientes_mayores_65", label: "Padres o abuelos mayores de 65 que viven contigo", kind: "count", hint: "Al menos medio año y sin rentas de más de 8.000 €. Los de tu cónyuge, en su declaración." },
  { name: "num_ascendientes_mayores_75", label: "De ellos, mayores de 75", kind: "count" },
  { name: "num_ascendientes_discapacidad", label: "Padres o abuelos con discapacidad del 33 % o más", kind: "count", hint: "Que viven contigo, tengan la edad que tengan" },
  { name: "num_ascendientes_discapacidad_hasta_65", label: "De ellos, de 65 años o menos", kind: "count", hint: "También dan derecho al mínimo por ascendientes" },
  { name: "num_ascendientes_discapacidad_65", label: "De ellos, con discapacidad del 65 % o más", kind: "count" },
  { name: "num_ascendientes_asistencia", label: "Padres o abuelos del 33 % al 64 % que necesitan ayuda de terceros", kind: "count", hint: "O que tienen movilidad reducida" },
  { name: "num_declaraciones_mismos_ascendientes", label: "Declaraciones que aplican el mínimo por ellos", kind: "count", hint: "Contando la tuya: 2 si un hermano también vive con ellos; se reparte" },
  { name: "rendimiento_alquiler_neto", label: factLabel("rendimiento_alquiler_neto"), kind: "signed" },
  { name: "rendimientos_negativos_pendientes", label: factLabel("rendimientos_negativos_pendientes"), kind: "money" },
  { name: "contribucion_empresa_plan_pensiones", label: factLabel("contribucion_empresa_plan_pensiones"), kind: "money" },
  { name: "familia_numerosa", label: factLabel("familia_numerosa"), kind: "flag" },
];
const SPOUSE: FieldDef[] = [
  { name: "rendimiento_trabajo_bruto", label: "Su salario bruto anual", kind: "money" },
  { name: "cotizaciones_seguridad_social", label: "Sus cotizaciones a la Seguridad Social", kind: "money" },
  { name: "retenciones", label: "Su IRPF ya retenido", kind: "money" },
  { name: "rendimiento_actividad_neto", label: "Beneficio neto de su actividad", kind: "signed" },
  { name: "rendimiento_capital_mobiliario", label: "Sus intereses y dividendos", kind: "signed" },
  { name: "ganancias_patrimoniales", label: "Sus ganancias por ventas de este año", kind: "signed" },
  { name: "aportacion_plan_pensiones", label: "Ya aportado a su plan este año", kind: "money" },
  { name: "edad", label: "Su edad", kind: "age" },
  { name: "grado_discapacidad", label: "Su grado de discapacidad", kind: "percent" },
  { name: "necesita_asistencia", label: "Necesita ayuda de terceras personas o tiene movilidad reducida", kind: "flag", hint: "Con una discapacidad del 33 % al 64 %, suma los gastos de asistencia a su mínimo" },
];
const USER_FIELDS = [...INGRESOS, ...HECHO, ...FAMILIA, MONOPARENTAL, ...EXTRA];
const FIELD_BY_NAME = new Map(USER_FIELDS.map((f) => [f.name, f]));
const EXTRA_NAMES = new Set(EXTRA.map((f) => f.name));
// Counts the per-child detail of the case already gives: never typed over it (the calculator would refuse them).
const COUNT_NAMES = new Set([
  "num_descendientes",
  "num_descendientes_menores_3",
  "num_descendientes_discapacidad",
  "num_descendientes_discapacidad_65",
  "num_descendientes_asistencia",
]);

const ESTADOS = [
  { value: "", label: "Sin indicar" },
  { value: "soltero", label: "Soltero o soltera" },
  { value: "casado", label: "Casado o casada" },
  { value: "pareja_de_hecho", label: "Pareja de hecho" },
  { value: "separado", label: "Separado o separada" },
  { value: "divorciado", label: "Divorciado o divorciada" },
  { value: "viudo", label: "Viudo o viuda" },
];

function estadoDeExpediente(value: unknown): string {
  if (typeof value !== "string") return "";
  const saved = value.trim().toLowerCase();
  return ESTADOS.find((e) => e.value && saved.startsWith(e.value.slice(0, 4)))?.value ?? "";
}

/** Children in the case's per-child detail (the simulator edits it), or null when there is none. */
function childrenInCase(facts: Fact[]): number | null {
  const fact = facts.find((f) => f.name === "descendientes");
  if (!fact) return null;
  try {
    const list: unknown = typeof fact.value === "string" ? JSON.parse(fact.value) : fact.value;
    return Array.isArray(list) ? list.length : null;
  } catch {
    return null;
  }
}

/** What is wrong with a typed figure, or null. */
function fieldProblem(kind: Kind, raw: string | undefined): string | null {
  if (kind === "flag" || !raw || !raw.trim()) return null;
  const n = parseAmount(raw);
  if (n === null) return "Escribe una cifra, por ejemplo 1.500,50.";
  if (kind === "money" && n < 0) return "No puede ser negativo.";
  if (kind === "count" && (!Number.isInteger(n) || n < 0 || n > 20)) return "Un número entero de 0 a 20.";
  if (kind === "age" && (!Number.isInteger(n) || n < 0 || n > 130)) return "Una edad en años, de 0 a 130.";
  if (kind === "percent" && (!Number.isInteger(n) || n < 0 || n > 100)) return "Un porcentaje entero de 0 a 100.";
  return null;
}

interface Position {
  id: string;
  clase: "perdida" | "ganancia";
  descripcion: string;
  importe: string;
  tipo: TipoActivo;
  titular: Titular;
}

type Iliquidez = "" | "si" | "no";
type Tab = "datos" | "plan";
type TributacionPrevista = "individual" | "conjunta_biparental";

// The money constraints of the form (fields of `preferencias_optimizacion` too).
const LIMIT_NAMES = [
  "aportacion_maxima_pensiones",
  "aportacion_maxima_pensiones_conyuge",
  "donativo_previsto",
  "presupuesto_donativos",
  "donativo_recurrente_importe_anterior",
] as const;
type LimitName = (typeof LIMIT_NAMES)[number];
// The case's own facts the plan does NOT depend on through `perfil`: the screen sends their content in full.
const PLAN_INPUT_FACTS = new Set([DATOS_CONYUGE, PREFERENCIAS_OPTIMIZACION]);
const TIPOS = new Set<string>(TIPO_ACTIVO.map((t) => t.value));

/** A stored latent position as a row of the form (rows stored without an id get a stable one). */
function storedRow(p: StoredPosition, clase: Position["clase"], index: number): Position {
  return {
    id: p.id ?? `guardada-${clase}-${index + 1}`,
    clase,
    descripcion: p.descripcion ?? "",
    importe: amountText(storedNumber(p.importe) ?? 0),
    tipo: p.tipo && TIPOS.has(p.tipo) ? (p.tipo as TipoActivo) : "valores_cotizados",
    titular: p.titular === "conyuge" ? "conyuge" : "contribuyente",
  };
}

/** A typed box as what it states ("" = nothing), so «1.500» and «1500,00» are the same figure. */
function stated(kind: Kind, raw: string | undefined): string {
  if (kind === "flag") return raw === "true" ? "true" : "";
  const n = parseAmount(raw);
  return n === null ? "" : String(n);
}

/** The rows as what would be stored (same order, trimmed description, parsed amount). */
function rowsKey(rows: Position[]): string {
  return JSON.stringify(rows.map((p) => [p.id, p.clase, p.descripcion.trim(), parseAmount(p.importe), p.tipo, p.titular]));
}

interface Computed {
  plan: PlanOptimo;
  key: string;
  /** The figures the plan was computed from (to carry it over to the simulator). */
  datos: Record<string, unknown>;
  comunidad: string | null;
}

// ---- Small presentational pieces --------------------------------------------------------

const INPUT = "w-full rounded-lg border bg-surface px-3 py-2 text-[16px] text-ink focus:outline-none sm:text-[14px]";

function Field({
  id,
  def,
  value,
  onChange,
  problem,
  dirty,
  readOnlyNote,
}: {
  id: string;
  def: FieldDef;
  value: string;
  onChange: (value: string) => void;
  problem?: string | null;
  dirty?: boolean;
  readOnlyNote?: string;
}) {
  const describedBy = [def.hint ? `${id}-hint` : null, problem ? `${id}-problem` : null].filter(Boolean).join(" ") || undefined;
  if (def.kind === "flag") {
    return (
      <label className="flex items-start gap-2.5 sm:col-span-2">
        <input
          id={id}
          type="checkbox"
          checked={value === "true"}
          onChange={(e) => onChange(String(e.target.checked))}
          aria-describedby={describedBy}
          className="mt-0.5 h-4 w-4 flex-shrink-0 accent-[rgb(var(--primary))]"
        />
        <span>
          <span className={clsx("text-[13.5px]", dirty ? "font-semibold text-primary-dark" : "text-ink")}>{def.label}</span>
          {def.hint && (
            <span id={`${id}-hint`} className="block text-[11.5px] leading-snug text-muted">
              {def.hint}
            </span>
          )}
        </span>
      </label>
    );
  }
  const money = def.kind === "money" || def.kind === "signed";
  return (
    <div>
      <label htmlFor={id} className="block text-[12.5px] leading-snug text-ink-soft">
        {def.label}
      </label>
      <span className="relative mt-1 block">
        <input
          id={id}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          readOnly={readOnlyNote !== undefined}
          inputMode={money ? "decimal" : "numeric"}
          placeholder="—"
          aria-invalid={problem ? true : undefined}
          aria-describedby={describedBy}
          className={clsx(
            INPUT,
            money && "pr-7",
            readOnlyNote !== undefined && "cursor-default bg-line/40 text-ink-soft",
            problem ? "border-risk-high-fg" : dirty ? "border-primary" : "border-line focus:border-primary",
          )}
        />
        {money && <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-[13px] text-muted">€</span>}
        {def.kind === "percent" && <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-[13px] text-muted">%</span>}
      </span>
      {problem ? (
        <span id={`${id}-problem`} className="mt-1 block text-[11.5px] font-semibold leading-snug text-risk-high-fg">
          {problem}
        </span>
      ) : readOnlyNote ? (
        <span className="mt-1 block text-[11.5px] leading-snug text-muted">{readOnlyNote}</span>
      ) : (
        def.hint && (
          <span id={`${id}-hint`} className="mt-1 block text-[11.5px] leading-snug text-muted">
            {def.hint}
          </span>
        )
      )}
    </div>
  );
}

function Group({ title, children, note }: { title: string; children: ReactNode; note?: ReactNode }) {
  return (
    <fieldset className="min-w-0">
      <legend className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">{title}</legend>
      {note && <div className="mb-2.5 text-[12.5px] leading-snug text-ink-soft">{note}</div>}
      <div className="grid gap-3 sm:grid-cols-2">{children}</div>
    </fieldset>
  );
}

/** A collapsible block whose open state lives in React (a re-render must not undo the user's click). */
function Collapsible({
  title,
  open,
  onToggle,
  children,
  tone = "plain",
  id,
}: {
  title: ReactNode;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
  tone?: "plain" | "warn";
  id?: string;
}) {
  return (
    <details id={id} open={open} className={clsx("group rounded-xl border", tone === "warn" ? "border-grounding-uncited-bd bg-grounding-uncited-bg" : "border-line bg-surface")}>
      <summary
        onClick={(e) => {
          e.preventDefault();
          onToggle();
        }}
        className={clsx(
          "flex cursor-pointer list-none items-center justify-between gap-2 px-4 py-2.5 text-[13px] font-semibold [&::-webkit-details-marker]:hidden",
          tone === "warn" ? "text-grounding-uncited-fg" : "text-ink-soft",
        )}
      >
        <span className="min-w-0">{title}</span>
        <ChevronDown size={16} aria-hidden className="flex-shrink-0 transition group-open:rotate-180" />
      </summary>
      <div className={clsx("border-t px-4 py-3", tone === "warn" ? "border-grounding-uncited-bd" : "border-line")}>{children}</div>
    </details>
  );
}

/** Text with the literal quotes of the law («…») set apart. */
function Quoted({ text }: { text: string }) {
  return (
    <>
      {splitQuotes(text).map((part, i) =>
        part.quote ? (
          <span key={i} className="italic text-ink">
            «{part.text}»
          </span>
        ) : (
          <span key={i}>{part.text}</span>
        ),
      )}
    </>
  );
}

function Badge({ className, children }: { className: string; children: ReactNode }) {
  return <span className={clsx("inline-flex items-center gap-1 rounded-pill border px-2 py-0.5 text-[11.5px] font-semibold", className)}>{children}</span>;
}

function euroPorEuro(value: string): string {
  const n = Number(value);
  return Number.isFinite(n) ? `${n.toLocaleString("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} €` : value;
}

function Trace({ declaracion, title }: { declaracion: Declaracion; title: string }) {
  const r = declaracion.resultado;
  return (
    <div>
      <p className="mb-1 text-[12.5px] font-semibold text-ink">
        {title} · {resultadoTexto(r.resultado)}
      </p>
      <table className="w-full text-[12.5px]">
        <caption className="sr-only">{title}</caption>
        <tbody>
          {r.lineas.map((line, i) => (
            <tr key={i} className="border-b border-line last:border-0">
              <td className="py-1.5 pr-3 text-ink-soft [overflow-wrap:anywhere]">
                {line.concepto}
                {line.referencia && <span className="block text-[11px] text-muted">{line.referencia}</span>}
              </td>
              <td className="whitespace-nowrap py-1.5 text-right font-mono tabular-nums text-ink">{eur(line.importe)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {r.parametros_no_verificados.length > 0 && (
        <p className="mt-1.5 text-[11.5px] leading-snug text-risk-high-fg">
          <span className="font-semibold">No verificado: </span>
          {r.parametros_no_verificados.join("; ")}
        </p>
      )}
    </div>
  );
}

function declaracionTitulo(hogar: ResultadoHogar, d: Declaracion): string {
  if (d.titular === "conjunta") return hogar.tributacion === "conjunta_monoparental" ? "Declaración conjunta monoparental" : "Declaración conjunta";
  if (d.titular === "conyuge") return "Declaración de tu cónyuge";
  return hogar.declaraciones.length > 1 ? "Tu declaración individual" : hogar.tributacion === "individual" ? "Tu declaración" : "Tu declaración conjunta";
}

// ---- The panel --------------------------------------------------------------------------

interface OptimizerPanelProps {
  open: boolean;
  onClose: () => void;
  caseId: string | null;
  facts: Fact[];
  taxPeriod: string | null;
  /** Persist one fact to the case (null deletes it). Resolves true when saved. */
  onSaveFact?: (factName: string, value: string | number | boolean | null) => Promise<boolean>;
  busy?: boolean;
  /** Open the simulator (for data this screen does not ask, such as the detail of each child). */
  onOpenSimulator: () => void;
  /** Open the simulator with the plan's figures. */
  onUseInSimulator?: (seed: SimulatorSeed) => void;
}

export function OptimizerPanel({ open, onClose, caseId, facts, taxPeriod, onSaveFact, busy = false, onOpenSimulator, onUseInSimulator }: OptimizerPanelProps) {
  const [tab, setTab] = useState<Tab>("datos");
  const [today] = useState(todayInSpain);
  // Figures layered over the case facts: { field: raw text }. Absent = what the case says.
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [comunidadEdit, setComunidadEdit] = useState<string | null>(null);
  const [estadoEdit, setEstadoEdit] = useState<string | null>(null);
  const [extraFields, setExtraFields] = useState<string[]>([]);
  // The spouse's figures and the constraints typed over what the case keeps (absent / null = what it says).
  const [spouseEdits, setSpouseEdits] = useState<Record<string, string>>({});
  const [tributacionEdit, setTributacionEdit] = useState<TributacionPrevista | null>(null);
  const [limitEdits, setLimitEdits] = useState<Partial<Record<LimitName, string>>>({});
  const [iliquidezEdit, setIliquidezEdit] = useState<Iliquidez | null>(null);
  const [positionsEdit, setPositionsEdit] = useState<Position[] | null>(null);
  const [fechaEdit, setFechaEdit] = useState<string | null>(null);
  const [cifrasOpen, setCifrasOpen] = useState<boolean | null>(null);
  const [sections, setSections] = useState<Record<string, boolean>>({});
  const [computed, setComputed] = useState<Computed | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const headlineRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // A request still running when the panel goes away is cancelled.
  useEffect(() => () => abortRef.current?.abort(), []);

  // ---- What the case already says --------------------------------------------------------
  const baseline = useMemo(() => {
    const map: Record<string, string> = {};
    for (const fact of facts) {
      if (typeof fact.value === "number") map[fact.name] = amountText(fact.value);
      else if (typeof fact.value === "boolean") map[fact.name] = String(fact.value);
    }
    return map;
  }, [facts]);
  const savedComunidad = useMemo(() => comunidadDeExpediente(facts.find((f) => f.name === "comunidad_autonoma")?.value), [facts]);
  const savedEstado = useMemo(() => estadoDeExpediente(facts.find((f) => f.name === "estado_civil")?.value), [facts]);
  const caseChildren = useMemo(() => childrenInCase(facts), [facts]);
  const storedSpouse = useMemo(() => parseStoredObject(facts.find((f) => f.name === DATOS_CONYUGE)?.value), [facts]);
  const storedPrefs = useMemo(
    () => parseStoredObject(facts.find((f) => f.name === PREFERENCIAS_OPTIMIZACION)?.value) as StoredPreferences | null,
    [facts],
  );
  const spouseBaseline = useMemo(() => {
    const map: Record<string, string> = {};
    for (const def of SPOUSE) {
      const v = storedSpouse?.[def.name];
      if (typeof v === "boolean") map[def.name] = String(v);
      else if (storedNumber(v) !== null) map[def.name] = amountText(storedNumber(v) ?? 0);
    }
    return map;
  }, [storedSpouse]);
  const limitsBaseline = useMemo(() => {
    const map: Partial<Record<LimitName, string>> = {};
    for (const name of LIMIT_NAMES) {
      const n = storedNumber(storedPrefs?.[name]);
      if (n !== null) map[name] = amountText(n);
    }
    return map;
  }, [storedPrefs]);
  const positionsBaseline = useMemo(
    () => [
      ...storedPositions(storedPrefs?.perdidas_latentes).map((p, i) => storedRow(p, "perdida", i)),
      ...storedPositions(storedPrefs?.ganancias_latentes).map((p, i) => storedRow(p, "ganancia", i)),
    ],
    [storedPrefs],
  );
  const iliquidezBaseline: Iliquidez = storedPrefs?.acepta_iliquidez === true ? "si" : storedPrefs?.acepta_iliquidez === false ? "no" : "";
  const tributacionBaseline: TributacionPrevista = storedPrefs?.tributacion_prevista === "conjunta_biparental" ? "conjunta_biparental" : "individual";
  const conExpediente = Boolean(caseId);
  const caseHasIncome = ["rendimiento_trabajo_bruto", "rendimiento_actividad_neto"].some((name) => name in baseline);
  const showCifras = cifrasOpen ?? !(conExpediente && caseHasIncome);

  const value = (name: string) => edits[name] ?? baseline[name] ?? "";
  const spouseValue = (name: string) => spouseEdits[name] ?? spouseBaseline[name] ?? "";
  const limitValue = (name: LimitName) => limitEdits[name] ?? limitsBaseline[name] ?? "";
  const iliquidez = iliquidezEdit ?? iliquidezBaseline;
  const positions = positionsEdit ?? positionsBaseline;
  const tributacionActual = tributacionEdit ?? tributacionBaseline;
  /** Edit the rows (the first edit starts from the stored ones). */
  const setPositions = (update: (prev: Position[]) => Position[]) => setPositionsEdit((prev) => update(prev ?? positionsBaseline));
  const comunidad = comunidadEdit ?? savedComunidad;
  const estado = estadoEdit ?? savedEstado;
  const married = estado === "casado";
  const taxYear = taxPeriod && /^\d{4}/.test(taxPeriod) ? Number(taxPeriod.slice(0, 4)) : null;
  const year = taxYear ?? computed?.plan.ejercicio ?? Number(today.slice(0, 4));
  const fecha = fechaEdit ?? today;
  const hijos = caseChildren ?? parseAmount(value("num_descendientes")) ?? 0;
  const visibleExtra = useMemo(() => {
    const shown = new Set(extraFields);
    for (const f of EXTRA) if (f.name in baseline) shown.add(f.name);
    // With the per-child detail in the case, the children's disability comes from it (edited in the simulator).
    return EXTRA.filter((f) => shown.has(f.name) && !(caseChildren !== null && COUNT_NAMES.has(f.name)));
  }, [extraFields, baseline, caseChildren]);

  // ---- The request ------------------------------------------------------------------------
  const problems: Record<string, string> = {};
  const datos: Record<string, unknown> = {};
  for (const def of USER_FIELDS) {
    if (caseChildren !== null && COUNT_NAMES.has(def.name)) continue;
    if (def === MONOPARENTAL && (married || hijos <= 0)) continue;
    const raw = edits[def.name];
    const problem = fieldProblem(def.kind, value(def.name));
    if (problem) problems[`opt-${def.name}`] = problem;
    if (raw === undefined || problem) continue;
    if (def.kind === "flag") {
      if ((raw === "true") !== (baseline[def.name] === "true")) datos[def.name] = raw === "true";
      continue;
    }
    const n = parseAmount(raw);
    const before = parseAmount(baseline[def.name]);
    if (n === null) {
      // A saved figure cleared in the form: zero, except an age (unknown is not zero years).
      if (before !== null && def.kind !== "age") datos[def.name] = 0;
    } else if (n !== before) datos[def.name] = n;
  }
  if (comunidadEdit !== null && comunidadEdit !== savedComunidad && comunidadEdit) {
    datos.comunidad_autonoma = comunidadEdit === "Otra" ? "otra comunidad" : comunidadEdit;
  }
  if (estadoEdit !== null && estadoEdit !== savedEstado && estadoEdit) datos.estado_civil = estadoEdit;

  // Every field of the spouse's section, null when empty: it replaces what the case keeps, field by field.
  const conyuge: Record<string, number | boolean | null> = {};
  let spouseGiven = false;
  if (married) {
    for (const def of SPOUSE) {
      const raw = spouseValue(def.name);
      conyuge[def.name] = null;
      if (def.kind === "flag") {
        if (raw === "true") conyuge[def.name] = true;
      } else {
        const problem = fieldProblem(def.kind, raw);
        if (problem) problems[`opt-conyuge-${def.name}`] = problem;
        const n = parseAmount(raw);
        if (!problem && n !== null) conyuge[def.name] = n;
      }
      if (conyuge[def.name] !== null) spouseGiven = true;
    }
  }
  // Sent either way: a stored «conjunta» must not stay the starting point when the screen says «individual».
  if (spouseGiven) datos.tributacion = tributacionActual;

  const limit = (name: LimitName, positive = false): number | undefined => {
    const raw = limitValue(name);
    if (!raw || !raw.trim()) return undefined;
    const n = parseAmount(raw);
    if (n === null) problems[`opt-${name}`] = "Escribe una cifra, por ejemplo 1.500,50.";
    else if (n < 0 || (positive && n === 0)) problems[`opt-${name}`] = positive ? "Debe ser mayor que cero." : "No puede ser negativo.";
    else return n;
    return undefined;
  };
  const pensionMax = limit("aportacion_maxima_pensiones");
  const pensionMaxConyuge = married ? limit("aportacion_maxima_pensiones_conyuge") : undefined;
  const previsto = limit("donativo_previsto");
  let presupuesto = limit("presupuesto_donativos");
  if (presupuesto !== undefined && presupuesto < (previsto ?? 0)) {
    problems["opt-presupuesto_donativos"] = "No puede ser menor que el donativo que ya vas a hacer.";
    presupuesto = undefined;
  }
  const recurrente = limit("donativo_recurrente_importe_anterior", true);
  const perdidas: PosicionLatente[] = [];
  const ganancias: PosicionLatente[] = [];
  for (const pos of positions) {
    const n = parseAmount(pos.importe);
    const id = `opt-pos-${pos.id}-importe`;
    if (!pos.importe.trim()) problems[id] = "Indica el importe (o quita esta fila).";
    else if (n === null) problems[id] = "Escribe una cifra, por ejemplo 1.500,50.";
    else if (n <= 0) problems[id] = "Escribe el importe en positivo, mayor que cero.";
    if (pos.titular === "conyuge" && !spouseGiven) problems[`opt-pos-${pos.id}-titular`] = "Indica las cifras de tu cónyuge o pon esta inversión a tu nombre.";
    if (n === null || n <= 0) continue;
    const item: PosicionLatente = { importe: n, tipo: pos.tipo, titular: pos.titular };
    if (pos.descripcion.trim()) item.descripcion = pos.descripcion.trim();
    (pos.clase === "perdida" ? perdidas : ganancias).push(item);
  }
  // Every constraint, null / 0 / [] when not stated: the screen, not the case's stored preferences, decides the run.
  const restricciones: Restricciones = {
    aportacion_maxima_pensiones: pensionMax ?? null,
    aportacion_maxima_pensiones_conyuge: (spouseGiven ? pensionMaxConyuge : undefined) ?? null,
    acepta_iliquidez: iliquidez ? iliquidez === "si" : null,
    donativo_previsto: previsto ?? 0,
    presupuesto_donativos: presupuesto ?? null,
    donativo_recurrente_importe_anterior: recurrente ?? null,
    perdidas_latentes: perdidas,
    ganancias_latentes: ganancias,
  };
  if (fechaEdit !== null && !/^\d{4}-\d{2}-\d{2}$/.test(fechaEdit)) problems["opt-fecha"] = "Elige una fecha.";

  const problemIds = Object.keys(problems);
  const invalid = problemIds.length > 0;
  const body: OptimizeRequest = { datos, restricciones };
  if (caseId) body.case_id = caseId;
  else if (taxYear !== null) body.ejercicio = taxYear;
  // Married: the spouse's figures on screen, or explicitly none (the case's stored ones are not used).
  if (married) body.conyuge = spouseGiven ? conyuge : null;
  if (fechaEdit !== null && fechaEdit !== today) body.fecha = fechaEdit;
  // What the plan depends on: the case's facts and the figures typed over them, the spouse, the
  // constraints and the date. Saving what is on screen to the case leaves it unchanged (the spouse's
  // figures and the constraints count through the request, which carries them in full).
  const perfil: Record<string, unknown> = {};
  for (const fact of facts) if (!PLAN_INPUT_FACTS.has(fact.name)) perfil[fact.name] = fact.value;
  Object.assign(perfil, datos);
  const key = JSON.stringify({
    perfil: Object.fromEntries(Object.entries(perfil).sort(([a], [b]) => a.localeCompare(b))),
    conyuge: body.conyuge === undefined ? "sin cónyuge" : body.conyuge,
    restricciones,
    fecha: body.fecha ?? null,
    ejercicio: body.ejercicio ?? null,
    caseId,
  });
  const stale = computed !== null && computed.key !== key;

  // What «Guardar en mi expediente» would write: [fact, value | null to delete], and how many changes that is.
  const pendientes: Array<[string, string | number | boolean | null]> = [];
  for (const [name, v] of Object.entries(datos)) {
    if (name === "tributacion") continue;
    pendientes.push([name, name in baseline && v === 0 && !(edits[name] ?? "").trim() ? null : (v as string | number | boolean)]);
  }
  let cambios = pendientes.length;

  // The spouse's figures: one fact, written whole (figures this screen does not show are kept), only
  // when none of its boxes has a problem. Each changed box is one change.
  const spouseOk = married && !SPOUSE.some((def) => problems[`opt-conyuge-${def.name}`]);
  const spouseChanges = spouseOk ? SPOUSE.filter((def) => stated(def.kind, spouseValue(def.name)) !== stated(def.kind, spouseBaseline[def.name])).length : 0;
  if (spouseChanges > 0) {
    const next: Record<string, unknown> = { ...(storedSpouse ?? {}) };
    for (const def of SPOUSE) {
      const now = stated(def.kind, spouseValue(def.name));
      if (!now) delete next[def.name];
      else next[def.name] = def.kind === "flag" ? true : Number(now);
    }
    // No box of the section left: the spouse's figures are removed (hidden ones alone would describe nobody).
    pendientes.push([DATOS_CONYUGE, SPOUSE.some((def) => def.name in next) ? JSON.stringify(next) : null]);
    cambios += spouseChanges;
  }

  // The constraints: one fact, written whole, only when none of them has a problem. What is hidden
  // (the spouse's limit and filing plan of a user who is not married) keeps its stored value.
  const prefProblem = Object.keys(problems).some(
    (id) => LIMIT_NAMES.some((name) => id === `opt-${name}`) || id.startsWith("opt-pos-"),
  );
  let prefChanges = 0;
  if (!prefProblem) {
    for (const name of LIMIT_NAMES) {
      if (name === "aportacion_maxima_pensiones_conyuge" && !married) continue;
      const zeroIsNone = name === "donativo_previsto";
      const now = stated("money", limitValue(name));
      const before = stated("money", limitsBaseline[name]);
      if ((zeroIsNone && now === "0" ? "" : now) !== (zeroIsNone && before === "0" ? "" : before)) prefChanges += 1;
    }
    if (iliquidez !== iliquidezBaseline) prefChanges += 1;
    if (rowsKey(positions) !== rowsKey(positionsBaseline)) prefChanges += 1;
    if (married && tributacionActual !== tributacionBaseline) prefChanges += 1;
  }
  if (prefChanges > 0) {
    const next: StoredPreferences = {};
    for (const name of LIMIT_NAMES) {
      const raw = name === "aportacion_maxima_pensiones_conyuge" && !married ? limitsBaseline[name] : limitValue(name);
      const n = parseAmount(raw);
      if (n !== null && !(name === "donativo_previsto" && n === 0)) next[name] = n;
    }
    if (iliquidez) next.acepta_iliquidez = iliquidez === "si";
    const row = (p: Position): StoredPosition => {
      const item: StoredPosition = { id: p.id, importe: parseAmount(p.importe) ?? 0, tipo: p.tipo, titular: p.titular };
      if (p.descripcion.trim()) item.descripcion = p.descripcion.trim();
      return item;
    };
    const perdidasGuardar = positions.filter((p) => p.clase === "perdida").map(row);
    const gananciasGuardar = positions.filter((p) => p.clase === "ganancia").map(row);
    if (perdidasGuardar.length) next.perdidas_latentes = perdidasGuardar;
    if (gananciasGuardar.length) next.ganancias_latentes = gananciasGuardar;
    // The filing plan only once the user chose it (the screen's «individual» is a default, not a statement).
    const tributacion = married ? (tributacionEdit ?? storedPrefs?.tributacion_prevista) : storedPrefs?.tributacion_prevista;
    if (tributacion) next.tributacion_prevista = tributacion;
    pendientes.push([PREFERENCIAS_OPTIMIZACION, Object.keys(next).length ? JSON.stringify(next) : null]);
    cambios += prefChanges;
  }

  if (!open) return null;

  // ---- Actions -----------------------------------------------------------------------------
  async function calcular(event?: FormEvent) {
    event?.preventDefault();
    if (invalid) {
      flushSync(() => {
        setTab("datos");
        if (problemIds.some((id) => FIELD_BY_NAME.has(id.slice(4)))) setCifrasOpen(true);
      });
      const first = document.getElementById(problemIds[0]);
      first?.scrollIntoView({ block: "center" });
      first?.focus();
      return;
    }
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const snapshot = { key, datos: { ...datos }, comunidad: comunidadEdit };
    setLoading(true);
    setError(null);
    try {
      const plan = await optimizar(body, controller.signal);
      flushSync(() => {
        setComputed({ plan, ...snapshot });
        setTab("plan");
      });
      headlineRef.current?.focus();
      headlineRef.current?.scrollIntoView({ block: "start" });
    } catch (err) {
      if (controller.signal.aborted) return;
      setError(err instanceof ApiError ? humanizeOptimizeError(err.message, err.status) : "No se pudo calcular el plan: comprueba tu conexión e inténtalo de nuevo.");
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
        setLoading(false);
      }
    }
  }

  /** Take the user to the place where a missing figure is typed (or add it inline). */
  function completar(name: string) {
    let target: string | null = null;
    if (name === "conyuge") target = `opt-conyuge-${SPOUSE[0].name}`;
    else if (name === "estado_civil" || name === "comunidad_autonoma") target = `opt-${name}`;
    else if (name === "perdidas_latentes") {
      const existing = positions.find((p) => p.clase === "perdida");
      const row = existing ?? newPosition("perdida");
      target = `opt-pos-${row.id}-importe`;
      if (!existing) flushSync(() => setPositions((prev) => [...prev, row]));
    } else if (FIELD_BY_NAME.has(name)) target = `opt-${name}`;
    if (target === null) {
      onOpenSimulator();
      return;
    }
    flushSync(() => {
      setTab("datos");
      if (name === "conyuge" && !married) setEstadoEdit("casado");
      if (FIELD_BY_NAME.has(name) || name === "estado_civil" || name === "comunidad_autonoma") setCifrasOpen(true);
      if (EXTRA_NAMES.has(name)) setExtraFields((prev) => (prev.includes(name) ? prev : [...prev, name]));
    });
    const el = document.getElementById(target);
    el?.scrollIntoView({ block: "center" });
    el?.focus();
  }

  /** Switch tab and start it from the top (the two tabs share one scrolling area). */
  function showTab(next: Tab) {
    flushSync(() => setTab(next));
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  }

  function goToData() {
    showTab("datos");
    document.getElementById("opt-tab-datos")?.focus();
  }

  async function saveToCase() {
    if (!onSaveFact) return;
    setSaving(true);
    setSaveError(null);
    try {
      for (const [name, v] of pendientes) {
        if (!(await onSaveFact(name, v))) {
          setSaveError("No se pudo guardar todo en tu expediente. Lo que no se guardó sigue en el formulario.");
          return;
        }
        setEdits((prev) => {
          const next = { ...prev };
          delete next[name];
          return next;
        });
        if (name === "comunidad_autonoma") setComunidadEdit(null);
        if (name === "estado_civil") setEstadoEdit(null);
        if (name === DATOS_CONYUGE) setSpouseEdits({});
        if (name === PREFERENCIAS_OPTIMIZACION) {
          setLimitEdits({});
          setIliquidezEdit(null);
          setPositionsEdit(null);
          setTributacionEdit(null);
        }
      }
    } finally {
      setSaving(false);
    }
  }

  /** Back to what the case says, everywhere on «Tus datos» (the date of the plan is not in the case). */
  function undoChanges() {
    setEdits({});
    setComunidadEdit(null);
    setEstadoEdit(null);
    setSpouseEdits({});
    setTributacionEdit(null);
    setLimitEdits({});
    setIliquidezEdit(null);
    setPositionsEdit(null);
  }

  function sendToSimulator() {
    if (!computed || !onUseInSimulator) return;
    const { plan } = computed;
    const figure = (name: string) => {
      const typed = computed.datos[name];
      if (typeof typed === "number") return typed;
      return parseAmount(baseline[name]) ?? 0;
    };
    const mapped = planEnSimulador(plan, figure);
    if (!mapped.ok) return;
    const seedEdits: Record<string, string> = {};
    for (const [name, v] of Object.entries(computed.datos)) {
      if (typeof v === "number") seedEdits[name] = amountText(v);
      else if (typeof v === "boolean") seedEdits[name] = String(v);
    }
    for (const [name, v] of Object.entries(mapped.cambios)) seedEdits[name] = amountText(v);
    onUseInSimulator({
      edits: seedEdits,
      tributacion: mapped.tributacion,
      comunidad: computed.comunidad,
      note: `Cifras del plan óptimo cargadas: con ellas el plan da ${resultadoTexto(plan.optimo.resultado_total)}. Si cambias algo aquí, el plan deja de ser el óptimo calculado.`,
    });
  }

  const setEdit = (name: string, raw: string) => setEdits((prev) => ({ ...prev, [name]: raw }));
  const limitDirty = (name: LimitName) => conExpediente && stated("money", limitValue(name)) !== stated("money", limitsBaseline[name]);
  const toggle = (name: string) => setSections((prev) => ({ ...prev, [name]: !prev[name] }));

  // ---- Form ---------------------------------------------------------------------------------
  const renderUserField = (def: FieldDef) => (
    <Field
      key={def.name}
      id={`opt-${def.name}`}
      def={def}
      value={value(def.name)}
      onChange={(raw) => setEdit(def.name, raw)}
      problem={problems[`opt-${def.name}`]}
      dirty={def.name in datos}
    />
  );

  const fechaPasada = fecha > `${year}-12-31`;

  function datosTab() {
    const addable = EXTRA.filter(
      (f) => !visibleExtra.includes(f) && !(caseChildren !== null && COUNT_NAMES.has(f.name)),
    );
    return (
      <form id="opt-form" onSubmit={(e) => void calcular(e)} noValidate className="space-y-5">
        <p className="text-[13px] leading-snug text-ink-soft">
          {conExpediente
            ? "Partimos de los datos de tu expediente. Puedes corregirlos aquí: los cambios solo cuentan para este cálculo hasta que los guardes."
            : "Escribe tus cifras del año. Una casilla vacía cuenta como «no lo sé», no como cero: el plan te dirá qué datos le faltan."}
        </p>

        <Collapsible
          id="opt-cifras"
          open={showCifras}
          onToggle={() => setCifrasOpen(!showCifras)}
          title={
            <>
              Tus cifras
              {conExpediente && <span className="ml-1.5 font-normal text-muted">(de tu expediente)</span>}
            </>
          }
        >
          <div className="space-y-5">
            <Group title="Ingresos del año">{INGRESOS.map(renderUserField)}</Group>
            <Group title="Lo que ya has hecho este año">{HECHO.map(renderUserField)}</Group>
            <Group title="Tu situación">
              <div>
                <label htmlFor="opt-comunidad_autonoma" className="block text-[12.5px] leading-snug text-ink-soft">
                  Comunidad autónoma
                </label>
                <select
                  id="opt-comunidad_autonoma"
                  value={comunidad}
                  onChange={(e) => setComunidadEdit(e.target.value)}
                  className={clsx(INPUT, "mt-1", "comunidad_autonoma" in datos ? "border-primary" : "border-line focus:border-primary")}
                >
                  {COMUNIDADES.filter((c) => c.value || !savedComunidad).map((c) => (
                    <option key={c.value} value={c.value}>
                      {c.label}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="opt-estado_civil" className="block text-[12.5px] leading-snug text-ink-soft">
                  Estado civil
                </label>
                <select
                  id="opt-estado_civil"
                  value={estado}
                  onChange={(e) => setEstadoEdit(e.target.value)}
                  aria-describedby="opt-estado_civil-hint"
                  className={clsx(INPUT, "mt-1", "estado_civil" in datos ? "border-primary" : "border-line focus:border-primary")}
                >
                  {ESTADOS.filter((e) => e.value || !savedEstado).map((e) => (
                    <option key={e.value} value={e.value}>
                      {e.label}
                    </option>
                  ))}
                </select>
                <span id="opt-estado_civil-hint" className="mt-1 block text-[11.5px] leading-snug text-muted">
                  Si estás casado o casada, se compara la declaración conjunta.
                </span>
              </div>
              {caseChildren !== null ? (
                <>
                  {renderUserField(FAMILIA[0])}
                  <p className="text-[12.5px] leading-snug text-ink-soft sm:col-span-2">
                    Hijos: {caseChildren}, del detalle de cada hijo de tu expediente.{" "}
                    <button type="button" onClick={onOpenSimulator} className="font-semibold text-primary-dark underline underline-offset-2 hover:no-underline">
                      Se edita en el simulador
                    </button>
                    .
                  </p>
                </>
              ) : (
                FAMILIA.map(renderUserField)
              )}
              {!married && hijos > 0 && renderUserField(MONOPARENTAL)}
            </Group>
            <Group
              title="Más datos que pueden cambiar tus deducciones"
              note={
                <>
                  Gastos de guardería, alquiler, ascendientes… Una medida puede bajar tu base por debajo del límite de una deducción: si tienes alguno de estos gastos, añádelo.
                  {caseChildren === null && " Para el detalle de cada hijo, usa el simulador."}
                </>
              }
            >
              {visibleExtra.map(renderUserField)}
              {addable.length > 0 && (
                <div className="sm:col-span-2">
                  <label htmlFor="opt-add-extra" className="block text-[12.5px] leading-snug text-ink-soft">
                    Añadir un dato
                  </label>
                  <select
                    id="opt-add-extra"
                    value=""
                    onChange={(e) => {
                      const name = e.target.value;
                      if (!name) return;
                      flushSync(() => setExtraFields((prev) => [...prev, name]));
                      document.getElementById(`opt-${name}`)?.focus();
                    }}
                    className={clsx(INPUT, "mt-1 border-line focus:border-primary")}
                  >
                    <option value="">Elige un dato…</option>
                    {addable.map((f) => (
                      <option key={f.name} value={f.name}>
                        {f.label}
                      </option>
                    ))}
                  </select>
                </div>
              )}
            </Group>
          </div>
        </Collapsible>

        {married && (
          <div className="rounded-xl border border-line bg-surface px-4 py-3.5">
          <Group
            title="Tu cónyuge"
            note={
              "Para comparar la declaración conjunta con las individuales hacen falta sus cifras por separado. Sin ellas, el plan no puede valorar la conjunta." +
              (storedSpouse ? " Las que ya tienes guardadas en tu expediente aparecen rellenas." : "")
            }
          >
            {SPOUSE.map((def) => (
              <Field
                key={def.name}
                id={`opt-conyuge-${def.name}`}
                def={def}
                value={spouseValue(def.name)}
                onChange={(raw) => setSpouseEdits((prev) => ({ ...prev, [def.name]: raw }))}
                problem={problems[`opt-conyuge-${def.name}`]}
                dirty={conExpediente && stated(def.kind, spouseValue(def.name)) !== stated(def.kind, spouseBaseline[def.name])}
              />
            ))}
            <div className="sm:col-span-2">
              <label htmlFor="opt-tributacion-actual" className="block text-[12.5px] leading-snug text-ink-soft">
                Cómo pensabais declarar
              </label>
              <select
                id="opt-tributacion-actual"
                value={tributacionActual}
                onChange={(e) => setTributacionEdit(e.target.value === "conjunta_biparental" ? "conjunta_biparental" : "individual")}
                aria-describedby="opt-tributacion-actual-hint"
                className={clsx(INPUT, "mt-1", conExpediente && tributacionActual !== tributacionBaseline ? "border-primary" : "border-line focus:border-primary")}
              >
                <option value="individual">Cada uno su declaración (individual)</option>
                <option value="conjunta_biparental">Una declaración conjunta</option>
              </select>
              <span id="opt-tributacion-actual-hint" className="mt-1 block text-[11.5px] leading-snug text-muted">
                Es el punto de partida con el que se compara el plan; el plan valora las dos opciones.
              </span>
            </div>
          </Group>
          </div>
        )}

        <div className="border-t border-line pt-4">
          <h3 className="font-serif text-[16px] font-bold text-ink">Lo que puedes hacer antes de fin de año</h3>
          <p className="mt-0.5 text-[12.5px] leading-snug text-ink-soft">
            El plan solo propone lo que aceptes aquí, y nunca por encima de los importes que indiques.
            {storedPrefs && " Lo que guardaste en tu expediente aparece ya rellenado."}
          </p>
        </div>

        <Group title="Planes de pensiones">
          <Field
            id="opt-aportacion_maxima_pensiones"
            def={{
              name: "aportacion_maxima_pensiones",
              label: "Lo que aún puedes aportar a tu plan",
              kind: "money",
              hint: "Este año, además de lo ya aportado. Vacío si no lo sabes: el plan supone que puedes llegar al máximo que reduce tu impuesto, y te lo dice.",
            }}
            value={limitValue("aportacion_maxima_pensiones")}
            onChange={(raw) => setLimitEdits((prev) => ({ ...prev, aportacion_maxima_pensiones: raw }))}
            problem={problems["opt-aportacion_maxima_pensiones"]}
            dirty={limitDirty("aportacion_maxima_pensiones")}
          />
          {married && (
            <Field
              id="opt-aportacion_maxima_pensiones_conyuge"
              def={{ name: "aportacion_maxima_pensiones_conyuge", label: "Y al plan de tu cónyuge", kind: "money", hint: "Igual: vacío si no lo sabes." }}
              value={limitValue("aportacion_maxima_pensiones_conyuge")}
              onChange={(raw) => setLimitEdits((prev) => ({ ...prev, aportacion_maxima_pensiones_conyuge: raw }))}
              problem={problems["opt-aportacion_maxima_pensiones_conyuge"]}
              dirty={limitDirty("aportacion_maxima_pensiones_conyuge")}
            />
          )}
          <fieldset className="sm:col-span-2" aria-describedby="opt-iliquidez-hint">
            <legend className="block text-[12.5px] leading-snug text-ink-soft">¿Aceptas que el dinero quede inmovilizado hasta la jubilación?</legend>
            <p id="opt-iliquidez-hint" className="mt-0.5 text-[11.5px] leading-snug text-muted">
              Lo aportado a un plan de pensiones solo se puede rescatar en la jubilación y en otros supuestos tasados.
            </p>
            <div className="mt-1.5 space-y-1.5">
              {(
                [
                  ["si", "Sí, acepto que el dinero quede inmovilizado hasta la jubilación"],
                  ["no", "No: necesito poder disponer de ese dinero (no se proponen planes de pensiones)"],
                  ["", "Aún no lo sé (el plan supondrá que sí y te lo dirá)"],
                ] as Array<[Iliquidez, string]>
              ).map(([v, label]) => (
                <label key={v || "nose"} className="flex items-start gap-2.5 text-[13.5px] text-ink">
                  <input
                    type="radio"
                    name="opt-iliquidez"
                    id={v === "si" ? "opt-acepta_iliquidez" : undefined}
                    checked={iliquidez === v}
                    onChange={() => setIliquidezEdit(v)}
                    className="mt-0.5 h-4 w-4 flex-shrink-0 accent-[rgb(var(--primary))]"
                  />
                  {label}
                </label>
              ))}
            </div>
          </fieldset>
        </Group>

        <Group title="Donativos">
          <Field
            id="opt-donativo_previsto"
            def={{
              name: "donativo_previsto",
              label: "Donativo que harás de todos modos",
              kind: "money",
              hint: "El plan elige a qué tipo de entidad conviene hacerlo (y, si sois matrimonio, quién lo hace); no te propone donar más.",
            }}
            value={limitValue("donativo_previsto")}
            onChange={(raw) => setLimitEdits((prev) => ({ ...prev, donativo_previsto: raw }))}
            problem={problems["opt-donativo_previsto"]}
            dirty={limitDirty("donativo_previsto")}
          />
          <Field
            id="opt-presupuesto_donativos"
            def={{
              name: "presupuesto_donativos",
              label: "Presupuesto máximo (opcional)",
              kind: "money",
              hint: "Si lo indicas, el plan puede proponerte donar algo más, nunca por encima de este importe. Donar siempre cuesta dinero: verás su coste neto.",
            }}
            value={limitValue("presupuesto_donativos")}
            onChange={(raw) => setLimitEdits((prev) => ({ ...prev, presupuesto_donativos: raw }))}
            problem={problems["opt-presupuesto_donativos"]}
            dirty={limitDirty("presupuesto_donativos")}
          />
          <div className="sm:col-span-2">
          <Field
            id="opt-donativo_recurrente_importe_anterior"
            def={{
              name: "donativo_recurrente_importe_anterior",
              label: "Lo que donaste el año pasado a la misma entidad (opcional)",
              kind: "money",
              hint: "Solo si le donas desde hace al menos dos años y el año pasado no le diste menos que el anterior: parte del donativo deduce más (art. 19.1 Ley 49/2002).",
            }}
            value={limitValue("donativo_recurrente_importe_anterior")}
            onChange={(raw) => setLimitEdits((prev) => ({ ...prev, donativo_recurrente_importe_anterior: raw }))}
            problem={problems["opt-donativo_recurrente_importe_anterior"]}
            dirty={limitDirty("donativo_recurrente_importe_anterior")}
          />
          </div>
        </Group>

        <fieldset className="min-w-0">
          <legend className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">Inversiones que podrías vender antes del 31 de diciembre</legend>
          <p className="text-[12.5px] leading-snug text-ink-soft">
            Una <strong className="font-semibold">pérdida latente</strong> es lo que perderías si vendieras hoy: venderla puede compensar tus ganancias del año. Las ganancias latentes no bajan el
            impuesto, pero el plan te avisa si realizarlas este año te sale gratis.
          </p>
          {positions.length > 0 && <ul className="mt-2.5 space-y-2.5">{positions.map((pos, index) => renderPosition(pos, index))}</ul>}
          <div className="mt-2.5 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => addPosition("perdida")}
              disabled={positions.filter((p) => p.clase === "perdida").length >= 20}
              className="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1.5 text-[12.5px] font-semibold text-primary-dark transition hover:border-primary disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Plus size={14} aria-hidden />
              Añadir una pérdida latente
            </button>
            <button
              type="button"
              onClick={() => addPosition("ganancia")}
              disabled={positions.filter((p) => p.clase === "ganancia").length >= 20}
              className="inline-flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1.5 text-[12.5px] font-semibold text-primary-dark transition hover:border-primary disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Plus size={14} aria-hidden />
              Añadir una ganancia latente
            </button>
          </div>
        </fieldset>

        <fieldset className="min-w-0">
          <legend className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">Fecha del plan</legend>
          <div className="flex flex-wrap items-end gap-2">
            <div>
              <label htmlFor="opt-fecha" className="block text-[12.5px] leading-snug text-ink-soft">
                Día para el que se hace el plan
              </label>
              <input
                id="opt-fecha"
                type="date"
                value={fecha}
                onChange={(e) => setFechaEdit(e.target.value)}
                aria-describedby="opt-fecha-hint"
                aria-invalid={problems["opt-fecha"] ? true : undefined}
                className={clsx(INPUT, "mt-1 block w-auto", problems["opt-fecha"] ? "border-risk-high-fg" : fechaEdit && fechaEdit !== today ? "border-primary" : "border-line focus:border-primary")}
              />
            </div>
            {fechaEdit !== null && fechaEdit !== today && (
              <button type="button" onClick={() => setFechaEdit(null)} className="rounded-lg px-2.5 py-2 text-[12.5px] font-semibold text-primary-dark hover:bg-line/60">
                Volver a hoy
              </button>
            )}
          </div>
          <p id="opt-fecha-hint" className="mt-1 text-[11.5px] leading-snug text-muted">
            Por defecto, hoy. Hasta el 31 de diciembre de {year} aún puedes aportar, vender o donar; después solo quedan las opciones de la declaración (conjunta o individual).
          </p>
          {problems["opt-fecha"] && <p className="mt-1 text-[11.5px] font-semibold text-risk-high-fg">{problems["opt-fecha"]}</p>}
          {fechaPasada && (
            <p className="mt-1.5 rounded-lg bg-grounding-abstain-bg px-3 py-1.5 text-[12.5px] text-grounding-abstain-fg">
              Con esta fecha el ejercicio {year} ya ha terminado: solo se valoran las opciones de la declaración.
            </p>
          )}
        </fieldset>

        <div className="sticky bottom-0 z-10 -mx-4 border-t border-line bg-paper px-4 pb-1 pt-3 sm:-mx-5 sm:px-5">
          {error && (
            <p id="opt-error" role="alert" className="mb-2 flex gap-2 rounded-lg border border-risk-high-fg/30 bg-risk-high-bg px-3 py-2 text-[13px] text-ink-soft">
              <TriangleAlert size={15} aria-hidden className="mt-0.5 flex-shrink-0 text-risk-high-fg" />
              <span>{error}</span>
            </p>
          )}
          {invalid ? (
            <p role="status" className="mb-2 flex gap-1.5 text-[12.5px] font-semibold text-risk-high-fg">
              <TriangleAlert size={15} aria-hidden className="mt-0.5 flex-shrink-0" />
              Hay {problemIds.length === 1 ? "un dato" : `${problemIds.length} datos`} que revisar (en rojo).
            </p>
          ) : (
            stale && (
              <p role="status" className="mb-2 text-[12.5px] font-semibold text-grounding-uncited-fg">
                Has cambiado datos: el plan calculado ya no corresponde a lo que has escrito.
              </p>
            )
          )}
          {conExpediente && onSaveFact && cambios > 0 && (
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => void saveToCase()}
                disabled={saving || busy}
                className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-surface px-3 py-2 text-[13px] font-semibold text-primary-dark transition hover:border-primary disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Save size={14} aria-hidden />
                {saving ? "Guardando…" : `Guardar ${cambios === 1 ? "este cambio" : `estos ${cambios} cambios`} en mi expediente`}
              </button>
              <button type="button" onClick={undoChanges} className="inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-[13px] font-semibold text-ink-soft hover:bg-line/60">
                <RotateCcw size={14} aria-hidden />
                Deshacer cambios
              </button>
              {saveError && (
                <p role="alert" className="w-full text-[12.5px] font-semibold text-risk-high-fg">
                  {saveError}
                </p>
              )}
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="submit"
              disabled={loading}
              aria-describedby={error ? "opt-error" : undefined}
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2.5 text-[14px] font-semibold text-white transition hover:bg-primary-hover disabled:cursor-wait disabled:opacity-70"
            >
              {loading ? <Loader2 size={16} aria-hidden className="animate-spin" /> : <PiggyBank size={16} aria-hidden />}
              {loading ? "Calculando…" : computed ? "Recalcular mi plan" : "Calcular mi plan"}
            </button>
            {computed && !stale && !loading && (
              <button type="button" onClick={() => showTab("plan")} className="inline-flex items-center gap-1 rounded-lg px-3 py-2.5 text-[13px] font-semibold text-primary-dark hover:bg-line/60">
                Ver mi plan
                <ArrowRight size={14} aria-hidden />
              </button>
            )}
          </div>
        </div>
      </form>
    );
  }

  function newPosition(clase: Position["clase"]): Position {
    return { id: newId(), clase, descripcion: "", importe: "", tipo: "valores_cotizados", titular: "contribuyente" };
  }

  function addPosition(clase: Position["clase"]) {
    const row = newPosition(clase);
    flushSync(() => setPositions((prev) => [...prev, row]));
    document.getElementById(`opt-pos-${row.id}-importe`)?.focus();
  }

  function setPosition(id: string, patch: Partial<Position>) {
    setPositions((prev) => prev.map((p) => (p.id === id ? { ...p, ...patch } : p)));
  }

  function renderPosition(pos: Position, index: number) {
    const base = `opt-pos-${pos.id}`;
    const n = positions.filter((p, i) => p.clase === pos.clase && i <= index).length;
    const name = `${pos.clase === "perdida" ? "Pérdida" : "Ganancia"} latente ${n}`;
    const importeProblem = problems[`${base}-importe`];
    const titularProblem = problems[`${base}-titular`];
    return (
      <li key={pos.id} className="rounded-xl border border-line bg-surface px-3 py-3">
        <div className="mb-2 flex items-center justify-between gap-2">
          <p className={clsx("text-[13px] font-semibold", pos.clase === "perdida" ? "text-ink" : "text-ink-soft")}>{name}</p>
          <button
            type="button"
            onClick={() => setPositions((prev) => prev.filter((p) => p.id !== pos.id))}
            aria-label={`Quitar la ${name.toLowerCase()}`}
            title="Quitar"
            className="rounded-lg p-1.5 text-muted transition hover:bg-risk-high-bg hover:text-risk-high-fg"
          >
            <Trash2 size={15} aria-hidden />
          </button>
        </div>
        <div className="grid gap-2.5 sm:grid-cols-2">
          <div>
            <label htmlFor={`${base}-descripcion`} className="block text-[12px] leading-snug text-ink-soft">
              Qué es (opcional)
            </label>
            <input
              id={`${base}-descripcion`}
              value={pos.descripcion}
              maxLength={200}
              onChange={(e) => setPosition(pos.id, { descripcion: e.target.value })}
              placeholder="Fondo indexado, acciones de…"
              className={clsx(INPUT, "mt-0.5 border-line focus:border-primary")}
            />
          </div>
          <div>
            <label htmlFor={`${base}-importe`} className="block text-[12px] leading-snug text-ink-soft">
              {pos.clase === "perdida" ? "Pérdida si vendieras hoy" : "Ganancia si vendieras hoy"}
            </label>
            <span className="relative mt-0.5 block">
              <input
                id={`${base}-importe`}
                value={pos.importe}
                inputMode="decimal"
                placeholder="—"
                onChange={(e) => setPosition(pos.id, { importe: e.target.value })}
                aria-invalid={importeProblem ? true : undefined}
                aria-describedby={importeProblem ? `${base}-importe-problem` : `${base}-importe-hint`}
                className={clsx(INPUT, "pr-7", importeProblem ? "border-risk-high-fg" : "border-line focus:border-primary")}
              />
              <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-[13px] text-muted">€</span>
            </span>
            {importeProblem ? (
              <span id={`${base}-importe-problem`} className="mt-1 block text-[11.5px] font-semibold leading-snug text-risk-high-fg">
                {importeProblem}
              </span>
            ) : (
              <span id={`${base}-importe-hint`} className="mt-1 block text-[11.5px] leading-snug text-muted">
                Valor de venta menos lo que te costó, gastos incluidos. En positivo.
              </span>
            )}
          </div>
          <div>
            <label htmlFor={`${base}-tipo`} className="block text-[12px] leading-snug text-ink-soft">
              ¿Es un valor cotizado?
            </label>
            <select id={`${base}-tipo`} value={pos.tipo} onChange={(e) => setPosition(pos.id, { tipo: e.target.value as TipoActivo })} className={clsx(INPUT, "mt-0.5 border-line focus:border-primary")}>
              {TIPO_ACTIVO.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
          </div>
          {(spouseGiven || pos.titular === "conyuge") && (
            <div>
              <label htmlFor={`${base}-titular`} className="block text-[12px] leading-snug text-ink-soft">
                De quién es
              </label>
              <select
                id={`${base}-titular`}
                value={pos.titular}
                onChange={(e) => setPosition(pos.id, { titular: e.target.value === "conyuge" ? "conyuge" : "contribuyente" })}
                aria-invalid={titularProblem ? true : undefined}
                className={clsx(INPUT, "mt-0.5", titularProblem ? "border-risk-high-fg" : "border-line focus:border-primary")}
              >
                <option value="contribuyente">Mía</option>
                <option value="conyuge">De mi cónyuge</option>
              </select>
              {titularProblem && <span className="mt-1 block text-[11.5px] font-semibold leading-snug text-risk-high-fg">{titularProblem}</span>}
            </div>
          )}
        </div>
        {pos.clase === "perdida" && (
          <p className="mt-2.5 flex gap-1.5 rounded-lg bg-grounding-uncited-bg px-2.5 py-2 text-[12px] leading-snug text-ink-soft">
            <Info size={14} aria-hidden className="mt-0.5 flex-shrink-0 text-grounding-uncited-fg" />
            <span>{reglaRecompra(pos.tipo)}</span>
          </p>
        )}
      </li>
    );
  }

  // ---- Plan -----------------------------------------------------------------------------------
  function completeButtons(names: string[]) {
    return (
      <div className="mt-2 flex flex-wrap gap-2">
        {names.map((name) => {
          const inForm = name === "conyuge" || name === "estado_civil" || name === "comunidad_autonoma" || name === "perdidas_latentes" || FIELD_BY_NAME.has(name);
          return (
            <button
              key={name}
              type="button"
              onClick={() => completar(name)}
              className="inline-flex items-center gap-1 rounded-lg border border-primary/40 bg-surface px-2.5 py-1 text-[12.5px] font-semibold text-primary-dark transition hover:border-primary"
            >
              {inForm ? `Completar: ${enMinuscula(missingLabel(name))}` : `${missingLabel(name)}: abrir el simulador`}
              <ArrowRight size={13} aria-hidden />
            </button>
          );
        })}
      </div>
    );
  }

  function leverCard(p: PalancaElegida, index: number) {
    const nat = NATURALEZA[p.naturaleza];
    const aislado = Number(p.ahorro_aislado);
    const compromiso = Number(p.compromiso);
    const reqOpen = sections[`req-${p.id}`] ?? false;
    return (
      <li key={p.id} className="rounded-xl border border-line bg-surface px-4 py-3.5 shadow-card">
        <div className="flex items-start gap-3">
          <span aria-hidden className="mt-0.5 flex h-6 w-6 flex-shrink-0 items-center justify-center rounded-full bg-primary-soft text-[12.5px] font-bold text-primary-dark">
            {index + 1}
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-[12px] text-muted">
              {p.nombre}
              {p.titular !== "hogar" && <> · {titularTexto(p.titular)}</>}
            </p>
            <h4 className="text-[14.5px] font-semibold leading-snug text-ink">{p.accion}</h4>
            <p className="mt-0.5 text-[12.5px] text-ink-soft">{p.plazo ? <>Fecha límite: {formatDate(p.plazo)}</> : p.id === "tributacion" ? "Se elige al presentar la declaración" : null}</p>
            <div className="mt-2 flex flex-wrap gap-1.5">
              <Badge className={nat.cls}>{nat.label}</Badge>
              {!p.verificado && (
                <Badge className="border-risk-high-fg/30 bg-risk-high-bg text-risk-high-fg">
                  <CircleAlert size={12} aria-hidden />
                  No verificado
                </Badge>
              )}
            </div>
          </div>
        </div>
        <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-2.5 border-t border-line pt-3 sm:grid-cols-3">
          <div>
            <dt className="text-[11.5px] leading-tight text-muted">Ahorro dentro del plan</dt>
            <dd className="mt-0.5 font-serif text-[18px] font-bold leading-tight text-grounding-cited-fg">{eur(p.ahorro_marginal)}</dd>
          </div>
          <div>
            <dt className="text-[11.5px] leading-tight text-muted">Por sí sola</dt>
            <dd className={clsx("mt-0.5 text-[13.5px] font-semibold leading-tight", aislado < 0 ? "text-risk-high-fg" : "text-ink")}>
              {aislado > 0 ? eur(aislado) : aislado < 0 ? `costaría ${eur(-aislado)} más` : "no ahorraría nada"}
            </dd>
          </div>
          {compromiso > 0 && (
            <div>
              <dt className="text-[11.5px] leading-tight text-muted">{compromisoLabel(p)}</dt>
              <dd className="mt-0.5 text-[13.5px] font-semibold leading-tight text-ink">{eur(compromiso)}</dd>
            </div>
          )}
          {p.ahorro_por_euro !== null && (
            <div>
              <dt className="text-[11.5px] leading-tight text-muted">Ahorro por euro comprometido</dt>
              <dd className="mt-0.5 text-[13.5px] font-semibold leading-tight text-ink">{euroPorEuro(p.ahorro_por_euro)}</dd>
            </div>
          )}
          {p.coste_neto !== null && (
            <div>
              <dt className="text-[11.5px] leading-tight text-muted">Coste neto (lo que te cuesta)</dt>
              <dd className="mt-0.5 text-[13.5px] font-semibold leading-tight text-ink">{eur(p.coste_neto)}</dd>
            </div>
          )}
        </dl>
        <p className="mt-2.5 text-[12.5px] leading-snug text-ink-soft">{naturalezaTexto(p)}</p>
        {aislado <= 0 && Number(p.ahorro_marginal) > 0 && (
          <p className="mt-1.5 text-[12.5px] leading-snug text-ink-soft">Por sí sola no compensaría: solo ahorra combinada con el resto del plan.</p>
        )}
        {p.accion_aislada !== p.accion && aislado > 0 && (
          <p className="mt-1.5 text-[12.5px] leading-snug text-ink-soft">Por sí sola, lo mejor sería otra cosa: «{p.accion_aislada}».</p>
        )}
        <details open={reqOpen} className="group mt-2.5">
          <summary
            onClick={(e) => {
              e.preventDefault();
              toggle(`req-${p.id}`);
            }}
            className="flex cursor-pointer list-none items-center gap-1 text-[12.5px] font-semibold text-primary-dark [&::-webkit-details-marker]:hidden"
          >
            <ChevronDown size={14} aria-hidden className="transition group-open:rotate-180" />
            Requisitos que debes comprobar ({p.requisitos.length})
          </summary>
          <ul className="mt-2 list-disc space-y-1.5 pl-5 text-[12.5px] leading-snug text-ink-soft">
            {p.requisitos.map((r, i) => (
              <li key={i}>
                <Quoted text={r} />
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[12px] leading-snug text-muted">{p.explicacion}</p>
        </details>
        <p className="mt-2 text-[11.5px] leading-snug text-muted">
          <span className="font-semibold">Base legal:</span> {p.referencia}
          {!p.verificado && " · cifras pendientes de contrastar con el BOE"}
        </p>
      </li>
    );
  }

  function noDisponible(nd: PalancaNoDisponible) {
    return (
      <li key={`${nd.id}-${nd.causa}`} className="rounded-xl border border-line bg-surface px-3.5 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-[13.5px] font-semibold text-ink">{nd.nombre}</p>
          <Badge className={nd.causa === "faltan_datos" ? "border-primary/40 bg-primary-soft text-primary-dark" : "border-grounding-abstain-bd bg-grounding-abstain-bg text-grounding-abstain-fg"}>{CAUSA_LABEL[nd.causa]}</Badge>
        </div>
        <p className="mt-1 text-[12.5px] leading-snug text-ink-soft">
          <Quoted text={nd.detalle} />
        </p>
        {nd.datos_que_faltan.length > 0 && completeButtons(nd.datos_que_faltan)}
      </li>
    );
  }

  function planTab() {
    if (!computed) {
      return (
        <div className="rounded-xl border border-dashed border-line px-4 py-6 text-center">
          <p className="text-[13.5px] text-ink-soft">{loading ? "Calculando tu plan…" : "Aún no has calculado tu plan."}</p>
          {!loading && (
            <button type="button" onClick={goToData} className="mt-2 inline-flex items-center gap-1 text-[13px] font-semibold text-primary-dark hover:underline">
              Revisa tus datos y pulsa «Calcular mi plan»
              <ArrowRight size={14} aria-hidden />
            </button>
          )}
        </div>
      );
    }
    const { plan } = computed;
    const ahorro = Number(plan.ahorro_total);
    const interaccion = Number(plan.interaccion);
    const diferidas = plan.palancas.filter((p) => p.naturaleza === "diferimiento");
    const gastos = plan.palancas.filter((p) => p.naturaleza === "requiere_gastar");
    const simulador = planEnSimulador(plan, () => 0);
    const changesOpen = sections.cambios ?? true;
    const sec = (name: string, fallback = false) => sections[name] ?? fallback;
    return (
      <div className="space-y-5">
        {stale && (
          <div role="status" className="sticky top-0 z-10 rounded-xl border border-grounding-uncited-bd bg-grounding-uncited-bg px-4 py-3 shadow-pop">
            <p className="flex gap-2 text-[13px] font-semibold text-grounding-uncited-fg">
              <TriangleAlert size={16} aria-hidden className="mt-0.5 flex-shrink-0" />
              Plan desactualizado: has cambiado datos desde que se calculó.
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => void calcular()}
                disabled={loading}
                className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-[13px] font-semibold text-white transition hover:bg-primary-hover disabled:opacity-70"
              >
                {loading ? <Loader2 size={14} aria-hidden className="animate-spin" /> : <RotateCcw size={14} aria-hidden />}
                {loading ? "Calculando…" : "Recalcular"}
              </button>
              <button type="button" onClick={goToData} className="rounded-lg px-3 py-1.5 text-[13px] font-semibold text-ink-soft hover:bg-line/60">
                Ver mis datos
              </button>
            </div>
          </div>
        )}

        <div className={clsx("space-y-5 transition-opacity", (stale || loading) && "opacity-50")} aria-busy={loading || undefined}>
          <section
            ref={headlineRef}
            tabIndex={-1}
            aria-labelledby="opt-headline"
            className="scroll-mt-4 rounded-xl border border-line bg-surface px-4 py-4 shadow-pop focus:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          >
            <p className="text-[12px] font-semibold uppercase tracking-[0.1em] text-muted">
              Tu plan para el IRPF {plan.ejercicio}
              {stale && <span className="ml-1 normal-case tracking-normal text-grounding-uncited-fg">· desactualizado</span>}
            </p>
            <h3 id="opt-headline" className={clsx("mt-1 font-serif font-bold leading-tight", ahorro > 0 ? "text-[28px] text-grounding-cited-fg" : "text-[21px] text-ink")}>
              {ahorro > 0 ? `Puedes ahorrar ${eur(plan.ahorro_total)}` : "Con estos datos no hay nada que mejorar"}
            </h3>
            <p className="mt-1 text-[13px] text-ink-soft">
              {ahorro > 0
                ? `en tu IRPF de ${plan.ejercicio}, frente a lo que pagarías ahora`
                : plan.fase === "solo_opciones_de_declaracion"
                  ? "El ejercicio ya ha terminado y ninguna opción de la declaración lo reduce."
                  : "Ninguna combinación de las opciones disponibles reduce tu impuesto."}
            </p>
            <dl className="mt-3 grid gap-2 text-[13px] sm:grid-cols-2">
              <div className="rounded-lg bg-paper px-3 py-2">
                <dt className="text-[11.5px] text-muted">Ahora</dt>
                <dd className="font-semibold text-ink">{resultadoTexto(plan.base.resultado_total)}</dd>
                <dd className="block text-[12px] leading-snug text-ink-soft">{tributacionTexto(plan.base)}</dd>
              </div>
              <div className="rounded-lg bg-paper px-3 py-2">
                <dt className="text-[11.5px] text-muted">Con el plan</dt>
                <dd className="font-semibold text-ink">{resultadoTexto(plan.optimo.resultado_total)}</dd>
                <dd className="block text-[12px] leading-snug text-ink-soft">{tributacionTexto(plan.optimo)}</dd>
              </div>
            </dl>
            {(diferidas.length > 0 || gastos.length > 0) && (
              <p className="mt-3 flex gap-2 rounded-lg border border-grounding-uncited-bd bg-grounding-uncited-bg px-3 py-2 text-[12.5px] leading-snug text-ink-soft">
                <Info size={15} aria-hidden className="mt-0.5 flex-shrink-0 text-grounding-uncited-fg" />
                <span>
                  <span className="font-semibold text-ink">No todo es ahorro definitivo.</span>
                  {diferidas.length > 0 && (
                    <>
                      {" "}
                      {joinList(diferidas.map((p) => `«${p.nombre}»`))} {diferidas.length === 1 ? "es un diferimiento" : "son diferimientos"}: pagas menos este año, pero ese impuesto
                      puede llegar más adelante.
                    </>
                  )}
                  {gastos.length > 0 && (
                    <>
                      {" "}
                      {joinList(gastos.map((p) => `«${p.nombre}»`))} exige gastar dinero
                      {gastos.some((p) => p.coste_neto !== null) &&
                        ` (coste neto: ${gastos
                          .filter((p) => p.coste_neto !== null)
                          .map((p) => eur(p.coste_neto ?? 0))
                          .join(" + ")})`}
                      .
                    </>
                  )}
                </span>
              </p>
            )}
            {plan.fase === "solo_opciones_de_declaracion" && ahorro > 0 && (
              <p className="mt-2 text-[12.5px] leading-snug text-ink-soft">El ejercicio ya ha terminado: solo quedan las opciones de la declaración.</p>
            )}
            <p className="mt-3 text-[11.5px] leading-snug text-muted">
              {plan.busqueda.metodo === "exhaustiva"
                ? `Búsqueda exhaustiva: ${plan.busqueda.evaluaciones.toLocaleString("es-ES")} cálculos de la declaración, todas las combinaciones de candidatos. Plan para el ${formatDate(plan.fecha)}.`
                : `Plan para el ${formatDate(plan.fecha)}.`}
            </p>
          </section>

          {plan.busqueda.metodo === "heuristica" && (
            <p role="note" className="flex gap-2 rounded-xl border border-grounding-uncited-bd bg-grounding-uncited-bg px-4 py-3 text-[13px] leading-snug text-ink-soft">
              <TriangleAlert size={16} aria-hidden className="mt-0.5 flex-shrink-0 text-grounding-uncited-fg" />
              <span>
                <span className="font-semibold text-ink">Búsqueda aproximada ({plan.busqueda.evaluaciones.toLocaleString("es-ES")} cálculos).</span> {plan.busqueda.nota}
              </span>
            </p>
          )}

          {plan.palancas.length > 1 && (interaccion !== 0 || plan.explicacion_interaccion.length > 0) && (
            <section aria-labelledby="opt-interaccion" className="rounded-xl border-2 border-primary/40 bg-primary-soft px-4 py-3.5">
              <h3 id="opt-interaccion" className="text-[14px] font-bold text-primary-dark">
                {interaccion > 0
                  ? `Juntas valen ${eur(interaccion)} más que por separado`
                  : interaccion < 0
                    ? `Juntas valen ${eur(-interaccion)} menos que por separado`
                    : "Cómo se combinan las medidas"}
              </h3>
              <p className="mt-1 text-[12.5px] leading-snug text-ink">
                Las medidas se influyen entre sí, así que sus ahorros no se suman. Calculada cada una por separado, la suma daría {eur(plan.suma_ahorros_aislados)}; eso{" "}
                <strong>no</strong> es lo que ahorras. El ahorro real del plan es {eur(plan.ahorro_total)}.
              </p>
              {plan.explicacion_interaccion.length > 0 && (
                <ul className="mt-2 list-disc space-y-1 pl-5 text-[12.5px] leading-snug text-ink">
                  {plan.explicacion_interaccion.map((line, i) => (
                    <li key={i}>{line}</li>
                  ))}
                </ul>
              )}
            </section>
          )}

          {plan.datos_que_faltan.length > 0 && (
            <section aria-labelledby="opt-faltan" className="rounded-xl border border-line bg-surface px-4 py-3">
              <h3 id="opt-faltan" className="text-[13.5px] font-semibold text-ink">
                Faltan datos que pueden cambiar el plan
              </h3>
              <p className="mt-0.5 text-[12.5px] leading-snug text-ink-soft">Complétalos y recalcula: con ellos el plan puede ser otro.</p>
              {completeButtons(plan.datos_que_faltan)}
            </section>
          )}

          {plan.palancas.length > 0 && (
            <section aria-labelledby="opt-que-hacer">
              <h3 id="opt-que-hacer" className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">
                Qué hacer
              </h3>
              {plan.palancas.length > 1 && (
                <p className="mb-2.5 text-[12.5px] leading-snug text-ink-soft">
                  «Ahorro dentro del plan» es lo que pagarías de más si quitaras solo esa medida. Estas cifras tampoco se suman entre sí: el ahorro del plan completo es{" "}
                  {eur(plan.ahorro_total)}.
                </p>
              )}
              <ol className="space-y-3">{plan.palancas.map(leverCard)}</ol>
            </section>
          )}

          {plan.oportunidades.length > 0 && (
            <section aria-labelledby="opt-oportunidades" className="rounded-xl border border-grounding-cited-bd bg-grounding-cited-bg px-4 py-3">
              <h3 id="opt-oportunidades" className="text-[13.5px] font-semibold text-grounding-cited-fg">
                Oportunidades
              </h3>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-[12.5px] leading-snug text-ink-soft">
                {plan.oportunidades.map((o, i) => (
                  <li key={i}>
                    <Quoted text={o} />
                  </li>
                ))}
              </ul>
            </section>
          )}

          {plan.no_disponibles.length > 0 && (
            <section aria-labelledby="opt-no-disponibles">
              <h3 id="opt-no-disponibles" className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">
                Lo que no se ha podido usar
              </h3>
              <ul className="space-y-2">{plan.no_disponibles.map(noDisponible)}</ul>
            </section>
          )}

          {plan.cambios_traza.length > 0 && (
            <Collapsible open={changesOpen} onToggle={() => toggle("cambios")} title="Qué cambia en tu declaración">
              <table className="w-full text-[12.5px]">
                <caption className="sr-only">Líneas del cálculo que cambian, sumadas para el hogar</caption>
                <thead>
                  <tr className="text-left text-[11px] uppercase tracking-[0.08em] text-muted">
                    <th scope="col" className="pb-1 pr-2 font-semibold">
                      Concepto
                    </th>
                    <th scope="col" className="pb-1 pr-2 text-right font-semibold">
                      Ahora
                    </th>
                    <th scope="col" className="pb-1 text-right font-semibold">
                      Con el plan
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {plan.cambios_traza.map((c, i) => (
                    <tr key={i} className="border-t border-line align-top">
                      <td className="py-1.5 pr-2 text-ink-soft [overflow-wrap:anywhere]">
                        {c.concepto}
                        {c.referencia && <span className="block text-[11px] text-muted">{c.referencia}</span>}
                      </td>
                      <td className="whitespace-nowrap py-1.5 pr-2 text-right font-mono tabular-nums text-ink-soft">{eur(c.antes)}</td>
                      <td className="whitespace-nowrap py-1.5 text-right font-mono font-semibold tabular-nums text-ink">{eur(c.despues)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {plan.con_conyuge && <p className="mt-2 text-[11.5px] leading-snug text-muted">Con dos declaraciones, cada línea es la suma de las dos.</p>}
            </Collapsible>
          )}

          {plan.supuestos.length > 0 && (
            <Collapsible tone="warn" open={sec("supuestos", true)} onToggle={() => setSections((prev) => ({ ...prev, supuestos: !sec("supuestos", true) }))} title={`Lo que se ha supuesto (${plan.supuestos.length})`}>
              <ul className="list-disc space-y-1.5 pl-4 text-[12.5px] leading-snug text-ink-soft">
                {plan.supuestos.map((s, i) => (
                  <li key={i}>
                    <Quoted text={s} />
                  </li>
                ))}
              </ul>
            </Collapsible>
          )}

          {plan.avisos.length > 0 && (
            <Collapsible tone="warn" open={sec("avisos", true)} onToggle={() => setSections((prev) => ({ ...prev, avisos: !sec("avisos", true) }))} title={`Qué debes saber de este cálculo (${plan.avisos.length})`}>
              <ul className="list-disc space-y-1.5 pl-4 text-[12.5px] leading-snug text-ink-soft">
                {plan.avisos.map((a, i) => (
                  <li key={i}>
                    <Quoted text={a} />
                  </li>
                ))}
              </ul>
            </Collapsible>
          )}

          {plan.descartadas.length > 0 && (
            <Collapsible open={sec("descartadas")} onToggle={() => toggle("descartadas")} title={`Por qué no te propongo… (${plan.descartadas.length})`}>
              <ul className="space-y-2.5">
                {plan.descartadas.map((d) => (
                  <li key={d.id} className="text-[12.5px] leading-snug">
                    <p className="font-semibold text-ink">{d.nombre}</p>
                    <p className="text-ink-soft">{d.motivo}</p>
                    <p className="text-[11.5px] text-muted">{d.referencia}</p>
                  </li>
                ))}
              </ul>
            </Collapsible>
          )}

          <Collapsible open={sec("explicacion")} onToggle={() => toggle("explicacion")} title="La explicación completa">
            <p className="whitespace-pre-line text-[12.5px] leading-relaxed text-ink-soft">{plan.explicacion}</p>
          </Collapsible>

          {(plan.textos_legales.length > 0 || plan.citas.length > 0) && (
            <Collapsible
              open={sec("fuentes")}
              onToggle={() => toggle("fuentes")}
              title={
                <span className="inline-flex items-center gap-1.5">
                  <BookOpen size={14} aria-hidden />
                  Textos legales y fuentes
                </span>
              }
            >
              {plan.textos_legales.length > 0 && (
                <ul className="space-y-2.5">
                  {plan.textos_legales.map((t) => {
                    const url = boeUrl(t.boe_id);
                    return (
                      <li key={t.referencia} className="text-[12.5px] leading-snug">
                        <p className="font-semibold text-ink">
                          {t.referencia}{" "}
                          {url ? (
                            <a href={url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 font-normal text-primary-dark underline underline-offset-2 hover:no-underline">
                              {t.boe_id}
                              <ExternalLink size={11} aria-hidden />
                              <span className="sr-only"> (se abre en una pestaña nueva)</span>
                            </a>
                          ) : (
                            <span className="font-normal text-muted">{t.boe_id}</span>
                          )}
                        </p>
                        <blockquote className="mt-0.5 border-l-2 border-line pl-2.5 italic text-ink-soft">«{t.texto}»</blockquote>
                      </li>
                    );
                  })}
                </ul>
              )}
              {plan.citas.length > 0 && (
                <>
                  <p className="mb-1 mt-3 text-[11px] font-semibold uppercase tracking-[0.1em] text-muted">Referencias del cálculo</p>
                  <ul className="list-disc space-y-1 pl-4 text-[12px] leading-snug text-ink-soft">
                    {plan.citas.map((c, i) => (
                      <li key={i} className="[overflow-wrap:anywhere]">
                        {c}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </Collapsible>
          )}

          <Collapsible open={sec("calculo")} onToggle={() => toggle("calculo")} title="El cálculo completo de cada declaración">
            <div className="space-y-4">
              {plan.base.declaraciones.map((d) => (
                <Trace key={`base-${d.titular}`} declaracion={d} title={`Ahora · ${declaracionTitulo(plan.base, d)}`} />
              ))}
              {plan.optimo.declaraciones.map((d) => (
                <Trace key={`opt-${d.titular}`} declaracion={d} title={`Con el plan · ${declaracionTitulo(plan.optimo, d)}`} />
              ))}
            </div>
          </Collapsible>

          {onUseInSimulator && plan.palancas.length > 0 && (
            <div className="rounded-xl border border-line bg-surface px-4 py-3">
              {simulador.ok ? (
                <>
                  <button
                    type="button"
                    onClick={sendToSimulator}
                    disabled={stale || loading}
                    className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-[13px] font-semibold text-white transition hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Usar este plan en el simulador
                    <ArrowRight size={14} aria-hidden />
                  </button>
                  <p className="mt-1.5 text-[12px] leading-snug text-muted">
                    {stale ? "Recalcula el plan antes de llevarlo al simulador." : "Abre el simulador con las cifras del plan, para ver la declaración con detalle o probar variaciones."}
                  </p>
                </>
              ) : (
                <p className="flex gap-1.5 text-[12.5px] leading-snug text-ink-soft">
                  <Info size={14} aria-hidden className="mt-0.5 flex-shrink-0 text-muted" />
                  {simulador.motivo}
                </p>
              )}
            </div>
          )}

          <p className="text-[11.5px] leading-relaxed text-muted [overflow-wrap:anywhere]">
            Cada cifra sale de la calculadora del IRPF con los parámetros del ejercicio {plan.ejercicio}
            {plan.optimo.declaraciones[0] ? `: ${plan.optimo.declaraciones[0].resultado.fuente}` : ""}.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <button type="button" aria-label="Cerrar el optimizador" className="absolute inset-0 bg-ink/40" onClick={onClose} />
      <aside role="dialog" aria-modal="true" aria-labelledby="opt-title" className="relative flex h-full w-full max-w-[560px] flex-col border-l border-line bg-paper shadow-pop">
        <header className="border-b border-line px-4 pt-4 sm:px-5">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <h2 id="opt-title" className="flex items-center gap-2 font-serif text-[19px] font-bold text-ink">
                <PiggyBank size={18} aria-hidden className="flex-shrink-0 text-primary-dark" />
                Optimizar mis impuestos
              </h2>
              <p className="text-[12.5px] leading-snug text-muted">La mejor combinación de lo que la ley te deja elegir, para tu IRPF de {computed?.plan.ejercicio ?? year}</p>
            </div>
            <button type="button" onClick={onClose} aria-label="Cerrar" className="rounded-lg p-2 text-muted hover:bg-line/60 hover:text-ink">
              <X size={18} />
            </button>
          </div>
          <div role="tablist" aria-label="Optimizador" className="mt-3 flex gap-1">
            {(
              [
                ["datos", "1. Tus datos"],
                ["plan", "2. Tu plan"],
              ] as Array<[Tab, string]>
            ).map(([id, label]) => (
              <button
                key={id}
                type="button"
                role="tab"
                id={`opt-tab-${id}`}
                aria-selected={tab === id}
                aria-controls="opt-tabpanel"
                onClick={() => showTab(id)}
                onKeyDown={(e) => {
                  if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
                    const next: Tab = id === "datos" ? "plan" : "datos";
                    showTab(next);
                    document.getElementById(`opt-tab-${next}`)?.focus();
                  }
                }}
                tabIndex={tab === id ? 0 : -1}
                className={clsx(
                  "-mb-px inline-flex items-center gap-1.5 whitespace-nowrap border-b-2 px-3 py-2 text-[13px] font-semibold transition",
                  tab === id ? "border-primary text-primary-dark" : "border-transparent text-ink-soft hover:text-ink",
                )}
              >
                {label}
                {id === "plan" && computed && !stale && (
                  <>
                    <Check size={13} aria-hidden className="text-grounding-cited-fg" />
                    <span className="sr-only">(calculado)</span>
                  </>
                )}
                {id === "plan" && stale && <span className="rounded-pill bg-grounding-uncited-bg px-1.5 text-[11px] font-semibold text-grounding-uncited-fg">desactualizado</span>}
              </button>
            ))}
          </div>
        </header>

        <div ref={scrollRef} role="tabpanel" id="opt-tabpanel" aria-labelledby={`opt-tab-${tab}`} className="flex-1 overflow-y-auto overflow-x-hidden px-4 pt-4 sm:px-5">
          {error && tab === "plan" && (
            <p id="opt-error" role="alert" className="mb-4 flex gap-2 rounded-lg border border-risk-high-fg/30 bg-risk-high-bg px-3 py-2 text-[13px] text-ink-soft">
              <TriangleAlert size={15} aria-hidden className="mt-0.5 flex-shrink-0 text-risk-high-fg" />
              <span>
                {error}
                {computed && <span className="block text-[12px] text-muted">El plan de la pestaña «Tu plan» es el del último cálculo que salió bien.</span>}
              </span>
            </p>
          )}
          {loading && (
            <p role="status" className="sr-only">
              Calculando la mejor combinación…
            </p>
          )}
          <div key={tab} className={clsx(tab === "plan" && "pb-6")}>
            {tab === "datos" ? datosTab() : planTab()}
          </div>
        </div>

        <footer className="border-t border-line px-4 py-2.5 text-[11.5px] leading-snug text-muted sm:px-5">
          Cálculo determinista, sin inteligencia artificial. No sustituye al borrador de la Agencia Tributaria.
        </footer>
      </aside>
    </div>
  );
}
