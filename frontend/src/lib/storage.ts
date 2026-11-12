// Conversation persistence in localStorage: the "Recientes" list is the
// user's real history on this browser, and reloading never loses a chat.
// (No account system exists yet, so nothing is synced to the server beyond
// the case each conversation is attached to.)

import type { AnswerEnvelope, ClaimCheck, Fact, ObligationCandidate } from "../api";
import type { ActivityRow } from "./presentation";

export interface ChatTurn {
  id: string;
  userText: string;
  envelope: AnswerEnvelope;
  claims: ClaimCheck[];
  obligations: ObligationCandidate[];
  // What the advisor did to get here, for the collapsible "how I got this".
  activity: ActivityRow[];
  elapsedMs: number;
  errored?: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  caseId: string | null;
  turns: ChatTurn[];
  // The case as of the last turn (facts the advisor holds about the user).
  facts: Fact[];
  obligations: ObligationCandidate[];
  taxPeriod: string | null;
  updatedAt: number;
}

const KEY = "haciendagpt_conversations_v1";
const MAX_CONVERSATIONS = 40;

function isConversation(value: unknown): value is Conversation {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return typeof v.id === "string" && typeof v.title === "string" && Array.isArray(v.turns) && Array.isArray(v.facts);
}

export function loadConversations(): Conversation[] {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(isConversation)
      .map((c) => ({ ...c, obligations: Array.isArray(c.obligations) ? c.obligations : [] }))
      .sort((a, b) => b.updatedAt - a.updatedAt);
  } catch {
    return [];
  }
}

export function saveConversations(conversations: Conversation[]): void {
  // Empty drafts are not history; keep only the most recent ones that have content.
  const kept = conversations
    .filter((c) => c.turns.length > 0)
    .sort((a, b) => b.updatedAt - a.updatedAt)
    .slice(0, MAX_CONVERSATIONS);
  try {
    localStorage.setItem(KEY, JSON.stringify(kept));
  } catch {
    // Storage full or disabled: the session keeps working, it just won't persist.
  }
}

export function emptyConversation(id: string): Conversation {
  return {
    id,
    title: "Nueva consulta",
    caseId: null,
    turns: [],
    facts: [],
    obligations: [],
    taxPeriod: null,
    updatedAt: Date.now(),
  };
}
