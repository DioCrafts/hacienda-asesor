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


# Compile once at import time. Sanitisation runs once per retrieved doc on
# every ``/qa`` (~20 docs/request after the reranker first stage); previously
# the loop called ``re.sub`` with **string** patterns, relying on Python's
# internal LRU cache (512 entries, evictable). Pre-compiled patterns sidestep
# the cache lookup entirely.
#
# We intentionally keep the **sequential** application (each pattern's
# substitution feeds the next) rather than collapsing into a single
# ``|``-alternation. The alternation form would change semantics: the engine
# picks the longest match per position across the alternation, so
# ``ignore (all|previous|prior) instructions`` would now also fire on inputs
# the serial form left alone, and the order-of-application interactions
# (e.g. ``system prompt`` redacted first, then ``reveal .*prompt`` no longer
# matches) disappear. Behaviour stability over micro-perf — keep the list.
_COMPILED_PATTERNS: list[re.Pattern[str]] = [
    re.compile(pattern, re.IGNORECASE) for pattern in MALICIOUS_PATTERNS
]
_REDACTION = "[REDACTED_INJECTION_PATTERN]"


def sanitize_retrieved_context(text: str) -> str:
    """Redact common prompt-injection fragments from retrieved docs.

    Defence-in-depth; primary policy remains in the system prompt.
    """
    sanitized = text
    for pattern in _COMPILED_PATTERNS:
        sanitized = pattern.sub(_REDACTION, sanitized)
    return sanitized
