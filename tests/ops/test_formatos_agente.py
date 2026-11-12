"""Agentic diagnosis of a drifted format: gate, reflection, proposals, privacy, budget. No network, no model."""

from __future__ import annotations

from datetime import UTC, date, datetime
import json
from typing import Any

from click.testing import CliRunner
import formatos_util as util
import pytest

from hacienda_gpt import settings
from hacienda_gpt.agent.providers.base import ProviderError
from hacienda_gpt.cli import formatos as cli_formatos
from hacienda_gpt.cli.ops import cli
from hacienda_gpt.ops import formatos, formatos_agente, vigia
from hacienda_gpt.ops.consumo import PresupuestoAgotadoError
from hacienda_gpt.ops.formatos import FormatoError
from hacienda_gpt.ops.formatos_documentos import clase_de, esqueleto, palabras_permitidas
from hacienda_gpt.proactive import novedades
from hacienda_gpt.tax import documentos

SUMARIO = "boe_sumario_20261110.json"
T0 = datetime(2026, 11, 10, 8, 0, tzinfo=UTC)
V1 = formatos.spec_incorporada("boe_sumario").reglas
# The fix a careful agent proposes: the new name NEXT TO the old one.
CON_ALIAS = {**V1, "identificador": ["identificador", "id_disposicion"]}
# The careless one: the old name replaced, so payloads in the previous format stop being read.
SUSTITUIDO = {**V1, "identificador": "id_disposicion"}
CONFORME = {"conforme": True, "objeciones": [], "riesgo": "Bajo: solo se añade un nombre alternativo."}


@pytest.fixture
def deriva(tmp_path, monkeypatch):
    """The BOE renames ``identificador`` to ``id_disposicion``: one good read on record, then a bad one."""
    monkeypatch.setattr(settings, "FORMAT_WATCH", True)
    monkeypatch.setattr(settings, "FORMAT_STATE_PATH", str(tmp_path / "estado.json"))
    monkeypatch.setattr(settings, "FORMAT_DIR", str(tmp_path / "formatos"))
    monkeypatch.setattr(settings, "JOBS_STATE_PATH", str(tmp_path / "jobs.json"))
    monkeypatch.setattr(settings, "HEALTHCHECK_URL", "")
    monkeypatch.setattr(settings, "FORMAT_AUTO_APPLY", False)
    monkeypatch.setattr(settings, "FORMAT_MAX_ITER", 3)
    monkeypatch.setattr(cli_formatos, "_canales", lambda: [])
    monkeypatch.setattr(vigia.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("no network in tests"))
    bueno = util.leer(SUMARIO)
    nuevo = bueno.replace('"identificador"', '"id_disposicion"')
    assert formatos.observar("boe_sumario", bueno, contexto="2026-11-10", ahora=T0).ok
    assert not formatos.observar("boe_sumario", nuevo, contexto="2026-11-11").ok
    return type("Deriva", (), {"dir": tmp_path / "formatos", "bueno": bueno, "nuevo": nuevo})()


def _diagnostico(reglas: dict[str, Any] | None, **cambios: Any) -> dict[str, Any]:
    base = {
        "que_ha_cambiado": "La clave «identificador» de cada item ahora se llama «id_disposicion».",
        "causa": "formato_cambiado",
        "evidencia": '"id_disposicion": "BOE-A-2026-23528"',
        "adaptable_con_datos": reglas is not None,
        "reglas_json": json.dumps(reglas) if reglas is not None else "",
        "justificacion": "Se añade el nombre nuevo como alternativa.",
    }
    return {**base, **cambios}


# --------------------------------------------------------------------------- #
# The gate on its own
# --------------------------------------------------------------------------- #


def test_gate_accepts_rules_that_read_both_samples_and_refuses_the_rest(deriva) -> None:
    bien = formatos.ensayar("boe_sumario", CON_ALIAS)
    assert bien.ok and bien.reproduce_muestra_buena and bien.cumple_en_muestra_nueva

    rompe_lo_anterior = formatos.ensayar("boe_sumario", SUSTITUIDO)
    assert not rompe_lo_anterior.ok and rompe_lo_anterior.reproduce_muestra_buena is False
    assert rompe_lo_anterior.cumple_en_muestra_nueva is True

    no_arregla = formatos.ensayar("boe_sumario", {**V1, "titulo": ["titulo", "rotulo"]})
    assert not no_arregla.ok and no_arregla.reproduce_muestra_buena and no_arregla.cumple_en_muestra_nueva is False

    assert not formatos.ensayar("boe_sumario", V1).ok  # nothing changed
    assert not formatos.ensayar("boe_sumario", {**V1, "inventada": "x"}).ok  # unknown key
    assert not formatos.ensayar("doc_nomina", {}).ok  # no declarative spec


def test_gate_refuses_everything_without_a_known_good_sample(deriva) -> None:
    (deriva.dir / "muestras" / "boe_sumario" / "buena.json").unlink()

    ensayo = formatos.ensayar("boe_sumario", CON_ALIAS)

    assert not ensayo.ok and "No hay muestra buena" in ensayo.motivos[0]


# --------------------------------------------------------------------------- #
# The loop
# --------------------------------------------------------------------------- #


def test_agent_proposes_a_spec_that_passes_the_gate_and_the_reflection(deriva) -> None:
    proveedor = util.ProveedorGuionizado([_diagnostico(CON_ALIAS), CONFORME])

    propuesta = formatos_agente.diagnosticar("boe_sumario", proveedor)

    assert (propuesta.tipo, propuesta.estado, propuesta.llamadas_llm) == ("spec", "propuesta", 2)
    assert propuesta.reglas_propuestas == CON_ALIAS and propuesta.ensayo is not None and propuesta.ensayo.ok
    assert proveedor.nombres() == ["diagnostico_formato", "reflexion_formato"]
    # The agent was given the expectations, the rules in force, both samples and the failing canary.
    enviado = proveedor.llamadas[0]["user"]
    for esperado in ("Reglas vigentes", "id_disposicion", "antigua, bien leída", "items (grave)", "Un día laborable"):
        assert esperado in enviado
    # The reflection is a different call: its own instructions, the samples again, the diagnosis to challenge.
    assert proveedor.llamadas[1]["system"] == formatos_agente.SISTEMA_REFLEXION != proveedor.llamadas[0]["system"]
    assert "id_disposicion" in proveedor.llamadas[1]["user"] and "superado" in proveedor.llamadas[1]["user"]
    # Nothing is in force yet, and the source is still marked.
    assert formatos.spec_activa("boe_sumario").version == 1
    assert novedades.items_del_sumario(json.loads(deriva.nuevo), date(2026, 11, 11)) == []
    assert {e.fuente: e for e in formatos.resumen()}["boe_sumario"].propuestas_pendientes == [propuesta.id]


def test_a_proposal_that_breaks_the_known_good_sample_is_refused_and_retried(deriva) -> None:
    proveedor = util.ProveedorGuionizado([_diagnostico(SUSTITUIDO), _diagnostico(CON_ALIAS), CONFORME])

    propuesta = formatos_agente.diagnosticar("boe_sumario", proveedor)

    assert propuesta.tipo == "spec" and propuesta.reglas_propuestas == CON_ALIAS
    assert propuesta.intentos[0]["ensayo"]["ok"] is False and "rechazo" in propuesta.intentos[0]
    # No reflection was spent on what the gate had already refused…
    assert proveedor.nombres() == ["diagnostico_formato", "diagnostico_formato", "reflexion_formato"]
    # …and the second attempt was told why.
    assert "No reproduce la muestra buena" in proveedor.llamadas[1]["user"]


def test_a_reflection_objection_triggers_a_second_attempt(deriva) -> None:
    objecion = {"conforme": False, "objeciones": ["No explicas por qué no cambia también «titulo»."], "riesgo": "?"}
    proveedor = util.ProveedorGuionizado([_diagnostico(CON_ALIAS), objecion, _diagnostico(CON_ALIAS), CONFORME])

    propuesta = formatos_agente.diagnosticar("boe_sumario", proveedor)

    assert propuesta.tipo == "spec" and propuesta.llamadas_llm == 4
    assert propuesta.intentos[0]["rechazo"] == ["Un revisor independiente objeta: No explicas por qué no cambia también «titulo»."]  # fmt: skip
    assert "No explicas por qué no cambia" in proveedor.llamadas[2]["user"]


def test_an_evidence_that_is_not_in_the_new_sample_is_refused(deriva) -> None:
    inventada = _diagnostico(CON_ALIAS, evidencia='"clave_que_no_existe": 1')
    proveedor = util.ProveedorGuionizado([inventada, _diagnostico(CON_ALIAS), CONFORME])

    propuesta = formatos_agente.diagnosticar("boe_sumario", proveedor)

    assert propuesta.tipo == "spec" and "no aparece literalmente" in proveedor.llamadas[1]["user"]


def test_no_convergence_ends_in_a_developer_report_and_the_source_stays_broken(deriva) -> None:
    proveedor = util.ProveedorGuionizado([_diagnostico(SUSTITUIDO)] * 3)

    propuesta = formatos_agente.diagnosticar("boe_sumario", proveedor)

    assert (propuesta.tipo, propuesta.estado, propuesta.reglas_propuestas) == ("informe", "sin_converger", None)
    assert propuesta.llamadas_llm == 3 and len(propuesta.intentos) == 3
    informe = open(propuesta.informe, encoding="utf-8").read()
    assert "no ha convergido en 3 intento(s)" in informe and "hacienda_gpt/proactive/novedades.py" in informe
    adjuntos = sorted(p.name for p in (deriva.dir / "informes" / propuesta.id).iterdir())
    assert adjuntos == ["informe.md", "muestra_buena.json", "muestra_nueva.json"]
    assert formatos.spec_activa("boe_sumario").version == 1
    assert {e.fuente: e.estado for e in formatos.resumen()}["boe_sumario"] == "degradada"
    with pytest.raises(FormatoError):
        formatos.aplicar(propuesta.id)


def test_a_change_that_is_not_data_ends_in_a_developer_report(deriva) -> None:
    proveedor = util.ProveedorGuionizado(
        [_diagnostico(None, justificacion="Los items llegan ahora en otra petición paginada."), CONFORME]
    )

    propuesta = formatos_agente.diagnosticar("boe_sumario", proveedor)

    assert propuesta.tipo == "informe" and propuesta.llamadas_llm == 2
    assert "hace falta tocar el lector" in open(propuesta.informe, encoding="utf-8").read()


def test_the_reviewer_can_be_a_different_provider(deriva) -> None:
    analista = util.ProveedorGuionizado([_diagnostico(CON_ALIAS)])
    revisor = util.ProveedorGuionizado([CONFORME])

    propuesta = formatos_agente.diagnosticar("boe_sumario", analista, revisor=revisor)

    assert propuesta.tipo == "spec" and analista.nombres() == ["diagnostico_formato"]
    assert revisor.nombres() == ["reflexion_formato"]


# --------------------------------------------------------------------------- #
# Applying, rejecting, confirming
# --------------------------------------------------------------------------- #


def test_apply_puts_the_spec_in_force_and_reject_restores_the_previous_one(deriva) -> None:
    propuesta = formatos_agente.diagnosticar("boe_sumario", util.ProveedorGuionizado([_diagnostico(CON_ALIAS), CONFORME]))
    viejo, nuevo = json.loads(deriva.bueno), json.loads(deriva.nuevo)
    antes = novedades.items_del_sumario(viejo, date(2026, 11, 10))

    aplicada = formatos.aplicar(propuesta.id)

    assert (aplicada.estado, aplicada.aplicada_por, aplicada.spec_version_aplicada) == ("aplicada", "manual", 2)
    spec = formatos.spec_activa("boe_sumario")
    assert (spec.version, spec.origen) == (2, f"propuesta {propuesta.id}")
    # The parser now reads the new format, and the old one exactly as before.
    assert [i.boe_id for i in novedades.items_del_sumario(nuevo, date(2026, 11, 11))] == [i.boe_id for i in antes]
    assert novedades.items_del_sumario(viejo, date(2026, 11, 10)) == antes
    # The next real read clears the status by itself.
    assert formatos.observar("boe_sumario", deriva.nuevo, contexto="2026-11-12").ok
    assert {e.fuente: e.estado for e in formatos.resumen()}["boe_sumario"] == "ok"
    with pytest.raises(FormatoError):
        formatos.aplicar(propuesta.id)

    rechazada = formatos.rechazar(propuesta.id, motivo="prefiero esperar")

    assert rechazada.estado == "rechazada" and rechazada.nota == "prefiero esperar"
    assert formatos.spec_activa("boe_sumario").version == 1
    assert novedades.items_del_sumario(nuevo, date(2026, 11, 11)) == []
    with pytest.raises(FormatoError):
        formatos.confirmar(propuesta.id)


def test_apply_runs_the_gate_again(deriva) -> None:
    propuesta = formatos_agente.diagnosticar("boe_sumario", util.ProveedorGuionizado([_diagnostico(CON_ALIAS), CONFORME]))
    # Someone edits the stored proposal after the fact.
    propuesta.reglas_propuestas = SUSTITUIDO
    formatos.guardar_propuesta(propuesta)

    with pytest.raises(FormatoError, match="no supera el ensayo"):
        formatos.aplicar(propuesta.id)
    assert formatos.spec_activa("boe_sumario").version == 1


def test_auto_apply_is_off_by_default_and_applies_only_what_passed_everything(deriva, monkeypatch) -> None:
    assert settings.FORMAT_AUTO_APPLY is False
    monkeypatch.setattr(settings, "FORMAT_AUTO_APPLY", True)

    propuesta = formatos_agente.diagnosticar("boe_sumario", util.ProveedorGuionizado([_diagnostico(CON_ALIAS), CONFORME]))

    assert (propuesta.estado, propuesta.aplicada_por) == ("aplicada", "auto")
    assert formatos.spec_activa("boe_sumario").version == 2
    # Still waiting for a person to look at it.
    assert {e.fuente: e for e in formatos.resumen()}["boe_sumario"].propuestas_pendientes == [propuesta.id]
    assert formatos.confirmar(propuesta.id, nota="revisado").estado == "confirmada"


def test_cli_diagnose_proposals_apply_confirm(deriva, monkeypatch) -> None:
    proveedor = util.ProveedorGuionizado([_diagnostico(CON_ALIAS), CONFORME])
    monkeypatch.setattr(formatos_agente, "hay_llm", lambda: True)
    monkeypatch.setattr(cli_formatos, "_proveedores", lambda: (proveedor, proveedor))
    runner = CliRunner()

    seco = json.loads(runner.invoke(cli, ["formatos", "diagnose", "--dry-run"]).output)
    assert seco == {"pendientes": ["boe_sumario"], "llm": "no consultado", "resultados": []} and not proveedor.llamadas

    result = runner.invoke(cli, ["formatos", "diagnose"])
    assert result.exit_code == 0, result.output
    resultado = json.loads(result.output)["resultados"][0]
    assert (resultado["fuente"], resultado["resultado"], resultado["estado"]) == ("boe_sumario", "spec_propuesta", "propuesta")  # fmt: skip
    identificador = resultado["propuesta"]

    # The same failure is not diagnosed (and paid for) twice.
    assert json.loads(runner.invoke(cli, ["formatos", "diagnose"]).output)["pendientes"] == []

    listado = json.loads(runner.invoke(cli, ["formatos", "proposals", "--estado", "propuesta"]).output)
    assert [p["id"] for p in listado] == [identificador] and listado[0]["ensayo_superado"] is True
    entera = json.loads(runner.invoke(cli, ["formatos", "proposals", "--show", identificador]).output)
    assert entera["reglas_propuestas"]["identificador"] == ["identificador", "id_disposicion"]

    assert json.loads(runner.invoke(cli, ["formatos", "apply", identificador]).output)["estado"] == "aplicada"
    assert json.loads(runner.invoke(cli, ["formatos", "confirm", identificador]).output)["estado"] == "confirmada"
    fallo = runner.invoke(cli, ["formatos", "reject", identificador, "--motivo", "tarde"])
    assert fallo.exit_code != 0 and "ya está" in fallo.output
    assert runner.invoke(cli, ["formatos", "apply", "no-existe"]).exit_code != 0


# --------------------------------------------------------------------------- #
# Budget and provider errors
# --------------------------------------------------------------------------- #


def test_a_spent_budget_surfaces_cleanly_and_records_nothing(deriva, monkeypatch) -> None:
    agotado = PresupuestoAgotadoError("Se ha alcanzado el límite diario de gasto en modelos.", limite="dia")
    proveedor = util.ProveedorGuionizado([agotado])

    with pytest.raises(ProviderError):
        formatos_agente.diagnosticar("boe_sumario", proveedor)
    assert formatos.listar_propuestas() == []

    proveedor = util.ProveedorGuionizado([agotado])
    resultados = formatos_agente.diagnosticar_pendientes(proveedor, fuentes=["boe_sumario", "boe_metadatos"])
    # One entry, with the reason, and the run stops spending.
    assert resultados == [{"fuente": "boe_sumario", "resultado": "error_llm", "motivo": str(agotado)}]

    proveedor = util.ProveedorGuionizado([agotado])
    monkeypatch.setattr(formatos_agente, "hay_llm", lambda: True)
    monkeypatch.setattr(cli_formatos, "_proveedores", lambda: (proveedor, proveedor))
    result = CliRunner().invoke(cli, ["formatos", "check", "--diagnose"])
    assert result.exit_code == 0, result.output
    salida = json.loads(result.output)
    # The deterministic status is reported all the same.
    assert salida["problemas"] and salida["diagnostico"]["resultados"][0]["resultado"] == "error_llm"
    assert formatos.listar_propuestas() == []


def test_a_provider_that_cannot_be_built_does_not_hide_the_status(deriva, monkeypatch) -> None:
    monkeypatch.setattr(formatos_agente, "hay_llm", lambda: True)

    def sin_proveedor():
        raise ValueError("LLM_DAILY_BUDGET_USD debe ser un importe")

    monkeypatch.setattr(cli_formatos, "_proveedores", sin_proveedor)

    result = CliRunner().invoke(cli, ["formatos", "check", "--diagnose"])

    assert result.exit_code == 0, result.output
    salida = json.loads(result.output)
    assert salida["problemas"] and salida["diagnostico"]["llm"].startswith("no disponible")


def test_nothing_to_diagnose_is_an_error_in_spanish(deriva) -> None:
    with pytest.raises(FormatoError, match="no hay nada que diagnosticar"):
        formatos_agente.diagnosticar("boe_metadatos", util.ProveedorGuionizado([]))


# --------------------------------------------------------------------------- #
# User documents: no personal value ever leaves the reader
# --------------------------------------------------------------------------- #

NOMINA = """\
EMPRESA: Construcciones Hermanos Zubizarreta S.L.   CIF B48123456
TRABAJADOR: Itziar Goikoetxea Urrutia   NIF 72345678K   Nº afiliación 48/1234567/89
Domicilio: Calle Licenciado Poza 31, 4º B, 48011 Bilbao
PERIODO DE LIQUIDACIÓN: del 01/10/2026 al 31/10/2026
DEVENGOS
Salario base                       1.845,32
Importe íntegro satisfecho         2.310,77
DEDUCCIONES
Contingencias comunes  4,70 %        108,61
Retención IRPF  14,25 %              329,28
LÍQUIDO A PERCIBIR                 1.872,88
IBAN ES91 2100 0418 4502 0005 1332
"""
VALORES_PERSONALES = (
    "Zubizarreta", "Hermanos", "Construcciones", "B48123456", "Itziar", "Goikoetxea", "Urrutia", "72345678K",
    "1234567", "Licenciado", "Poza", "Bilbao", "48011", "1.845,32", "2.310,77", "108,61", "329,28", "1.872,88",
    "ES91", "0418", "01/10/2026", "14,25",
)  # fmt: skip


def test_skeleton_keeps_labels_and_layout_and_no_value() -> None:
    forma = esqueleto(NOMINA)

    for valor in VALORES_PERSONALES:
        assert valor.lower() not in forma.lower(), valor
    assert "importe integro _ #" in forma and "liquido a percibir #" in forma and "retencion irpf #" in forma
    assert not any(ch.isdigit() for ch in forma)
    assert len(forma.splitlines()) <= len(NOMINA.splitlines())
    assert clase_de("Nómina de octubre") == "nomina" and clase_de("Factura de la luz") == "otros"
    assert palabras_permitidas("Importe íntegro") and not palabras_permitidas("Importe Goikoetxea")
    assert not palabras_permitidas("12345")


@pytest.fixture
def lector_degradado(deriva, monkeypatch):
    """Five payslips in a row whose proposed fact fails the literal-quote check, with skeletons enabled."""
    monkeypatch.setattr(settings, "FORMAT_DOCUMENT_SKELETONS", True)
    for _ in range(5):
        lector = util.ProveedorGuionizado(
            [{"tipo_documento": "Nómina mensual", "hechos": [{"name": "retenciones", "value": "329.28", "evidencia": "Retenciones practicadas 329,28"}]}]  # fmt: skip
        )
        documentos.analizar_documento(lector, filename="nomina_itziar.txt", data=NOMINA.encode())
    assert {e.fuente: e.estado for e in formatos.resumen()}["doc_nomina"] == "rota"
    return deriva


def test_no_value_of_a_user_document_reaches_the_disk_or_the_prompt(lector_degradado) -> None:
    proveedor = util.ProveedorGuionizado(
        [
            {
                "que_ha_cambiado": "El bruto aparece como «importe íntegro».",
                "etiquetas": [{"campo": "rendimiento_trabajo_bruto", "etiqueta": "importe integro"}],
                "justificacion": "Es el rótulo junto a la cifra mayor de los devengos.",
            },
            CONFORME,
        ]
    )

    propuesta = formatos_agente.diagnosticar("doc_nomina", proveedor)

    en_disco = "".join(p.read_text(encoding="utf-8") for p in lector_degradado.dir.parent.rglob("*") if p.is_file())
    for valor in (*VALORES_PERSONALES, "nomina_itziar"):
        assert valor not in proveedor.texto_recibido, valor
        assert valor not in en_disco, valor
    # What the model did get: the skeleton and the names of the discarded fields.
    assert "liquido a percibir #" in proveedor.texto_recibido and "retenciones" in proveedor.texto_recibido
    # A vocabulary proposal is never applied by itself, whatever the setting says.
    assert (propuesta.fuente, propuesta.tipo, propuesta.estado) == ("documentos", "spec", "propuesta")
    assert documentos.anexo_vocabulario() == ""


def test_vocabulary_proposal_is_reviewed_then_reaches_the_reader_prompt(lector_degradado, monkeypatch) -> None:
    monkeypatch.setattr(settings, "FORMAT_AUTO_APPLY", True)
    etiquetas = [{"campo": "rendimiento_trabajo_bruto", "etiqueta": "importe integro"}]
    respuesta = {"que_ha_cambiado": "x", "etiquetas": etiquetas, "justificacion": "y"}
    propuesta = formatos_agente.diagnosticar("doc_nomina", util.ProveedorGuionizado([respuesta, CONFORME]))
    assert propuesta.estado == "propuesta"

    formatos.aplicar(propuesta.id)

    lector = util.ProveedorGuionizado([{"tipo_documento": "Nómina", "hechos": []}])
    documentos.analizar_documento(lector, filename="n.txt", data=NOMINA.encode())
    assert lector.llamadas[0]["system"].startswith(documentos.SYSTEM_PROMPT)
    assert "- rendimiento_trabajo_bruto: «importe integro»" in lector.llamadas[0]["system"]
    formatos.rechazar(propuesta.id)
    assert documentos.anexo_vocabulario() == ""


def test_labels_that_are_not_in_the_skeleton_or_not_generic_are_refused(lector_degradado) -> None:
    def respuesta(campo: str, etiqueta: str) -> dict[str, Any]:
        return {"que_ha_cambiado": "x", "etiquetas": [{"campo": campo, "etiqueta": etiqueta}], "justificacion": "y"}

    proveedor = util.ProveedorGuionizado(
        [
            respuesta("rendimiento_trabajo_bruto", "sueldo pactado"),  # not in any skeleton
            respuesta("campo_inventado", "importe integro"),  # unknown field
            respuesta("rendimiento_trabajo_bruto", "importe integro"),
            CONFORME,
        ]
    )

    propuesta = formatos_agente.diagnosticar("doc_nomina", proveedor)

    assert propuesta.estado == "propuesta" and len(propuesta.intentos) == 3
    assert "no aparecen literalmente" in proveedor.llamadas[1]["user"]
    assert "campo desconocido" in proveedor.llamadas[2]["user"]
    # The gate of the vocabulary spec refuses personal-looking labels, removals and no-ops by itself.
    assert not formatos.ensayar("documentos", {"vocabulario": [{"campo": "retenciones", "etiqueta": "Itziar Goikoetxea"}]}).ok  # fmt: skip
    assert not formatos.ensayar("documentos", {"vocabulario": []}).ok


def test_without_skeletons_no_model_is_called(deriva) -> None:
    for _ in range(5):
        lector = util.ProveedorGuionizado([{"tipo_documento": "Nómina", "hechos": []}])
        documentos.analizar_documento(lector, filename="n.txt", data=NOMINA.encode())
    proveedor = util.ProveedorGuionizado([])

    resultados = formatos_agente.diagnosticar_pendientes(proveedor, fuentes=["doc_nomina"])

    assert resultados[0]["resultado"] == "informe_para_desarrollador" and resultados[0]["llamadas_llm"] == 0
    assert not proveedor.llamadas and not (deriva.dir / "esqueletos").exists()
    assert "FORMAT_DOCUMENT_SKELETONS" in formatos.listar_propuestas()[0].justificacion
