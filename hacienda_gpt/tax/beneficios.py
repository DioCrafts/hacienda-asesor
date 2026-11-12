"""Catalogue of state-level IRPF tax benefits, checked against a profile.

This is the "what could lower my tax?" list. For each benefit it says whether
the simulation already applies it, whether it is available and worth how much,
or what must be confirmed first. Amounts come from running the calculator on a
"what if" scenario — never from a formula duplicated here.

Regional (autonomous-community) deductions are deliberately NOT enumerated:
there are hundreds, they change yearly, and listing them from memory would be
exactly the kind of invented law this project exists to avoid. The catalogue
returns a pointer telling the advisor to look them up in the corpus.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from hacienda_gpt.tax.autonomicas import campo_que_la_activa
from hacienda_gpt.tax.irpf import ZERO, IRPFInput, IRPFResult, calcular_irpf
from hacienda_gpt.tax.params import Fuente, IRPFParams, load_params, normalizar_comunidad

Estado = Literal["aplicado", "disponible", "por_confirmar", "no_aplica"]


class Beneficio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    nombre: str
    estado: Estado
    detalle: str
    requisitos: list[str]
    referencia: str
    verificado: bool
    # Estimated tax saving from acting on it, when the calculator can tell.
    ahorro_estimado: Decimal | None = None
    datos_que_faltan: list[str] = []


def _importe_linea(result: IRPFResult, prefix: str) -> Decimal:
    return sum((-line.importe for line in result.lineas if line.concepto.startswith(prefix)), ZERO)


def _ahorro(base: IRPFResult, inp: IRPFInput, params: IRPFParams, **cambios: Any) -> Decimal:
    alternativa = calcular_irpf(inp.model_copy(update=cambios), params)
    return base.resultado - alternativa.resultado


def evaluar_beneficios(inp: IRPFInput, *, conocidos: set[str], params: IRPFParams | None = None) -> list[Beneficio]:
    """Check each benefit against the scenario.

    ``conocidos`` are the input fields the user actually provided: a zero the
    user stated ("no tengo hijos") is different from a zero nobody asked about.
    """
    p = params or load_params(inp.ejercicio)
    base = calcular_irpf(inp, p)
    out: list[Beneficio] = []

    def add(
        id: str,
        nombre: str,
        estado: Estado,
        detalle: str,
        requisitos: list[str],
        fuente: Fuente | str,
        *,
        ahorro: Decimal | None = None,
        faltan: list[str] | None = None,
    ) -> None:
        out.append(
            Beneficio(
                id=id,
                nombre=nombre,
                estado=estado,
                detalle=detalle,
                requisitos=requisitos,
                referencia=fuente.referencia if isinstance(fuente, Fuente) else fuente,
                verificado=fuente.verificado if isinstance(fuente, Fuente) else True,
                ahorro_estimado=ahorro,
                datos_que_faltan=faltan or [],
            )
        )

    def faltan(*campos: str) -> list[str]:
        return [campo for campo in campos if campo not in conocidos]

    # --- Pension plans: the main year-end lever ---------------------------
    ps = p.prevision_social
    margen = max(ZERO, ps.limite_general - inp.aportacion_plan_pensiones)
    if inp.rendimiento_trabajo_bruto or inp.rendimiento_actividad_neto:
        if margen > 0:
            ahorro = _ahorro(base, inp, p, aportacion_plan_pensiones=ps.limite_general)
            add(
                "plan_pensiones",
                "Aportación a plan de pensiones",
                "disponible" if ahorro > 0 else "no_aplica",
                (
                    f"Puedes aportar hasta {margen:.0f} € más este ejercicio (límite individual de "
                    f"{ps.limite_general:.0f} €) y reducir tu base imponible."
                    if ahorro > 0
                    else "Aportar no reduciría tu cuota (ya es cero o no hay base que reducir)."
                ),
                [
                    "La aportación debe hacerse dentro del año natural.",
                    "La reducción no puede superar el 30 % de tus rendimientos netos del trabajo y de actividades.",
                    "El dinero queda ilíquido hasta la jubilación u otros supuestos tasados, y tributa como "
                    "rendimiento del trabajo cuando se rescata: es un diferimiento, no una exención.",
                ],
                ps.fuente,
                ahorro=ahorro if ahorro > 0 else None,
            )
        else:
            add(
                "plan_pensiones",
                "Aportación a plan de pensiones",
                "aplicado",
                "Ya has agotado el límite individual de reducción.",
                ["El límite puede ser mayor con contribuciones de tu empresa a un plan de empleo (art. 52.1 LIRPF)."],
                ps.fuente,
            )

    # --- Joint filing -------------------------------------------------------
    tc = p.tributacion_conjunta
    if inp.tributacion != "individual":
        add(
            "tributacion_conjunta",
            "Tributación conjunta",
            "aplicado",
            f"Reducción aplicada: {_importe_linea(base, 'Reducción por tributación conjunta'):.2f} €.",
            ["Deben incluirse las rentas de todos los miembros de la unidad familiar."],
            tc.fuente,
        )
    else:
        add(
            "tributacion_conjunta",
            "Tributación conjunta",
            "por_confirmar",
            (
                f"Si estás casado o tienes hijos a tu cargo sin convivir con el otro progenitor, la declaración "
                f"conjunta reduce la base en {tc.reduccion_biparental:.0f} € (matrimonio) o "
                f"{tc.reduccion_monoparental:.0f} € (monoparental), pero suma las rentas de toda la unidad familiar. "
                "Suele compensar solo cuando el otro miembro tiene rentas muy bajas: hay que simular ambas opciones."
            ),
            [
                "Matrimonio no separado legalmente con, en su caso, hijos menores; o padre/madre con todos los hijos "
                "que convivan con él (art. 82 LIRPF).",
                "La reducción monoparental no se aplica si convives con el otro progenitor.",
            ],
            tc.fuente,
            faltan=faltan("estado_civil"),
        )

    # --- Family minimums ----------------------------------------------------
    m = p.minimos
    if inp.num_descendientes:
        add(
            "minimo_descendientes",
            "Mínimo por descendientes",
            "aplicado",
            f"Aplicado por {inp.num_descendientes} descendiente(s): {-_importe_linea(base, 'Mínimo por descendientes'):.0f} € de mínimo.",
            [
                "Menores de 25 años (o con discapacidad), que convivan contigo y sin rentas superiores a 8.000 €.",
                "Si ambos progenitores tienen derecho, se reparte al 50 %.",
            ],
            m.fuente,
        )
    elif "num_descendientes" not in conocidos:
        add(
            "minimo_descendientes",
            "Mínimo por descendientes",
            "por_confirmar",
            f"Si tienes hijos a tu cargo, el mínimo exento sube ({m.descendientes[0]:.0f} € por el primero, "
            f"{m.descendientes[1]:.0f} € por el segundo; {m.incremento_descendiente_menor_3:.0f} € más por cada menor de tres años).",
            ["Menores de 25 años (o con discapacidad), que convivan contigo y sin rentas superiores a 8.000 €."],
            m.fuente,
            faltan=["num_descendientes", "num_descendientes_menores_3"],
        )
    if inp.num_ascendientes_mayores_65:
        add(
            "minimo_ascendientes",
            "Mínimo por ascendientes",
            "aplicado",
            f"Aplicado por {inp.num_ascendientes_mayores_65} ascendiente(s).",
            [
                "Mayores de 65 años (o con discapacidad), que convivan contigo al menos medio año y sin rentas superiores a 8.000 €."
            ],
            m.fuente,
        )
    elif "num_ascendientes_mayores_65" not in conocidos:
        add(
            "minimo_ascendientes",
            "Mínimo por ascendientes",
            "por_confirmar",
            f"Si convives con padres o abuelos mayores de 65 años a tu cargo, suma {m.ascendiente:.0f} € de mínimo por "
            f"cada uno ({m.incremento_ascendiente_mayor_75:.0f} € más si supera los 75).",
            ["Que conviva contigo y no tenga rentas anuales superiores a 8.000 €."],
            m.fuente,
            faltan=["num_ascendientes_mayores_65"],
        )
    if inp.grado_discapacidad >= 33:
        add(
            "minimo_discapacidad",
            "Mínimo por discapacidad",
            "aplicado",
            f"Aplicado para un grado del {inp.grado_discapacidad} %.",
            ["Grado de discapacidad reconocido igual o superior al 33 %."],
            m.fuente,
        )

    # --- Work income --------------------------------------------------------
    reduccion_trabajo = _importe_linea(base, "Reducción por obtención de rendimientos del trabajo")
    if reduccion_trabajo:
        add(
            "reduccion_trabajo",
            "Reducción por rendimientos del trabajo",
            "aplicado",
            f"Reducción aplicada: {reduccion_trabajo:.2f} €.",
            ["Rendimientos netos del trabajo inferiores al umbral y otras rentas no superiores a 6.500 €."],
            p.trabajo.fuente,
        )
    deduccion_trabajo = _importe_linea(base, "Deducción por obtención de rendimientos del trabajo")
    if deduccion_trabajo and p.deduccion_rendimientos_trabajo is not None:
        add(
            "deduccion_trabajo",
            "Deducción por rendimientos del trabajo (salarios bajos)",
            "aplicado",
            f"Deducción aplicada: {deduccion_trabajo:.2f} €.",
            ["Rendimientos de una relación laboral o estatutaria con prestación efectiva de servicios."],
            p.deduccion_rendimientos_trabajo.fuente,
        )

    # --- Deductions from tax -----------------------------------------------
    d = p.donativos
    if inp.donativos:
        add(
            "donativos",
            "Deducción por donativos",
            "aplicado",
            f"Deducción aplicada: {_importe_linea(base, 'Deducción por donativos'):.2f} €.",
            ["Donativos a entidades acogidas a la Ley 49/2002, con certificado de la entidad."],
            d.fuente,
        )
    else:
        add(
            "donativos",
            "Deducción por donativos",
            "disponible" if base.cuota_liquida > 0 else "no_aplica",
            f"Los primeros {d.tramo_primero:.0f} € donados a entidades acogidas a la Ley 49/2002 deducen un "
            f"{d.tipo_primero * 100:.0f} %, y el resto un {d.tipo_resto * 100:.0f} % "
            f"({d.tipo_resto_recurrente * 100:.0f} % si llevas tres años donando a la misma entidad importes iguales o crecientes).",
            [
                "La entidad debe estar acogida a la Ley 49/2002 y certificar el donativo.",
                "La base de deducción no puede superar el 10 % de la base liquidable.",
            ],
            d.fuente,
            ahorro=_ahorro(base, inp, p, donativos=d.tramo_primero) if base.cuota_liquida > 0 else None,
        )

    mat = p.maternidad
    if inp.hijos_deduccion_maternidad:
        add(
            "maternidad",
            "Deducción por maternidad",
            "aplicado",
            f"Deducción aplicada: {_importe_linea(base, 'Deducción por maternidad'):.2f} €.",
            ["Se prorratea por meses y se descuenta lo cobrado por anticipado."],
            mat.fuente,
        )
    elif inp.num_descendientes_menores_3:
        add(
            "maternidad",
            "Deducción por maternidad",
            "por_confirmar",
            f"Con hijos menores de tres años, la madre puede deducir hasta {mat.importe_por_hijo:.0f} € por hijo "
            f"(y hasta {mat.incremento_guarderia:.0f} € más por gastos de guardería), incluso si la cuota es cero.",
            [
                "Madre con derecho al mínimo por descendientes, de alta en la Seguridad Social o mutualidad (30 días "
                "cotizados) o que perciba prestación por desempleo en el nacimiento.",
                "El padre solo tiene derecho en caso de fallecimiento de la madre o custodia exclusiva.",
            ],
            mat.fuente,
            ahorro=mat.importe_por_hijo * inp.num_descendientes_menores_3,
        )

    av = p.alquiler_vivienda_habitual
    if av is not None:
        nombre_av = "Deducción estatal por alquiler de vivienda habitual"
        requisitos_av = [
            f"Base imponible inferior a {av.umbral_superior:.2f} € (la base máxima se reduce a partir de {av.umbral_inferior:.2f} €).",
            "Durante al menos medio año, ni tú ni tu unidad familiar podéis ser titulares de otra vivienda a menos de "
            "50 km de la alquilada.",
            "La introdujo el Real Decreto-ley 29/2026, pendiente de convalidación por el Congreso.",
        ]
        aplicada = _importe_linea(base, nombre_av)
        if aplicada:
            add(
                "alquiler_estatal",
                nombre_av,
                "aplicado",
                f"Deducción aplicada: {aplicada:.2f} €.",
                requisitos_av,
                av.fuente,
            )
        elif (
            "alquiler_vivienda_habitual" not in conocidos
            and base.base_imponible_general + base.base_imponible_ahorro < av.umbral_superior
        ):
            add(
                "alquiler_estatal",
                nombre_av,
                "por_confirmar",
                f"Si vives de alquiler, puedes deducir el {av.porcentaje * 100:.0f} % de lo pagado en el año, sobre una "
                f"base máxima de {av.base_maxima:.0f} €.",
                requisitos_av,
                av.fuente,
                faltan=["alquiler_vivienda_habitual"],
            )

    if inp.num_descendientes >= 2:
        add(
            "familia_numerosa",
            "Deducción por familia numerosa o personas con discapacidad a cargo",
            "por_confirmar",
            "Si formas parte de una familia numerosa (o eres progenitor separado con dos hijos sin derecho a "
            "anualidades por alimentos), puedes deducir hasta 1.200 € anuales, el doble en categoría especial; "
            "también por descendientes, ascendientes o cónyuge con discapacidad a cargo. No está incluida en la simulación.",
            [
                "Realizar una actividad por cuenta propia o ajena con alta en la Seguridad Social (o percibir pensión o desempleo).",
                "Límite: las cotizaciones devengadas en el ejercicio.",
            ],
            "art. 81 bis LIRPF",
        )

    v = p.vivienda_habitual_transitoria
    if inp.pagos_vivienda_habitual_pre2013:
        add(
            "vivienda_pre2013",
            "Deducción por vivienda habitual comprada antes de 2013",
            "aplicado",
            f"Deducción aplicada: {_importe_linea(base, 'Deducción por inversión en vivienda habitual'):.2f} €.",
            ["Haber comprado antes del 1-1-2013 y haberse aplicado la deducción antes de esa fecha."],
            v.fuente,
        )
    elif "pagos_vivienda_habitual_pre2013" not in conocidos:
        add(
            "vivienda_pre2013",
            "Deducción por vivienda habitual comprada antes de 2013",
            "por_confirmar",
            f"Si compraste tu vivienda habitual antes de 2013 y sigues pagando hipoteca, puedes deducir el "
            f"{v.tipo * 100:.0f} % de lo pagado en el año (capital e intereses), sobre un máximo de {v.base_maxima:.0f} €.",
            [
                "Compra anterior al 1-1-2013.",
                "Haber aplicado la deducción por esa vivienda en un ejercicio anterior a 2013.",
            ],
            v.fuente,
            ahorro=None,
            faltan=["pagos_vivienda_habitual_pre2013"],
        )

    # --- Regional deductions ------------------------------------------------
    # Only what was read from the community's own law is stated; everything
    # else is a pointer to look it up, never an amount recited from memory.
    ccaa = inp.comunidad_autonoma
    comunidad = p.comunidad(ccaa)
    if comunidad is None:
        add(
            "deducciones_autonomicas",
            "Deducciones autonómicas",
            "por_confirmar",
            (
                f"{ccaa} tiene deducciones propias (alquiler, nacimiento, guardería, donativos, etc.) que no están en "
                f"este catálogo. Búscalas en el corpus con buscar_fuentes (ámbito 'autonomico:{ccaa}') antes de "
                "mencionarlas."
                if ccaa
                else "Cada comunidad autónoma tiene deducciones propias. Hace falta saber en cuál resides para buscarlas."
            ),
            ["Varían por comunidad y por ejercicio; suelen tener límites de renta."],
            "Normativa autonómica del IRPF (Ley 22/2009, art. 46)",
            faltan=faltan("comunidad_autonoma"),
        )
        return out

    base_imponible = base.base_imponible_general + base.base_imponible_ahorro
    es_madrid = comunidad.deducciones_madrid is not None
    norma = f"DLeg. 1/2010 de la {comunidad.nombre}" if es_madrid else comunidad.nombre
    # article → (calculator line it produces, input that triggers it)
    calculadas = {
        "art. 4": ("Deducción autonómica por nacimiento", "hijos_nacidos_ultimos_3_ejercicios"),
        "art. 8": ("Deducción autonómica por alquiler", "alquiler_vivienda_habitual"),
        "art. 11": ("Deducción autonómica por gastos educativos", "gastos_escolaridad"),
        "art. 11 bis": ("Deducción autonómica por cuidado", "cotizaciones_empleada_hogar"),
    }
    for deduccion in comunidad.deducciones:
        linea, campo = calculadas.get(deduccion.articulo, ("", ""))
        aplicado = _importe_linea(base, linea) if linea and comunidad.deducciones_madrid is not None else ZERO
        limite = deduccion.limite_base_individual if inp.tributacion == "individual" else deduccion.limite_base_conjunta
        supera = limite is not None and base_imponible > limite
        if aplicado:
            estado: Estado = "aplicado"
            detalle = f"Deducción aplicada en la simulación: {aplicado:.2f} €. {deduccion.resumen}"
        elif supera:
            estado = "no_aplica"
            detalle = (
                f"No te corresponde con los datos actuales: tu base imponible ({base_imponible:.2f} €) supera el "
                f"límite de {limite:.2f} €. {deduccion.resumen}"
            )
        else:
            estado = "por_confirmar"
            detalle = deduccion.resumen + (
                f" La simulación la aplica si indicas «{campo}» y cumples los requisitos."
                if linea and comunidad.deducciones_madrid is not None
                else " No está incluida en la simulación."
            )
        add(
            f"autonomica_{deduccion.articulo.replace('art. ', 'art_').replace(' ', '_')}",
            f"{comunidad.nombre}: {deduccion.titulo}",
            estado,
            detalle,
            [deduccion.limites, "Requiere justificación documental (art. 18.4)."],
            f"{deduccion.articulo} {norma} ({comunidad.fuente_principal.boe_id})",
            faltan=faltan(campo) if estado == "por_confirmar" and campo else None,
        )
    for calculable in comunidad.deducciones_calculables:
        activador = campo_que_la_activa(calculable)
        aplicado = _importe_linea(base, f"Deducción autonómica: {calculable.titulo}")
        add(
            f"autonomica_{normalizar_comunidad(calculable.articulo).replace('.', '').replace(' ', '_')}",
            f"{comunidad.nombre}: {calculable.titulo}",
            "aplicado" if aplicado else "por_confirmar",
            (f"Deducción aplicada en la simulación: {aplicado:.2f} €. " if aplicado else "")
            + calculable.resumen
            + (
                ""
                if aplicado or not activador
                else f" La simulación la aplica si indicas «{activador}» y cumples los requisitos."
            ),
            calculable.requisitos_no_comprobados or ["Requiere justificación documental."],
            f"{calculable.articulo} {comunidad.fuente_principal.norma.split(',')[0]} ({comunidad.fuente_principal.boe_id})",
            faltan=faltan(activador) if activador and not aplicado else None,
        )
    if comunidad.otras_deducciones:
        add(
            "autonomicas_otras",
            f"{comunidad.nombre}: otras deducciones",
            "por_confirmar",
            (
                "La comunidad tiene además estas deducciones"
                if es_madrid
                else "Estas son todas las deducciones de la comunidad"
            )
            + ", por título (la simulación solo calcula las que aparecen arriba con su detalle): "
            + "; ".join(comunidad.otras_deducciones)
            + ". Consulta sus requisitos e importes con buscar_fuentes antes de mencionarlas.",
            [
                (
                    "Cada una tiene sus propios requisitos y límites de renta (art. 18)."
                    if es_madrid
                    else "Cada una tiene sus propios requisitos y límites de renta."
                )
            ],
            (
                f"arts. 3 a 18 {norma} ({comunidad.fuente_principal.boe_id})"
                if es_madrid
                else f"{comunidad.fuente_principal.norma} ({comunidad.fuente_principal.boe_id})"
            ),
        )
    return out
