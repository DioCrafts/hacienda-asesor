from __future__ import annotations

import re

MALICIOUS_PATTERNS = [
    r"ignore (all|previous|prior) instructions",
    r"system prompt",
    r"developer message",
    r"reveal .*prompt",
    r"you are now",
    r"bypass",
    r"exfiltrate",
    r"do not follow",
]


def sanitize_retrieved_context(text: str) -> str:
    """Redact common prompt-injection fragments from retrieved docs.

    This is defense-in-depth; primary policy remains in system prompt.
    """
    sanitized = text
    for pattern in MALICIOUS_PATTERNS:
        sanitized = re.sub(pattern, "[REDACTED_INJECTION_PATTERN]", sanitized, flags=re.IGNORECASE)
    return sanitized
