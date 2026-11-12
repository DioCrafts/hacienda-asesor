from datetime import date

from fastapi.testclient import TestClient
import pytest

from hacienda_gpt.api import api as api_module
from hacienda_gpt.api.api import app, get_case_store, get_notification_store
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.proactive.store import Notification, NotificationStore
from . import SALARIED, make_case


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    """API wired to a temporary database (cases and notifications in the same file)."""
    monkeypatch.delenv("HACIENDA_API_KEY", raising=False)
    db = str(tmp_path / "state.sqlite3")
    cases, notifications = SQLiteCaseStateStore(db), NotificationStore(db)
    cases.save_case(make_case("case-1", **SALARIED))
    app.dependency_overrides[get_case_store] = lambda: cases
    app.dependency_overrides[get_notification_store] = lambda: notifications
    try:
        yield TestClient(app), notifications
    finally:
        app.dependency_overrides.pop(get_case_store, None)
        app.dependency_overrides.pop(get_notification_store, None)


def _add(store: NotificationStore, key: str, **extra) -> Notification:
    fields = {"kind": "plazo", "title": "Modelo 303", "body": "Vence pronto", "severity": "aviso", **extra}
    notification = Notification(case_id="case-1", key=key, **fields)
    assert store.add(notification)
    return notification


def test_list_notifications_shape_and_unread_filter(ctx) -> None:
    client, store = ctx
    empty = client.get("/cases/case-1/notifications")
    assert empty.status_code == 200 and empty.json() == {"case_id": "case-1", "unread": 0, "notifications": []}

    first = _add(store, "k1", due_date=date(2027, 2, 1), actions=["Presenta el modelo"], sources=["art. 71.4 RIVA"])
    second = _add(store, "k2", kind="fin_de_ejercicio", amount="738.50", severity="urgente")
    store.mark_read("case-1", first.id)

    body = client.get("/cases/case-1/notifications").json()
    assert body["case_id"] == "case-1" and body["unread"] == 1
    assert [n["id"] for n in body["notifications"]] == [second.id, first.id]
    newest, oldest = body["notifications"]
    assert set(newest) == {
        "id", "case_id", "kind", "key", "title", "body", "severity", "actions", "sources", "amount", "due_date",
        "created_at", "read_at",
    }  # fmt: skip
    assert newest["amount"] == "738.50" and newest["due_date"] is None and newest["read_at"] is None
    assert newest["kind"] == "fin_de_ejercicio" and newest["severity"] == "urgente"
    assert oldest["amount"] is None and oldest["due_date"] == "2027-02-01" and oldest["read_at"] is not None
    assert oldest["actions"] == ["Presenta el modelo"] and oldest["sources"] == ["art. 71.4 RIVA"]

    unread = client.get("/cases/case-1/notifications", params={"unread_only": "true"}).json()
    assert [n["id"] for n in unread["notifications"]] == [second.id] and unread["unread"] == 1


def test_mark_read(ctx) -> None:
    client, store = ctx
    notification = _add(store, "k1")

    response = client.post(f"/cases/case-1/notifications/{notification.id}/read")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"case_id", "unread", "notifications"} and body["unread"] == 0
    assert body["notifications"][0]["id"] == notification.id and body["notifications"][0]["read_at"] is not None

    assert client.post("/cases/case-1/notifications/nope/read").status_code == 404
    assert client.post("/cases/case-1/notifications/nope/read").json() == {"detail": "notification not found"}


def test_run_proactive_jobs_on_demand(ctx, monkeypatch) -> None:
    client, _ = ctx
    monkeypatch.setattr(api_module, "hoy_en_espana", lambda: date(2026, 11, 12))

    response = client.post("/cases/case-1/proactive/run")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"case_id", "unread", "notifications", "created"}
    assert body["created"] == {"plazo": 0, "fin_de_ejercicio": 1} and body["unread"] == 1
    (plan,) = body["notifications"]
    assert plan["kind"] == "fin_de_ejercicio" and plan["amount"] == "738.50" and plan["due_date"] == "2026-12-31"
    assert plan["title"] == "Plan de fin de ejercicio 2026: hasta 738,50 € de ahorro estimado"

    again = client.post("/cases/case-1/proactive/run").json()
    assert again["created"] == {"plazo": 0, "fin_de_ejercicio": 0} and len(again["notifications"]) == 1

    # A profile edit through the API refreshes the plan on the next run.
    assert client.put("/cases/case-1/facts/aportacion_plan_pensiones", json={"value": 1500}).status_code == 200
    refreshed = client.post("/cases/case-1/proactive/run").json()
    assert refreshed["created"]["fin_de_ejercicio"] == 1 and len(refreshed["notifications"]) == 1
    assert refreshed["notifications"][0]["amount"] == "200.00"


def test_unknown_case_is_404_everywhere(ctx) -> None:
    client, _ = ctx
    for method, url in (
        ("get", "/cases/missing/notifications"),
        ("post", "/cases/missing/notifications/x/read"),
        ("post", "/cases/missing/proactive/run"),
    ):
        response = getattr(client, method)(url)
        assert response.status_code == 404 and response.json() == {"detail": "case not found"}


def test_routes_require_the_api_key_when_configured(ctx, monkeypatch) -> None:
    client, _ = ctx
    monkeypatch.setenv("HACIENDA_API_KEY", "secreto")
    assert client.get("/cases/case-1/notifications").status_code == 401
    assert client.post("/cases/case-1/notifications/x/read").status_code == 401
    assert client.post("/cases/case-1/proactive/run").status_code == 401
    assert client.get("/cases/case-1/notifications", headers={"X-API-Key": "secreto"}).status_code == 200
