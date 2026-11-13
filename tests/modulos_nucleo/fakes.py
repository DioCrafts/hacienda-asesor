"""Fake household modules for the core's tests (never the real ones, which other people are building)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from hacienda_gpt.agent.providers.base import ToolSpec
from hacienda_gpt.modulos import registro as registro_mod
from hacienda_gpt.modulos.contrato import (
    AvisoModulo,
    ContextoModulo,
    FuenteModulo,
    HechoModulo,
    HerramientaModulo,
    ModuloBase,
    ResultadoHerramienta,
)
from hacienda_gpt.modulos.fiscal import MODULO as FISCAL


def spec(nombre: str, descripcion: str = "Calcula algo del hogar. Devuelve cifras.") -> ToolSpec:
    return ToolSpec(
        name=nombre,
        description=descripcion,
        input_schema={"type": "object", "additionalProperties": False, "properties": {"x": {"type": "number"}}},
    )


@dataclass
class Llamadas:
    """Records every execution of a fake tool."""

    registro: list[tuple[str, dict[str, Any], ContextoModulo]] = field(default_factory=list)

    def herramienta(
        self,
        nombre: str,
        efecto: str = "lectura",
        contenido: str | Callable[[Mapping[str, Any], ContextoModulo], str] = "Resultado: 12,34 €.",
        *,
        citas: tuple[str, ...] = ("BOE-A-2000-1",),
        falla: bool = False,
    ) -> HerramientaModulo:
        def ejecutar(argumentos: Mapping[str, Any], contexto: ContextoModulo) -> ResultadoHerramienta:
            self.registro.append((nombre, dict(argumentos), contexto))
            if falla:
                raise RuntimeError("fallo interno del módulo")
            texto = contenido(argumentos, contexto) if callable(contenido) else contenido
            return ResultadoHerramienta(contenido=texto, resumen=f"{nombre} ok", citas=citas)

        return HerramientaModulo(spec=spec(nombre), ejecutar=ejecutar, efecto=efecto)  # type: ignore[arg-type]

    def nombres(self) -> list[str]:
        return [nombre for nombre, _a, _c in self.registro]


def validar_lecturas(valor: Any) -> Any:
    if not isinstance(valor, list) or not all(isinstance(x, dict) and "m3" in x for x in valor):
        raise ValueError("Las lecturas deben ser una lista de objetos con «m3».")
    return sorted(({"m3": float(x["m3"]), "mes": str(x.get("mes", ""))} for x in valor), key=lambda x: x["mes"])


def validar_compania(valor: Any) -> Any:
    texto = str(valor).strip()
    if len(texto) < 2:
        raise ValueError("Indica el nombre de la compañía.")
    return texto.upper()


class ModuloFalso(ModuloBase):
    """A module whose notices are whatever the test sets (or an exception)."""

    def __init__(self, *args: Any, avisos_fijos: list[Any] | None = None, error: Exception | None = None, **kw: Any):
        super().__init__(*args, **kw)
        self.avisos_fijos = list(avisos_fijos or [])
        self.error = error
        self.contextos: list[ContextoModulo] = []

    def avisos(self, contexto: ContextoModulo) -> list[AvisoModulo]:
        self.contextos.append(contexto)
        if self.error is not None:
            raise self.error
        return list(self.avisos_fijos)


def agua(llamadas: Llamadas, **kw: Any) -> ModuloFalso:
    return ModuloFalso(
        clave="agua",
        nombre="Agua",
        descripcion="Factura del agua del hogar: consumo y tarifa.",
        instrucciones="Explica la factura del agua con las cifras de tus herramientas.",
        hechos=(
            HechoModulo("agua_compania", "string", "Compañía del agua", "Quién te suministra", validar_compania),
            HechoModulo("agua_lecturas", "json", "Lecturas", "Lecturas del contador", validar_lecturas),
            HechoModulo("agua_personas", "number", "Personas", "Personas en casa"),
            HechoModulo("agua_bonificada", "boolean", "Tarifa social", "Si tienes tarifa social"),
            # A shared fact, declared because the module reads it.
            HechoModulo("comunidad_autonoma", "string", "Comunidad", "Dónde vives"),
        ),
        herramientas=(
            llamadas.herramienta("agua_consumo", contenido="Consumo medio: 9,5 m3 al mes; coste 31,20 €."),
            llamadas.herramienta("agua_reclamacion", efecto="prepara", contenido="Estimada compañía: reclamo…"),
            llamadas.herramienta("agua_cambiar_tarifa", efecto="actua", contenido="Tarifa cambiada."),
            llamadas.herramienta("agua_roto", falla=True),
        ),
        fuentes=(
            FuenteModulo(
                "BOE-A-2001-14276",
                "Ley de Aguas (texto refundido)",
                "https://www.boe.es/buscar/act.php?id=BOE-A-2001-14276",
            ),
        ),
        **kw,
    )


def seguros(llamadas: Llamadas, **kw: Any) -> ModuloFalso:
    return ModuloFalso(
        clave="seguros",
        nombre="Seguros",
        descripcion="Seguros del hogar: coberturas y renovaciones.",
        instrucciones="Revisa las pólizas.",
        hechos=(HechoModulo("seguros_poliza", "string", "Póliza", "Número de póliza", sensible=True),),
        herramientas=(llamadas.herramienta("seguros_renovacion", contenido="Renovación: 210,00 € el 01/12/2026."),),
        fuentes=(FuenteModulo("dgsfp-registro", "Registro de la DGSFP", "https://example.invalid/dgsfp"),),
        **kw,
    )


def aviso(clave: str, **kw: Any) -> AvisoModulo:
    datos: dict[str, Any] = {"titulo": f"Aviso {clave}", "cuerpo": "Cuerpo del aviso."}
    datos.update(kw)
    return AvisoModulo(clave=clave, **datos)


def instalar(*modulos: Any) -> registro_mod.Registro:
    """Install a registry with the real tax module and the given fakes."""
    reg = registro_mod.construir_registro([(m.clave, m) for m in (FISCAL, *modulos)])
    assert not reg.errores, reg.errores
    registro_mod.reiniciar(reg)
    return reg
