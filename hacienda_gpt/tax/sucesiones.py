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
    Seccion,
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
    # Value of the whole property, for the laws that grade the percentage by it ("valor real del inmueble").
    valor_total_vivienda: Decimal | None = Field(default=None, gt=0)
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
        if self.valor_total_vivienda is not None and self.valor_total_vivienda < self.valor_vivienda_habitual:
            raise ValueError("valor_total_vivienda no puede ser menor que la parte de la vivienda que se adquiere")
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


@dataclass(frozen=True)
class _Vista:
    """The beneficiary as each legislator's rules see them.

    They differ only when the community assimilates the relationship to another
    one for its own rules alone (``Equiparacion.alcance == "autonomico"``): then
    ``propio`` carries the assimilated relationship and group, ``estatal`` the real ones.
    """

    estatal: _Caso
    propio: _Caso
    solo_en: frozenset[str] = frozenset()  # the community's lists that see ``propio``; empty = all

    def autonomico(self, seccion: Seccion) -> _Caso:
        return self.propio if not self.solo_en or seccion in self.solo_en else self.estatal

    def con_base(self, base_imponible: Decimal, base_liquidable: Decimal | None = None) -> _Vista:
        return _Vista(
            replace(self.estatal, base_imponible=base_imponible, base_liquidable=base_liquidable),
            replace(self.propio, base_imponible=base_imponible, base_liquidable=base_liquidable),
            self.solo_en,
        )


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

    def sub(self) -> _Traza:
        """A scratch trace for a candidate that may be discarded."""
        return _Traza(self.fecha)

    def absorbe(self, otra: _Traza) -> None:
        self.lineas.extend(otra.lineas)
        for aviso in otra.avisos:
            self.aviso(aviso)

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


def _elegir[R: _Regla](
    propias: Sequence[R], estatales: Sequence[R], vista: _Vista, seccion: Seccion, t: _Traza
) -> tuple[R, _Caso] | None:
    """The community's rule when one applies, the state's otherwise — each with the case it is read against."""
    caso_propio = vista.autonomico(seccion)
    propia = _primera(propias, caso_propio, t)
    if propia is not None:
        return propia, caso_propio
    del_estado = _primera(estatales, vista.estatal, t)
    return (del_estado, vista.estatal) if del_estado is not None else None


def _mas_favorable[R: _Regla](
    candidatos: Sequence[tuple[R, Decimal | None, _Traza]], etiqueta: str, t: _Traza
) -> Decimal:
    """Keep the candidate with the larger reduction (the community's first on a tie) and say which one it was.

    ``candidatos``: the community's rule first and, when it defers to a more favourable
    state reduction, the state's second; an amount of None = the beneficiary does not qualify.
    """
    if not candidatos:
        return ZERO
    elegido = max(candidatos, key=lambda c: c[1] if c[1] is not None else Decimal(-1))
    t.absorbe(elegido[2])
    if len(candidatos) == 2:
        propia, del_estado = candidatos
        if elegido is del_estado and elegido is not propia:
            t.aviso(
                f"{etiqueta}: {propia[0].articulo} ordena aplicar la reducción estatal cuando es más favorable; se ha "
                f"aplicado la estatal ({del_estado[0].articulo}): {_eur(del_estado[1] or ZERO)} frente a "
                f"{_eur(propia[1] or ZERO)} con la regla autonómica."
            )
        elif del_estado[1] is not None:
            t.aviso(
                f"{etiqueta}: se ha comparado con la reducción estatal ({del_estado[0].articulo}, "
                f"{_eur(del_estado[1])}) y se ha aplicado la autonómica ({propia[0].articulo}), que no es menor."
            )
    return elegido[1] or ZERO


def grupo_de_parentesco(parentesco: str, edad: int | None) -> Grupo:
    """Kinship group of art. 20.2.a) Ley 29/1987. A descendant of unknown age is taken as 21 or older."""
    clase = _CLASE_PARENTESCO[parentesco]
    if clase == "descendiente":
        return "I" if edad is not None and edad < 21 else "II"
    if clase in {"conyuge", "ascendiente"}:
        return "II"
    return "III" if clase in {"colateral", "afinidad"} else "IV"


def _importe_tramo(tramo: TramoReduccion, edad: int | None) -> Decimal:
    """Amount of a fixed-amount tramo (the target, for a "hasta completar" one)."""
    assert tramo.importe is not None
    importe = tramo.importe
    if tramo.incremento_por_ano is not None and tramo.edad_referencia is not None and edad is not None:
        importe += tramo.incremento_por_ano * max(0, tramo.edad_referencia - edad)
    return min(importe, tramo.maximo) if tramo.maximo is not None else importe


def _reducciones_personales(
    estatal: NormativaISD, comunidad: NormativaAutonomicaISD | None, vista: _Vista, t: _Traza
) -> list[tuple[Reduccion, TramoReduccion]]:
    """Personal reductions that apply, state ones first; a regional "mejora" displaces the state one of its class."""

    def aplicables(reducciones: list[Reduccion], caso: _Caso) -> list[tuple[Reduccion, TramoReduccion]]:
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

    propias = aplicables(comunidad.reducciones, vista.autonomico("reducciones")) if comunidad is not None else []
    del_estado = aplicables(estatal.reducciones, vista.estatal)
    edad = vista.estatal.edad
    for propia in list(propias):
        reduccion, tramo = propia
        if not reduccion.estatal_si_mas_favorable or reduccion.caracter != "mejora" or tramo.importe is None:
            continue
        rival = next((x for x in del_estado if x[0].clase == reduccion.clase and x[1].importe is not None), None)
        if rival is None or tramo.hasta_completar:
            continue
        importe_propio, importe_estatal = _importe_tramo(tramo, edad), _importe_tramo(rival[1], edad)
        if importe_estatal > importe_propio:
            propias.remove(propia)
            t.aviso(
                f"«{reduccion.titulo}»: {reduccion.articulo} ordena aplicar la reducción estatal cuando es más "
                f"favorable; se ha aplicado la estatal ({rival[0].articulo}): {_eur(importe_estatal)} frente a "
                f"{_eur(importe_propio)} con la regla autonómica."
            )
    mejoradas = {r.clase for r, _ in propias if r.caracter == "mejora"}
    elegidas = [(r, tr) for r, tr in del_estado if r.clase not in mejoradas] + propias
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
    if regla.mantiene_limite_estatal:
        t.aviso(
            f"Vivienda habitual: {regla.articulo} solo mejora el porcentaje y mantiene los demás límites estatales; "
            f"se ha aplicado el límite de {_eur(del_estado.limite_por_sujeto)} por adquirente ({del_estado.articulo})."
        )
    else:
        t.aviso(
            f"Vivienda habitual: {regla.articulo} mejora el porcentaje pero no menciona límite; se ha mantenido el "
            f"límite estatal de {_eur(del_estado.limite_por_sujeto)} por adquirente ({del_estado.articulo}). "
            "Confírmalo con la Administración autonómica."
        )
    return del_estado.limite_por_sujeto


def _porcentaje_vivienda(regla: ReduccionVivienda, inp: BeneficiarioISD, t: _Traza) -> Decimal:
    if regla.porcentaje is not None:
        return regla.porcentaje
    total = inp.valor_total_vivienda
    if total is None and inp.participacion_vivienda is not None:
        total = inp.valor_vivienda_habitual / inp.participacion_vivienda
        t.aviso(
            f"Vivienda habitual ({regla.articulo}): el porcentaje depende del valor total del inmueble; se ha "
            f"estimado en {_eur(total)} a partir de tu parte y tu participación. Indica valor_total_vivienda si "
            "es otro."
        )
    if total is None:
        porcentaje = min(tramo.porcentaje for tramo in regla.tramos_valor)
        t.aviso(
            f"Vivienda habitual ({regla.articulo}): el porcentaje depende del valor total del inmueble, que no has "
            f"indicado (valor_total_vivienda); se ha aplicado el más bajo de la escala ({_pct(porcentaje)})."
        )
        return porcentaje
    return next(tr.porcentaje for tr in regla.tramos_valor if tr.valor_hasta is None or total <= tr.valor_hasta)


def _vivienda_con(
    regla: ReduccionVivienda, caso: _Caso, estatal: NormativaISD, inp: BeneficiarioISD, t: _Traza
) -> Decimal | None:
    """The main-home reduction under ``regla``, traced on ``t``; None when the beneficiary does not qualify."""
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
            return None
        if caso.edad is None or caso.edad <= regla.colateral_mayor_de:
            t.aviso(
                f"Reducción por vivienda habitual ({regla.articulo}) no aplicada: un pariente colateral ha de ser "
                f"mayor de {regla.colateral_mayor_de} años (y haber convivido con el causante)."
            )
            return None
        t.aviso(
            f"Reducción por vivienda habitual ({regla.articulo}): como pariente colateral, exige haber convivido con "
            f"el causante durante los {regla.colateral_anos_convivencia or 'dos'} años anteriores al fallecimiento; "
            "no se ha comprobado."
        )
    porcentaje = _porcentaje_vivienda(regla, inp, t)
    importe = inp.valor_vivienda_habitual * porcentaje
    limite = _limite_vivienda(regla, estatal, inp, caso, t)
    if limite is not None:
        importe = min(importe, limite)
    t.linea(f"Reducción por vivienda habitual del causante ({_pct(porcentaje)})", -importe, regla.articulo)
    t.usa(regla, "Reducción por vivienda habitual")
    t.aviso(
        f"Reducción por vivienda habitual ({regla.articulo}): se ha calculado sobre el valor que has declarado y "
        f"queda condicionada a mantener la vivienda durante {regla.anos_mantenimiento} años desde el fallecimiento "
        "(salvo fallecimiento del adquirente); si se incumple hay que ingresar lo no pagado con intereses de demora."
    )
    return importe


def _reduccion_vivienda(
    estatal: NormativaISD, comunidad: NormativaAutonomicaISD | None, inp: BeneficiarioISD, vista: _Vista, t: _Traza
) -> Decimal:
    if not inp.valor_vivienda_habitual:
        return ZERO
    caso_propio = vista.autonomico("vivienda_habitual")
    propia = _primera(comunidad.vivienda_habitual, caso_propio, t) if comunidad is not None else None
    del_estado = _primera(estatal.vivienda_habitual, vista.estatal, t)
    reglas = [(propia, caso_propio)] if propia is not None else []
    if del_estado is not None and (propia is None or propia.estatal_si_mas_favorable):
        reglas.append((del_estado, vista.estatal))
    if not reglas:
        t.aviso("No hay regla cargada para la reducción por vivienda habitual; no se ha aplicado.")
        return ZERO
    candidatos = []
    for regla, caso in reglas:
        sub = t.sub()
        candidatos.append((regla, _vivienda_con(regla, caso, estatal, inp, sub), sub))
    return _mas_favorable(candidatos, "Reducción por vivienda habitual", t)


def _reduccion_seguro(
    estatal: NormativaISD, comunidad: NormativaAutonomicaISD | None, inp: BeneficiarioISD, vista: _Vista, t: _Traza
) -> Decimal:
    if not inp.seguro_vida:
        return ZERO
    caso_propio = vista.autonomico("seguro_vida")
    propia = _primera(comunidad.seguro_vida, caso_propio, t) if comunidad is not None else None
    del_estado = _primera(estatal.seguro_vida, vista.estatal, t)
    reglas = [(propia, caso_propio)] if propia is not None else []
    if del_estado is not None and (propia is None or propia.estatal_si_mas_favorable):
        reglas.append((del_estado, vista.estatal))
    candidatos = []
    for regla, caso in reglas:
        sub = t.sub()
        clase = _CLASE_PARENTESCO[caso.parentesco] if caso.parentesco else None
        directo = clase in {"conyuge", "descendiente", "ascendiente"} or (clase is None and caso.grupo in {"I", "II"})
        importe: Decimal | None = None
        if directo:
            importe = inp.seguro_vida * regla.porcentaje
            if regla.limite is not None:
                importe = min(importe, regla.limite)
            sub.linea("Reducción por seguro de vida", -importe, regla.articulo)
            sub.usa(regla, "Reducción por seguro de vida")
        else:
            sub.aviso(
                f"Reducción por seguro de vida ({regla.articulo}) no aplicada: es para cónyuge, ascendientes y "
                "descendientes del contratante."
            )
        candidatos.append((regla, importe, sub))
    return _mas_favorable(candidatos, "Reducción por seguro de vida", t)


def _reduccion_bienes(
    estatal: NormativaISD,
    comunidad: NormativaAutonomicaISD | None,
    clase_bienes: str,
    valor: Decimal,
    etiqueta: str,
    vista: _Vista,
    t: _Traza,
) -> Decimal:
    caso_propio = vista.autonomico("reducciones_bienes")
    propias = [r for r in (comunidad.reducciones_bienes if comunidad else []) if r.clase == clase_bienes]
    propia = _primera(propias, caso_propio, t)
    del_estado = _primera([r for r in estatal.reducciones_bienes if r.clase == clase_bienes], vista.estatal, t)
    reglas = [propia] if propia is not None else []
    if del_estado is not None and (propia is None or propia.estatal_si_mas_favorable):
        reglas.append(del_estado)
    if not reglas:
        t.aviso(f"No hay regla cargada para la reducción por {etiqueta}; no se ha aplicado.")
        return ZERO
    candidatos = []
    for regla in reglas:
        sub = t.sub()
        importe = valor * regla.porcentaje
        sub.linea(f"Reducción por {etiqueta} ({_pct(regla.porcentaje)})", -importe, regla.articulo)
        sub.usa(regla, f"Reducción por {etiqueta}")
        sub.aviso(
            f"Reducción por {etiqueta} ({regla.articulo}): se ha aplicado sobre el valor que has declarado, sin "
            f"comprobar ninguno de sus requisitos. {regla.resumen}"
        )
        candidatos.append((regla, importe, sub))
    if comunidad is not None and not propias:
        t.aviso(
            f"{comunidad.nombre} puede tener una reducción propia o mejorada por {etiqueta} que no está "
            "cargada; se ha aplicado la estatal."
        )
    return _mas_favorable(candidatos, f"Reducción por {etiqueta}", t)


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


def _bonificar(
    regla: ReglaBonificacion,
    cuota: Decimal,
    inp: BeneficiarioISD,
    magnitudes: dict[str, Decimal],
    t: _Traza,
    *,
    sucesiva: bool = False,
) -> Decimal:
    """Allowance of ``regla`` on ``cuota`` (the whole tax, or what an earlier allowance left of it)."""
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
        return ZERO
    porcentaje = _porcentaje_bonificacion(regla, magnitudes[regla.tramos_sobre or "base_imponible"])
    bonificacion = cuota * porcentaje
    if regla.importe_maximo is not None:
        bonificacion = min(bonificacion, regla.importe_maximo)
    bonificacion = _round(bonificacion)
    sobre = " sobre la cuota restante" if sucesiva else ""
    t.linea(f"{regla.titulo} ({_pct(porcentaje)}{sobre})", -bonificacion, regla.articulo)
    t.usa(regla, regla.titulo)
    return bonificacion


def _coeficiente(
    estatal: NormativaISD,
    comunidad: NormativaAutonomicaISD | None,
    vista: _Vista,
    cuota_integra: Decimal,
    t: _Traza,
) -> tuple[Decimal, Decimal, Grupo]:
    """(coefficient, tax after it, group it was read for), with the excess clause of art. 22.2 when the rule has it."""
    elegida = _elegir(comunidad.coeficientes if comunidad else [], estatal.coeficientes, vista, "coeficientes", t)
    if elegida is None:
        raise ValueError("no hay coeficientes multiplicadores cargados")
    regla, caso = elegida
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
    return coeficiente, cuota, caso.grupo


_ETIQUETA_SECCION = {
    "reducciones": "reducciones personales",
    "vivienda_habitual": "reducción por vivienda habitual",
    "seguro_vida": "reducción por seguro de vida",
    "reducciones_bienes": "reducciones por empresa o patrimonio histórico",
    "tarifas": "tarifa",
    "coeficientes": "coeficientes",
    "bonificaciones": "bonificaciones",
}


def _quien(inp: BeneficiarioISD, comunidad: NormativaAutonomicaISD | None, t: _Traza) -> _Vista:
    """Relationship and kinship group, as the state rules and as the community's own rules see them."""
    real = inp.parentesco
    equiparacion = None
    if comunidad is not None and real is not None:
        # The first assimilation whose conditions hold: a community may have one per mode.
        equiparacion = next(
            (
                e
                for e in comunidad.equiparaciones
                if e.parentesco == real and not _incumple(e.cuando, _caso_minimo(inp))
            ),
            None,
        )
    asimilado = equiparacion.equivale_a if equiparacion is not None else real
    solo_propio = equiparacion is not None and equiparacion.alcance == "autonomico"
    del_estado = real if solo_propio else asimilado

    def caso(parentesco: str | None) -> _Caso:
        if parentesco is not None:
            grupo = grupo_de_parentesco(parentesco, inp.edad)
        else:
            assert inp.grupo is not None
            grupo = inp.grupo
        return _Caso(
            modo=inp.modo,
            grupo=grupo,
            parentesco=parentesco,
            edad=inp.edad,
            discapacidad=inp.grado_discapacidad,
            patrimonio=inp.patrimonio_preexistente,
            requisitos=frozenset({"escritura_publica"} if inp.escritura_publica else ()),
        )

    vista = _Vista(caso(del_estado), caso(asimilado), frozenset(equiparacion.solo_en) if equiparacion else frozenset())
    if equiparacion is not None and comunidad is not None and real is not None:
        de, a = real.replace("_", " "), equiparacion.equivale_a.replace("_", " ")
        if solo_propio:
            ambito = (
                "sus " + ", ".join(_ETIQUETA_SECCION[s] for s in equiparacion.solo_en)
                if equiparacion.solo_en
                else "las reducciones, tarifa, coeficientes y bonificaciones que ella misma ha regulado"
            )
            t.aviso(
                f"{comunidad.nombre} equipara «{de}» a «{a}» SOLO para {ambito} ({equiparacion.articulo}): "
                f"{equiparacion.resumen} En todo lo demás —las reglas de la ley estatal que sigan siendo aplicables, "
                f"entre ellas su coeficiente multiplicador si la comunidad no tiene uno propio— se ha mantenido el "
                f"parentesco real (grupo {vista.estatal.grupo})."
            )
        else:
            t.aviso(f"{comunidad.nombre} equipara «{de}» a «{a}» ({equiparacion.articulo}): {equiparacion.resumen}")
        t.usa(equiparacion, "Equiparación")
    if del_estado is None:
        t.aviso(
            "Solo has indicado el grupo de parentesco: las reglas que dependen del parentesco exacto (cónyuge, "
            "hijo, ascendiente…) no se han podido aplicar."
        )
        return vista
    if _CLASE_PARENTESCO[del_estado] == "descendiente" and inp.edad is None:
        t.aviso(
            "No has indicado la edad del descendiente: se le ha tratado como de 21 años o más (grupo II). "
            "Los menores de 21 (grupo I) tienen una reducción mayor."
        )
    if inp.grupo is not None and inp.grupo != vista.estatal.grupo:
        t.aviso(
            f"Has indicado el grupo {inp.grupo}, pero al parentesco «{del_estado.replace('_', ' ')}» le "
            f"corresponde el grupo {vista.estatal.grupo} (art. 20.2.a) Ley 29/1987); se ha aplicado el grupo "
            f"{vista.estatal.grupo}."
        )
    if del_estado == "pareja_de_hecho" and equiparacion is None:
        t.aviso(
            "La ley estatal no equipara la pareja de hecho al cónyuge: se la ha tratado como extraño (grupo IV). "
            "Muchas comunidades sí la equiparan si está inscrita."
        )
    if del_estado == "colateral_afinidad":
        t.aviso(
            "El art. 20.2.a) habla de «colaterales de segundo y tercer grado» sin distinguir consanguinidad y "
            "afinidad; se ha aplicado el grupo III al colateral por afinidad."
        )
    return vista


def calcular_isd(inp: BeneficiarioISD, params: ISDParams | None = None) -> ResultadoISD:
    """Tax due by one beneficiary, with the trace of the computation and what was not checked."""
    p = params or load_isd_params(inp.ejercicio)
    estatal = p.estatal
    t = _Traza(inp.fecha_devengo)
    encontrada = p.comunidad(inp.comunidad)
    clave, comunidad = encontrada if encontrada else (None, None)
    vista = _quien(inp, comunidad, t)

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
    vista = vista.con_base(base_imponible)

    # --- Reductions ---------------------------------------------------------------
    # Fixed amounts first; then the ones measured on the base ("100 por 100 de la base imponible") and the
    # ones that only top the others up ("hasta completar"), which need to know what the rest add up to.
    total = ZERO
    por_categoria: dict[str, Decimal] = {}
    sobre_la_base: list[tuple[Reduccion, TramoReduccion]] = []
    for reduccion, tramo in _reducciones_personales(estatal, comunidad, vista, t):
        if tramo.hasta_completar or tramo.porcentaje_base is not None:
            sobre_la_base.append((reduccion, tramo))
            continue
        importe = _importe_tramo(tramo, inp.edad)
        total += importe
        t.linea(reduccion.titulo, -importe, reduccion.articulo)
        t.usa(reduccion, reduccion.titulo)
    por_categoria["seguro_vida"] = _reduccion_seguro(estatal, comunidad, inp, vista, t)
    por_categoria["vivienda_habitual"] = _reduccion_vivienda(estatal, comunidad, inp, vista, t)
    for clase_bienes, valor, etiqueta in (
        ("empresa", inp.valor_empresa_familiar, "empresa individual, negocio profesional o participaciones"),
        ("patrimonio_historico", inp.valor_patrimonio_historico, "bienes del patrimonio histórico o cultural"),
    ):
        if valor:
            por_categoria[clase_bienes] = _reduccion_bienes(estatal, comunidad, clase_bienes, valor, etiqueta, vista, t)
    if inp.otras_reducciones:
        por_categoria["otras_reducciones"] = inp.otras_reducciones
        t.linea("Otras reducciones declaradas", -inp.otras_reducciones)
        t.aviso("«Otras reducciones declaradas» se ha restado tal como la has indicado, sin comprobar nada.")
    total += sum(por_categoria.values(), ZERO)
    for reduccion, tramo in sorted(sobre_la_base, key=lambda par: par[1].hasta_completar):
        restante = max(ZERO, base_imponible - total)
        if tramo.porcentaje_base is not None:
            importe = base_imponible * tramo.porcentaje_base
            if tramo.maximo is not None:
                importe = min(importe, tramo.maximo)
            concepto = f"{reduccion.titulo} ({_pct(tramo.porcentaje_base)} de la base imponible)"
        else:
            objetivo = _importe_tramo(tramo, inp.edad)
            no_cuentan = sum((por_categoria.get(c, ZERO) for c in tramo.hasta_completar_excluye), ZERO)
            importe = max(ZERO, objetivo - (total - no_cuentan))
            concepto = f"{reduccion.titulo} (hasta completar {_eur(objetivo)})"
        # Never more than what is left of the base: the line shows what is really applied.
        importe = min(importe, restante)
        total += importe
        t.linea(concepto, -importe, reduccion.articulo)
        t.usa(reduccion, reduccion.titulo)
    if total > base_imponible:
        t.aviso("Las reducciones superan la base imponible: la base liquidable es cero.")
    reducciones = min(total, base_imponible)
    base_liquidable = base_imponible - reducciones
    t.linea("Base liquidable", base_liquidable, "art. 20 Ley 29/1987")
    vista = vista.con_base(base_imponible, base_liquidable)

    # --- Tax ------------------------------------------------------------------------
    tarifa = _elegir(comunidad.tarifas if comunidad else [], estatal.tarifas, vista, "tarifas", t)
    if tarifa is None:
        raise ValueError("no hay tarifa cargada")
    cuota_integra = tarifa[0].escala.cuota(base_liquidable)
    t.linea("Cuota íntegra", cuota_integra, tarifa[0].articulo)
    t.usa(tarifa[0], "Tarifa")
    coeficiente, cuota_tributaria, grupo = _coeficiente(estatal, comunidad, vista, cuota_integra, t)
    cuota_tributaria = _round(cuota_tributaria)
    t.linea("Cuota tributaria", cuota_tributaria, "art. 22 Ley 29/1987")

    bonificacion = ZERO
    if comunidad is not None:
        caso_bonificacion = vista.autonomico("bonificaciones")
        magnitudes = {
            "base_imponible": base_imponible,
            "base_liquidable": base_liquidable,
            "cuota_tributaria": cuota_tributaria,
        }
        primera = _primera(comunidad.bonificaciones, caso_bonificacion, t)
        if primera is not None:
            bonificacion = _bonificar(primera, cuota_tributaria, inp, magnitudes, t)
        despues = _primera(comunidad.bonificaciones_sucesivas, caso_bonificacion, t)
        if despues is not None:
            bonificacion += _bonificar(
                despues, cuota_tributaria - bonificacion, inp, magnitudes, t, sucesiva=primera is not None
            )
    cuota_a_ingresar = cuota_tributaria - bonificacion
    t.linea("Cuota a ingresar", cuota_a_ingresar)

    _avisos_generales(inp, p, clave, comunidad, t)
    fuentes = [estatal.fuente_principal] + ([comunidad.fuente_principal] if comunidad else [])
    return ResultadoISD(
        ejercicio=inp.ejercicio,
        modo=inp.modo,
        comunidad=clave,
        grupo=grupo,
        parentesco=vista.estatal.parentesco,
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
