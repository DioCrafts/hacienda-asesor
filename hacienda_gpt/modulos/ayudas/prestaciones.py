"""Deterministic evaluation of the stable public benefits (family A) for one household.

Each evaluator returns an :class:`Evaluacion`: a verdict, the amount when the law fixes it, a trace of
lines (concept, amount, legal reference), the literal quotes it rests on, and — explicitly — the
requirements that could NOT be checked with the profile. A benefit is never proposed on a guess: what
rests on estimated data is "probable", what lacks data is "faltan_datos".
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.modulos.ayudas.hogar import PRESTACIONES, Hogar
from hacienda_gpt.modulos.ayudas.parametros import Grupo, PrestacionesParams

Estado = Literal["cumple", "probable", "automatica", "proximamente", "ya_solicitada", "faltan_datos", "no_cumple"]
# States worth telling the user about (and notifying).
ESTADOS_PROPONIBLES: frozenset[str] = frozenset({"cumple", "probable", "automatica", "proximamente"})
ORDEN_ESTADOS: dict[str, int] = {
    "cumple": 0,
    "probable": 1,
    "proximamente": 2,
    "automatica": 3,
    "faltan_datos": 4,
    "ya_solicitada": 5,
    "no_cumple": 6,
}
CENT = Decimal("0.01")


def eur(importe: Decimal) -> str:
    """1234.5 -> '1.234,50 €'."""
    texto = f"{importe.quantize(CENT, ROUND_HALF_UP):,.2f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def pct(fraccion: Decimal) -> str:
    valor = (fraccion * 100).normalize()
    return f"{valor:f}".replace(".", ",") + " %"


def fecha(d: date) -> str:
    return d.strftime("%d/%m/%Y")


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Linea(_Model):
    concepto: str
    importe: Decimal | None = None
    referencia: str = ""


class Cita(_Model):
    boe_id: str
    articulo: str
    cita: str


class Evaluacion(_Model):
    id: str
    nombre: str
    estado: Estado
    motivo: str
    importe_anual: Decimal | None = None
    importe_mensual: Decimal | None = None
    # When the benefit is not money (a discount, a title) or the amount is not computed.
    importe_texto: str = ""
    lineas: list[Linea] = Field(default_factory=list)
    requisitos_comprobados: list[str] = Field(default_factory=list)
    requisitos_no_comprobados: list[str] = Field(default_factory=list)
    faltan: list[str] = Field(default_factory=list)
    plazo: str = ""
    vence: date | None = None
    como_solicitar: str = ""
    citas: list[Cita] = Field(default_factory=list)
    avisos: list[str] = Field(default_factory=list)
    fuentes: list[str] = Field(default_factory=list)

    @property
    def proponible(self) -> bool:
        return self.estado in ESTADOS_PROPONIBLES


def _citas(grupo: Grupo, *articulos: str) -> list[Cita]:
    """The group's quotes whose article starts with any of ``articulos`` (all when none given)."""
    return [
        Cita(boe_id=ev.boe_id, articulo=ev.articulo, cita=ev.cita)
        for ev in grupo.evidencia
        if not articulos or any(ev.articulo.startswith(a) for a in articulos)
    ]


def _fuentes(p: PrestacionesParams, *boe_ids: str) -> list[str]:
    return [f"{p.fuentes_principales[b].norma}: {p.fuentes_principales[b].url}" for b in boe_ids]


def _ya(id_: str, h: Hogar) -> Evaluacion | None:
    if id_ in h.ya_solicitadas:
        return Evaluacion(
            id=id_,
            nombre=PRESTACIONES[id_],
            estado="ya_solicitada",
            motivo="Consta en tu perfil que ya la cobras o la has solicitado («ayudas_ya_solicitadas»).",
        )
    return None


def _estimado(h: Hogar, *, ingresos: bool = True, base: bool = False) -> bool:
    return h.composicion_estimada or (ingresos and h.ingresos_estimados) or (base and h.base_imponible_estimada)


# --- Ingreso mínimo vital and complemento de ayuda para la infancia -----------


def renta_garantizada_mensual(p: PrestacionesParams, h: Hogar) -> tuple[Decimal, list[Linea]]:
    imv = p.imv
    base = imv.renta_garantizada_individual
    assert h.adultos is not None and h.menores is not None
    escala = imv.escala(h.adultos, h.menores)
    lineas = [
        Linea(
            concepto=f"Renta garantizada de una persona: pensión no contributiva de {p.ejercicio} / 12",
            importe=base,
            referencia="art. 13.2.a) Ley 19/2021; art. 2.5 RDL 3/2026",
        )
    ]
    total = (base * escala).quantize(CENT, ROUND_HALF_UP)
    if escala != 1:
        lineas.append(
            Linea(
                concepto=f"Escala por {h.adultos} adulto(s) y {h.menores} menor(es): {str(escala).replace('.', ',')}",
                importe=total,
                referencia="art. 13.2.b) Ley 19/2021",
            )
        )
    if monoparental_imv(h):
        extra = (base * imv.complemento_monoparental).quantize(CENT, ROUND_HALF_UP)
        total += extra
        lineas.append(
            Linea(concepto="Complemento de monoparentalidad", importe=extra, referencia="art. 13.2.c) Ley 19/2021")
        )
    if h.discapacidad_65:
        extra = (base * imv.complemento_discapacidad).quantize(CENT, ROUND_HALF_UP)
        total += extra
        lineas.append(
            Linea(
                concepto="Complemento por persona con discapacidad igual o superior al 65 %",
                importe=extra,
                referencia="art. 13.2.a) y d) Ley 19/2021",
            )
        )
    lineas.append(Linea(concepto="Renta garantizada mensual del hogar", importe=total, referencia="art. 13.2"))
    return total, lineas


def monoparental_imv(h: Hogar) -> bool:
    return bool(h.adultos == 1 and (h.menores or 0) >= 1 and not h.conyuge)


def _faltan_composicion_ingresos(h: Hogar) -> list[str]:
    faltan: list[str] = []
    if h.adultos is None:
        faltan.append("ayudas_adultos_hogar")
    if h.menores is None:
        faltan.append("ayudas_menores_hogar (o las edades de los hijos en «descendientes»)")
    if h.ingresos is None:
        faltan.append("ayudas_ingresos_hogar")
    return faltan


def evaluar_imv(p: PrestacionesParams, h: Hogar) -> Evaluacion:
    ya = _ya("imv", h)
    if ya:
        return ya
    imv = p.imv
    citas = _citas(imv)
    comunes: dict[str, Any] = dict(
        id="imv",
        nombre=PRESTACIONES["imv"],
        citas=citas,
        fuentes=_fuentes(p, "BOE-A-2021-21007", "BOE-A-2026-2548"),
        como_solicitar="Ante el Instituto Nacional de la Seguridad Social (art. 25 Ley 19/2021), por su sede electrónica.",
        plazo="Sin plazo de solicitud: el derecho nace el primer día del mes siguiente a la solicitud (art. 14.1), "
        "así que cada mes de retraso se pierde.",
        avisos=list(imv.avisos),
    )
    faltan = _faltan_composicion_ingresos(h)
    if faltan:
        return Evaluacion(
            estado="faltan_datos",
            motivo="Para comprobar el ingreso mínimo vital faltan datos del hogar.",
            faltan=faltan,
            **comunes,
        )
    assert h.adultos is not None and h.menores is not None and h.ingresos is not None
    if h.edad is not None and h.edad < int(imv.edad_minima_titular) and not (h.menores and h.edad >= 18):
        return Evaluacion(
            estado="no_cumple",
            motivo=f"La persona titular debe tener al menos {imv.edad_minima_titular} años salvo excepciones "
            "(art. 5.2 Ley 19/2021: hijos a cargo, víctimas de violencia de género o trata, extutelados).",
            **comunes,
        )
    garantizada, lineas = renta_garantizada_mensual(p, h)
    mensual = (h.ingresos / 12).quantize(CENT, ROUND_HALF_UP)
    lineas.append(
        Linea(
            concepto="Ingresos mensuales del hogar del año anterior (ingresos anuales / 12)"
            + (" (estimados)" if h.ingresos_estimados else ""),
            importe=mensual,
            referencia="arts. 11.2 y 20 Ley 19/2021",
        )
    )
    comprobados = [
        f"Ingresos mensuales ({eur(mensual)}) frente a la renta garantizada ({eur(garantizada)}), con el margen de "
        f"{eur(imv.margen_minimo)} del art. 11.2."
    ]
    no_comprobados = [
        "Residencia legal y efectiva en España durante al menos el año anterior a la solicitud (art. 10.1.a).",
        "Que la unidad de convivencia esté constituida al menos seis meses antes de la solicitud (art. 10.3).",
        "Que ningún miembro sea administrador de derecho de una sociedad mercantil en activo (art. 11.3).",
        "Cómputo exacto de las rentas del art. 20 (se restan el IRPF devengado y las cotizaciones; hay rentas "
        "exentas que no cuentan).",
    ]
    if monoparental_imv(h):
        no_comprobados.append("Guarda y custodia exclusiva de los menores, para el complemento de monoparentalidad.")
    if mensual + imv.margen_minimo > garantizada:
        return Evaluacion(
            estado="no_cumple",
            motivo=f"Los ingresos mensuales del hogar ({eur(mensual)}) no quedan al menos {eur(imv.margen_minimo)} "
            f"por debajo de la renta garantizada ({eur(garantizada)}).",
            lineas=lineas,
            requisitos_comprobados=comprobados,
            requisitos_no_comprobados=no_comprobados,
            **comunes,
        )
    limite_patrimonio = (
        imv.patrimonio_veces * imv.renta_garantizada_individual * 12 * imv.factor_patrimonio(h.adultos, h.menores)
    ).quantize(CENT, ROUND_HALF_UP)
    if h.patrimonio is not None and h.patrimonio >= limite_patrimonio:
        return Evaluacion(
            estado="no_cumple",
            motivo=f"El patrimonio neto declarado ({eur(h.patrimonio)}) alcanza el límite de {eur(limite_patrimonio)} "
            "(art. 11.3 y anexo II).",
            lineas=lineas,
            requisitos_comprobados=comprobados,
            requisitos_no_comprobados=no_comprobados,
            **comunes,
        )
    if h.patrimonio is None:
        no_comprobados.append(
            f"Patrimonio neto sin la vivienda habitual inferior a {eur(limite_patrimonio)} y test de activos "
            "(art. 11.3, anexos II y III): indica «ayudas_patrimonio_neto_hogar»."
        )
    else:
        comprobados.append(f"Patrimonio neto ({eur(h.patrimonio)}) inferior a {eur(limite_patrimonio)}.")
    importe = garantizada - mensual
    lineas.append(Linea(concepto="Prestación mensual estimada", importe=importe, referencia="art. 13.1"))
    return Evaluacion(
        estado="probable" if _estimado(h) or h.patrimonio is None else "cumple",
        motivo=f"Los ingresos del hogar quedan por debajo de la renta garantizada: unos {eur(importe)} al mes.",
        importe_mensual=importe,
        importe_anual=(importe * 12).quantize(CENT),
        lineas=lineas,
        requisitos_comprobados=comprobados,
        requisitos_no_comprobados=no_comprobados,
        **comunes,
    )


def evaluar_capi(p: PrestacionesParams, h: Hogar) -> Evaluacion:
    ya = _ya("capi", h)
    if ya:
        return ya
    capi, imv = p.capi, p.imv
    comunes: dict[str, Any] = dict(
        id="capi",
        nombre=PRESTACIONES["capi"],
        citas=_citas(capi),
        fuentes=_fuentes(p, "BOE-A-2021-21007", "BOE-A-2022-22685"),
        como_solicitar="Ante el Instituto Nacional de la Seguridad Social, como el ingreso mínimo vital "
        "(art. 25 Ley 19/2021); puede pedirse aunque no se tenga derecho al ingreso mínimo vital (art. 11.6).",
        plazo="Sin plazo de solicitud.",
        avisos=list(capi.avisos),
    )
    if h.menores == 0:
        return Evaluacion(estado="no_cumple", motivo="No hay menores de edad en el hogar.", **comunes)
    faltan = _faltan_composicion_ingresos(h)
    if faltan:
        return Evaluacion(
            estado="faltan_datos",
            motivo="Para comprobar el complemento de ayuda para la infancia faltan datos del hogar.",
            faltan=faltan,
            **comunes,
        )
    assert h.adultos is not None and h.menores is not None and h.ingresos is not None
    umbral_anexo = (imv.renta_garantizada_individual * imv.escala(h.adultos, h.menores) * 12).quantize(CENT)
    limite = (umbral_anexo * capi.umbral_ingresos).quantize(CENT)
    lineas = [
        Linea(
            concepto="Renta garantizada anual del hogar según la escala del anexo I",
            importe=umbral_anexo,
            referencia="art. 13.2.b) y anexo I Ley 19/2021",
        ),
        Linea(
            concepto=f"Límite de ingresos: {pct(capi.umbral_ingresos)} de ese umbral",
            importe=limite,
            referencia="art. 11.6 Ley 19/2021",
        ),
        Linea(
            concepto="Ingresos anuales del hogar del año anterior" + (" (estimados)" if h.ingresos_estimados else ""),
            importe=h.ingresos,
            referencia="art. 20 Ley 19/2021",
        ),
    ]
    no_comprobados = [
        "Cuantía mensual por menor en 2026 (no se ha podido leer: la ley fija 100, 70 y 50 euros por tramo de edad, "
        "pero se actualizaron desde 2023).",
        "Residencia legal y efectiva en España (art. 10.1.a Ley 19/2021).",
    ]
    if h.ingresos >= limite:
        return Evaluacion(
            estado="no_cumple",
            motivo=f"Los ingresos del hogar ({eur(h.ingresos)}) no son inferiores al límite de {eur(limite)}.",
            lineas=lineas,
            requisitos_no_comprobados=no_comprobados,
            **comunes,
        )
    limite_patrimonio = (
        imv.patrimonio_veces * imv.renta_garantizada_individual * 12 * imv.factor_patrimonio(h.adultos, h.menores)
    ) * capi.umbral_patrimonio
    limite_patrimonio = limite_patrimonio.quantize(CENT, ROUND_HALF_UP)
    if h.patrimonio is not None and h.patrimonio >= limite_patrimonio:
        return Evaluacion(
            estado="no_cumple",
            motivo=f"El patrimonio neto ({eur(h.patrimonio)}) no es inferior al límite de {eur(limite_patrimonio)}.",
            lineas=lineas,
            requisitos_no_comprobados=no_comprobados,
            **comunes,
        )
    if h.patrimonio is None:
        no_comprobados.append(
            f"Patrimonio neto inferior a {eur(limite_patrimonio)} y test de activos (art. 11.6): indica "
            "«ayudas_patrimonio_neto_hogar»."
        )
    tramos = _tramos_capi(p, h)
    return Evaluacion(
        estado="probable" if _estimado(h) or h.patrimonio is None else "cumple",
        motivo=f"Los ingresos del hogar ({eur(h.ingresos)}) son inferiores al límite de {eur(limite)} para "
        f"{h.menores} menor(es).",
        importe_texto="Cuantía mensual por cada menor; el importe de 2026 no se ha podido leer en el BOE. " + tramos,
        lineas=lineas,
        requisitos_comprobados=[f"Ingresos inferiores a {eur(limite)}."],
        requisitos_no_comprobados=no_comprobados,
        **comunes,
    )


def _tramos_capi(p: PrestacionesParams, h: Hogar) -> str:
    capi = p.capi
    edades = [hijo.edad for hijo in h.hijos if hijo.edad is not None and hijo.edad < 18]
    if not edades:
        return ""
    partes = []
    for edad in edades:
        if edad < 3:
            partes.append(f"menor de 3 años: {eur(capi.cuantia_ley_menor_3)}/mes según la ley")
        elif edad < 6:
            partes.append(f"de 3 a 5 años: {eur(capi.cuantia_ley_3_a_6)}/mes según la ley")
        else:
            partes.append(f"de 6 a 17 años: {eur(capi.cuantia_ley_6_a_18)}/mes según la ley")
    return (
        "Tramos del art. 13.2.e) (edad a 1 de enero; importes del texto de la ley, no de 2026): "
        + "; ".join(partes)
        + "."
    )


# --- Asignación por hijo con discapacidad -------------------------------------


def evaluar_hijo_discapacidad(p: PrestacionesParams, h: Hogar) -> Evaluacion:
    ya = _ya("hijo_discapacidad", h)
    if ya:
        return ya
    g = p.hijo_discapacidad
    comunes: dict[str, Any] = dict(
        id="hijo_discapacidad",
        nombre=PRESTACIONES["hijo_discapacidad"],
        citas=_citas(g),
        fuentes=_fuentes(p, "BOE-A-2015-11724", "BOE-A-2026-2548"),
        como_solicitar="Ante la Seguridad Social (Instituto Nacional de la Seguridad Social).",
        plazo="Sin plazo, pero el derecho nace el primer día del trimestre natural siguiente a la solicitud "
        "(art. 355.2.a LGSS): cada trimestre de retraso se pierde.",
    )
    edad_limite = int(g.edad_limite)
    lineas: list[Linea] = []
    total = Decimal(0)
    for hijo in h.hijos:
        grado = Decimal(hijo.grado_discapacidad) / 100
        if hijo.edad is None or grado <= 0:
            continue
        if hijo.edad < edad_limite and grado >= g.grado_menor:
            importe, texto = (
                g.importe_menor,
                f"menor de {edad_limite} años con discapacidad de al menos {pct(g.grado_menor)}",
            )
        elif hijo.edad >= edad_limite and grado >= g.grado_asistencia and hijo.necesita_asistencia:
            importe = g.importe_mayor_75
            texto = f"mayor de {edad_limite} años con al menos {pct(g.grado_asistencia)} y ayuda de otra persona"
        elif hijo.edad >= edad_limite and grado >= g.grado_mayor:
            importe, texto = g.importe_mayor_65, f"mayor de {edad_limite} años con al menos {pct(g.grado_mayor)}"
        else:
            continue
        total += importe
        lineas.append(Linea(concepto=f"Hijo {hijo.numero}: {texto}", importe=importe, referencia="art. 2.6 RDL 3/2026"))
    if not lineas:
        if not h.hijos and h.descendientes_discapacidad:
            return Evaluacion(
                estado="faltan_datos",
                motivo="Hay una discapacidad en el hogar pero no consta el detalle de los hijos (edad y grado).",
                faltan=["descendientes (edad o fecha de nacimiento y grado de discapacidad de cada hijo)"],
                **comunes,
            )
        return Evaluacion(
            estado="no_cumple",
            motivo="No consta ningún hijo con el grado de discapacidad exigido (33 % si es menor; 65 % si es mayor de edad).",
            **comunes,
        )
    lineas.append(Linea(concepto="Total anual", importe=total, referencia="art. 353 LGSS"))
    return Evaluacion(
        estado="cumple",
        motivo=f"Por la discapacidad de {len(lineas) - 1} hijo(s): {eur(total)} al año, sin límite de ingresos.",
        importe_anual=total,
        lineas=lineas,
        requisitos_comprobados=["Edad y grado de discapacidad de cada hijo según el perfil."],
        requisitos_no_comprobados=[
            "Residencia legal en España del solicitante y del hijo (art. 352.1 LGSS).",
            "Que el hijo conviva y esté a cargo (si trabaja, que sus rendimientos no superen el salario mínimo).",
            "Que ningún progenitor tenga una prestación de la misma naturaleza en otro régimen (art. 352.1.c).",
            "Que el hijo no sea pensionista no contributivo de invalidez o jubilación (art. 361.3).",
            "Para la cuantía del 75 %, que el baremo reconozca la necesidad del concurso de otra persona (art. 354).",
        ],
        **comunes,
    )


# --- Prestación por nacimiento o adopción -------------------------------------


def _sumar_anios(d: date, anios: int) -> date:
    try:
        return d.replace(year=d.year + anios)
    except ValueError:  # 29 February
        return d.replace(year=d.year + anios, day=28)


def evaluar_nacimiento(p: PrestacionesParams, h: Hogar, grado_progenitor: int | None) -> Evaluacion:
    ya = _ya("nacimiento", h)
    if ya:
        return ya
    g = p.nacimiento
    anios = int(g.prescripcion_anios)
    comunes: dict[str, Any] = dict(
        id="nacimiento",
        nombre=PRESTACIONES["nacimiento"],
        citas=_citas(g),
        fuentes=_fuentes(p, "BOE-A-2015-11724", "BOE-A-2026-2548"),
        como_solicitar="Ante la Seguridad Social (Instituto Nacional de la Seguridad Social).",
    )
    recientes = [
        hijo
        for hijo in h.hijos
        if hijo.fecha_nacimiento is not None and _sumar_anios(hijo.fecha_nacimiento, anios) >= h.hoy
    ]
    if not recientes:
        sin_fecha = [
            hijo for hijo in h.hijos if hijo.fecha_nacimiento is None and hijo.edad is not None and hijo.edad < anios
        ]
        if sin_fecha:
            return Evaluacion(
                estado="faltan_datos",
                motivo=f"Hay hijos de menos de {anios} años sin fecha de nacimiento en el perfil.",
                faltan=["fecha de nacimiento de cada hijo en «descendientes»"],
                **comunes,
            )
        return Evaluacion(
            estado="no_cumple",
            motivo=f"No consta ningún nacimiento o adopción de los últimos {anios} años (plazo de prescripción, art. 53.1 LGSS).",
            **comunes,
        )
    supuestos: list[str] = []
    if h.familia_numerosa:
        supuestos.append("familia numerosa")
    if h.familia_monoparental:
        supuestos.append("familia monoparental")
    if grado_progenitor is not None and Decimal(grado_progenitor) / 100 >= g.grado_progenitor:
        supuestos.append(f"progenitor con discapacidad de al menos {pct(g.grado_progenitor)}")
    if not supuestos:
        return Evaluacion(
            estado="no_cumple",
            motivo="Solo se reconoce a familias numerosas, monoparentales o con madre o padre con discapacidad de al "
            "menos el 65 % (art. 357.1 LGSS), y el perfil no indica ninguno de esos supuestos.",
            **comunes,
        )
    if h.ingresos is None:
        return Evaluacion(
            estado="faltan_datos",
            motivo="Falta saber los ingresos anuales del hogar para comprobar el límite.",
            faltan=["ayudas_ingresos_hogar"],
            **comunes,
        )
    a_cargo = sum(
        1 for hijo in h.hijos if hijo.edad is not None and (hijo.edad < 18 or hijo.grado_discapacidad >= 65)
    ) or len(recientes)
    limite = g.limite(a_cargo, bool(h.familia_numerosa)).quantize(CENT, ROUND_HALF_UP)
    lineas = [
        Linea(
            concepto=f"Límite de ingresos {p.ejercicio} con {a_cargo} hijo(s) a cargo"
            + (" (familia numerosa)" if h.familia_numerosa else ""),
            importe=limite,
            referencia="art. 357.3 LGSS; art. 2.7 RDL 3/2026",
        ),
        Linea(
            concepto="Ingresos anuales del hogar" + (" (estimados)" if h.ingresos_estimados else ""),
            importe=h.ingresos,
            referencia="art. 357.3 LGSS",
        ),
    ]
    if h.ingresos <= limite:
        por_hijo = g.importe
    elif h.ingresos < limite + g.importe and limite + g.importe - h.ingresos >= g.diferencia_minima:
        por_hijo = (limite + g.importe - h.ingresos).quantize(CENT)
    else:
        return Evaluacion(
            estado="no_cumple",
            motivo=f"Los ingresos ({eur(h.ingresos)}) superan el límite de {eur(limite)} en {eur(g.importe)} o más.",
            lineas=lineas,
            **comunes,
        )
    total = por_hijo * len(recientes)
    for hijo in recientes:
        assert hijo.fecha_nacimiento is not None
        lineas.append(
            Linea(
                concepto=f"Hijo {hijo.numero}, nacido el {fecha(hijo.fecha_nacimiento)}: pago único",
                importe=por_hijo,
                referencia="art. 358 LGSS",
            )
        )
    vence = min(_sumar_anios(hijo.fecha_nacimiento, anios) for hijo in recientes if hijo.fecha_nacimiento)
    no_comprobados = [
        "Que la condición (" + ", ".join(supuestos) + ") existiera en la fecha del nacimiento o la adopción.",
        "Residencia legal en España y no tener derecho a una prestación análoga en otro régimen (art. 352.1 a y c).",
        "Que el nacimiento o la adopción se produjera en España (art. 357.1).",
    ]
    if any(hijo.fecha_nacimiento and hijo.fecha_nacimiento.year != p.ejercicio for hijo in recientes):
        no_comprobados.append(
            f"Para nacimientos de años anteriores rige el límite de ingresos de ese año (aquí se usa el de {p.ejercicio})."
        )
    if h.familia_monoparental:
        no_comprobados.append("Que seas el sustentador único de la familia (art. 357.2).")
    return Evaluacion(
        estado=(
            "probable"
            if h.ingresos_estimados
            or any(hijo.fecha_nacimiento and hijo.fecha_nacimiento.year != p.ejercicio for hijo in recientes)
            else "cumple"
        ),
        motivo=f"Nacimiento en una {supuestos[0]} con ingresos dentro del límite: {eur(por_hijo)} por hijo.",
        importe_anual=total,
        lineas=lineas,
        requisitos_comprobados=[f"Supuesto: {', '.join(supuestos)}.", f"Ingresos dentro del límite de {eur(limite)}."],
        requisitos_no_comprobados=no_comprobados,
        plazo=f"El derecho prescribe a los {anios} años del nacimiento (art. 53.1 LGSS): hasta el {fecha(vence)}.",
        vence=vence,
        **comunes,
    )


# --- Bono social eléctrico y térmico ------------------------------------------


def _circunstancias(h: Hogar) -> list[str]:
    out = []
    if h.discapacidad_33:
        out.append("discapacidad de al menos el 33 % en el hogar")
    if h.victima_violencia_genero:
        out.append("víctima de violencia de género")
    if h.victima_terrorismo:
        out.append("víctima de terrorismo")
    if h.dependencia_grado_2_3:
        out.append("dependencia reconocida de grado II o III")
    if h.adultos == 1 and (h.menores or 0) >= 1 and not h.conyuge:
        out.append("un único progenitor con al menos un menor")
    if h.electrodependencia:
        out.append("electrodependencia")
    return out


def evaluar_bono_social_electrico(p: PrestacionesParams, h: Hogar) -> Evaluacion:
    ya = _ya("bono_social_electrico", h)
    if ya:
        return ya
    b, iprem = p.bono_social_electrico, p.iprem
    vigentes = h.hoy <= b.descuentos_hasta
    d_vul = b.descuento_vulnerable if vigentes else b.descuento_vulnerable_general
    d_sev = b.descuento_severo if vigentes else b.descuento_severo_general
    fin_anio = date(h.hoy.year, 12, 31)
    comunes: dict[str, Any] = dict(
        id="bono_social_electrico",
        nombre=PRESTACIONES["bono_social_electrico"],
        citas=_citas(b) + _citas(iprem, "disp. adic."),
        fuentes=_fuentes(p, "BOE-A-2017-11505", "BOE-A-2017-11532", "BOE-A-2026-6544", "BOE-A-2022-22128"),
        como_solicitar="Ante una comercializadora de referencia, con contrato al PVPC (art. 7.2 RD 897/2017).",
        plazo=f"Sin plazo; pero si lo tienes concedido a 31 de diciembre ({fecha(fin_anio)}) cobrarás además el bono "
        "social térmico del año siguiente (art. 8 RDL 15/2018). Se concede por dos años renovables (art. 9.2).",
        vence=fin_anio,
        avisos=list(b.avisos) + list(iprem.avisos),
    )
    anual = iprem.anual_14_pagas
    no_comprobados = [
        "Ser titular del contrato de luz de la vivienda habitual (art. 3.1 RD 897/2017).",
        "Consentimiento de los miembros mayores de 14 años para comprobar los datos (art. 7.4).",
    ]
    if h.pvpc is None:
        no_comprobados.append("Tener el contrato en el PVPC con una comercializadora de referencia (art. 3.1).")
    elif h.pvpc is False:
        no_comprobados.append(
            "Tendrías que pasar tu contrato al PVPC con una comercializadora de referencia (art. 3.1)."
        )
    lineas: list[Linea] = []
    rutas: list[tuple[str, bool]] = []  # (route, severe)
    if h.familia_numerosa:
        severo = h.base_imponible is not None and h.base_imponible <= anual * b.severo_familia_numerosa
        rutas.append(("título de familia numerosa (art. 3.2.b)", severo))
    if "imv" in h.ya_solicitadas:
        rutas.append(("beneficiario del ingreso mínimo vital (art. 3.2.d)", False))
    if h.pensionistas_cuantia_minima:
        severo = h.base_imponible is not None and h.base_imponible <= anual * b.severo_pensionista
        rutas.append(("pensionistas con la cuantía mínima (art. 3.2.c)", severo))
        no_comprobados.append(
            f"Que los pensionistas no tengan otros ingresos de más de {eur(b.otros_ingresos_pensionista)} al año."
        )
    renta_faltan: list[str] = []
    if h.adultos is None or h.menores is None:
        renta_faltan.append("ayudas_adultos_hogar y ayudas_menores_hogar")
    if h.base_imponible is None:
        renta_faltan.append("ayudas_base_imponible_hogar")
    if not renta_faltan:
        assert h.adultos is not None and h.menores is not None and h.base_imponible is not None
        mult = b.multiplicador_base + b.incremento_adulto * max(0, h.adultos - 1) + b.incremento_menor * h.menores
        circ = _circunstancias(h)
        if circ:
            mult += b.incremento_circunstancias
        umbral = (mult * anual).quantize(CENT)
        lineas = [
            Linea(
                concepto=f"IPREM anual de 14 pagas ({eur(iprem.mensual)} × 14)",
                importe=anual,
                referencia="DA 90.ª Ley 31/2022",
            ),
            Linea(
                concepto=f"Multiplicador {str(mult).replace('.', ',')}: {h.adultos} adulto(s), {h.menores} menor(es)"
                + (f", más 1 por {circ[0]}" if circ else ""),
                importe=umbral,
                referencia="art. 3.2.a) y 3.3 RD 897/2017",
            ),
            Linea(
                concepto="Base imponible general y del ahorro del hogar"
                + (" (estimada)" if h.base_imponible_estimada else ""),
                importe=h.base_imponible,
                referencia="art. 4.1 Orden ETU/943/2017",
            ),
        ]
        if h.base_imponible <= umbral:
            rutas.append(("renta (art. 3.2.a)", h.base_imponible <= umbral * b.severo_fraccion))
    if not rutas:
        if renta_faltan:
            return Evaluacion(
                estado="faltan_datos",
                motivo="Para comprobar el límite de renta del bono social faltan datos.",
                faltan=renta_faltan,
                requisitos_no_comprobados=no_comprobados,
                **comunes,
            )
        return Evaluacion(
            estado="no_cumple",
            motivo="La renta del hogar supera el umbral y no consta familia numerosa, pensionistas con la mínima ni "
            "ingreso mínimo vital.",
            lineas=lineas,
            requisitos_no_comprobados=no_comprobados,
            **comunes,
        )
    severo = any(s for _, s in rutas)
    descuento = d_sev if severo else d_vul
    por_dato_declarado = any(r.startswith("título") or r.startswith("beneficiario") for r, _ in rutas)
    estimado = _estimado(h, ingresos=False, base=True) and not por_dato_declarado
    return Evaluacion(
        estado="probable" if estimado or h.pvpc is not True else "cumple",
        motivo=f"Consumidor {'vulnerable severo' if severo else 'vulnerable'} por "
        + "; ".join(r for r, _ in rutas)
        + ".",
        importe_texto=f"Descuento del {pct(descuento)} sobre el PVPC"
        + (
            f" hasta el {fecha(b.descuentos_hasta)} (después, {pct(b.descuento_severo_general if severo else b.descuento_vulnerable_general)})"
            if vigentes
            else ""
        )
        + ", con un límite anual de energía.",
        lineas=lineas,
        requisitos_comprobados=["Vía: " + "; ".join(r for r, _ in rutas) + "."],
        requisitos_no_comprobados=no_comprobados,
        **comunes,
    )


def evaluar_bono_social_termico(p: PrestacionesParams, h: Hogar, electrico: Evaluacion) -> Evaluacion:
    t = p.bono_social_termico
    comunes: dict[str, Any] = dict(
        id="bono_social_termico",
        nombre=PRESTACIONES["bono_social_termico"],
        citas=_citas(t),
        fuentes=_fuentes(p, "BOE-A-2018-13593", "BOE-A-2026-6544"),
        como_solicitar="No se solicita: lo paga tu comunidad autónoma a quien tenga el bono social eléctrico a 31 de "
        "diciembre del año anterior (arts. 8 y 10 RDL 15/2018).",
        importe_texto=f"Pago único anual de al menos {eur(t.ayuda_minima)} ({pct(t.incremento_severo)} más si eres "
        "vulnerable severo); el importe depende de la zona climática y del presupuesto.",
        avisos=list(t.avisos),
    )
    fin_anio = date(h.hoy.year, 12, 31)
    if electrico.estado == "ya_solicitada":
        return Evaluacion(
            estado="automatica",
            motivo=f"Si mantienes el bono social eléctrico a {fecha(fin_anio)}, recibirás el térmico de {h.hoy.year + 1} sin pedirlo.",
            **comunes,
        )
    if electrico.estado in ("cumple", "probable"):
        return Evaluacion(
            estado="probable",
            motivo=f"Si consigues el bono social eléctrico antes del {fecha(fin_anio)}, cobrarás también el térmico de {h.hoy.year + 1}.",
            plazo=f"Ten concedido el bono social eléctrico a {fecha(fin_anio)}.",
            vence=fin_anio,
            requisitos_no_comprobados=["Los mismos que el bono social eléctrico."],
            **comunes,
        )
    return Evaluacion(
        estado="faltan_datos" if electrico.estado == "faltan_datos" else "no_cumple",
        motivo="Solo lo reciben quienes tienen el bono social eléctrico a 31 de diciembre.",
        **comunes,
    )


# --- Título de familia monoparental (RDL 35/2026) ------------------------------


def evaluar_titulo_monoparental(p: PrestacionesParams, h: Hogar) -> Evaluacion:
    ya = _ya("titulo_monoparental", h)
    if ya:
        return ya
    t = p.titulo_monoparental
    desde = t.solicitable_desde
    comunes: dict[str, Any] = dict(
        id="titulo_monoparental",
        nombre=PRESTACIONES["titulo_monoparental"],
        citas=_citas(t),
        fuentes=_fuentes(p, "BOE-A-2026-23604"),
        como_solicitar="En tu comunidad autónoma de residencia (art. 5.2 RDL 35/2026).",
        avisos=list(t.avisos),
    )
    sola = h.familia_monoparental or (h.adultos == 1 and (h.menores or 0) >= 1 and not h.conyuge)
    if not sola:
        return Evaluacion(
            estado="no_cumple",
            motivo="No consta que seas la única persona progenitora con hijos menores a cargo.",
            **comunes,
        )
    especial = len([x for x in h.hijos if x.edad is not None and x.edad < int(t.edad_hijos)]) >= 2 or bool(
        h.discapacidad_33
    )
    categoria = (
        "especial (dos o más hijos o discapacidad)"
        if especial
        else "general (o especial si los ingresos por miembro no superan el 150 % del IPREM)"
    )
    return Evaluacion(
        estado="proximamente" if h.hoy < desde else "probable",
        motivo=f"Título estatal de familia monoparental, categoría {categoria}; se podrá pedir desde el {fecha(desde)}.",
        importe_texto="No es una ayuda en dinero: acredita la condición para descuentos, prioridad en becas y "
        "límites de renta corregidos (arts. 2, 6 y 7 RDL 35/2026).",
        requisitos_no_comprobados=[
            "Guarda y custodia de los hijos y que dependan económicamente de ti (art. 3.1).",
            "Que el Congreso convalide el Real Decreto-ley 35/2026.",
        ],
        plazo=f"Solicitable desde el {fecha(desde)}.",
        vence=desde,
        **comunes,
    )


# --- All of them ----------------------------------------------------------------


def evaluar_todas(p: PrestacionesParams, h: Hogar, grado_progenitor: int | None = None) -> list[Evaluacion]:
    electrico = evaluar_bono_social_electrico(p, h)
    evaluadores: list[Callable[[], Evaluacion]] = [
        lambda: evaluar_imv(p, h),
        lambda: evaluar_capi(p, h),
        lambda: evaluar_hijo_discapacidad(p, h),
        lambda: evaluar_nacimiento(p, h, grado_progenitor),
        lambda: electrico,
        lambda: evaluar_bono_social_termico(p, h, electrico),
        lambda: evaluar_titulo_monoparental(p, h),
    ]
    return [evaluar() for evaluar in evaluadores]
