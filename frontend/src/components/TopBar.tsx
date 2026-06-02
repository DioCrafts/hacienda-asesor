// Top bar of the main column: the active case title centred-left and a
// user-initials avatar pinned right. Identity-tinted (navy) so it reads
// as the user's own marker, not another HaciendaGPT element.

interface TopBarProps {
  caseTitle: string;
  userInitials?: string;
}

export function TopBar({ caseTitle, userInitials = "JD" }: TopBarProps) {
  return (
    <header className="flex items-center justify-between border-b border-line bg-paper px-8 py-4">
      <h1 className="font-serif text-[20px] font-bold text-ink truncate">
        {caseTitle}
      </h1>
      <div
        className="flex h-9 w-9 items-center justify-center rounded-full bg-navy text-[12px] font-bold uppercase tracking-wide text-white"
        aria-label="Tu perfil"
        title="Tu perfil"
      >
        {userInitials}
      </div>
    </header>
  );
}
