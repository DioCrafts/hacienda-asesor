// "Módulos": the household areas the assistant can help with beyond taxes (electricity, expiries,
// subscriptions…). The user switches each one on or off, fills in its data with a form built from the
// module's own fact declarations, reads a document with it when it can, and decides on the actions its
// specialist prepared: nothing that acts outside the app runs until the user confirms it here.

import { Check, ChevronDown, FileUp, House, ShieldAlert, Trash2, X } from "lucide-react";
import clsx from "clsx";
import { useEffect, useId, useState } from "react";

import type { CaseState, Fact } from "../api";
import { ApiError, deleteCaseFact, getCase, updateCaseFact } from "../api";
import type { AccionPendiente, CampoPropuesto, HechoInfo, HerramientaInfo, ModuloInfo, ModulosResponse } from "../apiModulos";
import { readModuleDocument, runModuleTool } from "../apiModulos";
import { fileToBase64, relativeTime } from "../lib/presentation";

const FIELD_CLASS =
  "w-full rounded-lg border border-line bg-paper px-2.5 py-1.5 text-[16px] text-ink focus:border-primary focus:outline-none sm:text-[14px]";
const BUTTON_PRIMARY =
  "inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-[13px] font-semibold text-white transition hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-50";
const BUTTON_QUIET =
  "inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[13px] font-semibold text-ink-soft hover:bg-line/60 disabled:cursor-not-allowed disabled:opacity-50";

function message(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  return "No hay conexión con el servicio.";
}

interface ModulosPanelProps {
  open: boolean;
  onClose: () => void;
  modulos: ModulosResponse | null;
  loading: boolean;
  error: string | null;
  // Switch a module; `borrar` also erases its data. Resolves true when saved.
  onToggle: (clave: string, activo: boolean, borrar: boolean) => Promise<boolean>;
  caseId: string | null;
  facts: Fact[];
  // The case after a module fact was saved or erased here.
  onCaseUpdated: (updated: CaseState) => void;
  acciones: AccionPendiente[];
  onDecide: (id: string, confirmar: boolean) => Promise<void>;
  disabled: boolean;
}

export function ModulosPanel(props: ModulosPanelProps) {
  const { open, onClose, modulos, loading, error, acciones } = props;
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  const nombres = Object.fromEntries((modulos?.modulos ?? []).map((m) => [m.clave, m.nombre]));
  const pendientes = acciones.filter((a) => a.estado === "pendiente");
  const decididas = acciones.filter((a) => a.estado !== "pendiente");
  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <button type="button" aria-label="Cerrar los módulos" className="absolute inset-0 bg-ink/40" onClick={onClose} />
      <aside role="dialog" aria-modal="true" aria-label="Módulos del hogar" className="relative flex h-full w-full max-w-[480px] flex-col border-l border-line bg-paper shadow-pop">
        <header className="flex items-center justify-between border-b border-line px-5 py-4">
          <div>
            <h2 className="flex items-center gap-2 font-serif text-[19px] font-bold text-ink">
              <House size={18} aria-hidden className="text-primary-dark" />
              Módulos
            </h2>
            <p className="text-[12.5px] text-muted">Lo que tu asistente puede llevarte además de los impuestos</p>
          </div>
          <button type="button" onClick={onClose} aria-label="Cerrar" className="rounded-lg p-2 text-muted hover:bg-line/60 hover:text-ink">
            <X size={18} />
          </button>
        </header>

        <div className="flex-1 space-y-4 overflow-y-auto px-5 py-5">
          {error && (
            <p role="alert" className="rounded-lg border border-risk-high-fg/30 bg-risk-high-bg px-3 py-2 text-[13px] text-ink-soft">
              {error}
            </p>
          )}
          {loading && !modulos && <p className="text-[13.5px] text-muted">Cargando los módulos…</p>}

          {pendientes.length > 0 && (
            <section aria-labelledby="acciones-pendientes" className="rounded-xl border border-risk-high-fg/40 bg-surface px-4 py-3.5 shadow-card">
              <h3 id="acciones-pendientes" className="flex items-center gap-2 text-[15px] font-bold text-ink">
                <ShieldAlert size={16} aria-hidden className="text-risk-high-fg" />
                Pendiente de tu confirmación ({pendientes.length})
              </h3>
              <p className="mt-1 text-[12.5px] leading-snug text-muted">
                Estas acciones actúan fuera de la aplicación. No se ha hecho nada todavía: solo se ejecutan si las confirmas.
              </p>
              <ul className="mt-3 space-y-3">
                {pendientes.map((a) => (
                  <AccionItem key={a.id} accion={a} modulo={nombres[a.modulo] ?? a.modulo} onDecide={props.onDecide} disabled={props.disabled} />
                ))}
              </ul>
            </section>
          )}

          {modulos && !modulos.configurable && (
            <p className="rounded-lg bg-line/50 px-3 py-2 text-[13px] leading-snug text-ink-soft">
              Sin cuentas de usuario, los módulos los activa el operador del servicio.
            </p>
          )}

          {modulos?.modulos.map((m) => (
            <ModuloCard key={m.clave} modulo={m} configurable={modulos.configurable} {...props} />
          ))}

          {decididas.length > 0 && (
            <details className="rounded-xl border border-line bg-surface px-4 py-3">
              <summary className="cursor-pointer text-[13.5px] font-semibold text-ink-soft">Acciones ya decididas ({decididas.length})</summary>
              <ul className="mt-2 space-y-2 text-[13px] leading-snug">
                {decididas.map((a) => (
                  <li key={a.id}>
                    <span className="font-semibold text-ink">{ESTADO[a.estado] ?? a.estado}</span> · {a.resumen}
                    {a.resultado && <p className={clsx("mt-0.5", a.es_error ? "text-risk-high-fg" : "text-muted")}>{a.resultado}</p>}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
        <footer className="border-t border-line px-5 py-3 text-[11.5px] leading-relaxed text-muted">
          Cada módulo calcula con datos oficiales y sus propias fuentes. El asistente nunca suma cifras de módulos distintos si
          ningún módulo ha calculado esa combinación.
        </footer>
      </aside>
    </div>
  );
}

const ESTADO: Record<string, string> = {
  ejecutada: "Hecha",
  fallida: "Falló",
  rechazada: "Rechazada",
  ejecutando: "En curso",
};

function AccionItem({ accion, modulo, onDecide, disabled }: { accion: AccionPendiente; modulo: string; onDecide: ModulosPanelProps["onDecide"]; disabled: boolean }) {
  const [step, setStep] = useState<"idle" | "confirming" | "busy">("idle");
  async function decide(confirmar: boolean) {
    setStep("busy");
    await onDecide(accion.id, confirmar);
    setStep("idle");
  }
  return (
    <li className="rounded-lg border border-line bg-paper px-3 py-2.5">
      <p className="text-[11.5px] font-semibold uppercase tracking-[0.08em] text-muted">
        {modulo} · {relativeTime(Date.parse(accion.created_at))}
      </p>
      <p className="mt-1 text-[13.5px] leading-snug text-ink">{accion.resumen}</p>
      <div className="mt-2 flex flex-wrap gap-2">
        {step === "confirming" ? (
          <>
            <button type="button" className={BUTTON_PRIMARY} onClick={() => void decide(true)} disabled={disabled}>
              <Check size={14} aria-hidden />
              Sí, ejecutarla ahora
            </button>
            <button type="button" className={BUTTON_QUIET} onClick={() => setStep("idle")}>
              Cancelar
            </button>
          </>
        ) : (
          <>
            <button type="button" className={BUTTON_PRIMARY} onClick={() => setStep("confirming")} disabled={disabled || step === "busy"}>
              Confirmar
            </button>
            <button type="button" className={BUTTON_QUIET} onClick={() => void decide(false)} disabled={disabled || step === "busy"}>
              Rechazar
            </button>
          </>
        )}
      </div>
    </li>
  );
}

function ModuloCard({
  modulo,
  configurable,
  onToggle,
  caseId,
  facts,
  onCaseUpdated,
  disabled,
}: ModulosPanelProps & { modulo: ModuloInfo; configurable: boolean }) {
  const [turningOff, setTurningOff] = useState(false);
  const [busy, setBusy] = useState(false);
  const switchId = useId();
  const editable = configurable && !modulo.obligatorio;
  const ownFacts = facts.filter((f) => modulo.hechos.some((h) => h.nombre === f.name));
  const actua = modulo.herramientas.some((h) => h.efecto === "actua");

  async function toggle(activo: boolean, borrar = false) {
    setBusy(true);
    if (await onToggle(modulo.clave, activo, borrar)) setTurningOff(false);
    setBusy(false);
  }

  return (
    <article aria-labelledby={`${switchId}-title`} className={clsx("rounded-xl border bg-surface px-4 py-3.5 shadow-card", modulo.activo ? "border-primary/50" : "border-line")}>
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h3 id={`${switchId}-title`} className="text-[15.5px] font-bold text-ink">
            {modulo.nombre}
          </h3>
          <p className="mt-0.5 text-[13px] leading-snug text-ink-soft">{modulo.descripcion}</p>
        </div>
        {modulo.obligatorio ? (
          <span className="shrink-0 rounded-pill bg-line/70 px-2 py-0.5 text-[11.5px] font-semibold text-ink-soft">Siempre activo</span>
        ) : (
          <button
            type="button"
            role="switch"
            aria-checked={modulo.activo}
            aria-label={`${modulo.activo ? "Desactivar" : "Activar"} ${modulo.nombre}`}
            disabled={!editable || busy || disabled}
            onClick={() => (modulo.activo ? setTurningOff(true) : void toggle(true))}
            className={clsx(
              "relative h-7 w-12 shrink-0 rounded-pill transition disabled:cursor-not-allowed disabled:opacity-60",
              modulo.activo ? "bg-primary" : "bg-line",
            )}
          >
            <span className={clsx("absolute top-1 h-5 w-5 rounded-full bg-white shadow transition-all", modulo.activo ? "left-6" : "left-1")} />
          </button>
        )}
      </div>

      {turningOff && (
        <div role="group" aria-label={`Desactivar ${modulo.nombre}`} className="mt-3 rounded-lg border border-line bg-paper px-3 py-2.5 text-[13px] leading-snug">
          <p className="text-ink">
            Dejará de usarse en tus consultas y se quitarán sus avisos y sus acciones pendientes.
            {ownFacts.length > 0 ? " ¿Quieres borrar también sus datos de tu expediente?" : ""}
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button type="button" className={BUTTON_PRIMARY} disabled={busy} onClick={() => void toggle(false)}>
              {ownFacts.length > 0 ? "Desactivar y conservar los datos" : "Desactivar"}
            </button>
            {ownFacts.length > 0 && (
              <button type="button" className={clsx(BUTTON_QUIET, "text-risk-high-fg")} disabled={busy} onClick={() => void toggle(false, true)}>
                <Trash2 size={14} aria-hidden />
                Desactivar y borrar sus datos
              </button>
            )}
            <button type="button" className={BUTTON_QUIET} onClick={() => setTurningOff(false)}>
              Cancelar
            </button>
          </div>
        </div>
      )}

      {modulo.activo && modulo.clave === "fiscal" && (
        <p className="mt-2 text-[12.5px] text-muted">Sus datos se editan en «Tu expediente».</p>
      )}
      {modulo.activo && actua && (
        <p className="mt-2 text-[12.5px] leading-snug text-muted">
          Este módulo puede preparar acciones fuera de la aplicación; ninguna se ejecuta sin tu confirmación.
        </p>
      )}

      {modulo.activo && modulo.hechos.length > 0 && (
        <details className="mt-3">
          <summary className="flex cursor-pointer items-center gap-1 text-[13.5px] font-semibold text-primary-dark">
            <ChevronDown size={14} aria-hidden />
            Tus datos de este módulo ({ownFacts.length} de {modulo.hechos.length})
          </summary>
          {!caseId ? (
            <p className="mt-2 text-[13px] text-muted">Haz una primera consulta para crear tu expediente y poder guardar datos.</p>
          ) : (
            <div className="mt-2 space-y-3">
              {modulo.hechos.map((h) => (
                <HechoField
                  key={h.nombre}
                  hecho={h}
                  caseId={caseId}
                  fact={facts.find((f) => f.name === h.nombre)}
                  onCaseUpdated={onCaseUpdated}
                  disabled={disabled}
                />
              ))}
            </div>
          )}
        </details>
      )}

      {modulo.activo &&
        caseId &&
        modulo.herramientas
          .filter((h) => h.acepta_fichero && h.efecto !== "actua")
          .map((h) => <FileTool key={h.nombre} modulo={modulo} herramienta={h} caseId={caseId} onCaseUpdated={onCaseUpdated} disabled={disabled} />)}

      {modulo.activo && modulo.lee_documentos && caseId && (
        <DocumentReader modulo={modulo} caseId={caseId} onCaseUpdated={onCaseUpdated} disabled={disabled} />
      )}

      {modulo.fuentes.length > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-[12.5px] font-semibold text-muted">Fuentes ({modulo.fuentes.length})</summary>
          <ul className="mt-1 space-y-1 text-[12px] leading-snug text-muted">
            {modulo.fuentes.map((f) => (
              <li key={f.identificador}>
                {f.url.startsWith("https://") ? (
                  <a href={f.url} target="_blank" rel="noreferrer" className="underline hover:text-ink">
                    {f.descripcion}
                  </a>
                ) : (
                  f.descripcion
                )}{" "}
                ({f.identificador})
              </li>
            ))}
          </ul>
        </details>
      )}
    </article>
  );
}

function initialText(hecho: HechoInfo, fact: Fact | undefined): string {
  if (!fact) return "";
  if (hecho.tipo === "json" && typeof fact.value === "string") {
    try {
      return JSON.stringify(JSON.parse(fact.value), null, 2);
    } catch {
      return fact.value;
    }
  }
  return String(fact.value);
}

function HechoField({
  hecho,
  caseId,
  fact,
  onCaseUpdated,
  disabled,
}: {
  hecho: HechoInfo;
  caseId: string;
  fact: Fact | undefined;
  onCaseUpdated: (updated: CaseState) => void;
  disabled: boolean;
}) {
  const id = useId();
  const [draft, setDraft] = useState(() => initialText(hecho, fact));
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const stored = initialText(hecho, fact);

  async function save() {
    setError(null);
    setSaved(false);
    let value: string | number | boolean = draft.trim();
    if (hecho.tipo === "boolean") value = draft === "true";
    if (hecho.tipo === "json") {
      try {
        JSON.parse(draft);
      } catch (err) {
        setError(`No es JSON válido: ${err instanceof Error ? err.message : "revisa la sintaxis"}.`);
        return;
      }
    }
    if (value === "") {
      setError("Escribe un valor o usa «Borrar».");
      return;
    }
    setBusy(true);
    try {
      onCaseUpdated(await updateCaseFact({ caseId, factName: hecho.nombre, value }));
      setSaved(true);
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setError(null);
    setBusy(true);
    try {
      onCaseUpdated(await deleteCaseFact({ caseId, factName: hecho.nombre }));
      setDraft("");
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  const describedBy = `${id}-desc${error ? ` ${id}-err` : ""}`;
  return (
    <form
      className="rounded-lg border border-line bg-paper px-3 py-2.5"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <label htmlFor={id} className="flex flex-wrap items-center gap-2 text-[13.5px] font-semibold text-ink">
        {hecho.etiqueta}
        {hecho.sensible && <span className="rounded-pill bg-risk-high-bg px-2 py-px text-[11px] font-semibold text-risk-high-fg">Dato sensible</span>}
      </label>
      <p id={`${id}-desc`} className="mb-1.5 text-[12px] leading-snug text-muted">
        {hecho.descripcion}
        {hecho.tipo === "json" ? " (en formato JSON)" : ""}
      </p>
      {hecho.tipo === "boolean" ? (
        <select id={id} value={draft} onChange={(e) => setDraft(e.target.value)} className={FIELD_CLASS} aria-describedby={describedBy} aria-invalid={error !== null}>
          <option value="">— sin indicar —</option>
          <option value="true">Sí</option>
          <option value="false">No</option>
        </select>
      ) : hecho.tipo === "json" ? (
        <textarea
          id={id}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          rows={5}
          spellCheck={false}
          className={clsx(FIELD_CLASS, "font-mono text-[13px]")}
          aria-describedby={describedBy}
          aria-invalid={error !== null}
        />
      ) : (
        <input
          id={id}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          inputMode={hecho.tipo === "number" ? "decimal" : undefined}
          className={FIELD_CLASS}
          aria-describedby={describedBy}
          aria-invalid={error !== null}
        />
      )}
      {error && (
        <p id={`${id}-err`} role="alert" className="mt-1.5 text-[12.5px] leading-snug text-risk-high-fg">
          {error}
        </p>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <button type="submit" className={BUTTON_PRIMARY} disabled={busy || disabled || draft === stored}>
          <Check size={14} aria-hidden />
          {busy ? "Guardando…" : "Guardar"}
        </button>
        {fact && (
          <button type="button" className={BUTTON_QUIET} onClick={() => void remove()} disabled={busy || disabled}>
            <Trash2 size={14} aria-hidden />
            Borrar
          </button>
        )}
        {saved && <span role="status" className="text-[12.5px] text-grounding-cited-fg">Guardado</span>}
      </div>
    </form>
  );
}

// Binary formats travel as base64; everything else as text.
const BINARY_FILE = /\.(xlsx|xls|ods|zip)$/i;

/** Upload a file straight to a module tool (a bank statement): it never goes to the model or the audit trail. */
function FileTool({
  modulo,
  herramienta,
  caseId,
  onCaseUpdated,
  disabled,
}: {
  modulo: ModuloInfo;
  herramienta: HerramientaInfo;
  caseId: string;
  onCaseUpdated: (updated: CaseState) => void;
  disabled: boolean;
}) {
  const id = useId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resultado, setResultado] = useState<string | null>(null);

  async function send(file: File) {
    setError(null);
    setResultado(null);
    setBusy(true);
    try {
      const argumentos = BINARY_FILE.test(file.name) ? { contenido_base64: await fileToBase64(file) } : { contenido: await file.text() };
      const resp = await runModuleTool({ clave: modulo.clave, herramienta: herramienta.nombre, caseId, argumentos });
      if (resp.resultado?.es_error) setError(resp.resultado.contenido);
      else setResultado(resp.resultado?.contenido ?? null);
      if (resp.case_updated) onCaseUpdated(await getCase({ caseId }));
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 rounded-lg border border-dashed border-line px-3 py-2.5">
      <label htmlFor={id} className="flex cursor-pointer items-center gap-2 text-[13.5px] font-semibold text-primary-dark">
        <FileUp size={15} aria-hidden />
        {busy ? "Analizando el fichero…" : "Analizar un fichero"}
      </label>
      <input
        id={id}
        type="file"
        className="sr-only"
        disabled={busy || disabled}
        onChange={(e) => {
          const file = e.target.files?.[0];
          e.target.value = "";
          if (file) void send(file);
        }}
      />
      <p className="mt-1 text-[12px] leading-snug text-muted">
        {herramienta.descripcion.split(". ")[0]}. El fichero se analiza en el servidor sin pasar por el modelo de lenguaje y no se
        guarda: solo lo que se deduce de él.
      </p>
      {error && (
        <p role="alert" className="mt-1.5 whitespace-pre-line text-[12.5px] text-risk-high-fg">
          {error}
        </p>
      )}
      {resultado && (
        <p role="status" className="mt-2 max-h-64 overflow-y-auto whitespace-pre-line text-[13px] leading-snug text-ink">
          {resultado}
        </p>
      )}
    </div>
  );
}

function DocumentReader({
  modulo,
  caseId,
  onCaseUpdated,
  disabled,
}: {
  modulo: ModuloInfo;
  caseId: string;
  onCaseUpdated: (updated: CaseState) => void;
  disabled: boolean;
}) {
  const id = useId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [campos, setCampos] = useState<CampoPropuesto[] | null>(null);
  const [avisos, setAvisos] = useState<string[]>([]);
  const [resultado, setResultado] = useState<string | null>(null);

  async function read(file: File) {
    setError(null);
    setResultado(null);
    setBusy(true);
    try {
      const resp = await readModuleDocument({ clave: modulo.clave, caseId, filename: file.name, contentBase64: await fileToBase64(file) });
      setCampos(resp.propuesta.campos ?? []);
      setAvisos([...(resp.propuesta.avisos ?? []), ...(resp.propuesta.descartados ?? []).map((d) => `Descartado: ${d}`)]);
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  async function use() {
    if (!campos || !modulo.herramienta_de_documento) return;
    setError(null);
    setBusy(true);
    try {
      const resp = await runModuleTool({
        clave: modulo.clave,
        herramienta: modulo.herramienta_de_documento,
        caseId,
        argumentos: Object.fromEntries(campos.filter((c) => c.valor.trim()).map((c) => [c.campo, c.valor.trim()])),
      });
      if (resp.resultado) {
        setResultado(resp.resultado.contenido);
        if (resp.resultado.es_error) setError(resp.resultado.contenido);
        else setCampos(null);
      } else if (resp.accion) {
        setResultado("Ha quedado como acción pendiente de tu confirmación.");
      }
      // Facts the tool saved (the panel's case refreshes through the parent).
      if (resp.case_updated) onCaseUpdated(await getCase({ caseId }));
    } catch (err) {
      setError(message(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 rounded-lg border border-dashed border-line px-3 py-2.5">
      <label htmlFor={id} className="flex cursor-pointer items-center gap-2 text-[13.5px] font-semibold text-primary-dark">
        <FileUp size={15} aria-hidden />
        {busy && !campos ? "Leyendo el documento…" : "Leer un documento (tique, factura…)"}
      </label>
      <input
        id={id}
        type="file"
        accept=".pdf,.txt,image/*"
        className="sr-only"
        disabled={busy || disabled}
        onChange={(e) => {
          const file = e.target.files?.[0];
          e.target.value = "";
          if (file) void read(file);
        }}
      />
      <p className="mt-1 text-[12px] leading-snug text-muted">
        Se envía al proveedor del modelo de lenguaje para leerlo y no se guarda. Revisa los datos antes de usarlos.
      </p>
      {error && (
        <p role="alert" className="mt-1.5 text-[12.5px] text-risk-high-fg">
          {error}
        </p>
      )}
      {campos && (
        <div className="mt-2 space-y-2">
          {campos.length === 0 && <p className="text-[13px] text-muted">No he encontrado datos que pueda respaldar con el texto del documento.</p>}
          {campos.map((c, i) => (
            <div key={c.campo}>
              <label htmlFor={`${id}-${c.campo}`} className="text-[12.5px] font-semibold text-ink">
                {c.campo.replace(/_/g, " ")}
              </label>
              <input
                id={`${id}-${c.campo}`}
                value={c.valor}
                onChange={(e) => setCampos((prev) => prev?.map((p, j) => (j === i ? { ...p, valor: e.target.value } : p)) ?? prev)}
                className={FIELD_CLASS}
              />
              <p className="text-[11.5px] text-muted">En el documento: «{c.evidencia}»</p>
            </div>
          ))}
          {avisos.length > 0 && (
            <ul className="list-disc pl-5 text-[12px] text-muted">
              {avisos.map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          )}
          {campos.length > 0 && modulo.herramienta_de_documento && (
            <button type="button" className={BUTTON_PRIMARY} disabled={busy || disabled} onClick={() => void use()}>
              <Check size={14} aria-hidden />
              Usar estos datos
            </button>
          )}
        </div>
      )}
      {resultado && !error && (
        <p role="status" className="mt-2 whitespace-pre-line text-[13px] leading-snug text-ink">
          {resultado}
        </p>
      )}
    </div>
  );
}
