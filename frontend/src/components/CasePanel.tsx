// "Tu expediente": what the advisor has recorded about the user in this
// conversation. Answers depend on these facts, so the user can see every one,
// correct it in place, or remove it.

import { CalendarClock, Check, Pencil, Trash2, X } from "lucide-react";
import { useEffect, useState } from "react";

import type { Fact, ObligationCandidate } from "../api";
import { factInput, factLabel, factValue, formatDate } from "../lib/presentation";

type FactValue = string | number | boolean;

interface CasePanelProps {
  open: boolean;
  onClose: () => void;
  facts: Fact[];
  taxPeriod: string | null;
  obligations: ObligationCandidate[];
  hasCase: boolean;
  // Resolves true when the change was saved. `null` deletes the fact.
  onChangeFact: (factName: string, value: FactValue | null) => Promise<boolean>;
  saving: string | null;
  error: string | null;
  disabled: boolean;
}

// Internal flag, not something the user states or needs to manage.
const HIDDEN_FACTS = new Set(["menciona_ingresos"]);

const FIELD_CLASS =
  "w-full rounded-lg border border-line bg-paper px-2.5 py-1.5 text-[16px] text-ink focus:border-primary focus:outline-none sm:text-[14px]";

interface EditorProps {
  fact: Pick<Fact, "name" | "value">;
  busy: boolean;
  onSave: (value: FactValue) => void;
  onCancel: () => void;
}

function FactEditor({ fact, busy, onSave, onCancel }: EditorProps) {
  const input = factInput(fact);
  const [draft, setDraft] = useState<string>(() => String(fact.value));
  const label = factLabel(fact.name);

  function save() {
    if (input.kind === "choice") {
      const picked = input.options.find((o) => String(o.value) === draft);
      if (picked) onSave(picked.value);
      return;
    }
    const trimmed = draft.trim();
    if (trimmed) onSave(trimmed); // the server parses amounts ("45.000 €") and validates years
  }

  return (
    <form
      className="flex flex-col gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        save();
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") {
          // Cancel the edit only; do not let the panel's Escape handler close it.
          e.stopPropagation();
          e.nativeEvent.stopImmediatePropagation();
          onCancel();
        }
      }}
    >
      <label className="text-[12px] text-muted" htmlFor={`fact-${fact.name}`}>
        {label}
      </label>
      {input.kind === "choice" ? (
        <select id={`fact-${fact.name}`} autoFocus value={draft} onChange={(e) => setDraft(e.target.value)} className={FIELD_CLASS}>
          {input.options.map((o) => (
            <option key={String(o.value)} value={String(o.value)}>
              {o.label}
            </option>
          ))}
        </select>
      ) : (
        <input
          id={`fact-${fact.name}`}
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          inputMode={input.kind === "text" ? "text" : input.kind === "year" ? "numeric" : "decimal"}
          maxLength={input.kind === "year" ? 4 : 120}
          placeholder={input.kind === "money" ? "Importe en euros" : input.kind === "year" ? "Año, p. ej. 2025" : undefined}
          className={FIELD_CLASS}
        />
      )}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={busy || !draft.trim()}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-1.5 text-[13px] font-semibold text-white transition hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Check size={14} aria-hidden />
          {busy ? "Guardando…" : "Guardar"}
        </button>
        <button type="button" onClick={onCancel} disabled={busy} className="rounded-lg px-3 py-1.5 text-[13px] font-semibold text-ink-soft hover:bg-line/60">
          Cancelar
        </button>
      </div>
    </form>
  );
}

export function CasePanel({ open, onClose, facts, taxPeriod, obligations, hasCase, onChangeFact, saving, error, disabled }: CasePanelProps) {
  // Which row is in edit mode / awaiting delete confirmation (by fact name).
  const [editing, setEditing] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  // The fiscal year lives on the case even before the advisor records it as a
  // fact; show it as the first, always-present row.
  const yearFact: Pick<Fact, "name" | "value"> | null = taxPeriod ? { name: "periodo_fiscal", value: taxPeriod } : null;
  const rows: Array<Pick<Fact, "name" | "value"> & { removable: boolean }> = [
    ...(yearFact ? [{ ...yearFact, removable: false }] : []),
    ...facts.filter((f) => f.name !== "periodo_fiscal" && !HIDDEN_FACTS.has(f.name)).map((f) => ({ name: f.name, value: f.value, removable: true })),
  ];
  const locked = disabled || saving !== null || !hasCase;

  async function save(name: string, value: FactValue | null) {
    if (await onChangeFact(name, value)) {
      setEditing(null);
      setConfirming(null);
    }
  }

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
            {rows.length === 0 ? (
              <p className="text-[13.5px] leading-relaxed text-ink-soft">
                Todavía no he anotado nada. A medida que me cuentes tu situación (dónde resides, qué ingresos tienes, si eres
                autónomo…), lo verás aquí y podrás corregirlo.
              </p>
            ) : (
              <ul className="divide-y divide-line rounded-xl border border-line bg-surface">
                {rows.map((row) => {
                  const label = factLabel(row.name);
                  if (editing === row.name) {
                    return (
                      <li key={row.name} className="px-3.5 py-3">
                        <FactEditor fact={row} busy={saving === row.name} onSave={(value) => void save(row.name, value)} onCancel={() => setEditing(null)} />
                      </li>
                    );
                  }
                  if (confirming === row.name) {
                    return (
                      <li key={row.name} className="flex flex-wrap items-center gap-2 px-3.5 py-3">
                        <p className="min-w-0 flex-1 text-[13.5px] text-ink">¿Eliminar «{label}»?</p>
                        <button
                          type="button"
                          autoFocus
                          onClick={() => void save(row.name, null)}
                          disabled={saving !== null}
                          className="rounded-lg bg-risk-high-fg px-3 py-1.5 text-[13px] font-semibold text-white transition hover:opacity-90 disabled:opacity-50"
                        >
                          {saving === row.name ? "Eliminando…" : "Eliminar"}
                        </button>
                        <button type="button" onClick={() => setConfirming(null)} className="rounded-lg px-3 py-1.5 text-[13px] font-semibold text-ink-soft hover:bg-line/60">
                          Cancelar
                        </button>
                      </li>
                    );
                  }
                  return (
                    <li key={row.name} className="flex items-center gap-1 py-2.5 pl-3.5 pr-2">
                      <div className="min-w-0 flex-1">
                        <p className="text-[12px] text-muted">{label}</p>
                        <p className="truncate text-[14px] font-medium text-ink">{factValue(row)}</p>
                      </div>
                      <button
                        type="button"
                        onClick={() => {
                          setConfirming(null);
                          setEditing(row.name);
                        }}
                        disabled={locked}
                        aria-label={`Editar «${label}»`}
                        title="Editar"
                        className="rounded-lg p-2 text-muted transition hover:bg-primary-soft hover:text-primary-dark disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        <Pencil size={15} />
                      </button>
                      {row.removable && (
                        <button
                          type="button"
                          onClick={() => {
                            setEditing(null);
                            setConfirming(row.name);
                          }}
                          disabled={locked}
                          aria-label={`Eliminar «${label}»`}
                          title="Eliminar"
                          className="rounded-lg p-2 text-muted transition hover:bg-risk-high-bg hover:text-risk-high-fg disabled:cursor-not-allowed disabled:opacity-40"
                        >
                          <Trash2 size={15} />
                        </button>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
            {error && (
              <p role="alert" className="mt-2 text-[12.5px] text-risk-high-fg">
                {error}
              </p>
            )}
            {rows.length > 0 && (
              <p className="mt-2 text-[12.5px] leading-relaxed text-muted">
                Si corriges un dato, las posibles obligaciones se recalculan al momento. Las respuestas ya dadas no cambian:
                vuelve a preguntar para que las tenga en cuenta.
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
