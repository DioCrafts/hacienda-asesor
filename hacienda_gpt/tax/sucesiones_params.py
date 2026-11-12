"""Parameters of the Impuesto sobre Sucesiones y Donaciones (ISD), as versioned data.

The state rules (Ley 29/1987) and what each autonomous community legislates on
top of them share one schema, :class:`NormativaISD`: lists of *rules*. A rule
says when it applies (:class:`CondicionesISD`), carries its figures, the article
it was read from, the requirements the calculator cannot check and the literal
quotes of the law that contain every one of its figures. Where several rules of
a list could apply, the first one whose conditions hold wins.

A community is therefore pure data (:class:`NormativaAutonomicaISD`); the engine
in ``sucesiones.py`` knows no community by name.

:func:`comprobar_evidencias` re-reads the data mechanically: every quote must be
in the law verbatim and every figure must be spelled inside one of its rule's
quotes. Command line (also for whoever encodes a new community)::

    python -m hacienda_gpt.tax.sucesiones_params texto <page.html>
    python -m hacienda_gpt.tax.sucesiones_params comprobar <ejercicio> <dir with pages>
    python -m hacienda_gpt.tax.sucesiones_params comprobar-ccaa <community.json> <dir with pages>
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
import html
import json
from pathlib import Path
import re
import sys
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from hacienda_gpt.tax.params import (
    DATA_DIR,
    EjercicioNoDisponibleError,
    Escala,
    FuentePrincipal,
    normalizar_comunidad,
)

Modo = Literal["sucesion", "donacion"]
Grupo = Literal["I", "II", "III", "IV"]
# Relationship of the beneficiary to the deceased / donor. Adopted children and
# adoptive parents are "hijo" / "padre"; see PARENTESCO_ALIAS for what users type.
Parentesco = Literal[
    "conyuge",
    "pareja_de_hecho",
    "hijo",
    "otro_descendiente",
    "padre",
    "otro_ascendiente",
    "hermano",
    "sobrino",
    "tio",
    "colateral_afinidad",  # brother/sister-in-law, uncle/nephew by marriage (2nd-3rd degree)
    "hijastro",
    "yerno_nuera",
    "padrastro",
    "suegro",
    "colateral_cuarto_grado",  # first cousins
    "colateral_lejano",
    "extrano",
]
# Facts only the user can state and a rule may demand.
Requisito = Literal["escritura_publica"]
ClaseReduccion = Literal["parentesco", "discapacidad", "edad", "otra"]
ClaseBienes = Literal["empresa", "patrimonio_historico"]

PARENTESCOS: tuple[str, ...] = get_args(Parentesco)
PARENTESCO_ALIAS: dict[str, str] = {
    "esposo": "conyuge",
    "esposa": "conyuge",
    "marido": "conyuge",
    "mujer": "conyuge",
    "viudo": "conyuge",
    "viuda": "conyuge",
    "pareja": "pareja_de_hecho",
    "pareja de hecho": "pareja_de_hecho",
    "pareja estable": "pareja_de_hecho",
    "hija": "hijo",
    "adoptado": "hijo",
    "adoptada": "hijo",
    "descendiente": "otro_descendiente",
    "nieto": "otro_descendiente",
    "nieta": "otro_descendiente",
    "bisnieto": "otro_descendiente",
    "bisnieta": "otro_descendiente",
    "madre": "padre",
    "adoptante": "padre",
    "ascendiente": "otro_ascendiente",
    "abuelo": "otro_ascendiente",
    "abuela": "otro_ascendiente",
    "hermana": "hermano",
    "sobrina": "sobrino",
    "tia": "tio",
    "cunado": "colateral_afinidad",
    "cunada": "colateral_afinidad",
    "hijastra": "hijastro",
    "yerno": "yerno_nuera",
    "nuera": "yerno_nuera",
    "madrastra": "padrastro",
    "suegra": "suegro",
    "primo": "colateral_cuarto_grado",
    "prima": "colateral_cuarto_grado",
    "extrana": "extrano",
    "amigo": "extrano",
    "amiga": "extrano",
    "sin parentesco": "extrano",
    "ninguno": "extrano",
}


def normalizar_parentesco(valor: str) -> str:
    """'Hija' -> 'hijo', 'cuñado' -> 'colateral_afinidad'; ValueError when unknown."""
    plano = normalizar_comunidad(valor).replace("_", " ")
    canonico = PARENTESCO_ALIAS.get(plano, plano.replace(" ", "_"))
    if canonico not in PARENTESCOS:
        raise ValueError(f"parentesco desconocido: «{valor}» (admitidos: {', '.join(PARENTESCOS)})")
    return canonico


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Cita(_Model):
    """A literal passage of a consolidated text on boe.es."""

    boe_id: str
    articulo: str
    cita: str


class CondicionesISD(_Model):
    """When a rule applies. ALL the conditions present must hold; an empty one always holds.

    Bounds are named after the law's own wording so that the number stored is the
    number printed: "menores de veintiún años" is ``edad_menor_de: 21``, "de
    setenta y cinco años o más" is ``edad_desde: 75``, "cuya base imponible no
    supere los 1.000 euros" is ``base_imponible_hasta: 1000``.
    """

    modo: Modo | None = None  # None = inheritances and gifts alike
    grupos: list[Grupo] = Field(default_factory=list)  # any of them
    parentescos: list[Parentesco] = Field(default_factory=list)  # any of them
    edad_desde: int | None = None  # inclusive
    edad_hasta: int | None = None  # inclusive
    edad_menor_de: int | None = None  # exclusive
    edad_mayor_de: int | None = None  # exclusive
    discapacidad_desde: int | None = None  # degree, inclusive
    discapacidad_menor_de: int | None = None  # exclusive
    base_imponible_hasta: Decimal | None = None  # inclusive ("no supere")
    base_imponible_menor_de: Decimal | None = None  # exclusive ("inferior a")
    # Only for tariff, coefficient and allowance rules (the base does not exist yet for reductions).
    base_liquidable_hasta: Decimal | None = None
    base_liquidable_menor_de: Decimal | None = None
    patrimonio_preexistente_hasta: Decimal | None = None
    patrimonio_preexistente_menor_de: Decimal | None = None
    requiere: list[Requisito] = Field(default_factory=list)


class _Regla(_Model):
    """What every rule carries, whatever it computes."""

    articulo: str
    resumen: str  # one or two sentences in Spanish, shown to the user
    en_vigor_desde: str  # of the wording that was read (YYYY-MM-DD)
    cuando: CondicionesISD = Field(default_factory=CondicionesISD)
    # Legal conditions the calculator cannot see; repeated whenever the rule is applied.
    requisitos_no_comprobados: list[str] = Field(default_factory=list)
    evidencia: list[Cita] = Field(min_length=1)


class TramoReduccion(_Model):
    """One amount of a personal reduction, for the beneficiaries who meet ``cuando``."""

    cuando: CondicionesISD = Field(default_factory=CondicionesISD)
    importe: Decimal
    # "más X euros por cada año menos de veintiuno": importe + X × (edad_referencia − age), capped by ``maximo``.
    incremento_por_ano: Decimal | None = None
    edad_referencia: int | None = None
    maximo: Decimal | None = None
    # The law grants "the amount needed to reach ``importe`` together with the other reductions".
    hasta_completar: bool = False

    @model_validator(mode="after")
    def _coherente(self) -> TramoReduccion:
        if (self.incremento_por_ano is None) != (self.edad_referencia is None):
            raise ValueError("incremento_por_ano y edad_referencia van juntos")
        return self


class Reduccion(_Regla):
    """A fixed-amount reduction of the taxable base tied to the person (kinship, disability, age).

    ``caracter`` is what the community must declare by law (art. 48.1.a Ley
    22/2009): a "mejora" REPLACES the state reduction of the same ``clase`` for
    the beneficiaries one of its ``tramos`` covers (the others keep the state
    one); a "propia" is applied in addition, after the state ones.
    """

    clave: str
    titulo: str
    clase: ClaseReduccion
    caracter: Literal["estatal", "mejora", "propia"]
    tramos: list[TramoReduccion] = Field(min_length=1)  # first match wins
    # ``clave`` of reductions that exclude this one when they have been applied.
    incompatible_con: list[str] = Field(default_factory=list)


class ReduccionVivienda(_Regla):
    """Reduction for inheriting the deceased's main home (art. 20.2.c Ley 29/1987 and regional improvements)."""

    porcentaje: Decimal
    # Ceiling of the reduction per beneficiary. None with ``sin_limite`` false and no
    # ``limite_conjunto`` = the article does not restate a ceiling: the state one is kept.
    limite_por_sujeto: Decimal | None = None
    sin_limite: bool = False  # only when the law says so in as many words
    # Ceiling for the home as a whole, shared in proportion to each one's share...
    limite_conjunto: Decimal | None = None
    # ...with this floor for each beneficiary's share of it.
    limite_individual_minimo: Decimal | None = None
    anos_mantenimiento: int
    # Collateral relatives qualify only above this age (exclusive) and having lived with the deceased.
    colateral_mayor_de: int | None = None
    colateral_anos_convivencia: int | None = None

    @model_validator(mode="after")
    def _coherente(self) -> ReduccionVivienda:
        if self.sin_limite and (self.limite_por_sujeto is not None or self.limite_conjunto is not None):
            raise ValueError("sin_limite no admite límites")
        if self.limite_individual_minimo is not None and self.limite_conjunto is None:
            raise ValueError("limite_individual_minimo exige limite_conjunto")
        if not 0 < self.porcentaje <= 1:
            raise ValueError("porcentaje fuera de rango (0-1)")
        return self


class ReduccionSeguro(_Regla):
    """Reduction of life-insurance proceeds for spouse, ascendants and descendants (art. 20.2.b)."""

    porcentaje: Decimal
    limite: Decimal | None


class ReduccionBienes(_Regla):
    """Percentage reduction of the value of certain assets, on the amount the user declares as qualifying."""

    clase: ClaseBienes
    porcentaje: Decimal
    anos_mantenimiento: int | None = None


class ReglaTarifa(_Regla):
    escala: Escala


class TramoCoeficiente(_Model):
    patrimonio_hasta: Decimal | None  # pre-existing wealth up to, inclusive; None = "más de"
    coeficientes: dict[Grupo, Decimal]

    @model_validator(mode="after")
    def _completo(self) -> TramoCoeficiente:
        if set(self.coeficientes) != set(get_args(Grupo)):
            raise ValueError("coeficientes debe tener los cuatro grupos (I, II, III, IV)")
        return self


class ReglaCoeficientes(_Regla):
    tramos: list[TramoCoeficiente] = Field(min_length=1)
    # The "error de salto" clause of art. 22.2: the increase in tax caused by a higher
    # coefficient cannot exceed the wealth above the previous bracket. Only when the text has it.
    regla_exceso: bool


class TramoBonificacion(_Model):
    hasta: Decimal | None  # None = "en adelante"
    porcentaje: Decimal


class ReglaBonificacion(_Regla):
    """An allowance on the tax due ("bonificación en cuota"): a percentage, flat or by brackets."""

    titulo: str
    porcentaje: Decimal | None = None
    tramos: list[TramoBonificacion] = Field(default_factory=list)
    tramos_sobre: Literal["base_imponible", "base_liquidable", "cuota_tributaria"] | None = None
    # "medio_ponderado": each slice of the magnitude earns its bracket's percentage and the allowance is
    # the weighted average; "por_tramo": the whole tax earns the percentage of the bracket it falls in.
    tramos_metodo: Literal["medio_ponderado", "por_tramo"] | None = None
    # How the weighted average is rounded, when the law says: to two decimals, the thousandth being
    # dropped when it is 5 or less and rounded up when it is more.
    redondeo_porcentaje: Literal["dos_decimales_milesima_hasta_5_abajo"] | None = None
    importe_maximo: Decimal | None = None  # ceiling of the allowance in euros
    # Declared reductions whose use forfeits the allowance.
    pierde_con: list[Literal["empresa", "patrimonio_historico", "otras_reducciones"]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherente(self) -> ReglaBonificacion:
        if (self.porcentaje is None) == (not self.tramos):
            raise ValueError("una bonificación lleva porcentaje o tramos, uno de los dos")
        if self.tramos and (self.tramos_sobre is None or self.tramos_metodo is None):
            raise ValueError("los tramos exigen tramos_sobre y tramos_metodo")
        porcentajes = [t.porcentaje for t in self.tramos] + ([self.porcentaje] if self.porcentaje is not None else [])
        if any(not 0 <= p <= 1 for p in porcentajes):
            raise ValueError("porcentaje fuera de rango (0-1)")
        if self.tramos and (self.tramos[-1].hasta is not None or any(t.hasta is None for t in self.tramos[:-1])):
            raise ValueError("solo el último tramo queda abierto (hasta: null)")
        return self


class Equiparacion(_Regla):
    """The community treats one relationship as another for the whole tax (registered partners as spouses…)."""

    parentesco: Parentesco
    equivale_a: Parentesco


class OtraReduccion(_Model):
    """A reduction that exists but is not computed: named so the user can claim it as a declared amount."""

    articulo: str
    titulo: str
    modo: Modo


class Ajuar(_Regla):
    porcentaje: Decimal


class NormativaISD(_Model):
    """The rules of one legislator. Every list is ordered: the first rule that applies wins."""

    fuente_principal: FuentePrincipal
    reducciones: list[Reduccion] = Field(default_factory=list)
    vivienda_habitual: list[ReduccionVivienda] = Field(default_factory=list)
    seguro_vida: list[ReduccionSeguro] = Field(default_factory=list)
    reducciones_bienes: list[ReduccionBienes] = Field(default_factory=list)
    tarifas: list[ReglaTarifa] = Field(default_factory=list)
    coeficientes: list[ReglaCoeficientes] = Field(default_factory=list)
    bonificaciones: list[ReglaBonificacion] = Field(default_factory=list)
    otras_reducciones: list[OtraReduccion] = Field(default_factory=list)
    # Caveats the calculator repeats whenever these rules are used.
    notas: list[str] = Field(default_factory=list)
    # What the law has and the data does not; also repeated.
    no_modelado: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _claves(self) -> NormativaISD:
        claves = [r.clave for r in self.reducciones]
        if len(claves) != len(set(claves)):
            raise ValueError("claves de reducción repetidas")
        return self

    def reglas(self) -> list[tuple[str, _Regla]]:
        """Every rule with a path that names it, for messages and the self-check."""
        out: list[tuple[str, _Regla]] = []
        for nombre in type(self).model_fields:
            valor = getattr(self, nombre)
            if isinstance(valor, _Regla):
                out.append((nombre, valor))
            elif isinstance(valor, list):
                out.extend((f"{nombre}.{i}", item) for i, item in enumerate(valor) if isinstance(item, _Regla))
        return out


class NormativaAutonomicaISD(NormativaISD):
    """What an autonomous community legislates on the ISD, for inheritances and gifts."""

    nombre: str
    alias: list[str] = Field(default_factory=list)
    equiparaciones: list[Equiparacion] = Field(default_factory=list)

    @model_validator(mode="after")
    def _caracter(self) -> NormativaAutonomicaISD:
        if any(r.caracter == "estatal" for r in self.reducciones):
            raise ValueError("una reducción autonómica es «mejora» o «propia», nunca «estatal»")
        return self


class EstatalISD(NormativaISD):
    """Ley 29/1987: what applies when the community has not legislated (or its rules do not apply)."""

    ajuar: Ajuar
    # Which community's rules apply (art. 32 Ley 22/2009): explained to the user, never decided.
    puntos_conexion: _Regla
    acumulacion: _Regla  # art. 30: earlier gifts; explained, not computed


class ISDParams(_Model):
    ejercicio: int
    fuentes_principales: dict[str, FuentePrincipal]
    no_modelado: list[str] = Field(default_factory=list)
    estatal: EstatalISD
    comunidades: dict[str, NormativaAutonomicaISD] = Field(default_factory=dict)

    def comunidad(self, nombre: str | None) -> tuple[str, NormativaAutonomicaISD] | None:
        """(key, rules) of a community by key or alias."""
        if not nombre:
            return None
        wanted = normalizar_comunidad(nombre)
        for key, comunidad in self.comunidades.items():
            nombres = {key, key.replace("_", " "), normalizar_comunidad(comunidad.nombre)}
            if wanted in nombres | {normalizar_comunidad(alias) for alias in comunidad.alias}:
                return key, comunidad
        return None

    def normativas(self) -> dict[str, NormativaISD]:
        return {"estatal": self.estatal, **self.comunidades}


def ejercicios_isd_disponibles() -> list[int]:
    return sorted(int(path.stem.rsplit("_", 1)[1]) for path in DATA_DIR.glob("isd_*.json"))


@lru_cache(maxsize=8)
def load_isd_params(ejercicio: int) -> ISDParams:
    path = DATA_DIR / f"isd_{ejercicio}.json"
    if not path.exists():
        disponibles = ", ".join(str(year) for year in ejercicios_isd_disponibles())
        raise EjercicioNoDisponibleError(
            f"No hay parámetros del Impuesto sobre Sucesiones y Donaciones para {ejercicio} "
            f"(disponibles: {disponibles})."
        )
    return ISDParams.model_validate(json.loads(path.read_text(encoding="utf-8")))


# --- Self-check of the data against the law ---------------------------------


_ETIQUETA_DE_BLOQUE = re.compile(
    r"(?i)</?(?:p|div|br|hr|li|ul|ol|dl|dt|dd|h\d|table|thead|tbody|tfoot|tr|td|th|caption|blockquote|pre|"
    r"section|article|header|footer|nav|form|select|option)\b[^>]*>"
)


def texto_plano(raw: str) -> str:
    """A boe.es page (or a text extract) as one line of text: tags out, entities decoded, single spaces.

    Table cells end up separated by one space, so a quote may run across the
    cells and rows of a printed scale.
    """
    raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
    raw = re.sub(_ETIQUETA_DE_BLOQUE, " ", raw)
    raw = re.sub(r"<[^>]+>", "", raw)  # inline markup (<em>a</em>) must not split a word
    return " ".join(html.unescape(raw).replace("\xa0", " ").split())


_UNIDADES = (
    "cero uno dos tres cuatro cinco seis siete ocho nueve diez once doce trece catorce quince dieciséis "
    "diecisiete dieciocho diecinueve veinte veintiuno veintidós veintitrés veinticuatro veinticinco veintiséis "
    "veintisiete veintiocho veintinueve"
).split()
_DECENAS = {30: "treinta", 40: "cuarenta", 50: "cincuenta", 60: "sesenta", 70: "setenta", 80: "ochenta", 90: "noventa"}


def _en_palabras(n: int) -> list[str]:
    if 0 <= n < 30:
        formas = [_UNIDADES[n]]
        if n == 1:
            formas += ["un", "una"]
        if n == 21:
            formas.append("veintiún")
        return formas
    if n < 100:
        decena, unidad = n - n % 10, n % 10
        return [_DECENAS[decena] if not unidad else f"{_DECENAS[decena]} y {_UNIDADES[unidad]}"]
    return []


def _miles(entero: int) -> str:
    return f"{entero:,}".replace(",", ".")


def escrituras(valor: Decimal | int, clase: Literal["importe", "porcentaje", "coeficiente", "entero"]) -> list[str]:
    """The ways the law may print a stored figure ("16.000", "99 por 100" -> "99", "1,0500", "veintiún")."""
    if clase == "entero":
        return [str(valor), *_en_palabras(int(valor))]
    numero = Decimal(valor) * 100 if clase == "porcentaje" else Decimal(valor)
    numero = numero.normalize()
    signo, digitos, exponente = numero.as_tuple()
    assert isinstance(exponente, int) and not signo
    decimales = max(0, -exponente)
    entero = int(numero)
    fraccion = "".join(str(d) for d in digitos)[-decimales:].rjust(decimales, "0") if decimales else ""
    formas: list[str] = []
    for cabeza in dict.fromkeys([_miles(entero), str(entero), _miles(entero).replace(".", " ")]):
        if not decimales:
            # Coefficient tables print whole values bare too ("1", "2").
            formas.append(cabeza)
        for ancho in range(max(decimales, 1), 5):
            formas.append(f"{cabeza},{fraccion.ljust(ancho, '0')}")
    if clase == "porcentaje" and not decimales:
        formas += _en_palabras(entero)
    if clase == "importe" and not decimales and entero and entero % 1_000_000 == 0:
        formas.append(f"{entero // 1_000_000} millón" if entero == 1_000_000 else f"{entero // 1_000_000} millones")
    return formas


def _aparece(forma: str, cita: str) -> bool:
    """``forma`` as a whole token of ``cita``: "100" must not match inside "10.000" or "100,5"."""
    if forma[0].isdigit():
        return re.search(rf"(?<![\d.,]){re.escape(forma)}(?!\d|[.,]\d)", cita) is not None
    return re.search(rf"(?<!\w){re.escape(forma)}(?!\w)", cita, flags=re.IGNORECASE) is not None


def _clase_de(campo: str) -> Literal["importe", "porcentaje", "coeficiente"]:
    if campo in {"porcentaje", "tipo"}:
        return "porcentaje"
    return "coeficiente" if campo in get_args(Grupo) else "importe"


def _cifras(value: object, ruta: str = "", campo: str = "") -> list[tuple[str, Decimal | int, str]]:
    """Every figure inside a rule: (path, value, how to read it)."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return []
    if isinstance(value, Decimal):
        return [(ruta, value, _clase_de(campo))]
    if isinstance(value, int):
        return [(ruta, value, "entero")]
    items: list[tuple[str, object]]
    if isinstance(value, BaseModel):
        items = [(n, getattr(value, n)) for n in type(value).model_fields if n not in {"evidencia", "fuente"}]
    elif isinstance(value, dict):
        items = [(str(k), v) for k, v in value.items()]
    elif isinstance(value, list):
        # A list keeps the name of its field: tramos.3.hasta is still an "hasta".
        return [c for i, item in enumerate(value) for c in _cifras(item, f"{ruta}.{i}", campo)]
    else:
        return []
    return [c for nombre, item in items for c in _cifras(item, f"{ruta}.{nombre}" if ruta else nombre, nombre)]


def comprobar_normativa(
    nombre: str, normativa: NormativaISD, textos: dict[str, str] | None, fuentes: set[str] | None = None
) -> list[str]:
    """Problems of one legislator's rules (see :func:`comprobar_evidencias`)."""
    problemas: list[str] = []
    planos = {boe_id: texto_plano(texto) for boe_id, texto in (textos or {}).items()}
    reglas = normativa.reglas()
    if isinstance(normativa, NormativaAutonomicaISD):
        reglas += [(f"equiparaciones.{i}", e) for i, e in enumerate(normativa.equiparaciones)]
    for ruta_regla, regla in reglas:
        donde = f"{nombre}.{ruta_regla} ({regla.articulo})"
        citas = [texto_plano(c.cita) for c in regla.evidencia]
        for cita, origen in zip(citas, regla.evidencia, strict=True):
            if fuentes is not None and origen.boe_id not in fuentes:
                problemas.append(f"{donde}: {origen.boe_id} no figura en fuentes_principales")
            if len(cita) < 20:
                problemas.append(f"{donde}: cita demasiado corta para ser evidencia")
            if textos is not None:
                if origen.boe_id not in planos:
                    problemas.append(f"{donde}: no se ha aportado el texto de {origen.boe_id}")
                elif cita not in planos[origen.boe_id]:
                    problemas.append(f"{donde}: la cita «{cita[:60]}…» no aparece literal en {origen.boe_id}")
        for ruta, valor, clase in _cifras(regla):
            formas = escrituras(valor, clase)  # type: ignore[arg-type]
            if not any(_aparece(forma, cita) for forma in formas for cita in citas):
                problemas.append(f"{donde}: la cifra {ruta} = {valor} no aparece en ninguna cita ({formas[0]})")
        if isinstance(regla, ReglaTarifa):
            problemas.extend(f"{donde}: {p}" for p in _problemas_escala(regla.escala))
    return problemas


def _problemas_escala(escala: Escala) -> list[str]:
    """The printed cumulative column must agree with the brackets and rates (to within two cents)."""
    problemas: list[str] = []
    acumulado, previo = Decimal(0), Decimal(0)
    for i, tramo in enumerate(escala.tramos):
        if tramo.cuota_hasta_inicio is not None and abs(tramo.cuota_hasta_inicio - acumulado) > Decimal("0.02"):
            problemas.append(
                f"tramo {i}: la cuota impresa {tramo.cuota_hasta_inicio} no cuadra con la recalculada "
                f"{acumulado:.2f} (¿la tercera columna es un ancho o un límite?)"
            )
        if tramo.cuota_hasta_inicio is not None:
            acumulado = tramo.cuota_hasta_inicio
        if tramo.hasta is None:
            if i != len(escala.tramos) - 1:
                problemas.append(f"tramo {i}: solo el último tramo queda abierto")
            break
        if tramo.hasta <= previo:
            problemas.append(f"tramo {i}: «hasta» no crece")
        acumulado += (tramo.hasta - previo) * tramo.tipo
        previo = tramo.hasta
    else:
        problemas.append("el último tramo debe quedar abierto (hasta: null)")
    return problemas


def comprobar_evidencias(params: ISDParams, textos: dict[str, str] | None = None) -> list[str]:
    """Problems found when re-reading the parameters against their quotes.

    Always: every rule has a quote, every figure of a rule (amounts, percentages,
    coefficients, ages, years) is spelled as a whole token inside one of that
    rule's quotes, and each printed scale adds up. With ``textos`` (BOE id ->
    page HTML or text extract) every quote must also be in its law verbatim,
    whitespace and markup aside. An empty list means the data says what the
    text says.
    """
    fuentes = set(params.fuentes_principales)
    problemas: list[str] = []
    for nombre, normativa in params.normativas().items():
        if normativa.fuente_principal.boe_id not in fuentes:
            problemas.append(f"{nombre}: su fuente_principal no figura en fuentes_principales")
        problemas.extend(comprobar_normativa(nombre, normativa, textos, fuentes))
    return problemas


def textos_de_directorio(directorio: Path) -> dict[str, str]:
    """BOE id -> text, from ``<ID>.html`` (downloaded pages) or ``<ID>.txt`` (extracts) files."""
    textos: dict[str, str] = {}
    for path in sorted(directorio.glob("*.txt")) + sorted(directorio.glob("*.html")):
        textos[path.stem] = path.read_text(encoding="utf-8", errors="replace")
    return textos


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 2 and args[0] == "texto":
        print(texto_plano(Path(args[1]).read_text(encoding="utf-8", errors="replace")))
        return 0
    if len(args) == 3 and args[0] == "comprobar":
        problemas = comprobar_evidencias(load_isd_params(int(args[1])), textos_de_directorio(Path(args[2])))
    elif len(args) == 3 and args[0] == "comprobar-ccaa":
        datos = json.loads(Path(args[1]).read_text(encoding="utf-8"))
        clave = datos.pop("key", Path(args[1]).stem)
        datos.pop("dudas", None)
        try:
            normativa = NormativaAutonomicaISD.model_validate(datos)
        except ValidationError as exc:
            print(exc)
            return 1
        problemas = comprobar_normativa(clave, normativa, textos_de_directorio(Path(args[2])))
    else:
        print(__doc__)
        return 2
    for problema in problemas:
        print(problema)
    print("Datos coherentes con el texto." if not problemas else f"{len(problemas)} problema(s).")
    return 1 if problemas else 0


if __name__ == "__main__":
    raise SystemExit(main())
