"""Keeping the parameters alive: re-verification, year rollover and decree-laws awaiting validation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from hacienda_gpt.cli import proactive as cli_module
from hacienda_gpt.tax import vigencia_normas
from hacienda_gpt.tax.irpf import IRPFInput, calcular_irpf
from hacienda_gpt.tax.params import IRPFParams, load_params
from hacienda_gpt.tax.rollover import preparar_ejercicio
from hacienda_gpt.tax.verificacion import Comprobacion, _formas, comprobaciones, texto_plano, verificar
from hacienda_gpt.tax.vigencia_normas import EstadoNorma, estado_de, estado_desde_xml, guardar_estados

D = Decimal
RDL = "BOE-A-2026-20823"
LEY_GALLEGA = "BOE-A-2011-18161"


def _numero(x: Decimal) -> str:
    """One Spanish spelling of ``x`` (the one with thousands separators when it has them)."""
    return max(_formas(x * 100 if 0 < x < 1 else x), key=len)


def _leyes(checks: list[Comprobacion]) -> dict[tuple[str, str | None], str]:
    """A fake BOE: for each law, a page that prints every quote and figure its checks look for."""
    paginas: dict[tuple[str, str | None], list[str]] = {}
    for check in checks:
        bloque = " ".join(check.citas) + " " + " | ".join(_numero(c) for c in check.cifras)
        paginas.setdefault((check.boe_id, check.version), []).append(f"<p>{bloque}</p>")
    return {clave: "<html><body>" + "\n".join(bloques) + "</body></html>" for clave, bloques in paginas.items()}


@pytest.fixture(scope="module")
def checks_2026() -> list[Comprobacion]:
    return comprobaciones(load_params(2026))


def test_every_group_of_parameters_has_a_check(checks_2026: list[Comprobacion]) -> None:
    rutas = {"/".join(c.ruta) for c in checks_2026}
    assert {"escala_general_estatal", "minimos", "trabajo", "comunidades/galicia/escala_general"} <= rutas
    assert "comunidades/galicia/minimos" in rutas and "comunidades/madrid/deducciones_madrid" in rutas
    # Unverified parameters and computed minimums are not re-checked.
    assert "vivienda_habitual_transitoria" not in rutas and "comunidades/baleares/minimos" not in rutas
    citas = [c for c in checks_2026 if c.tipo == "cita"]
    assert len(citas) >= 80 and all(c.citas for c in citas)  # every regional deduction carries its quote
    # A scale read from a superseded wording is checked against that wording.
    historicas = {c.version for c in comprobaciones(load_params(2025)) if c.version}
    assert historicas == {"20230915", "20221231"}


def test_text_is_normalised_before_matching() -> None:
    assert (
        texto_plano("<p>Base&nbsp;liquidable<br/>HASTA  12.450,00</p><script>x=1</script>")
        == "base liquidable hasta 12.450,00"
    )


def test_all_checks_pass_on_a_text_that_prints_them_and_fail_when_it_changes(checks_2026: list[Comprobacion]) -> None:
    leyes = _leyes(checks_2026)
    resultados = verificar(checks_2026, lambda boe_id, version=None: leyes.get((boe_id, version)))
    assert [r for r in resultados if not r.ok] == []

    # Galicia raises the first bracket: only the Galician scale stops matching.
    escala = next(c for c in checks_2026 if c.ruta == ["comunidades", "galicia", "escala_general"])
    viejo = _numero(escala.cifras[0])
    cambiadas = {**leyes, (LEY_GALLEGA, None): leyes[(LEY_GALLEGA, None)].replace(viejo, "99.999,99")}
    malos = [r for r in verificar(checks_2026, lambda b, v=None: cambiadas.get((b, v))) if not r.ok]
    assert any(r.comprobacion is escala for r in malos)
    assert {r.comprobacion.boe_id for r in malos} == {LEY_GALLEGA}

    # A law that cannot be read fails loudly instead of passing.
    sin = verificar([escala], lambda b, v=None: None)
    assert not sin[0].ok and sin[0].faltan == ["sin texto"]


def test_scale_figures_must_still_be_together() -> None:
    check = Comprobacion(
        boe_id="X", ruta=["e"], descripcion="escala", tipo="tabla", cifras=[D("12450"), D("9.5"), D("20200")]
    )
    junta = "<p>hasta 12.450,00 al 9,5 y hasta 20.200,00</p>"
    separada = "<p>12.450,00 al 9,5</p>" + "<p>relleno</p>" * 2000 + "<p>20.200,00</p>"
    assert verificar([check], lambda b, v=None: junta)[0].ok
    assert verificar([check], lambda b, v=None: separada)[0].faltan == ["las cifras ya no aparecen juntas en una tabla"]


def test_rollover_keeps_what_matches_and_flags_what_does_not(checks_2026: list[Comprobacion]) -> None:
    leyes = _leyes(checks_2026)
    hoy = date(2027, 1, 4)
    data, informe = preparar_ejercicio(2026, 2027, lambda b, v=None: leyes.get((b, v)), hoy=hoy)
    assert data["ejercicio"] == 2027 and informe.conformes == informe.comprobaciones
    assert informe.sin_verificar == [] and informe.deducciones_retiradas == []
    assert data["fuente_principal"]["consultado_el"] == "2027-01-04"
    IRPFParams(**data)

    # The Galician law changes its scale and rewrites the rent deduction.
    galicia = load_params(2026).comunidades["galicia"]
    escala = next(c for c in checks_2026 if c.ruta == ["comunidades", "galicia", "escala_general"])
    deduccion = next(c for c in checks_2026 if c.ruta[:3] == ["comunidades", "galicia", "deducciones_calculables"])
    pagina = (
        leyes[(LEY_GALLEGA, None)]
        .replace(_numero(escala.cifras[0]), "99.999,99")
        .replace(deduccion.citas[0], "nuevo texto")
    )
    data, informe = preparar_ejercicio(
        2026, 2027, lambda b, v=None: {**leyes, (LEY_GALLEGA, None): pagina}.get((b, v)), hoy=hoy
    )
    assert data["comunidades"]["galicia"]["escala_general"]["fuente"]["verificado"] is False
    assert len(data["comunidades"]["galicia"]["deducciones_calculables"]) == len(galicia.deducciones_calculables) - 1
    assert any("Galicia: escala" in s for s in informe.sin_verificar) and len(informe.deducciones_retiradas) == 1
    # Everything else is untouched and the calculator reports the unverified scale.
    assert data["comunidades"]["murcia"]["escala_general"]["fuente"]["verificado"] is True
    r = calcular_irpf(
        IRPFInput(ejercicio=2027, comunidad_autonoma="Galicia", rendimiento_trabajo_bruto=D(30000)), IRPFParams(**data)
    )
    assert any(galicia.escala_general.fuente.referencia in ref for ref in r.parametros_no_verificados)


XML_PENDIENTE = "<documento><metadatos><estatus_derogacion>N</estatus_derogacion></metadatos><analisis><referencias><posteriores><posterior><palabra>SE DICTA EN RELACIÓN</palabra><texto>con la disposición final 5</texto></posterior></posteriores></referencias></analisis></documento>"
XML_CONVALIDADA = XML_PENDIENTE.replace("SE DICTA EN RELACIÓN", "SE PUBLICA Acuerdo de convalidación").replace(
    "con la disposición final 5", ", por Resolución de 5 de noviembre de 2026"
)
XML_DEROGADA = (
    XML_PENDIENTE.replace("<estatus_derogacion>N", "<estatus_derogacion>S")
    .replace("SE DICTA EN RELACIÓN", "SE DEROGA")
    .replace("con la disposición final 5", ", por Resolución de 5 de noviembre de 2026")
)


def test_decree_status_is_read_from_the_boe_record() -> None:
    assert estado_desde_xml(RDL, XML_PENDIENTE).estado == "pendiente"
    convalidada = estado_desde_xml(RDL, XML_CONVALIDADA)
    assert convalidada.estado == "convalidada" and "5 de noviembre" in convalidada.detalle
    assert estado_desde_xml(RDL, XML_DEROGADA).estado == "derogada"


def _inquilino() -> IRPFInput:
    return IRPFInput(ejercicio=2026, rendimiento_trabajo_bruto=D(20000), alquiler_vivienda_habitual=D(9000))


def _arrendador() -> IRPFInput:
    return IRPFInput(
        ejercicio=2026,
        rendimiento_trabajo_bruto=D(30000),
        rendimiento_alquiler_vivienda_sin_reducir=D(8000),
        arrendamiento_fecha_contrato=date(2026, 12, 15),
        arrendamiento_primer_alquiler=True,
        arrendamiento_renta_bajo_indice=True,
    )


def test_parameters_that_hang_on_a_decree_follow_its_fate(tmp_path: Path, monkeypatch) -> None:
    estado = tmp_path / "estado.json"
    monkeypatch.setattr(vigencia_normas.settings, "NORM_STATUS_PATH", str(estado))
    assert load_params(2026).alquiler_vivienda_habitual.fuente.condicionada_a == RDL
    assert estado_de(RDL).estado == "pendiente" and estado_de(None) is None

    pendiente = calcular_irpf(_inquilino())
    assert any(line.concepto.startswith("Deducción estatal por alquiler") for line in pendiente.lineas)
    assert any("pendiente de convalidación" in a for a in pendiente.avisos)
    reducido = calcular_irpf(_arrendador())
    assert any("(100 %" in line.concepto for line in reducido.lineas)

    guardar_estados({RDL: EstadoNorma(boe_id=RDL, estado="convalidada", detalle="Acuerdo de convalidación")})
    convalidado = calcular_irpf(_inquilino())
    assert convalidado.cuota_liquida == pendiente.cuota_liquida
    assert any("ya convalidado" in a for a in convalidado.avisos) and not any(
        "pendiente de convalidación" in a for a in convalidado.avisos
    )

    estado.unlink()
    guardar_estados(
        {
            RDL: EstadoNorma(
                boe_id=RDL, estado="derogada", detalle="SE DEROGA , por Resolución de 5 de noviembre de 2026"
            )
        }
    )
    derogado = calcular_irpf(_inquilino())
    assert not any(line.concepto.startswith("Deducción estatal por alquiler") for line in derogado.lineas)
    assert derogado.cuota_liquida > pendiente.cuota_liquida and any("ha sido derogado" in a for a in derogado.avisos)
    # The landlord falls back to the previous wording of art. 23.2: 50 % instead of 100 %.
    anterior = calcular_irpf(_arrendador())
    assert any("(50 %" in line.concepto for line in anterior.lineas) and any(
        "redacción anterior" in a for a in anterior.avisos
    )


def test_cli_verify_and_decree_check(tmp_path: Path, monkeypatch, checks_2026: list[Comprobacion]) -> None:
    snapshot = tmp_path / "leyes"
    snapshot.mkdir()
    for (boe_id, version), pagina in _leyes(checks_2026).items():
        (snapshot / (f"{boe_id}_p{version}.html" if version else f"{boe_id}.html")).write_text(pagina, encoding="utf-8")
    args = ["params-verify", "--ejercicio", "2026", "--snapshot-dir", str(snapshot), "--fail-on-mismatch"]
    ok = CliRunner().invoke(cli_module.cli, args)
    assert ok.exit_code == 0 and json.loads(ok.output)["2026"]["no_coinciden"] == []
    (snapshot / f"{LEY_GALLEGA}.html").write_text("<p>nada</p>", encoding="utf-8")
    mal = CliRunner().invoke(cli_module.cli, args)
    assert mal.exit_code == 1 and {x["boe_id"] for x in json.loads(mal.output)["2026"]["no_coinciden"]} == {LEY_GALLEGA}

    estado = tmp_path / "estado.json"
    monkeypatch.setattr(vigencia_normas.settings, "NORM_STATUS_PATH", str(estado))
    monkeypatch.setattr(cli_module, "_xml", lambda boe_id: XML_PENDIENTE.encode())
    primera = CliRunner().invoke(cli_module.cli, ["normas-check", "--fail-on-change"])
    assert primera.exit_code == 0 and json.loads(primera.output)["normas"] == {RDL: "pendiente"}
    monkeypatch.setattr(cli_module, "_xml", lambda boe_id: XML_DEROGADA.encode())
    segunda = CliRunner().invoke(cli_module.cli, ["normas-check", "--fail-on-change"])
    assert segunda.exit_code == 1 and json.loads(segunda.output)["cambios"][0]["ahora"] == "derogada"
    assert estado_de(RDL).estado == "derogada"
