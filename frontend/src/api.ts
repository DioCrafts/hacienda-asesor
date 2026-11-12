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
  facts: Fact[];
  tax_period?: string | null;
  case_id?: string | null;
  case_updated: boolean;
  provider: string;
  model: string;
}

// Live progress of a running agent turn (hacienda_gpt/agent/agent.py -> emit).
export type ProgressEvent =
  | { type: "thinking"; step: number }
  | { type: "tool_call"; tool: string; arguments: Record<string, unknown> }
  | { type: "tool_result"; tool: string; summary: string; is_error: boolean }
  | { type: "verifying"; citations: number }
  | { type: "revising"; problems: number };

/** An API failure with the server's human-readable `detail`, when it sent one. */
export class ApiError extends Error {
  readonly status: number | null;
  constructor(message: string, status: number | null = null, options?: ErrorOptions) {
    super(message, options);
    this.name = "ApiError";
    this.status = status;
  }
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
  // Filing deadline declared by the rule (ISO date) and its caveat, if any.
  due_date?: string | null;
  due_date_note?: string | null;
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
    Array.isArray(value.obligations) &&
    Array.isArray(value.facts)
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
  method?: "POST" | "DELETE" | "GET";
}

/** FastAPI errors are `{"detail": "..."}`; surface that sentence, not raw JSON. */
async function errorFromResponse(path: string, resp: Response): Promise<ApiError> {
  const text = await resp.text().catch(() => "");
  let detail = text || resp.statusText;
  try {
    const parsed: unknown = JSON.parse(text);
    if (isRecord(parsed) && typeof parsed.detail === "string") detail = parsed.detail;
  } catch {
    // not JSON: keep the raw text
  }
  if (resp.status === 401) detail = "El servicio requiere una clave de acceso que esta aplicación no tiene configurada.";
  if (resp.status === 429) detail = "Demasiadas consultas seguidas. Espera un momento y vuelve a intentarlo.";
  return new ApiError(detail || `${path}: error ${resp.status}`, resp.status);
}

async function request<T>(
  path: string,
  body: unknown,
  validate: (value: unknown) => value is T,
  { signal, timeoutMs = DEFAULT_TIMEOUT_MS, method = "POST" }: RequestOptions = {},
): Promise<T> {
  // Either the caller (e.g. "Nueva consulta" / unmount) or the timeout can
  // abort the request; combine both signals into one.
  const timeout = AbortSignal.timeout(timeoutMs);
  const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;

  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: combined,
    });
  } catch (err) {
    // A timeout gets a clear message; a caller-cancel or genuine network
    // failure propagates the original error (already an AbortError on cancel).
    if (timeout.aborted) throw new ApiError("El servicio tardó demasiado en responder.", null, { cause: err });
    throw err;
  }

  if (!resp.ok) throw await errorFromResponse(path, resp);

  const data: unknown = await resp.json();
  if (!validate(data)) {
    throw new ApiError("El servidor devolvió una respuesta con un formato inesperado.", resp.status);
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

// If the stream goes silent for this long the connection is dead: the server
// sends a keep-alive line every ~10 s even while the model is thinking.
const STREAM_IDLE_TIMEOUT_MS = 60_000;

const PROGRESS_TYPES: ReadonlySet<string> = new Set(["thinking", "tool_call", "tool_result", "verifying", "revising"]);

function isProgressEvent(value: unknown): value is ProgressEvent {
  return isRecord(value) && typeof value.type === "string" && PROGRESS_TYPES.has(value.type);
}

/**
 * Run one agent turn over the NDJSON stream, reporting live progress.
 *
 * Resolves with the final AgentResponse; rejects with ApiError on a server
 * error (HTTP or in-stream), a dead connection, or a malformed stream.
 */
export async function streamAgent(
  opts: {
    query: string;
    chatHistory?: Array<{ role: "user" | "assistant"; content: string }>;
    caseId?: string | null;
    signal?: AbortSignal;
  },
  onProgress: (event: ProgressEvent) => void,
): Promise<AgentResponse> {
  const body: Record<string, unknown> = { query: opts.query, chat_history: opts.chatHistory ?? [] };
  if (opts.caseId) body.case_id = opts.caseId;

  const idle = new AbortController();
  const combined = opts.signal ? AbortSignal.any([opts.signal, idle.signal]) : idle.signal;
  let idleTimer = setTimeout(() => idle.abort(), STREAM_IDLE_TIMEOUT_MS);
  const touch = () => {
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => idle.abort(), STREAM_IDLE_TIMEOUT_MS);
  };

  try {
    const resp = await fetch(`${API_BASE}/agent/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: combined,
    });
    if (!resp.ok) throw await errorFromResponse("/agent/stream", resp);
    if (!resp.body) throw new ApiError("El servidor no devolvió ninguna respuesta.", resp.status);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { done, value } = await reader.read();
      touch();
      buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.trim()) continue;
        const message: unknown = JSON.parse(line);
        if (!isRecord(message)) continue;
        if (message.event === "progress" && isProgressEvent(message.data)) onProgress(message.data);
        if (message.event === "error") {
          const data = isRecord(message.data) ? message.data : {};
          throw new ApiError(
            typeof data.detail === "string" ? data.detail : "El servicio no pudo procesar la consulta.",
            typeof data.status === "number" ? data.status : null,
          );
        }
        if (message.event === "result") {
          if (!isAgentResponse(message.data)) {
            throw new ApiError("El servidor devolvió una respuesta con un formato inesperado.");
          }
          return message.data;
        }
      }
      if (done) throw new ApiError("La conexión se cerró antes de recibir la respuesta.");
    }
  } catch (err) {
    if (idle.signal.aborted && !opts.signal?.aborted) {
      throw new ApiError("Se perdió la conexión con el servicio mientras preparaba la respuesta.", null, {
        cause: err,
      });
    }
    if (err instanceof SyntaxError) {
      throw new ApiError("El servidor devolvió una respuesta ilegible.", null, { cause: err });
    }
    throw err;
  } finally {
    clearTimeout(idleTimer);
  }
}

/** Remove a fact the advisor recorded about the user; returns the updated case. */
export function deleteCaseFact(opts: { caseId: string; factName: string; signal?: AbortSignal }): Promise<CaseState> {
  return request(
    `/cases/${encodeURIComponent(opts.caseId)}/facts/${encodeURIComponent(opts.factName)}`,
    undefined,
    isCaseState,
    { signal: opts.signal, method: "DELETE" },
  );
}
