// "Tu expediente": what the advisor has recorded about the user in this
// conversation. Answers depend on these facts, so the user must be able to
// see them and remove a wrong one.

import { CalendarClock, Trash2, X } from "lucide-react";
import { useEffect } from "react";

import type { Fact, ObligationCandidate } from "../api";
import { factLabel, factValue, formatDate } from "../lib/presentation";

interface CasePanelProps {
  open: boolean;
  onClose: () => void;
  facts: Fact[];
  taxPeriod: string | null;
  obligations: ObligationCandidate[];
  onRemoveFact: (factName: string) => void;
  removing: string | null;
  error: string | null;
  disabled: boolean;
}

// The fiscal year is shown as its own headline row, not twice.
const HIDDEN_FACTS = new Set(["periodo_fiscal", "menciona_ingresos"]);

export function CasePanel({ open, onClose, facts, taxPeriod, obligations, onRemoveFact, removing, error, disabled }: CasePanelProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  const visible = facts.filter((f) => !HIDDEN_FACTS.has(f.name));

  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <button type="button" aria-label="Cerrar el expediente" className="absolute inset-0 bg-ink/40" onClick={onClose} />
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Tu expediente"
        className="relative flex h-full w-full max-w-[400px] flex-col border-l border-line bg-paper shadow-pop"
      >
        <header className="flex items-center justify-between border-b border-line px-5 py-4">
          <div>
            <h2 className="font-serif text-[19px] font-bold text-ink">Tu expediente</h2>
            <p className="text-[12.5px] text-muted">Lo que sé de ti en esta consulta</p>
          </div>
          <button type="button" onClick={onClose} aria-label="Cerrar" className="rounded-lg p-2 text-muted hover:bg-line/60 hover:text-ink">
            <X size={18} />
          </button>
        </header>

        <div className="flex-1 space-y-6 overflow-y-auto px-5 py-5">
          <section>
            <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">Tus datos</h3>
            {taxPeriod && (
              <p className="mb-2 rounded-lg bg-primary-soft px-3 py-2 text-[13.5px] text-primary-dark">
                Ejercicio fiscal: <strong className="font-bold">{taxPeriod}</strong>
              </p>
            )}
            {visible.length === 0 ? (
              <p className="text-[13.5px] leading-relaxed text-ink-soft">
                Todavía no he anotado nada. A medida que me cuentes tu situación (dónde resides, qué ingresos tienes, si eres
                autónomo…), lo verás aquí.
              </p>
            ) : (
              <ul className="divide-y divide-line rounded-xl border border-line bg-surface">
                {visible.map((fact) => (
                  <li key={fact.fact_id} className="flex items-center gap-3 px-3.5 py-2.5">
                    <div className="min-w-0 flex-1">
                      <p className="text-[12px] text-muted">{factLabel(fact.name)}</p>
                      <p className="truncate text-[14px] font-medium text-ink">{factValue(fact)}</p>
                    </div>
                    <button
                      type="button"
                      onClick={() => onRemoveFact(fact.name)}
                      disabled={disabled || removing !== null}
                      aria-label={`Eliminar el dato «${factLabel(fact.name)}»`}
                      title="Eliminar este dato"
                      className="rounded-lg p-2 text-muted transition hover:bg-risk-high-bg hover:text-risk-high-fg disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      <Trash2 size={15} className={removing === fact.name ? "animate-pulse" : undefined} />
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {error && (
              <p role="alert" className="mt-2 text-[12.5px] text-risk-high-fg">
                {error}
              </p>
            )}
            {visible.length > 0 && (
              <p className="mt-2 text-[12.5px] leading-relaxed text-muted">
                ¿Algo está mal? Elimínalo y dime el dato correcto en el chat.
              </p>
            )}
          </section>

          {obligations.length > 0 && (
            <section>
              <h3 className="mb-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">Posibles obligaciones</h3>
              <ul className="space-y-2">
                {obligations.map((o) => (
                  <li key={o.obligation_id} className="rounded-xl border border-line bg-surface px-3.5 py-2.5">
                    <p className="text-[14px] font-semibold text-ink">{o.title}</p>
                    {o.due_date && (
                      <p className="mt-0.5 flex items-center gap-1.5 text-[12.5px] text-ink-soft">
                        <CalendarClock size={13} aria-hidden />
                        Hasta el {formatDate(o.due_date)}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>

        <footer className="border-t border-line px-5 py-3 text-[12px] leading-relaxed text-muted">
          Estos datos se guardan con tu consulta para no volver a preguntártelos.
        </footer>
      </aside>
    </div>
  );
}
