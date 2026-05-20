from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from hacienda_gpt.decision.schemas import CaseState


class CaseStateStore(ABC):
    """Abstract persistence contract for case state and audit trail."""

    @abstractmethod
    def get_case(self, case_id: str) -> CaseState | None:
        """Return the case for `case_id` or None when it does not exist."""

    @abstractmethod
    def save_case(self, case_state: CaseState) -> None:
        """Persist a case state (create or update)."""

    @abstractmethod
    def list_cases(self, user_id: str) -> list[CaseState]:
        """List all cases for a user sorted by update timestamp (newest first)."""

    @abstractmethod
    def append_audit_event(self, case_id: str, event: dict[str, Any]) -> None:
        """Append an audit event associated to a case."""
