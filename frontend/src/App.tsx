// Application shell: sidebar + main column (topbar + scrolling chat +
// composer). All component-level state lives here so the chat log, the
// "active case" title and the composer stay in sync without prop
// drilling through multiple layers.

import { useEffect, useMemo, useRef, useState } from "react";

import type { AnswerEnvelope, ObligationCandidate } from "./api";
import { createCase, postQA, postTurn } from "./api";
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
import { newId } from "./lib/id";

// Turn = one user/assistant exchange. We persist the full envelope so
// the trust strip and source cards can be re-rendered on every layout
// recomputation (e.g. window resize) without re-querying the backend.
interface ChatTurn {
  id: string;
  userText: string;
  envelope: AnswerEnvelope;
  obligations: ObligationCandidate[];
  nextQuestions: string[];
  errored?: boolean;
}

const INITIAL_GREETING =
  "Hola, soy HaciendaGPT. Puedo ayudarte con IRPF, IVA, autónomos, " +
  "Modelo 720 y otras consultas fiscales. Cuéntame tu caso y citaré la " +
  "normativa aplicable.";

// Honest, empty abstention used when the backend is unreachable. In a tax
// domain we must NEVER surface fabricated answers or citations: no body,
// no sources, just a clear "couldn't reach the service".
function offlineFallback(err: unknown): AnswerEnvelope {
  return {
    answer:
      "No pude contactar con el servicio, así que no tengo una respuesta " +
      "fundamentada en la normativa. Vuelve a intentarlo en unos instantes.",
    mode: "abstained",
    citations: [],
    reason: String(err),
    min_citations_required: 0,
  };
}

export default function App() {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [caseTitle, setCaseTitle] = useState<string>("Nueva consulta");
  const [busy, setBusy] = useState(false);
  // Decision-engine case for the current consultation. Created lazily on
  // the first turn and reused for the rest; reset by "Nueva consulta".
  const [caseId, setCaseId] = useState<string | null>(null);
  // Synchronous reentrancy lock. React state (`busy`) updates asynchronously,
  // so two events fired before the next render both read busy===false and run
  // in parallel; a ref is read/written synchronously and blocks that.
  const busyRef = useRef(false);
  // Memoized case-creation promise so concurrent first turns share ONE
  // POST /cases instead of creating duplicate (orphaned) cases.
  const caseCreateRef = useRef<Promise<string> | null>(null);

  // Stable per-browser identity. There is no auth yet, so a generated id
  // persisted in localStorage lets the backend group this user's cases.
  const [userId] = useState(() => {
    const KEY = "haciendagpt_user_id";
    try {
      const existing = localStorage.getItem(KEY);
      if (existing) return existing;
      const fresh = `web-${newId()}`;
      localStorage.setItem(KEY, fresh);
      return fresh;
    } catch {
      return `web-${newId()}`;
    }
  });

  // Provisional fiscal period the case opens with. The decision engine
  // refines it from the conversation (it asks for `periodo_fiscal`), so
  // this is only the creation default — never presented as tax advice.
  const taxPeriod = String(new Date().getFullYear());

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

  // Runs the decision flow for one turn: creates the case on first use,
  // then posts the turn and returns the engine's real obligations and
  // follow-up questions. Throws on network/HTTP errors (handled below).
  // Resolves the case id, creating the case exactly once. Concurrent callers
  // share the same in-flight promise; on failure the memo is cleared so a
  // later turn can retry rather than reusing a rejected promise forever.
  function ensureCaseId(): Promise<string> {
    if (caseId) return Promise.resolve(caseId);
    if (!caseCreateRef.current) {
      const pending = createCase({ userId, taxPeriod }).then((created) => {
        setCaseId(created.case_id);
        return created.case_id;
      });
      pending.catch(() => {
        caseCreateRef.current = null;
      });
      caseCreateRef.current = pending;
    }
    return caseCreateRef.current;
  }

  async function runDecisionTurn(
    text: string,
  ): Promise<{ obligations: ObligationCandidate[]; nextQuestions: string[] }> {
    const id = await ensureCaseId();
    const resp = await postTurn({ caseId: id, userInput: text, chatHistory });
    return { obligations: resp.obligations, nextQuestions: resp.next_questions };
  }

  async function handleSubmit(text: string) {
    // Synchronous lock first: blocks reentrancy even before `busy` (state)
    // commits and disables the inputs.
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      if (turns.length === 0) {
        const short = text.length > 60 ? text.slice(0, 57) + "…" : text;
        setCaseTitle(short);
      }

      // The grounded answer (/qa) and the decision flow (/cases + /turn) are
      // independent backend calls. We run them in parallel and degrade each
      // on its own — neither failure path may fabricate content.
      const qaPromise = postQA({ query: text, chatHistory }).then(
        (env) => ({ env, errored: false }),
        (err) => {
          console.error("postQA failed", err);
          return { env: offlineFallback(err), errored: true };
        },
      );

      const decisionPromise = runDecisionTurn(text).catch((err) => {
        console.error("decision turn failed", err);
        // Show no obligations rather than fake ones; the answer still renders.
        return {
          obligations: [] as ObligationCandidate[],
          nextQuestions: [] as string[],
        };
      });

      const [qa, decision] = await Promise.all([qaPromise, decisionPromise]);

      const turn: ChatTurn = {
        id: newId(),
        userText: text,
        envelope: qa.env,
        obligations: decision.obligations,
        nextQuestions: decision.nextQuestions,
        errored: qa.errored,
      };
      setTurns((prev) => [...prev, turn]);
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  }

  function handleNewConsultation() {
    setTurns([]);
    setCaseTitle("Nueva consulta");
    setCaseId(null);
    caseCreateRef.current = null;
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
                  <TrustStrip mode={t.envelope.mode} reason={t.envelope.reason} />
                  <SourceCardList citations={t.envelope.citations} />
                  <ObligationCardList obligations={t.obligations} />
                  {t.nextQuestions.length > 0 && (
                    <div className="mt-4">
                      <p className="mb-2 text-[12px] font-medium text-muted">
                        Para afinar el análisis, puedo preguntarte:
                      </p>
                      <div className="flex flex-wrap gap-2">
                        {t.nextQuestions.map((q, i) => (
                          <button
                            key={`${t.id}-q-${i}`}
                            type="button"
                            onClick={() => handleSubmit(q)}
                            disabled={busy}
                            className="rounded-pill border border-line bg-surface px-3 py-1.5 text-[13px] font-medium text-ink-soft transition hover:border-primary hover:bg-primary-soft hover:text-primary-dark disabled:cursor-not-allowed disabled:opacity-50"
                          >
                            {q}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                  {t.errored && (
                    <p className="mt-2 text-[12px] text-risk-high-fg">
                      {t.obligations.length > 0
                        ? "No se pudo generar la respuesta con citas (servicio de consulta no disponible); las obligaciones mostradas provienen del análisis del caso."
                        : "Error de conexión con el servicio; no se ha generado ninguna respuesta a partir de la normativa."}
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
