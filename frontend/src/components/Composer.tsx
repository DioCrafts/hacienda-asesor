// Composer = suggested chips + rounded input pill with circular send.
//
// Suggested chips share state with the input through the parent (App)
// so clicking a chip drives the same submit pathway as typing.

import { ArrowUp, Sparkles } from "lucide-react";
import { useState } from "react";

import { SUGGESTED_QUESTIONS } from "../lib/mock";

interface ComposerProps {
  onSubmit: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
}

export function Composer({
  onSubmit,
  disabled = false,
  placeholder = "Pregúntame sobre IRPF, IVA, autónomos…",
}: ComposerProps) {
  const [value, setValue] = useState("");

  function submit(text: string) {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSubmit(trimmed);
    setValue("");
  }

  return (
    <div className="mt-4 space-y-3">
      <div className="flex flex-wrap gap-2">
        {SUGGESTED_QUESTIONS.map((q) => (
          <button
            key={q}
            type="button"
            onClick={() => submit(q)}
            disabled={disabled}
            className="group flex items-center gap-1.5 rounded-pill border border-line bg-surface px-3 py-1.5 text-[13px] font-medium text-ink-soft transition hover:border-primary hover:bg-primary-soft hover:text-primary-dark disabled:cursor-not-allowed disabled:opacity-50"
          >
            <Sparkles
              size={13}
              className="text-primary group-hover:text-primary-dark"
            />
            {q}
          </button>
        ))}
      </div>

      <form
        className="flex items-center gap-2 rounded-2xl border border-line bg-surface px-5 py-2.5 shadow-card"
        onSubmit={(e) => {
          e.preventDefault();
          submit(value);
        }}
      >
        <input
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={disabled}
          placeholder={placeholder}
          className="flex-1 bg-transparent text-[15px] text-ink placeholder:text-muted focus:outline-none disabled:opacity-60"
          aria-label={placeholder}
        />
        <button
          type="submit"
          disabled={disabled || !value.trim()}
          className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-primary text-white transition hover:bg-primary-dark disabled:cursor-not-allowed disabled:opacity-40"
          aria-label="Enviar"
        >
          <ArrowUp size={18} />
        </button>
      </form>

      <p className="text-center text-[12px] text-muted">
        HaciendaGPT puede equivocarse y se abstiene si no encuentra base
        normativa. Verifica siempre con un profesional.
      </p>
    </div>
  );
}
