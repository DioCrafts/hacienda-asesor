from hacienda_gpt.decision.schemas import CaseState
from hacienda_gpt.ui import app


class FakeStore:
    def __init__(self) -> None:
        self._case: CaseState | None = None
        self.events: list[dict] = []

    def get_case(self, case_id: str) -> CaseState | None:
        return self._case

    def save_case(self, case_state: CaseState) -> None:
        self._case = case_state

    def append_audit_event(self, case_id: str, event: dict) -> None:
        self.events.append({"case_id": case_id, **event})


def test_persist_turn_creates_case_and_audit_events(monkeypatch) -> None:
    monkeypatch.setitem(app.st.session_state, "user_id", "user_test")
    store = FakeStore()

    case = app._persist_turn_local(
        store=store,
        case_id="case_x",
        user_input="Soy residente en España y tengo dudas sobre IRPF",
        assistant_output="respuesta",
    )

    assert case.case_id == "case_x"
    assert len(case.facts) >= 1
    assert len(store.events) == 2


def test_persist_turn_updates_existing_case(monkeypatch) -> None:
    monkeypatch.setitem(app.st.session_state, "user_id", "user_test")
    store = FakeStore()
    app._persist_turn_local(store=store, case_id="case_x", user_input="Soy residente en España", assistant_output="ok")
    updated = app._persist_turn_local(
        store=store, case_id="case_x", user_input="Tengo ingresos del trabajo", assistant_output="ok2"
    )

    assert updated.case_id == "case_x"
    assert updated.user_id == "user_test"
    assert len(store.events) == 4
