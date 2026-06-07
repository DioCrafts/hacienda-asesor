// Client + types for the HaciendaGPT FastAPI backend.
//
// These shapes mirror the Pydantic models in
//   hacienda_gpt/llm/grounding.py        -> AnswerEnvelope, Citation
//   hacienda_gpt/decision/schemas.py     -> ObligationCandidate, RiskLevel
//   hacienda_gpt/api/api.py              -> QARequest, TurnResponse
// so if the backend grows a new field, only this file changes.

export type AnswerMode = "cited" | "uncited" | "abstained";
export type RiskLevel = "low" | "medium" | "high" | "critical";

export interface Citation {
  title: string;
  locator: string;
  document_type?: string | null;
  section?: string | null;
  snippet?: string | null;
}

export interface AnswerEnvelope {
  answer: string;
  mode: AnswerMode;
  citations: Citation[];
  raw_answer?: string | null;
  reason?: string | null;
  min_citations_required: number;
}

export interface ObligationCandidate {
  obligation_id: string;
  title: string;
  description: string;
  risk_level: RiskLevel;
  confidence: number;
  blocking_missing_facts: string[];
  trigger_facts: string[];
  evidence_refs: Array<{ title?: string; locator?: string }>;
}

// Decision-engine shapes. These mirror hacienda_gpt/decision/schemas.py
// (Fact, MissingFact, CaseState) and the /cases/{id}/turn response in
// hacienda_gpt/api/api.py (TurnResponse). We only declare the fields the
// web UI reads; the backend may send more (extra keys are ignored).
export type CaseStatus = "open" | "in_review" | "closed";

export interface Fact {
  fact_id: string;
  name: string;
  value: string | number | boolean | Record<string, unknown> | unknown[];
  value_type: string;
  source: string;
  confidence: number;
  updated_at: string;
}

export interface MissingFact {
  fact_name: string;
  reason: string;
  priority: RiskLevel;
}

export interface CaseState {
  case_id: string;
  user_id: string;
  status: CaseStatus;
  jurisdiction: string;
  tax_period: string;
  facts: Fact[];
  missing_facts: MissingFact[];
  obligation_candidates: ObligationCandidate[];
}

export interface TurnResponse {
  case_id: string;
  facts: Fact[];
  missing_facts: MissingFact[];
  candidate_obligation_ids: string[];
  obligations: ObligationCandidate[];
  next_questions: string[];
  degraded: boolean;
  degraded_facts: string[];
}

// The FastAPI default is http://127.0.0.1:8000; Vite proxies /api/* to
// avoid CORS in dev. In production both should be served under the same
// origin so this remains an empty prefix.
const API_BASE = (import.meta.env.VITE_API_BASE as string) || "/api";

interface QAOptions {
  query: string;
  chatHistory?: Array<{ role: "user" | "assistant"; content: string }>;
}

export async function postQA({ query, chatHistory = [] }: QAOptions): Promise<AnswerEnvelope> {
  const resp = await fetch(`${API_BASE}/qa`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, chat_history: chatHistory }),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`/qa ${resp.status}: ${text || resp.statusText}`);
  }
  return resp.json();
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${path} ${resp.status}: ${text || resp.statusText}`);
  }
  return resp.json() as Promise<T>;
}

export function createCase(opts: {
  userId: string;
  taxPeriod: string;
  jurisdiction?: string;
}): Promise<CaseState> {
  return postJSON<CaseState>("/cases", {
    user_id: opts.userId,
    tax_period: opts.taxPeriod,
    jurisdiction: opts.jurisdiction ?? "ES",
  });
}

export function postTurn(opts: {
  caseId: string;
  userInput: string;
  chatHistory?: Array<{ role: "user" | "assistant"; content: string }>;
}): Promise<TurnResponse> {
  return postJSON<TurnResponse>(`/cases/${encodeURIComponent(opts.caseId)}/turn`, {
    user_input: opts.userInput,
    chat_history: opts.chatHistory ?? [],
  });
}
