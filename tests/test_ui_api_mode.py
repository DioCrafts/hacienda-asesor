
from types import SimpleNamespace

from hacienda_gpt.ui import app


def test_api_create_case_if_needed_stores_case_id(monkeypatch):
    app.st.session_state.clear()

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"case_id": "case_api_1"}

    monkeypatch.setattr(app.requests, "post", lambda *a, **k: Resp())
    cid = app._api_create_case_if_needed()
    assert cid == "case_api_1"
    assert app.st.session_state["api_case_id"] == "case_api_1"


def test_api_process_turn_returns_payload(monkeypatch):
    app.st.session_state.clear()

    def fake_post(url, json, timeout):
        class Resp:
            def raise_for_status(self):
                return None

            def json(self):
                if url.endswith('/cases'):
                    return {"case_id": "case_api_2"}
                return {"case_id": "case_api_2", "facts": [], "missing_facts": [], "candidate_obligation_ids": [], "next_questions": []}

        return Resp()

    monkeypatch.setattr(app.requests, "post", fake_post)
    result = app._api_process_turn("hola")
    assert result["case_id"] == "case_api_2"
