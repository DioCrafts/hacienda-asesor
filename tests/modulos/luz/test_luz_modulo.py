"""The ``luz`` module through its contract: REE adapter, comparison, cheap hours, power, notices and an
end-to-end run of every tool over a realistic household profile."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from typing import Any

import pytest

from hacienda_gpt.modulos.contrato import ContextoModulo, Modulo
from hacienda_gpt.modulos.luz import MODULO, esios, herramientas
from hacienda_gpt.modulos.luz.analisis import comparar, horas_baratas
from hacienda_gpt.modulos.luz.consumo import compactar, importar_csv
from hacienda_gpt.modulos.luz.factura import Contrato
from hacienda_gpt.modulos.luz.modulo import validar_cups, validar_potencia
from hacienda_gpt.modulos.luz.ofertas import Oferta

D = Decimal

HOY = date(2026, 11, 13)
ACTUAL = {
    "nombre": "Mi tarifa 3 periodos",
    "comercializadora": "Comercializadora X",
    "energia_eur_kwh": {"p1": "0.198", "p2": "0.142", "p3": "0.105"},
    "potencia_eur_kw_dia": {"p1": "0.098", "p2": "0.030"},
    "permanencia_hasta": "2026-12-01",
    "penalizacion_eur": "20",
}
OFERTA_BARATA = {
    "nombre": "Oferta Ahorro Fija",
    "energia_eur_kwh": "0.118",
    "potencia_eur_kw_dia": {"p1": "0.085", "p2": "0.012"},
}
OFERTA_CARA = {"nombre": "Oferta Cara", "energia_eur_kwh": "0.21", "potencia_eur_kw_dia": {"p1": "0.11", "p2": "0.05"}}


@pytest.fixture(autouse=True)
def _sin_red(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(esios.TOKEN_ENV, raising=False)
    monkeypatch.setitem(herramientas._FETCH, "fetch", None)


@pytest.fixture(scope="module")
def consumo_json(csv_anio: str) -> dict[str, Any]:
    return compactar(importar_csv(csv_anio).lecturas, importado_el=HOY).a_json()


def _perfil(consumo: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    perfil: dict[str, Any] = {
        "comunidad_autonoma": "Madrid",
        "estado_civil": "casado",
        "num_descendientes": 1,
        "luz_cups": "ES0031405912345678JY0F",
        "luz_potencia_p1_kw": 4.6,
        "luz_potencia_p2_kw": 4.6,
        "luz_tarifa_actual": ACTUAL,
        "luz_ofertas": [OFERTA_BARATA, OFERTA_CARA],
    }
    if consumo is not None:
        perfil["luz_consumo"] = consumo
    perfil.update(extra)
    return perfil


def _tool(nombre: str):  # type: ignore[no-untyped-def]
    return next(h for h in MODULO.herramientas if h.spec.name == nombre)


# --- contract ------------------------------------------------------------------------------------------


def test_module_satisfies_the_contract_and_the_registry() -> None:
    from hacienda_gpt.modulos.registro import registro

    assert isinstance(MODULO, Modulo)
    assert MODULO.clave == "luz"
    assert {h.spec.name for h in MODULO.herramientas} == {
        "luz_calcular_factura",
        "luz_comparar_tarifas",
        "luz_importar_consumo",
        "luz_horas_baratas",
        "luz_potencia_optima",
    }
    assert all(h.nombre.startswith("luz_") for h in MODULO.hechos)
    sensibles = {h.nombre for h in MODULO.hechos if h.sensible}
    assert {"luz_cups", "luz_consumo", "luz_tarifa_actual"} <= sensibles
    assert "luz" in registro()
    assert any(f.identificador == "BOE-A-2025-26348" for f in MODULO.fuentes)


def test_fact_validators() -> None:
    assert validar_cups("es 0031 4059 1234 5678 jy 0f") == "ES0031405912345678JY0F"
    with pytest.raises(ValueError, match="CUPS"):
        validar_cups("ES123")
    assert validar_potencia("4,6") == 4.6
    with pytest.raises(ValueError, match="15 kW"):
        validar_potencia(16)


# --- REE adapter --------------------------------------------------------------------------------------


def test_esios_adapter_builds_the_documented_request_and_reads_quarter_hours(sint) -> None:  # type: ignore[no-untyped-def]
    llamadas = []

    def fetch(url: str, cabeceras: dict[str, str]) -> bytes:
        llamadas.append((url, cabeceras))
        return json.dumps(sint.respuesta_esios(date(2026, 11, 14), date(2026, 11, 14), cuartos=True)).encode()

    avisos: list[str] = []
    precios = esios.obtener_precios(date(2026, 11, 14), date(2026, 11, 14), fetch=fetch, token="t0k3n", avisos=avisos)
    url, cabeceras = llamadas[0]
    assert url.startswith("https://api.esios.ree.es/indicators/1001?") and "geo_ids%5B%5D=8741" in url
    assert cabeceras["x-api-key"] == "t0k3n" and "esios-api-v1" in cabeceras["Accept"]
    assert len(precios) == 24
    # Four quarter-hour values (p-1,5 … p+1,5) average to the hourly price.
    assert precios[(date(2026, 11, 14), 20)] == sint.precio_pvpc(date(2026, 11, 14), 20) / 1000
    assert any("cuartos de hora" in a for a in avisos)


def test_esios_without_token_or_with_bad_data_explains_itself() -> None:
    with pytest.raises(esios.PreciosNoDisponiblesError, match="LUZ_ESIOS_TOKEN"):
        esios.obtener_precios(date(2026, 11, 14), date(2026, 11, 14))
    with pytest.raises(esios.PreciosNoDisponiblesError, match="1001"):
        esios.interpretar({"indicator": {"id": 600, "values": []}})
    with pytest.raises(esios.PreciosNoDisponiblesError, match="formato"):
        esios.interpretar("{}")

    def caida(url: str, cabeceras: dict[str, str]) -> bytes:
        raise OSError("sin conexión")

    with pytest.raises(esios.PreciosNoDisponiblesError, match="sin conexión"):
        esios.obtener_precios(date(2026, 11, 14), date(2026, 11, 14), fetch=caida, token="x")


# --- analyses -----------------------------------------------------------------------------------------


def test_comparison_ranks_offers_on_the_same_year(consumo_json, sint) -> None:  # type: ignore[no-untyped-def]
    from hacienda_gpt.modulos.luz.consumo import ConsumoGuardado

    consumo = ConsumoGuardado.model_validate(consumo_json)
    ofertas = [
        Oferta.model_validate(OFERTA_BARATA),
        Oferta.model_validate(OFERTA_CARA),
        Oferta(nombre="PVPC", tipo="pvpc"),
    ]

    def proveedor(a: date, b: date) -> dict[tuple[date, int], D]:
        return esios.interpretar(json.dumps(sint.respuesta_esios(a, b)))

    r = comparar(
        Contrato(D("4.6"), D("4.6")), consumo, ofertas, Oferta.model_validate(ACTUAL), proveedor_precios=proveedor
    )
    assert (r.desde, r.hasta, r.dias) == (date(2025, 11, 1), date(2026, 10, 31), 365)
    nombres = [c.oferta.nombre for c in r.costes]
    assert nombres[0] == "Oferta Ahorro Fija" and nombres[-1] == "Oferta Cara"
    assert set(nombres) == {"Oferta Ahorro Fija", "Oferta Cara", "PVPC", "Mi tarifa 3 periodos"}
    actual = r.actual()
    assert actual is not None and len(actual.detalle_meses) == 12
    assert D(500) < actual.total < D(1000)
    assert any("aproximación" in a for a in r.avisos)


def test_cheapest_hours_and_block(sint) -> None:  # type: ignore[no-untyped-def]
    dia = date(2026, 11, 14)
    precios = esios.interpretar(json.dumps(sint.respuesta_esios(dia, dia)))
    r = horas_baratas(dia, precios, "peninsula", 3, 3)
    assert [h for h, _ in r.baratas] == [12, 13, 14]  # the solar valley of the synthetic curve
    assert r.bloque_inicio == 12
    assert [h for h, _ in r.caras] == [18, 19, 20]


# --- notices ------------------------------------------------------------------------------------------


def test_notice_when_an_offer_saves_more_than_the_threshold_with_stable_keys(consumo_json) -> None:  # type: ignore[no-untyped-def]
    ctx = ContextoModulo(perfil=_perfil(consumo_json), hoy=HOY)
    avisos = MODULO.avisos(ctx)
    assert avisos == MODULO.avisos(ctx)  # idempotent
    ahorro = next(a for a in avisos if a.clave.startswith("luz:ahorro:"))
    assert ahorro.clave == "luz:ahorro:oferta-ahorro-fija:2026-10-31"
    assert ahorro.sustituye_prefijo == "luz:ahorro:"
    assert ahorro.importe is not None and ahorro.importe > 50
    assert "permanencia hasta el 01-12-2026" in ahorro.cuerpo
    fin = next(a for a in avisos if a.clave.startswith("luz:fin_permanencia"))
    assert fin.clave == "luz:fin_permanencia:2026-12-01" and fin.vence == date(2026, 12, 1)


def test_no_saving_notice_below_the_threshold(consumo_json, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("LUZ_UMBRAL_AHORRO_EUR", "100000")
    avisos = MODULO.avisos(ContextoModulo(perfil=_perfil(consumo_json), hoy=HOY))
    assert not any(a.clave.startswith("luz:ahorro:") for a in avisos)
    assert MODULO.avisos(ContextoModulo(perfil={}, hoy=HOY)) == []


def test_missing_regulated_data_for_a_new_year_is_announced(consumo_json) -> None:  # type: ignore[no-untyped-def]
    avisos = MODULO.avisos(ContextoModulo(perfil=_perfil(consumo_json), hoy=date(2027, 1, 10)))
    assert any(a.clave == "luz:datos_regulados:2027" for a in avisos)


# --- end to end ---------------------------------------------------------------------------------------


def test_end_to_end_every_tool_over_a_realistic_profile(csv_anio, fetch_esios, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    guardado: dict[str, Any] = {}
    hechos = {h.nombre: h for h in MODULO.hechos}

    def registrar(valores: dict[str, Any]) -> None:
        for nombre, valor in valores.items():
            validar = hechos[nombre].validar
            guardado[nombre] = validar(valor) if validar else valor

    perfil = _perfil()
    perfil.pop("luz_cups")
    ctx = ContextoModulo(perfil=perfil, hoy=HOY, case_id="caso-1", registrar=registrar)

    r = _tool("luz_importar_consumo").ejecutar({"csv": csv_anio}, ctx)
    assert not r.es_error, r.contenido
    assert "9.505 horas" in r.contenido and "Guardado" in r.contenido
    assert guardado["luz_cups"] == "ES0031405912345678JY0F"
    ctx = ContextoModulo(perfil={**perfil, **guardado}, hoy=HOY, case_id="caso-1", registrar=registrar)

    # Bill of the stored consumption, compared with the "real" total (here the module's own result).
    args = {"fecha_inicio": "2026-09-15", "fecha_fin": "2026-10-14"}
    r = _tool("luz_calcular_factura").ejecutar(args, ctx)
    assert not r.es_error, r.contenido
    total = r.resumen.split(": ")[1].removesuffix(" €")
    r2 = _tool("luz_calcular_factura").ejecutar(
        {**args, "total_factura_real": total.replace(".", "").replace(",", ".")}, ctx
    )
    assert "Coincide al céntimo" in r2.contenido
    r3 = _tool("luz_calcular_factura").ejecutar({**args, "total_factura_real": 1}, ctx)
    assert "diferencia" in r3.contenido
    assert "BOE-A-2026-13759" in " ".join(r.citas) and "BOE-A-1992-28741" in " ".join(r.citas)

    # Manual totals of a bill, without hourly data.
    r = _tool("luz_calcular_factura").ejecutar(
        {**args, "consumo_kwh": {"p1": 80, "p2": 70, "p3": 120}, "oferta": OFERTA_BARATA}, ctx
    )
    assert not r.es_error and "TOTAL" in r.contenido

    # PVPC without token: a readable error, never an exception.
    r = _tool("luz_calcular_factura").ejecutar({**args, "oferta": {"nombre": "PVPC", "tipo": "pvpc"}}, ctx)
    assert r.es_error and "LUZ_ESIOS_TOKEN" in r.contenido

    # With the REE adapter wired to the fake endpoint, PVPC of the last stored hourly days works.
    monkeypatch.setenv(esios.TOKEN_ENV, "token-de-prueba")
    monkeypatch.setitem(herramientas._FETCH, "fetch", fetch_esios)
    r = _tool("luz_calcular_factura").ejecutar(
        {"fecha_inicio": "2026-10-01", "fecha_fin": "2026-10-31", "oferta": {"nombre": "PVPC", "tipo": "pvpc"}}, ctx
    )
    assert not r.es_error, r.contenido
    assert "Energía consumida PVPC" in r.contenido
    assert all("token-de-prueba" == c[1]["x-api-key"] for c in fetch_esios.llamadas)

    r = _tool("luz_comparar_tarifas").ejecutar({}, ctx)
    assert not r.es_error, r.contenido
    assert "1. Oferta Ahorro Fija" in r.contenido and "PVPC" in r.contenido and "tu tarifa actual" in r.contenido

    r = _tool("luz_horas_baratas").ejecutar({"consultar_pvpc": True}, ctx)
    assert not r.es_error, r.contenido
    assert "14-11-2026" in r.contenido and "fin de semana" in r.contenido
    assert "Horas más baratas" in r.contenido

    r = _tool("luz_potencia_optima").ejecutar({}, ctx)
    assert not r.es_error and "mínimo orientativo" in r.contenido
    r = _tool("luz_potencia_optima").ejecutar(
        {"maximos": [{"mes": "2026-01", "p1_kw": 3.1, "p2_kw": 2.0}, {"mes": "2026-07", "p1_kw": 3.4, "p2_kw": 2.2}]},
        ctx,
    )
    assert not r.es_error
    assert "P1 3,8 kW, P2 2,5 kW" in r.contenido and "ahorrarías" in r.contenido

    # Notices on the profile the tools built.
    assert any(a.clave.startswith("luz:ahorro:") for a in MODULO.avisos(ctx))


def test_tools_return_spanish_errors_instead_of_raising() -> None:
    ctx = ContextoModulo(perfil={}, hoy=HOY)
    for nombre, args in (
        ("luz_calcular_factura", {"fecha_inicio": "2026-10-01"}),
        ("luz_calcular_factura", {"fecha_inicio": "ayer", "fecha_fin": "hoy"}),
        ("luz_calcular_factura", {"fecha_inicio": "2026-10-01", "fecha_fin": "2026-10-31", "potencia_p1_kw": 4}),
        ("luz_comparar_tarifas", {}),
        ("luz_importar_consumo", {"csv": "hola"}),
        ("luz_importar_consumo", {}),
        ("luz_potencia_optima", {"potencia_p1_kw": 4}),
    ):
        r = _tool(nombre).ejecutar(args, ctx)
        assert r.es_error and r.contenido, (nombre, args)
    r = _tool("luz_horas_baratas").ejecutar({"fecha": "2026-11-17"}, ctx)
    assert not r.es_error and "LUZ_ESIOS_TOKEN" in r.contenido and "Valle (P3)" in r.contenido
