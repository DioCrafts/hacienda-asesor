"""Every legal figure of the IRPF file behind the loop, and the loop behind an autopilot.

Covers what ``test_recodificacion`` does not: the groups of figures (one adapter class driven by the models), long
articles cut down to one apartado, articles split into the deductions they hold, amending norms read before the BOE
consolidates them, the autopilot that chains detection, verification and re-encoding, and the harness that measures
the loop against shipped encodings.

No network and no real model: the laws are small synthetic BOE pages and the providers replay a script.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any

from click.testing import CliRunner
import pytest

from hacienda_gpt.cli import proactive as cli_module
from hacienda_gpt.tax.params import DATA_DIR, Comunidad, IRPFParams
from hacienda_gpt.tax.recodificacion import (
    AlmacenPropuestas,
    Objetivo,
    Propuesta,
    aplicar_propuesta,
    confirmar,
    control,
    control_de_lo_publicado,
    evaluacion,
    nucleo,
    objetivo_de_ruta,
    objetivos_de_desglose,
    objetivos_de_verificacion,
    pendientes_de_confirmar,
    piloto,
    recodificar,
    todos_los_objetivos,
)
from hacienda_gpt.tax.recodificacion.adaptadores import (
    CIFRAS,
    DEDUCCIONES,
    NO_SON_GRUPOS,
    NO_SON_GRUPOS_DE_COMUNIDAD,
    REGLAS_CIFRAS,
    _reconocer,
    _subgrupos_que_fallan,
)
from hacienda_gpt.tax.verificacion import (
    Comprobacion,
    Resultado,
    _formas,
    comprobaciones,
    huella_de_grupo,
    leer_snapshot,
    url_de,
    verificar,
)

D = Decimal
HOY = date(2026, 11, 12)
LEY = "BOE-T-2020-1"  # the community's law
ESTATAL = "BOE-T-2006-1"  # stands for the IRPF Act
NORMA = "BOE-A-2026-9999"  # an amending norm
NOMBRE_LEY = "Ley 1/2020, de 1 de enero, de tributos cedidos (sintética)"

# --------------------------------------------------------------------------- #
# Synthetic laws
# --------------------------------------------------------------------------- #

P1 = (
    "1. Los contribuyentes menores de 36 años podrán deducir el 10 por ciento de las cantidades satisfechas por el "
    "alquiler de su vivienda habitual, con un máximo de 500 euros."
)
P2 = "2. Será requisito que la base imponible total no sea superior a 22.000 euros en tributación individual."
P1N = P1.replace("10 por ciento", "15 por ciento").replace("500 euros", "600 euros")
P2N = P2.replace("22.000", "25.000")
P3 = "3. El contrato de arrendamiento deberá estar depositado en el registro autonómico."
COMUNES = "Las deducciones de este artículo exigen que la base del ahorro no supere 1.800 euros."
EFECTOS = "Se modifica, con efectos desde el 1 de enero de 2026, por el art. 1 de la Ley 3/2026, de 20 de junio."
VIGOR = (
    "Esta ley entrará en vigor el día siguiente al de su publicación y sus disposiciones tributarias se aplicarán "
    "con efectos desde el 1 de enero de 2025."
)
NACIMIENTO = "Los contribuyentes podrán deducir 300 euros por cada hijo nacido en el período impositivo."
ADOPCION = "Los contribuyentes podrán deducir 400 euros por cada hijo adoptado en el período impositivo."
C3 = (
    "3.º En la primera de las modalidades de unidad familiar, la base imponible se reducirá en 3.600 euros anuales "
    "por declaración conjunta."
)
C4 = "4.º En la segunda de las modalidades de unidad familiar, dicha reducción será de 2.300 euros anuales."
EFECTOS_ESTATAL = (
    "Se modifica, con efectos desde el 1 de enero de 2026, por el art. 2 de la Ley 2/2026, de 1 de julio. "
    "Ref. BOE-A-2026-7777"
)
SELECTOR = (
    '<form class="lista formBOE" method="GET"><fieldset><legend class="fuera">Seleccionar redacción:</legend>'
    '<p><input type="radio" name="p" value="20260630" checked="checked"/>'
    "<label>Última actualización, publicada el 30/06/2026, en vigor a partir del 01/07/2026.</label></p>"
    "</fieldset></form>"
)
ESCALA = (
    "<table><tr><th>Base liquidable</th><th>Cuota íntegra</th><th>Resto</th><th>Tipo</th></tr>"
    "<tr><td>0,00</td><td>0,00</td><td>12.000,00</td><td>9,00 %</td></tr>"
    "<tr><td>12.000,00</td><td>1.080,00</td><td>En adelante</td><td>19,00 %</td></tr></table>"
)


def _articulo(numero: str, titulo: str, parrafos: list[str], notas: list[str]) -> str:
    cuerpo = "".join(f'<p class="parrafo">{p}</p>' for p in parrafos)
    pie = "".join(f'<p class="nota_pie">{n}</p>' for n in notas)
    return (
        f'<div class="bloque"><p class="bloque">[Bloque {numero}]</p>'
        f'<h5 class="articulo">Artículo {numero}. {titulo}</h5>{cuerpo}{pie}{SELECTOR}</div>'
    )


def _relleno(tema: str, n: int = 130) -> list[str]:
    return [
        f"El órgano competente regulará {tema} en los términos del expediente número {i} de este texto."
        for i in range(n)
    ]


def ley(
    *,
    alquiler: tuple[str, str] = (P1, P2),
    notas: tuple[str, ...] = (EFECTOS,),
    apartados: bool = False,
    extra: str = "",
) -> str:
    """The community's law: its scale (art. 4), its rent deduction (art. 5) and whatever ``extra`` adds.

    With ``apartados`` article 5 is very long and holds the deduction in its apartado Dos, as the laws of Galicia or
    Murcia do with theirs.
    """
    if apartados:
        parrafos = [
            "Uno. Normas comunes de las deducciones.",
            COMUNES,
            *_relleno("las normas comunes"),
            "Dos. Deducción por alquiler de vivienda habitual.",
            *alquiler,
            P3,
            "Tres. Deducción por nacimiento de hijos.",
            f"1. {NACIMIENTO}",
            "2. La deducción se prorrateará entre los progenitores.",
            *_relleno("el nacimiento"),
        ]
    else:
        parrafos = [*alquiler, P3]
    return (
        "<html><body>"
        + _articulo("4", "Escala autonómica.", ["La escala autonómica será la siguiente:" + ESCALA], [])
        + _articulo("5", "Deducciones autonómicas.", parrafos, list(notas))
        + extra
        + "</body></html>"
    )


def norma_modificadora(vigor: str = VIGOR) -> str:
    """The amending norm as the daily BOE prints it: its own articles, the new wording between quotation marks."""
    return (
        "<html><body>"
        '<p class="articulo">Artículo 1. Objeto.</p><p>Esta ley adopta medidas fiscales.</p>'
        f'<p class="articulo">Artículo 2. Modificación de la {NOMBRE_LEY.split(",")[0]}, de 1 de enero.</p>'
        "<p>Uno. Se modifica el artículo 5, que queda redactado como sigue:</p>"
        f"<p>«Artículo 5. Deducciones autonómicas.</p><p>{P1N}</p><p>{P2N}</p><p>{P3}»</p>"
        '<p class="articulo">Artículo 3. Modificación de la Ley 9/1999, de tasas.</p>'
        "<p>Se modifica el artículo 5, que fija una tasa de 30 euros.</p>"
        f'<p class="articulo">Disposición final segunda. Entrada en vigor.</p><p>{vigor}</p>'
        "</body></html>"
    )


def ley_estatal() -> str:
    return (
        "<html><body>"
        + _articulo("84", "Normas aplicables en la tributación conjunta.", ["2. Importes:", C3, C4], [EFECTOS_ESTATAL])
        + '<div class="bloque"><h5 class="articulo">Disposición adicional sexagésima primera. Deducción por '
        "obtención de rendimientos del trabajo.</h5><p>La deducción será de 340 euros.</p></div>"
        "</body></html>"
    )


ANTIGUA = {
    "articulo": "art. 5",
    "titulo": "Deducción por alquiler de vivienda habitual",
    "resumen": "El 10 por ciento del alquiler, con un máximo de 500 euros, para menores de 36 años.",
    "tipo": "porcentaje",
    "porcentaje": "0.10",
    "base": "alquiler_vivienda_habitual",
    "maximo": "500",
    "edad_max": 35,
    "limite_individual": "22000",
    "en_vigor_desde": "2020-01-02",
}
NUEVA = {
    **ANTIGUA,
    "resumen": "El 15 por ciento del alquiler de la vivienda habitual, con un máximo de 600 euros, para menores de 36 "
    "años con base imponible total no superior a 25.000 euros.",
    "porcentaje": "0.15",
    "maximo": "600",
    "limite_individual": "25000",
    "requisitos_no_comprobados": ["El contrato de arrendamiento debe estar depositado en el registro autonómico."],
}


def _numero(x: Decimal) -> str:
    return max(_formas(x * 100 if 0 < x < 1 else x), key=len)


def crear_datos(tmp_path: Path, *, etiqueta: str = "art. 5", estatal: bool = False) -> Path:
    """Temporary parameter files: the shipped ones with Galicia hanging from the synthetic law."""
    carpeta = tmp_path / "data"
    carpeta.mkdir()
    for ejercicio in (2025, 2026):
        raw = json.loads((DATA_DIR / f"irpf_{ejercicio}.json").read_text(encoding="utf-8"))
        if estatal:
            raw["fuente_principal"]["boe_id"] = ESTATAL
        galicia = raw["comunidades"]["galicia"]
        raw["comunidades"] = {"galicia": galicia}  # one community is enough here, and keeps the runs short
        galicia["fuente_principal"].update({"boe_id": LEY, "norma": NOMBRE_LEY})
        galicia["deducciones_calculables"] = [{**ANTIGUA, "articulo": etiqueta}]
        galicia["minimos"] = None
        galicia["escala_general"] = {
            "tramos": [
                {"hasta": "12000", "tipo": "0.09", "cuota_hasta_inicio": "0"},
                {"hasta": None, "tipo": "0.19", "cuota_hasta_inicio": "1080"},
            ],
            "fuente": {"referencia": "art. 4 de la ley sintética", "en_vigor_desde": "2020-01-02", "verificado": True},
        }
        IRPFParams.model_validate(raw)
        (carpeta / f"irpf_{ejercicio}.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", "utf-8")
    evidencia = {"galicia": {etiqueta: [P1, P2]}}
    (carpeta / "evidencia_deducciones.json").write_text(json.dumps(evidencia, ensure_ascii=False, indent=2), "utf-8")
    return carpeta


@pytest.fixture
def datos(tmp_path: Path) -> Path:
    return crear_datos(tmp_path)


def _raw(datos: Path, ejercicio: int) -> dict[str, Any]:
    return json.loads((datos / f"irpf_{ejercicio}.json").read_text(encoding="utf-8"))


def _deduccion(datos: Path, ejercicio: int) -> dict[str, Any]:
    return _raw(datos, ejercicio)["comunidades"]["galicia"]["deducciones_calculables"][0]


def boe(datos: Path, **propias: str | None) -> Callable[..., str | None]:
    """A fake BOE: the synthetic pages given by name, and for every other law a page printing what its checks seek."""
    paginas: dict[tuple[str, str | None], str | None] = {}
    evidencia = json.loads((datos / "evidencia_deducciones.json").read_text(encoding="utf-8"))
    for ejercicio in (2025, 2026):
        checks = comprobaciones(
            IRPFParams.model_validate(_raw(datos, ejercicio)), evidencia=evidencia, evidencia_parametros={}
        )
        for check in checks:
            bloque = " ".join(check.citas) + " " + " | ".join(_numero(c) for c in check.cifras)
            previa = paginas.get((check.boe_id, check.version)) or ""
            paginas[(check.boe_id, check.version)] = previa + f"<p>{bloque}</p>"
    paginas[(LEY, None)] = propias.get("consolidada", ley())
    paginas[(NORMA, "diario")] = propias.get("diario")
    if "estatal" in propias:
        paginas[(ESTATAL, None)] = propias["estatal"]
    return lambda boe_id, version=None: paginas.get((boe_id, version))


class Guion:
    """Replays answers in order; a call the script did not foresee is a test failure."""

    name = "guion"

    def __init__(self, *respuestas: Any) -> None:
        self.model = "modelo-de-prueba"
        self.respuestas = list(respuestas)
        self.llamadas: list[dict[str, Any]] = []

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str, attachments: Any = None
    ) -> dict[str, Any]:
        self.llamadas.append({"system": system, "user": user, "schema_name": schema_name})
        if not self.respuestas:
            raise AssertionError(f"llamada no prevista en el guion ({schema_name})")
        respuesta = self.respuestas.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return dict(respuesta)


def lectura(
    valor: Any,
    evidencia: list[str],
    *,
    desde: str = "2026-01-01",
    cita: str = EFECTOS,
    **cambios: Any,
) -> dict[str, Any]:
    base = {
        "resultado": "codificada",
        "valor_json": json.dumps(valor, ensure_ascii=False),
        "evidencia": evidencia,
        "efectos_desde": desde,
        "efectos_hasta": "",
        "cita_efectos": cita,
        "articulos_necesarios": [],
        "motivo": "",
        "cambios": "Cambian las cifras.",
    }
    return {**base, **cambios}


def caso(descripcion: str, datos: dict[str, Any], esperado: str, cita: str) -> dict[str, str]:
    return {
        "descripcion": descripcion,
        "datos_json": json.dumps(datos),
        "importe_esperado": esperado,
        "fundamento": cita,
    }


def revision(
    casos: list[dict[str, str]], *, desde: str = "2026-01-01", cita: str = EFECTOS, **cambios: Any
) -> dict[str, Any]:
    base = {"conforme": True, "discrepancias": [], "efectos_desde": desde, "cita_efectos": cita, "casos": casos}
    return {**base, **cambios}


def casos_alquiler(p1: str = P1N, p2: str = P2N, *, nueva: bool = True) -> list[dict[str, str]]:
    tipo, tope, limite = (D("0.15"), "600", "25000") if nueva else (D("0.10"), "500", "22000")

    def inquilino(edad: int, alquiler: str, ingreso: str = "20000") -> dict[str, Any]:
        return {"contribuyente": {"edad": edad, "alquiler_vivienda_habitual": alquiler}, "ingreso": ingreso}

    return [
        caso("35 años, 2.000 € de alquiler", inquilino(35, "2000"), str(2000 * tipo), p1),
        caso("36 años cumplidos", inquilino(36, "2000"), "0", p1),
        caso("alquiler alto: tope", inquilino(30, "9000"), tope, p1),
        caso("justo en el límite", inquilino(30, "1000", limite), str(1000 * tipo), p2),
        caso("un céntimo por encima", inquilino(30, "1000", limite + ".01"), "0", p2),
    ]


def revision_alquiler(**cambios: Any) -> dict[str, Any]:
    return revision(casos_alquiler(), base_limite_segun_ley="base_imponible_total", **cambios)


# --------------------------------------------------------------------------- #
# Nothing in the IRPF file is left without an adapter
# --------------------------------------------------------------------------- #


def test_every_field_of_the_irpf_file_is_a_group_with_an_adapter_or_is_listed_as_bookkeeping() -> None:
    """A field added to the models tomorrow must be given an adapter or be declared not to hold legal figures."""
    raw = json.loads((DATA_DIR / "irpf_2026.json").read_text(encoding="utf-8"))
    for campo in IRPFParams.model_fields:
        assert campo in NO_SON_GRUPOS or _reconocer([campo], raw) is not None, campo
    for regimen in raw["reduccion_arrendamiento"]:
        if regimen.startswith("regimen_"):
            assert _reconocer(["reduccion_arrendamiento", regimen], raw) is not None, regimen
    for campo in Comunidad.model_fields:
        if campo in NO_SON_GRUPOS_DE_COMUNIDAD:
            continue
        ruta = ["comunidades", "madrid", campo]
        if campo == "deducciones_madrid":
            assert all(_reconocer([*ruta, sub], raw) is not None for sub in raw["comunidades"]["madrid"][campo])
        else:
            assert _reconocer(ruta, raw) is not None, campo
    assert _reconocer(["comunidades", "madrid", "deducciones_calculables", "0"], raw)[0] is DEDUCCIONES  # type: ignore[index]


def test_every_numeric_field_of_every_group_of_figures_has_a_rule() -> None:
    reglas = REGLAS_CIFRAS
    con_regla = (
        reglas.euros | reglas.fracciones | reglas.porcentajes_de_tabla | reglas.literales | reglas.porcentajes_mixtos
    )
    enteros = {"edad_maxima_exclusiva", "minimo_descendientes"}  # checked by type, n or n ± 1 in figures or words
    for nombre, adaptador in CIFRAS.items():
        numericos = control.campos_numericos(adaptador.cifras.modelo)
        assert numericos - con_regla - enteros == set(), nombre
        assert adaptador.tipo == f"cifras:{nombre}" and nucleo.adaptador_de(adaptador.tipo) is adaptador
        # The schema shown to the model is the code of the model itself, nested models included.
        assert f"class {adaptador.cifras.modelo.__name__}" in adaptador.esquema()
        assert adaptador.casos_minimos(Objetivo.model_construct()) == (2 if adaptador.cifras.lineas else 0)


def test_every_shipped_group_is_located_in_its_law_or_said_to_be_out_of_reach() -> None:
    hallazgo = todos_los_objetivos(solo_irpf=True)
    por_tipo: dict[str, list[Objetivo]] = {}
    for objetivo in hallazgo.objetivos:
        por_tipo.setdefault(objetivo.tipo, []).append(objetivo)
    assert len(por_tipo["deduccion_autonomica"]) == 220 and len(por_tipo["escala"]) == 18
    assert por_tipo["cifras:trabajo"][0].articulos_ley == ["19", "20"]
    assert por_tipo["cifras:minimos"][0].articulos_ley == ["57", "58", "59", "60"]
    assert por_tipo["cifras:deduccion_rendimientos_trabajo"][0].articulos_ley == ["da 61"]
    assert por_tipo["cifras:arrendamiento_2026"][0].articulos_ley == ["23", "dt 38"]
    # Donations are read from their own law, named in the group's reference.
    assert por_tipo["cifras:donativos"][0].boe_id == "BOE-A-2002-25039"
    madrid = next(o for o in por_tipo["cifras:minimos_autonomicos"] if o.ambito == "madrid")
    assert madrid.articulos_ley == ["2", "2 bis", "2 ter", "2 quater"] and madrid.boe_id == "BOCM-m-2010-90068"
    # In a very long article a deduction is read from its own apartado.
    galicia = next(o for o in por_tipo["deduccion_autonomica"] if o.ruta[-1] == "articulo=art. 5.Dos.1")
    assert galicia.articulos_ley == ["5.dos.1"]
    # What the loop does not take on by itself, and why: superseded wordings and computed amounts.
    fuera = " | ".join(hallazgo.fuera_de_alcance)
    assert len(hallazgo.fuera_de_alcance) == 4 and fuera.count("--version") == 3 and "calculados" in fuera
    with pytest.raises(nucleo.RecodificacionError, match="--version"):
        objetivo_de_ruta(None, ["vivienda_habitual_transitoria"])
    assert objetivo_de_ruta(None, ["vivienda_habitual_transitoria"], version="20121231").articulos_ley == [
        "dt 18",
        "68",
    ]


def test_references_name_the_blocks_of_the_law() -> None:
    f = control.articulos_de_referencia
    assert f("arts. 19.2.f) y 20 LIRPF") == ["19", "20"]
    assert f("art. 84.2.3.º y 4.º LIRPF") == ["84"] and f("art. 96.2 y 3 LIRPF") == ["96"]
    assert f("arts. 2 bis a 2 quinquies DLeg. 2/2014 (añadidos por la Ley 3/2025)") == [
        "2 bis",
        "2 ter",
        "2 quater",
        "2 quinquies",
    ]
    assert f("art. 18 bis DLeg. 1/2009 de Canarias (redacción de la disp. final 11.17 de la Ley 9/2025)") == ["18 bis"]
    assert f("art. 23.2 LIRPF en su redacción a 31-12-2025 (disposición final 2.ª de la Ley 12/2023)") == ["23"]
    assert f("sin artículo alguno") == []
    g = control.preceptos_de_analisis
    assert g(
        ", con los efectos indicados, los arts. 9 a 9 ter, 13 y AÑADE los arts. 9 octies y 9 nonies a la Ley 8/2013"
    ) == [
        "9",
        "9 bis",
        "9 ter",
        "13",
        "9 octies",
        "9 nonies",
    ]
    assert g("determinados preceptos y AÑADE las disposiciones adicionales 65 y 66 a la Ley 35/2006") == [
        "da 65",
        "da 66",
    ]
    assert control.ruta_de_etiqueta("art. 4.Uno.a).1) (categoría especial)") == "4.uno.a.1"
    assert (
        control.ruta_de_etiqueta("art. 9 c), d) y e)") == "9"
        and control.ruta_de_etiqueta("art. 14 quáter") == "14 quater"
    )


def test_provisions_are_blocks_like_articles() -> None:
    texto = control.extraer_articulos(ley_estatal(), ["da 61", "84"])
    assert texto.encontrados == ("da 61", "84") and "340 euros" in texto.texto
    assert control.es_literal(C3, texto.comprimido)  # "3.º" is read as the BOE's text normalises it
    assert control.clave_de_disposicion("Disposición transitoria decimoctava. Deducción por inversión.") == "dt 18"
    assert control.clave_de_disposicion("Disposición adicional vigésimo sexta. Otra.") == "da 26"
    assert control.clave_de_disposicion("Disposición final única. Entrada en vigor.") == "df unica"
    assert (
        control.clave_de_disposicion("Disposición adicional sexagésima primera") is None
    )  # an index entry, no heading


# --------------------------------------------------------------------------- #
# A group of figures goes through the same loop
# --------------------------------------------------------------------------- #

CONJUNTA_NUEVA = {"reduccion_biparental": "3600", "reduccion_monoparental": "2300"}
LINEA_CONJUNTA = "Reducción por tributación conjunta"


def _casos_conjunta(biparental: str = "3600") -> list[dict[str, str]]:
    def familia(tributacion: str) -> dict[str, Any]:
        return {
            "contribuyente": {"tributacion": tributacion, "rendimiento_trabajo_bruto": "40000"},
            "linea": LINEA_CONJUNTA,
        }

    return [
        caso("matrimonio en conjunta", familia("conjunta_biparental"), biparental, C3),
        caso("monoparental en conjunta", familia("conjunta_monoparental"), "2300", C4),
        caso("individual: sin reducción", familia("individual"), "0", C3),
    ]


def _conjunta(datos: Path) -> Objetivo:
    hallazgo = objetivos_de_verificacion(
        [2025, 2026],
        boe(datos, estatal=ley_estatal()),
        data_dir=datos,
        ambito="estado",
        tipos=["cifras:tributacion_conjunta"],
        solo_irpf=True,
    )
    assert len(hallazgo.objetivos) == 1, hallazgo
    return hallazgo.objetivos[0]


def test_a_group_of_figures_is_reread_checked_through_the_calculator_applied_and_confirmed(tmp_path: Path) -> None:
    datos = crear_datos(tmp_path, estatal=True)
    texto_de = boe(datos, estatal=ley_estatal())
    objetivo = _conjunta(datos)
    assert objetivo.articulos_ley == ["84"] and objetivo.boe_id == ESTATAL and objetivo.ruta == ["tributacion_conjunta"]
    assert any("3400" in m for m in objetivo.motivo)
    ficheros = {e: _raw(datos, e) for e in (2025, 2026)}
    lector = Guion(lectura(CONJUNTA_NUEVA, [C3, C4], cita=EFECTOS_ESTATAL))
    revisor = Guion(revision(_casos_conjunta(), cita=EFECTOS_ESTATAL))
    propuesta = recodificar(objetivo, lector=lector, revisor=revisor, texto_de=texto_de, datos=ficheros)
    assert propuesta.estado == "propuesta" and propuesta.ejercicios == [2026], propuesta.objeciones_abiertas
    # The reviewer's cases went through the whole IRPF calculator with the proposed figures.
    assert [str(c.obtenido) for c in propuesta.casos] == ["3600.00", "2300.00", "0.00"]
    assert propuesta.casos[0].detalle == LINEA_CONJUNTA and propuesta.aplicable_sin_persona
    assert (
        "class TributacionConjunta" in lector.llamadas[0]["system"] and LINEA_CONJUNTA in revisor.llamadas[0]["system"]
    )
    assert "fuente" not in propuesta.despues

    almacen = AlmacenPropuestas(tmp_path / "propuestas")
    aplicar_propuesta(propuesta, texto_de, almacen=almacen, data_dir=datos, hoy=HOY)
    grupo = _raw(datos, 2026)["tributacion_conjunta"]
    assert (grupo["reduccion_biparental"], grupo["reduccion_monoparental"]) == ("3600", "2300")
    assert grupo["fuente"] == {
        "referencia": "art. 84.2.3.º y 4.º LIRPF (efectos desde 2026-01-01)",
        "en_vigor_desde": "2026-01-01",
        "verificado": False,
        "nota": "Recodificada automáticamente el 2026-11-12 a partir del BOE; pendiente de revisión humana.",
    }
    assert _raw(datos, 2025)["tributacion_conjunta"]["reduccion_biparental"] == "3400"  # effects do not reach 2025

    # From now on the weekly verification checks the group by the quotes it was read from.
    citas = json.loads((datos / "evidencia_parametros.json").read_text(encoding="utf-8"))
    entrada = citas["irpf_2026.json"]["tributacion_conjunta"]
    assert entrada["citas"] == [C3, C4] and entrada["sha256_valor"] == huella_de_grupo(grupo)
    params = IRPFParams.model_validate(_raw(datos, 2026))
    check = next(
        c
        for c in comprobaciones(params, evidencia={}, evidencia_parametros=citas)
        if c.ruta == ["tributacion_conjunta"]
    )
    assert (check.tipo, check.boe_id, check.citas) == ("cita", ESTATAL, [C3, C4])
    assert verificar([check], texto_de)[0].ok
    assert not verificar([check], lambda b, v=None: ley_estatal().replace("3.600", "3.700"))[0].ok
    # …but only while the group holds what those quotes were read for.
    editado = _raw(datos, 2026)
    editado["tributacion_conjunta"]["reduccion_biparental"] = "3700"
    rutas = {
        tuple(c.ruta)
        for c in comprobaciones(IRPFParams.model_validate(editado), evidencia={}, evidencia_parametros=citas)
    }
    assert ("tributacion_conjunta",) not in rutas

    pendientes = pendientes_de_confirmar(datos)
    assert [(p.tipo, p.ejercicio, p.ruta) for p in pendientes] == [
        ("cifras:tributacion_conjunta", 2026, ["tributacion_conjunta"])
    ]
    assert confirmar(pendientes, data_dir=datos, almacen=almacen) == 1
    fuente = _raw(datos, 2026)["tributacion_conjunta"]["fuente"]
    assert fuente["verificado"] is True and "nota" not in fuente and pendientes_de_confirmar(datos) == []


def test_the_gate_of_a_group_of_figures(tmp_path: Path) -> None:
    datos = crear_datos(tmp_path, estatal=True)
    objetivo = _conjunta(datos)
    ficheros = {e: _raw(datos, e) for e in (2025, 2026)}
    adaptador = CIFRAS["tributacion_conjunta"]

    def fallos(valor: Any, citas: list[str]) -> list[str]:
        return adaptador.controlar(valor, citas, objetivo, efectos_desde=None, datos=ficheros)[1].fallos

    assert fallos(CONJUNTA_NUEVA, [C3, C4]) == []
    assert any("reduccion_monoparental = 2300" in f for f in fallos(CONJUNTA_NUEVA, [C3]))  # figure in no quote
    assert any("fuente" in f for f in fallos({**CONJUNTA_NUEVA, "fuente": {}}, [C3, C4]))
    assert any("modelo" in f for f in fallos({"reduccion_biparental": "3600"}, [C3]))  # a field is missing
    assert fallos(["3600"], [C3]) == ["el valor no es un objeto JSON"]

    # A case the calculator refutes goes back to the reader.
    lector = Guion(lectura({**CONJUNTA_NUEVA, "reduccion_biparental": "2300"}, [C3, C4], cita=EFECTOS_ESTATAL))
    revisor = Guion(revision(_casos_conjunta(), cita=EFECTOS_ESTATAL))
    refutada = recodificar(
        objetivo, lector=lector, revisor=revisor, texto_de=boe(datos, estatal=ley_estatal()), datos=ficheros, max_iter=1
    )
    assert refutada.estado == "sin_converger" and "la calculadora da 2300.00" in refutada.objeciones_abiertas[0]
    # A structural change cannot be squeezed into the old fields: the group is left to a person, never withdrawn.
    rendido = Guion(lectura(None, [], resultado="no_calculable", valor_json="", motivo="Ahora hay tres modalidades."))
    atascada = recodificar(objetivo, lector=rendido, revisor=Guion(), texto_de=boe(datos, estatal=ley_estatal()))
    assert atascada.estado == "sin_converger" and "no se puede retirar" in atascada.objeciones_abiertas[0]

    # A case must name a line of the result the group feeds.
    modelo = adaptador.controlar(CONJUNTA_NUEVA, [C3, C4], objetivo, efectos_desde=None, datos=ficheros)[0]
    assert modelo is not None
    with pytest.raises(ValueError, match="debe ser una de"):
        adaptador.evaluar_caso(modelo, {"contribuyente": {}, "linea": "Cuota líquida"}, objetivo)
    with pytest.raises(ValueError, match="no conoce"):
        adaptador.evaluar_caso(modelo, {"contribuyente": {"inventado": 1}}, objetivo)


def _objetivo_de(nombre: str, antes: dict[int, Any], ambito: str = "estado") -> Objetivo:
    adaptador = CIFRAS[nombre]
    return Objetivo(
        tipo=adaptador.tipo,
        fichero=adaptador.fichero,
        ruta=adaptador.ruta_de(ambito),
        etiqueta=nombre,
        ambito=ambito,
        boe_id=ESTATAL,
        articulos_ley=["1"],
        ejercicios=sorted(antes),
        antes=antes,
    )


def test_groups_with_no_executable_case_or_with_figures_of_another_law_always_wait_for_a_person() -> None:
    raw = json.loads((DATA_DIR / "irpf_2026.json").read_text(encoding="utf-8"))
    # No line of the calculator isolates the share borne by the regional tax: gate and reviewer only, and flagged.
    reparto = CIFRAS["reparto_deducciones_estatales"]
    objetivo = _objetivo_de("reparto_deducciones_estatales", {2026: raw["reparto_deducciones_estatales"]})
    cita = ["El 50 por ciento del importe total de las deducciones previstas en esta ley."]
    modelo, visto = reparto.controlar({"parte_autonomica": "0.50"}, cita, objetivo, efectos_desde=None, datos={})
    assert modelo is not None and visto.ok and any("no tiene casos de prueba" in r for r in visto.revisar)
    assert reparto.casos_minimos(objetivo) == 0 and "empty list" in reparto.guia_casos(objetivo)
    with pytest.raises(ValueError):
        reparto.evaluar_caso(modelo, {"contribuyente": {}}, objetivo)

    # The 10 % ceiling of donations is printed by another law: kept as each file has it, never proposed.
    donativos = CIFRAS["donativos"]
    objetivo = _objetivo_de("donativos", {2026: raw["donativos"]})
    tabla = ["Hasta 250 euros 80", "Resto base de deducción 40", "Tipo incrementado por reiteración de donaciones 45"]
    escala = {"tramo_primero": "250", "tipo_primero": "0.80", "tipo_resto": "0.40", "tipo_resto_recurrente": "0.45"}
    modelo, visto = donativos.controlar(escala, tabla, objetivo, efectos_desde=None, datos={})
    assert visto.ok and modelo.limite_base_liquidable == D("0.10")  # type: ignore[union-attr]
    assert any("limite_base_liquidable" in r for r in visto.revisar)
    assert "limite_base_liquidable" not in donativos.normalizar(modelo)  # type: ignore[arg-type]
    cambiado = donativos.controlar(
        {**escala, "limite_base_liquidable": "0.15"}, tabla, objetivo, efectos_desde=None, datos={}
    )
    assert any("se conserva el anterior" in f for f in cambiado[1].fallos)
    assert "limite_base_liquidable" in donativos.guia()


def test_a_regional_minimum_may_leave_amounts_to_the_state_and_a_decree_law_keeps_its_condition() -> None:
    raw = json.loads((DATA_DIR / "irpf_2026.json").read_text(encoding="utf-8"))
    minimos = CIFRAS["minimos_autonomicos"]
    actual = {k: v for k, v in raw["comunidades"]["cataluna"]["minimos"].items() if k != "fuente"}
    objetivo = _objetivo_de("minimos_autonomicos", {2026: raw["comunidades"]["cataluna"]["minimos"]}, "cataluna")
    cita = ["El importe del mínimo personal del contribuyente es, con carácter general, de 5.800 euros anuales."]
    modelo, visto = minimos.controlar(
        {**actual, "contribuyente": "5800"}, cita, objetivo, efectos_desde=None, datos={2026: raw}
    )
    # Only the taxpayer minimum is printed; the rest equal the State's and are said to, not failed.
    assert modelo is not None and visto.ok and any("coincide con el importe estatal" in r for r in visto.revisar)
    distinto = minimos.controlar(
        {**actual, "contribuyente": "5800", "ascendiente": "1300"},
        cita,
        objetivo,
        efectos_desde=None,
        datos={2026: raw},
    )
    assert any("ascendiente = 1300" in f for f in distinto[1].fallos)

    # The State rent deduction exists because of a decree-law awaiting validation: a re-encoding keeps that link.
    alquiler = CIFRAS["alquiler_vivienda_habitual"]
    anterior = raw["alquiler_vivienda_habitual"]
    propuesta = Propuesta.model_construct(
        despues={k: v for k, v in anterior.items() if k != "fuente"},
        efectos_desde="2026-10-08",
        etiqueta="alquiler",
        version=None,
    )
    fuente = alquiler.json_final(propuesta, anterior, hoy=HOY)["fuente"]
    assert fuente["condicionada_a"] == "BOE-A-2026-20823" and fuente["verificado"] is False
    assert (
        fuente["nota"].startswith("Recodificada automáticamente el 2026-11-12")
        and "Nota anterior: Leído en el BOE" in fuente["nota"]
    )
    final = {"alquiler_vivienda_habitual": {"fuente": dict(fuente)}}
    alquiler.confirmar(final, ["alquiler_vivienda_habitual"])
    assert (
        final["alquiler_vivienda_habitual"]["fuente"]["nota"] == anterior["fuente"]["nota"]
    )  # the old note comes back


def test_a_failing_block_of_several_groups_is_taken_on_group_by_group() -> None:
    raw = json.loads((DATA_DIR / "irpf_2026.json").read_text(encoding="utf-8"))
    ruta = ["comunidades", "madrid", "deducciones_madrid"]
    tope = raw["comunidades"]["madrid"]["deducciones_madrid"]["cuidado"]["maximo_familia_numerosa"]
    check = Comprobacion(boe_id="X", ruta=ruta, descripcion="Madrid: deducciones calculadas", tipo="cifras")
    suyos = _subgrupos_que_fallan(raw, Resultado(comprobacion=check, ok=False, faltan=[str(D(tope))]))
    assert [(a.tipo, r) for a, r, _ in suyos] == [("cifras:madrid_cuidado", [*ruta, "cuidado"])]


def test_table_cells_are_not_glued_into_one_figure_and_spaced_thousands_still_are() -> None:
    filas = ["0,00 167.129,45 334.252,88 0,3", "12.450,00 1.182,75 7.750,00 12,00", "1.168,68 808,25 11,65 %"]
    ev = control.normalizar_evidencia(filas)
    assert all(control.hay_numero(ev, D(x)) for x in ("167129.45", "334252.88", "7750", "808.25", "1168.68", "1182.75"))
    ev = control.normalizar_evidencia(["no superior a 35 000 euros ni a 1 200 000,50 euros"])
    assert (
        control.hay_numero(ev, D(35000))
        and control.hay_numero(ev, D("1200000.50"))
        and not control.hay_numero(ev, D(35))
    )


def test_core_services_the_other_adapters_rely_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # A path may end in an index: items of a list with no field that tells them apart.
    raw: dict[str, Any] = {"reglas": [{"a": 1}, {"a": 2}, {"a": 3}], "por_clave": [{"k": "x"}]}
    nucleo.escribir_ruta(raw, ["reglas", "1"], {"a": 20})
    assert raw["reglas"] == [{"a": 1}, {"a": 20}, {"a": 3}] and nucleo.leer_ruta(raw, ["reglas", "1"]) == {"a": 20}
    nucleo.escribir_ruta(raw, ["reglas", "0"], None)
    assert raw["reglas"] == [{"a": 20}, {"a": 3}]
    with pytest.raises(IndexError):
        nucleo.escribir_ruta(raw, ["reglas", "7"], {"a": 9})
    nucleo.escribir_ruta(raw, ["por_clave", "k=y"], {"k": "y"})  # by key, a missing item is still appended
    assert [item["k"] for item in raw["por_clave"]] == ["x", "y"]
    # After writing, every adapter is asked to drop the caches of its own file.
    llamados: list[str] = []
    monkeypatch.setattr(type(DEDUCCIONES), "limpiar_caches", lambda self: llamados.append(self.tipo))
    nucleo._limpiar_caches()
    assert llamados == ["deduccion_autonomica"]


# --------------------------------------------------------------------------- #
# Long articles: only the apartado
# --------------------------------------------------------------------------- #


def test_a_very_long_article_is_cut_down_to_the_apartado_and_falls_back_when_in_doubt() -> None:
    pagina = ley(apartados=True)
    entero = control.extraer_articulos(pagina, ["5"])
    assert len(entero.texto) > control.UMBRAL_ACOTAR
    dos = control.extraer_articulos(pagina, ["5.Dos.1"])
    assert dos.encontrados == ("5.dos",) and not dos.faltan and len(dos.texto) < 3000
    assert P1 in dos.texto and P3 in dos.texto and NACIMIENTO not in dos.texto and COMUNES not in dos.texto
    # What is kept besides the apartado: the heading, the BOE's notes (they date the wording) and an index.
    assert (
        "Artículo 5. Deducciones autonómicas." in dos.texto
        and EFECTOS in dos.texto
        and "Redacción mostrada" in dos.texto
    )
    assert "uno: Uno. Normas comunes" in dos.texto and "tres: Tres. Deducción por nacimiento" in dos.texto
    # Asking again with the name returned gives the same text (what apply relies on).
    assert control.extraer_articulos(pagina, list(dos.encontrados)).sha256 == dos.sha256
    # A nested numbered list does not confuse the word apartados; a whole article makes its apartados redundant.
    tres = control.extraer_articulos(pagina, ["5.tres"])
    assert NACIMIENTO in tres.texto and "2. La deducción se prorrateará" in tres.texto and P1 not in tres.texto
    assert control.extraer_articulos(pagina, ["5.dos", "5"]).encontrados == ("5",)
    # An apartado that is not there, or a short article: the whole article, which is always right.
    assert control.extraer_articulos(pagina, ["5.Nueve"]).encontrados == ("5",)
    assert control.extraer_articulos(ley(), ["5.Dos"]).encontrados == ("5",)
    assert control.extraer_articulos(pagina, ["7.Dos"]).faltan == ("7.dos",)
    # A previous quote found in another apartado adds that apartado, not the whole article.
    assert control.articulos_que_contienen(pagina, [COMUNES, P1], junto_a=["5.dos"]) == ["5.uno", "5.dos"]
    assert control.articulos_que_contienen(pagina, [COMUNES]) == ["5"]


def test_numbered_and_lettered_apartados_are_followed_through_their_nesting() -> None:
    lineas = [
        "Artículo 2. Deducciones.",
        "1. Por alquiler.",
        "a) Requisito uno.",
        "b) Requisito dos.",
        "2. Por cuidado.",
        "3. Por obras.",
        "4. Por inversión.",
        "1. Los contribuyentes podrán deducir el 15 %.",
        "2. El límite será de 1.000 euros.",
        "3. Requisitos:",
        "1. Domicilio.",
        "2. Actividad.",
        "5. Por donativos.",
        "12. Otras.",
    ]
    # "12." does not follow "5.": the numbering cannot be told, so nothing is cut.
    assert control._segmentos(lineas[1:], "numero") is None
    numeros = control._segmentos(lineas[1:-1], "numero")
    assert numeros is not None
    assert [(e, i, f) for e, i, f in numeros] == [("1", 0, 3), ("2", 3, 4), ("3", 4, 5), ("4", 5, 11), ("5", 11, 12)]
    # A label that could continue the top level or a nested list: with the top level in doubt, no cut either.
    ambiguo = ["1. Por alquiler.", "2. Por cuidado.", "1. Uno.", "2. Dos.", "3. ¿Anidado o principal?"]
    assert control._segmentos(ambiguo, "numero") is None
    # A list whose first item is the unnumbered opening sentence ("12. … / 2. Límite conjunto") is still nested.
    implicita = [
        "11. Por residencia.",
        "12. Por inversiones o donaciones.",
        "2. Límite conjunto.",
        "3. Requisitos.",
        "13. Otra.",
    ]
    assert control._segmentos(["1. Una."] + [f"{n}. Relleno." for n in range(2, 11)] + implicita, "numero")[-2:] == [  # type: ignore[index]
        ("12", 11, 14),
        ("13", 14, 15),
    ]
    letras = control._segmentos(
        ["a) Por nacimiento", "b) Por adopción", "c) Por acogimiento:", "a) uno", "b) dos", "d) Otra"], "letra"
    )
    assert letras is not None and [(e, i, f) for e, i, f in letras] == [
        ("a", 0, 1),
        ("b", 1, 2),
        ("c", 2, 5),
        ("d", 5, 6),
    ]
    assert (
        control._segmentos(["a) Por nacimiento:", "a) uno", "b) ¿requisito dos o segunda deducción?"], "letra") is None
    )
    assert (
        control._siguiente_letra("n", "ñ")
        and control._siguiente_letra("n", "o")
        and control._siguiente_letra("z", "aa")
    )


def test_the_loop_reads_one_apartado_and_the_reader_may_ask_for_another(tmp_path: Path) -> None:
    datos = crear_datos(tmp_path, etiqueta="art. 5.Dos")
    pagina = ley(alquiler=(P1N, P2N), apartados=True)
    texto_de = boe(datos, consolidada=pagina)
    hallazgo = objetivos_de_verificacion(
        [2026], texto_de, data_dir=datos, ambito="galicia", tipos=["deduccion_autonomica"]
    )
    objetivo = hallazgo.objetivos[0]
    assert objetivo.articulos_ley == ["5.dos"]
    nueva = {**NUEVA, "articulo": "art. 5.Dos"}
    pide = lectura(
        None, [], resultado="no_calculable", valor_json="", articulos_necesarios=["5.Uno"], motivo="faltan artículos"
    )
    lector = Guion(pide, lectura(nueva, [P1N, P2N]))
    propuesta = recodificar(
        objetivo, lector=lector, revisor=Guion(revision_alquiler()), texto_de=texto_de, datos={2026: _raw(datos, 2026)}
    )
    assert propuesta.estado == "propuesta" and len(propuesta.iteraciones) == 1, propuesta.objeciones_abiertas
    primero, segundo = (llamada["user"] for llamada in lector.llamadas)
    assert P1N in primero and COMUNES not in primero and NACIMIENTO not in primero and len(primero) < 6000
    assert COMUNES in segundo and NACIMIENTO not in segundo  # the common rules came without spending a pass
    assert propuesta.articulos_ley == ["5.dos", "5.uno"]
    aplicar_propuesta(propuesta, texto_de, data_dir=datos, hoy=HOY)
    assert _deduccion(datos, 2026)["porcentaje"] == "0.15"
    # A change in ANOTHER apartado of the article does not look like a change of this group's text.
    otra = pagina.replace(NACIMIENTO, NACIMIENTO.replace("300", "350"))
    assert (
        nucleo.leer_texto(objetivo, boe(datos, consolidada=otra))[1].sha256
        == nucleo.leer_texto(objetivo, texto_de)[1].sha256
    )


# --------------------------------------------------------------------------- #
# Articles that create deductions
# --------------------------------------------------------------------------- #

ART_14 = _articulo(
    "14",
    "Nuevas deducciones.",
    [
        "Uno. Deducción por nacimiento.",
        NACIMIENTO,
        "Dos. Deducción por adopción.",
        ADOPCION,
        "Tres. Normas comunes.",
        COMUNES,
    ],
    [f"Se añade por el art. 3 de la Ley 4/2026, de 1 de octubre. Ref. {NORMA}"],
)
ELEMENTOS = {
    "elementos": [
        {"etiqueta": "art. 14.Uno", "titulo": "Deducción por nacimiento", "cita": NACIMIENTO},
        {"etiqueta": "art. 14.Dos", "titulo": "Deducción por adopción", "cita": ADOPCION},
        {"etiqueta": "art. 15.Uno", "titulo": "De otro artículo", "cita": NACIMIENTO},
        {"etiqueta": "art. 14.Cuatro", "titulo": "Inventada", "cita": "Los contribuyentes podrán deducir 900 euros."},
        {"etiqueta": "art. 14.Uno", "titulo": "Repetida", "cita": NACIMIENTO},
    ]
}


def test_an_article_is_split_into_the_deductions_it_holds_and_only_the_new_ones_are_offered(datos: Path) -> None:
    for ejercicio in (2025, 2026):  # one of them is already encoded
        raw = _raw(datos, ejercicio)
        raw["comunidades"]["galicia"]["deducciones_calculables"].append({**ANTIGUA, "articulo": "art. 14.Dos"})
        (datos / f"irpf_{ejercicio}.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", "utf-8")
    texto_de = boe(datos, consolidada=ley(extra=ART_14))
    lector = Guion(ELEMENTOS)
    hallazgo, desglose = objetivos_de_desglose(
        [2025, 2026], comunidad="galicia", articulo="art. 14", lector=lector, texto_de=texto_de, data_dir=datos
    )
    assert desglose is not None and [e.etiqueta for e in desglose.elementos] == ["art. 14.Uno", "art. 14.Dos"]
    descartes = " | ".join(desglose.descartados)
    assert "no cuelga de «art. 14»" in descartes and "no es literal" in descartes and "repetida" in descartes
    assert [o.ruta[-1] for o in hallazgo.objetivos] == ["articulo=art. 14.Uno"]
    nueva = hallazgo.objetivos[0]
    assert nueva.antes == {2025: None, 2026: None} and nueva.origen == "desglose" and nueva.articulos_ley == ["14.uno"]
    assert nueva.etiqueta.endswith("Deducción por nacimiento") and "deducción nueva" in nueva.motivo[0]
    # The reader was shown the article and asked only to list.
    assert NACIMIENTO in lector.llamadas[0]["user"] and "do not encode" in lector.llamadas[0]["system"]
    assert lector.llamadas[0]["schema_name"] == "recodificacion_desglose"


# --------------------------------------------------------------------------- #
# An amending norm before the BOE consolidates it
# --------------------------------------------------------------------------- #


def test_the_new_wording_is_found_in_the_amending_norm_itself() -> None:
    texto = control.extraer_de_modificadora(norma_modificadora(), ["5"], ley="1/2020")
    assert texto.encontrados == ("5",) and P1N in texto.texto and VIGOR in texto.texto
    assert "tasa de 30 euros" not in texto.texto and "medidas fiscales" not in texto.texto  # another law's article 5
    sin_numero = control.extraer_de_modificadora(norma_modificadora(), ["5"])
    assert P1N in sin_numero.texto and "tasa de 30 euros" in sin_numero.texto  # without the law's number, every mention
    assert control.extraer_de_modificadora(norma_modificadora(), ["8"], ley="1/2020").faltan == ("8",)
    assert url_de(NORMA, "diario") == f"https://www.boe.es/diario_boe/txt.php?id={NORMA}"
    assert url_de(LEY, "20250101").endswith("&tn=1&p=20250101")


REGISTRO = {
    NORMA: {
        "boe_id": NORMA,
        "fecha": "2026-11-10",
        "titulo": "Ley 3/2026, de 5 de noviembre, de medidas fiscales.",
        "modifica": [
            {
                "boe_id": LEY,
                "accion": "MODIFICA",
                "texto": "el art. 5 de la Ley 1/2020, de 1 de enero",
                "parametros": True,
            },
            {
                "boe_id": "BOE-A-1999-1",
                "accion": "MODIFICA",
                "texto": "el art. 5 de la Ley 9/1999",
                "parametros": False,
            },
        ],
    },
    "BOE-A-2026-1": {"boe_id": "BOE-A-2026-1", "fecha": "2026-11-10", "titulo": "Otra norma", "modifica": []},
}
CONSOLIDADA_NUEVA = ley(alquiler=(P1N, P2N), notas=(f"Se modifica por el art. 2 de la Ley 3/2026. Ref. {NORMA}", VIGOR))


def _pilotar(datos: Path, tmp_path: Path, texto_de: Any, lector: Any = None, revisor: Any = None, **kw: Any):
    return piloto.pilotar(
        kw.pop("registro", REGISTRO),
        leyes={LEY: NOMBRE_LEY},
        comunidades={LEY: "galicia"},
        texto_de=texto_de,
        estado_path=tmp_path / "estado" / "_params_autopilot.json",
        almacen=AlmacenPropuestas(tmp_path / "propuestas"),
        data_dir=datos,
        lector=lector,
        revisor=revisor,
        hoy=HOY,
        **kw,
    )


def _lectura_de_la_norma() -> dict[str, Any]:
    return lectura(NUEVA, [P1N, P2N], desde="2025-01-01", cita=VIGOR)


def _revision_de_la_norma() -> dict[str, Any]:
    return revision(casos_alquiler(), desde="2025-01-01", cita=VIGOR, base_limite_segun_ley="base_imponible_total")


def test_autopilot_reads_the_amending_norm_then_the_consolidated_text_bears_it_out(datos: Path, tmp_path: Path) -> None:
    sin_consolidar = boe(datos, consolidada=ley(), diario=norma_modificadora())
    # Without a model the deterministic part still runs and says what is waiting.
    informe, nuevas = _pilotar(datos, tmp_path, sin_consolidar)
    assert nuevas == [] and "No hay credenciales" in (informe.omitido or "") and informe.objetivos == 1
    assert informe.normas_nuevas == [f"{NORMA} modifica {LEY} ({NOMBRE_LEY})"]
    assert informe.normas[0]["etapa"] == "sin_consolidar" and informe.normas[0]["preceptos"] == ["5"]
    assert informe.normas[0]["grupos"] == [
        "irpf_{ejercicio}.json:comunidades/galicia/deducciones_calculables/articulo=art. 5"
    ]
    assert informe.no_coinciden == [] and informe.errores == []  # the consolidated text still says the old thing

    # With a model: the new wording is read from the norm's own text…
    lector, revisor = Guion(_lectura_de_la_norma()), Guion(_revision_de_la_norma())
    informe, nuevas = _pilotar(datos, tmp_path, sin_consolidar, lector, revisor, auto_aplicar=True)
    assert informe.normas_nuevas == [] and len(nuevas) == 1
    propuesta = nuevas[0]
    assert propuesta.estado == "propuesta" and propuesta.norma_texto == NORMA and propuesta.ley_citada == "1/2020"
    assert propuesta.ejercicios == [2025, 2026] and propuesta.origen == "norma"
    assert (
        "TODAVÍA SIN CONSOLIDAR" in lector.llamadas[0]["user"] and "tasa de 30 euros" not in lector.llamadas[0]["user"]
    )
    assert (
        f"{NORMA}: MODIFICA el art. 5 de la Ley 1/2020" in lector.llamadas[0]["user"]
    )  # the BOE's analysis, as annotations
    # …and such a proposal is never applied without a person, auto-application or not.
    assert any("aún no había consolidado" in r for r in propuesta.control.revisar) and not propuesta.aplicable_sin_persona  # type: ignore[union-attr]
    assert _deduccion(datos, 2026) == ANTIGUA

    # Running again costs nothing: same norm, same text, same data.
    informe, nuevas = _pilotar(datos, tmp_path, sin_consolidar, Guion(), Guion(), auto_aplicar=True)
    assert nuevas == [] and informe.recodificacion["ya_tratados"][0]["propuesta"] == propuesta.id  # type: ignore[index]

    # A person applies it. Until the BOE consolidates, the group is neither re-read nor reported as failing-to-fix.
    almacen = AlmacenPropuestas(tmp_path / "propuestas")
    aplicar_propuesta(almacen.leer(propuesta.id), sin_consolidar, almacen=almacen, data_dir=datos, hoy=HOY)
    assert _deduccion(datos, 2026)["porcentaje"] == "0.15" and _deduccion(datos, 2025)["porcentaje"] == "0.15"
    informe, nuevas = _pilotar(datos, tmp_path, sin_consolidar, Guion(), Guion())
    assert nuevas == [] and informe.objetivos == 0 and len(informe.no_coinciden) == 1

    # The consolidated text arrives and prints the quotes the proposal was applied with: borne out, no model needed.
    consolidado = boe(datos, consolidada=CONSOLIDADA_NUEVA, diario=norma_modificadora())
    informe, nuevas = _pilotar(datos, tmp_path, consolidado, Guion(), Guion())
    assert nuevas == [] and informe.no_coinciden == [] and len(informe.confirmadas_por_el_consolidado) == 1
    estado = piloto.leer_estado(tmp_path / "estado" / "_params_autopilot.json")
    assert (
        estado.normas[f"{NORMA}|{LEY}"].etapa == "cerrada"
        and estado.normas[f"{NORMA}|{LEY}"].consolidada_el == "2026-11-12"
    )
    assert "respalda lo que se aplicó" in piloto.texto_del_parte(informe, nuevas)
    # Closed: nothing is looked at again for that norm.
    informe, nuevas = _pilotar(datos, tmp_path, consolidado, Guion(), Guion())
    assert informe.normas == [] and not informe.hay_algo


def test_autopilot_rereads_from_the_consolidated_text_and_applies_what_needs_nobody(
    datos: Path, tmp_path: Path
) -> None:
    # The norm was seen before consolidation and its proposal was left waiting.
    sin_consolidar = boe(datos, consolidada=ley(), diario=norma_modificadora())
    _, primeras = _pilotar(
        datos, tmp_path, sin_consolidar, Guion(_lectura_de_la_norma()), Guion(_revision_de_la_norma())
    )
    assert primeras[0].norma_texto == NORMA
    # Consolidated: the stored quotes are gone from the law AND the norm names the article — one target, both reasons.
    consolidado = boe(datos, consolidada=CONSOLIDADA_NUEVA, diario=norma_modificadora())
    lector, revisor = Guion(_lectura_de_la_norma()), Guion(_revision_de_la_norma())
    informe, nuevas = _pilotar(datos, tmp_path, consolidado, lector, revisor, auto_aplicar=True)
    assert len(nuevas) == 1 and informe.objetivos == 1 and len(informe.no_coinciden) == 1
    aplicada = nuevas[0]
    assert aplicada.estado == "aplicada" and aplicada.aplicada_automaticamente and aplicada.norma_texto is None
    assert any(NORMA in m for m in aplicada.motivo) and any("ya no está en el texto" in m for m in aplicada.motivo)
    assert "texto consolidado vigente" in lector.llamadas[0]["user"]
    assert _deduccion(datos, 2026)["recodificada_el"] == "2026-11-12" and _deduccion(datos, 2025)["maximo"] == "600"
    assert (
        piloto.leer_estado(tmp_path / "estado" / "_params_autopilot.json").normas[f"{NORMA}|{LEY}"].etapa == "cerrada"
    )
    # The proposal made from the norm's text can no longer be applied over it.
    with pytest.raises(nucleo.DatosCambiadosError):
        aplicar_propuesta(primeras[0], consolidado, data_dir=datos, hoy=HOY)
    # Next day: everything matches, nothing to do, no model called.
    informe, nuevas = _pilotar(datos, tmp_path, consolidado, Guion(), Guion(), auto_aplicar=True)
    assert nuevas == [] and informe.objetivos == 0 and informe.no_coinciden == []


def test_autopilot_catches_a_change_no_norm_announced_and_respects_the_cap_of_the_run(
    datos: Path, tmp_path: Path
) -> None:
    # The consolidated text changed (deduction and scale) and the registry knows of no norm: verification finds it.
    cambiada = ley(alquiler=(P1N, P2N)).replace("12.000,00", "13.000,00")
    texto_de = boe(datos, consolidada=cambiada)
    por_tipo = {
        "deduccion_autonomica": (lectura(NUEVA, [P1N, P2N]), revision_alquiler()),
        # A scale that does not add up (13.000 at 9 % is not 1.080): stopped by the gate, no reviewer call.
        "escala": (
            lectura(
                {
                    "tramos": [
                        {"hasta": "13000", "tipo": "0.09", "cuota_hasta_inicio": "0"},
                        {"hasta": None, "tipo": "0.19", "cuota_hasta_inicio": "1080"},
                    ]
                },
                ["0,00 0,00 13.000,00 9,00 %", "13.000,00 1.080,00 En adelante 19,00 %"],
            ),
            None,
        ),
    }
    sin_modelo, _ = _pilotar(datos, tmp_path, texto_de, registro={})
    assert sin_modelo.objetivos == 2 and len(sin_modelo.no_coinciden) == 2 and sin_modelo.normas == []
    primero = "escala" if "escala" in sin_modelo.no_coinciden[0] else "deduccion_autonomica"
    leido, revisado = por_tipo[primero]
    informe, nuevas = _pilotar(
        datos,
        tmp_path,
        texto_de,
        Guion(leido),
        Guion(*([revisado] if revisado else [])),
        registro={},
        max_grupos=1,
        max_iter=1,
    )
    assert informe.objetivos == 2 and len(nuevas) == 1 and nuevas[0].tipo == primero
    assert nuevas[0].estado == ("sin_converger" if primero == "escala" else "propuesta")
    assert len(informe.recodificacion["sin_procesar"]) == 1  # type: ignore[index]
    parte = piloto.texto_del_parte(informe, nuevas)
    assert (
        "Grupos que hoy no coinciden" in parte
        and "Quedan para la próxima ejecución: 1" in parte
        and nuevas[0].id in parte
    )
    assert piloto.huella_del_parte(informe) != piloto.huella_del_parte(piloto.InformePiloto())


def test_autopilot_splits_an_article_a_norm_adds_into_new_deductions(datos: Path, tmp_path: Path) -> None:
    registro = {
        NORMA: {
            "fecha": "2026-11-10",
            "titulo": "Ley 4/2026",
            "modifica": [{"boe_id": LEY, "accion": "AÑADE", "texto": "el art. 14 a la Ley 1/2020", "parametros": True}],
        }
    }
    texto_de = boe(datos, consolidada=ley(extra=ART_14))
    # Without a model the new article is reported as waiting.
    informe, _ = _pilotar(datos, tmp_path, texto_de, registro=registro)
    assert informe.desgloses == [{"norma": NORMA, "articulo": "14", "pendiente": informe.desgloses[0]["pendiente"]}]
    no_calculable = lectura(
        None, [], resultado="no_calculable", valor_json="", motivo="Depende de un dato que no existe."
    )
    lector = Guion(ELEMENTOS, no_calculable, no_calculable)
    informe, nuevas = _pilotar(datos, tmp_path, texto_de, lector, Guion(), registro=registro)
    assert informe.desgloses[0]["deducciones_nuevas"] == ["art. 14.Uno", "art. 14.Dos"]
    assert [p.ruta[-1] for p in nuevas] == ["articulo=art. 14.Uno", "articulo=art. 14.Dos"]
    assert all(p.estado == "sin_converger" and p.origen == "desglose" for p in nuevas)
    assert informe.recodificacion["llamadas_llm"] == 3  # type: ignore[index]
    # The article is split once; its deductions are not proposed again for the same text.
    informe, nuevas = _pilotar(datos, tmp_path, texto_de, Guion(), Guion(), registro=registro)
    assert nuevas == [] and informe.desgloses == []


def test_autopilot_does_not_reread_the_past_nor_wait_for_ever(datos: Path, tmp_path: Path) -> None:
    sin_consolidar = boe(datos, consolidada=ley(), diario=norma_modificadora())
    # The parameters were read from a text later than the norm: it is already in the data.
    informe, _ = _pilotar(datos, tmp_path, sin_consolidar, Guion(), Guion(), leidas={LEY: "2026-11-11"})
    assert informe.objetivos == 0 and informe.normas == [] and informe.normas_nuevas == []
    assert "anterior al texto del que se leyeron los parámetros (2026-11-11)" in informe.normas_descartadas[0]
    assert "Normas que no se releen" in piloto.texto_del_parte(informe, [])
    informe, _ = _pilotar(datos, tmp_path, sin_consolidar, Guion(), Guion(), leidas={LEY: "2026-11-11"})
    assert informe.normas_descartadas == [] and not informe.hay_algo  # said once
    # A norm the consolidated text never names is not waited for indefinitely.
    otro = tmp_path / "otro"
    otro.mkdir()
    informe, _ = _pilotar(datos, otro, sin_consolidar, leidas={LEY: "2026-03-27"})
    assert informe.normas[0]["etapa"] == "sin_consolidar" and informe.objetivos == 1
    tarde = date(2027, 3, 1)
    informe, _ = piloto.pilotar(
        REGISTRO,
        leyes={LEY: NOMBRE_LEY},
        comunidades={LEY: "galicia"},
        texto_de=sin_consolidar,
        estado_path=otro / "estado" / "_params_autopilot.json",
        almacen=AlmacenPropuestas(otro / "propuestas"),
        data_dir=datos,
        hoy=tarde,
    )
    assert (
        informe.objetivos == 0 and "sin aparecer en el texto consolidado tras 90 días" in informe.normas_descartadas[0]
    )


def test_norms_of_the_registry_are_matched_to_the_source_laws() -> None:
    registro = {
        NORMA: {
            "fecha": "2026-11-10",
            "titulo": "Ley",
            "modifica": [
                {
                    "boe_id": LEY,
                    "accion": "MODIFICA",
                    "texto": ", con efectos desde el 1 de enero de 2026, los arts. 4 y 5",
                },
                {"boe_id": LEY, "accion": "AÑADE", "texto": "el art. 5 bis y la disposición adicional 3"},
                {"boe_id": LEY, "accion": "MODIFICA", "texto": "el art. 9 y AÑADE el art. 9 bis a la Ley 1/2020"},
            ],
        },
        LEY: {"modifica": [{"boe_id": LEY, "accion": "CORRIGE", "texto": "errores"}]},  # a law does not amend itself
    }
    (norma,) = piloto.normas_del_registro(registro, {LEY: NOMBRE_LEY})
    assert (norma.norma, norma.ley) == (NORMA, LEY)
    assert norma.preceptos == ["4", "5", "5 bis", "da 3", "9", "9 bis"] and norma.anadidos == ["5 bis", "da 3", "9 bis"]
    assert norma.anotaciones[0].startswith(f"{NORMA}: MODIFICA , con efectos desde el 1 de enero de 2026")
    # The source laws, and the date of the text each was read from, come from the parameter files themselves.
    fuentes = cli_module._leyes_de_los_parametros(DATA_DIR)
    assert fuentes["BOE-A-2011-18161"]["comunidad"] == "galicia" and fuentes["BOE-A-2006-20764"]["comunidad"] is None
    assert fuentes["BOE-A-2006-20764"]["norma"].startswith("Ley 35/2006") and fuentes["BOE-A-2014-12328"]["leida"]
    assert cli_module._ley_citada_de("galicia", DATA_DIR) == "1/2011"


# --------------------------------------------------------------------------- #
# Measuring the loop against shipped encodings
# --------------------------------------------------------------------------- #


def test_evaluation_hides_the_encoding_and_compares_field_by_field(datos: Path) -> None:
    texto_de = boe(datos)
    objetivo = todos_los_objetivos([2026], data_dir=datos, boe_id=LEY, solo_irpf=True).objetivos[0]
    assert objetivo.tipo == "deduccion_autonomica" and objetivo.antes[2026] == ANTIGUA
    igual = {
        **ANTIGUA,
        "resumen": "Otra redacción: el 10 por ciento, máximo 500 euros.",
        "limite_individual": "22000.00",
    }
    sin_limite = {k: v for k, v in ANTIGUA.items() if k != "limite_individual"}
    lector = Guion(lectura(igual, [P1, P2]), lectura(sin_limite, [P1]))
    casos = casos_alquiler(P1, P2, nueva=False)
    revisor = Guion(*(revision(casos, base_limite_segun_ley="base_imponible_total") for _ in range(2)))
    resultado = evaluacion.evaluar(
        [objetivo, objetivo],
        lector=lector,
        revisor=revisor,
        texto_de=texto_de,
        data_dir=datos,
        max_iter=1,
        usd_por_millon_de_tokens=3.0,
    )
    # The reader saw the law, not the encoding it is measured against.
    assert "codificacion_anterior" not in lector.llamadas[0]["user"] and P1 in lector.llamadas[0]["user"]
    bien, mal = resultado.grupos
    assert (bien.estado, bien.acuerdo, bien.campos_distintos) == ("propuesta", 1.0, []) and bien.textos_distintos == [
        "resumen"
    ]
    assert mal.estado == "sin_converger" and mal.acuerdo is not None and mal.acuerdo < 1
    assert mal.campos_distintos == [{"campo": "limite_individual", "humano": "22000", "bucle": None}]
    assert "un céntimo por encima" in mal.objeciones_abiertas[0]  # the reviewer's case caught the missing ceiling
    assert (resultado.evaluados, resultado.convergieron, resultado.identicos) == (2, 1, 1)
    assert resultado.aplicables_sin_persona == 1 and resultado.aplicables_sin_persona_con_diferencias == 0
    assert resultado.llamadas_llm == 4 and resultado.tokens_entrada_aprox > 1000 and resultado.coste_entrada_aprox_usd
    assert _deduccion(datos, 2026) == ANTIGUA  # nothing written
    # The draw is reproducible and filtered by kind.
    todos = todos_los_objetivos(solo_irpf=True).objetivos
    elegidos = evaluacion.seleccionar(todos, 5, semilla=7, tipos=["deduccion_autonomica"])
    assert [o.ruta for o in elegidos] == [
        o.ruta for o in evaluacion.seleccionar(todos, 5, semilla=7, tipos=["deduccion_autonomica"])
    ]
    assert len(elegidos) == 5 and {o.tipo for o in evaluacion.seleccionar(todos, 40, tipos=["cifras"])} <= {
        a.tipo for a in CIFRAS.values()
    }
    assert evaluacion.comparar({"a": "1.50", "b": [1, 2], "titulo": "x"}, {"a": "1.5", "b": [1, 3], "titulo": "y"}) == (
        2,
        [{"campo": "b[1]", "humano": "2", "bucle": "3"}],
        ["titulo"],
    )


# --------------------------------------------------------------------------- #
# The shipped data against the gate
# --------------------------------------------------------------------------- #


def test_every_shipped_regional_deduction_passes_the_gate_with_its_stored_quotes() -> None:
    """What needs no law: the model, every figure inside a quote, no figure in the user's texts that no quote prints.

    The literal presence of each quote in its law is checked against the BOE itself by ``params-gate`` (and, more
    loosely, by ``params-verify``): the laws are too large to ship as fixtures.
    """
    assert control_de_lo_publicado() == []


def test_the_gate_reads_labels_and_family_units_as_the_laws_write_them() -> None:
    assert control.numeros_de_texto_sin_respaldo("compatible con la deducción del art. 110-2", "nada") == []
    assert control.numeros_de_texto_sin_respaldo("hasta 110-2 euros", "nada") == ["110"]
    modelo = nucleo.adaptador_de("deduccion_autonomica").controlar(  # the children of a family unit are minors
        {
            "articulo": "art. 5",
            "titulo": "t",
            "resumen": "88 euros por cada miembro de la unidad familiar con discapacidad.",
            "tipo": "fija",
            "importe": "88",
            "unidad_descendientes": {"edad_max": 17},
            "en_vigor_desde": "2020-01-01",
        },
        ["Por cada miembro de la unidad familiar con discapacidad se establece una deducción de 88 euros."],
        Objetivo.model_construct(
            ruta=["comunidades", "galicia", "deducciones_calculables", "articulo=art. 5"], ejercicios=[]
        ),
        efectos_desde=None,
        datos={},
    )[1]
    assert modelo.fallos == [] and any("unidad familiar" in r for r in modelo.revisar)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


class Canal:
    nombre = "prueba"

    def __init__(self) -> None:
        self.enviados: list[tuple[str, str]] = []

    def enviar(self, asunto: str, cuerpo: str) -> None:
        self.enviados.append((asunto, cuerpo))


@pytest.fixture
def cli(datos: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    snapshot = tmp_path / "leyes"
    snapshot.mkdir()
    texto_de = boe(datos, consolidada=ley(), diario=norma_modificadora())
    ids = {
        c.boe_id for e in (2025, 2026) for c in comprobaciones(IRPFParams.model_validate(_raw(datos, e)), evidencia={})
    }
    for boe_id in ids | {LEY}:
        for version in (None, "20250407", "20221231"):
            pagina = texto_de(boe_id, version)
            if pagina:
                (snapshot / (f"{boe_id}_p{version}.html" if version else f"{boe_id}.html")).write_text(pagina, "utf-8")
    (snapshot / f"{NORMA}_pdiario.html").write_text(norma_modificadora(), encoding="utf-8")
    salida = tmp_path / "html"
    salida.mkdir()
    (salida / cli_module.REGISTRO_NOVEDADES).write_text(json.dumps(REGISTRO), encoding="utf-8")
    canal = Canal()
    monkeypatch.setattr(cli_module, "_canales_del_operador", lambda: [canal])
    monkeypatch.setattr(cli_module, "hoy_en_espana", lambda: HOY)
    comunes = ["--snapshot-dir", str(snapshot), "--data-dir", str(datos)]

    def invocar(*args: str, propuestas: bool = True) -> Any:
        extra = ["--proposals-dir", str(tmp_path / "propuestas")] if propuestas else []
        return CliRunner().invoke(cli_module.cli, [*args, *comunes, *extra])

    invocar.canal = canal  # type: ignore[attr-defined]
    invocar.salida = salida  # type: ignore[attr-defined]
    invocar.snapshot = snapshot  # type: ignore[attr-defined]
    return invocar


def test_cli_autopilot_is_safe_without_a_key_idempotent_and_reports_once(cli: Any, datos: Path, monkeypatch) -> None:
    assert leer_snapshot(cli.snapshot)(NORMA, "diario") == norma_modificadora()
    args = ["params-autopilot", "--no-detect", "--output-dir", str(cli.salida)]
    monkeypatch.setattr(cli_module, "hay_credenciales_llm", lambda: False)
    monkeypatch.setattr(cli_module, "_proveedores_de_recodificacion", lambda: pytest.fail("no key, no model"))
    primera = cli(*args)
    assert primera.exit_code == 0, primera.output
    informe = json.loads(primera.output)["piloto"]
    assert (
        "No hay credenciales" in informe["omitido"]
        and informe["objetivos"] == 1
        and informe["avisado_por"] == ["prueba"]
    )
    assert informe["normas"][0]["etapa"] == "sin_consolidar" and informe["errores"] == []
    asunto, cuerpo = cli.canal.enviados[0]
    assert (
        asunto == piloto.ASUNTO_PARTE
        and NORMA in cuerpo
        and "AÚN SIN CONSOLIDAR" in cuerpo
        and "Grupos en espera: 1" in cuerpo
    )
    # The same situation the next day: looked at again, not announced again.
    segunda = json.loads(cli(*args).output)["piloto"]
    assert segunda["objetivos"] == 1 and segunda["avisado_por"] == [] and len(cli.canal.enviados) == 1

    monkeypatch.setattr(cli_module, "hay_credenciales_llm", lambda: True)
    monkeypatch.setattr(
        cli_module,
        "_proveedores_de_recodificacion",
        lambda: (Guion(_lectura_de_la_norma()), Guion(_revision_de_la_norma())),
    )
    tercera = json.loads(cli(*args, "--auto-apply").output)["piloto"]
    assert tercera["propuestas"][0]["estado"] == "propuesta" and tercera["propuestas"][0]["ejercicios"] == [2025, 2026]
    assert tercera["avisado_por"] == ["prueba"] and "Propuestas de esta ejecución" in cli.canal.enviados[1][1]
    assert _deduccion(datos, 2026) == ANTIGUA  # read from an unconsolidated norm: never applied without a person
    # --no-llm runs the deterministic part even with a key.
    monkeypatch.setattr(cli_module, "_proveedores_de_recodificacion", lambda: pytest.fail("--no-llm"))
    assert "--no-llm" in json.loads(cli(*args, "--no-llm").output)["piloto"]["omitido"]


def test_cli_recode_can_read_an_unconsolidated_norm_split_an_article_and_take_any_group(cli: Any, monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "hay_credenciales_llm", lambda: True)
    xml = (
        f'<documento><analisis><referencias><anteriores><anterior referencia="{LEY}"><palabra codigo="270">MODIFICA'
        "</palabra><texto>el art. 5 de la Ley 1/2020</texto></anterior></anteriores></referencias></analisis></documento>"
    )
    monkeypatch.setattr(cli_module, "_xml", lambda boe_id: xml.encode())
    monkeypatch.setattr(
        cli_module,
        "_proveedores_de_recodificacion",
        lambda: (Guion(_lectura_de_la_norma()), Guion(_revision_de_la_norma())),
    )
    assert cli("params-recode", "--norma", NORMA, "--sin-consolidar").exit_code != 0  # needs a named group
    hecho = cli("params-recode", "--comunidad", "galicia", "--articulo", "art. 5", "--norma", NORMA, "--sin-consolidar")
    assert hecho.exit_code == 0, hecho.output
    propuesta = json.loads(hecho.output)["propuestas"][0]
    assert propuesta["estado"] == "propuesta" and any("aún no había consolidado" in r for r in propuesta["por_revisar"])

    (cli.snapshot / f"{LEY}.html").write_text(ley(extra=ART_14), encoding="utf-8")
    rendido = lectura(None, [], resultado="no_calculable", valor_json="", motivo="Falta un dato.")
    monkeypatch.setattr(
        cli_module, "_proveedores_de_recodificacion", lambda: (Guion(ELEMENTOS, rendido, rendido), Guion())
    )
    assert cli("params-recode", "--desglosar").exit_code != 0  # needs --comunidad and --articulo
    desglosado = json.loads(
        cli("params-recode", "--comunidad", "galicia", "--articulo", "art. 14", "--desglosar").output
    )
    assert [e["etiqueta"] for e in desglosado["desglose"]["elementos"]] == ["art. 14.Uno", "art. 14.Dos"]
    assert desglosado["grupos_a_revisar"] == 2 and {p["estado"] for p in desglosado["propuestas"]} == {"sin_converger"}

    # Any group of figures can be asked for by its path, and the simulation needs no model.
    monkeypatch.setattr(cli_module, "_proveedores_de_recodificacion", lambda: pytest.fail("dry run"))
    simulada = json.loads(cli("params-recode", "--ruta", "minimos", "--dry-run").output)
    assert simulada["simulacion"][0]["tipo"] == "cifras:minimos" and simulada["simulacion"][0]["articulos"] == [
        "57",
        "58",
        "59",
        "60",
    ]
    congelada = cli("params-recode", "--ruta", "reduccion_arrendamiento/regimen_2021", "--dry-run")
    assert congelada.exit_code != 0 and "--version" in congelada.output


def test_cli_eval_and_gate(cli: Any, monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "_proveedores_de_recodificacion", lambda: pytest.fail("dry run"))
    ruta = "comunidades/galicia/deducciones_calculables/articulo=art. 5"
    simulada = cli("params-eval", "--ruta", ruta, "--dry-run", propuestas=False)
    assert simulada.exit_code == 0, simulada.output
    assert json.loads(simulada.output)["grupos"][0]["articulos"] == ["5"]
    monkeypatch.setattr(cli_module, "hay_credenciales_llm", lambda: False)
    assert "No hay credenciales" in cli("params-eval", "--ruta", ruta, propuestas=False).output
    monkeypatch.setattr(cli_module, "hay_credenciales_llm", lambda: True)
    casos = casos_alquiler(P1, P2, nueva=False)
    monkeypatch.setattr(
        cli_module,
        "_proveedores_de_recodificacion",
        lambda: (
            Guion(lectura(ANTIGUA, [P1, P2])),
            Guion(revision(casos, base_limite_segun_ley="base_imponible_total")),
        ),
    )
    medida = cli("params-eval", "--ruta", ruta, "--usd-por-millon", "3", propuestas=False)
    assert medida.exit_code == 0, medida.output
    resultado = json.loads(medida.output)
    assert (
        resultado["identicos"] == 1 and resultado["acuerdo_medio"] == 1.0 and resultado["coste_entrada_aprox_usd"] > 0
    )

    # The gate over what is shipped: offline here; against the fixture's law the temporary Galicia passes too.
    offline = CliRunner().invoke(cli_module.cli, ["params-gate", "--offline"])
    assert offline.exit_code == 0 and json.loads(offline.output) == {"fallan": 0, "deducciones": []}
    con_ley = cli("params-gate", "--ejercicio", "2026", propuestas=False)
    galicia = [d for d in json.loads(con_ley.output)["deducciones"] if d["comunidad"] == "galicia"]
    assert galicia == []
    (cli.snapshot / f"{LEY}.html").write_text(ley(alquiler=(P1N, P2N)), encoding="utf-8")
    roto = json.loads(cli("params-gate", "--ejercicio", "2026", propuestas=False).output)
    assert any(d["comunidad"] == "galicia" and "cita no literal" in d["fallos"][0] for d in roto["deducciones"])


def test_a_summary_that_is_not_json_reaches_the_format_watch(monkeypatch: pytest.MonkeyPatch) -> None:
    from hacienda_gpt.ops import formatos

    vistos: list[tuple[str, str, str]] = []
    monkeypatch.setattr(formatos, "observar", lambda clave, crudo, **kw: vistos.append((clave, crudo, kw["contexto"])))
    monkeypatch.setattr(cli_module, "_get", lambda url, **kw: b"<html>Servicio no disponible</html>")
    monkeypatch.setattr(cli_module.time, "sleep", lambda _s: None)
    with pytest.raises(ValueError):
        cli_module._sumario(date(2026, 11, 10))
    assert vistos == [("boe_sumario", "<html>Servicio no disponible</html>", "2026-11-10")]
    monkeypatch.setattr(cli_module, "_get", lambda url, **kw: b'{"data": {}}')
    assert cli_module._sumario(date(2026, 11, 10)) == {"data": {}} and len(vistos) == 1
    monkeypatch.setattr(cli_module, "_get", lambda url, **kw: None)
    assert cli_module._sumario(date(2026, 11, 8)) is None


def test_the_weekly_workflow_runs_the_autopilot_after_the_verification() -> None:
    workflow = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "vigilancia.yml").read_text("utf-8")
    verify, autopilot = workflow.index("params-verify --fail-on-mismatch"), workflow.index("proactive params-autopilot")
    assert verify < autopilot
    paso = workflow[workflow.rindex("- name:", 0, autopilot) : autopilot]
    # Always runs (its deterministic part needs no secret) and never writes parameters on the runner.
    assert "if: always()" in paso and 'RECODE_AUTO_APPLY: "false"' in paso and "secrets.ANTHROPIC_API_KEY" in paso
    assert re.search(r"path: recode-proposals\n\s+key: recode-proposals-", workflow)  # its memory survives the week
