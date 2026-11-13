// Self-employed panel: annual net income of the activity, the quarterly IRPF
// payment (modelo 130) and the quarterly VAT return (modelo 303). Everything
// is the deterministic calculators behind POST /autonomos/* — no model call,
// so it recomputes as the user types. Same visual language as SimulatorPanel.

import { ArrowRight, BriefcaseBusiness, ChevronDown, RotateCcw, Save, TriangleAlert, X } from "lucide-react";
import clsx from "clsx";
import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import type { CalcLine, Fact } from "../api";
import { ApiError } from "../api";
import type { ActividadPerfil, ResultadoActividad, ResultadoModelo130, ResultadoModelo303, Trimestre } from "../apiAutonomos";
import { calcularActividad, calcularActividadDelExpediente, calcularModelo130, calcularModelo303, getEjerciciosAutonomos } from "../apiAutonomos";
import { formatMoney } from "../lib/presentation";

type Tab = "actividad" | "m130" | "m303";
const TABS: Array<{ id: Tab; label: string }> = [
  { id: "actividad", label: "Mi actividad" },
  { id: "m130", label: "Pago trimestral (130)" },
  { id: "m303", label: "IVA trimestral (303)" },
];
const TRIMESTRES: Array<{ value: Trimestre; label: string; meses: string; hasta: string }> = [
  { value: 1, label: "1.º", meses: "enero a marzo", hasta: "31 de marzo" },
  { value: 2, label: "2.º", meses: "abril a junio", hasta: "30 de junio" },
  { value: 3, label: "3.º", meses: "julio a septiembre", hasta: "30 de septiembre" },
  { value: 4, label: "4.º", meses: "octubre a diciembre", hasta: "31 de diciembre" },
];

interface FieldDef {
  name: string;
  label: string;
  hint?: string;
}

const ACTIVIDAD_INGRESOS: FieldDef[] = [
  { name: "ingresos_integros", label: "Ingresos del año", hint: "Todo lo facturado en el año, sin IVA y antes de retenciones" },
];
const ACTIVIDAD_GASTOS: FieldDef[] = [
  { name: "seguridad_social_autonomo", label: "Cuota de autónomos", hint: "Lo pagado a la Seguridad Social (RETA) en el año" },
  { name: "suministros", label: "Suministros", hint: "Luz, agua, gas, teléfono e internet de la actividad" },
  { name: "alquileres", label: "Alquiler del local u oficina" },
  { name: "servicios_profesionales", label: "Gestoría y otros profesionales" },
  { name: "amortizaciones", label: "Amortizaciones", hint: "La parte del año de equipos, vehículos, mobiliario…" },
  { name: "otros_gastos", label: "Otros gastos", hint: "Material, seguros, software, publicidad, viajes…" },
];
const ACTIVIDAD_IRREGULARES: FieldDef = {
  name: "rendimientos_irregulares",
  label: "Beneficio extraordinario de varios años",
  hint: "La parte del beneficio generada durante más de dos años o por una indemnización por cese, una subvención de capital o un premio. Tiene una reducción propia.",
};
// Only asked when there is no case to take them from.
const ACTIVIDAD_MAS: FieldDef[] = [
  { name: "otras_rentas_no_exentas", label: "Otros ingresos del año", hint: "Sueldo, alquileres, intereses… Déjalo vacío si no lo sabes: no se aplicarán las reducciones que dependen de ello" },
  { name: "reduccion_rendimientos_trabajo", label: "Reducción por rendimientos del trabajo", hint: "Solo si además tienes un sueldo bajo y conoces la reducción que se le aplica" },
  { name: "cifra_negocios_ano_anterior", label: "Facturación del año anterior", hint: "Para comprobar que puedes usar la modalidad simplificada" },
];
const M130_FIELDS: FieldDef[] = [
  { name: "ingresos_acumulados", label: "Ingresos acumulados", hint: "Sin IVA" },
  { name: "gastos_acumulados", label: "Gastos acumulados", hint: "Sin IVA; no añadas los gastos de difícil justificación, los suma el cálculo" },
  { name: "retenciones_acumuladas", label: "Retenciones acumuladas", hint: "El IRPF que tus clientes te han descontado en las facturas" },
];
const M303_VENTAS: FieldDef[] = [
  { name: "base_tipo_general", label: "Ventas al tipo general", hint: "Base (sin IVA) de lo facturado con el tipo general" },
  { name: "base_tipo_reducido", label: "Ventas al tipo reducido", hint: "Base (sin IVA)" },
  { name: "base_tipo_superreducido", label: "Ventas al tipo superreducido", hint: "Base (sin IVA)" },
];
const M303_COMPRAS: FieldDef[] = [
  { name: "cuotas_soportadas_deducibles", label: "IVA de tus gastos", hint: "La cuota de IVA (no la base) de las facturas de gastos corrientes" },
  { name: "cuotas_soportadas_bienes_inversion", label: "IVA de bienes de inversión", hint: "La cuota de IVA de equipos, vehículos, maquinaria…" },
  { name: "cuotas_a_compensar_periodos_anteriores", label: "Saldo a tu favor de trimestres anteriores", hint: "Lo que quedó «a compensar» en declaraciones anteriores" },
];

// Panel field -> the case fact that stores it (hacienda_gpt/tax/profile.py: ACTIVIDAD_FACT_FIELDS).
const FACT_OF: Record<string, string> = {
  ...Object.fromEntries([...ACTIVIDAD_INGRESOS, ...ACTIVIDAD_GASTOS].map((f) => [f.name, `actividad_${f.name}`])),
  rendimientos_irregulares: "actividad_rendimientos_irregulares",
};
const FLAG_FACT_OF: Record<string, string> = {
  cumple_requisitos_reduccion_dependiente: "actividad_cumple_requisitos_dependiente",
  reduccion_inicio_actividad: "actividad_reduccion_inicio",
};
const CEUTA_FACT = "actividad_ceuta_melilla";
const NET_FACT = "rendimiento_actividad_neto";

type Values = Record<string, string>;
type Flags = Record<string, boolean>;

function parseNumber(raw: string | undefined): number | null {
  const cleaned = (raw ?? "").trim().replace(/[€\s]/g, "").replace(/\.(?=\d{3}(\D|$))/g, "").replace(",", ".");
  if (!cleaned || cleaned === "-") return null;
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : null;
}

/** Only the figures the user typed: an empty box is "not given", not zero. */
function numbers(values: Values, names: string[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const name of names) {
    const n = parseNumber(values[name]);
    if (n !== null) out[name] = n;
  }
  return out;
}

/** Debounced, last-request-wins call to one calculator. `bodyKey` is the JSON body; null = do not call. */
function useCalculo<T>(active: boolean, bodyKey: string | null, run: (body: unknown, signal: AbortSignal) => Promise<T>) {
  const [result, setResult] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const runRef = useRef(run);
  useEffect(() => {
    runRef.current = run;
  });
  useEffect(() => {
    if (!active || bodyKey === null) return;
    const mine = ++seq.current;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      runRef.current(JSON.parse(bodyKey) as unknown, controller.signal).then(
        (value) => {
          if (mine !== seq.current) return;
          setResult(value);
          setError(null);
        },
        (err: unknown) => {
          if (controller.signal.aborted || mine !== seq.current) return;
          setError(err instanceof ApiError ? err.message : "No se pudo hacer el cálculo.");
        },
      );
    }, 300);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [active, bodyKey]);
  return { result, error };
}

function MoneyField({ field, values, onChange }: { field: FieldDef; values: Values; onChange: (name: string, value: string) => void }) {
  return (
    <label className="block">
      <span className="text-[12.5px] text-ink-soft">{field.label}</span>
      <span className="relative mt-1 block">
        <input
          value={values[field.name] ?? ""}
          onChange={(e) => onChange(field.name, e.target.value)}
          inputMode="decimal"
          placeholder="0"
          className="w-full rounded-lg border border-line bg-surface px-3 py-2 pr-7 text-[16px] text-ink focus:border-primary focus:outline-none sm:text-[14px]"
        />
        <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-[13px] text-muted">€</span>
      </span>
      {field.hint && <span className="mt-1 block text-[11.5px] leading-snug text-muted">{field.hint}</span>}
    </label>
  );
}

function Flag({ name, label, hint, flags, onChange }: { name: string; label: string; hint?: string; flags: Flags; onChange: (name: string, value: boolean) => void }) {
  return (
    <label className="flex items-start gap-2.5 sm:col-span-2">
      <input type="checkbox" checked={flags[name] ?? false} onChange={(e) => onChange(name, e.target.checked)} className="mt-0.5 h-4 w-4 accent-[rgb(var(--primary))]" />
      <span>
        <span className="text-[13.5px] text-ink">{label}</span>
        {hint && <span className="block text-[11.5px] leading-snug text-muted">{hint}</span>}
      </span>
    </label>
  );
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <fieldset>
      <legend className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">{title}</legend>
      <div className="grid gap-3 sm:grid-cols-2">{children}</div>
    </fieldset>
  );
}

function Optional({ title, children }: { title: string; children: ReactNode }) {
  return (
    <details className="group rounded-xl border border-line bg-surface">
      <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-2.5 text-[13px] font-semibold text-ink-soft [&::-webkit-details-marker]:hidden">
        {title}
        <ChevronDown size={16} aria-hidden className="text-muted transition group-open:rotate-180" />
      </summary>
      <div className="grid gap-3 border-t border-line px-4 py-3 sm:grid-cols-2">{children}</div>
    </details>
  );
}

function QuarterPicker({ value, onChange, cumulative }: { value: Trimestre; onChange: (value: Trimestre) => void; cumulative: boolean }) {
  const current = TRIMESTRES[value - 1];
  return (
    <div>
      <p className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">Trimestre</p>
      <div role="radiogroup" aria-label="Trimestre" className="grid grid-cols-4 gap-2">
        {TRIMESTRES.map((t) => (
          <button
            key={t.value}
            type="button"
            role="radio"
            aria-checked={t.value === value}
            onClick={() => onChange(t.value)}
            className={clsx(
              "rounded-lg border px-2 py-2 text-[13.5px] font-semibold transition",
              t.value === value ? "border-primary bg-primary text-white" : "border-line bg-surface text-ink-soft hover:border-primary hover:text-primary-dark",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>
      <p className="mt-1.5 text-[11.5px] leading-snug text-muted">
        {cumulative
          ? `Pon las cifras acumuladas desde el 1 de enero hasta el ${current.hasta}, no solo las del trimestre.`
          : `Pon solo las cifras del trimestre (${current.meses}).`}
      </p>
    </div>
  );
}

function Detail({ lineas, avisos }: { lineas: CalcLine[]; avisos: string[] }) {
  return (
    <>
      {lineas.length > 0 && (
      <details className="group rounded-xl border border-line bg-surface">
        <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-2.5 text-[13px] font-semibold text-ink-soft [&::-webkit-details-marker]:hidden">
          Desglose del cálculo
          <ChevronDown size={16} aria-hidden className="text-muted transition group-open:rotate-180" />
        </summary>
        <table className="w-full border-t border-line text-[12.5px]">
          <tbody>
            {lineas.map((line, i) => (
              <tr key={i} className="border-b border-line last:border-0">
                <td className="px-4 py-1.5 text-ink-soft">
                  {line.concepto}
                  {line.referencia && <span className="block text-[11px] text-muted">{line.referencia}</span>}
                </td>
                <td className="whitespace-nowrap px-4 py-1.5 text-right font-mono tabular-nums text-ink">{formatMoney(line.importe)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
      )}
      {avisos.length > 0 && (
        <details open className="group rounded-xl border border-grounding-uncited-bd bg-grounding-uncited-bg">
          <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-2.5 text-[13px] font-semibold text-grounding-uncited-fg [&::-webkit-details-marker]:hidden">
            Qué debes saber de este cálculo ({avisos.length})
            <ChevronDown size={16} aria-hidden className="transition group-open:rotate-180" />
          </summary>
          <ul className="list-disc space-y-1.5 px-4 pb-3 pl-8 text-[12.5px] leading-snug text-ink-soft">
            {avisos.map((aviso, i) => (
              <li key={i}>{aviso}</li>
            ))}
          </ul>
        </details>
      )}
    </>
  );
}

function ResultCard({ title, amount, suffix, good, children }: { title: string; amount: string | number; suffix?: string; good?: boolean; children?: ReactNode }) {
  return (
    <section aria-live="polite" className="z-10 rounded-xl sm:sticky sm:top-0 border border-line bg-surface px-4 py-4 shadow-pop">
      <p className="text-[12px] font-semibold uppercase tracking-[0.1em] text-muted">{title}</p>
      <p className={clsx("mt-1 font-serif text-[30px] font-bold leading-none", good ? "text-grounding-cited-fg" : "text-ink")}>
        {formatMoney(amount)}
        {suffix && <span className="ml-2 text-[14px] font-semibold text-ink-soft">{suffix}</span>}
      </p>
      {children}
    </section>
  );
}

function ErrorNote({ message }: { message: string }) {
  return (
    <p role="alert" className="flex gap-2 rounded-lg border border-risk-high-fg/30 bg-risk-high-bg px-3 py-2 text-[13px] text-ink-soft">
      <TriangleAlert size={15} aria-hidden className="mt-0.5 flex-shrink-0 text-risk-high-fg" />
      {message}
    </p>
  );
}

function CeutaSelect({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <label className="block sm:col-span-2">
      <span className="text-[12.5px] text-ink-soft">¿Desarrollas la actividad en Ceuta o Melilla?</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-[16px] text-ink focus:border-primary focus:outline-none sm:text-[14px]"
      >
        <option value="">No</option>
        <option value="ceuta">Sí, en Ceuta</option>
        <option value="melilla">Sí, en Melilla</option>
      </select>
      <span className="mt-1 block text-[11.5px] leading-snug text-muted">
        Solo si sus rendimientos tienen derecho a la deducción por rentas obtenidas en Ceuta o Melilla. Cambia los pagos trimestrales y, según el año, los gastos de difícil justificación.
      </span>
    </label>
  );
}

interface AutonomosPanelProps {
  open: boolean;
  onClose: () => void;
  /** "Usar en mi simulación": hands over the annual net income of the activity, in euros. */
  onUse: (net: number) => void;
  /** Fiscal year of the conversation ("2026"); used when the calculators have parameters for it. */
  taxPeriod?: string | null;
  /** With a case, its facts prefill "Mi actividad" and its other income decides the reductions. */
  caseId?: string | null;
  facts?: Fact[];
  /** Persist one fact to the case (null deletes it). Resolves true when saved. */
  onSaveFact?: (factName: string, value: string | number | boolean | null) => Promise<boolean>;
  busy?: boolean;
}

const NO_FACTS: Fact[] = [];

export function AutonomosPanel({ open, onClose, onUse, taxPeriod = null, caseId = null, facts = NO_FACTS, onSaveFact, busy = false }: AutonomosPanelProps) {
  const [tab, setTab] = useState<Tab>("actividad");
  const [ejercicios, setEjercicios] = useState<number[] | null>(null);
  const [ejercicioEdit, setEjercicioEdit] = useState<number | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // One set of typed figures per tab, kept while the panel stays mounted.
  const [actividad, setActividad] = useState<Values>({});
  const [actividadFlags, setActividadFlags] = useState<Flags>({});
  const [discapacidad, setDiscapacidad] = useState("no");
  // null = whatever the case says.
  const [ceutaEdit, setCeutaEdit] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [m130, setM130] = useState<Values>({});
  const [m130Flags, setM130Flags] = useState<Flags>({ aplicar_gastos_dificil_justificacion: true });
  const [trimestre130, setTrimestre130] = useState<Trimestre>(1);
  const [m303, setM303] = useState<Values>({});
  const [trimestre303, setTrimestre303] = useState<Trimestre>(1);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  useEffect(() => {
    if (!open || ejercicios !== null) return;
    const controller = new AbortController();
    getEjerciciosAutonomos(controller.signal).then(
      (years) => {
        setEjercicios(years);
        setLoadError(years.length ? null : "No hay ningún ejercicio disponible para estos cálculos.");
      },
      (err: unknown) => {
        if (controller.signal.aborted) return;
        setLoadError(err instanceof ApiError ? err.message : "No se pudo conectar con el servicio de cálculo.");
      },
    );
    return () => controller.abort();
  }, [open, ejercicios]);

  // What the case already holds about the activity: typed figures are edits over it.
  const saved = useMemo(() => {
    const byName = new Map(facts.map((fact) => [fact.name, fact.value]));
    const values: Values = {};
    for (const [field, fact] of Object.entries(FACT_OF)) {
      const value = byName.get(fact);
      if (typeof value === "number") values[field] = String(value);
    }
    const flags: Flags = {};
    for (const [field, fact] of Object.entries(FLAG_FACT_OF)) {
      const value = byName.get(fact);
      if (typeof value === "boolean") flags[field] = value;
    }
    const ciudad = byName.get(CEUTA_FACT);
    const profesional = byName.get("actividad_profesional");
    return {
      values,
      flags,
      ceuta: typeof ciudad === "string" && ["ceuta", "melilla"].includes(ciudad.trim().toLowerCase()) ? ciudad.trim().toLowerCase() : "",
      profesional: typeof profesional === "boolean" ? profesional : null,
      hasNet: byName.has(NET_FACT),
    };
  }, [facts]);
  const conExpediente = Boolean(caseId);
  const actividadValues: Values = { ...saved.values, ...actividad };
  const actividadFlagValues: Flags = { ...saved.flags, ...actividadFlags };
  const ceuta = ceutaEdit ?? saved.ceuta;

  const wanted = taxPeriod ? Number(taxPeriod.slice(0, 4)) : null;
  const ejercicio = ejercicios?.length ? (ejercicioEdit ?? (wanted !== null && ejercicios.includes(wanted) ? wanted : ejercicios[ejercicios.length - 1])) : null;

  // Without a case: the figures alone, with whatever "other income" the user types.
  const actividadBody =
    ejercicio === null || conExpediente
      ? null
      : JSON.stringify({
          ejercicio,
          ...numbers(actividadValues, [...Object.keys(FACT_OF), ...ACTIVIDAD_MAS.map((f) => f.name)]),
          ...(actividadFlagValues.cumple_requisitos_reduccion_dependiente ? { cumple_requisitos_reduccion_dependiente: true } : {}),
          ...(actividadFlagValues.reduccion_inicio_actividad ? { reduccion_inicio_actividad: true } : {}),
          ...(discapacidad === "si" ? { discapacidad: true } : discapacidad === "ampliada" ? { discapacidad_ampliada: true } : {}),
          ...(ceuta ? { ceuta_melilla: ceuta } : {}),
        });
  // With a case: the same figures as facts layered over the saved ones, so the case's other
  // income (salary, interest…) decides the reductions exactly as the simulator will.
  const cambiosExpediente: Record<string, unknown> = {};
  for (const [field, fact] of Object.entries(FACT_OF)) {
    const n = parseNumber(actividadValues[field]);
    if (n !== null) cambiosExpediente[fact] = n;
    else if (field in saved.values) cambiosExpediente[fact] = 0; // cleared in the form
  }
  for (const [field, fact] of Object.entries(FLAG_FACT_OF)) cambiosExpediente[fact] = actividadFlagValues[field] ?? false;
  if (ceuta || saved.ceuta) cambiosExpediente[CEUTA_FACT] = ceuta;
  const expedienteBody = ejercicio === null || !caseId ? null : JSON.stringify({ caseId, ejercicio, cambios: cambiosExpediente, facts: facts.length });

  // What "Guardar en mi expediente" would write: [fact, value | null to delete].
  const pendientes: Array<[string, string | number | boolean | null]> = [];
  for (const [field, fact] of Object.entries(FACT_OF)) {
    const n = parseNumber(actividadValues[field]);
    const before = field in saved.values ? Number(saved.values[field]) : null;
    if (n !== before) pendientes.push([fact, n]);
  }
  for (const [field, fact] of Object.entries(FLAG_FACT_OF)) {
    const value = actividadFlagValues[field] ?? false;
    if (value !== (saved.flags[field] ?? false)) pendientes.push([fact, value]);
  }
  if (ceuta !== saved.ceuta) pendientes.push([CEUTA_FACT, ceuta || null]);
  const primero130 = trimestre130 === 1;
  const profesional = m130Flags.actividad_profesional ?? saved.profesional ?? true;
  const inicio = m130Flags.inicio_actividad ?? false;
  const m130Body =
    ejercicio === null
      ? null
      : JSON.stringify({
          ejercicio,
          trimestre: trimestre130,
          ...numbers(m130, [
            ...M130_FIELDS.map((f) => f.name),
            "rendimiento_neto_ejercicio_anterior",
            // Nothing comes from "previous quarters" in the first one.
            ...(primero130 ? [] : ["pagos_trimestres_anteriores", "minoracion_pendiente_trimestres_anteriores"]),
            ...(profesional ? (inicio ? ["ingresos_con_retencion_acumulados"] : ["ingresos_ano_anterior", "ingresos_con_retencion_ano_anterior"]) : []),
          ]),
          actividad_profesional: profesional,
          aplicar_gastos_dificil_justificacion: m130Flags.aplicar_gastos_dificil_justificacion ?? true,
          ...(profesional && inicio ? { inicio_actividad: true } : {}),
          ...(ceuta ? { ceuta_melilla: ceuta } : {}),
        });
  const m303Body =
    ejercicio === null ? null : JSON.stringify({ ejercicio, trimestre: trimestre303, ...numbers(m303, [...M303_VENTAS, ...M303_COMPRAS].map((f) => f.name)) });

  const calcActividad = useCalculo<ResultadoActividad>(open && tab === "actividad", actividadBody, (body, signal) => calcularActividad(body as Parameters<typeof calcularActividad>[0], signal));
  const calcExpediente = useCalculo<ActividadPerfil | null>(open && tab === "actividad", expedienteBody, (body, signal) => {
    const { caseId: id, ejercicio: year, cambios } = body as { caseId: string; ejercicio: number; cambios: Record<string, unknown> };
    return calcularActividadDelExpediente({ caseId: id, ejercicio: year, cambios, signal });
  });
  const calc130 = useCalculo<ResultadoModelo130>(open && tab === "m130", m130Body, (body, signal) => calcularModelo130(body as Parameters<typeof calcularModelo130>[0], signal));
  const calc303 = useCalculo<ResultadoModelo303>(open && tab === "m303", m303Body, (body, signal) => calcularModelo303(body as Parameters<typeof calcularModelo303>[0], signal));

  if (!open) return null;

  const setIn = (setter: (update: (prev: Values) => Values) => void) => (name: string, value: string) => setter((prev) => ({ ...prev, [name]: value }));
  const setFlagIn = (setter: (update: (prev: Flags) => Flags) => void) => (name: string, value: boolean) => setter((prev) => ({ ...prev, [name]: value }));
  const fields = (defs: FieldDef[], values: Values, setter: (update: (prev: Values) => Values) => void) =>
    defs.map((field) => <MoneyField key={field.name} field={field} values={values} onChange={setIn(setter)} />);

  const perfil = conExpediente ? calcExpediente.result : null;
  const resultadoActividad = conExpediente ? (perfil?.resultado ?? null) : calcActividad.result;
  const current = tab === "actividad" ? (conExpediente ? calcExpediente : calcActividad) : tab === "m130" ? calc130 : calc303;
  const fuente = tab === "actividad" ? resultadoActividad?.fuente : (current.result as { fuente?: string } | null)?.fuente;
  const flagProps = { flags: { ...actividadFlagValues }, onChange: setFlagIn(setActividadFlags) };

  /** Write the typed figures to the case. The breakdown replaces a net figure saved before. */
  async function saveActividad(): Promise<boolean> {
    if (!onSaveFact) return false;
    setSaving(true);
    try {
      for (const [fact, value] of pendientes) {
        if (!(await onSaveFact(fact, value))) return false;
      }
      if (saved.hasNet && parseNumber(actividadValues.ingresos_integros) !== null && !(await onSaveFact(NET_FACT, null))) return false;
      setActividad({});
      setActividadFlags({});
      setCeutaEdit(null);
      return true;
    } finally {
      setSaving(false);
    }
  }

  function actividadTab() {
    const r = resultadoActividad;
    const neto = r ? Number(r.rendimiento_actividad_neto) : 0;
    const hasIncome = parseNumber(actividadValues.ingresos_integros) !== null;
    const canSave = conExpediente && onSaveFact !== undefined;
    const reducciones = r ? Number(r.reduccion_art_32_1) + Number(r.reduccion_art_32_2) + Number(r.reduccion_art_32_3) : 0;
    return (
      <>
        {r && (
          <ResultCard title="Rendimiento neto de tu actividad" amount={r.rendimiento_actividad_neto} suffix={neto < 0 ? "de pérdida" : "al año"}>
            <p className="mt-2 text-[13px] text-ink-soft">
              Ingresos {formatMoney(r.ingresos_integros)} · gastos {formatMoney(r.gastos_deducibles)}
              {Number(r.gastos_dificil_justificacion) > 0 && <> · gastos de difícil justificación {formatMoney(r.gastos_dificil_justificacion)}</>}
              {reducciones > 0 && <> · reducciones {formatMoney(reducciones)}</>}
            </p>
            <p className="mt-1 text-[12.5px] text-muted">Es la cifra por la que tu actividad tributa en el IRPF, antes de sumar tus otros ingresos.</p>
            {perfil?.otras_rentas_no_exentas != null && (
              <p className="mt-1 text-[12.5px] text-muted">
                Se han tenido en cuenta los otros ingresos de tu expediente ({formatMoney(perfil.otras_rentas_no_exentas)}) para ver qué reducciones te corresponden.
              </p>
            )}
            {hasIncome ? (
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={saving || busy}
                  onClick={() => {
                    // With a case the breakdown itself is saved, so the simulator recomputes it with the rest of the case.
                    if (!canSave) onUse(neto);
                    else void saveActividad().then((ok) => ok && onUse(neto));
                  }}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-[13px] font-semibold text-white transition hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-50"
                >
                  Usar en mi simulación
                  <ArrowRight size={14} aria-hidden />
                </button>
                {canSave && pendientes.length > 0 && (
                  <>
                    <button
                      type="button"
                      disabled={saving || busy}
                      onClick={() => void saveActividad()}
                      className="inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-2 text-[13px] font-semibold text-primary-dark transition hover:border-primary disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      <Save size={14} aria-hidden />
                      {saving ? "Guardando…" : "Guardar en mi expediente"}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setActividad({});
                        setActividadFlags({});
                        setCeutaEdit(null);
                      }}
                      className="inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-[13px] font-semibold text-ink-soft hover:bg-line/60"
                    >
                      <RotateCcw size={14} aria-hidden />
                      Deshacer cambios
                    </button>
                  </>
                )}
              </div>
            ) : (
              <p className="mt-2 text-[12.5px] text-muted">Introduce al menos tus ingresos del año para obtener el cálculo.</p>
            )}
            {canSave && pendientes.length > 0 && hasIncome && (
              <p className="mt-2 text-[12px] leading-snug text-muted">Tienes cambios sin guardar: al guardarlos, el simulador y el asesor calcularán tu actividad con estas cifras.</p>
            )}
          </ResultCard>
        )}
        {!r && conExpediente && !current.error && <p className="text-[13px] text-muted">Introduce tus ingresos del año para calcular el beneficio de tu actividad.</p>}
        <Group title="Ingresos">{fields(ACTIVIDAD_INGRESOS, actividadValues, setActividad)}</Group>
        <Group title="Gastos del año (sin IVA)">{fields(ACTIVIDAD_GASTOS, actividadValues, setActividad)}</Group>
        <Optional title="Más datos (opcional)">
          {!conExpediente && fields(ACTIVIDAD_MAS, actividadValues, setActividad)}
          {conExpediente && (
            <p className="text-[12px] leading-snug text-muted sm:col-span-2">
              Tus otros ingresos (sueldo, intereses, alquileres…) y tu grado de discapacidad se toman de tu expediente: cámbialos en «Tu expediente» o en el simulador.
            </p>
          )}
          <MoneyField field={ACTIVIDAD_IRREGULARES} values={actividadValues} onChange={setIn(setActividad)} />
          <Flag
            name="cumple_requisitos_reduccion_dependiente"
            label="Trabajo para un único cliente y cumplo los requisitos de la reducción"
            hint="Autónomo dependiente o de cliente único no vinculado, sin sueldo y con retención en casi todos los ingresos. Sustituye a los gastos de difícil justificación; el resultado detalla los requisitos."
            {...flagProps}
          />
          <Flag
            name="reduccion_inicio_actividad"
            label="Es una actividad nueva y este es mi primer año con beneficio (o el siguiente)"
            hint="Solo si no ejerciste ninguna actividad el año anterior a empezar y la mayor parte de tus ingresos no viene de tu antiguo empleador. Tiene una reducción propia."
            {...flagProps}
          />
          <CeutaSelect value={ceuta} onChange={setCeutaEdit} />
          {!conExpediente && (
          <label className="block sm:col-span-2">
            <span className="text-[12.5px] text-ink-soft">Discapacidad reconocida</span>
            <select
              value={discapacidad}
              onChange={(e) => setDiscapacidad(e.target.value)}
              className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-[16px] text-ink focus:border-primary focus:outline-none sm:text-[14px]"
            >
              <option value="no">No</option>
              <option value="si">Sí</option>
              <option value="ampliada">Sí, del 65 % o más, con movilidad reducida o ayuda de terceras personas</option>
            </select>
            <span className="mt-1 block text-[11.5px] leading-snug text-muted">Solo cuenta junto con la reducción de cliente único.</span>
          </label>
          )}
        </Optional>
        {r && <Detail lineas={r.lineas} avisos={perfil ? [perfil.aviso, ...r.avisos] : r.avisos} />}
        {!r && perfil && <Detail lineas={[]} avisos={[perfil.aviso]} />}
      </>
    );
  }

  function m130Tab() {
    const r = calc130.result;
    return (
      <>
        {r && (
          <ResultCard title={`Pago del ${TRIMESTRES[r.trimestre - 1]?.label ?? ""} trimestre (modelo 130)`} amount={r.a_ingresar} suffix={Number(r.a_ingresar) > 0 ? "a ingresar" : "nada que ingresar"} good={Number(r.a_ingresar) === 0}>
            <p className="mt-2 text-[13px] text-ink-soft">
              Beneficio acumulado {formatMoney(r.rendimiento_neto_acumulado)} · pago a cuenta sobre ese beneficio {formatMoney(r.pago_sobre_rendimiento)}
            </p>
            <p
              className={clsx(
                "mt-2 rounded-lg px-3 py-1.5 text-[13px]",
                r.obligado_a_pago_fraccionado === false ? "bg-grounding-cited-bg text-grounding-cited-fg" : r.obligado_a_pago_fraccionado === null ? "bg-grounding-abstain-bg text-grounding-abstain-fg" : "bg-grounding-uncited-bg text-grounding-uncited-fg",
              )}
            >
              <span className="font-semibold">
                {r.obligado_a_pago_fraccionado === false ? "No tienes que hacer este pago. " : r.obligado_a_pago_fraccionado === null ? "No sabemos si tienes que presentarlo. " : "Tienes que presentarlo. "}
              </span>
              {r.motivo_obligacion}
              {r.obligado_a_pago_fraccionado === false && Number(r.resultado) > 0 && <> Si tuvieras que presentarlo, saldrían {formatMoney(r.resultado)}.</>}
            </p>
            {r.trimestre < 4 && (
              <p className="mt-2 text-[12.5px] leading-snug text-muted">
                Para el trimestre siguiente, apunta: pago antes de la minoración {formatMoney(r.pago_previo_a_minoracion)}
                {Number(r.minoracion_pendiente) > 0 && <> y minoración pendiente {formatMoney(r.minoracion_pendiente)}</>}.
              </p>
            )}
          </ResultCard>
        )}
        <QuarterPicker value={trimestre130} onChange={setTrimestre130} cumulative />
        <Group title="Desde el 1 de enero">
          {fields(M130_FIELDS, m130, setM130)}
          {!primero130 && (
            <MoneyField
              field={{ name: "pagos_trimestres_anteriores", label: "Pagos de los trimestres anteriores", hint: "Suma de los «pagos antes de la minoración» de los trimestres anteriores de este año" }}
              values={m130}
              onChange={setIn(setM130)}
            />
          )}
          <Flag
            name="actividad_profesional"
            label="Mi actividad es profesional"
            hint="Abogados, médicos, programadores, diseñadores… (no comercio, hostelería o talleres). Solo los profesionales restan las retenciones."
            flags={{ ...m130Flags, actividad_profesional: profesional }}
            onChange={setFlagIn(setM130Flags)}
          />
          <Flag
            name="aplicar_gastos_dificil_justificacion"
            label="Estoy en estimación directa simplificada"
            hint="Lo habitual. El cálculo añade el porcentaje de gastos de difícil justificación que permite la norma."
            flags={m130Flags}
            onChange={setFlagIn(setM130Flags)}
          />
        </Group>
        <Optional title="Rebaja por ingresos bajos el año pasado (opcional)">
          <MoneyField
            field={{ name: "rendimiento_neto_ejercicio_anterior", label: "Beneficio neto de la actividad el año pasado", hint: "Si fue bajo, el pago se rebaja una cantidad fija cada trimestre; el resultado te dirá cuánto" }}
            values={m130}
            onChange={setIn(setM130)}
          />
          {!primero130 && (
            <MoneyField
              field={{ name: "minoracion_pendiente_trimestres_anteriores", label: "Rebaja sin usar de trimestres anteriores", hint: "La «minoración pendiente» que te quedó en el trimestre anterior" }}
              values={m130}
              onChange={setIn(setM130)}
            />
          )}
        </Optional>
        <Optional title="Ceuta y Melilla (opcional)">
          <CeutaSelect value={ceuta} onChange={setCeutaEdit} />
        </Optional>
        {profesional && (
          <Optional title="¿Tengo que presentarlo? (opcional)">
            <Flag name="inicio_actividad" label="He empezado la actividad este año" flags={m130Flags} onChange={setFlagIn(setM130Flags)} />
            {inicio ? (
              <MoneyField
                field={{ name: "ingresos_con_retencion_acumulados", label: "Ingresos de este año con retención", hint: "De los ingresos acumulados, los facturados con retención de IRPF" }}
                values={m130}
                onChange={setIn(setM130)}
              />
            ) : (
              <>
                <MoneyField field={{ name: "ingresos_ano_anterior", label: "Ingresos del año pasado" }} values={m130} onChange={setIn(setM130)} />
                <MoneyField
                  field={{ name: "ingresos_con_retencion_ano_anterior", label: "De ellos, con retención", hint: "Si la mayor parte llevaba retención de IRPF, no tienes que presentar el modelo 130; el resultado te lo dirá" }}
                  values={m130}
                  onChange={setIn(setM130)}
                />
              </>
            )}
          </Optional>
        )}
        {r && <Detail lineas={r.lineas} avisos={r.avisos} />}
      </>
    );
  }

  function m303Tab() {
    const r = calc303.result;
    const resultado = r ? Number(r.resultado) : 0;
    return (
      <>
        {r && (
          <ResultCard
            title={`IVA del ${TRIMESTRES[r.trimestre - 1]?.label ?? ""} trimestre (modelo 303)`}
            amount={Math.abs(resultado)}
            suffix={r.tipo_resultado === "a_ingresar" ? "a ingresar" : r.tipo_resultado === "a_compensar" ? "a tu favor" : "sin nada que pagar"}
            good={r.tipo_resultado !== "a_ingresar"}
          >
            <p className="mt-2 text-[13px] text-ink-soft">
              IVA cobrado {formatMoney(r.iva_devengado)} · IVA pagado {formatMoney(r.iva_deducible)}
              {Number(r.compensacion_aplicada) > 0 && <> · compensado de antes {formatMoney(r.compensacion_aplicada)}</>}
            </p>
            {r.tipo_resultado === "a_compensar" && (
              <p className="mt-2 rounded-lg bg-grounding-cited-bg px-3 py-1.5 text-[13px] text-grounding-cited-fg">
                {r.puede_solicitar_devolucion
                  ? "Es el último trimestre del año: puedes pedir la devolución o dejarlo a compensar."
                  : "Se resta en tus próximas declaraciones; la devolución solo se puede pedir en la del cuarto trimestre."}
              </p>
            )}
          </ResultCard>
        )}
        <QuarterPicker value={trimestre303} onChange={setTrimestre303} cumulative={false} />
        <Group title="Lo que has facturado en el trimestre">{fields(M303_VENTAS, m303, setM303)}</Group>
        <Group title="Lo que puedes restar">{fields(M303_COMPRAS, m303, setM303)}</Group>
        {r && <Detail lineas={r.lineas} avisos={r.avisos} />}
      </>
    );
  }

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <button type="button" aria-label="Cerrar el panel de autónomos" className="absolute inset-0 bg-ink/40" onClick={onClose} />
      <aside role="dialog" aria-modal="true" aria-label="Autónomos" className="relative flex h-full w-full max-w-[520px] flex-col border-l border-line bg-paper shadow-pop">
        <header className="border-b border-line px-5 pt-4">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="flex items-center gap-2 font-serif text-[19px] font-bold text-ink">
                <BriefcaseBusiness size={18} aria-hidden className="text-primary-dark" />
                Autónomos{ejercicio ? ` ${ejercicio}` : ""}
              </h2>
              <p className="text-[12.5px] text-muted">Tu beneficio del año y lo que toca pagar cada trimestre</p>
            </div>
            <div className="flex items-center gap-1">
              {ejercicios && ejercicios.length > 1 && ejercicio !== null && (
                <select
                  aria-label="Ejercicio"
                  value={ejercicio}
                  onChange={(e) => setEjercicioEdit(Number(e.target.value))}
                  className="rounded-lg border border-line bg-surface px-2 py-1.5 text-[13px] text-ink focus:border-primary focus:outline-none"
                >
                  {ejercicios.map((year) => (
                    <option key={year} value={year}>
                      {year}
                    </option>
                  ))}
                </select>
              )}
              <button type="button" onClick={onClose} aria-label="Cerrar" className="rounded-lg p-2 text-muted hover:bg-line/60 hover:text-ink">
                <X size={18} />
              </button>
            </div>
          </div>
          <div role="tablist" aria-label="Cálculos para autónomos" className="mt-3 flex gap-1 overflow-x-auto">
            {TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                role="tab"
                id={`autonomos-tab-${t.id}`}
                aria-selected={tab === t.id}
                aria-controls="autonomos-tabpanel"
                onClick={() => setTab(t.id)}
                className={clsx(
                  "-mb-px whitespace-nowrap border-b-2 px-3 py-2 text-[13px] font-semibold transition",
                  tab === t.id ? "border-primary text-primary-dark" : "border-transparent text-ink-soft hover:text-ink",
                )}
              >
                {t.label}
              </button>
            ))}
          </div>
        </header>

        <div role="tabpanel" id="autonomos-tabpanel" aria-labelledby={`autonomos-tab-${tab}`} className="flex-1 space-y-5 overflow-y-auto px-5 pb-5 pt-3">
          {loadError && <ErrorNote message={loadError} />}
          {current.error && <ErrorNote message={current.error} />}
          {/* Keyed by tab so React does not reuse one tab's DOM (open <details>, focus) for another. */}
          <div key={tab} className="space-y-5">
            {tab === "actividad" ? actividadTab() : tab === "m130" ? m130Tab() : m303Tab()}
          </div>
        </div>

        <footer className="border-t border-line px-5 py-3 text-[11.5px] leading-relaxed text-muted">
          Cálculo determinista con las cifras que indicas; no valora si un gasto es deducible ni sustituye a la Agencia Tributaria.
          {fuente && <span className="block">{fuente}.</span>}
        </footer>
      </aside>
    </div>
  );
}
