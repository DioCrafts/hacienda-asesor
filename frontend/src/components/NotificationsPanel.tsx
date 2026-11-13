// What the advisor has to tell the user without being asked: deadlines coming
// up, the year-end plan, law changes that affect their profile, the notices of
// the household modules the user switched on, and the weekly digest. Grouped by
// module, the digest first.

import { Bell, CalendarClock, Check, House, Landmark, ListChecks, PiggyBank, RefreshCw, X } from "lucide-react";
import clsx from "clsx";
import { useEffect } from "react";

import type { AdvisorNotification } from "../api";
import { moduloDeKind } from "../apiModulos";
import { formatDate, formatMoney, relativeTime } from "../lib/presentation";
import { RichText } from "./RichText";

const KIND: Record<string, { icon: typeof Bell; label: string }> = {
  plazo: { icon: CalendarClock, label: "Plazo" },
  fin_de_ejercicio: { icon: PiggyBank, label: "Antes de fin de año" },
  cambio_normativo: { icon: Landmark, label: "Cambio normativo" },
  resumen_semanal: { icon: ListChecks, label: "Resumen semanal" },
};

function kindInfo(kind: string, moduleNames: Record<string, string>): { icon: typeof Bell; label: string } {
  const known = KIND[kind];
  if (known) return known;
  const clave = moduloDeKind(kind);
  return { icon: House, label: clave ? (moduleNames[clave] ?? clave) : "Aviso" };
}

interface Group {
  key: string;
  title: string;
  items: AdvisorNotification[];
}

/** The digest first, then the tax notices, then each module (by name); newest first inside each. */
function groupByModule(notifications: AdvisorNotification[], moduleNames: Record<string, string>): Group[] {
  const groups = new Map<string, Group>();
  for (const n of notifications) {
    const clave = moduloDeKind(n.kind) ?? "resumen";
    const title = clave === "resumen" ? "Resumen semanal" : clave === "fiscal" ? "Impuestos" : (moduleNames[clave] ?? clave);
    const group = groups.get(clave) ?? { key: clave, title, items: [] };
    group.items.push(n);
    groups.set(clave, group);
  }
  const rank = (g: Group) => (g.key === "resumen" ? 0 : g.key === "fiscal" ? 1 : 2);
  return [...groups.values()].sort((a, b) => rank(a) - rank(b) || a.title.localeCompare(b.title, "es"));
}

interface NotificationsPanelProps {
  open: boolean;
  onClose: () => void;
  notifications: AdvisorNotification[];
  hasCase: boolean;
  loading: boolean;
  error: string | null;
  onMarkRead: (id: string) => void;
  onRefresh: () => void;
  // Ask the advisor about a notification (prefills and sends the question).
  onAsk: (question: string) => void;
  // Display names of the household modules, by key.
  moduleNames?: Record<string, string>;
}

export function NotificationsPanel({ open, onClose, notifications, hasCase, loading, error, onMarkRead, onRefresh, onAsk, moduleNames = {} }: NotificationsPanelProps) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div className="fixed inset-0 z-40 flex justify-end">
      <button type="button" aria-label="Cerrar los avisos" className="absolute inset-0 bg-ink/40" onClick={onClose} />
      <aside role="dialog" aria-modal="true" aria-label="Avisos de tu asesor" className="relative flex h-full w-full max-w-[440px] flex-col border-l border-line bg-paper shadow-pop">
        <header className="flex items-center justify-between border-b border-line px-5 py-4">
          <div>
            <h2 className="flex items-center gap-2 font-serif text-[19px] font-bold text-ink">
              <Bell size={18} aria-hidden className="text-primary-dark" />
              Avisos de tu asesor
            </h2>
            <p className="text-[12.5px] text-muted">Plazos, plan de fin de año, cambios que te afectan y avisos de tus módulos</p>
          </div>
          <div className="flex items-center gap-1">
            {hasCase && (
              <button type="button" onClick={onRefresh} disabled={loading} aria-label="Actualizar los avisos" title="Actualizar" className="rounded-lg p-2 text-muted hover:bg-line/60 hover:text-ink disabled:opacity-50">
                <RefreshCw size={16} className={loading ? "animate-spin" : undefined} />
              </button>
            )}
            <button type="button" onClick={onClose} aria-label="Cerrar" className="rounded-lg p-2 text-muted hover:bg-line/60 hover:text-ink">
              <X size={18} />
            </button>
          </div>
        </header>

        <div className="flex-1 space-y-3 overflow-y-auto px-5 py-5">
          {error && (
            <p role="alert" className="rounded-lg border border-risk-high-fg/30 bg-risk-high-bg px-3 py-2 text-[13px] text-ink-soft">
              {error}
            </p>
          )}
          {!hasCase && (
            <p className="text-[13.5px] leading-relaxed text-ink-soft">
              Cuando me cuentes tu situación, aquí te avisaré de los plazos que te tocan y de lo que puedes hacer antes de fin de año para pagar menos.
            </p>
          )}
          {hasCase && !error && notifications.length === 0 && !loading && (
            <p className="text-[13.5px] leading-relaxed text-ink-soft">
              No tengo nada que avisarte ahora. Cuantos más datos tenga de tu situación (ingresos, familia, si tienes actividad económica), más útiles serán los avisos.
            </p>
          )}
          {groupByModule(notifications, moduleNames).map((group) => (
            <section key={group.key} aria-label={`Avisos: ${group.title}`} className="space-y-3">
              <h3 className="pt-1 text-[12px] font-bold uppercase tracking-[0.08em] text-ink-soft">
                {group.title} <span className="font-normal text-muted">({group.items.length})</span>
              </h3>
          {group.items.map((n) => {
            const kind = kindInfo(n.kind, moduleNames);
            const Icon = kind.icon;
            const unread = !n.read_at;
            const fiscal = moduloDeKind(n.kind) === "fiscal";
            return (
              <article
                key={n.id}
                className={clsx(
                  "rounded-xl border bg-surface px-4 py-3.5 shadow-card",
                  n.severity === "urgente" ? "border-risk-high-fg/40" : unread ? "border-primary/50" : "border-line",
                )}
              >
                <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[11.5px] font-semibold uppercase tracking-[0.08em] text-muted">
                  <Icon size={14} aria-hidden className={n.severity === "urgente" ? "text-risk-high-fg" : "text-primary-dark"} />
                  {kind.label}
                  {n.severity === "urgente" && <span className="rounded-pill bg-risk-high-bg px-2 py-px normal-case tracking-normal text-risk-high-fg">Urgente</span>}
                  {unread && <span className="rounded-pill bg-primary px-2 py-px normal-case tracking-normal text-white">Nuevo</span>}
                  <span className="ml-auto font-normal normal-case tracking-normal">{relativeTime(Date.parse(n.created_at))}</span>
                </p>
                <h4 className="mt-1.5 text-[15px] font-bold leading-snug text-ink">{n.title}</h4>
                {(n.amount && Number(n.amount) > 0) || n.due_date ? (
                  <p className="mt-1 flex flex-wrap gap-2 text-[12.5px]">
                    {n.amount && Number(n.amount) > 0 && fiscal && (
                      <span className="rounded-pill bg-grounding-cited-bg px-2 py-0.5 font-bold text-grounding-cited-fg">hasta −{formatMoney(n.amount)} (estimado)</span>
                    )}
                    {n.amount && Number(n.amount) > 0 && !fiscal && (
                      <span className="rounded-pill bg-line/70 px-2 py-0.5 font-bold text-ink">importe: {formatMoney(n.amount)}</span>
                    )}
                    {n.due_date && <span className="rounded-pill bg-grounding-abstain-bg px-2 py-0.5 font-semibold text-grounding-abstain-fg">hasta el {formatDate(n.due_date)}</span>}
                  </p>
                ) : null}
                <div className="mt-2 text-[13.5px] [&_div]:text-[13.5px] [&_div]:leading-snug">
                  <RichText text={n.body} />
                </div>
                {n.actions.length > 0 && (
                  <ul className="mt-2 list-disc space-y-1 pl-5 text-[13px] leading-snug text-ink">
                    {n.actions.map((action, i) => (
                      <li key={i}>{action}</li>
                    ))}
                  </ul>
                )}
                {n.sources.length > 0 && <p className="mt-2 text-[11.5px] leading-snug text-muted">{n.sources.join(" · ")}</p>}
                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => onAsk(`Sobre el aviso «${n.title}»: explícame cómo me afecta y qué debería hacer.`)}
                    className="rounded-lg border border-line px-2.5 py-1 text-[12.5px] font-semibold text-primary-dark transition hover:border-primary"
                  >
                    Preguntar al asesor
                  </button>
                  {unread && (
                    <button type="button" onClick={() => onMarkRead(n.id)} className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1 text-[12.5px] font-semibold text-ink-soft hover:bg-line/60">
                      <Check size={13} aria-hidden />
                      Marcar como leído
                    </button>
                  )}
                </div>
              </article>
            );
          })}
            </section>
          ))}
        </div>
        <footer className="border-t border-line px-5 py-3 text-[11.5px] leading-relaxed text-muted">
          Los importes son estimaciones calculadas con los datos de tu expediente, cada uno por su módulo (no se suman entre sí); los plazos no tienen en cuenta festivos.
        </footer>
      </aside>
    </div>
  );
}
