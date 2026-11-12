// Application shell: sidebar (history) + main column (top bar, chat, composer)
// + the case-file panel. All cross-component state lives here.
//
// When the backend requires an account (AUTH_MODE=required) the shell is
// wrapped by a gate that shows the login screen first; with accounts off the
// gate renders the shell directly and nothing else changes.

import { Check, Copy, RotateCcw, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ProgressEvent } from "./api";
import type { AdvisorNotification, AuthConfig, SessionResponse, SessionUser } from "./api";
import {
  ApiError,
  createCase,
  deleteCaseFact,
  getAuthConfig,
  getMe,
  getNotifications,
  logout,
  markNotificationRead,
  onSessionExpired,
  runProactive,
  streamAgent,
  updateCaseFact,
} from "./api";
import { ActivitySummary, LiveActivity } from "./components/ActivityPanel";
import { AuthAccountBar } from "./components/AuthAccountBar";
import { AuthScreen } from "./components/AuthScreen";
import { CasePanel } from "./components/CasePanel";
import { DoctrineNotice } from "./components/DoctrineNotice";
import { AssistantBubble, UserBubble } from "./components/ChatBubbles";
import { Composer } from "./components/Composer";
import { EmptyState } from "./components/EmptyState";
import { NotificationsPanel } from "./components/NotificationsPanel";
import { ObligationCardList } from "./components/ObligationCard";
import { PrivacyPanel } from "./components/PrivacyPanel";
import { RichText } from "./components/RichText";
import { AutonomosPanel } from "./components/AutonomosPanel";
import { SimulatorPanel } from "./components/SimulatorPanel";
import { Sidebar } from "./components/Sidebar";
import { SourceCardList } from "./components/SourceCard";
import { TopBar } from "./components/TopBar";
import { TrustStrip } from "./components/TrustStrip";
import { VerificationPanel } from "./components/VerificationPanel";
import { newId } from "./lib/id";
import { activityRows, sourceAnchorId, verificationSummary } from "./lib/presentation";
import { cacheAuthMode, cachedAuthMode, getSessionToken, historyOwner, setHistoryOwner, setSessionToken } from "./lib/session";
import { type ChatTurn, type Conversation, emptyConversation, loadConversations, saveConversations } from "./lib/storage";
import { useTheme } from "./lib/theme";

// A turn that is still running: the question plus the agent's live progress.
interface PendingTurn {
  text: string;
  events: ProgressEvent[];
  startedAt: number;
  // The answer text streamed so far for the current model step (unverified).
  draft: string;
}

// Warnings the backend derives from individual claims; those are shown in the
// claim-by-claim panel, so they are not repeated as loose banners.
const CLAIM_WARNING_PREFIXES = ["Sin respaldo en la fuente citada", "Solo respaldada en parte"];

function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  if (err instanceof TypeError) return "No hay conexión con el servicio. Comprueba que está en marcha y tu conexión a internet.";
  return "Ha ocurrido un error inesperado al preparar la respuesta.";
}

function obligationKey(turn: ChatTurn | undefined): string {
  return (turn?.obligations ?? []).map((o) => o.obligation_id).sort().join("|");
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard
          ?.writeText(text)
          .then(() => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1800);
          })
          .catch(() => undefined);
      }}
      className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-[12.5px] font-medium text-muted transition hover:bg-line/60 hover:text-ink-soft"
    >
      {copied ? <Check size={14} aria-hidden /> : <Copy size={14} aria-hidden />}
      {copied ? "Copiada" : "Copiar respuesta"}
    </button>
  );
}

// The signed-in account, when the backend has accounts.
interface AccountControls {
  email: string;
  onOpenPrivacy: () => void;
  onLogout: () => void;
}

function Advisor({ account }: { account?: AccountControls }) {
  const [theme, toggleTheme] = useTheme();
  const [conversations, setConversations] = useState<Conversation[]>(loadConversations);
  const [activeId, setActiveId] = useState<string>(() => newId());
  const [pending, setPending] = useState<PendingTurn | null>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [caseOpen, setCaseOpen] = useState(false);
  const [simulatorOpen, setSimulatorOpen] = useState(false);
  const [autonomosOpen, setAutonomosOpen] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [notifications, setNotifications] = useState<{ caseId: string; items: AdvisorNotification[] } | null>(null);
  // Key of the last request that finished; while it differs from the current one we are loading.
  const [notificationsSettled, setNotificationsSettled] = useState<string | null>(null);
  const [notificationsError, setNotificationsError] = useState<string | null>(null);
  const [highlightedId, setHighlightedId] = useState<string | null>(null);
  const [removingFact, setRemovingFact] = useState<string | null>(null);
  const [caseError, setCaseError] = useState<string | null>(null);
  // Remounting the composer is how a stopped question is put back in the box.
  const [composerSeed, setComposerSeed] = useState({ id: 0, text: "" });

  // Synchronous reentrancy lock: `pending` (state) commits asynchronously, so
  // two submits fired before the next render would both see it as null.
  const busyRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const highlightTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const lastTurnRef = useRef<HTMLElement | null>(null);

  const active = useMemo(
    () => conversations.find((c) => c.id === activeId) ?? emptyConversation(activeId),
    [conversations, activeId],
  );
  const busy = pending !== null;

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

  useEffect(() => saveConversations(conversations), [conversations]);

  // While the advisor works, follow its progress at the bottom. When the answer
  // lands, bring its START into view instead: a long answer must be read from
  // the top, not from its last source card.
  // (The draft grows token by token; re-scroll only every ~120 characters.)
  const draftBucket = Math.floor((pending?.draft.length ?? 0) / 120);
  useEffect(() => {
    if (busy) bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [busy, pending?.events.length, draftBucket]);
  useEffect(() => {
    lastTurnRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [active.turns.length, activeId]);

  // Cancel the in-flight request and pending timers if the app unmounts.
  useEffect(
    () => () => {
      abortRef.current?.abort();
      if (highlightTimer.current) clearTimeout(highlightTimer.current);
    },
    [],
  );

  /** Insert or update a conversation by id. */
  const upsert = useCallback((id: string, change: (current: Conversation) => Conversation) => {
    setConversations((prev) => {
      const current = prev.find((c) => c.id === id) ?? emptyConversation(id);
      const next = change(current);
      return [next, ...prev.filter((c) => c.id !== id)];
    });
  }, []);

  const handleCite = useCallback((turnId: string, fragmentId: string) => {
    const anchor = sourceAnchorId(turnId, fragmentId);
    document.getElementById(anchor)?.scrollIntoView({ behavior: "smooth", block: "center" });
    setHighlightedId(anchor);
    if (highlightTimer.current) clearTimeout(highlightTimer.current);
    highlightTimer.current = setTimeout(() => setHighlightedId(null), 2000);
  }, []);

  async function handleSubmit(text: string, replaceTurnId?: string) {
    if (busyRef.current) return;
    busyRef.current = true;
    const conversation = active;
    const controller = new AbortController();
    abortRef.current = controller;
    const startedAt = Date.now();
    const events: ProgressEvent[] = [];
    setPending({ text, events: [], startedAt, draft: "" });
    if (replaceTurnId) {
      upsert(conversation.id, (c) => ({ ...c, turns: c.turns.filter((t) => t.id !== replaceTurnId) }));
    }

    const priorTurns = conversation.turns.filter((t) => t.id !== replaceTurnId && !t.errored);
    const chatHistory = priorTurns.flatMap((t) => [
      { role: "user" as const, content: t.userText },
      { role: "assistant" as const, content: t.envelope.answer },
    ]);

    try {
      // The case lets the advisor remember facts across turns. It is
      // best-effort: without it the advisor still answers (search-only).
      let caseId = conversation.caseId;
      if (!caseId) {
        // Provisional fiscal period; the advisor refines it from the conversation.
        const taxPeriod = String(new Date().getFullYear());
        caseId = await createCase({ userId, taxPeriod, signal: controller.signal }).then(
          (created) => created.case_id,
          (err) => {
            console.error("case creation failed; continuing without a case", err);
            return null;
          },
        );
        if (caseId) {
          const createdId = caseId;
          upsert(conversation.id, (c) => ({ ...c, caseId: createdId }));
        }
      }

      let turn: ChatTurn;
      try {
        const resp = await streamAgent({ query: text, chatHistory, caseId, signal: controller.signal }, (event) => {
          if (event.type === "text_delta") {
            // Tokens only grow the draft; they are not timeline entries.
            setPending((p) => (p ? { ...p, draft: p.draft + event.text } : p));
            return;
          }
          events.push(event);
          // A new model step, or a tool call, means whatever text came before
          // was not the answer (a preamble, or a draft now being rewritten).
          const resetsDraft = event.type === "thinking" || event.type === "tool_call";
          setPending((p) => (p ? { ...p, events: [...events], draft: resetsDraft ? "" : p.draft } : p));
        });
        turn = {
          id: newId(),
          userText: text,
          envelope: {
            answer: resp.answer,
            mode: resp.mode,
            citations: resp.citations,
            warnings: resp.warnings,
            conflicts: resp.conflicts ?? [],
            sinCriterioFirme: resp.sin_criterio_firme ?? false,
          },
          claims: resp.claims,
          obligations: resp.obligations,
          activity: activityRows(events),
          elapsedMs: Date.now() - startedAt,
        };
        upsert(conversation.id, (c) => ({
          ...c,
          facts: resp.facts,
          obligations: resp.obligations,
          taxPeriod: resp.tax_period ?? c.taxPeriod,
        }));
      } catch (err) {
        if (controller.signal.aborted) {
          // Stopped by the user: no turn is recorded and the question goes back
          // into the composer so it is not lost.
          setComposerSeed((seed) => ({ id: seed.id + 1, text }));
          return;
        }
        console.error("agent turn failed", err);
        // Never fabricate content on failure: no answer, no sources.
        turn = {
          id: newId(),
          userText: text,
          envelope: { answer: errorMessage(err), mode: "abstained", citations: [] },
          claims: [],
          obligations: [],
          activity: activityRows(events),
          elapsedMs: Date.now() - startedAt,
          errored: true,
        };
      }

      upsert(conversation.id, (c) => ({
        ...c,
        title: c.turns.length === 0 ? (text.length > 60 ? `${text.slice(0, 57)}…` : text) : c.title,
        turns: [...c.turns, turn],
        updatedAt: Date.now(),
      }));
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      busyRef.current = false;
      setPending(null);
    }
  }

  function stopTurn() {
    abortRef.current?.abort();
  }

  function switchTo(id: string) {
    stopTurn();
    setActiveId(id);
    setMenuOpen(false);
    setCaseError(null);
    setComposerSeed((seed) => ({ id: seed.id + 1, text: "" }));
  }

  function handleDelete(id: string) {
    const target = conversations.find((c) => c.id === id);
    if (!target || !window.confirm(`¿Eliminar la consulta «${target.title}»? No se puede deshacer.`)) return;
    if (id === activeId) switchTo(newId());
    setConversations((prev) => prev.filter((c) => c.id !== id));
  }

  /** Apply a user edit to the case file: a new value, or `null` to delete the fact. */
  async function handleChangeFact(factName: string, value: string | number | boolean | null): Promise<boolean> {
    if (!active.caseId || removingFact) return false;
    setRemovingFact(factName);
    setCaseError(null);
    try {
      const updated =
        value === null
          ? await deleteCaseFact({ caseId: active.caseId, factName })
          : await updateCaseFact({ caseId: active.caseId, factName, value });
      upsert(active.id, (c) => ({
        ...c,
        facts: updated.facts,
        obligations: updated.obligation_candidates,
        taxPeriod: updated.tax_period,
      }));
      return true;
    } catch (err) {
      setCaseError(`No se pudo guardar el cambio. ${errorMessage(err)}`);
      return false;
    } finally {
      setRemovingFact(null);
    }
  }

  // The advisor speaks first: whenever the case or what we know about the user
  // changes, re-run the deterministic jobs (deadlines, year-end plan) and load
  // the notifications. No model call is involved.
  const factsKey = active.facts.map((f) => `${f.name}=${String(f.value)}`).join("&");
  const [notificationsTick, setNotificationsTick] = useState(0);
  const activeCaseId = active.caseId;
  const notificationsKey = `${activeCaseId ?? ""}|${factsKey}|${notificationsTick}`;
  const notificationsLoading = activeCaseId !== null && notificationsSettled !== notificationsKey;
  useEffect(() => {
    if (!activeCaseId) return;
    const controller = new AbortController();
    runProactive({ caseId: activeCaseId, signal: controller.signal })
      .catch(() => getNotifications({ caseId: activeCaseId, signal: controller.signal }))
      .then(
        (resp) => {
          setNotifications({ caseId: activeCaseId, items: resp.notifications });
          setNotificationsError(null);
        },
        (err: unknown) => {
          if (!controller.signal.aborted) setNotificationsError(`No se pudieron cargar los avisos. ${errorMessage(err)}`);
        },
      )
      .finally(() => {
        if (!controller.signal.aborted) setNotificationsSettled(notificationsKey);
      });
    return () => controller.abort();
  }, [activeCaseId, notificationsKey]);

  const currentNotifications = notifications && notifications.caseId === activeCaseId ? notifications.items : [];
  const unreadNotifications = currentNotifications.filter((n) => !n.read_at).length;

  function handleMarkRead(notificationId: string) {
    if (!activeCaseId) return;
    markNotificationRead({ caseId: activeCaseId, notificationId }).then(
      (resp) => setNotifications({ caseId: activeCaseId, items: resp.notifications }),
      (err: unknown) => setNotificationsError(`No se pudo marcar como leído. ${errorMessage(err)}`),
    );
  }

  const visibleFactCount = active.facts.filter((f) => f.name !== "periodo_fiscal" && f.name !== "menciona_ingresos").length;

  return (
    <div className="flex h-[100dvh] w-screen overflow-hidden">
      <Sidebar
        conversations={conversations.filter((c) => c.turns.length > 0)}
        activeId={activeId}
        onSelect={switchTo}
        onDelete={handleDelete}
        onNewConsultation={() => switchTo(newId())}
        open={menuOpen}
        onClose={() => setMenuOpen(false)}
      />
      <main className="flex h-full min-w-0 flex-1 flex-col bg-paper">
        {account && <AuthAccountBar email={account.email} onOpenPrivacy={account.onOpenPrivacy} onLogout={account.onLogout} />}
        <TopBar
          title={active.title}
          factCount={visibleFactCount}
          onOpenCase={() => setCaseOpen(true)}
          onOpenSimulator={() => setSimulatorOpen(true)}
          onOpenAutonomos={() => setAutonomosOpen(true)}
          onOpenNotifications={() => setNotificationsOpen(true)}
          unreadNotifications={unreadNotifications}
          onOpenMenu={() => setMenuOpen(true)}
          theme={theme}
          onToggleTheme={toggleTheme}
        />
        <div className="flex-1 overflow-y-auto px-3 py-5 sm:px-8 sm:py-6">
          <div className="mx-auto flex max-w-3xl flex-col gap-7">
            {active.turns.length === 0 && !pending && <EmptyState onPick={(prompt) => void handleSubmit(prompt)} />}

            {active.turns.map((t, index) => {
              const claimsSummary = verificationSummary(t.claims);
              const otherWarnings = (t.envelope.warnings ?? []).filter(
                (w) => t.claims.length === 0 || !CLAIM_WARNING_PREFIXES.some((prefix) => w.startsWith(prefix)),
              );
              // Obligations belong to the case, not the turn: show them only
              // when this turn changed them, not under every answer.
              const obligationsChanged = obligationKey(t) !== obligationKey(active.turns[index - 1]);
              return (
                <section
                  key={t.id}
                  ref={index === active.turns.length - 1 ? lastTurnRef : undefined}
                  className="flex scroll-mt-4 flex-col gap-4"
                  aria-label={`Consulta: ${t.userText}`}
                >
                  <UserBubble text={t.userText} />
                  <AssistantBubble>
                    {t.errored ? (
                      <div role="alert" className="rounded-xl border border-risk-high-fg/30 bg-risk-high-bg px-4 py-3.5">
                        <p className="flex items-center gap-2 text-[14.5px] font-bold text-risk-high-fg">
                          <TriangleAlert size={17} aria-hidden />
                          No he podido responder
                        </p>
                        <p className="mt-1 text-[13.5px] leading-snug text-ink-soft">{t.envelope.answer}</p>
                        <p className="mt-1 text-[12.5px] text-muted">No se ha generado ninguna respuesta a partir de la normativa.</p>
                        <button
                          type="button"
                          onClick={() => void handleSubmit(t.userText, t.id)}
                          disabled={busy}
                          className="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-surface px-3 py-1.5 text-[13px] font-semibold text-ink shadow-card transition hover:bg-line/60 disabled:cursor-not-allowed disabled:opacity-50"
                        >
                          <RotateCcw size={14} aria-hidden />
                          Reintentar
                        </button>
                      </div>
                    ) : (
                      <>
                        <RichText
                          text={t.envelope.answer}
                          citations={t.envelope.citations}
                          onCite={(fragmentId) => handleCite(t.id, fragmentId)}
                        />
                        <TrustStrip mode={t.envelope.mode} detail={claimsSummary} />
                        <DoctrineNotice conflicts={t.envelope.conflicts ?? []} unsettled={t.envelope.sinCriterioFirme ?? false} />
                        <VerificationPanel
                          claims={t.claims}
                          otherWarnings={otherWarnings}
                          onCite={(fragmentId) => handleCite(t.id, fragmentId)}
                        />
                        <SourceCardList citations={t.envelope.citations} scope={t.id} highlightedId={highlightedId} />
                        {obligationsChanged && <ObligationCardList obligations={t.obligations} />}
                        <div className="mt-3 flex flex-wrap items-start justify-between gap-x-4">
                          <ActivitySummary rows={t.activity} elapsedMs={t.elapsedMs} />
                          <div className="mt-2">
                            <CopyButton text={t.envelope.answer} />
                          </div>
                        </div>
                      </>
                    )}
                  </AssistantBubble>
                </section>
              );
            })}

            {pending && (
              <section className="flex flex-col gap-4">
                <UserBubble text={pending.text} />
                <AssistantBubble>
                  <LiveActivity events={pending.events} startedAt={pending.startedAt} draft={pending.draft} />
                </AssistantBubble>
              </section>
            )}
            <div ref={bottomRef} />
          </div>
        </div>
        <div className="border-t border-line bg-paper px-3 pb-3 pt-3 sm:px-8">
          <div className="mx-auto max-w-3xl">
            <Composer
              key={`${activeId}-${composerSeed.id}`}
              initialValue={composerSeed.text}
              onSubmit={(text) => void handleSubmit(text)}
              onStop={stopTurn}
              busy={busy}
            />
          </div>
        </div>
      </main>
      <CasePanel
        open={caseOpen}
        onClose={() => setCaseOpen(false)}
        facts={active.facts}
        taxPeriod={active.taxPeriod}
        obligations={active.obligations}
        caseId={active.caseId}
        onChangeFact={handleChangeFact}
        saving={removingFact}
        error={caseError}
        disabled={busy}
      />
      <NotificationsPanel
        open={notificationsOpen}
        onClose={() => setNotificationsOpen(false)}
        notifications={currentNotifications}
        hasCase={activeCaseId !== null}
        loading={notificationsLoading}
        error={notificationsError}
        onMarkRead={handleMarkRead}
        onRefresh={() => setNotificationsTick((n) => n + 1)}
        onAsk={(question) => {
          setNotificationsOpen(false);
          void handleSubmit(question);
        }}
      />
      {/* Keyed by conversation: figures typed for one consultation never leak into another. */}
      <SimulatorPanel
        key={activeId}
        open={simulatorOpen}
        onClose={() => setSimulatorOpen(false)}
        caseId={active.caseId}
        facts={active.facts}
        taxPeriod={active.taxPeriod}
        onSaveFact={handleChangeFact}
        busy={busy}
      />
      <AutonomosPanel
        key={`autonomos-${activeId}`}
        open={autonomosOpen}
        onClose={() => setAutonomosOpen(false)}
        taxPeriod={active.taxPeriod}
        onUse={(net) => {
          // The net income of the activity becomes a fact of the case and feeds the simulator.
          void handleChangeFact("rendimiento_actividad_neto", net);
          setAutonomosOpen(false);
          setSimulatorOpen(true);
        }}
      />
    </div>
  );
}

function Splash({ message }: { message?: string }) {
  return (
    <div role="status" className="flex h-[100dvh] w-screen items-center justify-center bg-paper px-6 text-center text-[13.5px] text-muted">
      {message}
    </div>
  );
}

/**
 * Make the conversation history in this browser safe to show to `userId`.
 * Another account's history is discarded. History from before accounts existed
 * is kept, detached from its cases: those have no owner and cannot be reached.
 */
function adoptLocalHistory(userId: string): void {
  const owner = historyOwner();
  if (owner === userId) return;
  if (owner === null) {
    saveConversations(loadConversations().map((c) => ({ ...c, caseId: null, facts: [], obligations: [], taxPeriod: null })));
  } else {
    saveConversations([]);
  }
  setHistoryOwner(userId);
}

const CONFIG_RETRY_MS = 5_000;

export default function App() {
  // Applies the stored theme to the document, also on the screens shown before the shell.
  useTheme();
  const [lastKnownMode] = useState(cachedAuthMode);
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [configFailed, setConfigFailed] = useState(false);
  const [configAttempt, setConfigAttempt] = useState(0);
  const [user, setUser] = useState<SessionUser | null>(null);
  // A stored token must be checked with the server before it is trusted.
  const [sessionChecked, setSessionChecked] = useState(() => getSessionToken() === null);
  const [notice, setNotice] = useState<string | null>(null);
  const [privacyOpen, setPrivacyOpen] = useState(false);
  // Set by an explicit sign-out or an account deletion: the history in this browser must go.
  const wipeHistoryRef = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    let retry: ReturnType<typeof setTimeout> | undefined;
    getAuthConfig(controller.signal).then(
      (loaded) => {
        cacheAuthMode(loaded.auth_mode);
        setConfig(loaded);
        setConfigFailed(false);
      },
      () => {
        if (controller.signal.aborted) return;
        // Backend unreachable: keep asking, so the right screen appears as soon as it is back.
        setConfigFailed(true);
        retry = setTimeout(() => setConfigAttempt((n) => n + 1), CONFIG_RETRY_MS);
      },
    );
    return () => {
      controller.abort();
      clearTimeout(retry);
    };
  }, [configAttempt]);

  const required = config?.auth_mode === "required";

  useEffect(() => {
    onSessionExpired(() => {
      setSessionToken(null);
      setUser(null);
      setPrivacyOpen(false);
      setSessionChecked(true);
      setNotice("Tu sesión ha caducado. Vuelve a iniciar sesión.");
      // If we believed accounts were off, the backend has changed: ask again.
      if (!required) setConfigAttempt((n) => n + 1);
    });
    return () => onSessionExpired(null);
  }, [required]);

  useEffect(() => {
    if (!required || sessionChecked) return;
    const controller = new AbortController();
    getMe(controller.signal).then(
      (me) => {
        adoptLocalHistory(me.id);
        setUser(me);
        setSessionChecked(true);
      },
      (err: unknown) => {
        if (controller.signal.aborted) return;
        // A rejected token was already dropped by the session-expired handler.
        if (!(err instanceof ApiError && err.status === 401)) {
          setNotice("No se pudo comprobar tu sesión. Inicia sesión de nuevo.");
        }
        setSessionChecked(true);
      },
    );
    return () => controller.abort();
  }, [required, sessionChecked]);

  // Runs once the shell is unmounted, so nothing it still held can be written back.
  useEffect(() => {
    if (user !== null || !wipeHistoryRef.current) return;
    wipeHistoryRef.current = false;
    saveConversations([]);
    setHistoryOwner(null);
  }, [user]);

  function handleSignedIn(session: SessionResponse) {
    setSessionToken(session.token);
    adoptLocalHistory(session.user.id);
    setNotice(null);
    setUser(session.user);
  }

  /** Forget the session on this browser, and the conversations with it. */
  function signOutLocally(message: string | null) {
    setSessionToken(null);
    wipeHistoryRef.current = true;
    setPrivacyOpen(false);
    setUser(null);
    setNotice(message);
  }

  function handleLogout() {
    const confirmed = window.confirm(
      "Al cerrar sesión se borra de este navegador el historial de conversaciones. Tu expediente sigue guardado en tu cuenta. ¿Cerrar sesión?",
    );
    if (!confirmed) return;
    // Best effort: the session is dropped here even if the server cannot be told.
    logout()
      .catch(() => undefined)
      .finally(() => signOutLocally(null));
  }

  if (config === null) {
    // Until the backend answers, trust what it said last time; a first visit
    // (or an unreachable backend that never had accounts) gets the open app.
    if (lastKnownMode === "required") {
      return <Splash message={configFailed ? "No hay conexión con el servicio. Reintentando…" : undefined} />;
    }
    if (lastKnownMode === "off" || configFailed) return <Advisor key="open" />;
    return <Splash />;
  }
  if (!required) return <Advisor key="open" />;
  if (!sessionChecked) return <Splash />;
  if (!user) return <AuthScreen config={config} notice={notice} onSignedIn={handleSignedIn} />;
  return (
    <>
      <Advisor key={user.id} account={{ email: user.email, onOpenPrivacy: () => setPrivacyOpen(true), onLogout: handleLogout }} />
      {/* Mounted only while open, so the typed password never outlives the panel. */}
      {privacyOpen && (
        <PrivacyPanel
          open
          onClose={() => setPrivacyOpen(false)}
          email={user.email}
          noticeUrl={config.privacy_notice_url}
          onAccountDeleted={() => signOutLocally("Tu cuenta y tus datos se han eliminado.")}
        />
      )}
    </>
  );
}
