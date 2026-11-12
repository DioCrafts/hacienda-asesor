// Top bar: menu (mobile), the conversation title, and the two global actions —
// open the user's case file and switch theme.

import { Bell, BriefcaseBusiness, Calculator, ClipboardList, Menu, Moon, Sun } from "lucide-react";

import type { Theme } from "../lib/theme";

interface TopBarProps {
  title: string;
  factCount: number;
  onOpenCase: () => void;
  onOpenSimulator: () => void;
  onOpenAutonomos: () => void;
  onOpenNotifications: () => void;
  unreadNotifications: number;
  onOpenMenu: () => void;
  theme: Theme;
  onToggleTheme: () => void;
}

export function TopBar({ title, factCount, onOpenCase, onOpenSimulator, onOpenAutonomos, onOpenNotifications, unreadNotifications, onOpenMenu, theme, onToggleTheme }: TopBarProps) {
  return (
    <header className="flex items-center gap-2 border-b border-line bg-paper px-3 py-3 sm:px-6">
      <button type="button" onClick={onOpenMenu} aria-label="Abrir el menú" className="rounded-lg p-2 text-ink-soft hover:bg-line/60 lg:hidden">
        <Menu size={20} />
      </button>
      <h1 className="min-w-0 flex-1 truncate font-serif text-[18px] font-bold text-ink sm:text-[20px]">{title}</h1>
      <button
        type="button"
        onClick={onOpenNotifications}
        aria-label={unreadNotifications > 0 ? `Avisos: ${unreadNotifications} sin leer` : "Avisos"}
        title="Avisos de tu asesor"
        className="relative rounded-lg border border-line bg-surface p-2 text-ink-soft transition hover:border-primary hover:text-primary-dark"
      >
        <Bell size={17} aria-hidden />
        {unreadNotifications > 0 && (
          <span className="absolute -right-1.5 -top-1.5 min-w-[18px] rounded-pill bg-risk-high-fg px-1 text-center text-[11px] font-bold leading-[18px] text-white">
            {unreadNotifications}
          </span>
        )}
      </button>
      <button
        type="button"
        onClick={onOpenSimulator}
        aria-label="Abrir el simulador de IRPF"
        className="flex items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2 text-[13.5px] font-semibold text-ink-soft transition hover:border-primary hover:text-primary-dark"
      >
        <Calculator size={16} aria-hidden />
        <span className="hidden md:inline">Simulador</span>
      </button>
      <button
        type="button"
        onClick={onOpenAutonomos}
        aria-label="Abrir los cálculos para autónomos"
        className="flex items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2 text-[13.5px] font-semibold text-ink-soft transition hover:border-primary hover:text-primary-dark"
      >
        <BriefcaseBusiness size={16} aria-hidden />
        <span className="hidden md:inline">Autónomos</span>
      </button>
      <button
        type="button"
        onClick={onOpenCase}
        className="flex items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2 text-[13.5px] font-semibold text-ink-soft transition hover:border-primary hover:text-primary-dark"
      >
        <ClipboardList size={16} aria-hidden />
        <span className="hidden sm:inline">Tu expediente</span>
        {factCount > 0 && (
          <span className="rounded-pill bg-primary px-1.5 text-[11px] font-bold leading-[18px] text-white" aria-label={`${factCount} datos`}>
            {factCount}
          </span>
        )}
      </button>
      <button
        type="button"
        onClick={onToggleTheme}
        aria-label={theme === "dark" ? "Cambiar a tema claro" : "Cambiar a tema oscuro"}
        title={theme === "dark" ? "Tema claro" : "Tema oscuro"}
        className="rounded-lg p-2 text-ink-soft hover:bg-line/60"
      >
        {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
      </button>
    </header>
  );
}
