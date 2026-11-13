"""Shared helpers for the format-drift tests: fixtures on disk and a scripted provider."""

from __future__ import annotations

from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
FORMATOS = FIXTURES / "formatos"


def leer(nombre: str) -> str:
    return (FORMATOS / nombre).read_text(encoding="utf-8")


class ProveedorGuionizado:
    """A provider that answers ``complete_json`` from a script and keeps everything it was sent."""

    def __init__(self, respuestas: list[Any]) -> None:
        self.respuestas = list(respuestas)
        self.llamadas: list[dict[str, Any]] = []

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any], schema_name: str, **_: Any) -> Any:
        self.llamadas.append({"system": system, "user": user, "schema_name": schema_name})
        if not self.respuestas:
            raise AssertionError("the script has no more answers")
        respuesta = self.respuestas.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta

    @property
    def texto_recibido(self) -> str:
        return "\n".join(llamada["system"] + "\n" + llamada["user"] for llamada in self.llamadas)

    def nombres(self) -> list[str]:
        return [llamada["schema_name"] for llamada in self.llamadas]
