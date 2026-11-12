// Slim strip above the top bar with the signed-in account and its two
// actions. Rendered only when the backend requires an account.

import { LogOut, ShieldCheck } from "lucide-react";

interface AuthAccountBarProps {
  email: string;
  onOpenPrivacy: () => void;
  onLogout: () => void;
}

export function AuthAccountBar({ email, onOpenPrivacy, onLogout }: AuthAccountBarProps) {
  return (
    <div className="flex items-center justify-end gap-1 border-b border-line bg-surface px-3 py-1 text-[12.5px] text-muted sm:px-6">
      <span className="mr-1 min-w-0 truncate" title={email}>
        {email}
      </span>
      <button
        type="button"
        onClick={onOpenPrivacy}
        className="inline-flex shrink-0 items-center gap-1.5 rounded-md px-2 py-1 font-semibold text-ink-soft transition hover:bg-line/60 hover:text-primary-dark"
      >
        <ShieldCheck size={14} aria-hidden />
        Privacidad y datos
      </button>
      <button
        type="button"
        onClick={onLogout}
        className="inline-flex shrink-0 items-center gap-1.5 rounded-md px-2 py-1 font-semibold text-ink-soft transition hover:bg-line/60 hover:text-primary-dark"
      >
        <LogOut size={14} aria-hidden />
        Cerrar sesión
      </button>
    </div>
  );
}
