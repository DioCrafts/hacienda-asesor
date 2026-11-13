"""What-if analyses over the stored consumption: offer comparison, contracted power and cheap hours."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal

from hacienda_gpt.modulos.luz.consumo import ConsumoGuardado
from hacienda_gpt.modulos.luz.datos import DatoNoDisponibleError, cargar_regulados
from hacienda_gpt.modulos.luz.factura import CERO, Contrato, FacturaError, calcular_factura, euros, fmt
from hacienda_gpt.modulos.luz.ofertas import Oferta
from hacienda_gpt.modulos.luz.periodos import Zona, es_dia_valle, hora_local, periodo_energia

# date range -> hourly prices; raises PreciosNoDisponiblesError when they cannot be obtained
ProveedorPrecios = Callable[[date, date], dict[tuple[date, int], Decimal]]


@dataclass
class CosteOferta:
    oferta: Oferta
    total: Decimal
    dias: int
    anual: Decimal
    es_actual: bool = False
    detalle_meses: list[tuple[str, Decimal]] = field(default_factory=list)


@dataclass
class Comparacion:
    desde: date
    hasta: date
    dias: int
    costes: list[CosteOferta]
    descartadas: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    def actual(self) -> CosteOferta | None:
        return next((c for c in self.costes if c.es_actual), None)

    def mejor(self) -> CosteOferta | None:
        return self.costes[0] if self.costes else None


def _segmentos(desde: date, hasta: date) -> list[tuple[date, date]]:
    """Calendar months between two dates (bills of the comparison are simulated month by month)."""
    salida = []
    inicio = desde
    while inicio <= hasta:
        siguiente = date(inicio.year + (inicio.month == 12), inicio.month % 12 + 1, 1)
        fin = min(hasta, siguiente - timedelta(days=1))
        salida.append((inicio, fin))
        inicio = fin + timedelta(days=1)
    return salida


def estimar_energia_pvpc(
    consumo: ConsumoGuardado, desde: date, hasta: date, precios: dict[tuple[date, int], Decimal], zona: Zona
) -> dict[str, Decimal]:
    """€ of PVPC energy per month: exact where hourly readings are stored, else the month's hour-of-day
    consumption profile × the month's average price of each hour of the day."""
    salida: dict[str, Decimal] = {}
    for inicio, fin in _segmentos(desde, hasta):
        mes = f"{inicio:%Y-%m}"
        lecturas = consumo.lecturas_horarias(inicio, fin)
        if lecturas is not None and all((x.fecha, x.indice) in precios for x in lecturas):
            salida[mes] = sum((x.kwh * precios[(x.fecha, x.indice)] for x in lecturas), CERO)
            continue
        resumen = consumo.meses.get(mes)
        if resumen is None:
            raise FacturaError(f"No hay perfil horario de {mes} para estimar el PVPC.")
        por_hora: dict[int, list[Decimal]] = defaultdict(list)
        for (dia, indice), precio in precios.items():
            if inicio <= dia <= fin:
                por_hora[hora_local(dia, indice, zona)].append(precio)
        if len(por_hora) < 24:
            raise FacturaError(f"Faltan precios PVPC de {mes}.")
        salida[mes] = sum(
            (resumen.perfil[h] * sum(por_hora[h], CERO) / len(por_hora[h]) for h in range(24)),
            CERO,
        )
    return salida


def comparar(
    contrato: Contrato,
    consumo: ConsumoGuardado,
    ofertas: list[Oferta],
    actual: Oferta | None,
    *,
    dias_max: int = 365,
    proveedor_precios: ProveedorPrecios | None = None,
) -> Comparacion:
    """Cost of every offer over the last ``dias_max`` days with consumption data (same kWh for all)."""
    rango = consumo.rango()
    avisos: list[str] = []
    if rango is not None:
        hasta = rango[1]
        desde = max(rango[0], hasta - timedelta(days=dias_max - 1))
        segmentos = _segmentos(desde, hasta)
        faltan = [
            desde + timedelta(days=i)
            for i in range((hasta - desde).days + 1)
            if desde + timedelta(days=i) not in consumo.dias
        ]
        if faltan:
            avisos.append(f"Faltan datos de {len(faltan)} día(s): esos días no se comparan.")
            segmentos = [
                (a, b)
                for a, b in segmentos
                if all(a + timedelta(days=i) in consumo.dias for i in range((b - a).days + 1))
            ]
        manual = False
    elif consumo.manual:
        segmentos = [(m.desde, m.hasta) for m in sorted(consumo.manual, key=lambda m: m.desde)]
        segmentos = [s for s in segmentos if (segmentos[-1][1] - s[0]).days < dias_max]
        desde, hasta = segmentos[0][0], segmentos[-1][1]
        manual = True
    else:
        raise FacturaError("No hay consumo guardado: importa el CSV de Datadis o indica los kWh de tus facturas.")
    if not segmentos:
        raise FacturaError("No hay ningún mes completo con datos para comparar.")

    candidatas: list[tuple[Oferta, bool]] = [(o, False) for o in ofertas]
    if actual is not None:
        candidatas = [(actual, True)] + [(o, False) for o in ofertas if o.nombre != actual.nombre]
    precios: dict[tuple[date, int], Decimal] | None = None
    descartadas: list[str] = []
    costes: list[CosteOferta] = []
    dias_total = sum((b - a).days + 1 for a, b in segmentos)
    for oferta, es_actual in candidatas:
        energia_pvpc: dict[str, Decimal] | None = None
        if oferta.tipo == "pvpc":
            if manual:
                descartadas.append(f"«{oferta.nombre}»: el PVPC necesita consumo horario (importa el CSV).")
                continue
            if proveedor_precios is None:
                descartadas.append(f"«{oferta.nombre}»: sin precios horarios de REE no se puede valorar el PVPC.")
                continue
            try:
                if precios is None:
                    precios = proveedor_precios(segmentos[0][0], segmentos[-1][1])
                energia_pvpc = estimar_energia_pvpc(consumo, segmentos[0][0], segmentos[-1][1], precios, contrato.zona)
            except (FacturaError, RuntimeError) as exc:
                descartadas.append(f"«{oferta.nombre}»: {exc}")
                continue
        total = CERO
        meses: list[tuple[str, Decimal]] = []
        try:
            for a, b in segmentos:
                if manual:
                    m = next(x for x in consumo.manual if x.desde == a and x.hasta == b)
                    factura = calcular_factura(oferta, contrato, a, b, totales={"p1": m.p1, "p2": m.p2, "p3": m.p3})
                else:
                    factura = calcular_factura(
                        oferta, contrato, a, b, dias_kwh=consumo.dias, energia_pvpc_eur=energia_pvpc
                    )
                total += factura.total
                meses.append((f"{a:%Y-%m}", factura.total))
        except (FacturaError, DatoNoDisponibleError) as exc:
            descartadas.append(f"«{oferta.nombre}»: {exc}")
            continue
        anual = euros(total * 365 / dias_total)
        costes.append(CosteOferta(oferta, total, dias_total, anual, es_actual, meses))
    costes.sort(key=lambda c: (c.total, not c.es_actual))
    if any(o.tipo == "pvpc" for o, _ in candidatas) and not manual:
        avisos.append(
            "El PVPC de los meses sin consumo horario guardado se estima con el perfil horario medio de cada mes "
            "y el precio medio de cada hora del día: es una aproximación, no la factura exacta."
        )
    if dias_total < 330:
        avisos.append(
            f"Solo hay {dias_total} días de datos: el coste anual se extrapola y no recoge la estacionalidad completa."
        )
    avisos.append(
        "La comparación usa los peajes, cargos e impuestos vigentes en cada mes y el mismo consumo para todas las "
        "ofertas; no incluye permanencias, penalizaciones ni cambios futuros de precio."
    )
    return Comparacion(segmentos[0][0], segmentos[-1][1], dias_total, costes, descartadas, avisos)


# --- Contracted power -----------------------------------------------------------------------------


@dataclass
class PotenciaOptima:
    recomendada_p1: Decimal
    recomendada_p2: Decimal
    max_p1: Decimal
    max_p2: Decimal
    fuente_datos: str
    ahorro_anual: Decimal | None
    avisos: list[str]


def _arriba(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.1"), rounding=ROUND_CEILING)


def potencia_optima(
    contrato: Contrato,
    consumo: ConsumoGuardado | None,
    maximos: list[tuple[str, Decimal, Decimal | None]],
    actual: Oferta | None,
    margen_pct: Decimal,
    hoy: date,
) -> PotenciaOptima:
    avisos: list[str] = []
    if maximos:
        max_p1 = max(m[1] for m in maximos)
        p2s = [m[2] for m in maximos if m[2] is not None]
        max_p2 = max(p2s) if p2s else max_p1
        fuente = f"maxímetro de {len(maximos)} mes(es)"
        recomendada_p1 = _arriba(max_p1 * (1 + margen_pct / 100))
        recomendada_p2 = _arriba(max_p2 * (1 + margen_pct / 100))
    elif consumo is not None and consumo.meses:
        max_p1 = max(r.max_kwh_hora for r in consumo.meses.values())
        max_p2 = max_p1
        fuente = f"consumo horario de {len(consumo.meses)} mes(es) (kWh en una hora = potencia media de esa hora)"
        recomendada_p1 = recomendada_p2 = _arriba(max_p1 * (1 + margen_pct / 100))
        avisos.append(
            "Sin maxímetro solo se conoce la potencia media de cada hora: los picos de unos minutos (horno, "
            "vitrocerámica, lavadora a la vez) son mayores y harían saltar el limitador. Esta cifra es un mínimo "
            "orientativo, no una recomendación para bajar la potencia: descarga las potencias máximas en Datadis."
        )
    else:
        raise FacturaError("Faltan datos: importa el consumo horario o indica las potencias máximas del maxímetro.")
    recomendada_p1 = max(recomendada_p1, Decimal("0.1"))
    recomendada_p2 = max(recomendada_p2, Decimal("0.1"))
    ahorro = None
    if actual is not None and actual.tipo == "fijo" and maximos:
        datos = cargar_regulados()
        try:
            peaje, cargo = datos.peaje(hoy), datos.cargo(hoy)
        except DatoNoDisponibleError:
            peaje = cargo = None  # type: ignore[assignment]
        if actual.potencia_eur_kw_dia is not None:
            precios = {p: getattr(actual.potencia_eur_kw_dia, p) * 365 for p in ("p1", "p2")}
        else:
            assert actual.potencia_eur_kw_anio is not None
            precios = {p: getattr(actual.potencia_eur_kw_anio, p) for p in ("p1", "p2")}
        if not actual.incluye_peajes_cargos and peaje is not None and cargo is not None:
            for p in ("p1", "p2"):
                precios[p] += peaje.potencia_eur_kw_anio[p] + cargo.potencia_eur_kw_anio[p]  # type: ignore[index]
        delta = {
            "p1": max(contrato.potencia_p1_kw - recomendada_p1, CERO),
            "p2": max(contrato.potencia_p2_kw - recomendada_p2, CERO),
        }
        sin_impuestos = sum((delta[p] * precios[p] for p in ("p1", "p2")), CERO)
        iee, iva = datos.impuestos.iee_general.tipo, datos.impuestos.iva_general.tipo
        ahorro = euros(sin_impuestos * (1 + iee) * (1 + iva))
        avisos.append(
            "Ahorro anual aproximado con los tipos generales de IVA e Impuesto sobre la Electricidad. Cambiar de "
            "potencia puede tener costes de la distribuidora (derechos de acometida/enganche) que no se calculan."
        )
    avisos.append(
        f"Margen de seguridad aplicado: {fmt(margen_pct, 0)} % sobre el máximo observado (elección del módulo, no "
        "una norma). La distribuidora puede exigir que la potencia sea compatible con tu instalación (boletín)."
    )
    return PotenciaOptima(recomendada_p1, recomendada_p2, max_p1, max_p2, fuente, ahorro, avisos)


# --- Cheap hours -----------------------------------------------------------------------------------


@dataclass
class HorasBaratas:
    dia: date
    precios: list[tuple[int, Decimal]]  # (local start hour, €/kWh) in order
    baratas: list[tuple[int, Decimal]]
    caras: list[tuple[int, Decimal]]
    bloque_inicio: int | None
    bloque_media: Decimal | None


def horas_baratas(dia: date, precios: dict[tuple[date, int], Decimal], zona: Zona, n: int, bloque: int) -> HorasBaratas:
    del_dia = sorted((i, p) for (d, i), p in precios.items() if d == dia)
    if not del_dia:
        raise FacturaError(f"No hay precios del {dia:%d-%m-%Y}.")
    por_hora = [(hora_local(dia, i, zona), p) for i, p in del_dia]
    ordenadas = sorted(por_hora, key=lambda x: (x[1], x[0]))
    mejor_inicio, mejor_media = None, None
    if 1 <= bloque <= len(por_hora):
        for k in range(len(por_hora) - bloque + 1):
            media = sum((p for _, p in por_hora[k : k + bloque]), CERO) / bloque
            if mejor_media is None or media < mejor_media:
                mejor_inicio, mejor_media = por_hora[k][0], media
    return HorasBaratas(
        dia=dia,
        precios=por_hora,
        baratas=sorted(ordenadas[:n]),
        caras=sorted(ordenadas[-n:]),
        bloque_inicio=mejor_inicio,
        bloque_media=mejor_media,
    )


def horas_por_periodo(dia: date, zona: Zona) -> dict[str, list[int]]:
    salida: dict[str, list[int]] = {"p1": [], "p2": [], "p3": []}
    for indice in range(24):
        salida[periodo_energia(dia, indice, zona)].append(hora_local(dia, indice, zona))
    return salida


def dia_valle_texto(dia: date) -> str | None:
    if not es_dia_valle(dia):
        return None
    return "fin de semana" if dia.weekday() >= 5 else "festivo nacional"
