// Stable id generator. `crypto.randomUUID` only exists in secure
// contexts (HTTPS or localhost); on a plain-HTTP host it is undefined and
// would throw. This helper degrades to a non-cryptographic fallback so id
// generation never breaks the UI.
export function newId(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  return `id-${Math.random().toString(36).slice(2)}-${Date.now().toString(36)}`;
}
