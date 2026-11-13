// Sidebar = brand + new-consultation CTA + the user's real conversation
// history + coverage + disclaimer. On small screens it is an off-canvas drawer.

import { Info, MessageSquare, Plus, Trash2, X } from "lucide-react";
import clsx from "clsx";

import { BrandHeader } from "./Brand";
import { TOPICS } from "../lib/mock";
import { relativeTime } from "../lib/presentation";
import type { Conversation } from "../lib/storage";

interface SidebarProps {
  conversations: Conversation[];
  activeId: string;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  onNewConsultation: () => void;
  open: boolean; // mobile drawer state; ignored on large screens
  onClose: () => void;
}

export function Sidebar({ conversations, activeId, onSelect, onDelete, onNewConsultation, open, onClose }: SidebarProps) {
  return (
    <>
      {open && <button type="button" aria-label="Cerrar el menú" className="fixed inset-0 z-20 bg-ink/40 lg:hidden" onClick={onClose} />}
      <aside
        className={clsx(
          "fixed inset-y-0 left-0 z-30 flex w-[290px] flex-shrink-0 flex-col gap-5 border-r border-line bg-paper px-4 py-5 transition-transform duration-200 lg:static lg:translate-x-0",
          open ? "translate-x-0 shadow-pop" : "-translate-x-full",
        )}
      >
        <div className="flex items-center justify-between">
          <BrandHeader />
          <button type="button" onClick={onClose} aria-label="Cerrar el menú" className="rounded-lg p-2 text-muted hover:bg-line/60 lg:hidden">
            <X size={18} />
          </button>
        </div>

        <button
          type="button"
          onClick={onNewConsultation}
          className="flex items-center justify-center gap-2 rounded-xl bg-primary px-4 py-3 text-[15px] font-semibold text-white shadow-card transition hover:bg-primary-hover"
        >
          <Plus size={18} aria-hidden />
          Nueva consulta
        </button>

        <nav aria-label="Consultas recientes" className="flex min-h-0 flex-1 flex-col">
          <h2 className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">Recientes</h2>
          {conversations.length === 0 ? (
            <p className="px-2 py-2 text-[13px] leading-relaxed text-muted">
              Tus consultas aparecerán aquí y se conservan en este navegador.
            </p>
          ) : (
            <ul className="-mr-2 flex-1 space-y-0.5 overflow-y-auto pr-2">
              {conversations.map((c) => {
                const active = c.id === activeId;
                return (
                  <li key={c.id} className="group relative">
                    <button
                      type="button"
                      onClick={() => onSelect(c.id)}
                      aria-current={active ? "page" : undefined}
                      className={clsx(
                        "flex w-full items-start gap-2 rounded-r-lg border-l-[3px] py-2 pl-3 pr-9 text-left transition",
                        active ? "border-primary bg-primary-soft" : "border-transparent hover:bg-line/60",
                      )}
                    >
                      <MessageSquare size={14} aria-hidden className={clsx("mt-1 flex-shrink-0", active ? "text-primary-dark" : "text-muted")} />
                      <span className="min-w-0">
                        <span className={clsx("block truncate text-[14px]", active ? "font-semibold text-primary-dark" : "text-ink-soft")}>
                          {c.title}
                        </span>
                        <span className="block text-[11.5px] text-muted">{relativeTime(c.updatedAt)}</span>
                      </span>
                    </button>
                    <button
                      type="button"
                      onClick={() => onDelete(c.id)}
                      aria-label={`Eliminar la consulta «${c.title}»`}
                      title="Eliminar consulta"
                      className="absolute right-1 top-1/2 -translate-y-1/2 rounded-md p-1.5 text-muted opacity-0 transition hover:bg-risk-high-bg hover:text-risk-high-fg focus-visible:opacity-100 group-hover:opacity-100 max-lg:opacity-100"
                    >
                      <Trash2 size={14} />
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </nav>

        <div className="rounded-xl border border-line bg-surface px-4 py-3">
          <h3 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">Qué cubre</h3>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {TOPICS.map((t) => (
              <span key={t} className="rounded-pill bg-primary-soft px-2.5 py-1 text-[12px] font-medium text-primary-dark">
                {t}
              </span>
            ))}
          </div>
        </div>

        <p className="flex gap-2 text-[12px] leading-relaxed text-muted">
          <Info size={14} aria-hidden className="mt-0.5 flex-shrink-0 text-grounding-uncited-fg" />
          <span>
            <strong className="font-semibold text-ink-soft">Herramienta de apoyo.</strong> No sustituye el asesoramiento
            fiscal profesional ni el criterio oficial.
          </span>
        </p>
      </aside>
    </>
  );
}
