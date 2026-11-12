from datetime import UTC, datetime
import json
import sqlite3

import pytest

from hacienda_gpt import settings
from hacienda_gpt.proactive import entrega
from hacienda_gpt.proactive.entrega import (
    ConfiguracionCanalError,
    EntregaStore,
    SmtpCanal,
    WebhookCanal,
    canales_configurados,
    entregar_pendientes,
    texto_plano,
)
from hacienda_gpt.proactive.store import Notification, NotificationStore

AHORA = datetime(2026, 11, 12, 8, 0, tzinfo=UTC)

_NOTIFY_SETTINGS = {
    "NOTIFY_WEBHOOK_URL": "",
    "NOTIFY_WEBHOOK_FORMAT": "json",
    "NOTIFY_SMTP_HOST": "",
    "NOTIFY_SMTP_PORT": 587,
    "NOTIFY_SMTP_USER": "",
    "NOTIFY_SMTP_PASSWORD": "",
    "NOTIFY_SMTP_STARTTLS": True,
    "NOTIFY_EMAIL_FROM": "",
    "NOTIFY_EMAIL_TO": "",
}


def _n(key: str, case_id: str = "c1", **extra) -> Notification:
    fields = {"kind": "plazo", "title": f"Título {key}", "body": "Cuerpo", "severity": "aviso", **extra}
    return Notification(case_id=case_id, key=key, **fields)


class FakeCanal:
    def __init__(self, nombre: str, *, falla: bool = False) -> None:
        self.nombre = nombre
        self.falla = falla
        self.enviados: list[tuple[str, str]] = []
        self.intentos = 0

    def enviar(self, asunto: str, cuerpo: str) -> None:
        self.intentos += 1
        if self.falla:
            raise ConnectionError("servidor caído")
        self.enviados.append((asunto, cuerpo))


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "state.sqlite3")


@pytest.fixture
def sin_canales(monkeypatch) -> None:
    for name, value in _NOTIFY_SETTINGS.items():
        monkeypatch.setattr(settings, name, value)


def test_texto_plano_renders_body_actions_sources_without_markdown() -> None:
    notification = _n(
        "k",
        body="Te quedan **538,50 €** por aprovechar.\n\nHazlo antes de fin de año.",
        actions=["Aporta **1.500 €** al plan", "Guarda el justificante"],
        sources=["art. 51 LIRPF", "BOE-A-2006-20764"],
    )
    assert texto_plano(notification) == (
        "Te quedan 538,50 € por aprovechar.\n\nHazlo antes de fin de año.\n\n"
        "Qué hacer:\n- Aporta 1.500 € al plan\n- Guarda el justificante\n\n"
        "Fuentes:\n- art. 51 LIRPF\n- BOE-A-2006-20764\n\n"
        "Caso: c1\n"
    )
    assert texto_plano(_n("k2", case_id="c9")) == "Cuerpo\n\nCaso: c9\n"


def test_delivers_each_notification_once_per_channel(db) -> None:
    notifications = NotificationStore(db)
    notifications.add(_n("a", created_at=datetime(2026, 11, 1, tzinfo=UTC)))
    notifications.add(_n("b", case_id="c2", actions=["Haz X"], created_at=datetime(2026, 10, 1, tzinfo=UTC)))
    store = EntregaStore(db)
    uno, dos = FakeCanal("uno"), FakeCanal("dos")

    resumen = entregar_pendientes(store, [uno, dos], ahora=AHORA)

    cuenta = {"pendientes": 2, "enviadas": 2, "errores": 0}
    assert resumen == {"enviadas": 4, "errores": 0, "canales": {"uno": cuenta, "dos": cuenta}}
    # Every case goes to the same destination, in insertion order, one message each.
    assert [asunto for asunto, _ in uno.enviados] == ["Título a", "Título b"]
    assert uno.enviados[1][1] == "Cuerpo\n\nQué hacer:\n- Haz X\n\nCaso: c2\n"
    assert dos.enviados == uno.enviados

    again = entregar_pendientes(store, [uno, dos], ahora=AHORA)
    assert again["enviadas"] == 0 and len(uno.enviados) == 2 and len(dos.enviados) == 2

    # Only what arrived later goes out on the next run.
    notifications.add(_n("c"))
    assert entregar_pendientes(store, [uno], ahora=AHORA)["enviadas"] == 1
    assert [asunto for asunto, _ in uno.enviados] == ["Título a", "Título b", "Título c"]


def test_a_failing_channel_does_not_block_the_others_and_is_retried(db) -> None:
    notifications = NotificationStore(db)
    first = _n("a")
    notifications.add(first)
    notifications.add(_n("b"))
    store = EntregaStore(db)
    roto, sano = FakeCanal("roto", falla=True), FakeCanal("sano")

    resumen = entregar_pendientes(store, [roto, sano], ahora=AHORA)

    assert resumen["canales"]["roto"] == {"pendientes": 2, "enviadas": 0, "errores": 2}
    assert resumen["canales"]["sano"] == {"pendientes": 2, "enviadas": 2, "errores": 0}
    assert (resumen["enviadas"], resumen["errores"]) == (2, 2)
    assert store.entregada(first.id, "sano") and not store.entregada(first.id, "roto")

    # Once the channel is back it gets what it missed; the healthy one gets nothing again.
    roto.falla = False
    resumen = entregar_pendientes(store, [roto, sano], ahora=AHORA)
    assert resumen["canales"]["roto"]["enviadas"] == 2 and resumen["canales"]["sano"]["enviadas"] == 0
    assert len(sano.enviados) == 2


def test_a_channel_that_keeps_failing_is_left_for_the_next_run(db) -> None:
    notifications = NotificationStore(db)
    for index in range(10):
        notifications.add(_n(f"k{index}"))
    roto = FakeCanal("roto", falla=True)

    resumen = entregar_pendientes(EntregaStore(db), [roto], ahora=AHORA)

    assert roto.intentos == 3
    assert resumen["canales"]["roto"] == {"pendientes": 10, "enviadas": 0, "errores": 3}


def test_delivery_tracking_lives_in_its_own_table(db) -> None:
    notifications = NotificationStore(db)
    notification = _n("a")
    notifications.add(notification)
    store = EntregaStore(db)
    entregar_pendientes(store, [FakeCanal("uno")], ahora=AHORA)

    assert store.registrar(notification.id, "uno", ahora=datetime(2027, 1, 1, tzinfo=UTC)) is False
    rows = sqlite3.connect(db).execute("SELECT notification_id, canal, delivered_at FROM notification_deliveries")
    assert rows.fetchall() == [(notification.id, "uno", AHORA.isoformat())]
    # The notification itself is untouched: still unread, same content.
    assert notifications.list("c1") == [notification]


def test_entrega_store_works_on_a_database_without_notifications(db) -> None:
    store = EntregaStore(db)
    assert store.pendientes("uno") == []
    assert entregar_pendientes(store, [FakeCanal("uno")]) == {
        "enviadas": 0,
        "errores": 0,
        "canales": {"uno": {"pendientes": 0, "enviadas": 0, "errores": 0}},
    }


def test_marcar_todas_skips_the_backlog_without_sending(db) -> None:
    notifications = NotificationStore(db)
    notifications.add(_n("a"))
    notifications.add(_n("b"))
    store = EntregaStore(db)
    canal = FakeCanal("uno")

    assert store.marcar_todas("uno", ahora=AHORA) == 2
    assert store.marcar_todas("uno", ahora=AHORA) == 0
    assert entregar_pendientes(store, [canal], ahora=AHORA)["enviadas"] == 0 and canal.enviados == []
    assert len(store.pendientes("otro")) == 2  # per channel


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def read(self) -> bytes:
        return b"ok"


@pytest.fixture
def urlopen_calls(monkeypatch) -> list:
    calls: list = []

    def fake_urlopen(request, timeout=None):
        calls.append((request, timeout))
        return _Response()

    monkeypatch.setattr(entrega.urllib.request, "urlopen", fake_urlopen)
    return calls


def test_webhook_posts_json_with_title_and_text(urlopen_calls) -> None:
    WebhookCanal("https://hooks.example.test/abc").enviar("Título ñ", "Cuerpo €")

    ((request, timeout),) = urlopen_calls
    assert request.full_url == "https://hooks.example.test/abc"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json; charset=utf-8"
    assert json.loads(request.data.decode("utf-8")) == {"title": "Título ñ", "text": "Cuerpo €"}
    assert timeout == 30


def test_webhook_text_format_sends_the_body_and_a_title_header(urlopen_calls) -> None:
    canal = WebhookCanal("https://ntfy.example.test/tema", formato="text")
    canal.enviar("Plazo IVA", "Cuerpo €")
    canal.enviar("Título ñ", "x")

    (primero, _), (segundo, _) = urlopen_calls
    assert primero.data == "Cuerpo €".encode()
    assert primero.get_header("Content-type") == "text/plain; charset=utf-8"
    assert primero.get_header("Title") == "Plazo IVA"
    # Non-ASCII titles travel as an RFC 2047 encoded word (headers are latin-1 only).
    assert segundo.get_header("Title") == "=?utf-8?b?VMOtdHVsbyDDsQ==?="


@pytest.mark.parametrize("url", ["file:///etc/passwd", "hooks.example.test/abc", "ftp://example.test/x"])
def test_webhook_rejects_urls_that_are_not_http(url) -> None:
    with pytest.raises(ConfiguracionCanalError):
        WebhookCanal(url)


def test_webhook_rejects_an_unknown_format() -> None:
    with pytest.raises(ConfiguracionCanalError):
        WebhookCanal("https://hooks.example.test/abc", formato="xml")


class _FakeSMTP:
    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, **kwargs) -> None:
        self.host, self.port, self.kwargs = host, port, kwargs
        self.events: list = []
        self.messages: list = []
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.events.append("quit")

    def starttls(self, context=None) -> None:
        self.events.append("starttls")

    def login(self, user, password) -> None:
        self.events.append(("login", user, password))

    def send_message(self, message) -> None:
        self.events.append("send")
        self.messages.append(message)


@pytest.fixture
def smtp(monkeypatch):
    class Plain(_FakeSMTP):
        instances: list = []

    class Tls(_FakeSMTP):
        instances: list = []

    monkeypatch.setattr(entrega.smtplib, "SMTP", Plain)
    monkeypatch.setattr(entrega.smtplib, "SMTP_SSL", Tls)
    return Plain, Tls


def test_smtp_sends_a_plain_text_mail_with_starttls_and_login(smtp) -> None:
    plain, tls = smtp
    canal = SmtpCanal(
        host="smtp.example.test",
        user="usuario",
        password="secreto",  # noqa: S106 - fake credential for the fake server
        remitente="asesor@example.test",
        destinatario="yo@example.test",
    )
    canal.enviar("Plazo: modelo 130\n(3T)", "Cuerpo con eñe y €\n")

    (server,) = plain.instances
    assert tls.instances == []
    assert (server.host, server.port) == ("smtp.example.test", 587)
    assert server.events == ["starttls", ("login", "usuario", "secreto"), "send", "quit"]
    (message,) = server.messages
    assert message["Subject"] == "Plazo: modelo 130 (3T)"
    assert message["From"] == "asesor@example.test" and message["To"] == "yo@example.test"
    assert message.get_content_type() == "text/plain"
    assert message.get_content() == "Cuerpo con eñe y €\n"


def test_smtp_without_user_or_starttls_and_implicit_tls_on_465(smtp) -> None:
    plain, tls = smtp
    common = {"host": "smtp.example.test", "remitente": "a@example.test", "destinatario": "b@example.test"}

    SmtpCanal(port=25, starttls=False, **common).enviar("a", "b")
    assert plain.instances[0].events == ["send", "quit"]

    SmtpCanal(port=465, **common).enviar("a", "b")
    assert len(plain.instances) == 1
    assert tls.instances[0].events == ["send", "quit"] and tls.instances[0].port == 465


def test_canales_configurados_from_settings(monkeypatch, sin_canales) -> None:
    assert canales_configurados() == []

    monkeypatch.setattr(settings, "NOTIFY_WEBHOOK_URL", "https://hooks.example.test/abc")
    (webhook,) = canales_configurados()
    assert isinstance(webhook, WebhookCanal) and webhook.nombre == "webhook"

    monkeypatch.setattr(settings, "NOTIFY_SMTP_HOST", "smtp.example.test")
    with pytest.raises(ConfiguracionCanalError, match="NOTIFY_EMAIL_FROM, NOTIFY_EMAIL_TO"):
        canales_configurados()

    monkeypatch.setattr(settings, "NOTIFY_EMAIL_FROM", "a@example.test")
    monkeypatch.setattr(settings, "NOTIFY_EMAIL_TO", "b@example.test")
    assert [canal.nombre for canal in canales_configurados()] == ["webhook", "email"]


def test_delivery_can_be_restricted_to_consenting_owners(tmp_path) -> None:
    """With accounts on, notices of users who did not agree stay in the app (and stay pending)."""
    from datetime import UTC, datetime

    from hacienda_gpt.proactive.entrega import EntregaStore, entregar_pendientes
    from hacienda_gpt.proactive.store import Notification, NotificationStore

    db = str(tmp_path / "n.db")
    notifications = NotificationStore(db)
    for case_id in ("con", "sin"):
        notifications.add(
            Notification(case_id=case_id, kind="plazo", key="k", title=f"Aviso {case_id}", body="b", severity="info")
        )

    class Canal:
        nombre = "webhook"

        def __init__(self) -> None:
            self.enviados: list[str] = []

        def enviar(self, asunto: str, cuerpo: str) -> None:
            self.enviados.append(asunto)

    canal, store = Canal(), EntregaStore(db)
    ahora = datetime.now(UTC)
    resumen = entregar_pendientes(store, [canal], ahora=ahora, permitido=lambda n: n.case_id == "con")
    assert canal.enviados == ["Aviso con"] and resumen["enviadas"] == 1
    # The other one was not marked as delivered: it goes out once consent is given.
    assert entregar_pendientes(store, [canal], ahora=ahora)["enviadas"] == 1 and canal.enviados[-1] == "Aviso sin"
