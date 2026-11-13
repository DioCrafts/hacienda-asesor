"""Deterministic detection of recurring charges in a bank statement.

Steps, per merchant (normalised key, and PayPal apart so a service paid twice is visible):

1. charges of the same amount within three days of each other are set aside as possible double charges;
2. series of identical amounts are tested for a periodicity (weekly, monthly, bimonthly, quarterly,
   half-yearly, yearly) by the median interval, allowing up to two missed periods per gap;
3. the remaining charges are clustered by amount (25 % tolerance) and tested again; a series with
   variable amounts is accepted only for direct debits or utilities/telecom/insurance/loans;
4. consecutive series of the same merchant and periodicity are joined (a price change between them) and
   loose charges that continue a series are attached;
5. a small charge shortly before the first regular one is a trial converted to paid;
6. a single charge of a known subscription merchant in a statement of ten months or more is reported as a
   possible yearly charge, with low confidence.

Every number used here is a heuristic of the module (not a legal figure) and is stated in
``docs/modulos/suscripciones.md``. No LLM is involved; the optional labeller only names the category of
merchants the catalogue does not know (see ``etiquetador.py``).
"""

from __future__ import annotations

import calendar
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
import hashlib
from statistics import median

from hacienda_gpt.modulos.suscripciones import etiquetador
from hacienda_gpt.modulos.suscripciones.formato import euros
from hacienda_gpt.modulos.suscripciones.modelos import (
    CARGOS_POR_ANO,
    CambioPrecio,
    Duplicado,
    Extracto,
    Movimiento,
    PruebaConvertida,
    ServicioGuardado,
)
from hacienda_gpt.modulos.suscripciones.normalizacion import Identidad, identificar

# periodicity -> (nominal days, tolerance in days, months to add for the next charge)
PERIODOS: dict[str, tuple[float, float, int]] = {
    "semanal": (7.0, 2.0, 0),
    "mensual": (30.44, 5.0, 1),
    "bimestral": (60.9, 8.0, 2),
    "trimestral": (91.3, 10.0, 3),
    "semestral": (182.6, 15.0, 6),
    "anual": (365.25, 20.0, 12),
}
MINIMO_CARGOS = {"semanal": 4, "mensual": 3, "bimestral": 3, "trimestral": 3, "semestral": 2, "anual": 2}
# Charges that make full confidence on the count criterion.
CARGOS_PLENOS = {"semanal": 8, "mensual": 6, "bimestral": 4, "trimestral": 3, "semestral": 2, "anual": 2}
VENTANA_DOBLE_DIAS = 3
TOLERANCIA_GRUPO = Decimal("1.25")
DIAS_PRUEBA = 40
UMBRAL_PRUEBA = Decimal("0.20")
SECTORES_VARIABLES = {"suministros", "telecomunicaciones", "seguros", "financiero"}
CATEGORIAS_DIGITALES = {
    "streaming_video",
    "musica_audio",
    "software_nube",
    "prensa_lectura",
    "videojuegos",
    "comercio_online",
}
CENTIMO = Decimal("0.01")


@dataclass(frozen=True)
class Cargo:
    fecha: date
    importe: Decimal  # positive
    movimiento: Movimiento


@dataclass
class _Serie:
    identidad: Identidad
    paypal: bool
    periodicidad: str
    cargos: list[Cargo]
    regularidad: float
    variable: bool = False
    duplicados: list[Cargo] = field(default_factory=list)
    prueba: Cargo | None = None
    unica: bool = False  # a single charge, reported as a possible yearly one


@dataclass(frozen=True)
class ServicioDetectado:
    """A detected service: what is stored plus the details shown once to the user."""

    guardado: ServicioGuardado
    cambios: tuple[CambioPrecio, ...] = ()
    duplicados: tuple[Duplicado, ...] = ()
    observaciones: tuple[str, ...] = ()


@dataclass(frozen=True)
class Analisis:
    servicios: tuple[ServicioDetectado, ...]
    desde: date | None
    hasta: date | None
    num_movimientos: int
    num_cargos: int


# --- Periodicity ----------------------------------------------------------------------------------


def ajustar_periodo(fechas: Sequence[date]) -> tuple[str, float] | None:
    """(periodicity, regularity 0..1) of a sorted list of dates, or None if not periodic."""
    intervalos = [(b - a).days for a, b in zip(fechas, fechas[1:], strict=False)]
    if not intervalos:
        return None
    mediana = median(intervalos)
    for nombre, (dias, tolerancia, _) in PERIODOS.items():
        if abs(mediana - dias) > tolerancia:
            continue
        regulares = sum(abs(i - dias) <= tolerancia for i in intervalos)
        huecos = sum(
            abs(i - dias) > tolerancia and any(abs(i - k * dias) <= tolerancia * k for k in (2, 3)) for i in intervalos
        )
        encajan = (regulares + huecos) / len(intervalos)
        if encajan < 0.75 or regulares / len(intervalos) < 0.5:
            return None
        return nombre, (regulares + 0.5 * huecos) / len(intervalos)
    return None


def sumar_meses(base: date, meses: int, dia: int | None = None) -> date:
    total = base.month - 1 + meses
    ano, mes = base.year + total // 12, total % 12 + 1
    ultimo = calendar.monthrange(ano, mes)[1]
    return date(ano, mes, min(dia or base.day, ultimo))


def proxima_fecha(periodicidad: str, fechas: Sequence[date]) -> date:
    ultima = fechas[-1]
    _, _, meses = PERIODOS[periodicidad]
    if meses == 0:
        return ultima + timedelta(days=7)
    dias = Counter(f.day for f in fechas[-3:])
    dia = max(dias, key=lambda d: (dias[d], d == ultima.day))
    return sumar_meses(ultima, meses, dia)


def _variable(importes: Sequence[Decimal]) -> bool:
    minimo, maximo = min(importes), max(importes)
    return maximo > minimo * Decimal("1.10")


# --- Grouping and series --------------------------------------------------------------------------


def _admite_variable(identidad: Identidad, cargos: Sequence[Cargo]) -> bool:
    adeudos = sum(c.movimiento.medio in {"adeudo", "prestamo"} for c in cargos)
    return identidad.sector in SECTORES_VARIABLES or adeudos * 2 > len(cargos)


def _serie_de(identidad: Identidad, paypal: bool, cargos: list[Cargo], conocido: bool) -> _Serie | None:
    cargos = sorted(cargos, key=lambda c: c.fecha)
    ajuste = ajustar_periodo([c.fecha for c in cargos])
    if ajuste is None:
        return None
    periodicidad, regularidad = ajuste
    minimo = MINIMO_CARGOS[periodicidad]
    if len(cargos) < minimo and not (conocido and len(cargos) >= 2 and periodicidad != "semanal"):
        return None
    variable = _variable([c.importe for c in cargos])
    if variable and not _admite_variable(identidad, cargos):
        return None
    return _Serie(identidad, paypal, periodicidad, cargos, regularidad, variable)


def _clusters(cargos: list[Cargo]) -> list[list[Cargo]]:
    ordenados = sorted(cargos, key=lambda c: c.importe)
    grupos: list[list[Cargo]] = []
    for cargo in ordenados:
        if grupos and cargo.importe <= grupos[-1][-1].importe * TOLERANCIA_GRUPO + Decimal("0.50"):
            grupos[-1].append(cargo)
        else:
            grupos.append([cargo])
    return grupos


def _cerca(a: date, b: date, periodicidad: str) -> bool:
    dias, tolerancia, _ = PERIODOS[periodicidad]
    return abs((b - a).days - dias) <= tolerancia


def _series_del_grupo(
    identidad: Identidad, paypal: bool, cargos: list[Cargo], span_dias: int, hasta: date
) -> list[_Serie]:
    cargos = sorted(cargos, key=lambda c: (c.fecha, c.importe))
    conocido = identidad.en_catalogo and identidad.recurrente
    # 1. same amount within a few days: possible double charges
    dobles: list[Cargo] = []
    pool: list[Cargo] = []
    for cargo in cargos:
        if any(
            abs(p.importe - cargo.importe) <= CENTIMO and 0 <= (cargo.fecha - p.fecha).days <= VENTANA_DOBLE_DIAS
            for p in pool
        ):
            dobles.append(cargo)
        else:
            pool.append(cargo)
    series: list[_Serie] = []
    # 2. series of identical amounts
    por_importe: dict[Decimal, list[Cargo]] = {}
    for cargo in pool:
        por_importe.setdefault(cargo.importe, []).append(cargo)
    for grupo in por_importe.values():
        if len(grupo) >= 2 and (serie := _serie_de(identidad, paypal, grupo, conocido)):
            series.append(serie)
    usados = {id(c) for s in series for c in s.cargos}
    pool = [c for c in pool if id(c) not in usados]
    # 3. amount clusters (variable or slightly varying amounts)
    for grupo in _clusters(pool):
        if len(grupo) >= 2 and (serie := _serie_de(identidad, paypal, grupo, conocido)):
            series.append(serie)
    usados = {id(c) for s in series for c in s.cargos}
    pool = [c for c in pool if id(c) not in usados]
    # 4. join consecutive series of the same periodicity (price change) and attach loose charges
    series = _unir(series)
    cambiado = True
    while cambiado:
        cambiado = False
        for serie in series:
            for cargo in list(pool):
                ultimo, primero = serie.cargos[-1], serie.cargos[0]
                proporcion = cargo.importe / ultimo.importe if ultimo.importe else Decimal(0)
                if not Decimal("0.5") <= proporcion <= Decimal("2"):
                    continue
                if cargo.fecha > ultimo.fecha and _cerca(ultimo.fecha, cargo.fecha, serie.periodicidad):
                    serie.cargos.append(cargo)
                elif cargo.fecha < primero.fecha and _cerca(cargo.fecha, primero.fecha, serie.periodicidad):
                    serie.cargos.insert(0, cargo)
                else:
                    continue
                pool.remove(cargo)
                cambiado = True
    # 5. trial converted to paid
    for serie in series:
        primero = serie.cargos[0]
        for cargo in pool:
            if (
                0 < (primero.fecha - cargo.fecha).days <= DIAS_PRUEBA
                and cargo.importe <= max(Decimal("1.00"), primero.importe * UMBRAL_PRUEBA)
                and serie.prueba is None
            ):
                serie.prueba = cargo
        if serie.prueba is not None:
            pool.remove(serie.prueba)
    # double charges: confirmed only inside a monthly-or-longer series of the same amount
    for cargo in dobles:
        for serie in series:
            dias = PERIODOS[serie.periodicidad][0]
            if (
                serie.periodicidad != "semanal"
                and any(abs(c.importe - cargo.importe) <= CENTIMO for c in serie.cargos)
                and serie.cargos[0].fecha - timedelta(days=dias)
                <= cargo.fecha
                <= serie.cargos[-1].fecha + timedelta(days=dias)
            ):
                serie.duplicados.append(cargo)
                break
    # 6. a single charge of a known subscription merchant over a long statement: possibly yearly
    if not series and conocido and len(cargos) == 1 and span_dias >= 300 and (hasta - cargos[0].fecha).days > 45:
        series.append(_Serie(identidad, paypal, "anual", cargos, 0.0, unica=True))
    return series


def _encaja(a: date, b: date, periodicidad: str) -> bool:
    """``b`` follows ``a`` after one period, or two (one charge missing)."""
    dias, tolerancia, _ = PERIODOS[periodicidad]
    return any(abs((b - a).days - k * dias) <= tolerancia * k for k in (1, 2))


def _unir(series: list[_Serie]) -> list[_Serie]:
    series = sorted(series, key=lambda s: s.cargos[0].fecha)
    resultado: list[_Serie] = []
    for serie in series:
        previa = next(
            (
                r
                for r in resultado
                if r.periodicidad == serie.periodicidad
                and r.cargos[-1].fecha < serie.cargos[0].fecha
                and _encaja(r.cargos[-1].fecha, serie.cargos[0].fecha, serie.periodicidad)
                and not r.variable
                and not serie.variable
            ),
            None,
        )
        if previa is None:
            resultado.append(serie)
            continue
        previa.cargos.extend(serie.cargos)
        previa.regularidad = min(previa.regularidad, serie.regularidad)
    return resultado


# --- From series to services ----------------------------------------------------------------------


def _redondear(valor: Decimal) -> Decimal:
    return valor.quantize(CENTIMO, rounding=ROUND_HALF_UP)


def cambios_de_precio(cargos: Sequence[Cargo]) -> tuple[list[CambioPrecio], int]:
    """Persistent price changes and the number of one-off odd amounts."""
    cambios: list[CambioPrecio] = []
    atipicos = 0
    nivel = cargos[0].importe
    for i, cargo in enumerate(cargos[1:], start=1):
        if abs(cargo.importe - nivel) <= max(CENTIMO, nivel * Decimal("0.005")):
            continue
        siguiente = cargos[i + 1].importe if i + 1 < len(cargos) else None
        if siguiente is None or abs(siguiente - cargo.importe) <= max(CENTIMO, cargo.importe * Decimal("0.005")):
            porcentaje = _redondear((cargo.importe - nivel) / nivel * 100) if nivel else Decimal(0)
            cambios.append(CambioPrecio(fecha=cargo.fecha, anterior=nivel, nuevo=cargo.importe, porcentaje=porcentaje))
            nivel = cargo.importe
        else:
            atipicos += 1
    return cambios, atipicos


def _confianza(serie: _Serie) -> Decimal:
    if serie.unica:
        return Decimal("0.30")
    cuenta = min(1.0, len(serie.cargos) / CARGOS_PLENOS[serie.periodicidad])
    estabilidad = 0.7 if serie.variable else 1.0
    valor = 0.5 * serie.regularidad + 0.3 * cuenta + 0.2 * estabilidad
    if serie.identidad.en_catalogo:
        valor += 0.05
    if any(c.movimiento.medio == "adeudo" for c in serie.cargos):
        valor += 0.05
    return _redondear(Decimal(str(min(1.0, valor))))


def nivel_confianza(confianza: Decimal) -> str:
    return "alta" if confianza >= Decimal("0.80") else "media" if confianza >= Decimal("0.60") else "baja"


def _identificador(clave: str, paypal: bool, periodicidad: str, ordinal: int) -> str:
    base = f"{clave}|{'paypal' if paypal else ''}|{periodicidad}|{ordinal}"
    return "srv-" + hashlib.sha256(base.encode("utf-8")).hexdigest()[:10]


def _medio(serie: _Serie) -> str:
    medios = Counter(c.movimiento.medio for c in serie.cargos)
    return medios.most_common(1)[0][0]


def analizar(extracto: Extracto, *, usar_etiquetador: bool = True) -> Analisis:
    """Detect the recurring services of a parsed statement."""
    movimientos = extracto.movimientos
    desde, hasta = extracto.desde, extracto.hasta
    if not movimientos or desde is None or hasta is None:
        return Analisis((), desde, hasta, 0, 0)
    span = (hasta - desde).days
    grupos: dict[tuple[str, bool], tuple[Identidad, list[Cargo]]] = {}
    num_cargos = 0
    for movimiento in movimientos:
        if movimiento.importe >= 0 or movimiento.medio == "efectivo":
            continue
        num_cargos += 1
        identidad = identificar(movimiento)
        if not identidad.recurrente:
            continue
        paypal = identidad.canal == "paypal"
        grupos.setdefault((identidad.clave, paypal), (identidad, []))[1].append(
            Cargo(movimiento.fecha, (-movimiento.importe).quantize(CENTIMO), movimiento)
        )
    series: list[_Serie] = []
    for (_, paypal), (identidad, cargos) in sorted(grupos.items()):
        series.extend(_series_del_grupo(identidad, paypal, cargos, span, hasta))

    detectados: list[ServicioDetectado] = []
    ordinales: Counter[tuple[str, bool, str]] = Counter()
    for serie in sorted(series, key=lambda s: (s.identidad.clave, s.paypal, s.periodicidad, s.cargos[0].importe)):
        clave = (serie.identidad.clave, serie.paypal, serie.periodicidad)
        identificador = _identificador(*clave, ordinales[clave])
        ordinales[clave] += 1
        detectados.append(_a_servicio(serie, identificador, hasta, usar_etiquetador))
    detectados = _marcar_servicios_duplicados(detectados)
    detectados.sort(key=lambda d: (d.guardado.estado != "activa", -d.guardado.coste_anual, d.guardado.etiqueta))
    return Analisis(tuple(detectados), desde, hasta, len(movimientos), num_cargos)


def _a_servicio(serie: _Serie, identificador: str, hasta: date, usar_etiquetador: bool) -> ServicioDetectado:
    cargos = serie.cargos
    fechas = [c.fecha for c in cargos]
    dias, tolerancia, _ = PERIODOS[serie.periodicidad]
    activa = (hasta - fechas[-1]).days <= dias * 1.5 + tolerancia
    cambios, atipicos = ([], 0) if serie.variable else cambios_de_precio(cargos)
    if not serie.variable and len(cambios) + atipicos > max(2, len(cargos) * 0.3):
        serie.variable, cambios = True, []
    importe = (
        _redondear(sum((c.importe for c in cargos), Decimal(0)) / len(cargos)) if serie.variable else cargos[-1].importe
    )
    identidad = serie.identidad
    categoria, origen = identidad.categoria, "catalogo"
    if not identidad.en_catalogo:
        origen = "extracto" if categoria != "otros" else "sin_clasificar"
        if origen == "sin_clasificar" and usar_etiquetador and (sugerida := etiquetador.etiquetar(identidad.clave)):
            categoria, origen = sugerida, "llm"
    duplicados = [
        Duplicado(
            tipo="cargo_doble",
            fecha=c.fecha,
            importe=c.importe,
            detalle=f"dos cargos de {euros(c.importe)} en {VENTANA_DOBLE_DIAS} días o menos",
        )
        for c in serie.duplicados
    ]
    observaciones: list[str] = []
    if serie.unica:
        observaciones.append(
            "Un solo cargo en el extracto: podría ser anual. Confirma la fecha de renovación en tu contrato."
        )
    if atipicos and not serie.variable:
        observaciones.append(f"{atipicos} cargo(s) con un importe distinto que no se repitió.")
    if serie.variable:
        observaciones.append("Importe variable (según consumo): se usa la media para el gasto anual.")
    if activa and (hasta - fechas[0]).days <= 45 and not serie.unica:
        observaciones.append("Alta reciente: si estás en un periodo de prueba, comprueba cuándo empieza a cobrarse.")
    if not activa:
        observaciones.append("Sin cargos recientes: puede que ya esté dada de baja.")
    prueba = (
        PruebaConvertida(
            fecha_prueba=serie.prueba.fecha, importe_prueba=serie.prueba.importe, fecha_primer_pago=fechas[0]
        )
        if serie.prueba
        else None
    )
    guardado = ServicioGuardado(
        id=identificador,
        etiqueta=identidad.etiqueta + (" (PayPal)" if serie.paypal else ""),
        categoria=categoria,  # type: ignore[arg-type]
        categoria_origen=origen,  # type: ignore[arg-type]
        sector=identidad.sector,  # type: ignore[arg-type]
        periodicidad=serie.periodicidad,  # type: ignore[arg-type]
        importe=importe,
        importe_variable=serie.variable,
        medio=_medio(serie),  # type: ignore[arg-type]
        primera_fecha=fechas[0],
        ultima_fecha=fechas[-1],
        proxima_fecha=proxima_fecha(serie.periodicidad, fechas) if activa else None,
        estado="activa" if activa else "inactiva",
        confianza=_confianza(serie),
        num_cargos=len(cargos),
        cambio_precio=cambios[-1] if cambios else None,
        duplicado=duplicados[-1] if duplicados else None,
        prueba=prueba,
    )
    return ServicioDetectado(guardado, tuple(cambios), tuple(duplicados), tuple(observaciones))


def _marcar_servicios_duplicados(detectados: list[ServicioDetectado]) -> list[ServicioDetectado]:
    """Two active series of the same merchant with similar amounts and overlapping dates."""
    resultado = list(detectados)
    for i, a in enumerate(detectados):
        for j, b in enumerate(detectados):
            ga, gb = a.guardado, b.guardado
            if j <= i or ga.estado != "activa" or gb.estado != "activa":
                continue
            if _clave_etiqueta(ga.etiqueta) != _clave_etiqueta(gb.etiqueta) or ga.periodicidad != gb.periodicidad:
                continue
            mayor, menor = max(ga.importe, gb.importe), min(ga.importe, gb.importe)
            if mayor > menor * TOLERANCIA_GRUPO:
                continue
            inicio = max(ga.primera_fecha, gb.primera_fecha)
            if inicio > min(ga.ultima_fecha, gb.ultima_fecha):
                continue
            for indice, propio, otro in ((i, ga, gb), (j, gb, ga)):
                duplicado = Duplicado(
                    tipo="servicio_duplicado",
                    fecha=inicio,
                    importe=propio.importe,
                    detalle=f"también se cobra «{otro.etiqueta}» ({euros(otro.importe)}, {otro.periodicidad})",
                )
                actual = resultado[indice]
                resultado[indice] = ServicioDetectado(
                    actual.guardado.model_copy(update={"duplicado": duplicado}),
                    actual.cambios,
                    actual.duplicados + (duplicado,),
                    actual.observaciones,
                )
    return resultado


def _clave_etiqueta(etiqueta: str) -> str:
    return etiqueta.removesuffix(" (PayPal)")


def coste_anual(servicio: ServicioGuardado) -> Decimal:
    return _redondear(servicio.importe * CARGOS_POR_ANO[servicio.periodicidad])


def posible_olvido(servicio: ServicioGuardado) -> bool:
    """A small, steady digital subscription paid for months: worth checking whether it is still used."""
    mensual = coste_anual(servicio) / 12
    return (
        servicio.estado == "activa"
        and servicio.categoria in CATEGORIAS_DIGITALES
        and servicio.num_cargos >= 6
        and mensual <= Decimal("20")
        and servicio.cambio_precio is None
    )
