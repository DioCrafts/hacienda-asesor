"""Discovery and validation of modules against the contract (with fake modules only)."""

from __future__ import annotations

from pathlib import Path
import sys
import textwrap

import pytest

from hacienda_gpt import settings
from hacienda_gpt.modulos import registro as registro_mod
from hacienda_gpt.modulos.contrato import HechoModulo, ModuloBase
from hacienda_gpt.modulos.fiscal import MODULO as FISCAL
from hacienda_gpt.modulos.registro import construir_registro, descubrir, problemas_de
from .fakes import Llamadas, agua, seguros, spec


def _modulo(clave: str = "agua", **kw) -> ModuloBase:
    datos = {"nombre": "Agua", "descripcion": "Agua del hogar.", "instrucciones": "Sé exacto."}
    datos.update(kw)
    return ModuloBase(clave=clave, **datos)


def test_valid_modules_are_accepted_tax_first() -> None:
    llamadas = Llamadas()
    reg = construir_registro([("seguros", seguros(llamadas)), ("agua", agua(llamadas)), ("fiscal", FISCAL)])
    assert list(reg.modulos) == ["fiscal", "agua", "seguros"] and reg.errores == {}
    # Shared facts a module declares are not its own: the core keeps their definition.
    assert set(reg.hechos()) == {"agua_compania", "agua_lecturas", "agua_personas", "agua_bonificada", "seguros_poliza"}
    assert reg.herramienta("agua_consumo")[0].clave == "agua"
    assert reg.herramienta("calcular_irpf")[0].clave == "fiscal"


@pytest.mark.parametrize(
    ("modulo", "mensaje"),
    [
        (object(), "no cumple el protocolo"),
        (_modulo(clave="Agua!"), "clave no válida"),
        (_modulo(clave="plazo"), "reservada"),
        (_modulo(nombre=""), "nombre vacío"),
        (_modulo(hechos=(HechoModulo("consumo", "number", "Consumo", "x"),)), "debe empezar por agua_"),
        (_modulo(hechos=(HechoModulo("agua_x", "fecha", "X", "x"),)), "tipo desconocido"),  # type: ignore[arg-type]
        (_modulo(hechos=(HechoModulo("agua_x", "number", "X", "x"),) * 2), "hecho repetido"),
        (_modulo(herramientas=(Llamadas().herramienta("calcular_consumo"),)), "debe empezar por agua_"),
        (_modulo(herramientas=(Llamadas().herramienta("agua_x", efecto="borra"),)), "efecto desconocido"),
        (_modulo(herramientas=("no",)), "no es HerramientaModulo"),
        (_modulo(fuentes=("BOE",)), "fuente que no es FuenteModulo"),
    ],
)
def test_contract_rules(modulo, mensaje: str) -> None:
    problemas = problemas_de(modulo, paquete=getattr(modulo, "clave", None))
    assert any(mensaje in p for p in problemas), problemas


def test_key_must_match_the_package() -> None:
    assert any("no coincide" in p for p in problemas_de(_modulo(), paquete="otro"))


def test_a_broken_module_is_left_out_and_the_others_stay() -> None:
    llamadas = Llamadas()
    copia = agua(llamadas)
    copia_mala = _modulo(clave="riego", hechos=(HechoModulo("agua_compania", "string", "Compañía", "x"),))
    herramienta_repetida = _modulo(clave="riegos", herramientas=(llamadas.herramienta("agua_consumo"),))
    reg = construir_registro(
        [
            ("agua", copia),
            ("riego", copia_mala),
            ("riegos", herramienta_repetida),
            ("roto", _modulo(clave="roto", nombre="")),
        ]
    )
    assert list(reg.modulos) == ["agua"]
    assert set(reg.errores) == {"riego", "riegos", "roto"}
    assert "ya lo declara el módulo agua" in reg.errores["riego"] or "debe empezar por riego_" in reg.errores["riego"]


def test_two_modules_with_the_same_key() -> None:
    reg = construir_registro([("agua", agua(Llamadas())), ("agua", _modulo())])
    assert list(reg.modulos) == ["agua"] and "ya la usa otro módulo" in reg.errores["agua"]


def test_discovery_imports_packages_and_survives_broken_ones(tmp_path: Path, monkeypatch) -> None:
    raiz = tmp_path / "paquete_prueba_modulos"
    for nombre, codigo in {
        "": "",
        "bueno": """
            from hacienda_gpt.modulos.contrato import ModuloBase
            MODULO = ModuloBase(clave="bueno", nombre="Bueno", descripcion="Bien.", instrucciones="Bien.")
        """,
        "roto": "raise RuntimeError('no arranca')",
        "vacio": "X = 1",
    }.items():
        carpeta = raiz / nombre if nombre else raiz
        carpeta.mkdir(parents=True, exist_ok=True)
        (carpeta / "__init__.py").write_text(textwrap.dedent(codigo), encoding="utf-8")
    (raiz / "suelto.py").write_text("MODULO = None\n", encoding="utf-8")  # not a package: ignored
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        encontrados, errores = descubrir("paquete_prueba_modulos")
    finally:
        for nombre in [n for n in sys.modules if n.startswith("paquete_prueba_modulos")]:
            del sys.modules[nombre]
    assert [nombre for nombre, _m in encontrados] == ["bueno"]
    assert "no se pudo importar" in errores["roto"] and errores["vacio"] == "el paquete no expone MODULO"
    reg = construir_registro(encontrados, errores=errores)
    assert list(reg.modulos) == ["bueno"] and set(reg.errores) == {"roto", "vacio"}


def test_the_real_registry_always_has_the_tax_module() -> None:
    reg = registro_mod.registro()
    assert next(iter(reg)).clave == "fiscal"
    assert registro_mod.registro() is reg  # discovered once


def test_operator_switch(monkeypatch) -> None:
    llamadas = Llamadas()
    reg = construir_registro([("fiscal", FISCAL), ("agua", agua(llamadas)), ("seguros", seguros(llamadas))])
    monkeypatch.setattr(settings, "MODULOS_DESACTIVADOS", "seguros, fiscal")
    # The tax module cannot be switched off: the advisor's own tools are the tax tools.
    assert [m.clave for m in reg.activos()] == ["fiscal", "agua"]


def test_tool_spec_must_be_an_object_schema() -> None:
    from hacienda_gpt.modulos.contrato import HerramientaModulo, ResultadoHerramienta

    mala = HerramientaModulo(
        spec=spec("agua_x").__class__(name="agua_x", description="X.", input_schema={"type": "array"}),
        ejecutar=lambda a, c: ResultadoHerramienta(contenido="", resumen=""),
    )
    assert any("input_schema" in p for p in problemas_de(_modulo(herramientas=(mala,)), paquete="agua"))
