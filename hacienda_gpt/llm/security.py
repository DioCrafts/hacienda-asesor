from __future__ import annotations

import re

MALICIOUS_PATTERNS = [
    # English
    r"ignore (all|previous|prior) instructions",
    r"system prompt",
    r"developer message",
    r"reveal .*prompt",
    r"you are now",
    r"bypass",
    r"exfiltrate",
    r"do not follow",
    # Spanish — the corpus and the realistic attackers are Spanish-speaking, so
    # the English-only list above let injections through. Patterns are kept
    # high-signal to avoid mangling legitimate fiscal text (e.g. "el pagador
    # actúa como retenedor" must NOT be redacted, so "actúa como" is omitted).
    r"ignora(?:r)?\s+(?:todas\s+)?(?:las\s+)?(?:instrucciones|reglas)\s+(?:anteriores|previas)",
    r"ignora(?:r)?\s+(?:todas\s+)?(?:las\s+)?(?:instrucciones|reglas)\b",
    r"olvida(?:r|te)?\s+(?:las\s+)?instrucciones",
    r"haz\s+caso\s+omiso\s+de\s+(?:las\s+)?(?:instrucciones|reglas)",
    r"no\s+sigas\s+(?:las\s+)?(?:instrucciones|reglas)",
    r"(?:instrucciones|mensaje|prompt)\s+del?\s+(?:sistema|desarrollador)",
    r"(?:revela|muestra|ens(?:e|é)ña)(?:r)?\s+.*prompt",
    r"ahora\s+eres\b",
    r"eres\s+ahora\b",
    r"exfiltra(?:r)?",
]


def sanitize_retrieved_context(text: str) -> str:
    """Redact common prompt-injection fragments from retrieved docs.

    This is defense-in-depth; primary policy remains in system prompt.
    """
    sanitized = text
    for pattern in MALICIOUS_PATTERNS:
        sanitized = re.sub(pattern, "[REDACTED_INJECTION_PATTERN]", sanitized, flags=re.IGNORECASE)
    return sanitized
