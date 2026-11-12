"""Impuesto sobre el Patrimonio and Impuesto Temporal de Solidaridad de las
Grandes Fortunas (ITSGF) of one resident taxpayer, accrual of 31 December.

Order of the computation (Ley 19/1991 and art. 3 Ley 38/2022):

1. assets the user values, minus the exempt part of the main home (art. 4.Nueve)
   and the exempt business assets the user declares (art. 4.Ocho), minus debts
   (the main home's loan only in the non-exempt proportion, art. 25.Tres)
   = base imponible;
2. minus the mínimo exento (the community's, or the State's) = base liquidable;
3. scale (the community's, or the State's) = cuota íntegra;
4. joint limit with the IRPF (art. 31), when the IRPF figures are given;
5. the community's general allowance = cuota a ingresar;
6. ITSGF on the same base with its own minimum and scale, its own joint limit
   (IRPF quotas + the wealth-tax quota left by step 4 + the ITSGF quota, as the
   official form 718 adds them), minus the wealth tax actually payable (step 5);
7. whether a return must be filed (art. 37; art. 3.Diecinueve).

Several communities tie their allowance to the ITSGF ("the difference between
the wealth-tax quota and the ITSGF quota"), so step 5 needs the ITSGF quota of
step 6: both are computed together.

Limits, repeated as ``avisos`` on the result: one taxpayer taxed by obligación
personal in a common-regime community; values are taken as given (no valuation
rules); regional allowances for specific assets are listed, not applied.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.tax.actividad import Traza
from hacienda_gpt.tax.irpf import Linea
from hacienda_gpt.tax.params import normalizar_comunidad
from hacienda_gpt.tax.patrimonio_params import (
    BonificacionGeneral,
    ComunidadPatrimonio,
    LimiteConjunto,
    MinimoExento,
    PatrimonioParams,
    load_patrimonio_params,
)

CENT = Decimal("0.01")
ZERO = Decimal(0)
_FORALES = {
    "navarra",
    "comunidad foral de navarra",
    "pais vasco",
    "euskadi",
    "alava",
    "araba",
    "bizkaia",
    "vizcaya",
    "gipuzkoa",
    "guipuzcoa",
}


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _eur(value: Decimal) -> str:
    """1234567.5 -> '1.234.567,50 €' (Spanish format, for messages)."""
    text = f"{_round(value):,.2f}".replace(",", "#").replace(".", ",").replace("#", ".")
    return f"{text.removesuffix(',00')} €"


def _fecha(iso: str) -> str:
    """'2026-11-12' -> '12-11-2026'."""
    return "-".join(reversed(iso.split("-")))


class ComunidadNoDisponibleError(ValueError):
    """The community is not one of the fifteen common-regime ones the calculator covers."""


class PatrimonioInput(BaseModel):
    """Wealth of ONE taxpayer at 31 December. Amounts in euros, the taxpayer's share only; absent means zero."""

    model_config = ConfigDict(extra="forbid")

    ejercicio: int = 2026
    # Autonomous community of residence (key or usual name).
    comunidad: str

    # Assets, valued by the user under the tax's rules.
    vivienda_habitual: Decimal = Field(default=ZERO, ge=0)
    otros_inmuebles: Decimal = Field(default=ZERO, ge=0)
    depositos: Decimal = Field(default=ZERO, ge=0)
    valores_cotizados: Decimal = Field(default=ZERO, ge=0)
    participaciones_no_cotizadas: Decimal = Field(default=ZERO, ge=0)
    seguros_y_rentas: Decimal = Field(default=ZERO, ge=0)
    vehiculos_y_otros: Decimal = Field(default=ZERO, ge=0)
    # Business assets and family-company holdings the user states are exempt (art. 4.Ocho).
    # Not taxed; counted only for the filing threshold of art. 37.
    bienes_exentos_empresa_familiar: Decimal = Field(default=ZERO, ge=0)

    # Debts: the loan for the main home apart (deductible only in the non-exempt proportion).
    deuda_vivienda_habitual: Decimal = Field(default=ZERO, ge=0)
    otras_deudas: Decimal = Field(default=ZERO, ge=0)

    # Recognised degree of disability in percent (33, 65…); only some communities use it.
    grado_discapacidad: Decimal | None = Field(default=None, ge=0, le=100)

    # Art. 31 joint limit. Both IRPF figures or neither; None = the limit is not computed.
    # Sum of the general and savings taxable bases, and sum of the state and regional
    # cuotas íntegras, both already WITHOUT the part due to gains on assets held over a year.
    irpf_base_imponible: Decimal | None = Field(default=None, ge=0)
    irpf_cuota_integra: Decimal | None = Field(default=None, ge=0)
    # Net value, inside the taxable base, of assets that by nature cannot produce
    # income taxed by the IRPF (art. 31.Uno.b): their share of the quota stays out of the limit.
    bienes_no_productivos: Decimal = Field(default=ZERO, ge=0)


class ResultadoITSGF(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # False = net wealth does not exceed the threshold of the taxable event.
    sujeto: bool
    base_imponible: Decimal
    minimo_exento: Decimal
    base_liquidable: Decimal
    cuota_integra: Decimal
    reduccion_limite_conjunto: Decimal
    cuota_tras_limite: Decimal
    # Wealth tax actually payable, deducted up to the quota (art. 3.Quince).
    deduccion_patrimonio: Decimal
    cuota_a_ingresar: Decimal
    obligado_a_declarar: bool


class ResultadoPatrimonio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    comunidad: str
    comunidad_nombre: str
    # False = the community has not legislated on the tax: state rules throughout.
    comunidad_regula: bool
    # Which rule set each step used: "estatal" or "autonomica".
    origen_minimo_exento: str
    origen_escala: str

    # All assets, exempt ones included, before debts (the figure art. 37 looks at).
    bienes_y_derechos: Decimal
    exencion_vivienda_habitual: Decimal
    bienes_exentos_empresa_familiar: Decimal
    deudas_deducibles: Decimal
    base_imponible: Decimal
    minimo_exento: Decimal
    base_liquidable: Decimal
    cuota_integra: Decimal
    # None = not computed because the IRPF figures were not given.
    reduccion_limite_conjunto: Decimal | None
    cuota_tras_limite: Decimal
    bonificacion_autonomica: Decimal
    cuota_a_ingresar: Decimal
    obligado_a_declarar: bool
    motivo_obligacion: str

    # None = the ITSGF is not in force for this year.
    itsgf: ResultadoITSGF | None
    # Wealth tax plus ITSGF.
    total_a_ingresar: Decimal

    # Whether the community's figures used here also applied to the accrual of 31-12-2025.
    aplicable_2025: bool
    diferencias_2025: list[str]
    # Regional rules that exist and were NOT applied (article and title).
    no_modelado: list[str]
    lineas: list[Linea]
    avisos: list[str]
    parametros_no_verificados: list[str]
    fuente: str


def _comunidad(nombre: str, p: PatrimonioParams) -> tuple[str, ComunidadPatrimonio]:
    found = p.comunidad(nombre)
    if found is not None:
        return found
    if normalizar_comunidad(nombre) in _FORALES:
        raise ComunidadNoDisponibleError(
            "Navarra y los territorios del País Vasco tienen su propio Impuesto sobre el Patrimonio (régimen foral), "
            "que esta calculadora no cubre."
        )
    disponibles = ", ".join(sorted(p.comunidades))
    raise ComunidadNoDisponibleError(f"Comunidad autónoma no reconocida: «{nombre}». Disponibles: {disponibles}.")


def _minimo_exento(
    inp: PatrimonioInput, comunidad: ComunidadPatrimonio, p: PatrimonioParams, t: Traza
) -> tuple[Decimal, MinimoExento, str]:
    """Amount, the group it came from, and "estatal"/"autonomica"."""
    propio = comunidad.minimo_exento
    grado = None if inp.grado_discapacidad is None else inp.grado_discapacidad / 100
    if propio is not None and grado is not None:
        aplicables = [m for m in propio.discapacidad if grado >= m.grado_desde]
        if aplicables:
            t.aviso(
                f"Se ha aplicado el mínimo exento de {comunidad.nombre} para contribuyentes con discapacidad "
                f"({propio.fuente.referencia}); exige tener reconocido legalmente ese grado."
            )
            return max(m.importe for m in aplicables), propio, "autonomica"
    if grado is not None and grado > 0 and (propio is None or not propio.discapacidad):
        t.aviso(f"{comunidad.nombre} no establece un mínimo exento distinto por discapacidad: se aplica el general.")
    if propio is not None and propio.importe is not None:
        return propio.importe, propio, "autonomica"
    return p.estado.minimo_exento.importe or ZERO, p.estado.minimo_exento, "estatal"


def _reduccion_limite(
    cuotas_previas: Decimal, cuota: Decimal, computable: Decimal, limite: Decimal, regla: LimiteConjunto
) -> Decimal:
    """How much ``cuota`` is cut so that previous quotas + its computable part fit under ``limite``.

    The cut is capped at ``reduccion_maxima`` of the whole quota.
    """
    exceso = cuotas_previas + computable - limite
    if exceso <= 0:
        return ZERO
    return _round(min(exceso, cuota * regla.reduccion_maxima))


def _bonificacion(
    regla: BonificacionGeneral | None,
    cuota: Decimal,
    cuota_itsgf: Decimal | None,
    base_imponible: Decimal,
    comunidad: ComunidadPatrimonio,
    t: Traza,
    limitada: bool = False,
) -> tuple[Decimal, str]:
    """Regional general allowance on the wealth-tax quota, and the text of its trace line.

    ``cuota`` and ``cuota_itsgf`` are both the quotas left by their joint limits
    (boxes 40 of form 714 and 200 of form 718); ``limitada`` says the wealth-tax
    one was actually cut by art. 31.
    """
    if regla is None or cuota <= 0:
        return ZERO, ""
    porcentaje = regla.porcentaje or ZERO
    if regla.regimen_itsgf == "ninguno" or cuota_itsgf is None:
        # Also the fallback if the ITSGF were not in force (the parameters forbid that combination).
        return _round(cuota * porcentaje), f"Bonificación general de {comunidad.nombre} ({porcentaje:.0%} de la cuota)"
    if regla.regimen_itsgf == "diferencia":
        if regla.umbral_base_itsgf is not None and regla.minimo_descontado is not None:
            if base_imponible - regla.minimo_descontado <= regla.umbral_base_itsgf:
                return _round(cuota * porcentaje), (
                    f"Bonificación general de {comunidad.nombre} ({porcentaje:.0%} de la cuota; patrimonio neto menos "
                    f"{_eur(regla.minimo_descontado)} no superior a {_eur(regla.umbral_base_itsgf)})"
                )
        t.aviso(
            f"{comunidad.nombre}: mientras esté vigente el ITSGF la bonificación autonómica es la diferencia entre la "
            "cuota íntegra de Patrimonio, una vez aplicado su límite conjunto, y la cuota íntegra del ITSGF, una vez "
            "aplicado el suyo y antes de deducir Patrimonio. Si no hay cuota de ITSGF se bonifica toda la cuota; si la "
            "hay, se ingresa por Patrimonio a la comunidad lo que en otro caso se pagaría al Estado por el ITSGF "
            f"({regla.fuente.referencia})."
        )
        return _round(max(ZERO, cuota - cuota_itsgf)), (
            f"Bonificación de {comunidad.nombre}: cuota de Patrimonio menos cuota del ITSGF ({_eur(cuota_itsgf)})"
        )
    # "porcentaje_menos_itsgf": the percentage allowance, less the ITSGF that would be payable with it.
    plena = cuota * porcentaje
    itsgf_a_pagar = max(ZERO, cuota_itsgf - (cuota - plena))
    t.aviso(
        f"{comunidad.nombre}: la bonificación del {porcentaje:.0%} se reduce en el importe que resultaría a pagar por "
        f"el ITSGF ({regla.fuente.referencia}). Ese importe depende a su vez de la bonificación y la norma no lo precisa: "
        "se ha tomado el ITSGF que resultaría con la bonificación íntegra, de modo que lo que faltaría por ITSGF se "
        "ingresa por Patrimonio. Con la otra lectura posible el total a pagar entre los dos impuestos es el mismo y "
        "solo cambia su reparto entre la comunidad y el Estado."
    )
    if limitada:
        t.aviso(
            f"{comunidad.nombre}: la norma aplica la bonificación «sobre la cuota íntegra» sin decir si es la anterior o "
            "la posterior al límite conjunto con el IRPF; se ha aplicado sobre la cuota ya reducida por el límite "
            "(lectura prudente)."
        )
    return _round(max(ZERO, plena - itsgf_a_pagar)), (
        f"Bonificación de {comunidad.nombre}: {porcentaje:.0%} de la cuota ({_eur(plena)}) menos ITSGF a pagar ({_eur(itsgf_a_pagar)})"
    )


def calcular_patrimonio(inp: PatrimonioInput, params: PatrimonioParams | None = None) -> ResultadoPatrimonio:
    """Wealth tax and ITSGF of one taxpayer; raises ``EjercicioNoDisponibleError`` or ``ComunidadNoDisponibleError``."""
    p = params or load_patrimonio_params(inp.ejercicio)
    key, comunidad = _comunidad(inp.comunidad, p)
    e = p.estado
    t = Traza()

    # 1. Base imponible.
    gravables = (
        inp.otros_inmuebles
        + inp.depositos
        + inp.valores_cotizados
        + inp.participaciones_no_cotizadas
        + inp.seguros_y_rentas
        + inp.vehiculos_y_otros
    )
    bienes = inp.vivienda_habitual + gravables + inp.bienes_exentos_empresa_familiar
    exencion_vivienda = min(inp.vivienda_habitual, e.vivienda_habitual.limite_exento)
    t.linea("Bienes y derechos (incluidos los exentos)", bienes, "arts. 9 a 24 Ley 19/1991")
    if exencion_vivienda:
        t.linea("Exención de la vivienda habitual", -exencion_vivienda, e.vivienda_habitual.fuente)
    if inp.bienes_exentos_empresa_familiar:
        t.linea(
            "Bienes y participaciones exentos declarados (empresa familiar)",
            -inp.bienes_exentos_empresa_familiar,
            "art. 4.Ocho Ley 19/1991",
        )
        t.aviso(
            "La exención de bienes afectos y participaciones en empresas familiares (art. 4.Ocho de la Ley 19/1991) se "
            "ha tomado por el importe que indica el usuario: la calculadora no comprueba sus requisitos."
        )
    deuda_vivienda = ZERO
    if inp.deuda_vivienda_habitual:
        if inp.vivienda_habitual > 0:
            proporcion = (inp.vivienda_habitual - exencion_vivienda) / inp.vivienda_habitual
            deuda_vivienda = _round(inp.deuda_vivienda_habitual * proporcion)
            if deuda_vivienda < inp.deuda_vivienda_habitual:
                t.aviso(
                    "La deuda por la adquisición de la vivienda habitual solo se deduce en la proporción en que la "
                    f"vivienda no está exenta: {_eur(deuda_vivienda)} de {_eur(inp.deuda_vivienda_habitual)} "
                    "(art. 25.Tres de la Ley 19/1991)."
                )
        else:
            t.aviso(
                "Se ha indicado una deuda de la vivienda habitual sin valor de la vivienda: no se deduce. Si "
                "corresponde a otro bien no exento, indíquela en otras_deudas."
            )
    deudas = deuda_vivienda + inp.otras_deudas
    if deudas:
        t.linea("Deudas deducibles", -deudas, "arts. 9 y 25 Ley 19/1991")
    neto = inp.vivienda_habitual - exencion_vivienda + gravables - deudas
    base_imponible = max(ZERO, neto)
    if neto < 0:
        t.aviso("Las deudas superan a los bienes no exentos: la base imponible se toma como cero.")
    t.linea("Base imponible (patrimonio neto)", base_imponible, "art. 9 Ley 19/1991")

    # 2. Base liquidable.
    minimo, grupo_minimo, origen_minimo = _minimo_exento(inp, comunidad, p, t)
    t.linea(
        f"Mínimo exento ({'estatal' if origen_minimo == 'estatal' else comunidad.nombre})", -minimo, grupo_minimo.fuente
    )
    base_liquidable = max(ZERO, base_imponible - minimo)
    t.linea("Base liquidable", base_liquidable, "art. 28 Ley 19/1991")

    # 3. Cuota íntegra.
    escala = comunidad.escala or e.escala
    origen_escala = "autonomica" if comunidad.escala else "estatal"
    cuota_integra = _round(escala.cuota(base_liquidable))
    t.linea(
        f"Cuota íntegra (escala {'estatal' if origen_escala == 'estatal' else 'de ' + comunidad.nombre})",
        cuota_integra,
        escala.fuente,
    )

    # 4. Joint limit with the IRPF (art. 31).
    con_irpf = inp.irpf_base_imponible is not None and inp.irpf_cuota_integra is not None
    if (inp.irpf_base_imponible is None) != (inp.irpf_cuota_integra is None):
        t.aviso(
            "Para el límite conjunto renta-patrimonio hacen falta las dos cifras del IRPF (suma de bases imponibles y "
            "suma de cuotas íntegras): se ha dado solo una y el límite no se ha calculado."
        )
    proporcion_productiva = Decimal(1)
    if base_imponible > 0 and inp.bienes_no_productivos:
        proporcion_productiva = 1 - min(inp.bienes_no_productivos, base_imponible) / base_imponible
    limite = ZERO
    irpf_cuota = ZERO
    reduccion: Decimal | None = None
    if con_irpf:
        assert inp.irpf_base_imponible is not None and inp.irpf_cuota_integra is not None
        irpf_cuota = inp.irpf_cuota_integra
        limite = inp.irpf_base_imponible * e.limite_conjunto.porcentaje_base_irpf
        computable = cuota_integra * proporcion_productiva
        reduccion = _reduccion_limite(irpf_cuota, cuota_integra, computable, limite, e.limite_conjunto)
        if reduccion:
            t.linea(
                f"Reducción por límite conjunto renta-patrimonio ({e.limite_conjunto.porcentaje_base_irpf:.0%} de la base "
                f"del IRPF = {_eur(limite)})",
                -reduccion,
                e.limite_conjunto.fuente,
            )
        t.aviso(
            "Límite conjunto del art. 31 de la Ley 19/1991 calculado con las cifras del IRPF indicadas, que deben "
            "excluir la parte de base del ahorro y de cuota que corresponda a ganancias de elementos con más de un año "
            "de antigüedad. No se contempla la tributación conjunta."
        )
        if inp.bienes_no_productivos and cuota_integra:
            t.aviso(
                "El art. 31.Uno.b) deja fuera del límite «la parte del Impuesto sobre el Patrimonio que corresponda» a "
                "los elementos que no pueden producir rendimientos gravados por el IRPF, sin fijar cómo se calcula: se ha "
                "prorrateado la cuota íntegra por el valor neto de esos elementos sobre la base imponible "
                f"({_round((1 - proporcion_productiva) * 100):.2f}".replace(".", ",")
                + " % de la cuota), con los datos que piden los modelos 714 y 718."
            )
        if not inp.bienes_no_productivos and cuota_integra:
            t.aviso(
                "Se ha supuesto que todos los elementos patrimoniales pueden producir rendimientos gravados por el IRPF. "
                "La parte de cuota de los que no pueden (art. 31.Uno.b) no cuenta para el límite: indíquelos en "
                "bienes_no_productivos."
            )
    elif cuota_integra:
        t.aviso(
            "No se ha aplicado el límite conjunto renta-patrimonio (art. 31 de la Ley 19/1991) porque faltan las cifras "
            "del IRPF: la suma de las cuotas del IRPF y del Patrimonio no puede superar el "
            f"{e.limite_conjunto.porcentaje_base_irpf:.0%} de la base imponible del IRPF, con una reducción máxima del "
            f"{e.limite_conjunto.reduccion_maxima:.0%} de la cuota de Patrimonio. Con rentas bajas la cuota puede ser menor."
        )
    cuota_tras_limite = cuota_integra - (reduccion or ZERO)

    # 6 (first half). ITSGF quota: needed before the regional allowance.
    g = p.itsgf
    vigente = g.vigencia.vigente
    itsgf_sujeto = False
    itsgf_minimo = itsgf_base_liquidable = itsgf_cuota = itsgf_reduccion = itsgf_tras_limite = ZERO
    # Same limit under the other reading of art. 3.Doce (only the part of the wealth-tax quota open to limitation).
    itsgf_reduccion_alternativa = ZERO
    if vigente:
        itsgf_sujeto = base_imponible > g.hecho_imponible.umbral_patrimonio_neto
        # The threshold decides whether there is ITSGF at all, also when the answer is "no".
        t.usa(g.hecho_imponible.fuente)
        if itsgf_sujeto:
            itsgf_minimo = g.minimo_exento.importe or ZERO
            itsgf_base_liquidable = max(ZERO, base_imponible - itsgf_minimo)
            itsgf_cuota = _round(g.escala.cuota(itsgf_base_liquidable))
            if con_irpf and itsgf_cuota:
                # Wealth-tax quota that counts: the whole quota left by its own limit (box 152 of form 718
                # = box 40 of form 714), plus the part of the ITSGF quota open to limitation (box 153).
                itsgf_reduccion = _reduccion_limite(
                    irpf_cuota + cuota_tras_limite,
                    itsgf_cuota,
                    itsgf_cuota * proporcion_productiva,
                    limite,
                    g.limite_conjunto,
                )
                itsgf_reduccion_alternativa = _reduccion_limite(
                    irpf_cuota + max(ZERO, cuota_integra * proporcion_productiva - (reduccion or ZERO)),
                    itsgf_cuota,
                    itsgf_cuota * proporcion_productiva,
                    limite,
                    g.limite_conjunto,
                )
            itsgf_tras_limite = itsgf_cuota - itsgf_reduccion

    # 5. Regional general allowance.
    bonificacion, texto_bonificacion = _bonificacion(
        comunidad.bonificacion,
        cuota_tras_limite,
        itsgf_tras_limite if vigente else None,
        base_imponible,
        comunidad,
        t,
        limitada=bool(reduccion),
    )
    if comunidad.bonificacion is not None and texto_bonificacion:
        t.linea(texto_bonificacion, -bonificacion, comunidad.bonificacion.fuente)
    cuota_a_ingresar = cuota_tras_limite - bonificacion
    t.linea("Impuesto sobre el Patrimonio: cuota a ingresar", cuota_a_ingresar, "Ley 19/1991")

    # 6 (second half). ITSGF payable.
    itsgf: ResultadoITSGF | None = None
    itsgf_a_ingresar = ZERO
    if vigente:
        deduccion = min(itsgf_tras_limite, cuota_a_ingresar)
        itsgf_a_ingresar = itsgf_tras_limite - deduccion
        if itsgf_sujeto:
            t.linea(
                "ITSGF: base liquidable (base imponible menos mínimo exento)",
                itsgf_base_liquidable,
                g.minimo_exento.fuente,
            )
            t.linea("ITSGF: cuota íntegra", itsgf_cuota, g.escala.fuente)
            if itsgf_reduccion:
                t.linea(
                    "ITSGF: reducción por límite conjunto con IRPF y Patrimonio",
                    -itsgf_reduccion,
                    g.limite_conjunto.fuente,
                )
            if deduccion:
                t.linea(
                    "ITSGF: deducción de la cuota del Impuesto sobre el Patrimonio",
                    -deduccion,
                    g.deduccion_patrimonio.fuente,
                )
            t.linea("ITSGF: cuota a ingresar", itsgf_a_ingresar, "art. 3 Ley 38/2022")
            t.usa(g.vigencia.fuente)
            t.aviso(
                "El Impuesto Temporal de Solidaridad de las Grandes Fortunas sigue aplicándose por la prórroga de la "
                "disposición adicional 5.ª del Real Decreto-ley 8/2023 («en tanto no se produzca la revisión de la "
                "tributación patrimonial»), sin fecha de fin: los textos consolidados consultados el "
                f"{_fecha(p.fuentes_principales[g.vigencia.evidencia[0].boe_id].consultado_el)} no recogen esa revisión; si se "
                "aprobara antes del 31 de diciembre, este cálculo cambiaría. Se calcula con las exenciones estatales y "
                "su propio mínimo exento, y de su cuota se deduce la del Impuesto sobre el Patrimonio efectivamente "
                "satisfecha (la cuota a ingresar, después de bonificaciones autonómicas)."
            )
            if con_irpf and itsgf_cuota:
                t.aviso(
                    "En el límite conjunto del ITSGF (art. 3.Doce) la ley suma «las cuotas» del IRPF y de Patrimonio sin "
                    "precisar cuál de las de Patrimonio. Se ha computado, como hace el modelo 718 (casilla 152), la total "
                    "cuota íntegra de Patrimonio ya reducida por su propio límite y por entero, antes de bonificaciones "
                    "autonómicas."
                )
                if itsgf_reduccion_alternativa != itsgf_reduccion:
                    t.aviso(
                        "Al haber elementos improductivos, ese criterio del modelo 718 importa: si en la suma solo se "
                        "contara la parte de la cuota de Patrimonio susceptible de limitación (lectura estricta del art. "
                        "31.Uno.b), la cuota del ITSGF tras el límite sería de "
                        f"{_eur(itsgf_cuota - itsgf_reduccion_alternativa)} en lugar de {_eur(itsgf_tras_limite)}. "
                        "Conviene contrastarlo con el programa de la Agencia Tributaria."
                    )
        itsgf = ResultadoITSGF(
            sujeto=itsgf_sujeto,
            base_imponible=_round(base_imponible),
            minimo_exento=_round(itsgf_minimo),
            base_liquidable=_round(itsgf_base_liquidable),
            cuota_integra=_round(itsgf_cuota),
            reduccion_limite_conjunto=_round(itsgf_reduccion),
            cuota_tras_limite=_round(itsgf_tras_limite),
            deduccion_patrimonio=_round(deduccion),
            cuota_a_ingresar=_round(itsgf_a_ingresar),
            obligado_a_declarar=itsgf_a_ingresar > 0,
        )

    # 7. Filing obligation (art. 37).
    umbral = e.obligacion_declarar.umbral_bienes_derechos
    t.usa(e.obligacion_declarar.fuente)
    if cuota_a_ingresar > 0:
        obligado, motivo = True, "La cuota resulta a ingresar (art. 37 de la Ley 19/1991)."
    elif bienes > umbral:
        obligado = True
        motivo = (
            f"Aunque no hay cuota a ingresar, el valor de los bienes y derechos ({_eur(bienes)}) supera {_eur(umbral)} "
            "(art. 37 de la Ley 19/1991)."
        )
    else:
        obligado = False
        motivo = f"No hay cuota a ingresar y el valor de los bienes y derechos no supera {_eur(umbral)} (art. 37 de la Ley 19/1991)."
    if cuota_a_ingresar <= 0:
        t.aviso(
            f"Para el umbral de {_eur(umbral)} de la obligación de declarar se ha tomado el valor de todos los bienes y "
            "derechos indicados, incluidos los exentos y sin restar deudas (criterio prudente; el art. 37 habla del «valor "
            "de sus bienes o derechos, determinado de acuerdo con las normas reguladoras del impuesto», no del patrimonio "
            "neto, y no dice si cuentan los exentos). Los demás bienes exentos que no se piden aquí (ajuar doméstico, "
            "planes de pensiones, etc.) no están sumados: con el mismo criterio prudente, súmelos si se acerca al umbral."
        )

    # Standing caveats.
    t.aviso(
        "Cálculo para un solo contribuyente residente (obligación personal), con los valores que indica el usuario: no "
        "se aplican las reglas de valoración de cada bien (arts. 10 a 24 de la Ley 19/1991), ni la deducción por "
        "impuestos pagados en el extranjero, ni la bonificación de Ceuta y Melilla."
    )
    if not comunidad.regula:
        t.aviso(
            f"{comunidad.nombre} no ha regulado el Impuesto sobre el Patrimonio: se aplican íntegramente las reglas estatales."
        )
    for nota in comunidad.notas:
        t.aviso(nota)
    if comunidad.no_modelado:
        t.aviso(
            f"{comunidad.nombre} tiene además beneficios propios que NO se han aplicado: "
            + "; ".join(comunidad.no_modelado)
            + "."
        )
    t.cerrar()

    boe_ids = [e.escala.evidencia[0].boe_id]
    if vigente:
        boe_ids.append(g.escala.evidencia[0].boe_id)
    boe_ids.append(comunidad.boe_id)
    return ResultadoPatrimonio(
        ejercicio=inp.ejercicio,
        comunidad=key,
        comunidad_nombre=comunidad.nombre,
        comunidad_regula=comunidad.regula,
        origen_minimo_exento=origen_minimo,
        origen_escala=origen_escala,
        bienes_y_derechos=_round(bienes),
        exencion_vivienda_habitual=_round(exencion_vivienda),
        bienes_exentos_empresa_familiar=_round(inp.bienes_exentos_empresa_familiar),
        deudas_deducibles=_round(deudas),
        base_imponible=_round(base_imponible),
        minimo_exento=_round(minimo),
        base_liquidable=_round(base_liquidable),
        cuota_integra=_round(cuota_integra),
        reduccion_limite_conjunto=None if reduccion is None else _round(reduccion),
        cuota_tras_limite=_round(cuota_tras_limite),
        bonificacion_autonomica=_round(bonificacion),
        cuota_a_ingresar=_round(cuota_a_ingresar),
        obligado_a_declarar=obligado,
        motivo_obligacion=motivo,
        itsgf=itsgf,
        total_a_ingresar=_round(cuota_a_ingresar + itsgf_a_ingresar),
        aplicable_2025=p.aplicable_2025 and comunidad.aplicable_2025,
        diferencias_2025=list(comunidad.diferencias_2025),
        no_modelado=list(comunidad.no_modelado),
        lineas=t.lineas,
        avisos=t.avisos,
        parametros_no_verificados=t.no_verificados,
        fuente=p.fuente_texto(*boe_ids),
    )
