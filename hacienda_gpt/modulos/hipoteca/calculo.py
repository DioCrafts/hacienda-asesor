"""Deterministic amortisation schedule of a French-system mortgage (fixed, variable or mixed).

Conventions (documented in docs/modulos/hipoteca.md; a bank may use others and differ by cents):

- periodic rate = nominal annual rate (TIN) / 12 × months per instalment (30/360 months); no day count;
- instalment = French annuity on the outstanding capital over the remaining instalments, rounded to the
  cent half-up; interest of each period = capital × periodic rate, rounded to the cent half-up; the
  capital repaid is the instalment minus that interest; the last instalment repays what is left;
- the instalment is recomputed only when the rate changes (a review) or after an early repayment;
- a review dated ``r`` applies to the instalments whose period starts on or after ``r``;
- a variable rate is index + spread − bonifications, rounded to the multiple the deed sets, bounded by
  the deed's floor/cap, and never negative (art. 21.4 Ley 5/2019);
- an early repayment is applied right after the last instalment due on or before its date; the interest
  of the broken period is not modelled. "plazo": same instalment, fewer instalments (the new count is
  rounded up and the instalment recomputed on it); "cuota": same end date, lower instalment.
"""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from hacienda_gpt.modulos.hipoteca.indices import INDICES, Dato, SerieIndices
from hacienda_gpt.modulos.hipoteca.modelo import AmortizacionRealizada, Prestamo

CENT = Decimal("0.01")
ZERO = Decimal(0)


def euros(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def sumar_meses(base: date, meses: int, dia: int | None = None) -> date:
    """``base`` + ``meses`` months, on ``dia`` (default: base's day) clamped to the month's last day."""
    total = base.year * 12 + (base.month - 1) + meses
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(dia or base.day, monthrange(year, month)[1]))


def mes_menos(fecha: date, meses: int) -> str:
    """'AAAA-MM' of the month ``meses`` months before ``fecha``'s month."""
    total = fecha.year * 12 + (fecha.month - 1) - meses
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def cuota_francesa(capital: Decimal, tipo_anual: Decimal, n: int, periodicidad_meses: int = 1) -> Decimal:
    """French instalment (rounded to the cent) that repays ``capital`` in ``n`` periods at ``tipo_anual`` %."""
    if n <= 0:
        return euros(capital)
    i = tipo_anual / 100 * periodicidad_meses / 12
    if i == 0:
        return euros(capital / n)
    return euros(capital * i / (1 - (1 + i) ** -n))


def cuotas_necesarias(capital: Decimal, tipo_anual: Decimal, cuota: Decimal, periodicidad_meses: int = 1) -> int:
    """Instalments of ``cuota`` needed to repay ``capital`` (rounded up)."""
    if capital <= 0:
        return 0
    i = tipo_anual / 100 * periodicidad_meses / 12
    if i == 0:
        return int((capital / cuota).to_integral_value(rounding=ROUND_CEILING))
    if cuota <= capital * i:
        raise ValueError("La cuota no cubre ni los intereses: no se puede reducir el plazo con ella.")
    n = (cuota / (cuota - capital * i)).ln() / (1 + i).ln()
    return int(n.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class Supuestos:
    """What the schedule assumes about the future: the day it is computed on (what is already published),
    a shift of the projected index (sensitivity) and hypothetical early repayments."""

    hoy: date
    desplazamiento_pp: Decimal = ZERO
    amortizaciones: tuple[AmortizacionRealizada, ...] = ()
    # Replace the projected index (the last published value) by this one for future reviews.
    indice_proyectado: Decimal | None = None


@dataclass(frozen=True)
class CambioTipo:
    fecha: date
    tipo: Decimal  # percent applied from this date
    origen: str  # "contrato" | "banco" | "boe" | "estimado"
    dato: Dato | None = None  # the index value used, with its BOE id
    detalle: str = ""


@dataclass(frozen=True)
class Fila:
    numero: int
    fecha: date
    tipo: Decimal
    cuota: Decimal
    intereses: Decimal
    amortizacion: Decimal
    capital_pendiente: Decimal  # after this instalment and any early repayment right after it
    amortizacion_extra: Decimal = ZERO
    estimado: bool = False  # the rate comes from a projected (not yet published) index


@dataclass
class Cuadro:
    prestamo: Prestamo
    supuestos: Supuestos
    filas: list[Fila]
    cambios: list[CambioTipo]
    avisos: list[str] = field(default_factory=list)

    @property
    def fecha_fin(self) -> date:
        return self.filas[-1].fecha

    def ultima_pagada(self, fecha: date) -> Fila | None:
        pagadas = [f for f in self.filas if f.fecha <= fecha]
        return pagadas[-1] if pagadas else None

    def proxima(self, fecha: date) -> Fila | None:
        return next((f for f in self.filas if f.fecha > fecha), None)

    def capital_pendiente(self, fecha: date) -> Decimal:
        """Outstanding capital after the last instalment due on or before ``fecha`` (no accrued interest)."""
        fila = self.ultima_pagada(fecha)
        if fila is None:
            extra = sum((a.importe for a in self._extras() if a.fecha <= fecha), ZERO)
            return self.prestamo.capital_inicial - extra
        return fila.capital_pendiente

    def _extras(self) -> list[AmortizacionRealizada]:
        return sorted([*self.prestamo.amortizaciones_realizadas, *self.supuestos.amortizaciones], key=lambda a: a.fecha)

    def intereses_desde(self, fecha: date) -> Decimal:
        return sum((f.intereses for f in self.filas if f.fecha > fecha), ZERO)

    def intereses_totales(self) -> Decimal:
        return sum((f.intereses for f in self.filas), ZERO)

    def pagos_del_ano(self, ano: int) -> tuple[Decimal, Decimal]:
        """(capital, interest) of the ordinary instalments due in calendar year ``ano``."""
        filas = [f for f in self.filas if f.fecha.year == ano]
        return sum((f.amortizacion for f in filas), ZERO), sum((f.intereses for f in filas), ZERO)

    def cambio_vigente(self, fecha: date) -> CambioTipo:
        vigentes = [c for c in self.cambios if c.fecha <= fecha]
        return vigentes[-1] if vigentes else self.cambios[0]

    def hay_estimados(self) -> bool:
        return any(f.estimado for f in self.filas)


def fechas_revision(p: Prestamo) -> list[date]:
    """Review dates of a variable or mixed loan: end of the initial period, then every ``revision_meses``."""
    if p.tipo == "fijo" or p.meses_tipo_inicial is None:
        return []
    fechas = []
    meses = p.meses_tipo_inicial
    while meses < p.plazo_meses:
        fechas.append(sumar_meses(p.fecha_firma, meses))
        meses += p.revision_meses
    return fechas


def primera_cuota(p: Prestamo) -> date:
    return p.fecha_primera_cuota or sumar_meses(p.fecha_firma, p.periodicidad_meses)


def fecha_cuota(p: Prestamo, numero: int) -> date:
    base = primera_cuota(p)
    return sumar_meses(base, (numero - 1) * p.periodicidad_meses, base.day)


def dato_para_revision(p: Prestamo, serie: SerieIndices, fecha: date, hoy: date) -> tuple[Dato | None, bool]:
    """(index value, is_projection) for the review on ``fecha`` under the deed's rule.

    When the value the rule asks for is not published yet (as of ``hoy``), the last published one is used
    as a projection."""
    assert p.indice is not None
    if p.indice_regla == "meses_antes":
        dato = serie.valor(p.indice, mes_menos(fecha, p.indice_meses_antes))
        if dato is not None and dato.publicado <= hoy:
            return dato, False
    elif fecha <= hoy:
        dato = serie.ultimo(p.indice, publicado_hasta=fecha)
        if dato is not None:
            return dato, False
    return serie.ultimo(p.indice, publicado_hasta=hoy), True


def tipo_revisado(p: Prestamo, valor_indice: Decimal) -> tuple[Decimal, str]:
    """Rate after a review: index + spread − bonifications, rounded and bounded as the deed says."""
    assert p.diferencial is not None
    bonif = p.reduccion_bonificaciones()
    tipo = valor_indice + p.diferencial - bonif
    pasos = [f"{fmt_pct(valor_indice)} (índice) + {fmt_pct(p.diferencial)} (diferencial)"]
    if bonif:
        pasos.append(f"− {fmt_pct(bonif)} (bonificaciones)")
    if p.redondeo_tipo_multiplo:
        tipo = (tipo / p.redondeo_tipo_multiplo).quantize(Decimal(1), rounding=ROUND_HALF_UP) * p.redondeo_tipo_multiplo
        pasos.append(f"redondeado al múltiplo de {fmt_pct(p.redondeo_tipo_multiplo)}")
    if p.tipo_minimo is not None and tipo < p.tipo_minimo:
        tipo = p.tipo_minimo
        pasos.append(f"con el suelo del contrato ({fmt_pct(p.tipo_minimo)})")
    if p.tipo_maximo is not None and tipo > p.tipo_maximo:
        tipo = p.tipo_maximo
        pasos.append(f"con el techo del contrato ({fmt_pct(p.tipo_maximo)})")
    if tipo < 0:
        tipo = ZERO
        pasos.append("sin bajar de cero (el interés no puede ser negativo)")
    return tipo, " ".join(pasos) + f" = {fmt_pct(tipo)}"


def cambios_de_tipo(p: Prestamo, serie: SerieIndices, supuestos: Supuestos) -> tuple[list[CambioTipo], list[str]]:
    """Every rate change of the loan: the initial rate, each review (bank's figure, BOE index or projection)."""
    avisos: list[str] = []
    cambios = [CambioTipo(fecha=p.fecha_firma, tipo=p.tipo_inicial, origen="contrato", detalle="tipo inicial")]
    conocidas = {(r.fecha.year, r.fecha.month): r for r in p.revisiones_conocidas}
    usadas: set[tuple[int, int]] = set()
    for fecha in fechas_revision(p):
        clave = (fecha.year, fecha.month)
        if clave in conocidas:
            usadas.add(clave)
            cambios.append(CambioTipo(fecha, conocidas[clave].tipo, "banco", detalle="tipo comunicado por el banco"))
            continue
        dato, proyeccion = dato_para_revision(p, serie, fecha, supuestos.hoy)
        if dato is None:
            avisos.append(
                f"No hay valor publicado de {INDICES.get(p.indice or '', p.indice)} para la revisión del "
                f"{fecha:%d/%m/%Y}; se mantiene el tipo anterior. Indica el tipo que aplicó el banco."
            )
            continue
        valor = dato.valor
        if proyeccion:
            if supuestos.indice_proyectado is not None:
                valor = supuestos.indice_proyectado
            valor += supuestos.desplazamiento_pp
        tipo, detalle = tipo_revisado(p, valor)
        cambios.append(CambioTipo(fecha, tipo, "estimado" if proyeccion else "boe", dato, detalle))
    for clave, r in conocidas.items():
        if clave not in usadas:
            # A known rate on a date that is not a computed review (e.g. the deed's dates differ): it wins
            # from that date on, until the next review.
            cambios.append(CambioTipo(r.fecha, r.tipo, "banco", detalle="tipo comunicado por el banco"))
    cambios.sort(key=lambda c: c.fecha)
    return cambios, avisos


def calcular_cuadro(p: Prestamo, serie: SerieIndices, supuestos: Supuestos) -> Cuadro:
    """The whole schedule from the first instalment to the last."""
    cambios, avisos = cambios_de_tipo(p, serie, supuestos)
    extras = sorted([*p.amortizaciones_realizadas, *supuestos.amortizaciones], key=lambda a: a.fecha)
    pm = p.periodicidad_meses
    capital = p.capital_inicial
    ultimo = p.num_cuotas
    filas: list[Fila] = []
    tipo_actual: Decimal | None = None
    cuota = ZERO
    recalcular = True
    k = 0
    pendientes = list(extras)

    def aplicar_extras(hasta: date | None, despues_de: int) -> None:
        nonlocal capital, ultimo, cuota, recalcular
        while pendientes and (hasta is None or pendientes[0].fecha < hasta):
            extra = pendientes.pop(0)
            importe = min(extra.importe, capital)
            capital -= importe
            if filas:
                f = filas[-1]
                filas[-1] = Fila(
                    f.numero,
                    f.fecha,
                    f.tipo,
                    f.cuota,
                    f.intereses,
                    f.amortizacion,
                    capital,
                    f.amortizacion_extra + importe,
                    f.estimado,
                )
            if capital <= 0:
                ultimo = despues_de
                return
            restantes = ultimo - despues_de
            if extra.modalidad == "plazo" and tipo_actual is not None and cuota > 0:
                ultimo = despues_de + min(restantes, cuotas_necesarias(capital, tipo_actual, cuota, pm))
            recalcular = True

    aplicar_extras(fecha_cuota(p, 1), 0)
    while k < ultimo and capital > 0:
        k += 1
        fecha = fecha_cuota(p, k)
        inicio = sumar_meses(fecha, -pm)
        vigente = [c for c in cambios if c.fecha <= inicio] or cambios[:1]
        nuevo = vigente[-1]
        if tipo_actual is None or nuevo.tipo != tipo_actual:
            tipo_actual = nuevo.tipo
            recalcular = True
        estimado = nuevo.origen == "estimado"
        restantes = ultimo - k + 1
        if recalcular:
            cuota = cuota_francesa(capital, tipo_actual, restantes, pm)
            recalcular = False
        i = tipo_actual / 100 * pm / 12
        intereses = euros(capital * i)
        if k == ultimo:
            amortizacion = capital
            cuota_k = intereses + amortizacion
        else:
            amortizacion = min(cuota - intereses, capital)
            cuota_k = intereses + amortizacion
        capital -= amortizacion
        filas.append(Fila(k, fecha, tipo_actual, cuota_k, intereses, amortizacion, capital, ZERO, estimado))
        siguiente = fecha_cuota(p, k + 1) if k < ultimo else None
        aplicar_extras(siguiente, k)
    if pendientes:
        avisos.append("Hay amortizaciones anticipadas posteriores al fin del préstamo; se han ignorado.")
    return Cuadro(prestamo=p, supuestos=supuestos, filas=filas, cambios=cambios, avisos=avisos)


def resumen_anual(cuadro: Cuadro, anos: Iterable[int]) -> list[tuple[int, Decimal, Decimal]]:
    return [(ano, *cuadro.pagos_del_ano(ano)) for ano in anos]


def comparar_con_banco(cuadro: Cuadro) -> list[str]:
    """Lines comparing the schedule with the figures the bank showed (the acceptance check)."""
    lineas: list[str] = []
    for c in cuadro.prestamo.comprobacion_banco:
        fila = cuadro.ultima_pagada(c.fecha)
        if c.cuota is not None:
            nuestra = (cuadro.proxima(c.fecha) if fila is None or fila.fecha != c.fecha else fila) or fila
            if nuestra is not None:
                dif = nuestra.cuota - c.cuota
                lineas.append(
                    f"Cuota del {c.fecha:%d/%m/%Y}: banco {fmt(c.cuota)} €, cálculo {fmt(nuestra.cuota)} € "
                    f"(diferencia {fmt(dif)} €)."
                )
        if c.capital_pendiente is not None:
            nuestro = cuadro.capital_pendiente(c.fecha)
            dif = nuestro - c.capital_pendiente
            lineas.append(
                f"Capital pendiente a {c.fecha:%d/%m/%Y}: banco {fmt(c.capital_pendiente)} €, cálculo {fmt(nuestro)} € "
                f"(diferencia {fmt(dif)} €)."
            )
    return lineas


def fmt(x: Decimal, decimales: int = 2) -> str:
    """Spanish number format: 1.234,56."""
    q = Decimal(1).scaleb(-decimales)
    texto = f"{x.quantize(q, rounding=ROUND_HALF_UP):,.{decimales}f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_pct(x: Decimal) -> str:
    texto = format(x.normalize(), "f") if x == x.to_integral() else f"{x:.3f}".rstrip("0")
    return texto.replace(".", ",") + " %"


def filas_entre(cuadro: Cuadro, desde: date, n: int) -> Sequence[Fila]:
    return [f for f in cuadro.filas if f.fecha > desde][:n]
