"""End to end: the module through its contract, with realistic household profiles."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import json
from pathlib import Path
from typing import Any

import pytest

from hacienda_gpt.modulos.ayudas import MODULO, actualizar_convocatorias
from hacienda_gpt.modulos.contrato import ContextoModulo, Modulo
from .conftest import FIXTURES

HOY = date(2026, 11, 13)

# A single mother with a baby born this year and a child with a recognised disability, low income.
MADRE_SOLA: dict[str, Any] = {
    "comunidad_autonoma": "Andalucía",
    "estado_civil": "divorciado",
    "edad": 34,
    "familia_monoparental": True,
    "descendientes": [
        {"fecha_nacimiento": "2026-02-10"},
        {"fecha_nacimiento": "2018-09-01", "grado_discapacidad": 40},
    ],
    "ayudas_adultos_hogar": 1,
    "ayudas_menores_hogar": 2,
    "ayudas_ingresos_hogar": 11000,
    "ayudas_base_imponible_hogar": 10500,
    "ayudas_patrimonio_neto_hogar": 2000,
    "ayudas_contrato_pvpc": True,
}
# A widowed pensioner living alone.
PENSIONISTA: dict[str, Any] = {
    "comunidad_autonoma": "Castilla y León",
    "estado_civil": "viudo",
    "edad": 72,
    "ayudas_pensionista": True,
    "ayudas_adultos_hogar": 1,
    "ayudas_menores_hogar": 0,
    "ayudas_ingresos_hogar": 13500,
    "ayudas_base_imponible_hogar": 13000,
}


def _ctx(perfil: Mapping[str, Any], hoy: date = HOY) -> ContextoModulo:
    return ContextoModulo(perfil=perfil, hoy=hoy, case_id="caso-1")


def _llenar_almacen() -> None:
    def sumario(dia: date) -> Mapping[str, Any] | None:
        ruta = FIXTURES / "sumarios" / f"{dia:%Y%m%d}.json"
        return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else None

    def xml(boe_id: str) -> bytes | None:
        ruta = FIXTURES / "documentos" / f"{boe_id}.xml"
        return ruta.read_bytes() if ruta.exists() else None

    resumen = actualizar_convocatorias(HOY, sumario_de=sumario, xml_de=xml, dias_max=30)
    assert not resumen.errores


def _herramienta(nombre: str) -> Any:
    return next(h for h in MODULO.herramientas if h.spec.name == nombre)


def test_contract() -> None:
    assert isinstance(MODULO, Modulo)
    assert MODULO.clave == "ayudas"
    assert {h.spec.name for h in MODULO.herramientas} == {"ayudas_comprobar", "ayudas_convocatorias"}
    assert all(h.efecto == "lectura" for h in MODULO.herramientas)
    assert all(h.nombre.startswith("ayudas_") for h in MODULO.hechos)
    sensibles = {h.nombre for h in MODULO.hechos if h.sensible}
    assert {"ayudas_ingresos_hogar", "ayudas_patrimonio_neto_hogar", "ayudas_ya_solicitadas"} <= sensibles
    ids = {f.identificador for f in MODULO.fuentes}
    assert {"BOE-A-2021-21007", "BOE-A-2017-11505", "boe_sumario", "bdns"} <= ids
    for herramienta in MODULO.herramientas:
        assert herramienta.spec.input_schema["type"] == "object"


def test_fact_validators() -> None:
    hechos = {h.nombre: h for h in MODULO.hechos}
    assert hechos["ayudas_adultos_hogar"].validar is not None
    assert hechos["ayudas_adultos_hogar"].validar("2") == 2
    with pytest.raises(ValueError):
        hechos["ayudas_adultos_hogar"].validar(0)
    validar_lista = hechos["ayudas_ya_solicitadas"].validar
    assert validar_lista is not None
    assert validar_lista(["imv", "bono_social_electrico", "imv"]) == ["bono_social_electrico", "imv"]
    assert validar_lista("imv, capi") == ["capi", "imv"]
    with pytest.raises(ValueError, match="desconocidos"):
        validar_lista(["subsidio"])
    validar_importe = hechos["ayudas_ingresos_hogar"].validar
    assert validar_importe is not None
    with pytest.raises(ValueError):
        validar_importe(-5)
    validar_intereses = hechos["ayudas_intereses"].validar
    assert validar_intereses is not None and validar_intereses(["estudios"]) == ["estudios"]


def test_single_mother_finds_every_aid_she_is_entitled_to() -> None:
    res = _herramienta("ayudas_comprobar").ejecutar({}, _ctx(MADRE_SOLA))
    assert not res.es_error
    for nombre in (
        "Ingreso mínimo vital",
        "Complemento de ayuda para la infancia",
        "Asignación por hijo o menor a cargo con discapacidad",
        "Prestación por nacimiento",
        "Bono social eléctrico",
        "Bono social térmico",
        "Título de familia monoparental",
    ):
        assert nombre in res.contenido, nombre
    assert "No comprobado" in res.contenido and "Texto de la norma" in res.contenido
    assert "art. 81 LIRPF" in res.contenido  # cross-reference to the fiscal module (baby under three)
    assert "https://www.boe.es/buscar/act.php?id=BOE-A-2021-21007" in " ".join(res.citas)
    assert res.resumen.startswith("7 ayuda(s) posible(s)")


def test_one_benefit_only_and_errors() -> None:
    herramienta = _herramienta("ayudas_comprobar")
    res = herramienta.ejecutar({"prestacion": "imv"}, _ctx(PENSIONISTA))
    assert "Ingreso mínimo vital: No te corresponde" in res.contenido
    error = herramienta.ejecutar({"prestacion": "subsidio"}, _ctx(PENSIONISTA))
    assert error.es_error and "desconocida" in error.contenido


def test_calls_tool_with_and_without_a_review() -> None:
    herramienta = _herramienta("ayudas_convocatorias")
    sin_revision = herramienta.ejecutar({}, _ctx(PENSIONISTA))
    assert not sin_revision.es_error and "Todavía no se ha revisado el BOE" in sin_revision.contenido
    _llenar_almacen()
    res = herramienta.ejecutar({}, _ctx(PENSIONISTA))
    assert "Programa de Termalismo" in res.contenido and "04/12/2026" in res.contenido
    assert "https://www.boe.es/diario_boe/txt.php?id=BOE-A-2026-23163" in res.citas
    assert "boletines" in res.contenido  # regional bulletins are out of scope, said explicitly
    assert "exploración minera".lower() not in res.contenido.lower()
    joven = herramienta.ejecutar({"incluir_posibles": True}, _ctx({"edad": 30, "comunidad_autonoma": "Madrid"}))
    assert "Termalismo" not in joven.contenido


def test_notices_are_stable_and_idempotent() -> None:
    _llenar_almacen()
    primeros = MODULO.avisos(_ctx(PENSIONISTA))
    segundos = MODULO.avisos(_ctx(PENSIONISTA))
    assert [a.clave for a in primeros] == [a.clave for a in segundos]
    claves = {a.clave for a in primeros}
    assert "ayudas:convocatoria:BOE-A-2026-23163" in claves
    termalismo = next(a for a in primeros if a.clave == "ayudas:convocatoria:BOE-A-2026-23163")
    assert termalismo.vence == date(2026, 12, 4) and termalismo.fuentes
    # 13.000 € of taxable base is above 12.600 €: no social bonus for the pensioner.
    assert "ayudas:prestacion:bono_social_electrico:2026" not in claves

    madre = {a.clave: a for a in MODULO.avisos(_ctx(MADRE_SOLA))}
    assert "ayudas:prestacion:imv:2026" in madre
    assert madre["ayudas:prestacion:bono_social_electrico:2026"].vence == date(2026, 12, 31)
    assert madre["ayudas:prestacion:nacimiento:2026"].importe == 1000
    assert "ayudas:prestacion:titulo_monoparental:2027" in madre  # opens within 60 days
    assert "ayudas:convocatoria:BOE-A-2026-23163" not in madre


def test_no_notice_for_what_is_already_received() -> None:
    perfil = {**MADRE_SOLA, "ayudas_ya_solicitadas": ["imv", "capi", "bono_social_electrico"]}
    claves = {a.clave for a in MODULO.avisos(_ctx(perfil))}
    assert not {"ayudas:prestacion:imv:2026", "ayudas:prestacion:capi:2026"} & claves
    assert "ayudas:prestacion:bono_social_termico:2026" not in claves  # it will arrive by itself


def test_automatic_review_is_opt_in(monkeypatch: pytest.MonkeyPatch, _aislado: Path) -> None:
    llamadas: list[date] = []

    def falso(hoy: date, **_: Any) -> None:
        llamadas.append(hoy)

    monkeypatch.setattr("hacienda_gpt.modulos.ayudas.modulo.actualizar_convocatorias", falso)
    MODULO.avisos(_ctx(PENSIONISTA))
    assert llamadas == []
    monkeypatch.setenv("AYUDAS_BOE_AUTOMATICO", "1")
    MODULO.avisos(_ctx(PENSIONISTA))
    assert llamadas == [HOY]


def test_a_profile_with_nothing_is_not_an_error() -> None:
    for herramienta in MODULO.herramientas:
        res = herramienta.ejecutar({}, _ctx({}))
        assert not res.es_error, res.contenido
    assert MODULO.avisos(_ctx({})) == []
