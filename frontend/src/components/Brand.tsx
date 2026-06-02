// Brand mark: the "H" monogram on a soft circular plate, plus the
// HaciendaGPT serif logotype. Rendered inline as SVG so it stays crisp
// at any size and never costs a network round-trip.

interface MonogramProps {
  size?: number;
  className?: string;
}

export function Monogram({ size = 40, className = "" }: MonogramProps) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      role="img"
      aria-label="HaciendaGPT"
      className={className}
    >
      <rect width="64" height="64" rx="14" fill="#0f4f4a" />
      <text
        x="32"
        y="44"
        textAnchor="middle"
        fontFamily="Georgia, 'Times New Roman', serif"
        fontWeight="700"
        fontSize="36"
        fill="#e4efed"
      >
        H
      </text>
    </svg>
  );
}

interface BrandHeaderProps {
  title?: string;
  subtitle?: string;
}

export function BrandHeader({
  title = "HaciendaGPT",
  subtitle = "ASESOR FISCAL",
}: BrandHeaderProps) {
  return (
    <div className="flex items-center gap-3 px-1 py-2">
      <Monogram size={48} />
      <div className="flex flex-col leading-tight">
        <span className="font-serif text-[22px] font-bold text-ink tracking-tight">
          {title}
        </span>
        <span className="mt-1 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted">
          {subtitle}
        </span>
      </div>
    </div>
  );
}
