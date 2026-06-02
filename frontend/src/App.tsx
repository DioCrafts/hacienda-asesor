// Application shell: sidebar + main column (topbar + scrolling chat +
// composer). All component-level state lives here so the chat log, the
// "active case" title and the composer stay in sync without prop
// drilling through multiple layers.

import { useEffect, useMemo, useRef, useState } from "react";

import type { AnswerEnvelope, ObligationCandidate } from "./api";
import { postQA } from "./api";
import {
  AssistantBubble,
  Markdownish,
  UserBubble,
} from "./components/ChatBubbles";
import { Composer } from "./components/Composer";
import { ObligationCardList } from "./components/ObligationCard";
import { Sidebar } from "./components/Sidebar";
import { SourceCardList } from "./components/SourceCard";
import { TopBar } from "./components/TopBar";
import { TrustStrip } from "./components/TrustStrip";
import { MOCK_ENVELOPE, MOCK_OBLIGATION } from "./lib/mock";

// Turn = one user/assistant exchange. We persist the full envelope so
// the trust strip and source cards can be re-rendered on every layout
// recomputation (e.g. window resize) without re-querying the backend.
interface ChatTurn {
  id: string;
  userText: string;
  envelope: AnswerEnvelope;
  obligations: ObligationCandidate[];
  errored?: boolean;
}

const INITIAL_GREETING =
  "Hola, soy HaciendaGPT. Puedo ayudarte con IRPF, IVA, autónomos, " +
  "Modelo 720 y otras consultas fiscales. Cuéntame tu caso y citaré la " +
  "normativa aplicable.";

export default function App() {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [caseTitle, setCaseTitle] = useState<string>("Nueva consulta");
  const [busy, setBusy] = useState(false);

  const chatHistory = useMemo(
    () =>
      turns.flatMap((t) => [
        { role: "user" as const, content: t.userText },
        { role: "assistant" as const, content: t.envelope.answer },
      ]),
    [turns],
  );

  const bottomRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, busy]);

  async function handleSubmit(text: string) {
    setBusy(true);
    if (turns.length === 0) {
      const short = text.length > 60 ? text.slice(0, 57) + "…" : text;
      setCaseTitle(short);
    }

    let envelope: AnswerEnvelope;
    let errored = false;
    try {
      envelope = await postQA({ query: text, chatHistory });
    } catch (err) {
      // Falls back to the mock envelope so the UI keeps demoing offline
      // even when the backend is down or still being indexed.
      console.error("postQA failed; falling back to mock envelope", err);
      envelope = { ...MOCK_ENVELOPE, mode: "abstained", reason: String(err) };
      errored = true;
    }

    const turn: ChatTurn = {
      id: crypto.randomUUID(),
      userText: text,
      envelope,
      // Obligations require /cases/{id}/turn + a CaseState fetch — out
      // of scope for the first wire-up. The mocked obligation is shown
      // only on the first turn so the demo matches the screenshots.
      obligations: turns.length === 0 ? [MOCK_OBLIGATION] : [],
      errored,
    };
    setTurns((prev) => [...prev, turn]);
    setBusy(false);
  }

  function handleNewConsultation() {
    setTurns([]);
    setCaseTitle("Nueva consulta");
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <Sidebar onNewConsultation={handleNewConsultation} />
      <main className="flex h-full min-w-0 flex-1 flex-col bg-paper">
        <TopBar caseTitle={caseTitle} />
        <div className="flex-1 overflow-y-auto px-8 py-6">
          <div className="mx-auto flex max-w-3xl flex-col gap-6">
            {turns.length === 0 && (
              <AssistantBubble>
                <Markdownish text={INITIAL_GREETING} />
              </AssistantBubble>
            )}
            {turns.map((t) => (
              <div key={t.id} className="flex flex-col gap-4">
                <UserBubble text={t.userText} />
                <AssistantBubble>
                  <Markdownish text={t.envelope.answer} />
                  <TrustStrip mode={t.envelope.mode} />
                  <SourceCardList citations={t.envelope.citations} />
                  <ObligationCardList obligations={t.obligations} />
                  {t.errored && (
                    <p className="mt-2 text-[12px] text-risk-high-fg">
                      No pudimos contactar con el backend; se muestra una
                      respuesta de ejemplo.
                    </p>
                  )}
                </AssistantBubble>
              </div>
            ))}
            {busy && (
              <AssistantBubble>
                <p className="text-[14px] italic text-muted">
                  Consultando la normativa…
                </p>
              </AssistantBubble>
            )}
            <div ref={bottomRef} />
          </div>
        </div>
        <div className="border-t border-line bg-paper px-8 py-4">
          <div className="mx-auto max-w-3xl">
            <Composer onSubmit={handleSubmit} disabled={busy} />
          </div>
        </div>
      </main>
    </div>
  );
}
