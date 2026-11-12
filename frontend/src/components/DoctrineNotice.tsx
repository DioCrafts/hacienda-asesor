// Validity of the doctrine an answer relies on: shown when the question has no
// settled criterion, or when a cited source has been superseded or is disputed.

import { Scale } from "lucide-react";

interface DoctrineNoticeProps {
  conflicts: string[];
  unsettled: boolean;
}

export function DoctrineNotice({ conflicts, unsettled }: DoctrineNoticeProps) {
  if (!unsettled && conflicts.length === 0) return null;
  return (
    <div role="note" className="mt-3 rounded-xl border border-grounding-abstain-bd bg-grounding-abstain-bg px-4 py-3">
      <p className="flex items-center gap-2 text-[14px] font-bold text-ink">
        <Scale size={17} aria-hidden className="flex-shrink-0 text-grounding-abstain-fg" />
        {unsettled ? "No hay criterio firme sobre esta cuestión" : "Atención a la vigencia de las fuentes"}
      </p>
      <p className="mt-1 text-[13px] leading-snug text-ink-soft">
        {unsettled
          ? "Las fuentes discrepan o el criterio está discutido. La respuesta expone las posturas; ninguna es segura hasta que un tribunal o la Administración lo resuelva."
          : "Alguna de las fuentes citadas ha sido superada, matizada o discutida por resoluciones posteriores."}
      </p>
      {conflicts.length > 0 && (
        <ul className="mt-2 list-disc space-y-1 pl-5 text-[12.5px] leading-snug text-ink-soft">
          {conflicts.map((conflict, i) => (
            <li key={i}>{conflict}</li>
          ))}
        </ul>
      )}
      <p className="mt-2 text-[11.5px] text-muted">
        La vigencia se deduce de las citas entre resoluciones del corpus: es un indicio, no una declaración oficial.
      </p>
    </div>
  );
}
