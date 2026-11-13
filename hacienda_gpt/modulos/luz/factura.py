"""The bill of a 2.0TD household supply, line by line, from consumption, contract and offer.

Structure of a Spanish household bill (each line carries its source):

1. power term: contracted kW × days × price (P1 punta-llano and P2 valle); for PVPC, tolls + charges in
   €/kW and year prorated by the days of the year, plus the fixed commercialisation term on P1;
2. energy term: kWh of each period × price; for PVPC, the hourly energy price published by REE;
3. financing of the social bonus: the regulated value per supply and year, prorated by days;
4. electricity tax: rate on 1 + 2 + 3 (art. 97 Ley 38/1992), never below the per-MWh minimum;
5. meter rental, by days;
6. VAT on everything above (and on optional services).

A billing period is split wherever a regulated figure changes (tolls, charges, social-bonus value or a
temporary tax rate). Lines with the same concept and unit price are added before rounding, like bills do;
every amount is rounded to the cent (half up) once per line.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from hacienda_gpt.modulos.luz.consumo import Lectura
from hacienda_gpt.modulos.luz.datos import PERIODOS, Regulados, cargar_regulados
from hacienda_gpt.modulos.luz.ofertas import Oferta
from hacienda_gpt.modulos.luz.periodos import Zona, periodo_energia

CENT = Decimal("0.01")
CERO = Decimal(0)
TiposReducidos = Literal["auto", "si", "no"]
PreciosHorarios = Mapping[tuple[date, int], Decimal]  # (local date, ordinal hour) -> €/kWh


def euros(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def fmt(x: Decimal, decimales: int = 2) -> str:
    """Spanish number: 1.234,56."""
    q = Decimal(1).scaleb(-decimales)
    texto = f"{x.quantize(q, rounding=ROUND_HALF_UP):,.{decimales}f}"
    return texto.replace(",", "_").replace(".", ",").replace("_", ".")


def fmt_pct(x: Decimal) -> str:
    return fmt(x * 100, 8).rstrip("0").rstrip(",") + " %"


def dias_del_anio(anio: int) -> int:
    return (date(anio + 1, 1, 1) - date(anio, 1, 1)).days


@dataclass(frozen=True)
class Contrato:
    potencia_p1_kw: Decimal
    potencia_p2_kw: Decimal
    zona: Zona = "peninsula"


@dataclass(frozen=True)
class Linea:
    concepto: str
    detalle: str
    importe: Decimal
    fuente: str


@dataclass
class Factura:
    oferta: str
    desde: date
    hasta: date
    dias: int
    kwh: dict[str, Decimal]
    lineas: list[Linea]
    total: Decimal
    avisos: list[str] = field(default_factory=list)
    citas: tuple[str, ...] = ()

    def importe(self, prefijo: str) -> Decimal:
        return sum((linea.importe for linea in self.lineas if linea.concepto.startswith(prefijo)), CERO)

    def texto(self) -> str:
        kwh = ", ".join(f"{p.upper()} {fmt(self.kwh[p], 3)} kWh" for p in PERIODOS)
        partes = [
            f"Factura simulada «{self.oferta}» del {self.desde:%d-%m-%Y} al {self.hasta:%d-%m-%Y} "
            f"({self.dias} días). Consumo: {kwh} (total {fmt(sum(self.kwh.values(), CERO), 3)} kWh).",
            "",
        ]
        for linea in self.lineas:
            partes.append(f"- {linea.concepto}: {fmt(linea.importe)} € ({linea.detalle}). Fuente: {linea.fuente}.")
        partes.append(f"TOTAL: {fmt(self.total)} €")
        if self.avisos:
            partes.append("")
            partes.extend(f"Aviso: {a}" for a in self.avisos)
        return "\n".join(partes)


class FacturaError(ValueError):
    """The bill cannot be computed. The message is Spanish, for the user."""


@dataclass(frozen=True)
class Regimen:
    iee_tipo: Decimal
    iee_minimo_eur_mwh: Decimal
    iva_tipo: Decimal
    fuente_iee: str
    fuente_iva: str


@dataclass
class _Bruta:
    orden: int
    concepto: str
    precio_texto: str
    regimen: Regimen
    cantidad: Decimal  # days or kWh, for the detail
    unidad: str
    importe: Decimal  # exact, rounded once at the end
    fuente: str
    base_iee: bool = True
    iva_general: bool = False
    kwh: Decimal = CERO
    multiplicador: str = ""


def _regimen(
    datos: Regulados, dia: date, potencia_max: Decimal, tipos_reducidos: TiposReducidos, avisos: set[str]
) -> Regimen:
    iee, iva = datos.impuestos.iee_general, datos.impuestos.iva_general
    general = Regimen(
        iee.tipo, iee.minimo_eur_mwh, iva.tipo, "art. 99 Ley 38/1992 (BOE-A-1992-28741)", "art. 90 LIVA (BOE-A-1992-28740)"
    )
    tramo = datos.tramo_fiscal(dia)
    if tramo is None or tramo.estado == "no_aplicada":
        return general
    if tramo.estado == "condicional":
        if tipos_reducidos == "auto":
            avisos.add(
                f"Del {tramo.desde:%d-%m-%Y} al {tramo.hasta:%d-%m-%Y} la rebaja del IVA (10 %) y del Impuesto sobre "
                f"la Electricidad (0,5 %) era condicional: {tramo.condicion} Se han usado los tipos generales; si tu "
                "factura aplicó la rebaja, repite el cálculo con tipos_reducidos='si'."
            )
            return general
        if tipos_reducidos == "no":
            return general
    assert tramo.iee_tipo is not None and tramo.iee_minimo_eur_mwh is not None and tramo.iva_tipo is not None
    referencia = f"{tramo.fuente.referencia} ({', '.join(sorted({e.boe_id for e in tramo.evidencia}))})"
    iva_tipo, fuente_iva = tramo.iva_tipo, referencia
    if tramo.iva_potencia_max_kw is not None and potencia_max > tramo.iva_potencia_max_kw:
        iva_tipo, fuente_iva = general.iva_tipo, general.fuente_iva
        avisos.add(
            f"Con más de {fmt(tramo.iva_potencia_max_kw, 0)} kW contratados no se aplica el IVA reducido temporal."
        )
    return Regimen(tramo.iee_tipo, tramo.iee_minimo_eur_mwh, iva_tipo, referencia, fuente_iva)


def calcular_factura(
    oferta: Oferta,
    contrato: Contrato,
    desde: date,
    hasta: date,
    *,
    lecturas: Sequence[Lectura] | None = None,
    dias_kwh: Mapping[date, Sequence[Decimal]] | None = None,
    totales: Mapping[str, Decimal] | None = None,
    precios_pvpc: PreciosHorarios | None = None,
    energia_pvpc_eur: Mapping[str, Decimal] | None = None,
    tipos_reducidos: TiposReducidos = "auto",
    datos: Regulados | None = None,
) -> Factura:
    """Bill of ``oferta`` between ``desde`` and ``hasta`` (both included, as bills print them).

    Consumption, in order of preference: hourly ``lecturas``; ``dias_kwh`` (date -> [P1, P2, P3]); or the
    ``totales`` of the period by P1/P2/P3 (then spread by days if the period has to be split). PVPC needs
    ``precios_pvpc`` (hourly €/kWh, REE indicator 1001) with hourly readings, or ``energia_pvpc_eur``
    (month 'AAAA-MM' -> € of energy already estimated) for comparisons.
    """
    datos = datos or cargar_regulados()
    if hasta < desde:
        raise FacturaError("La fecha final es anterior a la inicial.")
    if contrato.zona not in ("peninsula", "baleares"):
        raise FacturaError(
            "En Canarias, Ceuta y Melilla no se aplica el IVA sino el IGIC o el IPSI, que este módulo aún no tiene "
            "cargados: no puedo reproducir la factura."
        )
    if not (CERO < contrato.potencia_p1_kw <= 15 and CERO < contrato.potencia_p2_kw <= 15):
        raise FacturaError("La tarifa 2.0TD es para potencias de más de 0 y hasta 15 kW en cada periodo.")
    n_dias = (hasta - desde).days + 1
    dias = [desde + timedelta(days=i) for i in range(n_dias)]
    avisos: set[str] = set()
    potencia_max = max(contrato.potencia_p1_kw, contrato.potencia_p2_kw)

    # --- consumption by day and period --------------------------------------------------------------
    por_dia: dict[date, dict[str, Decimal]] | None = None
    if lecturas is not None:
        por_dia = {d: {p: CERO for p in PERIODOS} for d in dias}
        for lectura in lecturas:
            if desde <= lectura.fecha <= hasta:
                por_dia[lectura.fecha][periodo_energia(lectura.fecha, lectura.indice, contrato.zona)] += lectura.kwh
        faltan = [d for d in dias if not any(lec.fecha == d for lec in lecturas)]
        if faltan:
            raise FacturaError(f"Faltan lecturas horarias de {len(faltan)} día(s), p. ej. el {faltan[0]:%d-%m-%Y}.")
    elif dias_kwh is not None:
        faltan = [d for d in dias if d not in dias_kwh]
        if faltan:
            raise FacturaError(f"Faltan consumos de {len(faltan)} día(s), p. ej. el {faltan[0]:%d-%m-%Y}.")
        por_dia = {d: dict(zip(PERIODOS, dias_kwh[d], strict=True)) for d in dias}
    elif totales is None:
        raise FacturaError("Falta el consumo: importa el CSV horario o indica los kWh de P1, P2 y P3 de la factura.")

    if oferta.tipo == "pvpc" and energia_pvpc_eur is None:
        if precios_pvpc is None or lecturas is None:
            raise FacturaError(
                "Para el PVPC hacen falta el consumo hora a hora y los precios horarios de REE (indicador 1001)."
            )
        sin_precio = [(lec.fecha, lec.indice) for lec in lecturas if desde <= lec.fecha <= hasta]
        sin_precio = [k for k in sin_precio if k not in precios_pvpc]
        if sin_precio:
            f, i = sin_precio[0]
            raise FacturaError(f"Faltan precios PVPC de {len(sin_precio)} hora(s), p. ej. el {f:%d-%m-%Y} hora {i + 1}.")

    # --- split the period where a regulated figure changes ------------------------------------------
    def clave(d: date) -> tuple[object, ...]:
        return (
            datos.peaje(d).desde,
            datos.cargo(d).desde,
            datos.bono(d).desde,
            _regimen(datos, d, potencia_max, tipos_reducidos, avisos),
            d.year,
        )

    tramos: list[list[date]] = []
    for d in dias:
        if tramos and clave(tramos[-1][-1]) == clave(d):
            tramos[-1].append(d)
        else:
            tramos.append([d])

    total_kwh_periodo = (
        {p: sum((por_dia[d][p] for d in dias), CERO) for p in PERIODOS}
        if por_dia is not None
        else {p: Decimal(totales[p]) for p in PERIODOS}  # type: ignore[index]
    )
    if por_dia is None and len(tramos) > 1:
        avisos.add(
            "El periodo se parte en tramos con precios o impuestos distintos y solo hay totales por periodo: los kWh "
            "se reparten entre tramos en proporción a los días (la comercializadora puede usar las lecturas reales)."
        )

    brutas: list[_Bruta] = []
    citas: set[str] = set()
    repartido = {p: CERO for p in PERIODOS}
    for n, tramo in enumerate(tramos):
        d0 = tramo[0]
        ndias = Decimal(len(tramo))
        nanio = Decimal(dias_del_anio(d0.year))
        reg = _regimen(datos, d0, potencia_max, tipos_reducidos, avisos)
        peaje, cargo, bono = datos.peaje(d0), datos.cargo(d0), datos.bono(d0)
        if por_dia is not None:
            kwh = {p: sum((por_dia[d][p] for d in tramo), CERO) for p in PERIODOS}
        elif n == len(tramos) - 1:
            kwh = {p: total_kwh_periodo[p] - repartido[p] for p in PERIODOS}
        else:
            kwh = {p: (total_kwh_periodo[p] * ndias / n_dias).quantize(Decimal("0.001")) for p in PERIODOS}
        for p in PERIODOS:
            repartido[p] += kwh[p]

        regulado_pot = f"peajes {peaje.fuente.referencia} y cargos {cargo.fuente.referencia}"
        # Power term.
        for p, kw in (("p1", contrato.potencia_p1_kw), ("p2", contrato.potencia_p2_kw)):
            nombre = "punta-llano" if p == "p1" else "valle"
            if oferta.tipo == "pvpc" or not oferta.incluye_peajes_cargos:
                anual = peaje.potencia_eur_kw_anio[p] + cargo.potencia_eur_kw_anio[p]  # type: ignore[index]
                brutas.append(
                    _Bruta(
                        1,
                        f"Potencia {p.upper()} ({nombre}): peajes y cargos",
                        f"{fmt(anual, 6)} €/kW·año",
                        reg,
                        ndias,
                        "días",
                        kw * anual * ndias / nanio,
                        regulado_pot,
                        multiplicador=f"{fmt(kw, 3)} kW × ",
                    )
                )
                citas.update(e.boe_id for e in peaje.evidencia + cargo.evidencia)
            if oferta.tipo == "pvpc":
                if p == "p1":
                    ccf = datos.pvpc.ccf_eur_kw_anio
                    brutas.append(
                        _Bruta(
                            2,
                            "Potencia P1: margen de comercialización fijo (PVPC)",
                            f"{fmt(ccf, 3)} €/kW·año",
                            reg,
                            ndias,
                            "días",
                            kw * ccf * ndias / nanio,
                            f"{datos.pvpc.fuente.referencia} (BOE-A-2016-12274)",
                            multiplicador=f"{fmt(kw, 3)} kW × ",
                        )
                    )
                continue
            if oferta.potencia_eur_kw_dia is not None:
                precio = getattr(oferta.potencia_eur_kw_dia, p)
                importe, precio_texto = kw * precio * ndias, f"{fmt(precio, 6)} €/kW·día"
            else:
                assert oferta.potencia_eur_kw_anio is not None
                precio = getattr(oferta.potencia_eur_kw_anio, p)
                importe, precio_texto = kw * precio * ndias / nanio, f"{fmt(precio, 6)} €/kW·año"
            etiqueta = "Potencia" if oferta.incluye_peajes_cargos else "Potencia (margen de la comercializadora)"
            brutas.append(
                _Bruta(
                    3,
                    f"{etiqueta} {p.upper()} ({nombre})",
                    precio_texto,
                    reg,
                    ndias,
                    "días",
                    importe,
                    f"precio de la oferta «{oferta.nombre}»",
                    multiplicador=f"{fmt(kw, 3)} kW × ",
                )
            )

        # Energy term.
        if oferta.tipo == "pvpc":
            en_tramo = set(tramo)
            if energia_pvpc_eur is not None:
                coste = CERO
                for mes in sorted({f"{d:%Y-%m}" for d in tramo}):
                    del_mes = [d for d in dias if f"{d:%Y-%m}" == mes]
                    en_ambos = [d for d in del_mes if d in en_tramo]
                    coste += energia_pvpc_eur.get(mes, CERO) * Decimal(len(en_ambos)) / Decimal(len(del_mes))
                fuente = "estimación con el perfil horario medio del mes y los precios horarios de REE (indicador 1001)"
            else:
                assert lecturas is not None and precios_pvpc is not None
                coste = sum(
                    (lec.kwh * precios_pvpc[(lec.fecha, lec.indice)] for lec in lecturas if lec.fecha in en_tramo),
                    CERO,
                )
                fuente = "precio horario PVPC 2.0TD de REE (indicador 1001, incluye peajes y cargos de energía)"
            total = sum(kwh.values(), CERO)
            brutas.append(
                _Bruta(
                    4,
                    "Energía consumida PVPC (precio hora a hora)",
                    f"{fmt(coste / total, 6) if total else '0'} €/kWh medio",
                    reg,
                    total,
                    "kWh",
                    coste,
                    fuente,
                    kwh=total,
                )
            )
        else:
            assert oferta.energia_eur_kwh is not None
            for p in PERIODOS:
                precio = getattr(oferta.energia_eur_kwh, p)
                if not oferta.incluye_peajes_cargos:
                    regulada = peaje.energia_eur_kwh[p] + cargo.energia_eur_kwh[p]  # type: ignore[index]
                    brutas.append(
                        _Bruta(
                            5,
                            f"Energía {p.upper()}: peajes y cargos",
                            f"{fmt(regulada, 6)} €/kWh",
                            reg,
                            kwh[p],
                            "kWh",
                            kwh[p] * regulada,
                            regulado_pot,
                        )
                    )
                brutas.append(
                    _Bruta(
                        6,
                        f"Energía {p.upper()}" + ("" if oferta.incluye_peajes_cargos else " (precio de la comercializadora)"),
                        f"{fmt(precio, 6)} €/kWh",
                        reg,
                        kwh[p],
                        "kWh",
                        kwh[p] * precio,
                        f"precio de la oferta «{oferta.nombre}»",
                        kwh=kwh[p],
                    )
                )

        # Social bonus financing.
        if oferta.bono_social_eur_dia is None:
            brutas.append(
                _Bruta(
                    8,
                    "Financiación del bono social",
                    f"{fmt(bono.eur_cups_anio, 6)} €/año ÷ {nanio} días",
                    reg,
                    ndias,
                    "días",
                    bono.eur_cups_anio * ndias / nanio,
                    f"{bono.fuente.referencia} ({', '.join(sorted({e.boe_id for e in bono.evidencia}))})",
                )
            )
            citas.update(e.boe_id for e in bono.evidencia)
        elif oferta.bono_social_eur_dia:
            brutas.append(
                _Bruta(
                    8,
                    "Financiación del bono social",
                    f"{fmt(oferta.bono_social_eur_dia, 6)} €/día",
                    reg,
                    ndias,
                    "días",
                    oferta.bono_social_eur_dia * ndias,
                    f"importe diario de la oferta «{oferta.nombre}»",
                )
            )

        # Meter rental (outside the electricity-tax base).
        if oferta.contador != "propio":
            if oferta.alquiler_contador_eur_dia is not None:
                importe = oferta.alquiler_contador_eur_dia * ndias
                precio_texto, fuente = f"{fmt(oferta.alquiler_contador_eur_dia, 6)} €/día", "importe de la oferta"
            else:
                ac = datos.alquiler_contador
                mensual = ac.monofasico_eur_mes if oferta.contador == "monofasico" else ac.trifasico_eur_mes
                importe = mensual * 12 * ndias / nanio
                precio_texto = f"{fmt(mensual, 2)} €/mes × 12 ÷ {nanio} días"
                fuente = f"{ac.fuente.referencia} (BOE-A-2013-8561)"
                citas.add("BOE-A-2013-8561")
            brutas.append(
                _Bruta(10, "Alquiler del contador", precio_texto, reg, ndias, "días", importe, fuente, base_iee=False)
            )
        if oferta.servicios_eur_mes:
            brutas.append(
                _Bruta(
                    11,
                    "Servicios adicionales de la oferta",
                    f"{fmt(oferta.servicios_eur_mes)} €/mes × 12 ÷ {nanio} días",
                    reg,
                    ndias,
                    "días",
                    oferta.servicios_eur_mes * 12 * ndias / nanio,
                    f"oferta «{oferta.nombre}»",
                    base_iee=False,
                    iva_general=True,
                )
            )

    # --- merge lines with the same concept, unit price and tax regime; round once -----------------
    fusionadas: dict[tuple[object, ...], _Bruta] = {}
    for b in brutas:
        k = (b.orden, b.concepto, b.precio_texto, b.regimen, b.multiplicador)
        if k in fusionadas:
            f = fusionadas[k]
            f.cantidad += b.cantidad
            f.importe += b.importe
            f.kwh += b.kwh
        else:
            fusionadas[k] = _Bruta(**{**b.__dict__})
    regimenes = list(dict.fromkeys(b.regimen for b in fusionadas.values()))
    varios = len(regimenes) > 1

    def sufijo(reg: Regimen) -> str:
        if not varios:
            return ""
        tramo_dias = [d for t in tramos for d in t if clave(d)[3] == reg]
        return f" [del {min(tramo_dias):%d-%m} al {max(tramo_dias):%d-%m}]"

    lineas: list[Linea] = []
    iva_bases: dict[tuple[Decimal, str], Decimal] = {}
    for reg in regimenes:
        propias = sorted((b for b in fusionadas.values() if b.regimen == reg), key=lambda b: b.orden)
        base_iee = CERO
        kwh_reg = CERO
        elec = CERO
        for b in propias:
            if b.orden >= 10:
                continue
            cantidad = fmt(b.cantidad, 3 if b.unidad == "kWh" else 0)
            importe = euros(b.importe)
            lineas.append(
                Linea(b.concepto + sufijo(reg), f"{b.multiplicador}{cantidad} {b.unidad} × {b.precio_texto}", importe, b.fuente)
            )
            base_iee += importe
            kwh_reg += b.kwh
        # Discounts on the offer's own energy / power prices (before taxes).
        for pct, ordenes, que in (
            (oferta.descuento_energia_pct, (6,), "energía"),
            (oferta.descuento_potencia_pct, (3,), "potencia"),
        ):
            if pct:
                sobre = sum((euros(b.importe) for b in propias if b.orden in ordenes), CERO)
                descuento = -euros(sobre * pct / 100)
                lineas.append(
                    Linea(
                        f"Descuento {fmt(pct, 2)} % sobre {que}{sufijo(reg)}",
                        f"{fmt(pct, 2)} % de {fmt(sobre)} €",
                        descuento,
                        f"oferta «{oferta.nombre}»",
                    )
                )
                base_iee += descuento
        cuota_pct = base_iee * reg.iee_tipo
        cuota_min = kwh_reg / 1000 * reg.iee_minimo_eur_mwh
        if cuota_min > cuota_pct:
            iee = euros(cuota_min)
            detalle = (
                f"mínimo {fmt(reg.iee_minimo_eur_mwh, 0)} €/MWh × {fmt(kwh_reg, 3)} kWh, mayor que "
                f"{fmt_pct(reg.iee_tipo)} × {fmt(base_iee)} €"
            )
        else:
            iee = euros(cuota_pct)
            detalle = f"{fmt_pct(reg.iee_tipo)} × {fmt(base_iee)} € (potencia + energía + bono social)"
        lineas.append(Linea("Impuesto sobre la electricidad" + sufijo(reg), detalle, iee, reg.fuente_iee))
        elec = base_iee + iee
        for b in propias:
            if b.orden == 10:
                importe = euros(b.importe)
                lineas.append(
                    Linea(
                        b.concepto + sufijo(reg),
                        f"{fmt(b.cantidad, 0)} {b.unidad} × {b.precio_texto}",
                        importe,
                        b.fuente,
                    )
                )
                elec += importe
            elif b.orden == 11:
                importe = euros(b.importe)
                lineas.append(
                    Linea(b.concepto + sufijo(reg), f"{fmt(b.cantidad, 0)} {b.unidad} × {b.precio_texto}", importe, b.fuente)
                )
                g = (datos.impuestos.iva_general.tipo, "art. 90 LIVA (BOE-A-1992-28740)")
                iva_bases[g] = iva_bases.get(g, CERO) + importe
        k_iva = (reg.iva_tipo, reg.fuente_iva)
        iva_bases[k_iva] = iva_bases.get(k_iva, CERO) + elec
        if reg.iva_tipo != datos.impuestos.iva_general.tipo and oferta.contador != "propio":
            avisos.add(
                "Con el IVA reducido temporal se ha aplicado el mismo tipo al alquiler del contador, como servicio "
                "accesorio del suministro; si tu factura le aplica el 21 %, la diferencia es de céntimos."
            )
    for (tipo, fuente), base in iva_bases.items():
        lineas.append(Linea(f"IVA {fmt_pct(tipo)}", f"{fmt_pct(tipo)} × {fmt(base)} €", euros(base * tipo), fuente))
    total = sum((linea.importe for linea in lineas), CERO)

    avisos_lista = sorted(avisos)
    if oferta.tipo == "pvpc":
        avisos_lista.append(
            "El margen de comercialización fijo del PVPC (3,113 €/kW·año) se lee de una orden de 2016 sin "
            "actualización encontrada en el BOE: compruébalo con tu factura."
        )
    if oferta.bono_social_eur_dia is None and oferta.tipo == "fijo":
        avisos_lista.append(
            "La financiación del bono social se ha calculado con el valor regulado por CUPS; una comercializadora "
            "libre puede cobrar otra cifra (indícala en «bono_social_eur_dia») o no cobrarla (0)."
        )
    citas.update({"BOE-A-1992-28741", "BOE-A-1992-28740", "BOE-A-2020-1066"})
    for d in dias:
        t = datos.tramo_fiscal(d)
        if t is not None:
            citas.update(e.boe_id for e in t.evidencia)
    return Factura(
        oferta=oferta.nombre,
        desde=desde,
        hasta=hasta,
        dias=n_dias,
        kwh=total_kwh_periodo,
        lineas=lineas,
        total=total,
        avisos=avisos_lista,
        citas=datos.citas(*sorted(c for c in citas if c in datos.fuentes_principales)),
    )
