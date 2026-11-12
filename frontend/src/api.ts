// Client + types for the HaciendaGPT FastAPI backend.
//
// These shapes mirror the Pydantic models in
//   hacienda_gpt/llm/grounding.py        -> AnswerEnvelope, Citation
//   hacienda_gpt/decision/schemas.py     -> ObligationCandidate, RiskLevel
//   hacienda_gpt/api/api.py              -> QARequest, TurnResponse
// so if the backend grows a new field, only this file changes.

// "cited" / "uncited" come from the legacy /qa grounding gate; the rest are
// the /agent verdicts (hacienda_gpt/agent/agent.py -> AgentMode).
export type AnswerMode =
  | "cited"
  | "uncited"
  | "abstained"
  | "verified"
  | "partially_verified"
  | "unverified"
  | "clarification";
export type RiskLevel = "low" | "medium" | "high" | "critical";

export interface Citation {
  title: string;
  locator: string;
  document_type?: string | null;
  section?: string | null;
  snippet?: string | null;
  // /agent only: the id the answer cites inline ("F2") and source provenance.
  fragment_id?: string | null;
  authority?: string | null;
  source_type?: string | null;
  binding?: boolean | null;
  date?: string | null;
  reference?: string | null;
}

export interface AnswerEnvelope {
  answer: string;
  mode: AnswerMode;
  citations: Citation[];
  raw_answer?: string | null;
  reason?: string | null;
  min_citations_required?: number;
  // /agent only: claims the verifier could not back with the cited fragment.
  warnings?: string[];
}

export type ClaimVerdict = "respaldada" | "parcial" | "no_respaldada";

export interface ClaimCheck {
  text: string;
  fragment_ids: string[];
  verdict: ClaimVerdict;
  reason: string;
}

export interface ToolTraceEntry {
  step: number;
  tool: string;
  arguments: Record<string, unknown>;
  summary: string;
  is_error: boolean;
}

// Mirrors hacienda_gpt/api/api.py -> AgentResponse.
export interface AgentResponse {
  answer: string;
  mode: AnswerMode;
  citations: Citation[];
  claims: ClaimCheck[];
  warnings: string[];
  raw_answer?: string | null;
  tool_trace: ToolTraceEntry[];
  obligations: ObligationCandidate[];
  case_id?: string | null;
  case_updated: boolean;
  provider: string;
  model: string;
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

// Abort any single request that outlives this budget, so a hung backend
// surfaces a clear timeout instead of leaving the UI stuck on "Consultando…".
const DEFAULT_TIMEOUT_MS = 30_000;
// The agent runs several search → reason → verify rounds per question; a
// thorough answer legitimately takes a few minutes.
const AGENT_TIMEOUT_MS = 300_000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

// Runtime shape guards. A 200 with an unexpected/partial body must NOT be cast
// blindly to the typed interface — that would defer the failure to a confusing
// `undefined.map` deep in render. We validate the fields the UI actually reads
// (extra keys are ignored) and throw a clear error otherwise.
function isAnswerEnvelope(value: unknown): value is AnswerEnvelope {
  return (
    isRecord(value) &&
    typeof value.answer === "string" &&
    (value.mode === "cited" || value.mode === "uncited" || value.mode === "abstained") &&
    Array.isArray(value.citations) &&
    typeof value.min_citations_required === "number"
  );
}

const AGENT_MODES: ReadonlySet<string> = new Set([
  "verified",
  "partially_verified",
  "unverified",
  "clarification",
  "abstained",
]);

function isAgentResponse(value: unknown): value is AgentResponse {
  return (
    isRecord(value) &&
    typeof value.answer === "string" &&
    typeof value.mode === "string" &&
    AGENT_MODES.has(value.mode) &&
    Array.isArray(value.citations) &&
    Array.isArray(value.claims) &&
    Array.isArray(value.warnings) &&
    Array.isArray(value.tool_trace) &&
    Array.isArray(value.obligations)
  );
}

function isTurnResponse(value: unknown): value is TurnResponse {
  return (
    isRecord(value) &&
    typeof value.case_id === "string" &&
    Array.isArray(value.obligations) &&
    Array.isArray(value.next_questions) &&
    Array.isArray(value.candidate_obligation_ids)
  );
}

function isCaseState(value: unknown): value is CaseState {
  return isRecord(value) && typeof value.case_id === "string";
}

interface RequestOptions {
  signal?: AbortSignal;
  timeoutMs?: number;
}

async function request<T>(
  path: string,
  body: unknown,
  validate: (value: unknown) => value is T,
  { signal, timeoutMs = DEFAULT_TIMEOUT_MS }: RequestOptions = {},
): Promise<T> {
  // Either the caller (e.g. "Nueva consulta" / unmount) or the timeout can
  // abort the request; combine both signals into one.
  const timeout = AbortSignal.timeout(timeoutMs);
  const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;

  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: combined,
    });
  } catch (err) {
    // A timeout gets a clear message; a caller-cancel or genuine network
    // failure propagates the original error (already an AbortError on cancel).
    if (timeout.aborted) throw new Error(`${path}: tiempo de espera agotado`, { cause: err });
    throw err;
  }

  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`${path} ${resp.status}: ${text || resp.statusText}`);
  }

  const data: unknown = await resp.json();
  if (!validate(data)) {
    throw new Error(`${path}: respuesta con formato inesperado del servidor`);
  }
  return data;
}

interface QAOptions {
  query: string;
  chatHistory?: Array<{ role: "user" | "assistant"; content: string }>;
  signal?: AbortSignal;
}

export function postQA({ query, chatHistory = [], signal }: QAOptions): Promise<AnswerEnvelope> {
  return request("/qa", { query, chat_history: chatHistory }, isAnswerEnvelope, { signal });
}

export function createCase(opts: {
  userId: string;
  taxPeriod: string;
  jurisdiction?: string;
  signal?: AbortSignal;
}): Promise<CaseState> {
  return request(
    "/cases",
    { user_id: opts.userId, tax_period: opts.taxPeriod, jurisdiction: opts.jurisdiction ?? "ES" },
    isCaseState,
    { signal: opts.signal },
  );
}

export function postTurn(opts: {
  caseId: string;
  userInput: string;
  chatHistory?: Array<{ role: "user" | "assistant"; content: string }>;
  signal?: AbortSignal;
}): Promise<TurnResponse> {
  return request(
    `/cases/${encodeURIComponent(opts.caseId)}/turn`,
    { user_input: opts.userInput, chat_history: opts.chatHistory ?? [] },
    isTurnResponse,
    { signal: opts.signal },
  );
}

export function postAgent(opts: {
  query: string;
  chatHistory?: Array<{ role: "user" | "assistant"; content: string }>;
  // Attach the case so the agent can read/record the user's facts and run the
  // rules engine; omit to get a search-only answer.
  caseId?: string | null;
  signal?: AbortSignal;
}): Promise<AgentResponse> {
  const body: Record<string, unknown> = { query: opts.query, chat_history: opts.chatHistory ?? [] };
  if (opts.caseId) body.case_id = opts.caseId;
  return request("/agent", body, isAgentResponse, { signal: opts.signal, timeoutMs: AGENT_TIMEOUT_MS });
}
