// First screen of a new consultation: what this is, how it earns trust, and
// four concrete ways to start.

import { ArrowRight, FileSearch, ShieldCheck, UserRound } from "lucide-react";

import { Monogram } from "./Brand";
import { STARTER_QUESTIONS } from "../lib/mock";

const HOW_IT_WORKS = [
  { icon: FileSearch, title: "Busca en la normativa", text: "Leyes del BOE, consultas de la DGT, resoluciones del TEAC y manuales de la AEAT." },
  { icon: ShieldCheck, title: "Comprueba lo que afirma", text: "Cada dato va con su fuente y se verifica contra el texto original antes de mostrártelo." },
  { icon: UserRound, title: "Se adapta a tu caso", text: "Recuerda lo que le cuentas y te pregunta lo que le falta en lugar de suponerlo." },
] as const;

export function EmptyState({ onPick }: { onPick: (prompt: string) => void }) {
  return (
    <div className="mx-auto flex max-w-2xl animate-fade-up flex-col items-center px-1 pb-4 pt-6 text-center sm:pt-12">
      <Monogram size={56} />
      <h2 className="mt-5 font-serif text-[26px] font-bold leading-tight text-ink sm:text-[30px]">¿Qué duda fiscal tienes?</h2>
      <p className="mt-2 max-w-md text-[15px] leading-relaxed text-ink-soft">
        Cuéntame tu situación con tus palabras. Te respondo con la normativa aplicable y te digo hasta dónde puedes fiarte de
        cada respuesta.
      </p>

      <div className="mt-8 grid w-full gap-2.5 text-left sm:grid-cols-2">
        {STARTER_QUESTIONS.map((q) => (
          <button
            key={q.title}
            type="button"
            onClick={() => onPick(q.prompt)}
            className="group flex flex-col rounded-xl border border-line bg-surface p-4 text-left shadow-card transition hover:border-primary"
          >
            <span className="flex items-center justify-between text-[13px] font-bold text-primary-dark">
              {q.title}
              <ArrowRight size={15} aria-hidden className="opacity-0 transition group-hover:translate-x-0.5 group-hover:opacity-100" />
            </span>
            <span className="mt-1 text-[14px] leading-snug text-ink-soft">{q.prompt}</span>
          </button>
        ))}
      </div>

      <ul className="mt-8 grid w-full gap-4 text-left sm:grid-cols-3">
        {HOW_IT_WORKS.map(({ icon: Icon, title, text }) => (
          <li key={title} className="flex gap-3 sm:flex-col sm:gap-2">
            <Icon size={18} aria-hidden className="mt-0.5 flex-shrink-0 text-primary-dark" />
            <div>
              <p className="text-[13.5px] font-semibold text-ink">{title}</p>
              <p className="text-[12.5px] leading-snug text-muted">{text}</p>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
