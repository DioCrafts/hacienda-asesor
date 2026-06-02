// Sidebar = brand + new-consultation CTA + recents list + topic chips +
// disclaimer. All copy lives in mock.ts so it's trivial to localise.

import { Info, MessageSquare, Plus } from "lucide-react";
import clsx from "clsx";

import { BrandHeader } from "./Brand";
import { MOCK_RECENT_CASES, TOPICS } from "../lib/mock";

interface SidebarProps {
  onNewConsultation: () => void;
}

export function Sidebar({ onNewConsultation }: SidebarProps) {
  return (
    <aside className="flex h-full w-[300px] flex-shrink-0 flex-col gap-6 border-r border-line bg-paper px-5 py-5">
      <BrandHeader />

      <button
        type="button"
        onClick={onNewConsultation}
        className="flex items-center justify-center gap-2 rounded-xl bg-primary px-4 py-3 text-[15px] font-semibold text-white shadow-card transition hover:bg-primary-dark"
      >
        <Plus size={18} />
        Nueva consulta
      </button>

      <RecentsList />

      <div className="mt-auto" />

      <TopicsBlock />

      <SidebarDisclaimer />
    </aside>
  );
}

function RecentsList() {
  return (
    <nav className="flex flex-col gap-1">
      <h2 className="px-2 pb-1 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">
        Recientes
      </h2>
      {MOCK_RECENT_CASES.map((c) => (
        <button
          key={c.id}
          type="button"
          className={clsx(
            "flex items-center gap-2 rounded-lg px-2 py-2 text-left text-[14px] transition",
            c.active
              ? "bg-primary-soft font-medium text-primary-dark"
              : "text-ink-soft hover:bg-line/60",
          )}
        >
          <MessageSquare
            size={14}
            className={c.active ? "text-primary" : "text-muted"}
          />
          <span className="truncate">{c.title}</span>
        </button>
      ))}
    </nav>
  );
}

function TopicsBlock() {
  return (
    <div className="rounded-xl border border-line bg-surface px-4 py-3">
      <h3 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">
        Qué cubre hoy
      </h3>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {TOPICS.map((t) => (
          <span
            key={t}
            className="rounded-pill bg-primary-soft px-2.5 py-1 text-[12px] font-medium text-primary-dark"
          >
            {t}
          </span>
        ))}
      </div>
    </div>
  );
}

function SidebarDisclaimer() {
  return (
    <p className="flex gap-2 text-[12px] leading-relaxed text-muted">
      <Info size={14} className="mt-0.5 flex-shrink-0 text-risk-medium-fg" />
      <span>
        <strong className="font-semibold text-ink-soft">
          Herramienta de apoyo.
        </strong>{" "}
        No sustituye asesoramiento fiscal profesional ni criterio oficial.
      </span>
    </p>
  );
}
