// Two blocks of the case panel that talk to the server on their own:
// upcoming deadlines, and "read my document" with confirm-before-save.

import { CalendarClock, FileUp, Loader2, TriangleAlert } from "lucide-react";
import clsx from "clsx";
import { useEffect, useRef, useState } from "react";

import type { Deadline, DocumentAnalysis } from "../api";
import { ApiError, analyzeDocument, getCalendar } from "../api";
import { factLabel, factValue, fileToBase64, formatDate } from "../lib/presentation";

function message(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

export function DeadlinesSection({ caseId, refreshKey }: { caseId: string | null; refreshKey: string }) {
  const [state, setState] = useState<{ key: string; items: Deadline[]; aviso: string; error: string | null } | null>(null);

  useEffect(() => {
    if (!caseId) return;
    const controller = new AbortController();
    const key = `${caseId}|${refreshKey}`;
    getCalendar({ caseId, dias: 270, signal: controller.signal }).then(
      (calendar) => setState({ key, items: calendar.vencimientos, aviso: calendar.aviso, error: null }),
      (err: unknown) => {
        if (!controller.signal.aborted) setState({ key, items: [], aviso: "", error: message(err, "No se pudieron cargar los plazos.") });
      },
    );
    return () => controller.abort();
  }, [caseId, refreshKey]);

  if (!caseId) return null;
  return (
    <section>
      <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">Próximos plazos</h3>
      {!state && <p className="text-[13px] text-muted">Cargando…</p>}
      {state?.error && <p className="text-[13px] text-risk-high-fg">{state.error}</p>}
      {state && !state.error && (
        <>
          <ul className="divide-y divide-line rounded-xl border border-line bg-surface">
            {state.items.map((d) => {
              const urgent = d.dias_restantes <= 30;
              return (
                <li key={d.id} className="px-3.5 py-2.5">
                  <div className="flex items-start gap-2.5">
                    <CalendarClock size={15} aria-hidden className={clsx("mt-0.5 flex-shrink-0", urgent ? "text-risk-high-fg" : "text-primary-dark")} />
                    <div className="min-w-0">
                      <p className="text-[13.5px] font-semibold text-ink">
                        {formatDate(d.fecha)}
                        <span className={clsx("ml-2 text-[12px] font-medium", urgent ? "text-risk-high-fg" : "text-muted")}>
                          {d.dias_restantes === 0 ? "hoy" : d.dias_restantes === 1 ? "mañana" : `en ${d.dias_restantes} días`}
                        </span>
                      </p>
                      <p className="text-[13px] text-ink-soft">
                        {d.titulo} · {d.periodo}
                        {d.modelo ? ` · modelo ${d.modelo}` : ""}
                      </p>
                      <details className="mt-0.5">
                        <summary className="cursor-pointer text-[11.5px] text-muted hover:text-ink-soft">Detalle</summary>
                        <p className="mt-1 text-[12px] leading-snug text-ink-soft">{d.detalle}</p>
                        <p className="mt-1 text-[11.5px] text-muted">
                          {d.referencia}
                          {!d.verificado && " · fecha habitual, pendiente de confirmar con la orden del año"}
                        </p>
                        {d.nota && <p className="mt-1 text-[11.5px] text-muted">{d.nota}</p>}
                      </details>
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
          <p className="mt-2 text-[11.5px] leading-relaxed text-muted">
            Solo aparecen las obligaciones que constan en tus datos. No se tienen en cuenta los festivos.
          </p>
        </>
      )}
    </section>
  );
}

interface DocumentSectionProps {
  caseId: string | null;
  disabled: boolean;
  // Persist one accepted fact. Resolves true when saved.
  onApply: (factName: string, value: string | number | boolean) => Promise<boolean>;
}

export function DocumentSection({ caseId, disabled, onApply }: DocumentSectionProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [status, setStatus] = useState<"idle" | "reading" | "saving">("idle");
  const [error, setError] = useState<string | null>(null);
  const [analysis, setAnalysis] = useState<DocumentAnalysis | null>(null);
  const [accepted, setAccepted] = useState<Set<string>>(new Set());
  const [savedCount, setSavedCount] = useState<number | null>(null);

  async function handleFile(file: File) {
    if (!caseId) return;
    setStatus("reading");
    setError(null);
    setAnalysis(null);
    setSavedCount(null);
    try {
      if (file.size > 10 * 1024 * 1024) throw new ApiError("El documento supera el tamaño máximo de 10 MB.");
      const contentBase64 = await fileToBase64(file);
      const result = await analyzeDocument({ caseId, filename: file.name, contentBase64 });
      setAnalysis(result);
      setAccepted(new Set(result.hechos_propuestos.map((f) => f.name)));
    } catch (err) {
      setError(message(err, "No se pudo leer el documento."));
    } finally {
      setStatus("idle");
      if (inputRef.current) inputRef.current.value = "";
    }
  }

  async function saveAccepted() {
    if (!analysis) return;
    setStatus("saving");
    let saved = 0;
    try {
      for (const fact of analysis.hechos_propuestos) {
        if (!accepted.has(fact.name)) continue;
        if (!(await onApply(fact.name, fact.value))) return;
        saved += 1;
      }
      setSavedCount(saved);
      setAnalysis(null);
    } finally {
      setStatus("idle");
    }
  }

  return (
    <section>
      <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">Tus documentos</h3>
      <input
        ref={inputRef}
        type="file"
        accept=".pdf,.txt,.csv,application/pdf,text/plain"
        className="sr-only"
        tabIndex={-1}
        aria-hidden
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) void handleFile(file);
        }}
      />
      <button
        type="button"
        onClick={() => inputRef.current?.click()}
        disabled={!caseId || disabled || status !== "idle"}
        className="flex w-full items-center justify-center gap-2 rounded-xl border border-dashed border-line bg-surface px-4 py-3 text-[13.5px] font-semibold text-ink-soft transition hover:border-primary hover:text-primary-dark disabled:cursor-not-allowed disabled:opacity-50"
      >
        {status === "reading" ? <Loader2 size={16} aria-hidden className="animate-spin" /> : <FileUp size={16} aria-hidden />}
        {status === "reading" ? "Leyendo el documento…" : "Leer un certificado o los datos fiscales (PDF)"}
      </button>
      <p className="mt-2 text-[11.5px] leading-relaxed text-muted">
        {caseId
          ? "Extraigo las cifras y te las enseño para que las confirmes antes de guardarlas. El documento no se almacena; su texto se envía al modelo de lenguaje para leerlo."
          : "Haz primero una consulta para abrir tu expediente."}
      </p>

      {error && (
        <p role="alert" className="mt-2 flex gap-2 rounded-lg border border-risk-high-fg/30 bg-risk-high-bg px-3 py-2 text-[13px] text-ink-soft">
          <TriangleAlert size={15} aria-hidden className="mt-0.5 flex-shrink-0 text-risk-high-fg" />
          {error}
        </p>
      )}
      {savedCount !== null && (
        <p role="status" className="mt-2 rounded-lg bg-grounding-cited-bg px-3 py-2 text-[13px] font-medium text-grounding-cited-fg">
          {savedCount === 1 ? "1 dato guardado en tu expediente." : `${savedCount} datos guardados en tu expediente.`}
        </p>
      )}

      {analysis && (
        <div className="mt-3 rounded-xl border border-line bg-surface">
          <p className="border-b border-line px-3.5 py-2.5 text-[13px] text-ink-soft">
            <strong className="font-semibold text-ink">{analysis.tipo_documento}</strong> · {analysis.filename}
          </p>
          {analysis.hechos_propuestos.length > 0 && (
            <ul className="divide-y divide-line">
              {analysis.hechos_propuestos.map((fact) => (
                <li key={fact.name}>
                  <label className="flex cursor-pointer items-start gap-3 px-3.5 py-2.5">
                    <input
                      type="checkbox"
                      checked={accepted.has(fact.name)}
                      onChange={(e) =>
                        setAccepted((prev) => {
                          const next = new Set(prev);
                          if (e.target.checked) next.add(fact.name);
                          else next.delete(fact.name);
                          return next;
                        })
                      }
                      className="mt-1 h-4 w-4 accent-[rgb(var(--primary))]"
                    />
                    <span className="min-w-0">
                      <span className="block text-[12px] text-muted">{factLabel(fact.name)}</span>
                      <span className="block text-[14px] font-medium text-ink">{factValue(fact)}</span>
                      <span className="mt-0.5 block text-[11.5px] italic leading-snug text-muted">«{fact.evidencia}»</span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
          {analysis.avisos.map((aviso, i) => (
            <p key={i} className="border-t border-line px-3.5 py-2 text-[12px] leading-snug text-ink-soft">
              {aviso}
            </p>
          ))}
          <div className="flex gap-2 border-t border-line px-3.5 py-2.5">
            {analysis.hechos_propuestos.length > 0 && (
              <button
                type="button"
                onClick={() => void saveAccepted()}
                disabled={accepted.size === 0 || status !== "idle"}
                className="rounded-lg bg-primary px-3 py-1.5 text-[13px] font-semibold text-white transition hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-50"
              >
                {status === "saving" ? "Guardando…" : `Guardar ${accepted.size} ${accepted.size === 1 ? "dato" : "datos"}`}
              </button>
            )}
            <button type="button" onClick={() => setAnalysis(null)} className="rounded-lg px-3 py-1.5 text-[13px] font-semibold text-ink-soft hover:bg-line/60">
              Descartar
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
