"""Delivery of notifications outside the app (webhook and e-mail).

The proactive jobs write notifications to SQLite and, until now, they were only
visible in the web UI. :func:`entregar_pendientes` pushes each one, once per
channel, to the channels configured through the ``NOTIFY_*`` settings.

Who receives them: cases have no e-mail address of their own (there are no user
accounts yet), so EVERY notification of EVERY case is delivered to the single
owner/operator destination configured in the environment. This is meant for a
personal or single-operator deployment; it is not per-user delivery.

Delivery is tracked in a separate table, ``notification_deliveries``, in the
same SQLite file, so the ``Notification`` model and its table stay untouched. A
notification is recorded as delivered on a channel only after that channel
accepted it, which makes the job safe to re-run: nothing is sent twice, and
whatever failed is retried on the next run.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from email.header import Header
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
import json
import logging
from pathlib import Path
import smtplib
import sqlite3
import ssl
from threading import RLock
from typing import Any, Protocol
from urllib.parse import urlsplit
import urllib.request

from hacienda_gpt import settings
from hacienda_gpt.proactive.store import Notification, NotificationStore

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 30
# After this many failures in a row a channel is considered down for the rest of
# the run, so an unreachable server does not cost one timeout per notification.
_MAX_CONSECUTIVE_FAILURES = 3


class ConfiguracionCanalError(ValueError):
    """A channel is configured only in part, or with a value that cannot work."""


class Canal(Protocol):
    """A destination for messages. ``nombre`` identifies it in the delivery records."""

    nombre: str

    def enviar(self, asunto: str, cuerpo: str) -> None:
        """Send one message; raise on any failure."""
        ...


class WebhookCanal:
    """POST each message to a webhook URL.

    Two payload formats:

    * ``json`` (default): ``{"title": asunto, "text": cuerpo}`` with
      ``Content-Type: application/json``. Slack-compatible incoming webhooks
      (Slack, Mattermost, Rocket.Chat...) read ``text``; generic receivers get
      both keys.
    * ``text``: the body as ``text/plain`` and the subject in a ``Title``
      header, which is what an ntfy.sh topic URL expects.

    The URL usually embeds a secret token, so it is never logged.
    """

    nombre = "webhook"

    def __init__(self, url: str, *, formato: str = "json", timeout: float = _TIMEOUT_SECONDS) -> None:
        if urlsplit(url).scheme not in {"http", "https"}:
            raise ConfiguracionCanalError("NOTIFY_WEBHOOK_URL debe ser una URL http(s).")
        if formato not in {"json", "text"}:
            raise ConfiguracionCanalError("NOTIFY_WEBHOOK_FORMAT debe ser «json» o «text».")
        self._url = url
        self._formato = formato
        self._timeout = timeout

    def enviar(self, asunto: str, cuerpo: str) -> None:
        if self._formato == "json":
            data = json.dumps({"title": asunto, "text": cuerpo}, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json; charset=utf-8"}
        else:
            data = cuerpo.encode("utf-8")
            # HTTP headers are latin-1 only; ntfy decodes RFC 2047 encoded words.
            titulo = asunto if asunto.isascii() else Header(asunto, "utf-8").encode(maxlinelen=10_000)
            headers = {"Content-Type": "text/plain; charset=utf-8", "Title": titulo}
        # The scheme was validated in __init__, so this never opens file:// and the like.
        request = urllib.request.Request(self._url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            response.read()


class SmtpCanal:
    """Send each message as a plain-text e-mail through an SMTP server (stdlib only).

    ``starttls`` upgrades the connection before authenticating (port 587). Port
    465 uses implicit TLS instead. ``user`` empty means no authentication.
    """

    nombre = "email"

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        user: str = "",
        password: str = "",
        remitente: str,
        destinatario: str,
        starttls: bool = True,
        timeout: float = _TIMEOUT_SECONDS,
    ) -> None:
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._remitente = remitente
        self._destinatario = destinatario
        self._starttls = starttls
        self._timeout = timeout

    def _mensaje(self, asunto: str, cuerpo: str) -> EmailMessage:
        message = EmailMessage()
        # A header cannot contain line breaks.
        message["Subject"] = " ".join(asunto.split())
        message["From"] = self._remitente
        message["To"] = self._destinatario
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid()
        message.set_content(cuerpo)
        return message

    def enviar(self, asunto: str, cuerpo: str) -> None:
        message = self._mensaje(asunto, cuerpo)
        context = ssl.create_default_context()
        smtp: smtplib.SMTP
        if self._port == 465:
            smtp = smtplib.SMTP_SSL(self._host, self._port, timeout=self._timeout, context=context)
        else:
            smtp = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        with smtp:
            if self._port != 465 and self._starttls:
                smtp.starttls(context=context)
            if self._user:
                smtp.login(self._user, self._password)
            smtp.send_message(message)


def canales_configurados() -> list[Canal]:
    """Channels enabled by the ``NOTIFY_*`` settings (empty list: nothing configured).

    Raises :class:`ConfiguracionCanalError` when a channel is configured only in
    part: silently skipping it would look like "delivery is on" while nothing
    is being sent.
    """
    canales: list[Canal] = []
    if settings.NOTIFY_WEBHOOK_URL:
        canales.append(WebhookCanal(settings.NOTIFY_WEBHOOK_URL, formato=settings.NOTIFY_WEBHOOK_FORMAT))
    correo = {
        "NOTIFY_SMTP_HOST": settings.NOTIFY_SMTP_HOST,
        "NOTIFY_EMAIL_FROM": settings.NOTIFY_EMAIL_FROM,
        "NOTIFY_EMAIL_TO": settings.NOTIFY_EMAIL_TO,
    }
    if any(correo.values()):
        faltan = [nombre for nombre, valor in correo.items() if not valor]
        if faltan:
            raise ConfiguracionCanalError(
                "El correo está configurado a medias: falta " + ", ".join(faltan) + ". "
                "Hacen falta NOTIFY_SMTP_HOST, NOTIFY_EMAIL_FROM y NOTIFY_EMAIL_TO."
            )
        canales.append(
            SmtpCanal(
                host=settings.NOTIFY_SMTP_HOST,
                port=settings.NOTIFY_SMTP_PORT,
                user=settings.NOTIFY_SMTP_USER,
                password=settings.NOTIFY_SMTP_PASSWORD,
                remitente=settings.NOTIFY_EMAIL_FROM,
                destinatario=settings.NOTIFY_EMAIL_TO,
                starttls=settings.NOTIFY_SMTP_STARTTLS,
            )
        )
    return canales


class EntregaStore:
    """Which notification was delivered on which channel (same SQLite file as the notifications)."""

    def __init__(self, db_path: str) -> None:
        self.db_path = str(Path(db_path))
        # NotificationStore owns the schema of ``notifications``; make sure it
        # exists so the join below works on a database no job has written to yet.
        NotificationStore(self.db_path).close()
        self._lock = RLock()
        self._conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS notification_deliveries (
                    notification_id TEXT NOT NULL,
                    canal TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    PRIMARY KEY (notification_id, canal)
                )
                """)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def pendientes(self, canal: str) -> list[Notification]:
        """Notifications of every case not yet delivered on ``canal``, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT n.payload_json, n.read_at FROM notifications n "
                "LEFT JOIN notification_deliveries d ON d.notification_id = n.id AND d.canal = ? "
                "WHERE d.notification_id IS NULL ORDER BY n.seq",
                (canal,),
            ).fetchall()
        out: list[Notification] = []
        for row in rows:
            notification = Notification.model_validate_json(row["payload_json"])
            notification.read_at = datetime.fromisoformat(row["read_at"]) if row["read_at"] else None
            out.append(notification)
        return out

    def registrar(self, notification_id: str, canal: str, *, ahora: datetime) -> bool:
        """Record a delivery; False when it was already recorded (the first timestamp is kept)."""
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO notification_deliveries (notification_id, canal, delivered_at) VALUES (?, ?, ?)",
                (notification_id, canal, ahora.isoformat()),
            )
            return cursor.rowcount == 1

    def entregada(self, notification_id: str, canal: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM notification_deliveries WHERE notification_id = ? AND canal = ?",
                (notification_id, canal),
            ).fetchone()
        return row is not None

    def marcar_todas(self, canal: str, *, ahora: datetime) -> int:
        """Record everything pending on ``canal`` as delivered WITHOUT sending it.

        For the day delivery is switched on over a database that already holds
        months of notifications nobody wants to receive in one burst.
        """
        return sum(self.registrar(notification.id, canal, ahora=ahora) for notification in self.pendientes(canal))


def texto_plano(notification: Notification) -> str:
    """Plain-text rendering of a notification: body, next steps, sources and the case it belongs to."""
    partes = [notification.body.replace("**", "").strip()]
    if notification.actions:
        partes.append("Qué hacer:\n" + "\n".join(f"- {accion.replace('**', '')}" for accion in notification.actions))
    if notification.sources:
        partes.append("Fuentes:\n" + "\n".join(f"- {fuente}" for fuente in notification.sources))
    # Everything goes to one operator address, so say which case this is about.
    partes.append(f"Caso: {notification.case_id}")
    return "\n\n".join(parte for parte in partes if parte) + "\n"


def entregar_pendientes(
    store: EntregaStore,
    canales: list[Canal],
    *,
    ahora: datetime | None = None,
    permitido: Callable[[Notification], bool] | None = None,
) -> dict[str, Any]:
    """Send every notification not yet delivered on each channel, one message per notification.

    Subject = the notification title; body = :func:`texto_plano`. A delivery is
    recorded only after the channel accepted the message, so a failed one is
    retried on the next run and nothing is ever sent twice on the same channel.
    Channels are independent: an exception in one is counted and logged, and
    the others still get everything. A channel that fails several times in a
    row is left for the next run.

    Returns ``{"enviadas", "errores", "canales": {nombre: {"pendientes", "enviadas", "errores"}}}``.
    """
    ahora = ahora or datetime.now(UTC)
    resumen: dict[str, Any] = {"enviadas": 0, "errores": 0, "canales": {}}
    for canal in canales:
        pendientes = store.pendientes(canal.nombre)
        if permitido is not None:
            # e.g. the owner of the case has not consented to notices leaving the app:
            # those stay pending, in case the consent is given later.
            pendientes = [n for n in pendientes if permitido(n)]
        cuenta = {"pendientes": len(pendientes), "enviadas": 0, "errores": 0}
        seguidos = 0
        for notification in pendientes:
            try:
                canal.enviar(notification.title, texto_plano(notification))
            except Exception as exc:  # one broken channel must not stop the others
                cuenta["errores"] += 1
                seguidos += 1
                # Only the exception type and message: never the webhook URL or credentials.
                logger.warning(
                    "Delivery of %s on %s failed: %s: %s", notification.id, canal.nombre, type(exc).__name__, exc
                )
                if seguidos >= _MAX_CONSECUTIVE_FAILURES:
                    logger.warning("Channel %s looks down; the rest is left for the next run", canal.nombre)
                    break
                continue
            seguidos = 0
            store.registrar(notification.id, canal.nombre, ahora=ahora)
            cuenta["enviadas"] += 1
        resumen["canales"][canal.nombre] = cuenta
        resumen["enviadas"] += cuenta["enviadas"]
        resumen["errores"] += cuenta["errores"]
    return resumen
