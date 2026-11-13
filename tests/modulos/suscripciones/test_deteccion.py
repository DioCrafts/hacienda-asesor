"""Recurrence detection over the synthetic year in every format, and its building blocks."""

from __future__ import annotations

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

import pytest

from hacienda_gpt.modulos.suscripciones import etiquetador
from hacienda_gpt.modulos.suscripciones.deteccion import (
    Analisis,
    ServicioDetectado,
    ajustar_periodo,
    analizar,
    posible_olvido,
    proxima_fecha,
)
from hacienda_gpt.modulos.suscripciones.importadores import importar
from hacienda_gpt.modulos.suscripciones.modelos import Extracto, Movimiento
from hacienda_gpt.modulos.suscripciones.normalizacion import clave_comercio, identificar
from tests.modulos.suscripciones.sintetico import ESPERADOS, FIXTURES
from tests.modulos.suscripciones.test_importadores import FORMATOS

_CACHE: dict[str, Analisis] = {}


def _analisis(nombre: str) -> Analisis:
    if nombre not in _CACHE:
        _CACHE[nombre] = analizar(importar((FIXTURES / nombre).read_bytes()))
    return _CACHE[nombre]


def _uno(analisis: Analisis, etiqueta: str) -> ServicioDetectado:
    encontrados = [s for s in analisis.servicios if s.guardado.etiqueta == etiqueta]
    assert len(encontrados) == 1, etiqueta
    return encontrados[0]


@pytest.mark.parametrize("nombre", sorted(FORMATOS))
def test_detecta_todas_las_suscripciones_y_nada_mas(nombre: str) -> None:
    analisis = _analisis(nombre)
    detectados = Counter((s.guardado.etiqueta, s.guardado.periodicidad, s.guardado.estado) for s in analisis.servicios)
    esperados = Counter((e, p, est) for e, (p, est) in ESPERADOS.items())
    esperados[("Apple (App Store / iCloud)", "mensual", "activa")] = 2
    assert detectados == esperados


@pytest.mark.parametrize("nombre", sorted(FORMATOS))
def test_cambios_duplicados_y_pruebas(nombre: str) -> None:
    analisis = _analisis(nombre)
    netflix = _uno(analisis, "Netflix")
    assert [(c.fecha, c.anterior, c.nuevo, c.porcentaje) for c in netflix.cambios] == [
        (date(2026, 4, 5), Decimal("12.99"), Decimal("13.99"), Decimal("7.70"))
    ]
    assert netflix.guardado.importe == Decimal("13.99")
    assert netflix.guardado.proxima_fecha == date(2026, 11, 5)
    orange = _uno(analisis, "Orange").guardado
    assert orange.cambio_precio is not None and orange.cambio_precio.porcentaje == Decimal("7.78")
    assert orange.sector == "telecomunicaciones"
    mapfre = _uno(analisis, "Mapfre").guardado
    assert mapfre.cambio_precio is not None and mapfre.cambio_precio.porcentaje == Decimal("8.50")
    assert mapfre.proxima_fecha == date(2027, 10, 20) and mapfre.sector == "seguros"
    maxhbo = _uno(analisis, "Max (HBO)")
    assert [(d.tipo, d.fecha) for d in maxhbo.duplicados] == [("cargo_doble", date(2026, 9, 15))]
    assert maxhbo.guardado.num_cargos == 13
    for etiqueta in ("Spotify", "Spotify (PayPal)"):
        duplicado = _uno(analisis, etiqueta).guardado.duplicado
        assert duplicado is not None and duplicado.tipo == "servicio_duplicado"
        assert duplicado.fecha == date(2026, 6, 20)
    apple = [s.guardado for s in analisis.servicios if s.guardado.etiqueta.startswith("Apple")]
    assert all(s.duplicado is None for s in apple)  # 0,99 and 10,99 are two different services
    dazn = _uno(analisis, "DAZN").guardado
    assert dazn.prueba is not None
    assert (dazn.prueba.fecha_prueba, dazn.prueba.importe_prueba) == (date(2026, 6, 3), Decimal("0.99"))
    assert dazn.prueba.fecha_primer_pago == date(2026, 7, 3)
    filmin = _uno(analisis, "Filmin").guardado
    assert filmin.estado == "inactiva" and filmin.proxima_fecha is None
    prime = _uno(analisis, "Amazon Prime").guardado
    assert prime.num_cargos == 1 and prime.confianza == Decimal("0.30")
    assert prime.proxima_fecha == date(2026, 12, 1)
    iberdrola = _uno(analisis, "Iberdrola").guardado
    assert iberdrola.importe_variable and iberdrola.importe == Decimal("65.69")
    kiosko = _uno(analisis, "Kiosko y Más").guardado
    assert kiosko.periodicidad == "semanal" and kiosko.proxima_fecha == date(2026, 11, 7)


def test_ids_estables_entre_formatos() -> None:
    ids = {nombre: sorted(s.guardado.id for s in _analisis(nombre).servicios) for nombre in FORMATOS}
    assert len({tuple(v) for v in ids.values()}) == 1


def test_formatos_con_medio_lo_usan() -> None:
    analisis = _analisis("anual_camt053.xml")
    assert _uno(analisis, "Basic-Fit").guardado.medio == "adeudo"
    assert _uno(analisis, "Netflix").guardado.medio == "tarjeta"
    assert _uno(analisis, "Préstamo").guardado.categoria == "prestamos"


def test_posible_olvido() -> None:
    analisis = _analisis("anual.n43")
    assert posible_olvido(_uno(analisis, "Kiosko y Más").guardado)
    assert not posible_olvido(_uno(analisis, "Netflix").guardado)  # price changed: the user was notified
    assert not posible_olvido(_uno(analisis, "Basic-Fit").guardado)  # not a digital subscription


@pytest.mark.parametrize(
    ("texto", "clave"),
    [
        ("COMPRA TARJ. 5540XXXXXXXX1234 NETFLIX.COM 866-579-7172 NL", "NETFLIX"),
        ("PAYPAL *SPOTIFY 4029357733 LU", "SPOTIFY"),
        ("RECIBO ACME GYM SL REF. 2026/03 MADRID ES", "ACME GYM"),
        ("Pago con Apple Pay en Panadería Lola 12/03/2026", "PANADERIA LOLA"),
        ("TRANSFERENCIA A FAVOR DE INMOBILIARIA SOL SL", "INMOBILIARIA SOL"),
        ("123456", ""),
    ],
)
def test_clave_comercio(texto: str, clave: str) -> None:
    assert clave_comercio(texto) == clave


def test_identificar_catalogo_y_desconocidos() -> None:
    netflix = identificar(Movimiento(date(2026, 1, 5), Decimal("-12.99"), "NETFLIX.COM", medio="tarjeta"))
    assert (netflix.etiqueta, netflix.categoria, netflix.en_catalogo) == ("Netflix", "streaming_video", True)
    super_ = identificar(Movimiento(date(2026, 1, 5), Decimal("-50"), "COMPRA MERCADONA"))
    assert not super_.recurrente
    acreedor = identificar(
        Movimiento(date(2026, 1, 5), Decimal("-5"), "RECIBO 0049", contraparte="CLUB DE AJEDREZ", medio="adeudo")
    )
    assert acreedor.clave == "CLUB AJEDREZ" and acreedor.categoria == "otros"


def test_ajustar_periodo() -> None:
    mensual = [date(2026, m, 5) for m in range(1, 8)]
    assert ajustar_periodo(mensual) == ("mensual", 1.0)
    con_hueco = [date(2026, m, 5) for m in (1, 2, 3, 5, 6, 7)]  # April missing
    nombre, regularidad = ajustar_periodo(con_hueco) or ("", 0.0)
    assert nombre == "mensual" and 0.85 < regularidad < 1
    assert ajustar_periodo([date(2026, 1, 1), date(2026, 1, 20), date(2026, 3, 2)]) is None
    semanal = [date(2026, 1, 3) + timedelta(days=7 * i) for i in range(6)]
    assert ajustar_periodo(semanal) == ("semanal", 1.0)


def test_proxima_fecha_fin_de_mes() -> None:
    assert proxima_fecha("mensual", [date(2026, 11, 30), date(2026, 12, 31), date(2027, 1, 31)]) == date(2027, 2, 28)
    assert proxima_fecha("anual", [date(2025, 2, 28), date(2026, 2, 28)]) == date(2027, 2, 28)


def _extracto(movs: list[Movimiento]) -> Extracto:
    return Extracto(formato="csv", movimientos=tuple(sorted(movs, key=lambda m: m.fecha)))


def test_compras_irregulares_no_son_servicios() -> None:
    movs = [
        Movimiento(date(2026, 1, 1) + timedelta(days=d), Decimal(f"-{10 + d % 17}.50"), "TIENDA DE BARRIO")
        for d in (0, 3, 11, 12, 30, 44, 45, 70, 71, 90)
    ]
    assert analizar(_extracto(movs)).servicios == ()


def test_variable_con_tarjeta_no_se_admite_pero_con_recibo_si() -> None:
    importes = ["40.00", "55.00", "47.00", "60.00", "43.00", "58.00"]
    tarjeta = [
        Movimiento(date(2026, m, 10), Decimal(f"-{i}"), "RESTAURANTE EL PATIO", medio="tarjeta")
        for m, i in zip(range(1, 7), importes, strict=True)
    ]
    assert analizar(_extracto(tarjeta)).servicios == ()
    recibo = [
        Movimiento(date(2026, m, 10), Decimal(f"-{i}"), "RECIBO COOPERATIVA ELECTRICA", medio="adeudo")
        for m, i in zip(range(1, 7), importes, strict=True)
    ]
    (servicio,) = analizar(_extracto(recibo)).servicios
    assert servicio.guardado.importe_variable and servicio.guardado.periodicidad == "mensual"


def test_subida_de_precio_grande_une_las_dos_series() -> None:
    movs = [
        Movimiento(date(2026, m, 3), Decimal("-8.00" if m < 5 else "-12.00"), "RECIBO REVISTA CIENCIA", medio="adeudo")
        for m in range(1, 10)
    ]
    (servicio,) = analizar(_extracto(movs)).servicios
    assert servicio.guardado.cambio_precio is not None
    assert servicio.guardado.cambio_precio.porcentaje == Decimal("50.00")
    assert servicio.guardado.num_cargos == 9


def test_etiquetador_llm_apagado_por_defecto_y_con_puerta(monkeypatch: pytest.MonkeyPatch) -> None:
    movs = [Movimiento(date(2026, m, 1), Decimal("-5.00"), "RECIBO CLUB AJEDREZ", medio="adeudo") for m in range(1, 7)]
    llamadas: list[str] = []

    def falso(clave: str, categorias: tuple[str, ...]) -> str:
        llamadas.append(clave)
        return "cuotas_asociaciones"

    etiquetador.configurar(falso)
    try:
        (sin_flag,) = analizar(_extracto(movs)).servicios
        assert sin_flag.guardado.categoria == "otros" and not llamadas
        monkeypatch.setenv("SUSCRIPCIONES_ETIQUETADOR_LLM", "1")
        (con_flag,) = analizar(_extracto(movs)).servicios
        assert con_flag.guardado.categoria == "cuotas_asociaciones"
        assert con_flag.guardado.categoria_origen == "llm"
        assert llamadas == ["CLUB AJEDREZ"]  # only the normalised key, never amounts or dates
        etiquetador.configurar(lambda clave, categorias: "categoria_inventada")
        assert etiquetador.etiquetar("CLUB AJEDREZ") is None
        assert etiquetador.etiquetar("REF 12345") is None  # digits never leave
        etiquetador.configurar(lambda clave, categorias: (_ for _ in ()).throw(RuntimeError("caído")))
        assert etiquetador.etiquetar("CLUB AJEDREZ") is None
    finally:
        etiquetador.configurar(None)
