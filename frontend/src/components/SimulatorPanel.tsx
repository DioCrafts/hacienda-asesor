// Simulator: change a figure, see the tax change. Everything here is the
// deterministic calculator (POST /simulate) — no model call, so it answers as
// you type. The saved case is the baseline; edits are compared against it.

import { Calculator, ChevronDown, Lightbulb, RotateCcw, Save, TriangleAlert, X } from "lucide-react";
import clsx from "clsx";
import { useEffect, useMemo, useRef, useState } from "react";

import type { Benefit, Fact, Simulation } from "../api";
import { ApiError, simulate } from "../api";
import { formatMoney } from "../lib/presentation";

type FieldKind = "money" | "count";
interface FieldDef {
  name: string;
  label: string;
  kind: FieldKind;
  hint?: string;
}

const GROUPS: Array<{ title: string; fields: FieldDef[] }> = [
  {
    title: "Ingresos del año",
    fields: [
      { name: "rendimiento_trabajo_bruto", label: "Salario bruto", kind: "money" },
      { name: "cotizaciones_seguridad_social", label: "Cotizaciones a la Seguridad Social", kind: "money", hint: "Lo que te descuentan en nómina (≈ 6,5 % del bruto)" },
      { name: "retenciones", label: "IRPF ya retenido", kind: "money" },
      { name: "rendimiento_capital_mobiliario", label: "Intereses y dividendos", kind: "money" },
      { name: "ganancias_patrimoniales", label: "Ganancias por ventas", kind: "money", hint: "Acciones, fondos, inmuebles… (negativo si son pérdidas)" },
    ],
  },
  {
    title: "Familia",
    fields: [
      { name: "num_descendientes", label: "Hijos a cargo", kind: "count" },
      { name: "num_descendientes_menores_3", label: "De ellos, menores de 3 años", kind: "count" },
    ],
  },
  {
    title: "Lo que puedes mover",
    fields: [
      { name: "aportacion_plan_pensiones", label: "Aportación a plan de pensiones", kind: "money", hint: "Reduce la base hasta 1.500 € al año" },
      { name: "donativos", label: "Donativos", kind: "money" },
    ],
  },
];
const FIELDS = GROUPS.flatMap((g) => g.fields);
const TRIBUTACION = [
  { value: "individual", label: "Individual" },
  { value: "conjunta_biparental", label: "Conjunta (matrimonio)" },
  { value: "conjunta_monoparental", label: "Conjunta (monoparental)" },
];
// What the "Probar" button on a lever sets.
const LEVER_PRESET: Record<string, { field: string; value: string }> = {
  plan_pensiones: { field: "aportacion_plan_pensiones", value: "1500" },
  donativos: { field: "donativos", value: "250" },
};

function parseNumber(raw: string): number | null {
  const cleaned = raw.trim().replace(/[€\s]/g, "").replace(/\.(?=\d{3}(\D|$))/g, "").replace(",", ".");
  if (!cleaned || cleaned === "-") return null;
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : null;
}

interface SimulatorPanelProps {
  open: boolean;
  onClose: () => void;
  caseId: string | null;
  facts: Fact[];
  taxPeriod: string | null;
  // Persist edited figures to the case. Resolves true when saved.
  onSaveFact: (factName: string, value: number) => Promise<boolean>;
  busy: boolean;
}

export function SimulatorPanel({ open, onClose, caseId, facts, taxPeriod, onSaveFact, busy }: SimulatorPanelProps) {
  // Edits layered over the case facts: { field: raw text }. Empty = baseline.
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [tributacion, setTributacion] = useState("individual");
  const [simulation, setSimulation] = useState<Simulation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const requestSeq = useRef(0);

  const baseline = useMemo(() => {
    const map: Record<string, string> = {};
    for (const fact of facts) if (typeof fact.value === "number") map[fact.name] = String(fact.value);
    return map;
  }, [facts]);

  const cambios = useMemo(() => {
    const out: Record<string, unknown> = {};
    for (const [name, raw] of Object.entries(edits)) {
      const n = parseNumber(raw);
      if (n !== null && String(n) !== baseline[name]) out[name] = n;
    }
    if (tributacion !== "individual") out.tributacion = tributacion;
    return out;
  }, [edits, tributacion, baseline]);
  const changed = Object.keys(cambios).length > 0;
  const cambiosKey = JSON.stringify(cambios);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Debounced, last-request-wins simulation.
  useEffect(() => {
    if (!open) return;
    const seq = ++requestSeq.current;
    const controller = new AbortController();
    const timer = setTimeout(() => {
      const year = taxPeriod ? Number(taxPeriod.slice(0, 4)) : null;
      simulate({
        caseId,
        ejercicio: caseId ? null : year,
        escenarios: [
          { nombre: "Tu expediente", cambios: {} },
          { nombre: "Simulación", cambios: JSON.parse(cambiosKey) as Record<string, unknown> },
        ],
        leversForLastScenario: true,
        signal: controller.signal,
      }).then(
        (result) => {
          if (seq !== requestSeq.current) return;
          setSimulation(result);
          setError(null);
        },
        (err: unknown) => {
          if (controller.signal.aborted || seq !== requestSeq.current) return;
          setError(err instanceof ApiError ? err.message : "No se pudo calcular la simulación.");
        },
      );
    }, 300);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [open, caseId, taxPeriod, cambiosKey, facts]);

  if (!open) return null;

  const base = simulation?.escenarios[0];
  const sim = simulation?.escenarios[1] ?? base;
  const resultado = sim ? Number(sim.resultado.resultado) : 0;
  const ahorro = simulation?.escenarios[1] ? Number(simulation.escenarios[1].ahorro_frente_a_base) : 0;
  const levers = (simulation?.beneficios ?? []).filter((b) => b.estado === "disponible" || b.estado === "por_confirmar");
  const hasIncome = FIELDS.some((f) => (edits[f.name] ?? baseline[f.name] ?? "") !== "" && f.name.startsWith("rendimiento"));

  async function saveAll() {
    setSaving(true);
    try {
      for (const [name, value] of Object.entries(cambios)) {
        if (typeof value !== "number") continue;
        if (!(await onSaveFact(name, value))) return;
      }
      setEdits({});
    } finally {
      setSaving(false);
    }
  }

  function lever(benefit: Benefit) {
    const preset = LEVER_PRESET[benefit.id];
    return (
      <li key={benefit.id} className="rounded-xl border border-line bg-surface px-3.5 py-3">
        <div className="flex items-start justify-between gap-3">
          <p className="text-[14px] font-semibold text-ink">{benefit.nombre}</p>
          {benefit.ahorro_estimado && Number(benefit.ahorro_estimado) > 0 && (
            <span className="flex-shrink-0 rounded-pill bg-grounding-cited-bg px-2 py-0.5 text-[12px] font-bold text-grounding-cited-fg">
              hasta −{formatMoney(benefit.ahorro_estimado)}
            </span>
          )}
        </div>
        <p className="mt-1 text-[13px] leading-snug text-ink-soft">{benefit.detalle}</p>
        <p className="mt-1.5 text-[11.5px] text-muted">
          {benefit.referencia}
          {!benefit.verificado && " · cifras pendientes de contrastar"}
        </p>
        {preset && (
          <button
            type="button"
            onClick={() => setEdits((e) => ({ ...e, [preset.field]: preset.value }))}
            className="mt-2 rounded-lg border border-line px-2.5 py-1 text-[12.5px] font-semibold text-primary-dark transition hover:border-primary"
          >
            Probar en la simulación
          </button>
        )}
      </li>
    );
  }

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <button type="button" aria-label="Cerrar el simulador" className="absolute inset-0 bg-ink/40" onClick={onClose} />
      <aside role="dialog" aria-modal="true" aria-label="Simulador de IRPF" className="relative flex h-full w-full max-w-[520px] flex-col border-l border-line bg-paper shadow-pop">
        <header className="flex items-center justify-between border-b border-line px-5 py-4">
          <div>
            <h2 className="flex items-center gap-2 font-serif text-[19px] font-bold text-ink">
              <Calculator size={18} aria-hidden className="text-primary-dark" />
              Simulador de IRPF{simulation ? ` ${simulation.ejercicio}` : ""}
            </h2>
            <p className="text-[12.5px] text-muted">Cambia una cifra y mira cómo cambia tu declaración</p>
          </div>
          <button type="button" onClick={onClose} aria-label="Cerrar" className="rounded-lg p-2 text-muted hover:bg-line/60 hover:text-ink">
            <X size={18} />
          </button>
        </header>

        <div className="flex-1 space-y-5 overflow-y-auto px-5 pb-5 pt-3">
          {error && (
            <p role="alert" className="flex gap-2 rounded-lg border border-risk-high-fg/30 bg-risk-high-bg px-3 py-2 text-[13px] text-ink-soft">
              <TriangleAlert size={15} aria-hidden className="mt-0.5 flex-shrink-0 text-risk-high-fg" />
              {error}
            </p>
          )}

          {sim && (
            <section aria-live="polite" className="sticky top-0 z-10 rounded-xl border border-line bg-surface px-4 py-4 shadow-pop">
              <p className="text-[12px] font-semibold uppercase tracking-[0.1em] text-muted">Resultado estimado</p>
              <p className={clsx("mt-1 font-serif text-[30px] font-bold leading-none", resultado > 0 ? "text-ink" : "text-grounding-cited-fg")}>
                {formatMoney(Math.abs(resultado))}
                <span className="ml-2 text-[14px] font-semibold text-ink-soft">{resultado > 0 ? "a pagar" : resultado < 0 ? "a devolver" : ""}</span>
              </p>
              <p className="mt-2 text-[13px] text-ink-soft">
                Cuota del año: {formatMoney(sim.resultado.cuota_liquida)} · tipo medio {sim.resultado.tipo_medio_efectivo.replace(".", ",")} %
              </p>
              {changed && (
                <p className={clsx("mt-2 rounded-lg px-3 py-1.5 text-[13.5px] font-semibold", ahorro > 0 ? "bg-grounding-cited-bg text-grounding-cited-fg" : ahorro < 0 ? "bg-grounding-uncited-bg text-grounding-uncited-fg" : "bg-grounding-abstain-bg text-grounding-abstain-fg")}>
                  {ahorro > 0 ? `Con estos cambios pagas ${formatMoney(ahorro)} menos` : ahorro < 0 ? `Con estos cambios pagas ${formatMoney(-ahorro)} más` : "Estos cambios no alteran el resultado"}
                  {base && <span className="font-normal"> (antes: {formatMoney(base.resultado.resultado)})</span>}
                </p>
              )}
              {!hasIncome && <p className="mt-2 text-[12.5px] text-muted">Introduce al menos tu salario bruto para obtener una estimación.</p>}
            </section>
          )}

          {GROUPS.map((group) => (
            <fieldset key={group.title}>
              <legend className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">{group.title}</legend>
              <div className="grid gap-3 sm:grid-cols-2">
                {group.fields.map((field) => {
                  const value = edits[field.name] ?? baseline[field.name] ?? "";
                  const dirty = field.name in cambios;
                  return (
                    <label key={field.name} className="block">
                      <span className="text-[12.5px] text-ink-soft">{field.label}</span>
                      <span className="relative mt-1 block">
                        <input
                          value={value}
                          onChange={(e) => setEdits((prev) => ({ ...prev, [field.name]: e.target.value }))}
                          inputMode={field.kind === "count" ? "numeric" : "decimal"}
                          placeholder="0"
                          className={clsx(
                            "w-full rounded-lg border bg-surface px-3 py-2 text-[16px] text-ink focus:outline-none sm:text-[14px]",
                            field.kind === "money" && "pr-7",
                            dirty ? "border-primary" : "border-line focus:border-primary",
                          )}
                        />
                        {field.kind === "money" && <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-[13px] text-muted">€</span>}
                      </span>
                      {field.hint && <span className="mt-1 block text-[11.5px] leading-snug text-muted">{field.hint}</span>}
                    </label>
                  );
                })}
                {group.title === "Familia" && (
                  <label className="block sm:col-span-2">
                    <span className="text-[12.5px] text-ink-soft">Tipo de declaración</span>
                    <select value={tributacion} onChange={(e) => setTributacion(e.target.value)} className="mt-1 w-full rounded-lg border border-line bg-surface px-3 py-2 text-[16px] text-ink focus:border-primary focus:outline-none sm:text-[14px]">
                      {TRIBUTACION.map((t) => (
                        <option key={t.value} value={t.value}>
                          {t.label}
                        </option>
                      ))}
                    </select>
                    {tributacion !== "individual" && (
                      <span className="mt-1 block text-[11.5px] leading-snug text-muted">
                        En conjunta, las cifras deben sumar las rentas de toda la unidad familiar.
                      </span>
                    )}
                  </label>
                )}
              </div>
            </fieldset>
          ))}

          {changed && (
            <div className="flex flex-wrap gap-2">
              {caseId && (
                <button type="button" onClick={() => void saveAll()} disabled={saving || busy} className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-[13px] font-semibold text-white transition hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-50">
                  <Save size={14} aria-hidden />
                  {saving ? "Guardando…" : "Guardar en mi expediente"}
                </button>
              )}
              <button
                type="button"
                onClick={() => {
                  setEdits({});
                  setTributacion("individual");
                }}
                className="inline-flex items-center gap-1.5 rounded-lg px-3 py-2 text-[13px] font-semibold text-ink-soft hover:bg-line/60"
              >
                <RotateCcw size={14} aria-hidden />
                Deshacer cambios
              </button>
            </div>
          )}

          {levers.length > 0 && (
            <section>
              <h3 className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">
                <Lightbulb size={13} aria-hidden />
                Formas de pagar menos
              </h3>
              <ul className="space-y-2">{levers.map(lever)}</ul>
            </section>
          )}

          {sim && (
            <details className="group rounded-xl border border-line bg-surface">
              <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-2.5 text-[13px] font-semibold text-ink-soft [&::-webkit-details-marker]:hidden">
                Desglose del cálculo
                <ChevronDown size={16} aria-hidden className="text-muted transition group-open:rotate-180" />
              </summary>
              <table className="w-full border-t border-line text-[12.5px]">
                <tbody>
                  {sim.resultado.lineas.map((line, i) => (
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

          {sim && sim.resultado.avisos.length > 0 && (
            <details open className="group rounded-xl border border-grounding-uncited-bd bg-grounding-uncited-bg">
              <summary className="flex cursor-pointer list-none items-center justify-between px-4 py-2.5 text-[13px] font-semibold text-grounding-uncited-fg [&::-webkit-details-marker]:hidden">
                Qué no incluye esta estimación ({sim.resultado.avisos.length})
                <ChevronDown size={16} aria-hidden className="transition group-open:rotate-180" />
              </summary>
              <ul className="list-disc space-y-1.5 px-4 pb-3 pl-8 text-[12.5px] leading-snug text-ink-soft">
                {sim.resultado.avisos.map((aviso, i) => (
                  <li key={i}>{aviso}</li>
                ))}
              </ul>
            </details>
          )}
        </div>

        <footer className="border-t border-line px-5 py-3 text-[11.5px] leading-relaxed text-muted">
          Cálculo determinista con los parámetros estatales del ejercicio. No sustituye al borrador de la Agencia Tributaria.
          {sim && <span className="block">{sim.resultado.fuente}.</span>}
        </footer>
      </aside>
    </div>
  );
}
