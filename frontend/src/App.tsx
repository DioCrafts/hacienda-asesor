// Application shell: sidebar + main column (topbar + scrolling chat +
// composer). All component-level state lives here so the chat log, the
// "active case" title and the composer stay in sync without prop
// drilling through multiple layers.

import { useEffect, useMemo, useRef, useState } from "react";

import type { AnswerEnvelope, ObligationCandidate } from "./api";
import { createCase, postAgent } from "./api";
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
  // How many corpus searches the agent ran for this answer (0 on error).
  searches: number;
  errored?: boolean;
}

const INITIAL_GREETING =
  "Hola, soy HaciendaGPT. Puedo ayudarte con IRPF, IVA, autónomos, " +
  "Modelo 720 y otras consultas fiscales. Cuéntame tu caso: buscaré la " +
  "normativa aplicable y comprobaré cada afirmación contra su fuente.";

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
  // Aborts the in-flight turn's requests on "Nueva consulta" or unmount, so a
  // late response can't fire setState against stale/torn-down state.
  const abortRef = useRef<AbortController | null>(null);

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

  // Cancel any in-flight request if the component unmounts.
  useEffect(() => () => abortRef.current?.abort(), []);

  // Resolves the case id, creating the case exactly once. Concurrent callers
  // share the same in-flight promise; on failure the memo is cleared so a
  // later turn can retry rather than reusing a rejected promise forever.
  function ensureCaseId(signal: AbortSignal): Promise<string> {
    if (caseId) return Promise.resolve(caseId);
    if (!caseCreateRef.current) {
      const pending = createCase({ userId, taxPeriod, signal }).then((created) => {
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

  async function handleSubmit(text: string) {
    // Synchronous lock first: blocks reentrancy even before `busy` (state)
    // commits and disables the inputs.
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      if (turns.length === 0) {
        const short = text.length > 60 ? text.slice(0, 57) + "…" : text;
        setCaseTitle(short);
      }

      // One call per turn: the agent searches the corpus, reads/records the
      // case facts, runs the rules engine and has its answer verified. The
      // case is best-effort — without it the agent still answers (search-only).
      const activeCaseId = await ensureCaseId(controller.signal).catch((err) => {
        console.error("case creation failed; continuing without a case", err);
        return null;
      });

      let turn: ChatTurn;
      try {
        const resp = await postAgent({
          query: text,
          chatHistory,
          caseId: activeCaseId,
          signal: controller.signal,
        });
        turn = {
          id: newId(),
          userText: text,
          envelope: {
            answer: resp.answer,
            mode: resp.mode,
            citations: resp.citations,
            raw_answer: resp.raw_answer,
            warnings: resp.warnings,
          },
          obligations: resp.obligations,
          searches: resp.tool_trace.filter((t) => t.tool === "buscar_fuentes" && !t.is_error).length,
        };
      } catch (err) {
        console.error("postAgent failed", err);
        // Never fabricate content on failure: empty abstention, no sources.
        turn = {
          id: newId(),
          userText: text,
          envelope: offlineFallback(err),
          obligations: [],
          searches: 0,
          errored: true,
        };
      }

      // The turn was cancelled (new consultation / unmount) while in flight —
      // drop the (possibly fallback) result instead of rendering stale state.
      if (controller.signal.aborted) return;
      setTurns((prev) => [...prev, turn]);
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      busyRef.current = false;
      setBusy(false);
    }
  }

  function handleNewConsultation() {
    abortRef.current?.abort();
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
                  {t.envelope.warnings && t.envelope.warnings.length > 0 && (
                    <div className="mb-3 rounded-lg border border-grounding-uncited-bd bg-grounding-uncited-bg px-4 py-3">
                      <p className="mb-1 text-[12px] font-semibold text-grounding-uncited-fg">
                        Avisos de la verificación
                      </p>
                      <ul className="list-disc pl-5 text-[13px] text-ink-soft">
                        {t.envelope.warnings.map((w, i) => (
                          <li key={`${t.id}-w-${i}`}>{w}</li>
                        ))}
                      </ul>
                    </div>
                  )}
                  <SourceCardList citations={t.envelope.citations} />
                  {t.searches > 0 && (
                    <p className="mt-2 text-[12px] text-muted">
                      {t.searches === 1
                        ? "1 búsqueda en el corpus normativo."
                        : `${t.searches} búsquedas en el corpus normativo.`}
                    </p>
                  )}
                  <ObligationCardList obligations={t.obligations} />
                  {t.errored && (
                    <p className="mt-2 text-[12px] text-risk-high-fg">
                      Error de conexión con el servicio; no se ha generado ninguna respuesta a
                      partir de la normativa.
                    </p>
                  )}
                </AssistantBubble>
              </div>
            ))}
            {busy && (
              <AssistantBubble>
                <p className="text-[14px] italic text-muted">
                  Buscando en la normativa y verificando la respuesta…
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
