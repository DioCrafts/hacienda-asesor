from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from hacienda_gpt.decision.schemas import CaseState


class CaseVersionConflictError(RuntimeError):
    """Raised when a case update loses an optimistic-concurrency race.

    The in-memory ``CaseState`` was derived from an older revision than the one
    now persisted — another turn updated the case in between. The caller should
    reload the case and retry (HTTP 409) rather than overwrite the newer state.
    """

    def __init__(self, case_id: str, expected: int, found: int) -> None:
        super().__init__(f"case {case_id!r} changed concurrently (expected version {expected}, found {found})")
        self.case_id = case_id
        self.expected = expected
        self.found = found


class CaseStateStore(ABC):
    """Abstract persistence contract for case state and audit trail."""

    @abstractmethod
    def get_case(self, case_id: str) -> CaseState | None:
        """Return the case for `case_id` or None when it does not exist."""

    @abstractmethod
    def save_case(self, case_state: CaseState) -> CaseState:
        """Persist a case state (create or update); return the stored state.

        Implementations enforce optimistic concurrency: updating an existing
        case whose ``version`` no longer matches the persisted row raises
        :class:`CaseVersionConflictError`. The returned state carries the bumped
        version so callers can keep working with the authoritative revision.
        """

    @abstractmethod
    def list_cases(self, user_id: str) -> list[CaseState]:
        """List all cases for a user sorted by update timestamp (newest first)."""

    @abstractmethod
    def append_audit_event(self, case_id: str, event: dict[str, Any]) -> None:
        """Append an audit event associated to a case."""
