"""The re-encoding loop: a scripted model proposes, the deterministic gate and the reviewer's cases decide.

No network and no real model: the "law" is a small synthetic BOE page and the providers replay a script. The
parameter files are temporary copies of the shipped ones with one community rewritten to depend on that law.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import re
import shutil
from typing import Any

from click.testing import CliRunner
import pytest

from hacienda_gpt.agent.providers.base import ProviderError
from hacienda_gpt.cli import proactive as cli_module
from hacienda_gpt.ops.consumo import PresupuestoAgotadoError
from hacienda_gpt.tax import autonomicas
from hacienda_gpt.tax.irpf import IRPFInput
from hacienda_gpt.tax.params import DATA_DIR, DeduccionCalculable, IRPFParams, load_params
from hacienda_gpt.tax.recodificacion import (
    AlmacenPropuestas,
    ControlFallidoError,
    DatosCambiadosError,
    LeyCambiadaError,
    Propuesta,
    RecodificacionError,
    SinTextoError,
    adaptadores,
    aplicar_propuesta,
    avisar_operador,
    confirmar,
    control,
    ejecutar,
    objetivo_de_ruta,
    objetivos_de_articulo,
    objetivos_de_rollover,
    objetivos_de_verificacion,
    pendientes_de_confirmar,
    prompts,
    rechazar_propuesta,
    recodificar,
)
from hacienda_gpt.tax.recodificacion.adaptadores import DEDUCCIONES, ESCALAS, REGLAS_DEDUCCION, REGLAS_ESCALA

D = Decimal
LEY = "BOE-T-2020-1"
HOY = date(2026, 11, 12)

# --------------------------------------------------------------------------- #
# A synthetic law, in the BOE's markup
# --------------------------------------------------------------------------- #

P1 = (
    "1. Los contribuyentes menores de 36 años podrán deducir el 15 por ciento de las cantidades satisfechas por el "
    "alquiler de su vivienda habitual, con un máximo de 600 euros."
)
P2 = "2. Será requisito que la base imponible total no sea superior a 25.000 euros en tributación individual."
P3 = "3. El contrato de arrendamiento deberá estar depositado en el registro autonómico."
EFECTOS_2026 = "Se modifica, con efectos desde el 1 de enero de 2026, por el art. 1 de la Ley 1/2026, de 20 de junio."
EFECTOS_2025 = "Se modifica, con efectos desde el 1 de enero de 2025, por el art. 1 de la Ley 1/2026, de 20 de junio."
COMUNES = "1. Las deducciones de este capítulo exigen que la base del ahorro no supere 1.800 euros."
FILAS = (
    "0,00 0,00 10.000,00 10,00 %",
    "10.000,00 1.000,00 20.000,00 15,00 %",
    "30.000,00 4.000,00 En adelante 20,00 %",
)


def _selector(n: int) -> str:
    return (
        f'<form class="lista formBOE" method="GET" action="/buscar/act.php#a{n}"><fieldset>'
        '<legend class="fuera">Seleccionar redacción:</legend>'
        '<p><input type="radio" name="p" value="20260630" checked="checked"/>'
        "<label>Última actualización, publicada el 30/06/2026, en vigor a partir del 01/07/2026.</label></p>"
        '<p><input type="radio" name="p" value="20200101"/>'
        "<label>Texto original, publicado el 01/01/2020, en vigor a partir del 02/01/2020.</label></p>"
        "</fieldset></form>"
    )


def ley(*, parrafo1: str = P1, efectos: str = EFECTOS_2026, filas: tuple[str, ...] = FILAS) -> str:
    tabla = "".join("<tr>" + "".join(f"<td>{celda}</td>" for celda in fila.split(" ", 3)) + "</tr>" for fila in filas)
    # The last row's "En adelante" has a space: keep the four columns as the BOE prints them.
    tabla = tabla.replace("<td>En</td><td>adelante 20,00 %</td>", "<td>En adelante</td><td>20,00 %</td>")
    return f"""<html><head><style>p {{}}</style></head><body>
<div class="bloque" id="a4"><p class="bloque">[Bloque 4: #a4]</p>
<h5 class="articulo">Artículo 4. Escala autonómica.</h5>
<p class="parrafo">La escala autonómica aplicable a la base liquidable general será la siguiente:</p>
<table><tr><th>Base liquidable</th><th>Cuota íntegra</th><th>Resto base liquidable</th><th>Tipo aplicable</th></tr>
{tabla}</table>
<p class="nota_pie">{efectos}</p>{_selector(4)}
<p class="linkSubir"><a href="#top">Subir</a></p></div>
<div class="bloque" id="a5"><p class="bloque">[Bloque 5: #a5]</p>
<h5 class="articulo">Artículo 5. Deducción por alquiler de vivienda habitual.</h5>
<p class="parrafo">{parrafo1}</p>
<p class="parrafo">{P2}</p>
<p class="parrafo">{P3}</p>
<p class="nota_pie">{efectos}</p>{_selector(5)}</div>
<div class="bloque" id="a13"><p class="bloque">[Bloque 13: #a13]</p>
<h5 class="articulo">Artículo 13. Normas comunes de las deducciones.</h5>
<p class="parrafo">{COMUNES}</p>{_selector(13)}</div>
<div class="bloque" id="dfunica"><p class="bloque">[Bloque 14: #dfunica]</p>
<h5 class="articulo">Disposición final única. Entrada en vigor.</h5><p>Esta ley entra en vigor al día siguiente.</p></div>
</body></html>"""


def texto_de_ley(pagina: str | None = None) -> Callable[..., str | None]:
    contenido = ley() if pagina is None else pagina
    return lambda boe_id, version=None: contenido if boe_id == LEY and version is None else None


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
CITAS_ANTIGUAS = [
    "1. Los contribuyentes menores de 36 años podrán deducir el 10 por ciento de las cantidades satisfechas por el "
    "alquiler de su vivienda habitual, con un máximo de 500 euros.",
    "2. Será requisito que la base imponible total no sea superior a 22.000 euros en tributación individual.",
]
NUEVA = {
    "articulo": "art. 5",
    "titulo": "Deducción por alquiler de vivienda habitual",
    "resumen": "El 15 por ciento del alquiler de la vivienda habitual, con un máximo de 600 euros, para menores de "
    "36 años con base imponible total no superior a 25.000 euros.",
    "tipo": "porcentaje",
    "porcentaje": "0.15",
    "base": "alquiler_vivienda_habitual",
    "maximo": "600",
    "edad_max": 35,
    "limite_individual": "25000",
    "requisitos_no_comprobados": ["El contrato de arrendamiento debe estar depositado en el registro autonómico."],
    "en_vigor_desde": "1999-01-01",
}
ESCALA_NUEVA = {
    "tramos": [
        {"hasta": "10000", "tipo": "0.10", "cuota_hasta_inicio": "0"},
        {"hasta": "30000", "tipo": "0.15", "cuota_hasta_inicio": "1000"},
        {"hasta": None, "tipo": "0.20", "cuota_hasta_inicio": "4000"},
    ]
}


@pytest.fixture
def datos(tmp_path: Path) -> Path:
    """Temporary parameter files: the shipped ones, with Galicia rewritten to hang from the synthetic law."""
    carpeta = tmp_path / "data"
    carpeta.mkdir()
    for ejercicio in (2025, 2026):
        raw = json.loads((DATA_DIR / f"irpf_{ejercicio}.json").read_text(encoding="utf-8"))
        galicia = raw["comunidades"]["galicia"]
        galicia["fuente_principal"]["boe_id"] = LEY
        galicia["deducciones_calculables"] = [dict(ANTIGUA)]
        galicia["minimos"] = None
        galicia["escala_general"] = {
            "tramos": [
                {"hasta": "12000", "tipo": "0.09", "cuota_hasta_inicio": "0"},
                {"hasta": None, "tipo": "0.19", "cuota_hasta_inicio": "1080"},
            ],
            "fuente": {
                "referencia": "art. 4 de la ley sintética (redacción original)",
                "en_vigor_desde": "2020-01-02",
                "verificado": True,
            },
        }
        IRPFParams.model_validate(raw)
        (carpeta / f"irpf_{ejercicio}.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", "utf-8")
    evidencia = json.loads((DATA_DIR / "evidencia_deducciones.json").read_text(encoding="utf-8"))
    evidencia["galicia"] = {"art. 5": list(CITAS_ANTIGUAS)}
    (carpeta / "evidencia_deducciones.json").write_text(json.dumps(evidencia, ensure_ascii=False, indent=2), "utf-8")
    return carpeta


def _raw(datos: Path, ejercicio: int) -> dict[str, Any]:
    return json.loads((datos / f"irpf_{ejercicio}.json").read_text(encoding="utf-8"))


def _deduccion(datos: Path, ejercicio: int) -> dict[str, Any]:
    return _raw(datos, ejercicio)["comunidades"]["galicia"]["deducciones_calculables"][0]


# --------------------------------------------------------------------------- #
# A scripted provider
# --------------------------------------------------------------------------- #


class Guion:
    """Replays answers in order; an exception in the script is raised instead of returned."""

    name = "guion"

    def __init__(self, *respuestas: Any, model: str = "modelo-de-prueba") -> None:
        self.model = model
        self.respuestas = list(respuestas)
        self.llamadas: list[dict[str, Any]] = []

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str, attachments: Any = None
    ) -> dict[str, Any]:
        self.llamadas.append({"system": system, "user": user, "schema": schema, "schema_name": schema_name})
        if not self.respuestas:
            raise AssertionError(f"llamada no prevista en el guion ({schema_name})")
        respuesta = self.respuestas.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return dict(respuesta)


def lectura(valor: dict[str, Any] | None = None, **cambios: Any) -> dict[str, Any]:
    base = {
        "resultado": "codificada",
        "valor_json": json.dumps(NUEVA if valor is None else valor, ensure_ascii=False),
        "evidencia": [P1, P2],
        "efectos_desde": "2026-01-01",
        "efectos_hasta": "",
        "cita_efectos": EFECTOS_2026,
        "articulos_necesarios": [],
        "motivo": "",
        "cambios": "Sube el porcentaje al 15 por ciento, el máximo a 600 euros y el límite a 25.000 euros.",
    }
    return {**base, **cambios}


def caso(descripcion: str, contribuyente: dict[str, Any], esperado: str, *, ingreso: str = "20000", cita: str = P1):
    return {
        "descripcion": descripcion,
        "datos_json": json.dumps({"contribuyente": contribuyente, "ingreso": ingreso}),
        "importe_esperado": esperado,
        "fundamento": cita,
    }


CASOS = [
    caso("35 años, 2.000 € de alquiler", {"edad": 35, "alquiler_vivienda_habitual": "2000"}, "300.00"),
    caso("36 años cumplidos: ya no es menor de 36", {"edad": 36, "alquiler_vivienda_habitual": "2000"}, "0"),
    caso("alquiler alto: tope", {"edad": 30, "alquiler_vivienda_habitual": "9000"}, "600"),
    caso("justo en el límite", {"edad": 30, "alquiler_vivienda_habitual": "1000"}, "150", ingreso="25000", cita=P2),
    caso("un céntimo por encima", {"edad": 30, "alquiler_vivienda_habitual": "1000"}, "0", ingreso="25000.01", cita=P2),
]


def revision(**cambios: Any) -> dict[str, Any]:
    base = {
        "conforme": True,
        "discrepancias": [],
        "efectos_desde": "2026-01-01",
        "cita_efectos": EFECTOS_2026,
        "base_limite_segun_ley": "base_imponible_total",
        "casos": CASOS,
    }
    return {**base, **cambios}


def _objetivo(datos: Path, pagina: str | None = None, ejercicios: tuple[int, ...] = (2025, 2026)):
    hallazgo = objetivos_de_verificacion(
        list(ejercicios), texto_de_ley(pagina), data_dir=datos, ambito="galicia", tipos=["deduccion_autonomica"]
    )
    assert len(hallazgo.objetivos) == 1 and not hallazgo.errores
    return hallazgo.objetivos[0]


def _correr(datos: Path, lector: Guion, revisor: Guion, pagina: str | None = None, **kw: Any) -> Propuesta:
    objetivo = _objetivo(datos, pagina)
    ficheros = {e: _raw(datos, e) for e in (2025, 2026)}
    return recodificar(objetivo, lector=lector, revisor=revisor, texto_de=texto_de_ley(pagina), datos=ficheros, **kw)


# --------------------------------------------------------------------------- #
# Finding what to re-read
# --------------------------------------------------------------------------- #


def test_failing_checks_become_targets_with_every_year_and_their_previous_quotes(datos: Path) -> None:
    hallazgo = objetivos_de_verificacion([2025, 2026], texto_de_ley(), data_dir=datos, ambito="galicia")
    por_tipo = {o.tipo: o for o in hallazgo.objetivos}
    assert set(por_tipo) == {"deduccion_autonomica", "escala"}
    deduccion = por_tipo["deduccion_autonomica"]
    assert deduccion.ruta == ["comunidades", "galicia", "deducciones_calculables", "articulo=art. 5"]
    assert deduccion.ejercicios == [2025, 2026] and deduccion.antes[2026] == ANTIGUA
    assert deduccion.boe_id == LEY and deduccion.version is None and deduccion.articulos_ley == ["5"]
    assert deduccion.citas_antes == CITAS_ANTIGUAS and any("10 por ciento" in m for m in deduccion.motivo)
    assert por_tipo["escala"].ruta == ["comunidades", "galicia", "escala_general"]
    assert por_tipo["escala"].articulos_ley == ["4"]
    # Nothing to do when the law still says what the data says.
    vieja = ley(parrafo1=CITAS_ANTIGUAS[0]).replace(P2, CITAS_ANTIGUAS[1])
    sin_cambios = objetivos_de_verificacion(
        [2026], texto_de_ley(vieja), data_dir=datos, ambito="galicia", tipos=["deduccion_autonomica"]
    )
    assert sin_cambios.objetivos == []


def test_every_failing_group_of_the_irpf_file_has_an_adapter(datos: Path) -> None:
    """A law that prints nothing makes every check fail: each failing group becomes a target, none is left over."""
    nada = objetivos_de_verificacion([2026], lambda boe_id, version=None: "<p>nada</p>", data_dir=datos, solo_irpf=True)
    assert nada.sin_adaptador == [] and nada.errores == []
    tipos = {o.tipo for o in nada.objetivos}
    assert {"deduccion_autonomica", "escala", "cifras:minimos", "cifras:trabajo", "cifras:minimos_autonomicos"} <= tipos
    # The block of Madrid's hand-coded deductions fails as a whole and is taken on sub-group by sub-group.
    assert {t for t in tipos if t.startswith("cifras:madrid_")} == {
        "cifras:madrid_nacimiento",
        "cifras:madrid_alquiler",
        "cifras:madrid_gastos_educativos",
        "cifras:madrid_cuidado",
        "cifras:madrid_donativos",
        "cifras:madrid_familias_ingresos_reducidos",
    }
    # "cifras" names the whole family of kinds.
    solo_cifras = objetivos_de_verificacion(
        [2026], lambda boe_id, version=None: "<p>nada</p>", data_dir=datos, tipos=["cifras"], solo_irpf=True
    )
    assert solo_cifras.objetivos and all(o.tipo.startswith("cifras:") for o in solo_cifras.objetivos)


def test_an_article_can_be_asked_for_even_if_its_check_passes_and_a_missing_one_is_new(datos: Path) -> None:
    todas = objetivos_de_articulo([2026], comunidad="galicia", articulo="art. 5", data_dir=datos)
    assert [o.ruta[-1] for o in todas.objetivos] == ["articulo=art. 5"] and todas.objetivos[0].origen == "articulo"
    nueva = objetivos_de_articulo([2026], comunidad="galicia", articulo="art. 13", data_dir=datos).objetivos[0]
    assert nueva.antes == {2026: None} and nueva.articulos_ley == ["13"]
    assert nueva.motivo == ["deducción nueva: no estaba codificada"]
    # A new article the reader finds not computable is not a "withdrawal": there is nothing to withdraw.
    rendido = Guion(lectura(resultado="no_calculable", valor_json="", evidencia=[], motivo="Solo fija normas comunes."))
    nada = recodificar(nueva, lector=rendido, revisor=Guion(), texto_de=texto_de_ley())
    assert nada.estado == "sin_converger" and nada.objeciones_abiertas == ["No calculable: Solo fija normas comunes."]
    with pytest.raises(RecodificacionError, match="Ningún adaptador"):
        objetivo_de_ruta([2026], ["comunidades", "galicia", "notas"], data_dir=datos)
    # A group the fixture's Galicia does not have cannot be located in the law: said, not guessed.
    with pytest.raises(RecodificacionError, match="ningún fichero contiene este grupo"):
        objetivo_de_ruta([2026], ["comunidades", "galicia", "minimos"], data_dir=datos)


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


def test_converges_in_one_pass(datos: Path) -> None:
    lector, revisor = Guion(lectura()), Guion(revision())
    propuesta = _correr(datos, lector, revisor)

    assert propuesta.estado == "propuesta" and propuesta.resultado == "recodificada"
    assert len(propuesta.iteraciones) == 1 and propuesta.llamadas_llm == 2
    assert propuesta.control is not None and propuesta.control.ok and propuesta.control.revisar == []
    assert propuesta.veredicto is not None and propuesta.veredicto.conforme
    assert [c.coincide for c in propuesta.casos] == [True] * 5
    assert [str(c.obtenido) for c in propuesta.casos] == ["300.00", "0.00", "600.00", "150.00", "0.00"]
    # Dated by the quote-backed effects date, not by what the model wrote in the field.
    assert propuesta.despues["en_vigor_desde"] == "2026-01-01" and propuesta.despues["porcentaje"] == "0.15"
    assert "recodificada_el" not in propuesta.despues
    assert propuesta.antes[2026] == ANTIGUA and propuesta.citas == [P1, P2]
    assert propuesta.modelo_lector == "guion/modelo-de-prueba" and propuesta.aplicable_sin_persona
    assert propuesta.sha256_texto and propuesta.sha256_html and propuesta.articulos_ley == ["5"]

    # The reader is shown the article, not the law; the reviewer neither the reader's reasoning nor its effects.
    leido = lector.llamadas[0]["user"]
    assert P1 in leido and FILAS[0] not in leido and "Artículo 13. Normas comunes" in leido  # 13 only in the index
    assert CITAS_ANTIGUAS[0] in leido and "Redacción mostrada por el BOE: Última actualización" in leido
    assert "Texto original, publicado" not in leido  # other wordings of the selector are not the text
    revisado = revisor.llamadas[0]["user"]
    assert P1 in revisado and '"porcentaje": "0.15"' in revisado
    assert "Sube el porcentaje" not in revisado and CITAS_ANTIGUAS[0] not in revisado
    assert '"efectos_desde"' not in revisado
    assert "class DeduccionCalculable" in lector.llamadas[0]["system"]  # the schema is the code itself
    assert "base_limite_segun_ley" in revisor.llamadas[0]["schema"]["properties"]


def test_effects_decide_the_years_a_proposal_touches(datos: Path) -> None:
    solo_2026 = _correr(datos, Guion(lectura()), Guion(revision()))
    assert solo_2026.ejercicios == [2026] and solo_2026.ejercicios_pedidos == [2025, 2026]
    assert "produce efectos desde 2026-01-01" in solo_2026.ejercicios_excluidos[2025]

    # The same law published in 2026 but with effects from 1 January 2025 reaches the previous year's file too.
    retroactiva = ley(efectos=EFECTOS_2025)
    propuesta = _correr(
        datos,
        Guion(lectura(efectos_desde="2025-01-01", cita_efectos=EFECTOS_2025)),
        Guion(revision(efectos_desde="2025-01-01", cita_efectos=EFECTOS_2025)),
        pagina=retroactiva,
    )
    assert propuesta.estado == "propuesta" and propuesta.ejercicios == [2025, 2026]
    assert propuesta.ejercicios_excluidos == {} and propuesta.despues["en_vigor_desde"] == "2025-01-01"


def test_the_entry_into_force_alone_is_flagged_when_the_text_has_an_effects_clause(datos: Path) -> None:
    vigor = "Redacción mostrada por el BOE: Última actualización, publicada el 30/06/2026, en vigor a partir del 01/07/2026."
    propuesta = _correr(
        datos,
        Guion(lectura(efectos_desde="2026-07-01", cita_efectos=vigor)),
        Guion(revision(efectos_desde="2026-07-01", cita_efectos=vigor)),
    )
    assert propuesta.estado == "propuesta" and propuesta.control is not None
    assert any("cláusulas de efectos" in nota for nota in propuesta.control.revisar)
    assert any("a mitad del ejercicio 2026" in nota for nota in propuesta.control.revisar)
    assert not propuesta.aplicable_sin_persona


def test_reader_and_reviewer_must_agree_on_the_effects_date(datos: Path) -> None:
    propuesta = _correr(datos, Guion(lectura()), Guion(revision(efectos_desde="2025-01-01")), max_iter=1)
    assert propuesta.estado == "sin_converger" and propuesta.ejercicios == []
    assert any(o.startswith("Efectos:") for o in propuesta.objeciones_abiertas)


def test_a_reviewer_case_that_fails_goes_back_to_the_reader_and_the_second_pass_fixes_it(datos: Path) -> None:
    con_36 = {**NUEVA, "edad_max": 36}  # "menores de 36" read as inclusive
    lector, revisor = Guion(lectura(con_36), lectura()), Guion(revision(), revision())
    propuesta = _correr(datos, lector, revisor)

    assert propuesta.estado == "propuesta" and len(propuesta.iteraciones) == 2
    primera = propuesta.iteraciones[0]
    assert primera.control is not None and primera.control.ok  # 36 IS in the quote: only the boundary case catches it
    assert [c.coincide for c in primera.casos] == [True, False, True, True, True]
    assert len(primera.objeciones) == 1 and "36 años cumplidos" in primera.objeciones[0]
    assert "corresponden 0.00 €" in primera.objeciones[0] and "da 300.00 €" in primera.objeciones[0]
    segunda = lector.llamadas[1]["user"]
    assert "<objeciones>" in segunda and "36 años cumplidos" in segunda and '"edad_max": 36' in segunda
    assert propuesta.despues["edad_max"] == 35 and propuesta.objeciones_abiertas == []


def test_a_reviewer_objection_blocks_and_an_observation_does_not(datos: Path) -> None:
    objecion = {
        "gravedad": "error",
        "tipo": "condicion_omitida",
        "descripcion": "No se menciona el depósito del contrato.",
        "cita": P3,
    }
    nota = {"gravedad": "observacion", "tipo": "otro", "descripcion": "El resumen podría ser más corto.", "cita": ""}
    sin_requisito = {k: v for k, v in NUEVA.items() if k != "requisitos_no_comprobados"}
    lector = Guion(lectura(sin_requisito), lectura())
    revisor = Guion(revision(conforme=False, discrepancias=[objecion]), revision(discrepancias=[nota]))
    propuesta = _correr(datos, lector, revisor)
    assert propuesta.estado == "propuesta" and len(propuesta.iteraciones) == 2
    assert "depósito del contrato" in propuesta.iteraciones[0].objeciones[0]
    assert propuesta.iteraciones[0].veredicto.discrepancias[0].cita_literal is True
    assert propuesta.veredicto is not None and [d.gravedad for d in propuesta.veredicto.discrepancias] == [
        "observacion"
    ]
    # The reviewer's own reading of the income notion is compared with the proposal's.
    otra_base = _correr(
        datos, Guion(lectura()), Guion(revision(base_limite_segun_ley="base_liquidable_general")), max_iter=1
    )
    assert otra_base.estado == "sin_converger" and "Base de los límites de renta" in otra_base.objeciones_abiertas[0]


def test_the_gate_rejects_a_quote_that_is_not_in_the_law_before_any_reviewer_call(datos: Path) -> None:
    inventada = "1. Los contribuyentes menores de 36 años podrán deducir el 15 por ciento, con un máximo de 600 euros."
    lector, revisor = Guion(lectura(evidencia=[inventada, P2])), Guion()
    propuesta = _correr(datos, lector, revisor, max_iter=1)
    assert propuesta.estado == "sin_converger" and revisor.llamadas == []
    assert any("cita no literal" in o for o in propuesta.objeciones_abiertas)
    # Stitching with an ellipsis is not a quote either, and neither is a fragment too short to prove anything.
    cosida = _correr(
        datos, Guion(lectura(evidencia=[P1[:60] + " [...] " + P1[-40:], P2, "600 euros"])), Guion(), max_iter=1
    )
    assert sum("cita no literal" in o for o in cosida.objeciones_abiertas) == 1
    assert any("demasiado corta" in o for o in cosida.objeciones_abiertas)


def test_the_gate_rejects_a_number_that_is_in_no_quote(datos: Path) -> None:
    con_700 = {**NUEVA, "maximo": "700"}
    propuesta = _correr(datos, Guion(lectura(con_700)), Guion(), max_iter=1)
    assert propuesta.estado == "sin_converger"
    assert any("maximo = 700" in o and "no está en ninguna cita" in o for o in propuesta.objeciones_abiertas)
    # The figure exists in the law but the quote that prints it was left out: same rejection.
    sin_cita = _correr(datos, Guion(lectura(evidencia=[P1])), Guion(), max_iter=1)
    assert any("limite_individual = 25000" in o for o in sin_cita.objeciones_abiertas)
    # A summary the user reads may not carry figures the quotes do not print, nor derived-figure notes.
    resumen = {**NUEVA, "resumen": "El 15 por ciento del alquiler, hasta 600 euros (unos 50 euros al mes)."}
    derivada = {**NUEVA, "requisitos_no_comprobados": ["El máximo resulta de aplicar el 15 por ciento a 4.000 euros."]}
    for valor, esperado in ((resumen, "menciona 50"), (derivada, "cifra derivada")):
        rechazada = _correr(datos, Guion(lectura(valor)), Guion(), max_iter=1)
        assert any(esperado in o for o in rechazada.objeciones_abiertas), rechazada.objeciones_abiertas


def test_the_gate_validates_the_model_the_article_and_the_effects_quote(datos: Path) -> None:
    def rechazo(**cambios: Any) -> list[str]:
        return _correr(datos, Guion(lectura(**cambios)), Guion(), max_iter=1).objeciones_abiertas

    assert any("modelo:" in o for o in rechazo(valor={**NUEVA, "base": "gasto_inventado"}))
    assert any("modelo:" in o for o in rechazo(valor={**NUEVA, "campo_inventado": 1}))
    assert any("debe ser exactamente «art. 5»" in o for o in rechazo(valor={**NUEVA, "articulo": "art. 5.1"}))
    assert any("lo pone el programa" in o for o in rechazo(valor={**NUEVA, "recodificada_el": "2026-01-01"}))
    assert rechazo(valor_json="{no es json") == ["valor_json no es JSON válido."]
    assert any(
        "cita de efectos no es literal" in o
        for o in rechazo(cita_efectos="Con efectos a partir del día 1 de enero de 2026")
    )
    assert any("no contiene la fecha 2025-01-01" in o for o in rechazo(efectos_desde="2025-01-01"))
    assert any("no es una fecha" in o for o in rechazo(efectos_desde="enero de 2026"))
    estricto = {**NUEVA, "limite_es_estricto": True}
    assert any("limite_es_estricto" in o for o in rechazo(valor=estricto))


def test_it_gives_up_after_the_configured_passes_with_the_open_objections(datos: Path) -> None:
    con_36 = {**NUEVA, "edad_max": 36}
    lector = Guion(lectura(con_36), lectura(con_36), lectura(con_36))
    revisor = Guion(revision(), revision(), revision())
    propuesta = _correr(datos, lector, revisor)
    assert propuesta.estado == "sin_converger" and propuesta.resultado is None
    assert len(propuesta.iteraciones) == 3 and len(lector.llamadas) == 3 and propuesta.llamadas_llm == 6
    assert propuesta.ejercicios == [] and "36 años cumplidos" in propuesta.objeciones_abiertas[0]
    assert not propuesta.aplicable_sin_persona
    una = _correr(datos, Guion(lectura(con_36)), Guion(revision()), max_iter=1)
    assert len(una.iteraciones) == 1 and una.max_iter == 1


def test_too_few_usable_cases_is_not_a_pass(datos: Path) -> None:
    malos = [
        caso("dato que no existe", {"edad": 30, "salario_del_vecino": "1"}, "0"),
        {**CASOS[0], "fundamento": "La ley dice que son 300 euros."},
        CASOS[2],
    ]
    propuesta = _correr(datos, Guion(lectura()), Guion(revision(casos=malos)), max_iter=1)
    assert propuesta.estado == "sin_converger"
    assert "no usa para esta deducción: salario_del_vecino" in (propuesta.casos[0].error or "")
    assert propuesta.casos[1].error == "el fundamento no es una cita literal del texto legal"
    assert propuesta.casos[2].coincide is True
    assert "solo aportó 1 caso(s)" in propuesta.objeciones_abiertas[0]


def test_the_reader_can_ask_for_another_article_without_spending_a_pass(datos: Path) -> None:
    pide = lectura(
        resultado="no_calculable", valor_json="", evidencia=[], articulos_necesarios=["13"], motivo="faltan artículos"
    )
    con_ahorro = {**NUEVA, "base_ahorro_max": "1800"}
    lector = Guion(pide, lectura(con_ahorro, evidencia=[P1, P2, COMUNES]))
    propuesta = _correr(datos, lector, Guion(revision()))
    assert propuesta.estado == "propuesta" and len(propuesta.iteraciones) == 1 and propuesta.llamadas_llm == 3
    assert propuesta.articulos_ley == ["5", "13"] and propuesta.despues["base_ahorro_max"] == "1800"
    assert COMUNES not in lector.llamadas[0]["user"] and COMUNES in lector.llamadas[1]["user"]
    # An article that does not exist costs a pass and is answered, not looped on.
    fantasma = lectura(
        resultado="no_calculable", valor_json="", evidencia=[], articulos_necesarios=["99"], motivo="faltan artículos"
    )
    agotada = _correr(datos, Guion(fantasma), Guion(), max_iter=1)
    assert agotada.estado == "sin_converger" and "No se pueden añadir" in agotada.objeciones_abiertas[0]


def test_not_computable_is_a_proposal_to_withdraw_that_a_person_decides(datos: Path, tmp_path: Path) -> None:
    motivo = "El importe depende de la renta por miembro de la unidad familiar, que no es un dato de entrada."
    lector = Guion(lectura(resultado="no_calculable", valor_json="", evidencia=[], motivo=motivo))
    revisor = Guion()
    propuesta = _correr(datos, lector, revisor)
    assert propuesta.estado == "propuesta" and propuesta.resultado == "retirada" and revisor.llamadas == []
    assert propuesta.despues is None and propuesta.motivo_lector == motivo and propuesta.ejercicios == [2026]
    assert not propuesta.aplicable_sin_persona
    aplicar_propuesta(propuesta, texto_de_ley(), data_dir=datos, hoy=HOY)
    galicia = _raw(datos, 2026)["comunidades"]["galicia"]
    assert galicia["deducciones_calculables"] == []
    assert "art. 5: Deducción por alquiler de vivienda habitual" in galicia["otras_deducciones"]
    assert len(_raw(datos, 2025)["comunidades"]["galicia"]["deducciones_calculables"]) == 1  # 2025 untouched


def test_a_law_that_cannot_be_read_proposes_nothing(datos: Path) -> None:
    objetivo = _objetivo(datos)
    with pytest.raises(SinTextoError, match="No se pudo leer"):
        recodificar(objetivo, lector=Guion(), revisor=Guion(), texto_de=lambda b, v=None: None)
    sin_articulo = ley().replace("Artículo 5.", "Artículo 55.")
    with pytest.raises(SinTextoError, match="no aparece"):
        recodificar(objetivo, lector=Guion(), revisor=Guion(), texto_de=texto_de_ley(sin_articulo))


# --------------------------------------------------------------------------- #
# Applying, confirming, provenance
# --------------------------------------------------------------------------- #


def _avisos(datos: Path, ejercicio: int = 2026) -> list[str]:
    deducciones = IRPFParams.model_validate(_raw(datos, ejercicio)).comunidades["galicia"].deducciones_calculables
    inp = IRPFInput(ejercicio=ejercicio, comunidad_autonoma="galicia", edad=30, alquiler_vivienda_habitual=D("2000"))
    bases = autonomicas.Bases(*([D("20000")] * 7), base_imponible_ahorro=D(0), resto_unidad_familiar=D(0))
    avisos: list[str] = []
    lineas: list[tuple[str, Decimal, str]] = []
    autonomicas.aplicar(
        list(deducciones),
        inp,
        bases,
        conjunta=False,
        norma="ley sintética",
        linea=lambda c, i, r: lineas.append((c, i, r)),
        aviso=avisos.append,
    )
    avisos.append(f"total {sum(i for _, i, _ in lineas)}")
    return avisos


def test_apply_writes_the_touched_years_marks_provenance_and_confirm_clears_it(datos: Path, tmp_path: Path) -> None:
    almacen = AlmacenPropuestas(tmp_path / "propuestas")
    propuesta = _correr(datos, Guion(lectura()), Guion(revision()))
    almacen.guardar(propuesta)
    antes_2025 = (datos / "irpf_2025.json").read_text(encoding="utf-8")
    assert _avisos(datos)[-1] == "total -200.00"  # the old 10 %

    aplicada = aplicar_propuesta(propuesta, texto_de_ley(), almacen=almacen, data_dir=datos, hoy=HOY)
    assert aplicada.estado == "aplicada" and almacen.leer(propuesta.id).estado == "aplicada"
    escrita = _deduccion(datos, 2026)
    assert escrita == {**propuesta.despues, "recodificada_el": "2026-11-12"} and escrita["maximo"] == "600"
    assert (datos / "irpf_2025.json").read_text(encoding="utf-8") == antes_2025  # the year it does not reach
    revisiones = _raw(datos, 2026)["revisiones"]
    assert "Recodificación automática" in revisiones[-1]["norma"] and propuesta.id in revisiones[-1]["resultado"]

    # The calculator computes with the new data and says who wrote it.
    avisos = _avisos(datos)
    assert avisos[-1] == "total -300.00"
    assert (
        "«Deducción por alquiler de vivienda habitual» (art. 5): deducción recodificada automáticamente el 12/11/2026 a "
        "partir del BOE; pendiente de revisión humana." in avisos
    )
    # ...also when the re-encoded deduction is the reason it is refused.
    deducciones = IRPFParams.model_validate(_raw(datos, 2026)).comunidades["galicia"].deducciones_calculables
    viejo = IRPFInput(ejercicio=2026, comunidad_autonoma="galicia", edad=50, alquiler_vivienda_habitual=D("2000"))
    rechazo: list[str] = []
    autonomicas.aplicar(
        list(deducciones),
        viejo,
        autonomicas.Bases(*([D("20000")] * 7), base_imponible_ahorro=D(0), resto_unidad_familiar=D(0)),
        conjunta=False,
        norma="ley",
        linea=lambda *a: None,
        aviso=rechazo.append,
    )
    assert any("no aplicada" in a for a in rechazo) and any("pendiente de revisión humana" in a for a in rechazo)

    # The weekly check: 2026 matches the law again; 2025 keeps failing on its own, older quotes.
    evidencia = json.loads((datos / "evidencia_deducciones.json").read_text(encoding="utf-8"))
    assert evidencia["galicia"]["art. 5"] == [P1, P2] and evidencia["galicia@2025"] == {"art. 5": CITAS_ANTIGUAS}
    pendientes_2026 = objetivos_de_verificacion(
        [2026], texto_de_ley(), data_dir=datos, ambito="galicia", tipos=["deduccion_autonomica"]
    )
    assert pendientes_2026.objetivos == []
    aun_2025 = objetivos_de_verificacion(
        [2025], texto_de_ley(), data_dir=datos, ambito="galicia", tipos=["deduccion_autonomica"]
    )
    assert [o.ejercicios for o in aun_2025.objetivos] == [[2025]]

    pendientes = pendientes_de_confirmar(datos)
    assert [(p.ejercicio, p.tipo, p.ruta[-1]) for p in pendientes] == [
        (2026, "deduccion_autonomica", "articulo=art. 5")
    ]
    assert confirmar(pendientes, data_dir=datos, almacen=almacen) == 1
    assert "recodificada_el" not in _deduccion(datos, 2026) and pendientes_de_confirmar(datos) == []
    assert not any("pendiente de revisión humana" in a for a in _avisos(datos))
    assert almacen.leer(propuesta.id).confirmada_el is not None
    with pytest.raises(RecodificacionError, match="está «aplicada»"):
        aplicar_propuesta(almacen.leer(propuesta.id), texto_de_ley(), data_dir=datos, hoy=HOY)


def test_a_retroactive_proposal_is_written_to_every_year_it_names(datos: Path) -> None:
    retroactiva = ley(efectos=EFECTOS_2025)
    propuesta = _correr(
        datos,
        Guion(lectura(efectos_desde="2025-01-01", cita_efectos=EFECTOS_2025)),
        Guion(revision(efectos_desde="2025-01-01", cita_efectos=EFECTOS_2025)),
        pagina=retroactiva,
    )
    aplicar_propuesta(propuesta, texto_de_ley(retroactiva), data_dir=datos, hoy=HOY)
    for ejercicio in (2025, 2026):
        assert _deduccion(datos, ejercicio)["porcentaje"] == "0.15"
        assert _deduccion(datos, ejercicio)["en_vigor_desde"] == "2025-01-01"
    evidencia = json.loads((datos / "evidencia_deducciones.json").read_text(encoding="utf-8"))
    assert "galicia@2025" not in evidencia  # both years share the new quotes
    assert {p.ejercicio for p in pendientes_de_confirmar(datos)} == {2025, 2026}


def test_apply_refuses_when_the_law_or_the_data_changed_since_the_proposal(datos: Path) -> None:
    propuesta = _correr(datos, Guion(lectura()), Guion(revision()))
    original = (datos / "irpf_2026.json").read_text(encoding="utf-8")

    cambiada = ley(parrafo1=P1.replace("600 euros", "650 euros"))
    with pytest.raises(LeyCambiadaError, match="ha cambiado desde que se hizo la propuesta"):
        aplicar_propuesta(propuesta, texto_de_ley(cambiada), data_dir=datos, hoy=HOY)
    with pytest.raises(SinTextoError):
        aplicar_propuesta(propuesta, lambda b, v=None: None, data_dir=datos, hoy=HOY)
    # A change elsewhere in the law (another article, the page's chrome) is not a change of this group's text.
    otra_parte = ley().replace(COMUNES, COMUNES.replace("1.800", "2.000")).replace("<body>", "<body><p>aviso</p>")
    assert control.extraer_articulos(otra_parte, ["5"]).sha256 == propuesta.sha256_texto

    # Tampered after the fact: the gate runs again on what is about to be written.
    manipulada = propuesta.model_copy(update={"despues": {**propuesta.despues, "maximo": "6000"}})
    with pytest.raises(ControlFallidoError, match="maximo = 6000"):
        aplicar_propuesta(manipulada, texto_de_ley(), data_dir=datos, hoy=HOY)
    assert (datos / "irpf_2026.json").read_text(encoding="utf-8") == original  # nothing was written

    raw = _raw(datos, 2026)
    raw["comunidades"]["galicia"]["deducciones_calculables"][0]["maximo"] = "550"  # a person edited the file
    (datos / "irpf_2026.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(DatosCambiadosError, match="alguien lo ha cambiado"):
        aplicar_propuesta(propuesta, texto_de_ley(), data_dir=datos, hoy=HOY)


def test_auto_apply_only_when_asked_and_only_what_needs_nobody(datos: Path, tmp_path: Path) -> None:
    def correr(nombre: str, lector: Guion, revisor: Guion, **kw: Any):
        almacen = AlmacenPropuestas(tmp_path / nombre)
        informe, nuevas = ejecutar(
            [_objetivo(datos)],
            lector=lector,
            revisor=revisor,
            texto_de=texto_de_ley(),
            almacen=almacen,
            data_dir=datos,
            hoy=HOY,
            **kw,
        )
        return informe, nuevas, almacen

    informe, nuevas, almacen = correr("apagado", Guion(lectura()), Guion(revision()))
    assert nuevas[0].estado == "propuesta" and _deduccion(datos, 2026) == ANTIGUA
    assert informe.propuestas[0]["estado"] == "propuesta" and informe.llamadas_llm == 2
    assert [p.id for p in almacen.listar("propuesta")] == [nuevas[0].id]

    # The same group against the same text is not paid for twice.
    repetido, nada, _ = (
        ejecutar(
            [_objetivo(datos)],
            lector=Guion(),
            revisor=Guion(),
            texto_de=texto_de_ley(),
            almacen=almacen,
            data_dir=datos,
        )
    ) + (None,)
    assert nada == [] and repetido.ya_tratados[0]["propuesta"] == nuevas[0].id

    # Something left for a person to look at (a figure in a requirement that no quote prints): proposed, not applied.
    dudosa = {**NUEVA, "requisitos_no_comprobados": ["El contrato debe durar al menos 12 meses."]}
    _, nuevas, _ = correr("dudosa", Guion(lectura(dudosa)), Guion(revision()), auto_aplicar=True)
    assert nuevas[0].estado == "propuesta" and nuevas[0].control.revisar and _deduccion(datos, 2026) == ANTIGUA

    informe, nuevas, almacen = correr("encendido", Guion(lectura()), Guion(revision()), auto_aplicar=True)
    assert nuevas[0].estado == "aplicada" and nuevas[0].aplicada_automaticamente and informe.errores == []
    assert _deduccion(datos, 2026)["recodificada_el"] == "2026-11-12" and _deduccion(datos, 2025) == ANTIGUA
    assert almacen.listar("aplicada")[0].id == nuevas[0].id


def test_budget_and_provider_errors_surface_cleanly(datos: Path, tmp_path: Path) -> None:
    almacen = AlmacenPropuestas(tmp_path / "propuestas")
    objetivos = objetivos_de_verificacion([2025, 2026], texto_de_ley(), data_dir=datos, ambito="galicia").objetivos
    assert len(objetivos) == 2
    agotado = PresupuestoAgotadoError("Se ha alcanzado el límite diario de gasto en LLM.", limite="diario")
    informe, nuevas = ejecutar(
        objetivos, lector=Guion(agotado), revisor=Guion(), texto_de=texto_de_ley(), almacen=almacen, data_dir=datos
    )
    assert informe.presupuesto_agotado == "Se ha alcanzado el límite diario de gasto en LLM."
    assert len(informe.sin_procesar) == 2 and nuevas == [] and almacen.listar() == []
    assert _deduccion(datos, 2026) == ANTIGUA
    # The budget can also run out between the reader and the reviewer: still nothing half-done on disk.
    informe, nuevas = ejecutar(
        [o for o in objetivos if o.tipo == "deduccion_autonomica"],
        lector=Guion(lectura()),
        revisor=Guion(agotado),
        texto_de=texto_de_ley(),
        almacen=almacen,
        data_dir=datos,
    )
    assert informe.presupuesto_agotado and nuevas == [] and almacen.listar() == []
    # Any other provider failure is recorded for that group and the run goes on with the next.
    por_tipo = sorted(objetivos, key=lambda o: o.tipo)
    lector = Guion(ProviderError("Claude devolvió JSON no válido"), lectura(ESCALA_NUEVA, evidencia=list(FILAS)))
    informe, nuevas = ejecutar(
        por_tipo,
        lector=lector,
        revisor=Guion(revision_escala()),
        texto_de=texto_de_ley(),
        almacen=almacen,
        data_dir=datos,
    )
    assert len(informe.errores) == 1 and "JSON no válido" in informe.errores[0] and informe.presupuesto_agotado is None
    assert [p.tipo for p in nuevas] == ["escala"] and nuevas[0].estado == "propuesta"
    # A cap on groups per run leaves the rest for the next one.
    informe, _ = ejecutar(
        objetivos,
        lector=Guion(),
        revisor=Guion(),
        texto_de=texto_de_ley(),
        almacen=almacen,
        data_dir=datos,
        max_objetivos=0,
    )
    assert len(informe.sin_procesar) == 1 and len(informe.ya_tratados) == 1


def test_rejecting_keeps_the_record_and_stops_retries(datos: Path, tmp_path: Path) -> None:
    almacen = AlmacenPropuestas(tmp_path / "propuestas")
    _, nuevas = ejecutar(
        [_objetivo(datos)],
        lector=Guion(lectura()),
        revisor=Guion(revision()),
        texto_de=texto_de_ley(),
        almacen=almacen,
        data_dir=datos,
    )
    rechazada = rechazar_propuesta(nuevas[0], motivo="El máximo es por contrato.", almacen=almacen)
    assert rechazada.estado == "rechazada" and almacen.leer(nuevas[0].id).motivo_rechazo == "El máximo es por contrato."
    with pytest.raises(RecodificacionError):
        aplicar_propuesta(rechazada, texto_de_ley(), data_dir=datos, hoy=HOY)
    informe, _ = ejecutar(
        [_objetivo(datos)], lector=Guion(), revisor=Guion(), texto_de=texto_de_ley(), almacen=almacen, data_dir=datos
    )
    assert informe.ya_tratados[0]["estado"] == "rechazada"
    assert almacen.leer(nuevas[0].id[:20]).id == nuevas[0].id  # a unique prefix is enough
    with pytest.raises(RecodificacionError, match="No hay ninguna propuesta"):
        almacen.leer("no-existe")


def test_the_operator_is_told_through_the_existing_channels(datos: Path, tmp_path: Path) -> None:
    class Canal:
        def __init__(self, nombre: str, falla: bool = False) -> None:
            self.nombre, self.falla, self.enviados = nombre, falla, []

        def enviar(self, asunto: str, cuerpo: str) -> None:
            if self.falla:
                raise OSError("sin red")
            self.enviados.append((asunto, cuerpo))

    almacen = AlmacenPropuestas(tmp_path / "propuestas")
    propuesta = _correr(datos, Guion(lectura()), Guion(revision()))
    almacen.guardar(propuesta)
    webhook, correo = Canal("webhook"), Canal("email", falla=True)
    assert avisar_operador([propuesta], [webhook, correo], almacen=almacen) == ["webhook"]
    asunto, cuerpo = webhook.enviados[0]
    assert "recodificación" in asunto and propuesta.id in cuerpo and "PENDIENTE DE TU DECISIÓN" in cuerpo
    assert "Ejercicios: 2026 (efectos desde 2026-01-01)" in cuerpo and "No toca 2025" in cuerpo
    assert almacen.leer(propuesta.id).avisada_por == ["webhook"]
    assert avisar_operador([], [webhook]) == [] and len(webhook.enviados) == 1


# --------------------------------------------------------------------------- #
# The second adapter: progressive scales
# --------------------------------------------------------------------------- #


def revision_escala(**cambios: Any) -> dict[str, Any]:
    def base(euros: str, cuota: str, fila: int) -> dict[str, str]:
        return {
            "descripcion": f"base liquidable de {euros} €",
            "datos_json": json.dumps({"base_liquidable": euros}),
            "importe_esperado": cuota,
            "fundamento": FILAS[fila],
        }

    casos = [
        base("10000", "1000.00", 0),
        base("10000.01", "1000.00", 1),
        base("25000", "3250", 1),
        base("40000", "6000", 2),
    ]
    return {
        "conforme": True,
        "discrepancias": [],
        "efectos_desde": "2026-01-01",
        "cita_efectos": EFECTOS_2026,
        "casos": casos,
        **cambios,
    }


def _escala(datos: Path):
    return objetivo_de_ruta([2025, 2026], ["comunidades", "galicia", "escala_general"], data_dir=datos)


def test_a_scale_is_maintained_by_the_same_loop(datos: Path, tmp_path: Path) -> None:
    almacen = AlmacenPropuestas(tmp_path / "propuestas")
    lector = Guion(lectura(ESCALA_NUEVA, evidencia=list(FILAS)))
    revisor = Guion(revision_escala())
    propuesta = recodificar(_escala(datos), lector=lector, revisor=revisor, texto_de=texto_de_ley())
    assert propuesta.estado == "propuesta" and propuesta.tipo == "escala" and propuesta.ejercicios == [2026]
    assert propuesta.ruta == ["comunidades", "galicia", "escala_general"] and propuesta.articulos_ley == ["4"]
    assert [str(c.obtenido) for c in propuesta.casos] == ["1000.00", "1000.00", "3250.00", "6000.00"]
    assert "class Tramo" in lector.llamadas[0]["system"] and FILAS[1] in lector.llamadas[0]["user"]
    assert P1 not in lector.llamadas[0]["user"]  # the deduction's article is not this group's text
    assert "base_limite_segun_ley" not in revisor.llamadas[0]["schema"]["properties"]

    aplicar_propuesta(propuesta, texto_de_ley(), almacen=almacen, data_dir=datos, hoy=HOY)
    escala = _raw(datos, 2026)["comunidades"]["galicia"]["escala_general"]
    assert escala["tramos"] == ESCALA_NUEVA["tramos"]
    assert escala["fuente"] == {
        "referencia": "art. 4 de la ley sintética (efectos desde 2026-01-01)",
        "en_vigor_desde": "2026-01-01",
        "verificado": False,
        "nota": "Recodificada automáticamente el 2026-11-12 a partir del BOE; pendiente de revisión humana.",
    }
    assert _raw(datos, 2025)["comunidades"]["galicia"]["escala_general"]["tramos"][0]["hasta"] == "12000"
    citas = json.loads((datos / "evidencia_parametros.json").read_text(encoding="utf-8"))
    assert citas["irpf_2026.json"]["comunidades/galicia/escala_general"]["citas"] == list(FILAS)
    # Verified against the law again; the calculator already reports unverified scales in every result.
    assert (
        objetivos_de_verificacion([2026], texto_de_ley(), data_dir=datos, ambito="galicia", tipos=["escala"]).objetivos
        == []
    )
    pendientes = pendientes_de_confirmar(datos)
    assert [(p.tipo, p.ejercicio) for p in pendientes] == [("escala", 2026)]
    confirmar(pendientes, data_dir=datos, almacen=almacen)
    fuente = _raw(datos, 2026)["comunidades"]["galicia"]["escala_general"]["fuente"]
    assert fuente["verificado"] is True and "nota" not in fuente and pendientes_de_confirmar(datos) == []


def test_the_scale_gate_catches_a_misread_cell_and_a_figure_not_quoted(datos: Path) -> None:
    def rechazo(valor: dict[str, Any], evidencia: list[str] | None = None) -> list[str]:
        lector = Guion(lectura(valor, evidencia=list(FILAS) if evidencia is None else evidencia))
        return recodificar(
            _escala(datos), lector=lector, revisor=Guion(), texto_de=texto_de_ley(), max_iter=1
        ).objeciones_abiertas

    mal_leida = json.loads(json.dumps(ESCALA_NUEVA))
    mal_leida["tramos"][1]["tipo"] = "0.20"  # a rate that IS printed in the table, on another row
    assert any("no cuadra con los tramos anteriores" in o for o in rechazo(mal_leida))
    assert any("tramos[1].hasta = 30000" in o for o in rechazo(ESCALA_NUEVA, list(FILAS[:2])))
    sin_fin = {"tramos": ESCALA_NUEVA["tramos"][:2]}
    assert any("solo el último tramo queda sin tope" in o for o in rechazo(sin_fin))
    assert any("modelo:" in o for o in rechazo({"tramos": []}))
    # A scale cannot be "withdrawn": a reader that gives up leaves it to a person.
    rendido = Guion(
        lectura(resultado="no_calculable", valor_json="", evidencia=[], motivo="La tabla tiene dos escalas.")
    )
    propuesta = recodificar(_escala(datos), lector=rendido, revisor=Guion(), texto_de=texto_de_ley())
    assert propuesta.estado == "sin_converger" and "no se puede retirar" in propuesta.objeciones_abiertas[0]
    assert len(propuesta.iteraciones) == 1


def test_a_boundary_case_refutes_a_scale_that_passes_the_gate(datos: Path) -> None:
    # Brackets swapped consistently enough to add up... but not to give the law's tax at 25.000.
    lector = Guion(lectura(ESCALA_NUEVA, evidencia=list(FILAS)))
    revisor = Guion(
        revision_escala(
            casos=[*revision_escala()["casos"][:2], {**revision_escala()["casos"][2], "importe_esperado": "3300"}]
        )
    )
    propuesta = recodificar(_escala(datos), lector=lector, revisor=revisor, texto_de=texto_de_ley(), max_iter=1)
    assert propuesta.estado == "sin_converger" and "da 3250.00 €" in propuesta.objeciones_abiertas[0]


# --------------------------------------------------------------------------- #
# A historical version is never chosen by the loop
# --------------------------------------------------------------------------- #


def test_a_historical_version_is_read_only_when_the_operator_names_it(datos: Path) -> None:
    historica = ley(efectos=EFECTOS_2025)
    paginas = {(LEY, None): ley(), (LEY, "20251231"): historica}
    texto_de = lambda boe_id, version=None: paginas.get((boe_id, version))  # noqa: E731
    objetivo = objetivos_de_articulo(
        [2025], comunidad="galicia", articulo="art. 5", data_dir=datos, version="20251231"
    ).objetivos[0]
    assert objetivo.version == "20251231" and objetivo.ejercicios == [2025]
    propuesta = recodificar(
        objetivo,
        lector=Guion(lectura(efectos_desde="2025-01-01", cita_efectos=EFECTOS_2025)),
        revisor=Guion(revision(efectos_desde="2025-01-01", cita_efectos=EFECTOS_2025)),
        texto_de=texto_de,
    )
    assert propuesta.estado == "propuesta" and propuesta.ejercicios == [2025] and propuesta.version == "20251231"
    assert any("versión histórica 20251231 indicada por el operador" in n for n in propuesta.control.revisar)
    assert not propuesta.aplicable_sin_persona  # a person must rule out later retroactive changes
    aplicar_propuesta(propuesta, texto_de, data_dir=datos, hoy=HOY)
    evidencia = json.loads((datos / "evidencia_deducciones.json").read_text(encoding="utf-8"))
    assert evidencia["galicia@2025"] == {"_version": "20251231", "art. 5": [P1, P2]}
    assert evidencia["galicia"]["art. 5"] == CITAS_ANTIGUAS  # 2026 still to be re-read from the current text
    # Verification discovery never invents a version: every target it builds reads the current text.
    assert all(
        o.version is None
        for o in objetivos_de_verificacion([2025, 2026], texto_de, data_dir=datos, ambito="galicia").objetivos
    )


def test_annotations_of_the_amending_norm_can_carry_the_effects_clause(datos: Path) -> None:
    sin_nota = ley(efectos="Se modifica por el art. 1 de la Ley 1/2026, de 20 de junio.")
    analisis = "BOE-T-2026-9: MODIFICA el art. 5, con efectos de 1 de enero de 2025"
    objetivo = _objetivo(datos, sin_nota).model_copy(update={"anotaciones": analisis})
    lector = Guion(lectura(efectos_desde="2025-01-01", cita_efectos=analisis))
    revisor = Guion(revision(efectos_desde="2025-01-01", cita_efectos=analisis))
    propuesta = recodificar(objetivo, lector=lector, revisor=revisor, texto_de=texto_de_ley(sin_nota))
    assert propuesta.estado == "propuesta" and propuesta.ejercicios == [2025, 2026]
    assert "<anotaciones_de_la_norma_modificadora>" in lector.llamadas[0]["user"]
    assert analisis in revisor.llamadas[0]["user"]
    # ...but the FIGURES must be in the law itself: the annotation only dates them.
    con_cifra = recodificar(
        objetivo,
        lector=Guion(lectura(efectos_desde="2025-01-01", cita_efectos=analisis, evidencia=[P1, P2, analisis])),
        revisor=Guion(),
        texto_de=texto_de_ley(sin_nota),
        max_iter=1,
    )
    assert any("cita no literal" in o for o in con_cifra.objeciones_abiertas)


# --------------------------------------------------------------------------- #
# Rollover hook
# --------------------------------------------------------------------------- #


def test_deductions_dropped_by_a_rollover_become_targets_for_the_new_year(datos: Path) -> None:
    raw = _raw(datos, 2026)
    galicia = raw["comunidades"]["galicia"]
    galicia["deducciones_calculables"] = [
        {**ANTIGUA, "articulo": "art. 3", "titulo": "Otra"},
        dict(ANTIGUA),
        {**ANTIGUA, "articulo": "art. 7", "titulo": "Otra más"},
    ]
    (datos / "irpf_2026.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    galicia["deducciones_calculables"] = [galicia["deducciones_calculables"][0], galicia["deducciones_calculables"][2]]
    raw["ejercicio"] = 2027
    (datos / "irpf_2027.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

    hallazgo = objetivos_de_rollover(2026, 2027, data_dir=datos)
    retirados = [o for o in hallazgo.objetivos if o.ambito == "galicia"]
    assert len(retirados) == 1 and len(hallazgo.objetivos) == 1
    objetivo = retirados[0]
    assert objetivo.ejercicios == [2027] and objetivo.antes == {2027: None} and objetivo.origen == "rollover"
    assert objetivo.referencia_anterior["porcentaje"] == "0.10" and objetivo.citas_antes == CITAS_ANTIGUAS
    assert objetivo.insertar_tras == "art. 3"

    lector = Guion(lectura(efectos_desde="2026-01-01"))
    propuesta = recodificar(
        objetivo, lector=lector, revisor=Guion(revision()), texto_de=texto_de_ley(), datos={2027: raw}
    )
    assert '"porcentaje": "0.10"' in lector.llamadas[0]["user"]  # the reader sees what was withdrawn
    assert propuesta.estado == "propuesta" and propuesta.ejercicios == [2027]
    aplicar_propuesta(propuesta, texto_de_ley(), data_dir=datos, hoy=HOY)
    orden = [d["articulo"] for d in _raw(datos, 2027)["comunidades"]["galicia"]["deducciones_calculables"]]
    assert orden == ["art. 3", "art. 5", "art. 7"]  # back where it was: file order decides incompatibilities


# --------------------------------------------------------------------------- #
# Deterministic helpers
# --------------------------------------------------------------------------- #


def test_articles_are_located_by_their_heading() -> None:
    assert control.articulo_base("art. 5.Dos.1") == "5" and control.articulo_base("art. 9 quáter") == "9 quater"
    assert control.articulo_base("art. 110-2") == "110-2" and control.articulo_base("art. 4.Uno.a).1)") == "4"
    assert control.articulo_base("art. 14 duodecies") == "14 duodecies"
    assert control.articulo_base("art. 18 bis DLeg. 1/2009 de Canarias") == "18 bis"
    assert control.articulo_base("disposición adicional primera") is None
    texto = control.extraer_articulos(ley(), ["5", "99"])
    assert texto.encontrados == ("5",) and texto.faltan == ("99",)
    assert texto.texto.startswith("Artículo 5. Deducción por alquiler") and P3 in texto.texto
    assert COMUNES not in texto.texto and "Bloque" not in texto.texto and "Subir" not in texto.texto
    assert texto.texto.count("Redacción mostrada por el BOE:") == 1 and "Texto original" not in texto.texto
    tabla = control.extraer_articulos(ley(), ["4"]).texto
    assert "10.000,00 1.000,00 20.000,00 15,00 %" in tabla  # a table row reads as one line
    assert control.indice_de(ley())[:2] == [
        "Artículo 4. Escala autonómica.",
        "Artículo 5. Deducción por alquiler de vivienda habitual.",
    ]
    assert control.articulos_que_contienen(ley(), [COMUNES, "no está"]) == ["13"]
    # Whitespace and markup do not change the hash; the words do.
    assert control.extraer_articulos(ley().replace("</p>\n", "</p>\n\n  "), ["5"]).sha256 == texto.sha256
    assert control.extraer_articulos(ley(parrafo1=P1 + " Bis."), ["5"]).sha256 != texto.sha256


def test_figures_are_looked_for_as_spanish_laws_print_them() -> None:
    ev = control.normalizar_evidencia(
        [
            "un máximo de 1.200,00 euros, el 15 por 100 y el 7,5 %",
            "mayores de sesenta y cinco años",
            "renta de 35 000 euros",
            "ciento ochenta y tres días",
        ]
    )
    assert (
        control.hay_numero(ev, D("1200"))
        and control.hay_numero(ev, D("35000"))
        and not control.hay_numero(ev, D("200"))
    )
    assert not control.hay_numero(ev, D("1")) and not control.hay_numero(ev, D("5"))
    assert control.hay_porcentaje(ev, D("0.15")) and control.hay_porcentaje(ev, D("0.075"))
    assert not control.hay_porcentaje(ev, D("0.05")) and not control.hay_porcentaje(ev, D("12"))
    assert control.hay_porcentaje("el quince por ciento", D("0.15"))
    assert control.hay_entero(ev, 65) and control.hay_entero(ev, 64) and control.hay_entero(ev, 183)
    assert not control.hay_entero(ev, 60) and not control.hay_entero(ev, 67)
    assert control.en_palabras(183) == ["ciento ochenta y tres"] and control.en_palabras(21)[0] == "veintiuno"
    assert control.en_palabras(100) == ["cien"] and control.en_palabras(1000) == []
    assert control.numeros_de_texto_sin_respaldo("Hasta 1.200 euros (art. 5) para mayores de 65; 40 % más", ev) == [
        "40"
    ]


def test_figures_the_law_prints_in_its_own_way_are_recognised() -> None:
    def fallos(valor: dict[str, Any], *citas: str) -> list[str]:
        modelo = DeduccionCalculable.model_validate({**ANTIGUA, **valor})
        todas = control.normalizar_evidencia([*CITAS_ANTIGUAS, *citas])
        return control.cifras_sin_respaldo(modelo, todas, REGLAS_DEDUCCION).fallos

    menores = {"descendientes": [{"edad_max": 17}]}
    assert fallos(menores, "si tiene dos o más hijos menores de edad") == []
    assert len(fallos(menores, "si tiene dos o más hijos a su cargo")) == 1
    acogimiento = {
        "tipo": "fija",
        "importe": "500",
        "unidad": "menores_acogidos",
        "porcentaje": None,
        "base": None,
        "maximo": None,
        "prorrateo": {
            "dato": "dias_acogimiento",
            "tramos": [{"desde": 90, "factor": "0.5"}, {"desde": 183, "factor": "1"}],
        },
    }
    impreso = "500 euros por cada menor si convive 183 días; 250 euros si convive más de 90 días"
    assert fallos(acogimiento, impreso) == []  # the law prints the amount the factor gives
    assert any("factor" in f for f in fallos(acogimiento, "500 euros por cada menor si convive 183 días o más de 90"))
    assert control.articulo_base("art. 6 sexties") == "6 sexties"  # ordinals as the laws spell them
    assert control.hay_entero("mayores de sesenta y cinco años", 65) and not control.hay_entero("sesenta y cinco", 5)


def test_effects_dates_must_be_printed_in_the_quote() -> None:
    dia = date(2025, 1, 1)
    assert control.fecha_en_cita("con efectos desde el 1 de enero de 2025", dia)
    assert control.fecha_en_cita("en vigor a partir del 01/01/2025.", dia) and control.fecha_en_cita(
        "desde el 1-1-2025", dia
    )
    assert control.fecha_en_cita("períodos impositivos iniciados a partir de 2025", dia)
    assert not control.fecha_en_cita("con efectos desde el 1 de enero de 2026", dia)
    assert not control.fecha_en_cita("publicada el 30/12/2025", dia)
    assert control.fecha_en_cita("en vigor a partir del 15/07/2025", date(2025, 7, 15))
    assert not control.fecha_en_cita(
        "durante el ejercicio 2025", date(2025, 7, 15)
    )  # the year alone only dates a 1 January
    afectados, fuera = DEDUCCIONES.ejercicios_afectados(date(2025, 7, 15), date(2026, 12, 31), [2024, 2025, 2026, 2027])
    assert afectados == [2025, 2026] and set(fuera) == {2024, 2027}


# --------------------------------------------------------------------------- #
# The schema and the rules cannot drift apart
# --------------------------------------------------------------------------- #


def test_every_numeric_field_of_the_models_has_a_rule() -> None:
    reglas = REGLAS_DEDUCCION
    con_regla = reglas.euros | reglas.fracciones | reglas.factores | reglas.grados
    enteros = {  # integers are checked by type: listed here so that a NEW numeric field has to be looked at
        "edad_min", "edad_max", "habitantes_max", "anios_desde_llegada_max", "minimo", "maximo", "total", "desde",
        "unidades_min", "unidades_max", "anios_desde_max", "ejercicio_min",
    }  # fmt: skip
    assert control.campos_numericos(DeduccionCalculable) - con_regla - enteros == set()
    assert control.campos_numericos(adaptadores.EscalaLeida) == REGLAS_ESCALA.euros | REGLAS_ESCALA.porcentajes_de_tabla


def test_the_shipped_deductions_fit_the_rules_and_load_unchanged() -> None:
    """The gate's rules cover every numeric field in use today, and the provenance field changed no dump."""
    evidencia = json.loads((DATA_DIR / "evidencia_deducciones.json").read_text(encoding="utf-8"))
    for ejercicio in (2025, 2026):
        raw = json.loads((DATA_DIR / f"irpf_{ejercicio}.json").read_text(encoding="utf-8"))
        for clave, comunidad in raw["comunidades"].items():
            for original in comunidad["deducciones_calculables"]:
                modelo = DeduccionCalculable.model_validate(original)
                assert modelo.recodificada_el is None
                assert modelo.model_dump(mode="json", exclude_defaults=True) == DeduccionCalculable.model_validate(
                    modelo.model_dump(mode="json", exclude_defaults=True)
                ).model_dump(mode="json", exclude_defaults=True)
                assert "recodificada_el" not in modelo.model_dump(mode="json", exclude_defaults=True)
                citas = (
                    evidencia.get(f"{clave}@{ejercicio}", {}).get(original["articulo"])
                    or evidencia[clave][original["articulo"]]
                )
                resultado = control.cifras_sin_respaldo(modelo, control.normalizar_evidencia(citas), REGLAS_DEDUCCION)
                assert not [f for f in resultado.fallos if "sin regla" in f], (clave, original["articulo"])
    assert pendientes_de_confirmar(DATA_DIR) == []
    assert load_params(2026).comunidades["galicia"].deducciones_calculables[0].recodificada_el is None


def test_the_prompts_describe_the_schema_from_the_code() -> None:
    esquema = DEDUCCIONES.esquema()
    for campo in DeduccionCalculable.model_fields:
        assert re.search(rf"\b{campo}\b", esquema), campo
    assert (
        "CAMPOS_GASTO = frozenset(" in esquema
        and "class Prorrateo" in esquema
        and "class RepartoDeducciones" not in esquema
    )
    # Every identifier the hand-written guide mentions exists in the schema or among the calculator's inputs.
    conocidos = esquema + " ".join(IRPFInput.model_fields)
    for nombre in set(re.findall(r"`([a-zA-Z_]+)`", adaptadores.GUIA_DEDUCCION)):
        assert re.search(rf"\b{nombre}\b", conocidos), nombre
    assert "class Tramo" in ESCALAS.esquema() and "cuota_hasta_inicio" in ESCALAS.esquema()
    assert set(DEDUCCIONES.campos_de_caso()) <= set(IRPFInput.model_fields)
    assert {"edad", "alquiler_vivienda_habitual", "descendientes", "familia_numerosa"} <= set(
        DEDUCCIONES.campos_de_caso()
    )
    assert "rendimiento_trabajo_bruto" not in DEDUCCIONES.campos_de_caso()
    # Response schemas a strict structured-output API accepts: closed objects, every property required.
    for esquema_json in (prompts.ESQUEMA_LECTOR, prompts.esquema_revisor(DEDUCCIONES.extra_revisor())):
        assert esquema_json["additionalProperties"] is False
        assert sorted(esquema_json["required"]) == sorted(esquema_json["properties"])


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


@pytest.fixture
def cli(datos: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    snapshot = tmp_path / "leyes"
    snapshot.mkdir()
    (snapshot / f"{LEY}.html").write_text(ley(), encoding="utf-8")
    propuestas = tmp_path / "propuestas"
    comunes = ["--data-dir", str(datos), "--proposals-dir", str(propuestas)]
    monkeypatch.setattr(cli_module, "_canales_del_operador", lambda: [])

    def invocar(*args: str, snapshot_dir: bool = True, comunes_: bool = True) -> Any:
        extra = (["--snapshot-dir", str(snapshot)] if snapshot_dir else []) + (comunes if comunes_ else [])
        return CliRunner().invoke(cli_module.cli, [*args, *extra])

    invocar.snapshot = snapshot  # type: ignore[attr-defined]
    invocar.propuestas = propuestas  # type: ignore[attr-defined]
    return invocar


def test_cli_dry_run_says_what_would_be_reread_and_how_much_text_without_any_model(cli: Any, monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "_proveedores_de_recodificacion", lambda: pytest.fail("no LLM in a dry run"))
    resultado = cli("params-recode", "--comunidad", "galicia", "--dry-run")
    assert resultado.exit_code == 0, resultado.output
    salida = json.loads(resultado.output)
    assert salida["grupos_a_revisar"] == 2 and {s["tipo"] for s in salida["simulacion"]} == {
        "deduccion_autonomica",
        "escala",
    }
    deduccion = next(s for s in salida["simulacion"] if s["tipo"] == "deduccion_autonomica")
    assert deduccion["articulos"] == ["5"] and deduccion["ejercicios_pedidos"] == [2025, 2026]
    assert deduccion["tokens_entrada_por_pasada_aprox"] > 5000 and not cli.propuestas.exists()
    assert cli("params-recode", "--version", "20251231", "--dry-run").exit_code != 0  # never a version on its own


def test_cli_recode_proposes_then_a_person_applies_and_confirms(cli: Any, datos: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "hay_credenciales_llm", lambda: False)
    omitido = json.loads(cli("params-recode", "--comunidad", "galicia", "--tipo", "deduccion_autonomica").output)
    assert "No hay credenciales" in omitido["omitido"] and not cli.propuestas.exists()

    monkeypatch.setattr(cli_module, "hay_credenciales_llm", lambda: True)
    monkeypatch.setattr(cli_module, "_proveedores_de_recodificacion", lambda: (Guion(lectura()), Guion(revision())))
    resultado = cli("params-recode", "--comunidad", "galicia", "--tipo", "deduccion_autonomica")
    assert resultado.exit_code == 0, resultado.output
    salida = json.loads(resultado.output)
    assert salida["propuestas"][0]["estado"] == "propuesta" and salida["propuestas"][0]["ejercicios"] == [2026]
    assert _deduccion(datos, 2026) == ANTIGUA  # RECODE_AUTO_APPLY is off by default: nothing written
    identificador = salida["propuestas"][0]["id"]

    listado = json.loads(cli("params-proposals", snapshot_dir=False).output)
    assert [p["id"] for p in listado] == [identificador]
    entera = json.loads(cli("params-proposals", "--show", identificador, snapshot_dir=False).output)
    assert entera["despues"]["porcentaje"] == "0.15" and entera["antes"]["2026"]["porcentaje"] == "0.10"

    aplicada = cli("params-apply", identificador)
    assert aplicada.exit_code == 0, aplicada.output
    assert _deduccion(datos, 2026)["recodificada_el"] and _deduccion(datos, 2025) == ANTIGUA
    otra_vez = cli("params-apply", identificador)
    assert otra_vez.exit_code != 0 and "no se puede aplicar" in otra_vez.output
    pendientes = json.loads(cli("params-proposals", "--pendientes", snapshot_dir=False).output)
    assert len(pendientes["pendientes_de_confirmar"]) == 1
    assert cli("params-confirm", snapshot_dir=False).exit_code != 0  # needs an id or --todas
    confirmada = json.loads(cli("params-confirm", identificador, snapshot_dir=False).output)
    assert confirmada["confirmadas"] == 1 and "recodificada_el" not in _deduccion(datos, 2026)


def test_cli_auto_apply_reject_and_exit_code_on_budget(cli: Any, datos: Path, monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "hay_credenciales_llm", lambda: True)
    agotado = PresupuestoAgotadoError("Límite diario alcanzado.", limite="diario")
    monkeypatch.setattr(cli_module, "_proveedores_de_recodificacion", lambda: (Guion(agotado), Guion()))
    sin_presupuesto = cli("params-recode", "--comunidad", "galicia", "--tipo", "deduccion_autonomica")
    assert sin_presupuesto.exit_code == 1 and json.loads(sin_presupuesto.output)["presupuesto_agotado"]

    con_36 = {**NUEVA, "edad_max": 36}
    monkeypatch.setattr(
        cli_module, "_proveedores_de_recodificacion", lambda: (Guion(lectura(con_36)), Guion(revision()))
    )
    fallida = json.loads(
        cli("params-recode", "--comunidad", "galicia", "--tipo", "deduccion_autonomica", "--max-iter", "1").output
    )
    assert fallida["propuestas"][0]["estado"] == "sin_converger"
    rechazada = cli(
        "params-reject",
        fallida["propuestas"][0]["id"],
        "--motivo",
        "revisada a mano",
        snapshot_dir=False,
        comunes_=False,
    )
    assert rechazada.exit_code != 0  # wrong store: the default directory has no such proposal
    rechazada = CliRunner().invoke(
        cli_module.cli, ["params-reject", fallida["propuestas"][0]["id"], "--proposals-dir", str(cli.propuestas)]
    )
    assert rechazada.exit_code == 0 and json.loads(rechazada.output)["estado"] == "rechazada"

    monkeypatch.setattr(cli_module, "_proveedores_de_recodificacion", lambda: (Guion(lectura()), Guion(revision())))
    ya = json.loads(
        cli("params-recode", "--comunidad", "galicia", "--tipo", "deduccion_autonomica", "--auto-apply").output
    )
    assert ya["ya_tratados"][0]["estado"] == "rechazada" and ya["propuestas"] == []
    auto = json.loads(
        cli(
            "params-recode", "--comunidad", "galicia", "--tipo", "deduccion_autonomica", "--auto-apply", "--force"
        ).output
    )
    assert auto["propuestas"][0]["estado"] == "aplicada" and _deduccion(datos, 2026)["recodificada_el"]
    todas = json.loads(cli("params-confirm", "--todas", "--comunidad", "galicia", snapshot_dir=False).output)
    assert todas["confirmadas"] == 1


def test_the_weekly_workflow_recodes_only_when_an_llm_key_is_configured() -> None:
    workflow = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "vigilancia.yml").read_text("utf-8")
    verify, recode = workflow.index("params-verify --fail-on-mismatch"), workflow.index("params-recode")
    assert verify < recode
    assert "HAY_CLAVE_LLM: ${{ secrets.ANTHROPIC_API_KEY != '' }}" in workflow
    paso = workflow[workflow.rindex("- name:", 0, recode) : recode]
    assert "env.HAY_CLAVE_LLM == 'true'" in paso


def test_the_temporary_copies_leave_the_shipped_data_alone(datos: Path, tmp_path: Path) -> None:
    antes = {p.name: p.read_bytes() for p in DATA_DIR.glob("*.json")}
    propuesta = _correr(datos, Guion(lectura()), Guion(revision()))
    aplicar_propuesta(propuesta, texto_de_ley(), data_dir=datos, hoy=HOY)
    assert {p.name: p.read_bytes() for p in DATA_DIR.glob("*.json")} == antes
    shutil.rmtree(datos)
