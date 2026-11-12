"""User accounts, sessions, consents and case ownership (see docs/privacidad.md)."""

from hacienda_gpt.auth.deps import (
    CaseAccess,
    auth_required,
    get_auth_store,
    get_case_access,
    require_case_access,
    require_document_consent,
    require_session,
    require_user,
)
from hacienda_gpt.auth.store import CONSENT_TEXT_VERSION, CONSENT_TYPES, AuthStore, ConsentRecord, ConsentType, User

__all__ = [
    "CONSENT_TEXT_VERSION",
    "CONSENT_TYPES",
    "AuthStore",
    "CaseAccess",
    "ConsentRecord",
    "ConsentType",
    "User",
    "auth_required",
    "get_auth_store",
    "get_case_access",
    "require_case_access",
    "require_document_consent",
    "require_session",
    "require_user",
]
