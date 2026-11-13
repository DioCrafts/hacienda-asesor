"""FastAPI dependencies: the session behind a request and who may touch a case.

Everything here reads ``settings.AUTH_MODE`` per request. With ``off`` the
dependencies let every request through unchanged and never open the database.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from hacienda_gpt import settings
from hacienda_gpt.auth.store import AuthStore, User

CONSENT_DOCUMENTS_DETAIL = (
    "Para analizar un documento hay que enviar su contenido al proveedor del modelo de lenguaje, "
    "y no has dado tu consentimiento para ello. Puedes darlo (y retirarlo cuando quieras) en "
    "«Privacidad y datos»; mientras tanto puedes introducir los datos a mano en tu expediente."
)

_store_lock = threading.Lock()
_stores: dict[str, AuthStore] = {}


def auth_required() -> bool:
    return settings.AUTH_MODE == "required"


def get_auth_store() -> AuthStore:
    """The process-wide store for the configured database (overridable in tests)."""
    path = settings.DECISION_STATE_DB_PATH
    with _store_lock:
        store = _stores.get(path)
        if store is None:
            store = _stores[path] = AuthStore(path)
        return store


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _unauthenticated() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Inicia sesión para continuar.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_user(
    store: Annotated[AuthStore, Depends(get_auth_store)],
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """The user behind ``Authorization: Bearer <token>``; 401 without a live session."""
    token = bearer_token(authorization)
    user = store.get_session_user(token) if token else None
    if user is None:
        raise _unauthenticated()
    return user


@dataclass(frozen=True)
class CaseAccess:
    """Who is asking, and what they may do with a case. ``user`` is None when AUTH_MODE is off."""

    user: User | None
    store: AuthStore | None

    def owner_id(self, requested: str) -> str:
        """``user_id`` to store on a new case: the account's id, never what the client sent."""
        return self.user.id if self.user is not None else requested

    def claim(self, case_id: str) -> None:
        """Record the current user as the owner of a case they just created."""
        if self.user is not None and self.store is not None:
            self.store.set_case_owner(case_id, self.user.id)

    def check(self, case_id: str | None) -> None:
        """404 unless the case belongs to the current user.

        The answer for somebody else's case is the same as for a case that does
        not exist, so case ids cannot be probed.
        """
        if case_id is None or self.user is None or self.store is None:
            return
        if not self.store.owns_case(case_id, self.user.id):
            raise HTTPException(status_code=404, detail="case not found")


def get_case_access(
    store: Annotated[AuthStore, Depends(get_auth_store)],
    authorization: Annotated[str | None, Header()] = None,
) -> CaseAccess:
    """Session check for an endpoint: no-op when AUTH_MODE is off, 401 without a session otherwise."""
    if not auth_required():
        return CaseAccess(user=None, store=None)
    return CaseAccess(user=require_user(store, authorization), store=store)


def require_session(access: Annotated[CaseAccess, Depends(get_case_access)]) -> None:
    """For endpoints that are not tied to a case but still need a logged-in user."""


def require_case_access(case_id: str, access: Annotated[CaseAccess, Depends(get_case_access)]) -> None:
    """For ``/cases/{case_id}/...`` endpoints: the case in the path must be the caller's."""
    access.check(case_id)


def require_document_consent(case_id: str, access: Annotated[CaseAccess, Depends(get_case_access)]) -> None:
    """403 unless the owner of the case agreed to send documents to the LLM provider."""
    access.check(case_id)
    if access.user is None or access.store is None:
        return
    if not access.store.has_consent(access.user.id, "envio_documentos_a_proveedor_llm"):
        raise HTTPException(status_code=403, detail=CONSENT_DOCUMENTS_DETAIL)
