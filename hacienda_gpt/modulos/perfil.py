"""The household profile: the user's case facts, shared by every module.

The tax module's vocabulary (``EXPECTED_FACT_TYPE``) is static; module facts (:class:`HechoModulo`) join it
dynamically from the registry. A module fact is stored in the case like any other fact (so it is exported
and erased with the account, and its value is redacted from the audit trail when the user deletes it):

* ``string`` / ``number`` / ``boolean`` facts are coerced exactly like an extracted one and then passed to
  the module's ``validar``;
* ``json`` facts are parsed, validated by ``validar`` (which returns the canonical value) and stored as
  canonical JSON text, the way ``descendientes`` is.

Module facts are never offered to the chat fact extractor: the contract has no flag for it, and free text
from a model would not respect a module's validation. They are written by the user (the profile form) or
by the module itself through ``ContextoModulo.registrar``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
import json
from typing import Any

from hacienda_gpt.decision.fact_extractor import EXPECTED_FACT_TYPE, _coerce_value
from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType
from hacienda_gpt.modulos.contrato import HechoModulo, Modulo
from hacienda_gpt.modulos.registro import Registro, registro
from hacienda_gpt.tax.profile import DETALLE_FACT_TYPES

TIPO_DE_HECHO: dict[str, FactValueType] = {
    "string": FactValueType.STRING,
    "number": FactValueType.NUMBER,
    "boolean": FactValueType.BOOLEAN,
    # Stored as canonical JSON text, like the tax module's structured facts.
    "json": FactValueType.STRING,
}
_ESPERADO = {"string": "un texto", "number": "un número", "boolean": "sí o no", "json": "un JSON válido"}


class HechoInvalidoError(ValueError):
    """The value cannot be stored for that fact. The message is Spanish, for the user."""


def hecho_de_modulo(nombre: str, reg: Registro | None = None) -> tuple[Modulo, HechoModulo] | None:
    """The module that owns fact ``nombre`` and its declaration (None for tax facts and unknown names)."""
    if nombre in EXPECTED_FACT_TYPE:
        return None
    return (reg or registro()).hechos().get(nombre)


def tipo_esperado(nombre: str, reg: Registro | None = None) -> FactValueType | None:
    """Stored type of any known fact, tax or module (None when nobody declares it)."""
    expected = EXPECTED_FACT_TYPE.get(nombre)
    if expected is not None:
        return expected
    propio = hecho_de_modulo(nombre, reg)
    return TIPO_DE_HECHO[propio[1].tipo] if propio is not None else None


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, tuple | set | frozenset):
        return list(value)
    raise TypeError(f"valor no serializable: {type(value).__name__}")


def texto_json(valor: Any) -> str:
    """Canonical JSON text (sorted keys, no spaces), as structured facts are stored."""
    return json.dumps(valor, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=_json_default)


def normalizar_valor(hecho: HechoModulo, crudo: Any) -> str | float | bool:
    """Validate ``crudo`` for a module fact and return the value to store. Raises :class:`HechoInvalidoError`."""
    if hecho.tipo == "json":
        if isinstance(crudo, str):
            try:
                valor: Any = json.loads(crudo)
            except json.JSONDecodeError as exc:
                raise HechoInvalidoError(
                    f"«{hecho.etiqueta}» debe ser JSON válido (error en la línea {exc.lineno}, columna {exc.colno})."
                ) from exc
        else:
            valor = crudo
        valor = _validar(hecho, valor)
        try:
            return texto_json(valor)
        except TypeError as exc:
            raise HechoInvalidoError(f"«{hecho.etiqueta}»: {exc}.") from exc
    expected = TIPO_DE_HECHO[hecho.tipo]
    coerced = _coerce_value(
        expected=expected,
        value_string=crudo if isinstance(crudo, str) else None,
        value_number=crudo if isinstance(crudo, int | float) and not isinstance(crudo, bool) else None,
        value_boolean=crudo if isinstance(crudo, bool) else None,
    )
    if coerced is None:
        raise HechoInvalidoError(f"«{hecho.etiqueta}»: el valor no es válido (se espera {_ESPERADO[hecho.tipo]}).")
    valor = _validar(hecho, coerced)
    if isinstance(valor, Decimal):
        valor = float(valor)
    if hecho.tipo == "number" and isinstance(valor, int) and not isinstance(valor, bool):
        valor = float(valor)
    tipos: dict[str, type | tuple[type, ...]] = {"string": str, "number": float, "boolean": bool}
    if not isinstance(valor, tipos[hecho.tipo]) or (hecho.tipo == "number" and isinstance(valor, bool)):
        raise HechoInvalidoError(f"«{hecho.etiqueta}»: la validación del módulo devolvió un valor de otro tipo.")
    if isinstance(valor, str) and not valor.strip():
        raise HechoInvalidoError(f"«{hecho.etiqueta}» no puede quedar vacío.")
    resultado: str | float | bool = valor
    return resultado


def _validar(hecho: HechoModulo, valor: Any) -> Any:
    if hecho.validar is None:
        return valor
    try:
        return hecho.validar(valor)
    except HechoInvalidoError:
        raise
    except ValueError as exc:
        raise HechoInvalidoError(str(exc) or f"«{hecho.etiqueta}»: valor no válido.") from exc
    except Exception as exc:  # a validator bug must reach the user as a refusal, not as a 500
        raise HechoInvalidoError(f"«{hecho.etiqueta}»: no se pudo validar el valor ({type(exc).__name__}).") from exc


def es_json(nombre: str, reg: Registro | None = None) -> bool:
    if nombre in DETALLE_FACT_TYPES:
        return True
    propio = hecho_de_modulo(nombre, reg)
    return propio is not None and propio[1].tipo == "json"


def valor_de_perfil(nombre: str, valor: Any, reg: Registro | None = None) -> Any:
    """A stored value as a module reads it: JSON facts parsed (left as text if the text is not JSON)."""
    if isinstance(valor, str) and es_json(nombre, reg):
        try:
            return json.loads(valor)
        except json.JSONDecodeError:
            return valor
    return valor


def perfil_de(case: CaseState | None, reg: Registro | None = None) -> dict[str, Any]:
    """Every fact of the case (name -> value), JSON-valued facts parsed: ``ContextoModulo.perfil``."""
    if case is None:
        return {}
    return {fact.name: valor_de_perfil(fact.name, fact.value, reg) for fact in case.facts}


def hechos_para_modulo(perfil: Mapping[str, Any], modulo: Modulo, reg: Registro | None = None) -> dict[str, Any]:
    """What a module's specialist is shown of the profile: the shared facts and its own, never another
    module's (data minimisation: the specialist's context is sent to the model provider)."""
    ajenos = {nombre for nombre, (dueno, _h) in (reg or registro()).hechos().items() if dueno.clave != modulo.clave}
    return {nombre: valor for nombre, valor in perfil.items() if nombre not in ajenos}


def nuevo_hecho(nombre: str, valor: Any, *, tipo: FactValueType, fuente: str, confianza: float = 1.0) -> Fact:
    return Fact(
        fact_id=f"fact_{nombre}",
        name=nombre,
        value=valor,
        value_type=tipo,
        source=fuente,
        confidence=confianza,
        updated_at=datetime.now(UTC),
    )


def con_hechos(case: CaseState, nuevos: list[Fact]) -> CaseState:
    """``case`` with ``nuevos`` replacing the facts of the same name (missing facts kept consistent)."""
    nombres = {fact.name for fact in nuevos}
    facts = [*(f for f in case.facts if f.name not in nombres), *nuevos]
    known = {fact.name for fact in facts}
    return case.model_copy(
        update={
            "facts": facts,
            "missing_facts": [m for m in case.missing_facts if m.fact_name not in known],
            "updated_at": datetime.now(UTC),
        }
    )


def registrar_hechos_de_modulo(
    case: CaseState, modulo: Modulo, valores: Mapping[str, Any], *, fuente: str, reg: Registro | None = None
) -> CaseState:
    """Validate facts a module writes (only facts it declared) and return the updated case.

    Raises :class:`HechoInvalidoError` for an undeclared name or an invalid value; nothing is written then.
    """
    propios = {h.nombre: h for h in modulo.hechos if hecho_de_modulo(h.nombre, reg) is not None}
    nuevos: list[Fact] = []
    for nombre, crudo in valores.items():
        hecho = propios.get(nombre)
        if hecho is None:
            raise HechoInvalidoError(f"El módulo {modulo.nombre} no puede registrar el dato «{nombre}».")
        nuevos.append(
            nuevo_hecho(nombre, normalizar_valor(hecho, crudo), tipo=TIPO_DE_HECHO[hecho.tipo], fuente=fuente)
        )
    return con_hechos(case, nuevos)


__all__ = [
    "HechoInvalidoError",
    "TIPO_DE_HECHO",
    "con_hechos",
    "es_json",
    "hecho_de_modulo",
    "hechos_para_modulo",
    "normalizar_valor",
    "nuevo_hecho",
    "perfil_de",
    "registrar_hechos_de_modulo",
    "texto_json",
    "tipo_esperado",
    "valor_de_perfil",
]
