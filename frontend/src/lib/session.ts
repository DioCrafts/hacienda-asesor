// The signed-in session on this browser (only used when the backend runs with
// AUTH_MODE=required). The token is an opaque bearer credential: it lives in
// localStorage so a reload or a second tab stays signed in, and it is dropped
// on logout, on account deletion and as soon as the server rejects it.

const TOKEN_KEY = "haciendagpt_session_token";
const OWNER_KEY = "haciendagpt_history_owner";
const MODE_KEY = "haciendagpt_auth_mode";

export type AuthMode = "off" | "required";

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // Storage disabled: the session then lasts until the page is reloaded.
  }
}

// Kept in memory too, so the app keeps working when localStorage is unavailable.
let currentToken: string | null = read(TOKEN_KEY);

export function getSessionToken(): string | null {
  return currentToken;
}

export function setSessionToken(token: string | null): void {
  currentToken = token;
  write(TOKEN_KEY, token);
}

/**
 * The conversation history in localStorage belongs to whoever was signed in
 * when it was written. Returns true when `userId` is a different account, in
 * which case the caller must discard that history before showing anything.
 */
export function historyBelongsToSomeoneElse(userId: string): boolean {
  const owner = read(OWNER_KEY);
  return owner !== userId;
}

export function setHistoryOwner(userId: string | null): void {
  write(OWNER_KEY, userId);
}

/** Last auth mode the backend reported, so a reload does not flash the wrong screen. */
export function cachedAuthMode(): AuthMode | null {
  const mode = read(MODE_KEY);
  return mode === "off" || mode === "required" ? mode : null;
}

export function cacheAuthMode(mode: AuthMode): void {
  write(MODE_KEY, mode);
}
