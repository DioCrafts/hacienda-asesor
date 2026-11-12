"""Impuesto sobre Sociedades for small companies (Ley 27/2014), as pure functions.

Three calculators, all for the common-territory rules and for entities taxed
under art. 29.1 (general rate, micro-company scale, reduced-size entities and
newly created companies):

* :func:`calcular_sociedades` — from the accounting profit to the amount due:
  adjustments (user totals), capitalisation reserve (art. 25), relief for
  losses brought forward (art. 26), rate (art. 29.1 and transitional
  provision 44), quota, user-supplied credits, payments on account.
* :func:`calcular_pago_fraccionado_sociedades` — one instalment of modelo 202
  by either method of art. 40.
* :func:`comparar_autonomo_vs_sociedad` — the arithmetic of earning the same
  profit as a self-employed person or through a company that pays a salary and
  distributes the rest. It adds up taxes only and says what it leaves out.

Whatever the functions cannot check (the conditions of each rate, the nature
of the adjustments) or do not model (see ``no_modelado`` in the data file) is
reported in ``avisos`` on every result.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.tax.actividad import ZERO, Traza, _round
from hacienda_gpt.tax.irpf import IRPFInput, IRPFResult, Linea, calcular_irpf
from hacienda_gpt.tax.sociedades_params import SociedadesParams, load_sociedades_params

Regimen = Literal["general", "entidad_patrimonial", "nueva_creacion", "microempresa", "reducida_dimension"]

_CIEN = Decimal(100)


def _pct(tipo: Decimal) -> str:
    """0.265 -> '26,5 %'."""
    return f"{(tipo * _CIEN).normalize():f}".replace(".", ",") + " %"


def _eur(importe: Decimal) -> str:
    """1000000 -> '1.000.000'."""
    return f"{importe:,.0f}".replace(",", ".")


# --- Rate --------------------------------------------------------------------


class _DatosTipo(BaseModel):
    """Facts that decide the rate of art. 29.1. Shared by the tax and the instalment inputs."""

    model_config = ConfigDict(extra="forbid")

    # Year in which the tax period starts.
    ejercicio: int
    # Net turnover of the immediately preceding tax period (art. 29.1 and 101: of the whole
    # group, raised to a year if that period was shorter). None = unknown: general rate.
    cifra_negocios_anterior: Decimal | None = Field(default=None, ge=0)
    # Net turnover in the 12 months before the start of the period (arts. 25, 40 and additional
    # provisions 14 and 15 use this wording). None = taken as ``cifra_negocios_anterior``.
    cifra_negocios_12_meses_anteriores: Decimal | None = Field(default=None, ge=0)
    # Art. 5.2: more than half of the assets are securities or not used in a business.
    entidad_patrimonial: bool = False
    # The user states that the company meets the conditions of the 15 % rate of art. 29.1 for
    # newly created entities in THIS period (first period with a positive base, or the next).
    nueva_creacion_tipo_reducido: bool = False

    @property
    def cifra_12_meses(self) -> Decimal | None:
        if self.cifra_negocios_12_meses_anteriores is not None:
            return self.cifra_negocios_12_meses_anteriores
        return self.cifra_negocios_anterior


class _Tipo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    regimen: Regimen
    tipo: Decimal  # the single rate, or the first bracket's rate for the scale
    tipo_resto: Decimal | None = None  # second bracket of the micro-company scale


def _determinar_tipo(datos: _DatosTipo, p: SociedadesParams, t: Traza) -> _Tipo:
    tg = p.tipos_gravamen
    ano = p.tipos_de(datos.ejercicio)
    cifra = datos.cifra_negocios_anterior
    t.usa(tg.fuente)
    if datos.ejercicio > p.ejercicio:
        t.aviso(
            f"Los tipos para períodos iniciados en {datos.ejercicio} son los que el texto consolidado a "
            f"{next(iter(p.fuentes_principales.values())).consolidado_a} anuncia; una ley posterior puede cambiarlos."
        )
    if datos.entidad_patrimonial:
        t.aviso(
            "Entidad patrimonial (art. 5.2 LIS): no puede aplicar la escala de microempresas ni los tipos de "
            f"reducida dimensión o de nueva creación; tributa al tipo general del {_pct(tg.general)}."
        )
        return _Tipo(regimen="entidad_patrimonial", tipo=tg.general)
    if datos.nueva_creacion_tipo_reducido:
        t.aviso(
            f"Se ha aplicado el {_pct(tg.nueva_creacion)} de las entidades de nueva creación porque así lo has "
            "indicado; no se comprueban sus requisitos (art. 29.1 LIS): solo vale para el primer período con base "
            "imponible positiva y el siguiente, la entidad debe realizar actividades económicas y no formar parte "
            "de un grupo del art. 42 del Código de Comercio. NO se considera iniciada una actividad si antes la "
            "realizaban personas o entidades vinculadas que la han transmitido a la sociedad, ni si la ejerció "
            "durante el año anterior a la constitución una persona física con más del 50 % del capital: es el "
            "caso habitual del autónomo que pasa su actividad a una S.L. propia, que NO tiene derecho a este tipo."
        )
        return _Tipo(regimen="nueva_creacion", tipo=tg.nueva_creacion)
    if cifra is None:
        t.aviso(
            "No se ha indicado el importe neto de la cifra de negocios del período anterior: se aplica el tipo "
            f"general del {_pct(tg.general)}. Si fue inferior a {_eur(tg.limite_microempresa)} euros corresponde la "
            f"escala del {_pct(ano.micro_primer_tramo)} / {_pct(ano.micro_resto)}, y si fue inferior a "
            f"{_eur(tg.limite_reducida_dimension)} euros, el {_pct(ano.reducida_dimension)}."
        )
        return _Tipo(regimen="general", tipo=tg.general)
    if cifra < tg.limite_microempresa:
        t.aviso(
            f"Escala de microempresa por cifra de negocios del período anterior inferior a "
            f"{_eur(tg.limite_microempresa)} euros. Esa cifra es la del conjunto del grupo (art. 101.3 LIS, incluidas "
            "las sociedades controladas por la misma persona física o sus familiares), se eleva al año si el período "
            "anterior duró menos y, en una entidad de nueva creación, es la del primer período de actividad "
            "(art. 101.2 LIS). No se comprueba."
        )
        return _Tipo(regimen="microempresa", tipo=ano.micro_primer_tramo, tipo_resto=ano.micro_resto)
    if cifra < tg.limite_reducida_dimension:
        t.aviso(
            f"Tipo de entidad de reducida dimensión por cifra de negocios del período anterior inferior a "
            f"{_eur(tg.limite_reducida_dimension)} euros, computada para todo el grupo y elevada al año "
            "(art. 101 LIS). No se comprueba."
        )
        return _Tipo(regimen="reducida_dimension", tipo=ano.reducida_dimension)
    t.aviso(
        f"Cifra de negocios del período anterior de al menos {_eur(tg.limite_reducida_dimension)} euros: tipo "
        "general. No se ha considerado la prórroga de 3 períodos del art. 101.4 LIS para quien acaba de superar "
        "ese importe."
    )
    return _Tipo(regimen="general", tipo=tg.general)


# --- Annual tax --------------------------------------------------------------


class SociedadesInput(_DatosTipo):
    """One tax period of a company. Amounts in euros; absent means zero."""

    # Accounting profit or loss BEFORE the corporate income tax expense.
    resultado_contable: Decimal = ZERO
    # Totals of the adjustments to the accounting result, as the user works them out.
    ajustes_positivos: Decimal = Field(default=ZERO, ge=0)
    ajustes_negativos: Decimal = Field(default=ZERO, ge=0)

    # Art. 25: increase of equity computed by the user under art. 25.2.
    incremento_fondos_propios: Decimal = Field(default=ZERO, ge=0)
    # Increase of the average workforce over the previous period, in percent (3 = 3 %).
    incremento_plantilla_porcentaje: Decimal | None = Field(default=None, ge=0)
    # Reduction of art. 25 generated in the two previous periods and not yet applied.
    reserva_capitalizacion_pendiente: Decimal = Field(default=ZERO, ge=0)

    # Art. 26: tax losses of previous periods not yet used (positive figure).
    bases_negativas_pendientes: Decimal = Field(default=ZERO, ge=0)
    # Art. 26.3: newly created entity in one of its first 3 periods with a positive base.
    nueva_creacion_sin_limite_bases_negativas: bool = False

    dias_periodo: int = Field(default=365, ge=1, le=366)

    # Tax credits and allowances against the quota, as a total the user supplies.
    bonificaciones_deducciones: Decimal = Field(default=ZERO, ge=0)
    retenciones_ingresos_cuenta: Decimal = Field(default=ZERO, ge=0)
    pagos_fraccionados: Decimal = Field(default=ZERO, ge=0)


class ResultadoSociedades(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    # Accounting result plus/minus adjustments, before art. 25 and art. 26.
    base_imponible_previa: Decimal
    reduccion_reserva_capitalizacion: Decimal
    # Art. 25 reduction that did not fit and stays for the following periods.
    reserva_capitalizacion_pendiente: Decimal
    bases_negativas_compensadas: Decimal
    # Losses left for the following periods (including this period's, if negative).
    bases_negativas_pendientes: Decimal
    base_imponible: Decimal
    regimen: Regimen
    tipo: Decimal
    tipo_resto: Decimal | None = None
    cuota_integra: Decimal
    cuota_liquida: Decimal
    # Positive: to pay. Negative: refund.
    cuota_diferencial: Decimal
    tipo_medio: Decimal  # cuota íntegra / base imponible, in %
    lineas: list[Linea]
    avisos: list[str]
    parametros_no_verificados: list[str]
    fuente: str


def _cuota_integra(base: Decimal, tipo: _Tipo, dias: int, p: SociedadesParams, t: Traza) -> Decimal:
    tg = p.tipos_gravamen
    if base <= 0:
        t.linea("Cuota íntegra", ZERO, "art. 30.1 LIS")
        return ZERO
    if tipo.tipo_resto is None:
        cuota = _round(base * tipo.tipo)
        t.linea(f"Cuota íntegra ({_pct(tipo.tipo)})", cuota, tg.fuente)
        return cuota
    limite = tg.limite_primer_tramo
    if Decimal(dias) < tg.dias_ano:
        limite = _round(limite * Decimal(dias) / tg.dias_ano)
        t.linea(f"Primer tramo de la escala prorrateado ({dias} días de {tg.dias_ano})", limite, tg.fuente)
    primer_tramo = min(base, limite)
    cuota_primera = _round(primer_tramo * tipo.tipo)
    cuota_resto = _round((base - primer_tramo) * tipo.tipo_resto)
    t.linea(f"Cuota de los primeros {_eur(primer_tramo)} euros al {_pct(tipo.tipo)}", cuota_primera, tg.fuente)
    if base > primer_tramo:
        t.linea(f"Cuota del resto de la base al {_pct(tipo.tipo_resto)}", cuota_resto, tg.fuente)
    cuota = cuota_primera + cuota_resto
    t.linea("Cuota íntegra", cuota, "art. 30.1 LIS")
    return cuota


def calcular_sociedades(inp: SociedadesInput, params: SociedadesParams | None = None) -> ResultadoSociedades:
    p = params or load_sociedades_params(inp.ejercicio)
    t = Traza()
    rc, bn = p.reserva_capitalizacion, p.compensacion_bases_negativas
    cifra_12 = inp.cifra_12_meses
    prorrata = min(Decimal(inp.dias_periodo) / p.tipos_gravamen.dias_ano, Decimal(1))

    # 1. Accounting result -> base before reductions (art. 10.3)
    t.linea("Resultado contable antes del Impuesto sobre Sociedades", inp.resultado_contable, "art. 10.3 LIS")
    if inp.ajustes_positivos:
        t.linea("Ajustes positivos al resultado contable", inp.ajustes_positivos, "art. 10.3 LIS")
    if inp.ajustes_negativos:
        t.linea("Ajustes negativos al resultado contable", -inp.ajustes_negativos, "art. 10.3 LIS")
    previa = _round(inp.resultado_contable + inp.ajustes_positivos - inp.ajustes_negativos)
    t.linea("Base imponible previa", previa, "art. 10 LIS")
    t.aviso(
        "El resultado contable y los ajustes fiscales (gastos no deducibles, amortizaciones, deterioros, "
        "operaciones vinculadas, exenciones...) son cifras que tú aportas: el cálculo no los revisa. El resultado "
        "contable debe ser el anterior al gasto por Impuesto sobre Sociedades."
    )
    tipo = _determinar_tipo(inp, p, t)

    # 2. Capitalisation reserve (art. 25)
    reduccion_rc = ZERO
    rc_pendiente = ZERO
    derecho_rc = ZERO
    if inp.incremento_fondos_propios or inp.reserva_capitalizacion_pendiente:
        t.usa(rc.fuente)
        for aviso in rc.avisos:
            t.aviso(aviso)
        plantilla = None if inp.incremento_plantilla_porcentaje is None else inp.incremento_plantilla_porcentaje / _CIEN
        porcentaje = rc.porcentaje_por_plantilla(plantilla)
        if porcentaje != rc.porcentaje:
            t.aviso(
                f"Se aplica el {_pct(porcentaje)} de reducción por el incremento de plantilla media indicado; ese "
                f"incremento debe mantenerse {rc.anos_mantenimiento} años (art. 25.1 LIS)."
            )
        generado = _round(inp.incremento_fondos_propios * porcentaje)
        derecho_rc = generado + inp.reserva_capitalizacion_pendiente
        es_micro = cifra_12 is not None and cifra_12 < rc.limite_cifra_negocios_micro
        limite_pct = rc.limite_base_micro if es_micro else rc.limite_base
        limite = _round(max(previa, ZERO) * limite_pct)
        reduccion_rc = min(derecho_rc, limite)
        rc_pendiente = derecho_rc - reduccion_rc
        if inp.incremento_fondos_propios:
            t.linea(f"Reserva de capitalización generada ({_pct(porcentaje)} del incremento)", generado, rc.fuente)
        t.linea(f"Límite de la reducción ({_pct(limite_pct)} de la base previa)", limite, rc.fuente)
        t.linea("Reducción por reserva de capitalización", -reduccion_rc, rc.fuente)
        if rc_pendiente:
            t.aviso(
                f"Quedan {_round(rc_pendiente)} euros de reducción por reserva de capitalización sin aplicar: solo "
                f"pueden aplicarse en los períodos que finalicen en los {rc.anos_pendientes} años siguientes al "
                "cierre del período en que se generaron (art. 25.1 LIS). El cálculo no controla esa antigüedad."
            )
        if cifra_12 is None:
            t.aviso(
                f"Sin cifra de negocios, el límite de la reserva de capitalización se ha tomado al {_pct(rc.limite_base)}"
                f"; es el {_pct(rc.limite_base_micro)} si la cifra de negocios de los 12 meses anteriores al inicio "
                f"del período fue inferior a {_eur(rc.limite_cifra_negocios_micro)} euros."
            )
    tras_reserva = previa - reduccion_rc

    # 3. Losses brought forward (art. 26)
    compensadas = ZERO
    if inp.bases_negativas_pendientes and tras_reserva > 0:
        t.usa(bn.fuente)
        if inp.nueva_creacion_sin_limite_bases_negativas:
            tope = tras_reserva
            t.aviso(
                "No se aplica el límite porcentual a la compensación de bases negativas por tratarse, según indicas, "
                "de una entidad de nueva creación en uno de sus 3 primeros períodos con base positiva (art. 26.3 LIS)."
            )
        else:
            limite_pct = bn.limite(cifra_12)
            minimo = _round(bn.minimo_compensable * prorrata)
            tope = max(_round(previa * limite_pct), minimo)
            t.linea(
                f"Límite de compensación (el mayor entre el {_pct(limite_pct)} de la base previa y "
                f"{_eur(minimo)} euros)",
                tope,
                bn.fuente,
            )
            if tope > minimo and cifra_12 is None:
                t.aviso(
                    f"El límite del {_pct(bn.limite_porcentaje)} se reduce al {_pct(bn.limite_gran_empresa)} o al "
                    f"{_pct(bn.limite_gran_empresa_superior)} si la cifra de negocios de los 12 meses anteriores fue de "
                    f"al menos {_eur(bn.umbral_gran_empresa)} o {_eur(bn.umbral_gran_empresa_superior)} euros "
                    "(disposición adicional 15.ª LIS); no se ha indicado esa cifra."
                )
            if inp.dias_periodo < int(p.tipos_gravamen.dias_ano):
                t.aviso(
                    f"Período inferior al año: el importe de {_eur(bn.minimo_compensable)} euros compensable en todo "
                    f"caso se ha prorrateado por días sobre {p.tipos_gravamen.dias_ano} (art. 26.2 LIS)."
                )
        compensadas = min(inp.bases_negativas_pendientes, tras_reserva, tope)
        t.linea("Compensación de bases imponibles negativas", -compensadas, bn.fuente)
        t.aviso(
            "Las bases negativas pendientes son la cifra que aportas (deben proceder de liquidaciones o "
            "autoliquidaciones presentadas). No se comprueban las exclusiones del art. 26.4 LIS ni las reglas "
            "especiales de quitas y esperas o de extinción de la entidad."
        )
    base = tras_reserva - compensadas
    negativas_futuras = inp.bases_negativas_pendientes - compensadas + (-base if base < 0 else ZERO)
    t.linea("Base imponible", base, "art. 10.1 LIS")
    if base < 0:
        t.aviso(
            f"La base imponible es negativa: no hay cuota y {_round(-base)} euros quedan como base negativa a "
            "compensar en períodos siguientes (art. 26 LIS)."
        )

    # 4. Quota
    cuota_integra = _cuota_integra(base, tipo, inp.dias_periodo, p, t)
    deducciones = min(inp.bonificaciones_deducciones, cuota_integra)
    if inp.bonificaciones_deducciones:
        t.linea("Bonificaciones y deducciones", -deducciones, "art. 30.2 LIS")
        t.aviso(
            "Las bonificaciones y deducciones son el total que aportas: no se calculan ni se comprueban sus "
            "requisitos y límites."
        )
        if deducciones < inp.bonificaciones_deducciones:
            t.aviso(
                "Las bonificaciones y deducciones superan la cuota íntegra: la cuota líquida no puede ser negativa "
                f"(art. 30.2 LIS) y {_round(inp.bonificaciones_deducciones - deducciones)} euros no se han aplicado."
            )
    cuota_liquida = cuota_integra - deducciones
    t.linea("Cuota líquida", cuota_liquida, "art. 30.2 LIS")
    if inp.retenciones_ingresos_cuenta:
        t.linea("Retenciones e ingresos a cuenta", -inp.retenciones_ingresos_cuenta, "art. 41 LIS")
    if inp.pagos_fraccionados:
        t.linea("Pagos fraccionados", -inp.pagos_fraccionados, "art. 41 LIS")
    diferencial = _round(cuota_liquida - inp.retenciones_ingresos_cuenta - inp.pagos_fraccionados)
    t.linea("Cuota diferencial (a ingresar si es positiva, a devolver si es negativa)", diferencial, "art. 41 LIS")

    umbral_minima = p.tributacion_minima.umbral_cifra_negocios
    if cifra_12 is not None and cifra_12 >= umbral_minima:
        t.usa(p.tributacion_minima.fuente)
        t.aviso(
            f"Con una cifra de negocios de al menos {_eur(umbral_minima)} euros se aplica la tributación mínima del "
            "art. 30 bis LIS, que este cálculo NO tiene en cuenta: la cuota líquida puede ser mayor."
        )
    t.aviso(
        "No se ha aplicado la reserva de nivelación del art. 105 LIS (minoración de hasta el 10 % de la base "
        "positiva para ciertas entidades de reducida dimensión): el texto no permite determinar con exactitud a "
        "qué tipos de gravamen alcanza tras la reforma de la Ley 7/2024."
    )
    t.aviso(
        "Cálculo para sociedades de territorio común que tributan por el art. 29.1 LIS. No vale para cooperativas, "
        "entidades sin fines lucrativos, entidades de crédito, grupos fiscales, regímenes especiales ni normativa "
        "foral o de Canarias."
    )
    t.cerrar()
    return ResultadoSociedades(
        ejercicio=inp.ejercicio,
        base_imponible_previa=previa,
        reduccion_reserva_capitalizacion=_round(reduccion_rc),
        reserva_capitalizacion_pendiente=_round(rc_pendiente),
        bases_negativas_compensadas=_round(compensadas),
        bases_negativas_pendientes=_round(negativas_futuras),
        base_imponible=_round(base),
        regimen=tipo.regimen,
        tipo=tipo.tipo,
        tipo_resto=tipo.tipo_resto,
        cuota_integra=cuota_integra,
        cuota_liquida=_round(cuota_liquida),
        cuota_diferencial=diferencial,
        tipo_medio=_round(cuota_integra / base * _CIEN) if base > 0 else ZERO,
        lineas=t.lineas,
        avisos=t.avisos,
        parametros_no_verificados=t.no_verificados,
        fuente=p.fuente_texto(),
    )


# --- Instalments (modelo 202) ------------------------------------------------


class PagoFraccionadoSociedadesInput(_DatosTipo):
    """One instalment of art. 40. ``ejercicio`` is the year the current period starts."""

    # "cuota": art. 40.2, on the last period's quota. "base": art. 40.3, on the current base.
    modalidad: Literal["cuota", "base"] = "cuota"

    # Art. 40.2: figures of the last period whose filing deadline had passed on the first
    # day of the payment window.
    cuota_integra_ultimo_periodo: Decimal = Field(default=ZERO, ge=0)
    deducciones_bonificaciones_ultimo_periodo: Decimal = Field(default=ZERO, ge=0)
    retenciones_ultimo_periodo: Decimal = Field(default=ZERO, ge=0)
    # That period lasted less than a year (art. 40.2 then adds part of earlier quotas).
    ultimo_periodo_inferior_al_ano: bool = False

    # Art. 40.3: taxable base of the first 3, 9 or 11 months of the calendar year.
    base_imponible_acumulada: Decimal = ZERO
    bonificaciones: Decimal = Field(default=ZERO, ge=0)
    retenciones_acumuladas: Decimal = Field(default=ZERO, ge=0)
    pagos_fraccionados_anteriores: Decimal = Field(default=ZERO, ge=0)


class ResultadoPagoFraccionadoSociedades(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    modalidad: Literal["cuota", "base"]
    # None when the text does not allow an exact figure for this case (see ``avisos``).
    importe: Decimal | None
    porcentaje: Decimal | None  # as a fraction (0.18)
    base_pago: Decimal | None
    lineas: list[Linea]
    avisos: list[str]
    parametros_no_verificados: list[str]
    fuente: str


def calcular_pago_fraccionado_sociedades(
    inp: PagoFraccionadoSociedadesInput, params: SociedadesParams | None = None
) -> ResultadoPagoFraccionadoSociedades:
    p = params or load_sociedades_params(inp.ejercicio)
    pf = p.pago_fraccionado
    t = Traza()
    t.usa(pf.fuente)
    for aviso in pf.avisos:
        t.aviso(aviso)
    cifra_12 = inp.cifra_12_meses
    importe: Decimal | None = None
    porcentaje: Decimal | None = None
    base_pago: Decimal | None = None

    if cifra_12 is None:
        t.aviso(
            f"No se ha indicado la cifra de negocios: si en los 12 meses anteriores al inicio del período superó "
            f"{_eur(pf.limite_modalidad_base_obligatoria)} euros, la modalidad del art. 40.3 LIS (sobre la base "
            "imponible del período) es obligatoria."
        )
    obligada_base = cifra_12 is not None and cifra_12 > pf.limite_modalidad_base_obligatoria

    if inp.modalidad == "cuota":
        if obligada_base:
            t.aviso(
                f"Con una cifra de negocios superior a {_eur(pf.limite_modalidad_base_obligatoria)} euros en los 12 "
                "meses anteriores es obligatoria la modalidad del art. 40.3 LIS: no se calcula el pago por la "
                "modalidad de cuota."
            )
        elif inp.ultimo_periodo_inferior_al_ano:
            t.aviso(
                "El último período impositivo duró menos de un año: el art. 40.2 LIS obliga a sumar la parte "
                "proporcional de la cuota de períodos anteriores hasta completar 12 meses, lo que no se modela. "
                "No se da importe."
            )
        else:
            porcentaje = pf.porcentaje_cuota
            t.linea("Cuota íntegra del último período", inp.cuota_integra_ultimo_periodo, "art. 40.2 LIS")
            if inp.deducciones_bonificaciones_ultimo_periodo:
                t.linea(
                    "Deducciones y bonificaciones de ese período",
                    -inp.deducciones_bonificaciones_ultimo_periodo,
                    "art. 40.2 LIS",
                )
            if inp.retenciones_ultimo_periodo:
                t.linea(
                    "Retenciones e ingresos a cuenta de ese período", -inp.retenciones_ultimo_periodo, "art. 40.2 LIS"
                )
            base_pago = _round(
                max(
                    inp.cuota_integra_ultimo_periodo
                    - inp.deducciones_bonificaciones_ultimo_periodo
                    - inp.retenciones_ultimo_periodo,
                    ZERO,
                )
            )
            t.linea("Base del pago fraccionado", base_pago, "art. 40.2 LIS")
            importe = _round(base_pago * porcentaje)
            t.linea(f"Pago fraccionado ({_pct(porcentaje)})", importe, pf.fuente)
            t.aviso(
                "La base es la cuota del último período cuyo plazo de declaración estuviese vencido el primer día "
                "del plazo de cada pago: en abril suele ser la del penúltimo ejercicio y en octubre y diciembre la "
                "del último (art. 40.2 LIS). Las cifras son las que aportas."
            )
    else:
        tipo = _determinar_tipo(inp, p, t)
        if cifra_12 is not None and cifra_12 >= pf.umbral_especialidades:
            t.aviso(
                f"Con una cifra de negocios de al menos {_eur(pf.umbral_especialidades)} euros se aplican las "
                "especialidades de la disposición adicional 14.ª LIS (porcentaje de diecinueve veinteavos del tipo "
                "y pago mínimo sobre el resultado contable), que no se modelan. No se da importe."
            )
        elif tipo.tipo_resto is not None:
            t.aviso(
                "La sociedad tributa por la escala de dos tramos de las microempresas y el art. 40.3 LIS fija el "
                "porcentaje del pago como cinco séptimos «del tipo de gravamen», sin decir cómo se aplica a una "
                "escala. No se da importe por la modalidad de base: consulta las instrucciones del modelo 202."
            )
        else:
            # Five sevenths of the rate, in percent, rounded down to a whole point.
            puntos = (tipo.tipo * _CIEN * pf.base_numerador / pf.base_denominador).quantize(
                Decimal(1), rounding=ROUND_DOWN
            )
            porcentaje = puntos / _CIEN
            base_pago = _round(max(inp.base_imponible_acumulada, ZERO))
            t.linea("Base imponible acumulada del período", base_pago, "art. 40.3 LIS")
            cuota = _round(base_pago * porcentaje)
            t.linea(
                f"{_pct(porcentaje)} de la base (cinco séptimos del {_pct(tipo.tipo)}, redondeado por defecto)",
                cuota,
                pf.fuente,
            )
            if inp.bonificaciones:
                t.linea("Bonificaciones", -inp.bonificaciones, "art. 40.3 LIS")
            if inp.retenciones_acumuladas:
                t.linea("Retenciones e ingresos a cuenta soportados", -inp.retenciones_acumuladas, "art. 40.3 LIS")
            if inp.pagos_fraccionados_anteriores:
                t.linea(
                    "Pagos fraccionados anteriores del período", -inp.pagos_fraccionados_anteriores, "art. 40.3 LIS"
                )
            importe = _round(
                max(cuota - inp.bonificaciones - inp.retenciones_acumuladas - inp.pagos_fraccionados_anteriores, ZERO)
            )
            t.linea("Pago fraccionado", importe, pf.fuente)
            t.aviso(
                "La ley no dice a qué unidad se redondea «por defecto» el porcentaje de cinco séptimos del tipo: se "
                "ha redondeado al entero inferior. La base imponible acumulada (3, 9 u 11 primeros meses del año "
                "natural, con sus ajustes y compensaciones) es la cifra que aportas."
            )
            t.aviso(
                "La modalidad del art. 40.3 LIS es opcional (se elige en la declaración censal, en general en "
                "febrero) salvo que la cifra de negocios supere "
                f"{_eur(pf.limite_modalidad_base_obligatoria)} euros, y vincula hasta que se renuncie."
            )
    t.cerrar()
    return ResultadoPagoFraccionadoSociedades(
        ejercicio=inp.ejercicio,
        modalidad=inp.modalidad,
        importe=importe,
        porcentaje=porcentaje,
        base_pago=base_pago,
        lineas=t.lineas,
        avisos=t.avisos,
        parametros_no_verificados=t.no_verificados,
        fuente=p.fuente_texto(),
    )


# --- Self-employed vs company ------------------------------------------------


class ComparacionInput(BaseModel):
    """Same yearly profit earned as a self-employed person or through the owner's company."""

    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    # Profit of the business before paying anything to the owner.
    beneficio_antes_retribucion: Decimal = Field(ge=0)
    # Gross salary the company would pay the owner (a deductible expense for the company).
    salario: Decimal = Field(default=ZERO, ge=0)
    # Dividend distributed out of the after-tax profit. None = all of it.
    dividendo: Decimal | None = Field(default=None, ge=0)
    comunidad_autonoma: str | None = None
    # For the company's rate (see ``SociedadesInput``).
    cifra_negocios_anterior: Decimal | None = Field(default=None, ge=0)
    nueva_creacion_tipo_reducido: bool = False


class ComparacionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    # (a) Everything as self-employed income in the IRPF.
    autonomo_irpf: Decimal
    # (b) Company tax + the owner's IRPF on salary and dividend.
    sociedad_impuesto_sociedades: Decimal
    sociedad_irpf_socio: Decimal
    sociedad_total: Decimal
    # After-tax profit of the company, what is distributed and what stays in it.
    beneficio_despues_impuesto: Decimal
    dividendo: Decimal
    beneficio_retenido: Decimal
    # sociedad_total - autonomo_irpf. Only the arithmetic of the taxes modelled.
    diferencia: Decimal
    lineas: list[Linea]
    avisos: list[str]
    detalle_autonomo: IRPFResult
    detalle_sociedad: ResultadoSociedades
    detalle_irpf_socio: IRPFResult
    fuente: str


AVISOS_COMPARACION = [
    "ESTA COMPARACIÓN SOLO SUMA IMPUESTOS SOBRE LA RENTA (IRPF e Impuesto sobre Sociedades). No dice qué opción "
    "conviene: lo que se ignora a continuación puede cambiar el resultado.",
    "NO se incluyen las cotizaciones a la Seguridad Social de ninguna de las dos opciones (cuota de autónomos por "
    "rendimientos reales; RETA de socio o administrador con control de la sociedad, o Régimen General), ni su efecto "
    "como gasto deducible. Suelen ser de varios miles de euros al año y no son iguales en ambos casos.",
    "NO se aplican las reglas sobre la calificación de lo que el socio cobra de su sociedad: si presta servicios "
    "profesionales a su propia sociedad y cotiza en autónomos o en una mutualidad alternativa, esos rendimientos son "
    "de actividad económica y no del trabajo (art. 27.1 LIRPF); la retribución entre socio y sociedad debe además "
    "valorarse a mercado por ser operación vinculada, y la deducibilidad de la retribución de administradores tiene "
    "requisitos propios. Aquí el salario se trata como rendimiento del trabajo y como gasto deducible sin más.",
    "NO se considera el IVA (igual en ambos casos en lo esencial, pero con obligaciones distintas), ni los costes de "
    "constituir y mantener una sociedad (notaría, registro, contabilidad ajustada al Código de Comercio, cuentas "
    "anuales, asesoría), ni los gastos que solo son deducibles en una de las dos opciones.",
    "El beneficio que la sociedad NO reparte no tributa en el IRPF del socio hasta que se distribuya (o se venda la "
    "participación): esa tributación queda aplazada, no eliminada, y no aparece en el total de la opción sociedad. "
    "El reparto de dividendos tiene además límites mercantiles (reserva legal, pérdidas anteriores) que no se "
    "comprueban.",
    "En el IRPF solo se han usado el rendimiento indicado y la comunidad autónoma: sin otras rentas, familia, "
    "deducciones ni reducciones de la actividad (gastos de difícil justificación, etc.). Con tus datos completos "
    "las cuotas serán distintas.",
]


def comparar_autonomo_vs_sociedad(inp: ComparacionInput) -> ComparacionResult:
    """Taxes on the same profit as self-employed (a) and through a company (b). No verdict."""
    if inp.salario > inp.beneficio_antes_retribucion:
        raise ValueError("El salario no puede superar el beneficio antes de la retribución del socio.")
    t = Traza()

    # (a) Self-employed: the whole profit is net income from the activity.
    autonomo = calcular_irpf(
        IRPFInput(
            ejercicio=inp.ejercicio,
            comunidad_autonoma=inp.comunidad_autonoma,
            rendimiento_actividad_neto=inp.beneficio_antes_retribucion,
        )
    )
    t.linea("(a) Autónomo: rendimiento neto de la actividad", inp.beneficio_antes_retribucion, "art. 27 LIRPF")
    t.linea("(a) Autónomo: IRPF", autonomo.resultado, "calcular_irpf")

    # (b) Company: tax on profit minus salary; owner taxed on salary and dividend.
    sociedad = calcular_sociedades(
        SociedadesInput(
            ejercicio=inp.ejercicio,
            resultado_contable=inp.beneficio_antes_retribucion - inp.salario,
            cifra_negocios_anterior=inp.cifra_negocios_anterior,
            nueva_creacion_tipo_reducido=inp.nueva_creacion_tipo_reducido,
        )
    )
    despues = _round(inp.beneficio_antes_retribucion - inp.salario - sociedad.cuota_liquida)
    if inp.dividendo is not None and inp.dividendo > despues:
        raise ValueError(
            f"El dividendo ({inp.dividendo}) no puede superar el beneficio después del impuesto ({despues})."
        )
    dividendo = despues if inp.dividendo is None else inp.dividendo
    socio = calcular_irpf(
        IRPFInput(
            ejercicio=inp.ejercicio,
            comunidad_autonoma=inp.comunidad_autonoma,
            rendimiento_trabajo_bruto=inp.salario,
            rendimiento_capital_mobiliario=dividendo,
        )
    )
    total = _round(sociedad.cuota_liquida + socio.resultado)
    t.linea("(b) Sociedad: beneficio menos salario del socio", inp.beneficio_antes_retribucion - inp.salario, None)
    t.linea("(b) Sociedad: Impuesto sobre Sociedades", sociedad.cuota_liquida, "art. 29.1 LIS")
    t.linea("(b) Sociedad: beneficio después del impuesto", despues, None)
    t.linea("(b) Socio: salario (rendimiento del trabajo)", inp.salario, None)
    t.linea("(b) Socio: dividendo (base del ahorro)", dividendo, None)
    t.linea("(b) Socio: IRPF sobre salario y dividendo", socio.resultado, "calcular_irpf")
    t.linea("(b) Sociedad + socio: total de impuestos", total, None)
    if despues - dividendo:
        t.linea("(b) Beneficio que queda en la sociedad sin repartir", despues - dividendo, None)
    diferencia = _round(total - autonomo.resultado)
    t.linea("Diferencia (b) − (a), solo de los impuestos calculados", diferencia, None)

    for aviso in AVISOS_COMPARACION:
        t.aviso(aviso)
    for origen, avisos in (
        ("Autónomo (IRPF)", autonomo.avisos),
        ("Sociedad (IS)", sociedad.avisos),
        ("Socio (IRPF)", socio.avisos),
    ):
        for aviso in avisos:
            t.aviso(f"{origen}: {aviso}")
    return ComparacionResult(
        ejercicio=inp.ejercicio,
        autonomo_irpf=autonomo.resultado,
        sociedad_impuesto_sociedades=sociedad.cuota_liquida,
        sociedad_irpf_socio=socio.resultado,
        sociedad_total=total,
        beneficio_despues_impuesto=despues,
        dividendo=_round(dividendo),
        beneficio_retenido=_round(despues - dividendo),
        diferencia=diferencia,
        lineas=t.lineas,
        avisos=t.avisos,
        detalle_autonomo=autonomo,
        detalle_sociedad=sociedad,
        detalle_irpf_socio=socio,
        fuente=f"{sociedad.fuente}; {autonomo.fuente}",
    )
