"""Impuesto sobre Sucesiones y Donaciones: what one heir or donee pays.

    base imponible  = value received (net) [+ 3 % household goods] [+ life insurance]
    base liquidable = base imponible − reductions (kinship, disability, insurance, main home, business…)
    cuota íntegra   = scale(base liquidable)
    cuota tributaria = cuota íntegra × coefficient(pre-existing wealth, kinship group)
    a ingresar      = cuota tributaria − regional allowance ("bonificación")

The state rules are those of Ley 29/1987; a community's rules are data
(:class:`~hacienda_gpt.tax.sucesiones_params.NormativaAutonomicaISD`) applied
generically: for each step the first community rule whose conditions hold is
used, and the state rule otherwise.

What it does not do, repeated as ``avisos`` on every result: decide which
community's rules apply, value the assets, accumulate earlier gifts (art. 30),
compute usufructs, or check the requirements of the reductions the user claims
by declaring an amount.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hacienda_gpt.tax.irpf import Linea
from hacienda_gpt.tax.sucesiones_params import (
    CondicionesISD,
    Grupo,
    ISDParams,
    Modo,
    NormativaAutonomicaISD,
    NormativaISD,
    Parentesco,
    Reduccion,
    ReduccionVivienda,
    ReglaBonificacion,
    TramoReduccion,
    _Regla,
    load_isd_params,
    normalizar_parentesco,
)

CENT = Decimal("0.01")
ZERO = Decimal(0)

# Art. 20.2.a) Ley 29/1987 sorts relatives into four groups; this is that sorting.
_CLASE_PARENTESCO: dict[str, str] = {
    "conyuge": "conyuge",
    "pareja_de_hecho": "extrano",  # a stranger for the state law; communities may say otherwise
    "hijo": "descendiente",
    "otro_descendiente": "descendiente",
    "padre": "ascendiente",
    "otro_ascendiente": "ascendiente",
    "hermano": "colateral",
    "sobrino": "colateral",
    "tio": "colateral",
    "colateral_afinidad": "colateral",
    "hijastro": "afinidad",
    "yerno_nuera": "afinidad",
    "padrastro": "afinidad",
    "suegro": "afinidad",
    "colateral_cuarto_grado": "colateral_lejano",
    "colateral_lejano": "colateral_lejano",
    "extrano": "extrano",
}
_ETIQUETA_MODO = {"sucesion": "adquisiciones por herencia", "donacion": "donaciones"}


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _eur(value: Decimal) -> str:
    return f"{_round(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def _pct(value: Decimal) -> str:
    return f"{(value * 100).normalize():f}".replace(".", ",") + " %"


class BeneficiarioISD(BaseModel):
    """One heir, legatee, insurance beneficiary or donee. Amounts in euros."""

    model_config = ConfigDict(extra="forbid")

    ejercicio: int = 2026
    modo: Modo
    # Community whose rules apply (see the warning about art. 32 Ley 22/2009). None = state rules only.
    comunidad: str | None = None
    fecha_devengo: date | None = None  # death, or the day of the gift
    # Relationship to the deceased / donor; or, failing that, the kinship group directly.
    parentesco: str | None = None
    grupo: Grupo | None = None
    edad: int | None = Field(default=None, ge=0, le=120)
    grado_discapacidad: int = Field(default=0, ge=0, le=100)
    # What this person receives, net of deductible charges, debts and expenses; life insurance apart.
    valor_recibido: Decimal = Field(ge=0)
    # Add the legal presumption of household goods (3 % of what is inherited). Inheritances only.
    incluir_ajuar: bool = False
    seguro_vida: Decimal = Field(default=ZERO, ge=0)
    # Parts of ``valor_recibido`` the user declares as qualifying for a reduction.
    valor_vivienda_habitual: Decimal = Field(default=ZERO, ge=0)
    participacion_vivienda: Decimal | None = Field(default=None, gt=0, le=1)  # share of the home, 0-1
    valor_empresa_familiar: Decimal = Field(default=ZERO, ge=0)
    valor_patrimonio_historico: Decimal = Field(default=ZERO, ge=0)
    # Any other reduction the user is entitled to, as an amount already computed.
    otras_reducciones: Decimal = Field(default=ZERO, ge=0)
    patrimonio_preexistente: Decimal = Field(default=ZERO, ge=0)
    escritura_publica: bool = False  # the gift was formalised in a public deed

    @field_validator("parentesco")
    @classmethod
    def _parentesco(cls, value: str | None) -> str | None:
        return None if value is None else normalizar_parentesco(value)

    @model_validator(mode="after")
    def _coherente(self) -> BeneficiarioISD:
        if self.parentesco is None and self.grupo is None:
            raise ValueError("indica el parentesco con el causante o donante (o, al menos, el grupo I-IV)")
        declarados = self.valor_vivienda_habitual + self.valor_empresa_familiar + self.valor_patrimonio_historico
        if declarados > self.valor_recibido:
            raise ValueError("los valores con derecho a reducción no pueden superar el valor recibido")
        if self.modo == "donacion" and (self.seguro_vida or self.valor_vivienda_habitual or self.incluir_ajuar):
            raise ValueError("seguro_vida, valor_vivienda_habitual e incluir_ajuar solo existen en las sucesiones")
        if self.fecha_devengo is not None and self.fecha_devengo.year != self.ejercicio:
            raise ValueError("la fecha de devengo no corresponde al ejercicio indicado")
        return self


class ResultadoISD(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    modo: Modo
    # Key of the community whose rules were applied; None = state rules only.
    comunidad: str | None
    grupo: Grupo
    parentesco: str | None
    base_imponible: Decimal
    reducciones: Decimal
    base_liquidable: Decimal
    cuota_integra: Decimal
    coeficiente: Decimal
    cuota_tributaria: Decimal
    bonificacion: Decimal
    cuota_a_ingresar: Decimal
    # cuota_a_ingresar / base_imponible, as a fraction.
    tipo_efectivo: Decimal
    lineas: list[Linea]
    avisos: list[str]
    fuente: str


@dataclass(frozen=True)
class _Caso:
    """The facts a rule's conditions are checked against."""

    modo: Modo
    grupo: Grupo
    parentesco: str | None
    edad: int | None
    discapacidad: int
    patrimonio: Decimal
    requisitos: frozenset[str]
    base_imponible: Decimal = ZERO
    base_liquidable: Decimal | None = None


class _Traza:
    def __init__(self, fecha: date | None) -> None:
        self.lineas: list[Linea] = []
        self.avisos: list[str] = []
        self.fecha = fecha

    def linea(self, concepto: str, importe: Decimal, referencia: str | None = None) -> None:
        self.lineas.append(Linea(concepto=concepto, importe=_round(importe), referencia=referencia))

    def aviso(self, text: str) -> None:
        if text not in self.avisos:
            self.avisos.append(text)

    def usa(self, regla: _Regla, titulo: str) -> None:
        """Warnings every applied rule brings with it."""
        if regla.requisitos_no_comprobados:
            requisitos = "; ".join(r.rstrip(".") for r in regla.requisitos_no_comprobados)
            self.aviso(f"«{titulo}» ({regla.articulo}) se ha aplicado sin comprobar: {requisitos}.")
        if self.fecha is not None and regla.en_vigor_desde > self.fecha.isoformat():
            self.aviso(
                f"«{titulo}» ({regla.articulo}): la redacción aplicada está en vigor desde el "
                f"{regla.en_vigor_desde}, después de la fecha de devengo indicada; la norma vigente ese día puede "
                "ser distinta y no está cargada."
            )


def _incumple(c: CondicionesISD, caso: _Caso) -> list[str]:
    """Why ``c`` does not hold (empty when it does). "falta:…" = the datum is missing; "requiere:…" = a flag."""
    motivos: list[str] = []
    if c.modo is not None and c.modo != caso.modo:
        motivos.append("modo")
    if c.grupos and caso.grupo not in c.grupos:
        motivos.append("grupo")
    if c.parentescos:
        if caso.parentesco is None:
            motivos.append("falta:parentesco")
        elif caso.parentesco not in c.parentescos:
            motivos.append("parentesco")
    if any(v is not None for v in (c.edad_desde, c.edad_hasta, c.edad_menor_de, c.edad_mayor_de)):
        if caso.edad is None:
            motivos.append("falta:edad")
        elif (
            (c.edad_desde is not None and caso.edad < c.edad_desde)
            or (c.edad_hasta is not None and caso.edad > c.edad_hasta)
            or (c.edad_menor_de is not None and caso.edad >= c.edad_menor_de)
            or (c.edad_mayor_de is not None and caso.edad <= c.edad_mayor_de)
        ):
            motivos.append("edad")
    if (c.discapacidad_desde is not None and caso.discapacidad < c.discapacidad_desde) or (
        c.discapacidad_menor_de is not None and caso.discapacidad >= c.discapacidad_menor_de
    ):
        motivos.append("discapacidad")
    if (c.base_imponible_hasta is not None and caso.base_imponible > c.base_imponible_hasta) or (
        c.base_imponible_menor_de is not None and caso.base_imponible >= c.base_imponible_menor_de
    ):
        motivos.append("base imponible")
    if c.base_liquidable_hasta is not None or c.base_liquidable_menor_de is not None:
        if (
            caso.base_liquidable is None
            or (c.base_liquidable_hasta is not None and caso.base_liquidable > c.base_liquidable_hasta)
            or (c.base_liquidable_menor_de is not None and caso.base_liquidable >= c.base_liquidable_menor_de)
        ):
            motivos.append("base liquidable")
    if (c.patrimonio_preexistente_hasta is not None and caso.patrimonio > c.patrimonio_preexistente_hasta) or (
        c.patrimonio_preexistente_menor_de is not None and caso.patrimonio >= c.patrimonio_preexistente_menor_de
    ):
        motivos.append("patrimonio preexistente")
    motivos.extend(f"requiere:{r}" for r in c.requiere if r not in caso.requisitos)
    return motivos


def _avisar_descartes(titulo: str, articulo: str, motivos: list[str], t: _Traza) -> None:
    """A rule that failed ONLY for a missing datum or flag is worth telling the user about."""
    if not motivos or not all(m.startswith(("falta:", "requiere:")) for m in motivos):
        return
    partes = []
    for motivo in motivos:
        clase, _, dato = motivo.partition(":")
        if clase == "falta":
            partes.append(f"falta indicar {'la edad' if dato == 'edad' else 'el parentesco exacto'}")
        else:
            partes.append("exige que conste en escritura pública" if dato == "escritura_publica" else f"exige {dato}")
    t.aviso(f"«{titulo}» ({articulo}) no se ha aplicado: {' y '.join(partes)}.")


def _primera[R: _Regla](reglas: Sequence[R], caso: _Caso, t: _Traza) -> R | None:
    """First rule whose conditions hold."""
    for regla in reglas:
        motivos = _incumple(regla.cuando, caso)
        if not motivos:
            return regla
        _avisar_descartes(str(getattr(regla, "titulo", regla.resumen)), regla.articulo, motivos, t)
    return None


def _elegir[R: _Regla](propias: Sequence[R], estatales: Sequence[R], caso: _Caso, t: _Traza) -> R | None:
    """The community's rule when one applies; the state's otherwise."""
    return _primera(propias, caso, t) or _primera(estatales, caso, t)


def grupo_de_parentesco(parentesco: str, edad: int | None) -> Grupo:
    """Kinship group of art. 20.2.a) Ley 29/1987. A descendant of unknown age is taken as 21 or older."""
    clase = _CLASE_PARENTESCO[parentesco]
    if clase == "descendiente":
        return "I" if edad is not None and edad < 21 else "II"
    if clase in {"conyuge", "ascendiente"}:
        return "II"
    return "III" if clase in {"colateral", "afinidad"} else "IV"


def _importe_tramo(tramo: TramoReduccion, edad: int | None) -> Decimal:
    importe = tramo.importe
    if tramo.incremento_por_ano is not None and tramo.edad_referencia is not None and edad is not None:
        importe += tramo.incremento_por_ano * max(0, tramo.edad_referencia - edad)
    return min(importe, tramo.maximo) if tramo.maximo is not None else importe


def _reducciones_personales(
    estatal: NormativaISD, comunidad: NormativaAutonomicaISD | None, caso: _Caso, t: _Traza
) -> list[tuple[Reduccion, TramoReduccion]]:
    """Personal reductions that apply, state ones first; a regional "mejora" displaces the state one of its class."""

    def aplicables(reducciones: list[Reduccion]) -> list[tuple[Reduccion, TramoReduccion]]:
        out = []
        for reduccion in reducciones:
            if _incumple(reduccion.cuando, caso):
                continue
            for tramo in reduccion.tramos:
                motivos = _incumple(tramo.cuando, caso)
                if not motivos:
                    out.append((reduccion, tramo))
                    break
                _avisar_descartes(reduccion.titulo, reduccion.articulo, motivos, t)
        return out

    propias = aplicables(comunidad.reducciones) if comunidad is not None else []
    mejoradas = {r.clase for r, _ in propias if r.caracter == "mejora"}
    elegidas = [(r, tr) for r, tr in aplicables(estatal.reducciones) if r.clase not in mejoradas] + propias
    aplicadas: list[tuple[Reduccion, TramoReduccion]] = []
    for reduccion, tramo in elegidas:
        excluyentes = [r.titulo for r, _ in aplicadas if r.clave in reduccion.incompatible_con]
        if excluyentes:
            t.aviso(f"«{reduccion.titulo}» no se ha aplicado: es incompatible con «{excluyentes[0]}».")
            continue
        aplicadas.append((reduccion, tramo))
    return aplicadas


def _limite_vivienda(
    regla: ReduccionVivienda, estatal: NormativaISD, inp: BeneficiarioISD, caso: _Caso, t: _Traza
) -> Decimal | None:
    if regla.sin_limite:
        return None
    if regla.limite_conjunto is not None:
        if inp.participacion_vivienda is not None:
            limite = regla.limite_conjunto * inp.participacion_vivienda
        else:
            limite = regla.limite_individual_minimo or regla.limite_conjunto
            t.aviso(
                f"Vivienda habitual: el límite de {_eur(regla.limite_conjunto)} es para toda la vivienda y se reparte "
                "según la participación de cada adquirente; como no has indicado la tuya "
                f"(participacion_vivienda), se ha tomado un límite individual de {_eur(limite)}."
            )
        return max(limite, regla.limite_individual_minimo or ZERO)
    if regla.limite_por_sujeto is not None:
        return regla.limite_por_sujeto
    del_estado = next((r for r in estatal.vivienda_habitual if not _incumple(r.cuando, caso)), None)
    if del_estado is None or del_estado.limite_por_sujeto is None:
        return None
    t.aviso(
        f"Vivienda habitual: {regla.articulo} mejora el porcentaje pero no menciona límite; se ha mantenido el "
        f"límite estatal de {_eur(del_estado.limite_por_sujeto)} por adquirente ({del_estado.articulo}). "
        "Confírmalo con la Administración autonómica."
    )
    return del_estado.limite_por_sujeto


def _reduccion_vivienda(
    estatal: NormativaISD, comunidad: NormativaAutonomicaISD | None, inp: BeneficiarioISD, caso: _Caso, t: _Traza
) -> Decimal:
    if not inp.valor_vivienda_habitual:
        return ZERO
    regla = _elegir(comunidad.vivienda_habitual if comunidad else [], estatal.vivienda_habitual, caso, t)
    if regla is None:
        t.aviso("No hay regla cargada para la reducción por vivienda habitual; no se ha aplicado.")
        return ZERO
    clase = _CLASE_PARENTESCO[caso.parentesco] if caso.parentesco else None
    directo = clase in {"conyuge", "descendiente", "ascendiente"} or (clase is None and caso.grupo in {"I", "II"})
    if not directo:
        if clase not in {"colateral", "colateral_lejano"} or regla.colateral_mayor_de is None:
            motivo = (
                "indica el parentesco exacto para saber si te corresponde"
                if clase is None
                else "solo es para cónyuge, ascendientes, descendientes y ciertos colaterales"
            )
            t.aviso(f"Reducción por vivienda habitual ({regla.articulo}) no aplicada: {motivo}.")
            return ZERO
        if caso.edad is None or caso.edad <= regla.colateral_mayor_de:
            t.aviso(
                f"Reducción por vivienda habitual ({regla.articulo}) no aplicada: un pariente colateral ha de ser "
                f"mayor de {regla.colateral_mayor_de} años (y haber convivido con el causante)."
            )
            return ZERO
        t.aviso(
            f"Reducción por vivienda habitual ({regla.articulo}): como pariente colateral, exige haber convivido con "
            f"el causante durante los {regla.colateral_anos_convivencia or 'dos'} años anteriores al fallecimiento; "
            "no se ha comprobado."
        )
    importe = inp.valor_vivienda_habitual * regla.porcentaje
    limite = _limite_vivienda(regla, estatal, inp, caso, t)
    if limite is not None:
        importe = min(importe, limite)
    t.linea(f"Reducción por vivienda habitual del causante ({_pct(regla.porcentaje)})", -importe, regla.articulo)
    t.usa(regla, "Reducción por vivienda habitual")
    t.aviso(
        f"Reducción por vivienda habitual ({regla.articulo}): se ha calculado sobre el valor que has declarado y "
        f"queda condicionada a mantener la vivienda durante {regla.anos_mantenimiento} años desde el fallecimiento "
        "(salvo fallecimiento del adquirente); si se incumple hay que ingresar lo no pagado con intereses de demora."
    )
    return importe


def _porcentaje_bonificacion(regla: ReglaBonificacion, magnitud: Decimal) -> Decimal:
    if regla.porcentaje is not None:
        return regla.porcentaje
    if regla.tramos_metodo == "por_tramo":
        return next(tr.porcentaje for tr in regla.tramos if tr.hasta is None or magnitud <= tr.hasta)
    if magnitud <= 0:
        return regla.tramos[0].porcentaje
    ponderado, previo = ZERO, ZERO
    for tramo in regla.tramos:
        superior = magnitud if tramo.hasta is None else min(magnitud, tramo.hasta)
        if superior > previo:
            ponderado += (superior - previo) * tramo.porcentaje
            previo = superior
    medio = ponderado / magnitud
    if regla.redondeo_porcentaje == "dos_decimales_milesima_hasta_5_abajo":
        # Percent with two decimals: look at the thousandth only; 5 or less keeps the hundredth.
        milesimas = (medio * 100).quantize(Decimal("0.001"), rounding=ROUND_DOWN)
        centesimas = milesimas.quantize(CENT, rounding=ROUND_DOWN)
        if milesimas - centesimas > Decimal("0.005"):
            centesimas += CENT
        medio = centesimas / 100
    return medio


def _coeficiente(
    estatal: NormativaISD,
    comunidad: NormativaAutonomicaISD | None,
    caso: _Caso,
    cuota_integra: Decimal,
    t: _Traza,
) -> tuple[Decimal, Decimal]:
    """(coefficient, tax after it), with the excess clause of art. 22.2 when the rule has it."""
    regla = _elegir(comunidad.coeficientes if comunidad else [], estatal.coeficientes, caso, t)
    if regla is None:
        raise ValueError("no hay coeficientes multiplicadores cargados")
    indice = next(
        i for i, tr in enumerate(regla.tramos) if tr.patrimonio_hasta is None or caso.patrimonio <= tr.patrimonio_hasta
    )
    coeficiente = regla.tramos[indice].coeficientes[caso.grupo]
    cuota = cuota_integra * coeficiente
    t.linea(f"Coeficiente multiplicador {coeficiente.normalize():f} (grupo {caso.grupo})".replace(".", ","), cuota)
    t.lineas[-1].referencia = regla.articulo
    t.usa(regla, "Coeficiente multiplicador")
    if regla.regla_exceso and indice > 0:
        anterior = regla.tramos[indice - 1]
        assert anterior.patrimonio_hasta is not None
        exceso = (cuota - cuota_integra * anterior.coeficientes[caso.grupo]) - (
            caso.patrimonio - anterior.patrimonio_hasta
        )
        if exceso > 0:
            cuota -= exceso
            t.linea("Reducción del exceso por salto de tramo de patrimonio preexistente", -exceso, regla.articulo)
    return coeficiente, cuota


def calcular_isd(inp: BeneficiarioISD, params: ISDParams | None = None) -> ResultadoISD:
    """Tax due by one beneficiary, with the trace of the computation and what was not checked."""
    p = params or load_isd_params(inp.ejercicio)
    estatal = p.estatal
    t = _Traza(inp.fecha_devengo)
    encontrada = p.comunidad(inp.comunidad)
    clave, comunidad = encontrada if encontrada else (None, None)

    # --- Who the beneficiary is --------------------------------------------------
    parentesco = inp.parentesco
    if comunidad is not None and parentesco is not None:
        # The first assimilation whose conditions hold: a community may have one per mode.
        equiparacion = next(
            (
                e
                for e in comunidad.equiparaciones
                if e.parentesco == parentesco and not _incumple(e.cuando, _caso_minimo(inp))
            ),
            None,
        )
        if equiparacion is not None:
            t.aviso(
                f"{comunidad.nombre} equipara «{parentesco.replace('_', ' ')}» a "
                f"«{equiparacion.equivale_a.replace('_', ' ')}» ({equiparacion.articulo}): {equiparacion.resumen}"
            )
            t.usa(equiparacion, "Equiparación")
            parentesco = equiparacion.equivale_a
    if parentesco is not None:
        grupo = grupo_de_parentesco(parentesco, inp.edad)
        if _CLASE_PARENTESCO[parentesco] == "descendiente" and inp.edad is None:
            t.aviso(
                "No has indicado la edad del descendiente: se le ha tratado como de 21 años o más (grupo II). "
                "Los menores de 21 (grupo I) tienen una reducción mayor."
            )
        if inp.grupo is not None and inp.grupo != grupo:
            t.aviso(
                f"Has indicado el grupo {inp.grupo}, pero al parentesco «{parentesco.replace('_', ' ')}» le "
                f"corresponde el grupo {grupo} (art. 20.2.a) Ley 29/1987); se ha aplicado el grupo {grupo}."
            )
        if parentesco == "pareja_de_hecho":
            t.aviso(
                "La ley estatal no equipara la pareja de hecho al cónyuge: se la ha tratado como extraño (grupo IV). "
                "Muchas comunidades sí la equiparan si está inscrita."
            )
        if parentesco == "colateral_afinidad":
            t.aviso(
                "El art. 20.2.a) habla de «colaterales de segundo y tercer grado» sin distinguir consanguinidad y "
                "afinidad; se ha aplicado el grupo III al colateral por afinidad."
            )
    else:
        assert inp.grupo is not None
        grupo = inp.grupo
        t.aviso(
            "Solo has indicado el grupo de parentesco: las reglas que dependen del parentesco exacto (cónyuge, "
            "hijo, ascendiente…) no se han podido aplicar."
        )
    caso = _Caso(
        modo=inp.modo,
        grupo=grupo,
        parentesco=parentesco,
        edad=inp.edad,
        discapacidad=inp.grado_discapacidad,
        patrimonio=inp.patrimonio_preexistente,
        requisitos=frozenset({"escritura_publica"} if inp.escritura_publica else ()),
    )

    # --- Base imponible ----------------------------------------------------------
    t.linea("Valor neto de lo adquirido", inp.valor_recibido, "arts. 9 y 12 a 17 Ley 29/1987")
    base_imponible = inp.valor_recibido
    if inp.incluir_ajuar:
        ajuar = inp.valor_recibido * estatal.ajuar.porcentaje
        base_imponible += ajuar
        t.linea(f"Ajuar doméstico ({_pct(estatal.ajuar.porcentaje)})", ajuar, estatal.ajuar.articulo)
        t.usa(estatal.ajuar, "Ajuar doméstico")
        t.aviso(estatal.ajuar.resumen)
    if inp.seguro_vida:
        base_imponible += inp.seguro_vida
        t.linea("Seguro de vida percibido", inp.seguro_vida, "art. 9.c) Ley 29/1987")
    t.linea("Base imponible", base_imponible, "art. 9 Ley 29/1987")
    caso = replace(caso, base_imponible=base_imponible)

    # --- Reductions ---------------------------------------------------------------
    total = ZERO
    pendientes: list[tuple[Reduccion, TramoReduccion]] = []
    for reduccion, tramo in _reducciones_personales(estatal, comunidad, caso, t):
        if tramo.hasta_completar:
            pendientes.append((reduccion, tramo))
            continue
        importe = _importe_tramo(tramo, inp.edad)
        total += importe
        t.linea(reduccion.titulo, -importe, reduccion.articulo)
        t.usa(reduccion, reduccion.titulo)
    if inp.seguro_vida:
        clase = _CLASE_PARENTESCO[parentesco] if parentesco else None
        seguro = _elegir(comunidad.seguro_vida if comunidad else [], estatal.seguro_vida, caso, t)
        directo = clase in {"conyuge", "descendiente", "ascendiente"} or (clase is None and grupo in {"I", "II"})
        if seguro is not None and directo:
            importe = inp.seguro_vida * seguro.porcentaje
            if seguro.limite is not None:
                importe = min(importe, seguro.limite)
            total += importe
            t.linea("Reducción por seguro de vida", -importe, seguro.articulo)
            t.usa(seguro, "Reducción por seguro de vida")
        elif seguro is not None:
            t.aviso(
                f"Reducción por seguro de vida ({seguro.articulo}) no aplicada: es para cónyuge, ascendientes y "
                "descendientes del contratante."
            )
    total += _reduccion_vivienda(estatal, comunidad, inp, caso, t)
    for clase_bienes, valor, etiqueta in (
        ("empresa", inp.valor_empresa_familiar, "empresa individual, negocio profesional o participaciones"),
        ("patrimonio_historico", inp.valor_patrimonio_historico, "bienes del patrimonio histórico o cultural"),
    ):
        if not valor:
            continue
        propias = [r for r in (comunidad.reducciones_bienes if comunidad else []) if r.clase == clase_bienes]
        bienes = _elegir(propias, [r for r in estatal.reducciones_bienes if r.clase == clase_bienes], caso, t)
        if bienes is None:
            t.aviso(f"No hay regla cargada para la reducción por {etiqueta}; no se ha aplicado.")
            continue
        importe = valor * bienes.porcentaje
        total += importe
        t.linea(f"Reducción por {etiqueta} ({_pct(bienes.porcentaje)})", -importe, bienes.articulo)
        t.usa(bienes, f"Reducción por {etiqueta}")
        t.aviso(
            f"Reducción por {etiqueta} ({bienes.articulo}): se ha aplicado sobre el valor que has declarado, sin "
            f"comprobar ninguno de sus requisitos. {bienes.resumen}"
        )
        if comunidad is not None and not propias:
            t.aviso(
                f"{comunidad.nombre} puede tener una reducción propia o mejorada por {etiqueta} que no está "
                "cargada; se ha aplicado la estatal."
            )
    if inp.otras_reducciones:
        total += inp.otras_reducciones
        t.linea("Otras reducciones declaradas", -inp.otras_reducciones)
        t.aviso("«Otras reducciones declaradas» se ha restado tal como la has indicado, sin comprobar nada.")
    for reduccion, tramo in pendientes:
        importe = max(ZERO, _importe_tramo(tramo, inp.edad) - total)
        total += importe
        t.linea(f"{reduccion.titulo} (hasta completar {_eur(tramo.importe)})", -importe, reduccion.articulo)
        t.usa(reduccion, reduccion.titulo)
    if total > base_imponible:
        t.aviso("Las reducciones superan la base imponible: la base liquidable es cero.")
    reducciones = min(total, base_imponible)
    base_liquidable = base_imponible - reducciones
    referencia_base = "art. 20 Ley 29/1987"
    t.linea("Base liquidable", base_liquidable, referencia_base)
    caso = replace(caso, base_liquidable=base_liquidable)

    # --- Tax ------------------------------------------------------------------------
    tarifa = _elegir(comunidad.tarifas if comunidad else [], estatal.tarifas, caso, t)
    if tarifa is None:
        raise ValueError("no hay tarifa cargada")
    cuota_integra = tarifa.escala.cuota(base_liquidable)
    t.linea("Cuota íntegra", cuota_integra, tarifa.articulo)
    t.usa(tarifa, "Tarifa")
    coeficiente, cuota_tributaria = _coeficiente(estatal, comunidad, caso, cuota_integra, t)
    cuota_tributaria = _round(cuota_tributaria)
    t.linea("Cuota tributaria", cuota_tributaria, "art. 22 Ley 29/1987")

    bonificacion = ZERO
    regla = _primera(comunidad.bonificaciones, caso, t) if comunidad is not None else None
    if regla is not None:
        perdida = [
            nombre
            for nombre, valor in (
                ("empresa", inp.valor_empresa_familiar),
                ("patrimonio_historico", inp.valor_patrimonio_historico),
                ("otras_reducciones", inp.otras_reducciones),
            )
            if valor and nombre in regla.pierde_con
        ]
        if perdida:
            t.aviso(
                f"«{regla.titulo}» ({regla.articulo}) no se ha aplicado: es incompatible con optar por las "
                f"reducciones que has declarado ({', '.join(n.replace('_', ' ') for n in perdida)}). Compara el "
                "resultado sin declararlas."
            )
        else:
            magnitudes = {
                "base_imponible": base_imponible,
                "base_liquidable": base_liquidable,
                "cuota_tributaria": cuota_tributaria,
            }
            porcentaje = _porcentaje_bonificacion(regla, magnitudes[regla.tramos_sobre or "base_imponible"])
            bonificacion = cuota_tributaria * porcentaje
            if regla.importe_maximo is not None:
                bonificacion = min(bonificacion, regla.importe_maximo)
            bonificacion = _round(bonificacion)
            t.linea(f"{regla.titulo} ({_pct(porcentaje)})", -bonificacion, regla.articulo)
            t.usa(regla, regla.titulo)
    cuota_a_ingresar = cuota_tributaria - bonificacion
    t.linea("Cuota a ingresar", cuota_a_ingresar)

    _avisos_generales(inp, p, clave, comunidad, t)
    fuentes = [estatal.fuente_principal] + ([comunidad.fuente_principal] if comunidad else [])
    return ResultadoISD(
        ejercicio=inp.ejercicio,
        modo=inp.modo,
        comunidad=clave,
        grupo=grupo,
        parentesco=parentesco,
        base_imponible=_round(base_imponible),
        reducciones=_round(reducciones),
        base_liquidable=_round(base_liquidable),
        cuota_integra=_round(cuota_integra),
        coeficiente=coeficiente,
        cuota_tributaria=cuota_tributaria,
        bonificacion=bonificacion,
        cuota_a_ingresar=cuota_a_ingresar,
        tipo_efectivo=(cuota_a_ingresar / base_imponible).quantize(Decimal("0.0001")) if base_imponible else ZERO,
        lineas=t.lineas,
        avisos=t.avisos,
        fuente="; ".join(f"{f.norma}, {f.boe_id}, consolidado a {f.consolidado_a}" for f in fuentes),
    )


def _caso_minimo(inp: BeneficiarioISD) -> _Caso:
    """Enough of the case to check the conditions of an assimilation (mode only, in practice)."""
    return _Caso(
        modo=inp.modo,
        grupo=inp.grupo or "IV",
        parentesco=inp.parentesco,
        edad=inp.edad,
        discapacidad=inp.grado_discapacidad,
        patrimonio=inp.patrimonio_preexistente,
        requisitos=frozenset(),
    )


def _avisos_generales(
    inp: BeneficiarioISD, p: ISDParams, clave: str | None, comunidad: NormativaAutonomicaISD | None, t: _Traza
) -> None:
    estatal = p.estatal
    t.aviso(estatal.puntos_conexion.resumen)
    if comunidad is None:
        if inp.comunidad:
            cargadas = ", ".join(c.nombre for c in p.comunidades.values())
            t.aviso(
                f"No hay normativa de «{inp.comunidad}» cargada para este impuesto (cargadas: {cargadas}). Se ha "
                "aplicado SOLO la ley estatal: casi todas las comunidades tienen reducciones y bonificaciones "
                "propias, así que lo que de verdad se paga suele ser menos, a veces mucho menos."
            )
        else:
            t.aviso(
                "No has indicado comunidad autónoma: se ha aplicado solo la ley estatal (la que rige, por ejemplo, "
                "para no residentes). Casi todas las comunidades tienen reducciones y bonificaciones propias."
            )
    else:
        for nota in comunidad.notas:
            t.aviso(f"{comunidad.nombre}: {nota}")
        otras = [o for o in comunidad.otras_reducciones if o.modo == inp.modo]
        if otras:
            t.aviso(
                f"{comunidad.nombre} tiene además estas reducciones para {_ETIQUETA_MODO[inp.modo]}, que no se "
                "calculan (si te corresponde alguna, indícala como importe en otras_reducciones): "
                + "; ".join(f"{o.titulo} ({o.articulo})" for o in otras)
                + "."
            )
        for item in comunidad.no_modelado:
            t.aviso(f"{comunidad.nombre}, no modelado: {item}")
    if inp.fecha_devengo is None:
        t.aviso(
            f"No has indicado la fecha de devengo: se ha aplicado la redacción vigente cuando se leyó la norma "
            f"({estatal.fuente_principal.consultado_el}). Si la norma cambió durante {inp.ejercicio}, el resultado "
            "puede no valer para una fecha anterior al cambio."
        )
    t.aviso(estatal.acumulacion.resumen)
    for nota in estatal.notas:
        t.aviso(nota)


def calcular_isd_varios(beneficiarios: list[BeneficiarioISD], params: ISDParams | None = None) -> list[ResultadoISD]:
    """One result per heir or donee (each one is taxed separately on what they receive)."""
    return [calcular_isd(b, params) for b in beneficiarios]


__all__ = [
    "BeneficiarioISD",
    "Parentesco",
    "ResultadoISD",
    "calcular_isd",
    "calcular_isd_varios",
    "grupo_de_parentesco",
]
