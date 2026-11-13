// Composer: an auto-growing text box. Enter sends, Shift+Enter adds a line;
// while a turn is running the send button becomes a stop button.

import { ArrowUp, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";

interface ComposerProps {
  onSubmit: (text: string) => void;
  onStop: () => void;
  busy: boolean;
  // Text to start with (e.g. the question restored after the user pressed stop).
  // The parent remounts the composer (via `key`) to apply a new one.
  initialValue?: string;
}

const MAX_HEIGHT_PX = 180;

export function Composer({ onSubmit, onStop, busy, initialValue = "" }: ComposerProps) {
  const [value, setValue] = useState(initialValue);
  const ref = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT_PX)}px`;
  }, [value]);

  useEffect(() => {
    if (!busy) ref.current?.focus();
  }, [busy]);

  function submit() {
    const trimmed = value.trim();
    if (!trimmed || busy) return;
    onSubmit(trimmed);
    setValue("");
  }

  return (
    <div>
      <form
        className="flex items-end gap-2 rounded-2xl border border-line bg-surface py-2 pl-4 pr-2 shadow-card transition focus-within:border-primary"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <textarea
          ref={ref}
          rows={1}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder="Escribe tu consulta fiscal…"
          aria-label="Escribe tu consulta"
          className="max-h-[180px] flex-1 resize-none self-center bg-transparent py-1.5 text-[16px] leading-snug sm:text-[15px] text-ink placeholder:text-muted focus:outline-none"
        />
        {busy ? (
          <button
            type="button"
            onClick={onStop}
            aria-label="Detener la respuesta"
            title="Detener"
            className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-ink text-paper transition hover:opacity-80"
          >
            <Square size={13} fill="currentColor" />
          </button>
        ) : (
          <button
            type="submit"
            disabled={!value.trim()}
            aria-label="Enviar"
            className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-full bg-primary text-white transition hover:bg-primary-hover disabled:cursor-not-allowed disabled:opacity-40"
          >
            <ArrowUp size={18} />
          </button>
        )}
      </form>
      <p className="mt-2 text-center text-[11.5px] text-muted">
        <span className="hidden sm:inline">Intro para enviar · Mayús + Intro para salto de línea · </span>
        Puede equivocarse: contrasta lo importante con un profesional.
      </p>
    </div>
  );
}
