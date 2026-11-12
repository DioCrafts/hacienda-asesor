"""The re-encoding loop over the parameter files that are not the IRPF one.

No network and no real model: each "law" is a small synthetic BOE page, the providers replay a script and the
parameter files are temporary copies of the shipped ones with ONE group rewritten to hang from that law. What
decides in every test is deterministic: the gate, and the real calculator of each tax run on the reviewer's cases.
"""

from __future__ import annotations

from collections.abc import Callable
import copy
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
from typing import Any

import pytest

from hacienda_gpt.tax import actividad, patrimonio, sociedades, sucesiones
from hacienda_gpt.tax.autonomos_params import AutonomosParams
from hacienda_gpt.tax.autonomos_params import comprobar_evidencias as comprobar_autonomos
from hacienda_gpt.tax.params import DATA_DIR
from hacienda_gpt.tax.patrimonio_params import PatrimonioParams
from hacienda_gpt.tax.patrimonio_params import comprobar_evidencias as comprobar_patrimonio
from hacienda_gpt.tax.recodificacion import (
    AlmacenPropuestas,
    Propuesta,
    adaptador_de,
    aplicar_propuesta,
    confirmar,
    pendientes_de_confirmar,
    recodificar,
)
from hacienda_gpt.tax.recodificacion import adaptadores_otros as otros
from hacienda_gpt.tax.recodificacion.control import campos_numericos
from hacienda_gpt.tax.sociedades_params import SociedadesParams
from hacienda_gpt.tax.sociedades_params import comprobar_evidencias as comprobar_sociedades
from hacienda_gpt.tax.sucesiones_params import ISDParams
from hacienda_gpt.tax.sucesiones_params import comprobar_evidencias as comprobar_isd

D = Decimal
LEY = "BOE-T-2020-9"
HOY = date(2026, 11, 12)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
EFECTOS_2026 = "Se modifica, con efectos desde el 1 de enero de 2026, por el art. 1 de la Ley 1/2026, de 20 de junio."
EFECTOS_2025 = "Se modifica, con efectos desde el 1 de enero de 2025, por el art. 1 de la Ley 1/2026, de 20 de junio."
EFECTOS_2027 = "Se modifica, con efectos desde el 1 de enero de 2027, por el art. 1 de la Ley 1/2026, de 20 de junio."
EFECTOS_JULIO = "Se modifica, con efectos desde el 1 de julio de 2026, por el art. 1 de la Ley 1/2026, de 20 de junio."
FUENTE_LEY = {
    "norma": "Ley sintética 1/2020 (texto consolidado)",
    "boe_id": LEY,
    "url": f"https://www.boe.es/buscar/act.php?id={LEY}",
    "consolidado_a": "2026-06-30",
    "consultado_el": "2026-11-12",
    "sha256_html": "0" * 64,
}


# --------------------------------------------------------------------------- #
# Shared scaffolding
# --------------------------------------------------------------------------- #


def pagina(numero: int, titulo: str, parrafos: list[str], efectos: str = EFECTOS_2026) -> str:
    """One article of a synthetic law in the BOE's markup, with its amendment note and version selector."""
    cuerpo = "".join(f'<p class="parrafo">{p}</p>\n' for p in parrafos)
    return f"""<html><head><style>p {{}}</style></head><body>
<div class="bloque" id="a{numero}"><p class="bloque">[Bloque {numero}: #a{numero}]</p>
<h5 class="articulo">Artículo {numero}. {titulo}.</h5>
{cuerpo}<p class="nota_pie">{efectos}</p>
<form class="lista formBOE" method="GET" action="/buscar/act.php#a{numero}"><fieldset>
<legend class="fuera">Seleccionar redacción:</legend>
<p><input type="radio" name="p" value="20260630" checked="checked"/>
<label>Última actualización, publicada el 30/06/2026, en vigor a partir del 01/07/2026.</label></p>
</fieldset></form>
<p class="linkSubir"><a href="#top">Subir</a></p></div>
<div class="bloque" id="df"><p class="bloque">[Bloque 99: #df]</p>
<h5 class="articulo">Disposición final única. Entrada en vigor.</h5><p>Esta ley entra en vigor al día siguiente.</p></div>
</body></html>"""


def texto_de(contenido: str) -> Callable[..., str | None]:
    return lambda boe_id, version=None: contenido if boe_id == LEY and version is None else None


class Guion:
    """Replays answers in order."""

    name = "guion"
    model = "modelo-de-prueba"

    def __init__(self, *respuestas: dict[str, Any]) -> None:
        self.respuestas = list(respuestas)
        self.llamadas: list[dict[str, Any]] = []

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any], schema_name: str, **_: Any) -> Any:
        self.llamadas.append({"system": system, "user": user, "schema_name": schema_name})
        if not self.respuestas:
            raise AssertionError(f"llamada no prevista en el guion ({schema_name})")
        return dict(self.respuestas.pop(0))


def lectura(valor: dict[str, Any], citas: list[str], efectos: str = EFECTOS_2026, desde: str = "2026-01-01") -> dict:
    return {
        "resultado": "codificada",
        "valor_json": json.dumps(valor, ensure_ascii=False),
        "evidencia": citas,
        "efectos_desde": desde,
        "efectos_hasta": "",
        "cita_efectos": efectos,
        "articulos_necesarios": [],
        "motivo": "",
        "cambios": "Cambian las cifras del artículo.",
    }


def caso(descripcion: str, calculo: str, entrada: dict[str, Any], linea: str, esperado: str, cita: str) -> dict:
    return {
        "descripcion": descripcion,
        "datos_json": json.dumps({"calculo": calculo, "entrada": entrada, "resultado": linea}),
        "importe_esperado": esperado,
        "fundamento": cita,
    }


def revision(casos: list[dict], efectos: str = EFECTOS_2026, desde: str = "2026-01-01") -> dict[str, Any]:
    return {"conforme": True, "discrepancias": [], "efectos_desde": desde, "cita_efectos": efectos, "casos": casos}


def _escribir(path: Path, raw: dict[str, Any]) -> None:
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _leer(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _objetivo(familia: otros.Familia, datos: Path, ley: str, ruta: list[str]):
    hallazgo = otros.objetivos_de_verificacion(None, texto_de(ley), data_dir=datos, familias=[familia])
    propios = [o for o in hallazgo.objetivos if o.ruta == ruta]
    assert len(propios) == 1, (hallazgo.objetivos, hallazgo.sin_adaptador)
    return propios[0]


def _correr(familia: otros.Familia, datos: Path, ley: str, ruta: list[str], lector: Guion, revisor: Guion) -> Propuesta:
    objetivo = _objetivo(familia, datos, ley, ruta)
    ficheros = {e: _leer(datos / familia.fichero.format(ejercicio=e)) for e in objetivo.ejercicios}
    return recodificar(objetivo, lector=lector, revisor=revisor, texto_de=texto_de(ley), datos=ficheros)


def _aplicar_y_confirmar(datos: Path, tmp_path: Path, propuesta: Propuesta, ley: str) -> AlmacenPropuestas:
    almacen = AlmacenPropuestas(tmp_path / "propuestas")
    almacen.guardar(propuesta)
    aplicada = aplicar_propuesta(propuesta, texto_de(ley), almacen=almacen, data_dir=datos, hoy=HOY, automatica=True)
    assert aplicada.estado == "aplicada"
    return almacen


# --------------------------------------------------------------------------- #
# The adapter family
# --------------------------------------------------------------------------- #


def test_one_adapter_per_group_model_and_every_numeric_field_has_a_rule() -> None:
    tipos = {a.tipo for a in otros.ADAPTADORES.values()}
    assert {"autonomos:PagoFraccionado", "sociedades:TiposGravamen", "patrimonio:EscalaPatrimonio"} <= tipos
    assert {"isd:ReglaTarifa", "isd:ReglaCoeficientes", "isd:ReglaBonificacion", "isd:Reduccion"} <= tipos
    for adaptador in otros.ADAPTADORES.values():
        assert adaptador_de(adaptador.tipo) is adaptador
        reglas = adaptador.familia.reglas
        con_regla = reglas.euros | reglas.fracciones | reglas.porcentajes_de_tabla | reglas.factores | reglas.grados
        for campo in campos_numericos(adaptador.modelo) - con_regla - reglas.ignorar:
            # What is left are whole numbers (ages, years, quarters): the walker reads them in figures or in words.
            assert campo.startswith(("edad_", "anos_", "colateral_", "pagos_fraccionados_desde")), (
                adaptador.tipo,
                campo,
            )
        # A Decimal the walker ignores is a count the file's own self-check reads ("cuatro años").
        assert (reglas.ignorar & campos_numericos(adaptador.modelo)) <= adaptador.familia.cuentas | {"en_vigor_desde"}
        assert adaptador.modelo.__name__ in adaptador.esquema() and "program" in adaptador.guia()
    # Groups no calculator turns into an amount ask for no test cases and are always left to a person.
    sin_casos = sorted(a.tipo for a in otros.ADAPTADORES.values() if not a.evaluable)
    assert sin_casos == [
        "autonomos:EstimacionDirectaSimplificada",
        "isd:Regla",
        "patrimonio:Grupo",
        "patrimonio:VigenciaITSGF",
        "sociedades:ComparacionIRPF",
        "sociedades:TributacionMinima",
    ]
    assert all(otros.ADAPTADORES[k].min_casos == 0 for k in [("isd", "_Regla"), ("patrimonio", "_Grupo")])


CONOCIDOS_ISD = {
    "estatal/reducciones_bienes/1",
    "comunidades/cataluna/bonificaciones/1",
    "comunidades/cataluna/bonificaciones/2",
    "comunidades/aragon/bonificaciones/0",
    "comunidades/aragon/bonificaciones/1",
    "comunidades/asturias/coeficientes/0",
    "comunidades/baleares/reducciones_bienes/0",
    "comunidades/baleares/reducciones_bienes/1",
    "comunidades/canarias/reducciones/0",
    "comunidades/castilla_y_leon/reducciones_bienes/0",
    "comunidades/extremadura/reducciones_bienes/0",
    "comunidades/galicia/bonificaciones/0",
}
# Groups with no quote of an article or provision of a law: none today (an annex or a preamble alone would be one).
ILEGIBLES: dict[str, set[str]] = {}


@pytest.mark.parametrize(
    ("clave", "ejercicio", "carpeta", "grupos"),
    [
        ("autonomos", 2026, "boe_autonomos", 10),
        ("autonomos", 2025, "boe_autonomos/2025", 9),
        ("sociedades", 2026, "boe_sociedades", 7),
        ("patrimonio", 2026, "boe_patrimonio", 37),
        ("isd", 2026, "boe_sucesiones", 177),
    ],
)
def test_shipped_groups_pass_the_gate_with_their_own_quotes(
    clave: str, ejercicio: int, carpeta: str, grupos: int
) -> None:
    """No model: each shipped group, read with the quotes it already has, against the law extracts of the repo."""
    familia = otros.familia_de(clave)
    raw = _leer(DATA_DIR / familia.fichero.format(ejercicio=ejercicio))
    textos = {p.stem: p.read_text(encoding="utf-8") for p in (FIXTURES / carpeta).glob("*.txt")}
    revisados = otros.revisar_grupos_vigentes(familia, raw, textos)
    assert len(revisados) == grupos
    por_estado: dict[str, set[str]] = {}
    for r in revisados:
        por_estado.setdefault(r.estado, set()).add("/".join(r.ruta))
    assert por_estado.get("ilegible", set()) == ILEGIBLES.get(clave, set())
    fallan = por_estado.get("falla", set())
    if clave != "isd":
        assert not fallan, [(r.ruta, r.fallos) for r in revisados if r.estado == "falla"]
    else:
        # Known: summaries written by hand that mention a figure none of the rule's own quotes prints. Data is not
        # touched here; the list may only shrink.
        assert fallan <= CONOCIDOS_ISD
        assert all("menciona" in f for r in revisados if r.estado == "falla" for f in r.fallos)
    # A group that rests on another law or on a disposition, or that no calculator evaluates, is never automatic.
    for r in revisados:
        if r.estado == "persona":
            assert r.revisar
    assert por_estado.get("ok"), "some group must be maintainable end to end"


def test_the_periodic_verification_covers_the_four_files() -> None:
    def de_fixtures(carpeta: str) -> Callable[..., str | None]:
        def leer(boe_id: str, version: str | None = None) -> str | None:
            path = FIXTURES / carpeta / f"{boe_id}.txt"
            return path.read_text(encoding="utf-8") if path.exists() and version is None else None

        return leer

    resultados = otros.verificar_ficheros(de_fixtures("boe_patrimonio"), familias=[otros.PATRIMONIO])
    ((fichero, checks),) = resultados.items()
    assert fichero == "patrimonio_2026.json" and len(checks) >= 37 and all(r.ok for r in checks)
    # A law that no longer says what a quote says: the group is found, with the article to re-read.
    cambiada = de_fixtures("boe_patrimonio")("BOE-A-1991-14392").replace("700.000 euros", "800.000 euros")
    leer = de_fixtures("boe_patrimonio")
    hallazgo = otros.objetivos_de_verificacion(
        None,
        lambda boe_id, version=None: cambiada if boe_id == "BOE-A-1991-14392" else leer(boe_id, version),
        familias=[otros.PATRIMONIO],
    )
    assert [(o.tipo, o.ruta, o.articulos_ley) for o in hallazgo.objetivos] == [
        ("patrimonio:MinimoExento", ["estado", "minimo_exento"], ["28"])
    ]
    assert hallazgo.objetivos[0].ambito == "estado" and hallazgo.objetivos[0].ejercicios == [2026]
    # A provision is re-read like an article: Galicia's scale lives in its transitional provision 3.
    otra_escala = leer("BOE-A-2011-18161").replace("183.670,29", "183.670,30")
    galicia = otros.objetivos_de_verificacion(
        None,
        lambda boe_id, version=None: otra_escala if boe_id == "BOE-A-2011-18161" else leer(boe_id, version),
        familias=[otros.PATRIMONIO],
        ambito="galicia",
    )
    assert [(o.tipo, o.ruta[-1], o.articulos_ley) for o in galicia.objetivos] == [
        ("patrimonio:EscalaPatrimonio", "escala", ["dt 3"])
    ]
    assert not galicia.sin_adaptador
    # A failing quote of an annex of a ministerial order is nothing the loop can re-read: reported for a person.
    en_sociedades = de_fixtures("boe_sociedades")
    sin_anexo = en_sociedades("BOE-A-2025-5407").replace("empresa de reducida dimensión", "empresa pequeña")
    orden = otros.objetivos_de_verificacion(
        None,
        lambda boe_id, version=None: sin_anexo if boe_id == "BOE-A-2025-5407" else en_sociedades(boe_id, version),
        familias=[otros.SOCIEDADES],
    )
    assert not orden.objetivos and orden.sin_adaptador
    assert all("revísalo a mano" in fila and "BOE-A-2025-5407" in fila for fila in orden.sin_adaptador)
    # A law that cannot be read is an error, never "everything is gone".
    nada = otros.objetivos_de_verificacion(None, lambda boe_id, version=None: None, familias=[otros.SOCIEDADES])
    assert not nada.objetivos and any("no se pudo leer" in e for e in nada.errores)


# --------------------------------------------------------------------------- #
# Self-employed: the flat allowance for hard-to-justify expenses (two year files)
# --------------------------------------------------------------------------- #

GDJ_VIEJA = (
    "2.ª El conjunto de las provisiones deducibles y los gastos de difícil justificación se cuantificará aplicando el "
    "porcentaje del 5 por ciento sobre el rendimiento neto, excluido este concepto, sin que la cuantía resultante "
    "pueda superar 2.000 euros anuales."
)
GDJ_NUEVA = GDJ_VIEJA.replace("5 por ciento", "7 por ciento").replace("2.000 euros", "3.000 euros")
GDJ_OTRA = "3.ª En las actividades agrícolas la cuantía resultante no podrá superar 1.500 euros anuales."
LEY_GDJ = pagina(30, "Determinación del rendimiento neto en estimación directa simplificada", [GDJ_NUEVA, GDJ_OTRA])
GDJ = ["gastos_dificil_justificacion"]
C_GDJ = [
    caso("por debajo del tope", "rendimiento_actividad", {"ingresos_integros": "30000"}, "gastos_dificil_justificacion", "2100.00", GDJ_NUEVA),
    caso("tope alcanzado", "rendimiento_actividad", {"ingresos_integros": "100000"}, "gastos_dificil_justificacion", "3000", GDJ_NUEVA),
    caso("pago fraccionado del primer trimestre", "modelo_130", {"trimestre": 1, "ingresos_acumulados": "10000"}, "rendimiento_neto_acumulado", "9300.00", GDJ_NUEVA),
]  # fmt: skip


@pytest.fixture
def autonomos(tmp_path: Path) -> Path:
    carpeta = tmp_path / "data"
    carpeta.mkdir()
    for ejercicio in (2025, 2026):
        raw = _leer(DATA_DIR / f"autonomos_{ejercicio}.json")
        raw["fuentes_principales"][LEY] = FUENTE_LEY
        raw["gastos_dificil_justificacion"]["fuente"] = {
            "referencia": "art. 30 de la ley sintética",
            "en_vigor_desde": "2020-01-02",
            "verificado": True,
        }
        raw["gastos_dificil_justificacion"]["evidencia"] = [
            {
                "boe_id": LEY,
                "articulo": "art. 30.2.ª de la ley sintética",
                "cita": GDJ_VIEJA,
                "cifras": {"porcentaje": "5 por ciento", "limite_anual": "2.000 euros"},
            }
        ]
        assert comprobar_autonomos(AutonomosParams.model_validate(raw)) == []
        _escribir(carpeta / f"autonomos_{ejercicio}.json", raw)
    return carpeta


def test_autonomos_converge_and_touch_only_the_years_the_wording_reaches(autonomos: Path) -> None:
    objetivo = _objetivo(otros.AUTONOMOS, autonomos, LEY_GDJ, GDJ)
    assert objetivo.tipo == "autonomos:GastosDificilJustificacion" and objetivo.ejercicios == [2025, 2026]
    assert objetivo.boe_id == LEY and objetivo.articulos_ley == ["30.2.a"] and objetivo.citas_antes == [GDJ_VIEJA]
    assert any("5 por ciento" in m for m in objetivo.motivo)
    lector = Guion(lectura({"porcentaje": "0.07", "limite_anual": "3000"}, [GDJ_NUEVA]))
    propuesta = _correr(otros.AUTONOMOS, autonomos, LEY_GDJ, GDJ, lector, Guion(revision(C_GDJ)))
    assert propuesta.estado == "propuesta" and propuesta.resultado == "recodificada" and propuesta.aplicable_sin_persona
    assert propuesta.ejercicios == [2026] and 2025 in propuesta.ejercicios_excluidos
    assert propuesta.despues == {"porcentaje": "0.07", "limite_anual": "3000"}
    assert [c.coincide for c in propuesta.casos] == [True, True, True]
    # The reader was shown the group's own model, not the whole file's schema.
    assert "class GastosDificilJustificacion" in lector.llamadas[0]["system"]
    assert "class ReduccionDependientes" not in lector.llamadas[0]["system"]
    # An amending law published in 2026 with effects from 1 January 2025 reaches both year files.
    retro = LEY_GDJ.replace(EFECTOS_2026, EFECTOS_2025)
    ambas = _correr(
        otros.AUTONOMOS,
        autonomos,
        retro,
        GDJ,
        Guion(lectura({"porcentaje": "0.07", "limite_anual": "3000"}, [GDJ_NUEVA], EFECTOS_2025, "2025-01-01")),
        Guion(revision(C_GDJ, EFECTOS_2025, "2025-01-01")),
    )
    assert ambas.estado == "propuesta" and ambas.ejercicios == [2025, 2026] and not ambas.ejercicios_excluidos


def test_autonomos_gate_refuses_a_figure_no_quote_prints(autonomos: Path) -> None:
    lector = Guion(*[lectura({"porcentaje": "0.07", "limite_anual": "3500"}, [GDJ_NUEVA])] * 3)
    revisor = Guion()  # the gate comes first: the reviewer is never called
    propuesta = _correr(otros.AUTONOMOS, autonomos, LEY_GDJ, GDJ, lector, revisor)
    assert propuesta.estado == "sin_converger" and not revisor.llamadas
    assert any("limite_anual = 3500" in o for o in propuesta.objeciones_abiertas)
    # Provenance is the program's, never the proposal's.
    con_fuente = {"porcentaje": "0.07", "limite_anual": "3000", "fuente": {"referencia": "x"}}
    otra = _correr(otros.AUTONOMOS, autonomos, LEY_GDJ, GDJ, Guion(*[lectura(con_fuente, [GDJ_NUEVA])] * 3), Guion())
    assert any("«fuente» lo pone el programa" in o for o in otra.objeciones_abiertas)


def test_autonomos_reviewer_case_through_the_calculator_catches_a_wrong_ceiling(autonomos: Path) -> None:
    # 1.500 is printed in the article (rule 3.ª), so the gate cannot tell it is the wrong ceiling: the calculator can.
    mala = lectura({"porcentaje": "0.07", "limite_anual": "1500"}, [GDJ_NUEVA, GDJ_OTRA])
    buena = lectura({"porcentaje": "0.07", "limite_anual": "3000"}, [GDJ_NUEVA])
    lector = Guion(mala, buena)
    propuesta = _correr(otros.AUTONOMOS, autonomos, LEY_GDJ, GDJ, lector, Guion(revision(C_GDJ), revision(C_GDJ)))
    assert propuesta.estado == "propuesta" and len(propuesta.iteraciones) == 2
    primera = propuesta.iteraciones[0]
    # The third case stays below either ceiling: it cannot tell them apart, the other two can.
    assert [c.coincide for c in primera.casos] == [False, False, True]
    assert primera.casos[1].obtenido == D("1500.00") and primera.casos[1].esperado == D("3000.00")
    assert any("según la ley corresponden 3000.00" in o for o in primera.objeciones)
    assert "<objeciones>" in lector.llamadas[1]["user"] and propuesta.despues["limite_anual"] == "3000"


def test_autonomos_apply_marks_the_group_and_the_calculator_says_so_until_confirmed(
    autonomos: Path, tmp_path: Path
) -> None:
    propuesta = _correr(
        otros.AUTONOMOS,
        autonomos,
        LEY_GDJ,
        GDJ,
        Guion(lectura({"porcentaje": "0.07", "limite_anual": "3000"}, [GDJ_NUEVA])),
        Guion(revision(C_GDJ)),
    )
    almacen = _aplicar_y_confirmar(autonomos, tmp_path, propuesta, LEY_GDJ)
    assert _leer(autonomos / "autonomos_2025.json")["gastos_dificil_justificacion"]["porcentaje"] == "0.05"
    grupo = _leer(autonomos / "autonomos_2026.json")["gastos_dificil_justificacion"]
    assert grupo["porcentaje"] == "0.07" and grupo["limite_anual"] == "3000" and grupo["avisos"] == []
    assert grupo["fuente"]["verificado"] is False and grupo["fuente"]["en_vigor_desde"] == "2026-01-01"
    assert grupo["fuente"]["nota"].startswith(otros.MARCA + "2026-11-12")
    # The quote inherits the label of the passage it rewords and carries the spelling of each figure,
    # so the file's own self-check — and next week's verification — accept what was written.
    assert grupo["evidencia"] == [
        {
            "boe_id": LEY,
            "articulo": "art. 30.2.ª de la ley sintética",
            "cita": GDJ_NUEVA,
            "cifras": {"porcentaje": "7 por ciento", "limite_anual": "3.000"},
        }
    ]
    params = AutonomosParams.model_validate(_leer(autonomos / "autonomos_2026.json"))
    assert comprobar_autonomos(params) == []
    assert not otros.objetivos_de_verificacion(
        [2026], texto_de(LEY_GDJ), data_dir=autonomos, familias=[otros.AUTONOMOS]
    ).objetivos
    entrada = actividad.ActividadInput(ejercicio=2026, ingresos_integros=D(30000))
    resultado = actividad.calcular_rendimiento_actividad(entrada, params)
    assert resultado.gastos_dificil_justificacion == D("2100.00")
    assert any("recodificación automática pendiente de revisión humana" in a for a in resultado.avisos)
    pendientes = pendientes_de_confirmar(autonomos)
    assert [(p.tipo, p.ejercicio, p.ruta) for p in pendientes] == [("autonomos:GastosDificilJustificacion", 2026, GDJ)]
    assert confirmar(pendientes, data_dir=autonomos, almacen=almacen) == 1
    confirmado = _leer(autonomos / "autonomos_2026.json")["gastos_dificil_justificacion"]["fuente"]
    assert confirmado == {
        "referencia": "art. 30 de la ley sintética",
        "en_vigor_desde": "2026-01-01",
        "verificado": True,
    }
    params = AutonomosParams.model_validate(_leer(autonomos / "autonomos_2026.json"))
    limpio = actividad.calcular_rendimiento_actividad(entrada, params)
    assert not any("recodificación automática" in a for a in limpio.avisos) and not pendientes_de_confirmar(autonomos)
    assert almacen.leer(propuesta.id).confirmada_el is not None


def test_a_group_that_also_rests_on_another_law_keeps_those_quotes_and_always_needs_a_person(tmp_path: Path) -> None:
    """The shipped group: art. 30 RIRPF (re-read) plus a quote of the LIRPF and one of art. 110 RIRPF."""
    carpeta = tmp_path / "data"
    carpeta.mkdir()
    raw = _leer(DATA_DIR / "autonomos_2026.json")
    _escribir(carpeta / "autonomos_2026.json", raw)
    antes = raw["gastos_dificil_justificacion"]
    objetivo = otros.objetivo_de_ruta("autonomos", [2026], GDJ, data_dir=carpeta, boe_id="BOE-A-2007-6820")
    assert objetivo.articulos_ley == ["30.2.a", "110.1.a"] and len(objetivo.citas_antes) == 2
    adaptador = otros.ADAPTADORES[("autonomos", "GastosDificilJustificacion")]
    valor = {"porcentaje": "0.05", "limite_anual": "2000"}
    modelo, control = adaptador.controlar(valor, objetivo.citas_antes, objetivo, efectos_desde=None, datos={2026: raw})
    assert modelo is not None and control.ok
    assert any("BOE-A-2006-20764" in nota and "no relee" in nota for nota in control.revisar)
    final, _, nuevas, heredadas = adaptador.componer(
        valor, objetivo.citas_antes, antes, ruta=GDJ, boe_id=objetivo.boe_id, articulos=["30", "110"], efectos=None
    )
    assert final["evidencia"][0]["cifras"] == antes["evidencia"][0]["cifras"]
    assert [e["articulo"] for e in final["evidencia"]] == [e["articulo"] for e in antes["evidencia"]][:1] + [
        antes["evidencia"][2]["articulo"],
        antes["evidencia"][1]["articulo"],
    ]
    assert len(nuevas) == 2 and [e["boe_id"] for e in heredadas] == ["BOE-A-2006-20764"]
    # A figure only the inherited quote could back cannot move: 2.500 is in no quote at all.
    _, peor = adaptador.controlar(
        {"porcentaje": "0.05", "limite_anual": "2500"},
        objetivo.citas_antes,
        objetivo,
        efectos_desde=None,
        datos={2026: raw},
    )
    assert any("limite_anual" in f for f in peor.fallos)


# --------------------------------------------------------------------------- #
# Corporate tax: rates by kind of company
# --------------------------------------------------------------------------- #

IS_1 = (
    "1. El tipo general de gravamen para los contribuyentes de este Impuesto será el 25 por ciento, excepto para las "
    "entidades cuyo importe neto de la cifra de negocios del período impositivo inmediato anterior sea inferior a "
    "1 millón de euros, que aplicarán la siguiente escala: a) Por la parte de base imponible comprendida entre 0 y "
    "50.000 euros, al tipo del 17 por ciento. b) Por la parte de base imponible restante, al tipo del 20 por ciento. "
    "En los períodos de duración inferior al año, el primer tramo se prorrateará entre 365 días."
)
IS_1_NUEVA = IS_1.replace("17 por ciento", "15 por ciento")
IS_2 = "Las entidades de nueva creación que realicen actividades económicas tributarán al tipo del 15 por ciento."
IS_3 = (
    "No obstante, las entidades que cumplan las previsiones del artículo 101 de esta Ley, por ser su cifra de negocios "
    "inferior a 10 millones de euros, tributarán al tipo del 24 por ciento."
)
IS_4 = (
    "A estos efectos, no se entenderá iniciada una actividad económica cuando la actividad hubiera sido realizada "
    "con carácter previo por otras personas o entidades vinculadas."
)
IS_CITAS = [IS_1_NUEVA, IS_2, IS_3, IS_4]
EFECTOS_IS = (
    "Se modifica, con efectos para los períodos impositivos que se inicien a partir del 1 de enero de 2026, por la "
    "disposición final 1 de la Ley 1/2026, de 20 de junio."
)
LEY_IS = pagina(29, "El tipo de gravamen", [IS_1_NUEVA, IS_2, IS_3, IS_4], EFECTOS_IS)
TIPOS = ["tipos_gravamen"]
IS_VALOR = {
    "general": "0.25",
    "nueva_creacion": "0.15",
    "limite_microempresa": "1000000",
    "limite_primer_tramo": "50000",
    "dias_ano": "365",
    "limite_reducida_dimension": "10000000",
    "por_ano": {"2026": {"micro_primer_tramo": "0.15", "micro_resto": "0.20", "reducida_dimension": "0.24"}},
}
C_IS = [
    caso("microempresa, base en el primer tramo", "impuesto_sociedades", {"resultado_contable": "40000", "cifra_negocios_anterior": "500000"}, "cuota_integra", "6000.00", IS_1_NUEVA),
    caso("microempresa, base sobre los dos tramos", "impuesto_sociedades", {"resultado_contable": "100000", "cifra_negocios_anterior": "500000"}, "cuota_integra", "17500.00", IS_1_NUEVA),
    caso("cifra de negocios de 2 millones: reducida dimensión", "impuesto_sociedades", {"resultado_contable": "100000", "cifra_negocios_anterior": "2000000"}, "cuota_integra", "24000.00", IS_3),
    caso("exactamente 10 millones: tipo general", "impuesto_sociedades", {"resultado_contable": "100000", "cifra_negocios_anterior": "10000000"}, "cuota_integra", "25000.00", IS_1_NUEVA),
]  # fmt: skip


@pytest.fixture
def datos_is(tmp_path: Path) -> Path:
    carpeta = tmp_path / "data"
    carpeta.mkdir()
    raw = _leer(DATA_DIR / "sociedades_2026.json")
    raw["fuentes_principales"][LEY] = FUENTE_LEY
    raw["ejercicios_cubiertos"] = [2026]
    viejo = copy.deepcopy(IS_VALOR)
    viejo["por_ano"]["2026"]["micro_primer_tramo"] = "0.17"
    raw["tipos_gravamen"] = {
        **viejo,
        "avisos": [],
        "fuente": {"referencia": "art. 29 de la ley sintética", "en_vigor_desde": "2020-01-02", "verificado": True},
        "evidencia": [
            {
                "boe_id": LEY,
                "articulo": "art. 29.1 LIS",
                "cita": IS_1,
                "cifras": {
                    "general": "25 por ciento",
                    "limite_microempresa": "1 millón de euros",
                    "limite_primer_tramo": "50.000 euros",
                    "dias_ano": "365 días",
                    "por_ano.2026.micro_primer_tramo": "17 por ciento",
                    "por_ano.2026.micro_resto": "20 por ciento",
                },
            },
            {"boe_id": LEY, "articulo": "art. 29.1 LIS (nueva creación)", "cita": IS_2, "cifras": {"nueva_creacion": "15 por ciento"}},
            {
                "boe_id": LEY,
                "articulo": "art. 29.1 LIS (reducida dimensión)",
                "cita": IS_3,
                "cifras": {"limite_reducida_dimension": "10 millones de euros", "por_ano.2026.reducida_dimension": "24 por ciento"},
            },
            {"boe_id": LEY, "articulo": "art. 29.1 LIS (cuándo no hay actividad nueva)", "cita": IS_4},
        ],
    }  # fmt: skip
    assert comprobar_sociedades(SociedadesParams.model_validate(raw)) == []
    _escribir(carpeta / "sociedades_2026.json", raw)
    return carpeta


def test_sociedades_converge_with_cases_run_through_the_real_calculator(datos_is: Path, tmp_path: Path) -> None:
    lector = Guion(lectura(IS_VALOR, IS_CITAS, EFECTOS_IS))
    propuesta = _correr(otros.SOCIEDADES, datos_is, LEY_IS, TIPOS, lector, Guion(revision(C_IS, EFECTOS_IS)))
    assert (
        propuesta.tipo == "sociedades:TiposGravamen" and propuesta.estado == "propuesta"
    ), propuesta.objeciones_abiertas
    assert propuesta.ejercicios == [2026] and propuesta.aplicable_sin_persona
    assert [c.obtenido for c in propuesta.casos] == [D("6000.00"), D("17500.00"), D("24000.00"), D("25000.00")]
    almacen = _aplicar_y_confirmar(datos_is, tmp_path, propuesta, LEY_IS)
    raw = _leer(datos_is / "sociedades_2026.json")
    params = SociedadesParams.model_validate(raw)
    assert comprobar_sociedades(params) == [] and params.tipos_de(2026).micro_primer_tramo == D("0.15")
    # The label the calculator looks up by name survives, and "1 millón de euros" is spelled as the file reads it.
    assert params.tipos_gravamen.cita("art. 29.1 LIS (cuándo no hay actividad nueva)") == IS_4
    assert raw["tipos_gravamen"]["evidencia"][0]["cifras"]["limite_microempresa"] == "1 millón"
    entrada = sociedades.SociedadesInput(ejercicio=2026, resultado_contable=D(40000), cifra_negocios_anterior=D(500000))
    resultado = sociedades.calcular_sociedades(entrada, params)
    assert resultado.cuota_integra == D("6000.00")
    assert any("recodificación automática pendiente de revisión humana" in a for a in resultado.avisos)
    assert confirmar(pendientes_de_confirmar(datos_is), data_dir=datos_is, almacen=almacen) == 1
    params = SociedadesParams.model_validate(_leer(datos_is / "sociedades_2026.json"))
    assert not any("recodificación" in a for a in sociedades.calcular_sociedades(entrada, params).avisos)
    assert params.tipos_gravamen.fuente.referencia == "art. 29 de la ley sintética"


def test_sociedades_gate_and_reviewer(datos_is: Path) -> None:
    # A rate no quote prints.
    inventado = {**IS_VALOR, "general": "0.23"}
    rechazada = _correr(
        otros.SOCIEDADES, datos_is, LEY_IS, TIPOS, Guion(*[lectura(inventado, IS_CITAS, EFECTOS_IS)] * 3), Guion()
    )
    assert rechazada.estado == "sin_converger" and any("general = 0.23" in o for o in rechazada.objeciones_abiertas)
    # "10 millones" is printed (the art. 101 threshold) but it is not the micro-company one: only a case shows it.
    confundido = {**IS_VALOR, "limite_microempresa": "10000000"}
    lector = Guion(lectura(confundido, IS_CITAS, EFECTOS_IS), lectura(IS_VALOR, IS_CITAS, EFECTOS_IS))
    revisor = Guion(revision(C_IS, EFECTOS_IS), revision(C_IS, EFECTOS_IS))
    propuesta = _correr(otros.SOCIEDADES, datos_is, LEY_IS, TIPOS, lector, revisor)
    assert propuesta.estado == "propuesta" and len(propuesta.iteraciones) == 2
    fallido = propuesta.iteraciones[0].casos[2]
    assert fallido.coincide is False and fallido.esperado == D("24000.00") and fallido.obtenido == D("17500.00")
    # Dropping the quote the calculator shows by its label is refused, not discovered at run time.
    sin_etiqueta = _correr(
        otros.SOCIEDADES, datos_is, LEY_IS, TIPOS, Guion(*[lectura(IS_VALOR, IS_CITAS[:3], EFECTOS_IS)] * 3), Guion()
    )
    assert any("la usa la calculadora por su nombre" in o for o in sin_etiqueta.objeciones_abiertas)


def test_sociedades_wording_with_effects_mid_year_does_not_reach_the_file_of_that_year(datos_is: Path) -> None:
    """The file of a year holds what applies to periods STARTING in it: 1 July does not cover them all."""
    ley = LEY_IS.replace(EFECTOS_IS, EFECTOS_JULIO)
    lector = Guion(lectura(IS_VALOR, IS_CITAS, EFECTOS_JULIO, "2026-07-01"))
    propuesta = _correr(otros.SOCIEDADES, datos_is, ley, TIPOS, lector, Guion())
    assert propuesta.estado == "sin_converger" and propuesta.ejercicios == []
    assert "no alcanza a todos los períodos iniciados en 2026" in propuesta.ejercicios_excluidos[2026]


def test_a_group_no_calculator_evaluates_is_always_left_to_a_person(tmp_path: Path) -> None:
    carpeta = tmp_path / "data"
    carpeta.mkdir()
    raw = _leer(DATA_DIR / "sociedades_2026.json")
    raw["fuentes_principales"][LEY] = FUENTE_LEY
    vieja = "1. Los contribuyentes cuyo importe neto de la cifra de negocios sea al menos de 20 millones de euros tributarán por la cuota mínima."
    nueva = vieja.replace("20 millones", "30 millones")
    raw["tributacion_minima"] = {
        "umbral_cifra_negocios": "20000000",
        "avisos": [],
        "fuente": {"referencia": "art. 30 bis de la ley sintética", "en_vigor_desde": "2022-01-01", "verificado": True},
        "evidencia": [
            {"boe_id": LEY, "articulo": "art. 30 bis.1 LIS", "cita": vieja, "cifras": {"umbral_cifra_negocios": "20 millones de euros"}}
        ],
    }  # fmt: skip
    _escribir(carpeta / "sociedades_2026.json", raw)
    ley = pagina(30, "Tributación mínima", [nueva], EFECTOS_IS).replace("Artículo 30.", "Artículo 30 bis.")
    lector = Guion(lectura({"umbral_cifra_negocios": "30000000"}, [nueva], EFECTOS_IS))
    revisor = Guion(revision([], EFECTOS_IS))
    propuesta = _correr(otros.SOCIEDADES, carpeta, ley, ["tributacion_minima"], lector, revisor)
    assert propuesta.tipo == "sociedades:TributacionMinima" and propuesta.articulos_ley == ["30 bis"]
    assert propuesta.estado == "propuesta" and not propuesta.aplicable_sin_persona
    assert any("ninguna calculadora" in nota for nota in propuesta.control.revisar)
    assert "empty list" in revisor.llamadas[0]["system"]


# --------------------------------------------------------------------------- #
# Wealth tax: the exempt minimum of a community
# --------------------------------------------------------------------------- #

PAT_VIEJA = "En el supuesto de obligación personal, la base imponible se reducirá, en concepto de mínimo exento, en 400.000 euros."
PAT_1 = "1. En el supuesto de obligación personal, la base imponible se reducirá, en concepto de mínimo exento, en 500.000 euros."
PAT_2 = (
    "2. Para los contribuyentes con un grado de discapacidad igual o superior al 65 por 100, el mínimo exento será de "
    "1.000.000 euros."
)
LEY_PAT = pagina(7, "Mínimo exento", [PAT_1, PAT_2])
MINIMO = ["comunidades", "aragon", "minimo_exento"]
PAT_VALOR = {"importe": "500000", "discapacidad": [{"grado_desde": "0.65", "importe": "1000000"}]}
C_PAT = [
    caso("sin discapacidad", "patrimonio", {"depositos": "1200000"}, "minimo_exento", "500000", PAT_1),
    caso("base liquidable", "patrimonio", {"depositos": "1200000"}, "base_liquidable", "700000.00", PAT_1),
    caso("discapacidad del 65", "patrimonio", {"depositos": "1200000", "grado_discapacidad": "65"}, "minimo_exento", "1000000", PAT_2),
    caso("discapacidad del 64", "patrimonio", {"depositos": "1200000", "grado_discapacidad": "64"}, "minimo_exento", "500000", PAT_2),
]  # fmt: skip


@pytest.fixture
def datos_pat(tmp_path: Path) -> Path:
    carpeta = tmp_path / "data"
    carpeta.mkdir()
    raw = _leer(DATA_DIR / "patrimonio_2026.json")
    raw["fuentes_principales"][LEY] = FUENTE_LEY
    raw["comunidades"]["aragon"]["minimo_exento"] = {
        "importe": "400000",
        "discapacidad": [],
        "fuente": {"referencia": "art. 7 de la ley sintética", "en_vigor_desde": "2020-01-02", "verificado": True},
        "evidencia": [
            {"boe_id": LEY, "articulo": "art. 7 de la ley sintética", "cita": PAT_VIEJA, "cifras": {"importe": "400.000 euros"}}
        ],
    }  # fmt: skip
    assert comprobar_patrimonio(PatrimonioParams.model_validate(raw)) == []
    _escribir(carpeta / "patrimonio_2026.json", raw)
    return carpeta


def test_patrimonio_converge_apply_and_confirm(datos_pat: Path, tmp_path: Path) -> None:
    lector = Guion(lectura(PAT_VALOR, [PAT_1, PAT_2]))
    propuesta = _correr(otros.PATRIMONIO, datos_pat, LEY_PAT, MINIMO, lector, Guion(revision(C_PAT)))
    assert propuesta.tipo == "patrimonio:MinimoExento" and propuesta.ambito == "aragon"
    assert propuesta.estado == "propuesta" and propuesta.aplicable_sin_persona, propuesta.objeciones_abiertas
    assert all(c.coincide for c in propuesta.casos) and len(propuesta.casos) == 4
    almacen = _aplicar_y_confirmar(datos_pat, tmp_path, propuesta, LEY_PAT)
    params = PatrimonioParams.model_validate(_leer(datos_pat / "patrimonio_2026.json"))
    assert comprobar_patrimonio(params) == []
    entrada = patrimonio.PatrimonioInput(ejercicio=2026, comunidad="aragon", depositos=D(1200000))
    resultado = patrimonio.calcular_patrimonio(entrada, params)
    assert resultado.minimo_exento == D(500000) and resultado.base_liquidable == D(700000)
    assert any("recodificación automática pendiente de revisión humana" in a for a in resultado.avisos)
    pendientes = pendientes_de_confirmar(datos_pat)
    assert [(p.tipo, p.ruta) for p in pendientes] == [("patrimonio:MinimoExento", MINIMO)]
    assert confirmar(pendientes, data_dir=datos_pat, almacen=almacen) == 1
    params = PatrimonioParams.model_validate(_leer(datos_pat / "patrimonio_2026.json"))
    assert not any("recodificación" in a for a in patrimonio.calcular_patrimonio(entrada, params).avisos)


def test_patrimonio_gate_reviewer_and_years(datos_pat: Path) -> None:
    inventado = {"importe": "600000", "discapacidad": []}
    rechazada = _correr(
        otros.PATRIMONIO, datos_pat, LEY_PAT, MINIMO, Guion(*[lectura(inventado, [PAT_1, PAT_2])] * 3), Guion()
    )
    assert rechazada.estado == "sin_converger" and any("importe = 600000" in o for o in rechazada.objeciones_abiertas)
    # Both amounts are printed, so the gate accepts them swapped; the calculator does not.
    cruzado = {"importe": "1000000", "discapacidad": [{"grado_desde": "0.65", "importe": "500000"}]}
    lector = Guion(lectura(cruzado, [PAT_1, PAT_2]), lectura(PAT_VALOR, [PAT_1, PAT_2]))
    propuesta = _correr(otros.PATRIMONIO, datos_pat, LEY_PAT, MINIMO, lector, Guion(revision(C_PAT), revision(C_PAT)))
    assert propuesta.estado == "propuesta" and len(propuesta.iteraciones) == 2
    assert [c.coincide for c in propuesta.iteraciones[0].casos] == [False, False, False, False]
    assert propuesta.iteraciones[0].casos[0].obtenido == D("1000000.00")
    # The tax accrues on 31 December: a wording with effects from 2027 does not reach the 2026 file.
    tarde = LEY_PAT.replace(EFECTOS_2026, EFECTOS_2027)
    futura = _correr(
        otros.PATRIMONIO,
        datos_pat,
        tarde,
        MINIMO,
        Guion(lectura(PAT_VALOR, [PAT_1, PAT_2], EFECTOS_2027, "2027-01-01")),
        Guion(),
    )
    assert futura.estado == "sin_converger" and 2026 in futura.ejercicios_excluidos


def test_patrimonio_scale_that_does_not_add_up_is_refused_by_the_files_own_check(tmp_path: Path) -> None:
    carpeta = tmp_path / "data"
    carpeta.mkdir()
    raw = _leer(DATA_DIR / "patrimonio_2026.json")
    _escribir(carpeta / "patrimonio_2026.json", raw)
    ruta = ["comunidades", "asturias", "escala"]
    objetivo = otros.objetivo_de_ruta("patrimonio", None, ruta, data_dir=carpeta)
    adaptador = adaptador_de(objetivo.tipo)
    valor = adaptador._sin_procedencia(objetivo.antes[2026])  # type: ignore[attr-defined]
    assert set(valor) == {"tramos"}
    modelo, control = adaptador.controlar(valor, objetivo.citas_antes, objetivo, efectos_desde=None, datos={2026: raw})
    assert modelo is not None and control.ok and not control.revisar
    # A case through the real calculator: 700.000 of exempt minimum (the State's) and Asturias's own scale.
    cuota, _ = adaptador.evaluar_caso(
        modelo, {"entrada": {"depositos": "1000000"}, "resultado": "cuota_integra"}, objetivo
    )
    segundo = valor["tramos"][1]
    exceso = (D(300000) - D(valor["tramos"][0]["hasta"])) * D(segundo["tipo"])
    assert cuota == (D(segundo["cuota_hasta_inicio"]) + exceso).quantize(D("0.01"))
    with pytest.raises(ValueError, match="no es una línea del resultado"):
        adaptador.evaluar_caso(modelo, {"entrada": {"depositos": "1"}, "resultado": "cuota_inventada"}, objetivo)
    with pytest.raises(ValueError, match="depositoss"):
        adaptador.evaluar_caso(modelo, {"entrada": {"depositoss": "1"}, "resultado": "cuota_integra"}, objetivo)
    # The width of a bracket taken for its upper end: every cell is printed, but the table no longer adds up.
    torcido = copy.deepcopy(valor)
    torcido["tramos"][1]["hasta"] = "167123.43"
    _, peor = adaptador.controlar(torcido, objetivo.citas_antes, objetivo, efectos_desde=None, datos={2026: raw})
    assert not peor.ok and any("autocontrol del fichero" in f or "modelo" in f for f in peor.fallos)


# --------------------------------------------------------------------------- #
# Inheritance tax: an allowance of a community
# --------------------------------------------------------------------------- #

ISD_VIEJA = (
    "Los sujetos pasivos incluidos en los grupos I y II aplicarán una bonificación del 99 por ciento en la cuota "
    "tributaria derivada de adquisiciones mortis causa."
)
ISD_NUEVA = (
    "Los sujetos pasivos incluidos en los grupos I y II aplicarán una bonificación del 95 por ciento en la cuota "
    "tributaria derivada de adquisiciones mortis causa, siempre que su base imponible no supere 300.000 euros."
)
LEY_ISD = pagina(25, "Bonificaciones de la cuota", [ISD_NUEVA], EFECTOS_JULIO)
BONIFICACION = ["comunidades", "madrid", "bonificaciones", "0"]
REGLA_VIEJA = {
    "titulo": "Bonificación para cónyuge, ascendientes y descendientes (herencias)",
    "articulo": "art. 25 de la ley sintética",
    "resumen": "Bonificación del 99 por ciento de la cuota en herencias para los grupos I y II.",
    "en_vigor_desde": "2020-01-02",
    "cuando": {"modo": "sucesion", "grupos": ["I", "II"]},
    "porcentaje": "0.99",
    "evidencia": [{"boe_id": LEY, "articulo": "art. 25 de la ley sintética", "cita": ISD_VIEJA}],
}
ISD_VALOR = {
    "titulo": "Bonificación para cónyuge, ascendientes y descendientes (herencias)",
    "articulo": "art. 25 de la ley sintética",
    "resumen": "Bonificación del 95 por ciento de la cuota en herencias para los grupos I y II con base imponible que no supere 300.000 euros.",
    "en_vigor_desde": "1999-01-01",
    "cuando": {"modo": "sucesion", "grupos": ["I", "II"], "base_imponible_hasta": "300000"},
    "porcentaje": "0.95",
}  # fmt: skip
HIJO = {"modo": "sucesion", "parentesco": "hijo", "edad": 40}


@pytest.fixture
def datos_isd(tmp_path: Path) -> Path:
    carpeta = tmp_path / "data"
    carpeta.mkdir()
    raw = _leer(DATA_DIR / "isd_2026.json")
    raw["fuentes_principales"][LEY] = FUENTE_LEY
    raw["comunidades"]["madrid"]["bonificaciones"] = [copy.deepcopy(REGLA_VIEJA)]
    raw["comunidades"]["madrid"]["bonificaciones_sucesivas"] = []
    assert comprobar_isd(ISDParams.model_validate(raw)) == []
    _escribir(carpeta / "isd_2026.json", raw)
    return carpeta


def _isd(datos: Path, valor_recibido: str, regla: dict[str, Any] | None = None) -> sucesiones.ResultadoISD:
    raw = _leer(datos / "isd_2026.json")
    if regla is not None:
        raw["comunidades"]["madrid"]["bonificaciones"] = [
            {**regla, "evidencia": [{"boe_id": LEY, "articulo": "a", "cita": ISD_NUEVA}]}
        ]
    entrada = sucesiones.BeneficiarioISD(ejercicio=2026, comunidad="madrid", valor_recibido=D(valor_recibido), **HIJO)
    return sucesiones.calcular_isd(entrada, ISDParams.model_validate(raw))


def _casos_isd(datos: Path) -> list[dict]:
    """What THE LAW gives: computed with the right encoding, never with the proposal under test."""
    en_el_limite = _isd(datos, "300000", ISD_VALOR)
    assert en_el_limite.base_imponible == D(300000) and en_el_limite.bonificacion > 0
    assert en_el_limite.bonificacion == (en_el_limite.cuota_tributaria * D("0.95")).quantize(D("0.01"))
    return [
        caso("hijo, base imponible justo en el límite", "isd", {**HIJO, "valor_recibido": "300000"}, "bonificacion", str(en_el_limite.bonificacion), ISD_NUEVA),
        caso("un céntimo por encima: sin bonificación", "isd", {**HIJO, "valor_recibido": "300000.01"}, "bonificacion", "0", ISD_NUEVA),
        caso("hermano (grupo III): sin bonificación", "isd", {"modo": "sucesion", "parentesco": "hermano", "valor_recibido": "100000"}, "bonificacion", "0", ISD_NUEVA),
    ]  # fmt: skip


def test_isd_converge_apply_and_confirm(datos_isd: Path, tmp_path: Path) -> None:
    objetivo = _objetivo(otros.ISD, datos_isd, LEY_ISD, BONIFICACION)
    assert objetivo.tipo == "isd:ReglaBonificacion" and objetivo.ambito == "madrid" and objetivo.articulos_ley == ["25"]
    casos = _casos_isd(datos_isd)
    lector = Guion(lectura(ISD_VALOR, [ISD_NUEVA], EFECTOS_JULIO, "2026-07-01"))
    revisor = Guion(revision(casos, EFECTOS_JULIO, "2026-07-01"))
    propuesta = _correr(otros.ISD, datos_isd, LEY_ISD, BONIFICACION, lector, revisor)
    assert propuesta.estado == "propuesta" and propuesta.ejercicios == [2026], propuesta.objeciones_abiertas
    assert [c.coincide for c in propuesta.casos] == [True, True, True]
    # The tax accrues on the day of the death: a wording with effects from 1 July applies to part of the year, so the
    # proposal is dated by that day and a person decides.
    assert propuesta.despues["en_vigor_desde"] == "2026-07-01" and not propuesta.aplicable_sin_persona
    assert any("a mitad del ejercicio 2026" in nota for nota in propuesta.control.revisar)
    almacen = AlmacenPropuestas(tmp_path / "propuestas")
    aplicar_propuesta(propuesta, texto_de(LEY_ISD), almacen=almacen, data_dir=datos_isd, hoy=HOY)
    regla = _leer(datos_isd / "isd_2026.json")["comunidades"]["madrid"]["bonificaciones"][0]
    assert regla["recodificada_el"] == "2026-11-12" and regla["porcentaje"] == "0.95"
    assert regla["evidencia"] == [{"boe_id": LEY, "articulo": "art. 25 de la ley sintética", "cita": ISD_NUEVA}]
    assert comprobar_isd(ISDParams.model_validate(_leer(datos_isd / "isd_2026.json"))) == []
    resultado = _isd(datos_isd, "200000")
    assert resultado.bonificacion > 0
    assert any(
        "recodificada automáticamente el 2026-11-12" in a and "pendiente de revisión humana" in a
        for a in resultado.avisos
    )
    # A beneficiary the rule is not applied to is not told about it.
    assert not any("recodificada automáticamente" in a for a in _isd(datos_isd, "400000").avisos)
    pendientes = pendientes_de_confirmar(datos_isd)
    assert [(p.tipo, p.ruta) for p in pendientes] == [("isd:ReglaBonificacion", BONIFICACION)]
    assert confirmar(pendientes, data_dir=datos_isd, almacen=almacen) == 1
    assert "recodificada_el" not in _leer(datos_isd / "isd_2026.json")["comunidades"]["madrid"]["bonificaciones"][0]
    assert not any("recodificada" in a for a in _isd(datos_isd, "200000").avisos)


def test_isd_gate_reviewer_and_years(datos_isd: Path) -> None:
    casos = _casos_isd(datos_isd)
    inventado = {**ISD_VALOR, "porcentaje": "0.97"}
    lector = Guion(*[lectura(inventado, [ISD_NUEVA], EFECTOS_JULIO, "2026-07-01")] * 3)
    rechazada = _correr(otros.ISD, datos_isd, LEY_ISD, BONIFICACION, lector, Guion())
    assert rechazada.estado == "sin_converger" and any("porcentaje = 0.97" in o for o in rechazada.objeciones_abiertas)
    # A summary that tells the user a figure the quotes do not print.
    hablador = {**ISD_VALOR, "resumen": "Bonificación del 95 por ciento, con un máximo de 50.000 euros."}
    lector = Guion(*[lectura(hablador, [ISD_NUEVA], EFECTOS_JULIO, "2026-07-01")] * 3)
    con_resumen = _correr(otros.ISD, datos_isd, LEY_ISD, BONIFICACION, lector, Guion())
    assert any("menciona 50.000" in o for o in con_resumen.objeciones_abiertas)
    # "no supere" is inclusive. Encoded as exclusive it passes the gate (the figure is printed) and fails AT the threshold.
    exclusivo = copy.deepcopy(ISD_VALOR)
    exclusivo["cuando"] = {"modo": "sucesion", "grupos": ["I", "II"], "base_imponible_menor_de": "300000"}
    lector = Guion(
        lectura(exclusivo, [ISD_NUEVA], EFECTOS_JULIO, "2026-07-01"),
        lectura(ISD_VALOR, [ISD_NUEVA], EFECTOS_JULIO, "2026-07-01"),
    )
    revisor = Guion(revision(casos, EFECTOS_JULIO, "2026-07-01"), revision(casos, EFECTOS_JULIO, "2026-07-01"))
    propuesta = _correr(otros.ISD, datos_isd, LEY_ISD, BONIFICACION, lector, revisor)
    assert propuesta.estado == "propuesta" and len(propuesta.iteraciones) == 2
    primera = propuesta.iteraciones[0].casos
    assert [c.coincide for c in primera] == [False, True, True] and primera[0].obtenido == D("0.00")
    # `articulo` identifies the rule: it cannot be renamed by a proposal.
    renombrado = {**ISD_VALOR, "articulo": "art. 26"}
    lector = Guion(*[lectura(renombrado, [ISD_NUEVA], EFECTOS_JULIO, "2026-07-01")] * 3)
    assert any("«articulo» debe seguir siendo" in o for o in _correr(otros.ISD, datos_isd, LEY_ISD, BONIFICACION, lector, Guion()).objeciones_abiertas)  # fmt: skip
    # Effects from 2027: nothing of 2026 is touched.
    tarde = LEY_ISD.replace(EFECTOS_JULIO, EFECTOS_2027)
    futura = _correr(otros.ISD, datos_isd, tarde, BONIFICACION, Guion(lectura(ISD_VALOR, [ISD_NUEVA], EFECTOS_2027, "2027-01-01")), Guion())  # fmt: skip
    assert futura.estado == "sin_converger" and 2026 in futura.ejercicios_excluidos


# --------------------------------------------------------------------------- #
# The calculators say which unverified groups a result relied on
# --------------------------------------------------------------------------- #


def test_calculators_surface_groups_that_were_silent() -> None:
    raw = _leer(DATA_DIR / "patrimonio_2026.json")
    raw["itsgf"]["hecho_imponible"]["fuente"].update(verificado=False, referencia="umbral ITSGF sin contrastar")
    entrada = patrimonio.PatrimonioInput(ejercicio=2026, comunidad="madrid", depositos=D(1000000))
    resultado = patrimonio.calcular_patrimonio(entrada, PatrimonioParams.model_validate(raw))
    assert "umbral ITSGF sin contrastar" in resultado.parametros_no_verificados
    raw_is = _leer(DATA_DIR / "sociedades_2026.json")
    raw_is["reserva_nivelacion"]["fuente"].update(verificado=False, referencia="art. 105 sin contrastar")
    empresa = sociedades.SociedadesInput(
        ejercicio=2026, resultado_contable=D(100000), cifra_negocios_anterior=D(5000000)
    )
    resultado_is = sociedades.calcular_sociedades(empresa, SociedadesParams.model_validate(raw_is))
    assert "art. 105 sin contrastar" in resultado_is.parametros_no_verificados
    # ISD: the rules that are only explained (never "applied") are covered too.
    raw_isd = _leer(DATA_DIR / "isd_2026.json")
    raw_isd["estatal"]["acumulacion"]["recodificada_el"] = "2026-11-12"
    heredero = sucesiones.BeneficiarioISD(ejercicio=2026, valor_recibido=D(50000), **HIJO)
    avisos = sucesiones.calcular_isd(heredero, ISDParams.model_validate(raw_isd)).avisos
    assert any("«Acumulación»" in a and "recodificada automáticamente el 2026-11-12" in a for a in avisos)
    assert not any("recodificada" in a for a in sucesiones.calcular_isd(heredero).avisos)
