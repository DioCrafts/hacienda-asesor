"""The last manual gaps of the autopilot: cases for every IRPF group, the provenance of the files, unnamed articles.

* Every IRPF group of figures a calculator uses has an executable test case (the split of the State deductions
  through the calculator's own function, the offsetting of savings balances through lines of the IRPF trace).
* A law the BOE updated is recorded as read today in every parameter file that cites it — deterministically, and
  only when every check matches and nothing about it is open.
* A norm whose analysis names no article: the blocks it changes come from the consolidated text's notes, from the
  norm's own sentences or, as a last resort, from a model that only lists them; each group read from one of them is
  re-read whole, and "it does not change" is an answer the gate and the reviewer must bear out.

No network and no real model: synthetic BOE pages and scripted providers, on temporary copies of the data.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import shutil
from typing import Any

from click.testing import CliRunner
import pytest

from hacienda_gpt.cli import proactive as cli_module
from hacienda_gpt.tax import fuentes as fuentes_mod
from hacienda_gpt.tax.irpf import repartir_deducciones_estatales
from hacienda_gpt.tax.params import DATA_DIR, FuentePrincipal, RepartoDeducciones
from hacienda_gpt.tax.recodificacion import (
    AlmacenPropuestas,
    Objetivo,
    aplicar_propuesta,
    control,
    nucleo,
    objetivo_de_ruta,
    piloto,
    procedencia,
    recodificar,
    todos_los_objetivos,
)
from hacienda_gpt.tax.recodificacion.adaptadores import CIFRAS
from hacienda_gpt.tax.verificacion import url_de

from .test_recodificacion_irpf import (
    ANTIGUA,
    EFECTOS,
    EFECTOS_ESTATAL,
    ESCALA,
    ESTATAL,
    LEY,
    NOMBRE_LEY,
    NORMA,
    NUEVA,
    P1,
    P2,
    P3,
    VIGOR,
    Guion,
    _deduccion,
    _raw,
    boe,
    casos_alquiler,
    crear_datos,
    lectura,
    norma_modificadora,
    revision,
)

D = Decimal
# The BOE updated the community's law on 20 November; the files read it on 12 November.
ACTUALIZADA = "20/11/2026"
DESPUES = date(2026, 11, 25)
P4 = "4. La deducción solo se aplica a contratos de arrendamiento de duración igual o superior a un año."
NOTA_NORMA = f"Se modifica por el art. 2 de la Ley 3/2026, de 5 de noviembre. Ref. {NORMA}"


# --------------------------------------------------------------------------- #
# Synthetic pages and data
# --------------------------------------------------------------------------- #


def _selector(fecha: str) -> str:
    return (
        '<form class="lista formBOE" method="GET"><fieldset><legend class="fuera">Seleccionar redacción:</legend>'
        '<p><input type="radio" name="p" value="1" checked="checked"/>'
        f"<label>Última actualización, publicada el {fecha}, en vigor a partir del {fecha}.</label></p>"
        "</fieldset></form>"
    )


def _articulo(numero: str, titulo: str, parrafos: list[str], notas: list[str], fecha: str) -> str:
    cuerpo = "".join(f'<p class="parrafo">{p}</p>' for p in parrafos)
    pie = "".join(f'<p class="nota_pie">{n}</p>' for n in notas)
    return (
        f'<div class="bloque"><p class="bloque">[Bloque {numero}]</p>'
        f'<h5 class="articulo">Artículo {numero}. {titulo}</h5>{cuerpo}{pie}{_selector(fecha)}</div>'
    )


def consolidada(
    *,
    cabecera: str = ACTUALIZADA,
    parrafos5: tuple[str, ...] = (P1, P2, P3),
    notas5: tuple[str, ...] = (EFECTOS,),
    version5: str = "30/06/2026",
) -> str:
    """The community's law as the BOE prints it, with the date of its last update at the top."""
    return (
        f"<html><body><p>Texto consolidado. Última actualización publicada el {cabecera}</p>"
        + _articulo("4", "Escala autonómica.", ["La escala autonómica será la siguiente:" + ESCALA], [], "30/06/2026")
        + _articulo("5", "Deducciones autonómicas.", list(parrafos5), list(notas5), version5)
        + "</body></html>"
    )


def _datos(tmp_path: Path, **kw: Any) -> Path:
    """The synthetic IRPF data of ``test_recodificacion_irpf``, with the community's source naming its current text."""
    datos = crear_datos(tmp_path, **kw)
    for ejercicio in (2025, 2026):
        path = datos / f"irpf_{ejercicio}.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["comunidades"]["galicia"]["fuente_principal"]["url"] = url_de(LEY)
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return datos


@pytest.fixture
def datos(tmp_path: Path) -> Path:
    return _datos(tmp_path)


def _fuente_gallega(datos: Path, ejercicio: int) -> dict[str, Any]:
    return dict(_raw(datos, ejercicio)["comunidades"]["galicia"]["fuente_principal"])


def _sin_procedencia(nodo: Any) -> Any:
    """A parameter file without its sources and its log: what must never change when a reading is recorded."""
    if isinstance(nodo, dict):
        if isinstance(nodo.get("boe_id"), str) and "consolidado_a" in nodo:
            return None
        return {k: _sin_procedencia(v) for k, v in nodo.items() if k != "revisiones"}
    if isinstance(nodo, list):
        return [_sin_procedencia(v) for v in nodo]
    return nodo


def _deduccion_objetivo(datos: Path) -> Objetivo:
    objetivo = next(
        o
        for o in todos_los_objetivos(data_dir=datos, boe_id=LEY).objetivos
        if o.tipo == "deduccion_autonomica" and o.ruta[-1] == "articulo=art. 5"
    )
    objetivo.admite_sin_cambios = True
    return objetivo


SIN_CAMBIOS = lectura(None, [], resultado="sin_cambios", valor_json="", desde="", cita="")


def _revision_vigente(**cambios: Any) -> dict[str, Any]:
    """A reviewer who checks the encoding in force (10 %, 500 euros, 22.000 euros) against the old wording."""
    return revision(
        casos_alquiler(P1, P2, nueva=False), base_limite_segun_ley="base_imponible_total", **cambios
    )


# --------------------------------------------------------------------------- #
# 1. Every IRPF group a calculator uses has an executable case
# --------------------------------------------------------------------------- #

ART_67 = (
    "b) El 50 por ciento del importe total de las deducciones previstas en los apartados 2, 3 y 5 del artículo 68 "
    "de esta Ley."
)
ART_67_OTRA = "c) El 40 por ciento de las cantidades destinadas a otros fines que esta Ley no regula aquí."
ART_77 = (
    "a) El 50 por ciento del importe total de las deducciones previstas en los apartados 2, 3 y 5 del artículo 68 "
    "de esta Ley."
)


def ley_estatal_reparto() -> str:
    return (
        "<html><body>"
        + _articulo("67", "Cuota líquida estatal.", ["1. La cuota líquida estatal será el resultado de disminuir:", ART_67, ART_67_OTRA], [EFECTOS_ESTATAL], "30/06/2026")
        + _articulo("77", "Cuota líquida autonómica.", ["1. La cuota líquida autonómica será:", ART_77], [EFECTOS_ESTATAL], "30/06/2026")
        + "</body></html>"
    )  # fmt: skip


def _casos_reparto() -> list[dict[str, str]]:
    def caso(total: str, parte: str, esperado: str) -> dict[str, str]:
        return {
            "descripcion": f"{total} € de deducciones generales, parte {parte}",
            "datos_json": json.dumps({"deducciones_generales": total, "parte": parte}),
            "importe_esperado": esperado,
            "fundamento": ART_77 if parte == "autonomica" else ART_67,
        }

    return [caso("1000", "autonomica", "500.00"), caso("250.50", "estatal", "125.25"), caso("0", "autonomica", "0")]


def test_the_split_of_the_state_deductions_is_tested_through_the_calculators_own_function(tmp_path: Path) -> None:
    reparto = RepartoDeducciones.model_validate(
        {"parte_autonomica": "0.50", "fuente": {"referencia": "x", "en_vigor_desde": "", "verificado": True}}
    )
    assert repartir_deducciones_estatales(D("100.01"), reparto) == (D("50.00"), D("50.01"))
    datos = crear_datos(tmp_path, estatal=True)
    texto_de = boe(datos, estatal=ley_estatal_reparto())
    objetivo = objetivo_de_ruta(None, ["reparto_deducciones_estatales"], data_dir=datos)
    assert objetivo.boe_id == ESTATAL and objetivo.articulos_ley == ["67", "77"]
    ficheros = {e: _raw(datos, e) for e in (2025, 2026)}
    lector = Guion(lectura({"parte_autonomica": "0.50"}, [ART_67, ART_77], cita=EFECTOS_ESTATAL))
    revisor = Guion(revision(_casos_reparto(), cita=EFECTOS_ESTATAL))
    propuesta = recodificar(objetivo, lector=lector, revisor=revisor, texto_de=texto_de, datos=ficheros)
    assert propuesta.estado == "propuesta" and propuesta.aplicable_sin_persona, propuesta.objeciones_abiertas
    assert [str(c.obtenido) for c in propuesta.casos] == ["500.00", "125.25", "0.00"]
    assert "deducciones_generales" in revisor.llamadas[0]["system"]
    # A share read from the wrong passage passes the figures check, and the reviewer's cases refute it.
    lector = Guion(lectura({"parte_autonomica": "0.40"}, [ART_67_OTRA, ART_77], cita=EFECTOS_ESTATAL))
    refutada = recodificar(
        objetivo,
        lector=lector,
        revisor=Guion(revision(_casos_reparto(), cita=EFECTOS_ESTATAL)),
        texto_de=texto_de,
        datos=ficheros,
        max_iter=1,
    )
    assert refutada.estado == "sin_converger" and "la calculadora da 400.00" in refutada.objeciones_abiertas[0]


def test_the_offsetting_of_savings_balances_is_tested_through_the_lines_of_the_irpf_trace() -> None:
    adaptador = CIFRAS["compensacion_ahorro"]
    raw = json.loads((DATA_DIR / "irpf_2026.json").read_text(encoding="utf-8"))
    objetivo = Objetivo(
        tipo=adaptador.tipo,
        fichero=adaptador.fichero,
        ruta=["compensacion_ahorro"],
        etiqueta="compensación",
        ambito="estado",
        boe_id="BOE-T-2006-1",
        articulos_ley=["49"],
        ejercicios=[2026],
        antes={2026: raw["compensacion_ahorro"]},
    )
    cita = ["podrá compensarse con el saldo positivo, con el límite del 25 por ciento de dicho saldo positivo."]
    modelo, visto = adaptador.controlar({"limite_cruzado": "0.25"}, cita, objetivo, efectos_desde=None, datos={})
    assert modelo is not None and visto.ok and visto.revisar == [] and adaptador.casos_minimos(objetivo) == 2
    perdidas = {"rendimiento_capital_mobiliario": "-1000", "ganancias_patrimoniales": "2000"}

    def evaluar(contribuyente: dict[str, str], linea: str, modelo: Any = modelo) -> Decimal:
        return adaptador.evaluar_caso(modelo, {"contribuyente": contribuyente, "linea": linea}, objetivo)[0]

    assert evaluar(perdidas, "Compensación entre rendimientos y ganancias del ahorro") == D("500.00")
    assert evaluar(perdidas, "Base imponible del ahorro") == D("1500.00")
    anteriores = {"rendimiento_capital_mobiliario": "500", "ganancias_patrimoniales": "2000"}
    anteriores["perdidas_patrimoniales_pendientes"] = "3000"
    assert evaluar(anteriores, "Compensación de saldos negativos de ejercicios anteriores") == D("2125.00")
    # A wrong ceiling gives another amount through the same calculator.
    otro = adaptador.controlar({"limite_cruzado": "0.30"}, [cita[0].replace("25", "30")], objetivo, efectos_desde=None, datos={})[0]
    assert evaluar(perdidas, "Compensación entre rendimientos y ganancias del ahorro", otro) == D("600.00")  # fmt: skip


# --------------------------------------------------------------------------- #
# 2. Recording that a law was read today
# --------------------------------------------------------------------------- #


def test_every_parameter_file_says_which_text_of_each_law_it_read() -> None:
    fuentes = procedencia.fuentes_de_los_parametros(DATA_DIR)
    assert {f.fichero for f in fuentes} == {p.name for p in procedencia.ficheros_de_parametros(DATA_DIR)}
    assert {f.fichero for f in fuentes} >= {
        "irpf_2025.json",
        "irpf_2026.json",
        "autonomos_2026.json",
        "sociedades_2026.json",
        "patrimonio_2026.json",
        "isd_2026.json",
    }
    # Sources pinned to a dated version or a PDF are never touched: they record texts that do not change.
    fijadas = {f.donde for f in fuentes if f.fijada}
    assert {d for d in fijadas if d.startswith("autonomos_2025.json")} == {
        f"autonomos_2025.json:fuentes_principales/{b}" for b in ("BOE-A-2006-20764", "BOE-A-2007-6820", "BOE-A-1992-28740")
    }
    assert any(d.startswith("sociedades_2026.json:fuentes_complementarias/") for d in fijadas)
    # Nested sources are found too (a community's own law in the inheritance tax file).
    assert "isd_2026.json:comunidades/aragon/fuentes_complementarias/0" in {f.donde for f in fuentes}
    assert procedencia.fecha_de_consolidacion(
        "<p>&Uacute;ltima actualizaci&oacute;n publicada el 11/11/2026</p>"
        "<label>Última actualización, publicada el 07/10/2026, en vigor a partir del 08/10/2026.</label>"
    ) == date(2026, 11, 11)
    assert procedencia.fecha_de_consolidacion("<p>Texto original, publicado el 01/01/2020</p>") is None


def test_recording_a_reading_writes_every_file_that_cites_the_law_and_nothing_else(tmp_path: Path) -> None:
    datos = tmp_path / "data"
    shutil.copytree(DATA_DIR, datos)
    antes = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in procedencia.ficheros_de_parametros(datos)}
    hecho = procedencia.refrescar(
        "BOE-A-2006-20764", consolidado_a="2026-11-20", pagina="<html>x</html>", data_dir=datos, hoy=DESPUES, motivo="m."
    )
    assert hecho["ficheros"] == ["autonomos_2026.json", "irpf_2025.json", "irpf_2026.json", "sociedades_2026.json"]
    # The four other files have no log of revisions: the reading is in the report only.
    assert hecho["sin_registro"] == ["autonomos_2026.json", "sociedades_2026.json"]
    despues = {p.name: json.loads(p.read_text(encoding="utf-8")) for p in procedencia.ficheros_de_parametros(datos)}
    for nombre, raw in despues.items():
        assert _sin_procedencia(raw) == _sin_procedencia(antes[nombre]), nombre  # no figure moved
        for fuente in procedencia.fuentes_del_fichero(nombre, raw):
            nuevo = fuente.boe_id == "BOE-A-2006-20764" and not fuente.fijada
            assert (fuente.consultado_el == DESPUES.isoformat()) is nuevo, fuente.donde
            assert (fuente.consolidado_a == "2026-11-20") is nuevo, fuente.donde
    revision_ = despues["irpf_2026.json"]["revisiones"][-1]
    assert revision_["boe_id"] == "BOE-A-2006-20764" and "sin tocar ninguna cifra" in revision_["resultado"]
    assert len(despues["irpf_2026.json"]["revisiones"]) == len(antes["irpf_2026.json"]["revisiones"]) + 1
    # Recording the same reading again writes nothing.
    otra = procedencia.refrescar(
        "BOE-A-2006-20764", consolidado_a="2026-11-20", pagina="<p>y</p>", data_dir=datos, hoy=DESPUES, motivo="m."
    )
    assert otra["ficheros"] == []


def test_params_check_does_not_wait_for_a_pinned_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    def fuente(url: str, consultado: str) -> FuentePrincipal:
        return FuentePrincipal(
            norma="Ley", boe_id="BOE-A-1", url=url, consolidado_a="2025-12-31", consultado_el=consultado, sha256_html="0"
        )

    class Params:
        def __init__(self, *fuentes: FuentePrincipal) -> None:
            self.fuentes_principales = {str(i): f for i, f in enumerate(fuentes)}

    fijada = fuente("https://www.boe.es/datosabiertos/api/legislacion-consolidada/id/BOE-A-1/texto", "2026-01-10")
    vigente = fuente(url_de("BOE-A-1"), "2026-11-25")
    monkeypatch.setattr(fuentes_mod, "ejercicios_disponibles", lambda: [])
    monkeypatch.setattr(
        fuentes_mod, "_OTRAS_FAMILIAS", lambda: [(lambda e: Params(fijada) if e == 2025 else Params(vigente), lambda: [2025, 2026])]
    )  # fmt: skip
    fuentes_mod.normas_de_parametros.cache_clear()
    try:
        (norma,) = fuentes_mod.normas_de_parametros()
        assert norma.consultado_el == date(2026, 11, 25) and norma.ejercicios == (2025, 2026)
        assert fuentes_mod.desactualizadas({"BOE-A-1": date(2026, 11, 20)}) == []
    finally:
        fuentes_mod.normas_de_parametros.cache_clear()


def test_a_stale_law_is_recorded_only_when_every_check_matches_and_nothing_is_open(
    datos: Path, tmp_path: Path
) -> None:
    almacen = AlmacenPropuestas(tmp_path / "propuestas")

    def refrescar(pagina: str, **kw: Any) -> procedencia.InformeProcedencia:
        return procedencia.refrescar_lo_que_se_pueda(
            boe(datos, consolidada=pagina), hoy=DESPUES, data_dir=datos, almacen=almacen, **kw
        )

    leida = _fuente_gallega(datos, 2026)
    # Not updated since the files read it: nothing to record.
    assert LEY in refrescar(consolidada(cabecera="01/11/2026")).al_dia
    # Updated, but a quote of a group is gone: blocked, nothing written.
    informe = refrescar(consolidada(parrafos5=(P1, P3)))
    assert LEY in informe.bloqueadas and any("no coincide" in m for m in informe.bloqueadas[LEY])
    # Updated, every check matches, but the caller still follows something about it.
    informe = refrescar(consolidada(), abiertas={LEY: ["la norma X sigue en curso"]})
    assert informe.bloqueadas[LEY] == ["la norma X sigue en curso"] and _fuente_gallega(datos, 2026) == leida
    # A proposal that did not converge is open too.
    sin_converger = recodificar(
        _deduccion_objetivo(datos),
        lector=Guion(lectura(None, [], resultado="no_calculable", valor_json="", motivo="")),
        revisor=Guion(),
        texto_de=boe(datos, consolidada=consolidada()),
        max_iter=1,
    )
    almacen.guardar(sin_converger)
    assert any(sin_converger.id in m for m in refrescar(consolidada()).bloqueadas[LEY])
    nucleo.rechazar_propuesta(sin_converger, motivo="visto", almacen=almacen)
    # Nothing open any more: today's reading is recorded in both years' files, figures untouched.
    informe = refrescar(consolidada())
    assert [r["boe_id"] for r in informe.refrescadas] == [LEY] and informe.bloqueadas == {}
    for ejercicio in (2025, 2026):
        fuente = _fuente_gallega(datos, ejercicio)
        assert (fuente["consolidado_a"], fuente["consultado_el"]) == ("2026-11-20", DESPUES.isoformat())
        assert _raw(datos, ejercicio)["revisiones"][-1]["boe_id"] == LEY
    assert _deduccion(datos, 2026) == ANTIGUA
    # The next run finds it up to date.
    assert LEY in refrescar(consolidada()).al_dia and refrescar(consolidada()).refrescadas == []


def test_a_law_changed_in_an_article_a_group_reads_waits_until_the_group_is_reread(
    datos: Path, tmp_path: Path
) -> None:
    almacen = AlmacenPropuestas(tmp_path / "propuestas")
    # Article 5 has a wording published after the files read the law; its quotes are untouched.
    pagina = consolidada(parrafos5=(P1, P2, P3, P4), version5=ACTUALIZADA)
    texto_de = boe(datos, consolidada=pagina)
    informe = procedencia.refrescar_lo_que_se_pueda(texto_de, hoy=DESPUES, data_dir=datos, almacen=almacen)
    (motivo,) = informe.bloqueadas[LEY]
    assert "artículos modificados desde la última lectura (5)" in motivo and "art. 5" in motivo
    estado = procedencia.diagnosticar(texto_de, data_dir=datos)[LEY]
    assert estado.desactualizada and estado.modificados == ["5"] and len(estado.pendientes_de_releer) == 1
    # The group is re-read against today's text and the result accepted: now the law can be recorded.
    objetivo = _deduccion_objetivo(datos)
    lector = Guion(lectura(NUEVA_P4, [P1, P2, P4], desde="2026-01-01", cita=EFECTOS))
    revisor = Guion(_revision_vigente(desde="2026-01-01"))
    propuesta = recodificar(objetivo, lector=lector, revisor=revisor, texto_de=texto_de, datos=_ficheros(datos))
    assert propuesta.aplicable_sin_persona, propuesta.objeciones_abiertas
    aplicar_propuesta(propuesta, texto_de, almacen=almacen, data_dir=datos, hoy=DESPUES, automatica=True)
    informe = procedencia.refrescar_lo_que_se_pueda(texto_de, hoy=DESPUES, data_dir=datos, almacen=almacen)
    assert [r["boe_id"] for r in informe.refrescadas] == [LEY], informe.bloqueadas
    # The BOE's metadata may say "updated" when the page's date does not: the date never goes backwards.
    viejo = consolidada(cabecera="01/11/2026")
    estados = procedencia.diagnosticar(
        boe(datos, consolidada=viejo), data_dir=datos, fecha_boe=lambda boe_id: date(2026, 11, 30)
    )
    assert estados[LEY].desactualizada and estados[LEY].actualizada_en_boe_el == "2026-11-30"
    # A law whose page cannot be read is not known to be up to date or not: nothing is said about it.
    sin_pagina = procedencia.refrescar_lo_que_se_pueda(
        lambda boe_id, version=None: None, hoy=DESPUES, data_dir=datos, almacen=almacen
    )
    assert sin_pagina.sin_saber[LEY] == "no se pudo leer su texto consolidado" and sin_pagina.refrescadas == []


NUEVA_P4 = {
    **ANTIGUA,
    "requisitos_no_comprobados": [
        "Solo para contratos de arrendamiento de duración igual o superior a un año (no se comprueba)."
    ],
}


def _ficheros(datos: Path) -> dict[int, dict[str, Any]]:
    return {e: _raw(datos, e) for e in (2025, 2026)}


def test_cli_verify_records_readings_only_from_todays_boe(datos: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    texto_de = boe(datos, consolidada=consolidada())
    monkeypatch.setattr(cli_module, "_texto_del_boe", lambda: texto_de)
    monkeypatch.setattr(cli_module, "_fecha_boe", lambda boe_id: None)
    monkeypatch.setattr(cli_module, "DATA_DIR", datos)
    monkeypatch.setattr(cli_module, "hoy_en_espana", lambda: DESPUES)
    args = ["params-verify", "--ejercicio", "2026", "--fichero", "irpf", "--proposals-dir", str(datos / "p")]
    # From a folder of pages: only simulated.
    carpeta = datos.parent / "leyes"
    carpeta.mkdir()
    (carpeta / f"{LEY}.html").write_text(consolidada(), encoding="utf-8")
    simulada = json.loads(CliRunner().invoke(cli_module.cli, [*args, "--snapshot-dir", str(carpeta)]).output)
    assert simulada["procedencia"]["refrescadas"] == [{"boe_id": LEY, "consolidado_a": "2026-11-20", "simulado": True}]
    assert "solo se simula" in simulada["procedencia"]["omitido"] and _fuente_gallega(datos, 2026)["consultado_el"] == "2026-11-12"
    # From today's BOE: recorded (and not with --no-anotar-lecturas).
    assert "procedencia" not in json.loads(CliRunner().invoke(cli_module.cli, [*args, "--no-anotar-lecturas"]).output)
    hecho = json.loads(CliRunner().invoke(cli_module.cli, args).output)
    assert hecho["procedencia"]["refrescadas"][0]["ficheros"] == ["irpf_2025.json", "irpf_2026.json"]
    assert _fuente_gallega(datos, 2026)["consultado_el"] == DESPUES.isoformat()  # fmt: skip


# --------------------------------------------------------------------------- #
# 3. Norms that name no article
# --------------------------------------------------------------------------- #

NORMA_SIN_NOMBRAR = (
    "<html><body>"
    '<p class="articulo">Artículo 1. Objeto.</p><p>Esta ley adopta medidas fiscales y modifica la Ley 4/2019.</p>'
    '<p class="articulo">Artículo 2. Modificación de la Ley 1/2020, de 1 de enero.</p>'
    "<p>Se introducen las siguientes modificaciones:</p>"
    "<p>Uno. El apartado 2 del artículo 5 queda redactado como sigue:</p>"
    f"<p>«2. {P2[3:]} El artículo 9 no se ve afectado.»</p>"
    "<p>Dos. Se añade una disposición adicional tercera con la siguiente redacción:</p>"
    "<p>«Disposición adicional tercera. Registro.</p><p>Los contratos se depositarán en el registro.»</p>"
    '<p class="articulo">Artículo 3. Modificación de la Ley 9/1999, de tasas.</p>'
    "<p>Se modifica el artículo 7, que fija una tasa de 30 euros.</p>"
    '<p class="articulo">Artículo 4.</p>'
    "<p>Con efectos desde el 1 de enero de 2026, se modifican los artículos 12 y 13 de la Ley 1/2020, de 1 de enero.</p>"
    f'<p class="articulo">Disposición final segunda. Entrada en vigor.</p><p>{VIGOR}</p>'
    "</body></html>"
)


def test_the_blocks_a_norm_changes_are_read_from_its_own_sentences() -> None:
    assert control.articulos_que_modifica(NORMA_SIN_NOMBRAR, ley="1/2020") == ["5", "da 3", "12", "13"]
    # Another law's article 7 is not ours, the new wording between «» is not read, nor the norm's own numbers.
    assert control.articulos_que_modifica(NORMA_SIN_NOMBRAR, ley="9/1999") == ["7"]
    assert control.articulos_que_modifica(NORMA_SIN_NOMBRAR, ley="") == []
    assert control.articulos_que_modifica(norma_modificadora(), ley="1/2020") == ["5"]
    assert control.claves_nombradas("Se modifican las disposiciones adicionales vigésima sexta y trigésima") == [
        "da 26",
        "da 30",
    ]
    assert control.clave_de_cabecera("Artículo 10 bis. Deducción por alquiler.") == "10 bis"


def test_the_consolidated_text_names_the_blocks_a_norm_changed() -> None:
    norma = piloto.NormaModificadora(
        norma=NORMA, ley=LEY, fecha="2026-11-20", titulo="Ley 3/2026, de 5 de noviembre, de medidas fiscales."
    )
    assert piloto.nombre_corto(norma.titulo) == "Ley 3/2026, de 5 de noviembre"
    # A wording published on the norm's date whose note names it…
    pagina = consolidada(notas5=(NOTA_NORMA,), version5=ACTUALIZADA)
    assert piloto.articulos_en_el_consolidado(pagina, norma) == {"5": piloto.VIA_VERSION}
    # …or only a note naming it, by its short name (an older version date, a future wording announced).
    tengase = "Téngase en cuenta que la Ley 3/2026, de 5 de noviembre, da otra redacción a este artículo desde 2027."
    pagina = consolidada(notas5=(EFECTOS, tengase))
    assert piloto.articulos_en_el_consolidado(pagina, norma) == {"5": piloto.VIA_ANOTACIONES}
    assert control.bloques_anotados(pagina, ["BOE-A-2099-1"]) == []


def test_a_model_may_only_list_the_blocks_and_its_list_is_checked() -> None:
    raro = NORMA_SIN_NOMBRAR.replace("queda redactado como sigue", "tendrá en adelante este contenido")
    cita = "Uno. El apartado 2 del artículo 5 tendrá en adelante este contenido:"
    lector = Guion(
        {
            "preceptos": [
                {"articulo": "5", "cita": cita},
                {"articulo": "8", "cita": "Se modifica el artículo 8, que no aparece en la norma."},
                {"articulo": "12", "cita": cita},  # a real passage that does not name it
                {"articulo": "art. 5", "cita": cita},  # repeated
            ]
        }
    )
    lista = nucleo.listar_preceptos(
        lector, pagina=raro, norma=NORMA, ley=LEY, nombre_ley=NOMBRE_LEY, ley_citada="1/2020"
    )
    assert [p.articulo for p in lista.preceptos] == ["5"] and lista.llamadas_llm == 1
    assert lista.descartados == ["«8»: su cita no es literal", "«12»: su cita no lo nombra"]
    assert "tasa de 30 euros" not in lector.llamadas[0]["user"]  # only the passages about the amended law
    assert lector.llamadas[0]["schema_name"] == "recodificacion_preceptos"
    nada = nucleo.listar_preceptos(Guion(), pagina=raro, norma=NORMA, ley=LEY, nombre_ley=NOMBRE_LEY, ley_citada="7/2001")
    assert nada.preceptos == [] and "no nombra la ley 7/2001" in nada.descartados[0]


def test_an_amended_article_may_leave_the_group_as_it_is_if_the_gate_and_the_reviewer_agree(
    datos: Path, tmp_path: Path
) -> None:
    texto_de = boe(datos, consolidada=consolidada())
    objetivo = _deduccion_objetivo(datos)
    revisor = Guion(_revision_vigente())
    propuesta = recodificar(
        objetivo, lector=Guion(SIN_CAMBIOS), revisor=revisor, texto_de=texto_de, datos=_ficheros(datos)
    )
    assert propuesta.estado == "propuesta" and propuesta.resultado == "sin_cambios" and propuesta.despues is None
    assert propuesta.aplicable_sin_persona and propuesta.ejercicios == [2025, 2026] and propuesta.citas == [P1, P2]
    # The reviewer was shown the encoding in force and asked to find what it misses; its cases ran with it.
    assert "revision_sin_cambios" in revisor.llamadas[0]["user"] and '"porcentaje": "0.10"' in revisor.llamadas[0]["user"]
    assert [str(c.obtenido) for c in propuesta.casos] == ["200.00", "0.00", "500.00", "100.00", "0.00"]
    antes = {e: (datos / f"irpf_{e}.json").read_bytes() for e in (2025, 2026)}
    almacen = AlmacenPropuestas(tmp_path / "propuestas")
    aceptada = aplicar_propuesta(propuesta, texto_de, almacen=almacen, data_dir=datos, hoy=DESPUES, automatica=True)
    assert aceptada.estado == "aplicada" and aceptada.aplicada_automaticamente
    assert {e: (datos / f"irpf_{e}.json").read_bytes() for e in (2025, 2026)} == antes  # nothing written
    assert "confirmar que no cambia" in nucleo.texto_del_aviso([propuesta])

    # Only when it is asked for.
    objetivo.admite_sin_cambios = False
    no_pedido = recodificar(objetivo, lector=Guion(SIN_CAMBIOS), revisor=Guion(), texto_de=texto_de, max_iter=1)
    assert no_pedido.estado == "sin_converger" and "solo se admite" in no_pedido.objeciones_abiertas[0]
    objetivo.admite_sin_cambios = True
    # Not when a quote the encoding rests on is gone: the reader then has to encode.
    cambiado = boe(datos, consolidada=consolidada(parrafos5=(P1, P2.replace("22.000", "23.000"), P3)))
    gate = recodificar(objetivo, lector=Guion(SIN_CAMBIOS), revisor=Guion(), texto_de=cambiado, max_iter=1)
    assert "ya no está en el texto" in gate.objeciones_abiertas[0]
    # Not when the reviewer finds a new condition outside the quoted passages.
    nueva = boe(datos, consolidada=consolidada(parrafos5=(P1, P2, P3, P4)))
    discrepancia = {"gravedad": "error", "tipo": "condicion_omitida", "descripcion": "Duración mínima.", "cita": P4}
    objetado = recodificar(
        objetivo,
        lector=Guion(SIN_CAMBIOS),
        revisor=Guion(_revision_vigente(conforme=False, discrepancias=[discrepancia])),
        texto_de=nueva,
        max_iter=1,
        datos=_ficheros(datos),
    )
    assert objetado.estado == "sin_converger" and "condicion_omitida" in objetado.objeciones_abiertas[0]


def _registro(texto: str, fecha: str = "2026-11-20") -> dict[str, Any]:
    return {
        NORMA: {
            "boe_id": NORMA,
            "fecha": fecha,
            "titulo": "Ley 3/2026, de 5 de noviembre, de medidas fiscales.",
            "modifica": [{"boe_id": LEY, "accion": "MODIFICA", "texto": texto, "parametros": True}],
        }
    }


SIN_PRECEPTOS = _registro("determinados preceptos de la Ley 1/2020, de 1 de enero")


def _pilotar(datos: Path, tmp_path: Path, texto_de: Any, lector: Any = None, revisor: Any = None, **kw: Any):
    return piloto.pilotar(
        kw.pop("registro", SIN_PRECEPTOS),
        leyes={LEY: NOMBRE_LEY},
        comunidades={LEY: "galicia"},
        texto_de=texto_de,
        estado_path=tmp_path / "estado" / "_params_autopilot.json",
        almacen=AlmacenPropuestas(tmp_path / "propuestas"),
        data_dir=datos,
        lector=lector,
        revisor=revisor,
        hoy=kw.pop("hoy", DESPUES),
        **kw,
    )


def test_autopilot_finds_the_articles_of_an_unconsolidated_norm_in_its_own_text(datos: Path, tmp_path: Path) -> None:
    texto_de = boe(datos, consolidada=consolidada(cabecera="30/06/2026"), diario=NORMA_SIN_NOMBRAR)
    informe, _ = _pilotar(datos, tmp_path, texto_de)
    (norma,) = informe.normas
    assert norma["etapa"] == "sin_consolidar" and norma["preceptos"] == ["5", "12", "13", "da 3"]
    assert norma["articulos"]["5"] == [piloto.VIA_TEXTO]
    assert norma["grupos"] == ["irpf_{ejercicio}.json:comunidades/galicia/deducciones_calculables/articulo=art. 5"]
    assert informe.objetivos == 1 and "preceptos: 5 (texto de la norma)" in piloto.texto_del_parte(informe, [])
    # The law has not changed yet, and the norm is followed: nothing recorded either way.
    assert informe.procedencia is not None and informe.procedencia["refrescadas"] == []

    # With a model the group is re-read from the norm's text, whole, and "no change" may be the answer.
    lector, revisor = Guion(SIN_CAMBIOS), Guion(_revision_vigente())
    informe, nuevas = _pilotar(datos, tmp_path, texto_de, lector, revisor)
    (propuesta,) = nuevas
    assert propuesta.norma_texto == NORMA and propuesta.admite_sin_cambios and propuesta.resultado == "sin_cambios"
    assert "revision_tras_modificacion" in lector.llamadas[0]["user"] and "TODAVÍA SIN CONSOLIDAR" in lector.llamadas[0]["user"]
    # Read from an unconsolidated norm: never accepted without a person.
    assert not propuesta.aplicable_sin_persona  # fmt: skip


def test_autopilot_asks_a_model_to_list_only_when_nothing_else_names_the_articles(datos: Path, tmp_path: Path) -> None:
    raro = NORMA_SIN_NOMBRAR.replace("queda redactado como sigue", "tendrá en adelante este contenido")
    raro = raro.replace("Dos. Se añade una disposición adicional tercera", "Dos. Disposición adicional tercera nueva")
    raro = raro.replace("se modifican los artículos 12 y 13", "los artículos 12 y 13 cambian")
    texto_de = boe(datos, consolidada=consolidada(cabecera="30/06/2026"), diario=raro)
    sin_modelo, _ = _pilotar(datos, tmp_path, texto_de)
    assert sin_modelo.normas[0]["preceptos"] == [] and "sin modelo" in sin_modelo.normas[0]["pendiente"]
    assert "aún no se sabe cuáles (sin modelo" in piloto.texto_del_parte(sin_modelo, [])
    cita = "Uno. El apartado 2 del artículo 5 tendrá en adelante este contenido:"
    lector = Guion({"preceptos": [{"articulo": "5", "cita": cita}]})
    informe, _ = _pilotar(datos, tmp_path, texto_de, lector, Guion(), max_grupos=0)
    assert informe.normas[0]["articulos"] == {"5": [piloto.VIA_MODELO]} and informe.objetivos == 1
    assert informe.recodificacion["llamadas_llm"] == 1  # type: ignore[index]
    # Asked once per stage.
    informe, _ = _pilotar(datos, tmp_path, texto_de, Guion(), Guion(), max_grupos=0)
    assert informe.normas[0]["articulos"] == {"5": [piloto.VIA_MODELO]}


def test_autopilot_rereads_a_group_whose_article_a_consolidated_norm_changed_although_its_check_passes(
    datos: Path, tmp_path: Path
) -> None:
    # Consolidated: article 5 now has a fourth paragraph; the passages the deduction quotes are untouched.
    pagina = consolidada(parrafos5=(P1, P2, P3, P4), notas5=(NOTA_NORMA, EFECTOS), version5=ACTUALIZADA)
    texto_de = boe(datos, consolidada=pagina)
    sin_modelo, _ = _pilotar(datos, tmp_path, texto_de)
    assert sin_modelo.no_coinciden == [] and sin_modelo.objetivos == 1  # verification alone sees nothing
    assert sin_modelo.normas[0]["articulos"] == {"5": [piloto.VIA_VERSION]}
    # Without a model nothing is re-read, so the law is not recorded as read.
    bloqueos = sin_modelo.procedencia["bloqueadas"][LEY]  # type: ignore[index]
    assert any("sigue en curso" in m for m in bloqueos) and any("pendiente de releer" in m for m in bloqueos)

    # The reader says "no change"; the reviewer finds the new condition; the reader encodes it; it is applied.
    discrepancia = {"gravedad": "error", "tipo": "condicion_omitida", "descripcion": "Duración mínima.", "cita": P4}
    lector = Guion(SIN_CAMBIOS, lectura(NUEVA_P4, [P1, P2, P4], desde="2026-01-01", cita=EFECTOS))
    revisor = Guion(
        _revision_vigente(conforme=False, discrepancias=[discrepancia]), _revision_vigente(desde="2026-01-01")
    )
    informe, nuevas = _pilotar(datos, tmp_path, texto_de, lector, revisor, auto_aplicar=True)
    (propuesta,) = nuevas
    assert propuesta.estado == "aplicada" and propuesta.resultado == "recodificada", propuesta.objeciones_abiertas
    assert len(propuesta.iteraciones) == 2 and propuesta.iteraciones[0].resultado_lector == "sin_cambios"
    assert _deduccion(datos, 2026)["requisitos_no_comprobados"] == NUEVA_P4["requisitos_no_comprobados"]
    # Everything about the law is settled: its new reading is recorded in both years' files, and the norm closed.
    assert [r["boe_id"] for r in informe.procedencia["refrescadas"]] == [LEY], informe.procedencia  # type: ignore[index]
    assert _fuente_gallega(datos, 2026)["consolidado_a"] == "2026-11-20"
    assert piloto.leer_estado(tmp_path / "estado" / "_params_autopilot.json").normas[f"{NORMA}|{LEY}"].etapa == "cerrada"
    assert "Leyes cuya lectura se anota hoy" in piloto.texto_del_parte(informe, nuevas)
    # Next day: nothing to do, no model called.
    informe, nuevas = _pilotar(datos, tmp_path, texto_de, Guion(), Guion())
    assert nuevas == [] and informe.objetivos == 0 and LEY in informe.procedencia["al_dia"]  # type: ignore[index]


def test_autopilot_rereads_what_changed_in_a_law_no_norm_announced(datos: Path, tmp_path: Path) -> None:
    pagina = consolidada(parrafos5=(P1, P2, P3, P4), version5=ACTUALIZADA)
    texto_de = boe(datos, consolidada=pagina)
    informe, _ = _pilotar(datos, tmp_path, texto_de, registro={})
    assert informe.normas == [] and informe.no_coinciden == [] and informe.objetivos == 1
    assert informe.releidos_por_cambio == ["Galicia: art. 5 Deducción por alquiler de vivienda habitual: 5"]
    lector = Guion(SIN_CAMBIOS)
    informe, nuevas = _pilotar(datos, tmp_path, texto_de, lector, Guion(_revision_vigente()), registro={}, auto_aplicar=True)
    assert nuevas[0].origen == "relectura" and nuevas[0].estado == "aplicada" and nuevas[0].resultado == "sin_cambios"
    assert [r["boe_id"] for r in informe.procedencia["refrescadas"]] == [LEY]  # type: ignore[index]
    # With --procedencia simulated (texts from a folder) nothing is written.
    otro = _datos(tmp_path / "otro")
    informe, _ = _pilotar(otro, tmp_path / "otro", boe(otro, consolidada=consolidada()), registro={}, procedencia="simular")
    assert informe.procedencia["refrescadas"] == [{"boe_id": LEY, "consolidado_a": "2026-11-20", "simulado": True}]  # type: ignore[index]
    assert _fuente_gallega(otro, 2026)["consultado_el"] == "2026-11-12"  # fmt: skip
