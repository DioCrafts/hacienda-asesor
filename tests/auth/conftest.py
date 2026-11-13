from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from fastapi.testclient import TestClient
import pytest

from hacienda_gpt import settings
from hacienda_gpt.api.api import app, get_case_store, get_notification_store
from hacienda_gpt.api.routers.auth import get_login_limiter
from hacienda_gpt.auth.deps import get_auth_store
from hacienda_gpt.auth.ratelimit import LoginRateLimiter
from hacienda_gpt.auth.store import AuthStore
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.proactive.store import NotificationStore

PASSWORD = "una-clave-larga-1"
CONSENTS = {"tratamiento_datos": True, "envio_documentos_a_proveedor_llm": False, "avisos_por_correo": False}


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch: pytest.MonkeyPatch) -> None:
    # The production work factor (128 MB per hash) would make the suite crawl.
    monkeypatch.setattr(settings, "AUTH_SCRYPT_N", 2**8)


@dataclass
class Env:
    client: TestClient
    db_path: str
    auth: AuthStore
    cases: SQLiteCaseStateStore
    notifications: NotificationStore
    limiter: LoginRateLimiter

    def register(self, email: str, *, consents: dict[str, bool] | None = None, password: str = PASSWORD) -> dict:
        response = self.client.post(
            "/auth/register", json={"email": email, "password": password, "consents": consents or CONSENTS}
        )
        assert response.status_code == 201, response.text
        body = response.json()
        return {"Authorization": f"Bearer {body['token']}", "_user_id": body["user"]["id"], "_token": body["token"]}

    def new_case(self, headers: dict) -> str:
        response = self.client.post(
            "/cases", json={"user_id": "lo-que-diga-el-cliente", "tax_period": "2025"}, headers=headers
        )
        assert response.status_code == 200, response.text
        return response.json()["case_id"]


@pytest.fixture
def env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    """The API with AUTH_MODE=required over a private database (all stores on the same file)."""
    monkeypatch.setattr(settings, "AUTH_MODE", "required")
    monkeypatch.delenv("HACIENDA_API_KEY", raising=False)
    db_path = str(tmp_path / "state.sqlite3")
    environment = Env(
        client=TestClient(app),
        db_path=db_path,
        auth=AuthStore(db_path),
        cases=SQLiteCaseStateStore(db_path),
        notifications=NotificationStore(db_path),
        limiter=LoginRateLimiter(3, 900),
    )
    overrides = {
        get_auth_store: lambda: environment.auth,
        get_case_store: lambda: environment.cases,
        get_notification_store: lambda: environment.notifications,
        get_login_limiter: lambda: environment.limiter,
    }
    app.dependency_overrides.update(overrides)
    try:
        yield environment
    finally:
        for dependency in overrides:
            app.dependency_overrides.pop(dependency, None)
        environment.auth.close()
        environment.cases.close()
        environment.notifications.close()
