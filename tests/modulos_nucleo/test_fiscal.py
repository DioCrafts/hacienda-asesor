"""The tax module through the contract: same tools, same notices, nothing moved."""

from __future__ import annotations

from datetime import UTC, date, datetime

from hacienda_gpt.agent.evidence import EvidenceLedger
from hacienda_gpt.agent.providers.base import ToolCall
from hacienda_gpt.agent.tools import Toolbox
from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType
from hacienda_gpt.modulos.contrato import ContextoModulo, Modulo
from hacienda_gpt.modulos.fiscal import MODULO
from hacienda_gpt.proactive.jobs import ejecutar_para_caso
from hacienda_gpt.proactive.store import NotificationStore

PERFIL = {"rendimiento_trabajo_bruto": 30000.0, "retenciones": 4000.0, "comunidad_autonoma": "Madrid"}
NOV = date(2026, 11, 12)


def _case() -> CaseState:
    now = datetime.now(UTC)
    return CaseState(
        case_id="c-fiscal",
        user_id="u",
        jurisdiction="ES",
        tax_period="2026",
        created_at=now,
        updated_at=now,
        facts=[
            Fact(
                fact_id=f"fact_{k}",
                name=k,
                value=v,
                value_type=FactValueType.STRING if isinstance(v, str) else FactValueType.NUMBER,
                source="t",
                confidence=1.0,
            )
            for k, v in PERFIL.items()
        ],
    )


def test_it_satisfies_the_contract_and_keeps_its_tool_names() -> None:
    assert isinstance(MODULO, Modulo)
    nombres = {h.spec.name for h in MODULO.herramientas}
    assert {"calcular_irpf", "optimizar_impuestos", "calendario_fiscal"} <= nombres
    # The corpus search and the case tools stay core tools of the agent.
    assert not nombres & {"buscar_fuentes", "registrar_hechos", "consultar_expediente"}
    assert all(h.efecto == "lectura" for h in MODULO.herramientas)
    assert any(f.identificador == "BOE-A-2006-20764" for f in MODULO.fuentes)


def test_a_tool_through_the_contract_says_what_the_toolbox_says() -> None:
    herramienta = next(h for h in MODULO.herramientas if h.spec.name == "calcular_irpf")
    resultado = herramienta.ejecutar({"ejercicio": 2026}, ContextoModulo(perfil=PERFIL, hoy=NOV, case_id="c-fiscal"))
    directo = Toolbox(searcher=None, ledger=EvidenceLedger(), case=_case()).execute(  # type: ignore[arg-type]
        ToolCall(id="1", name="calcular_irpf", arguments={"ejercicio": 2026})
    )
    assert not resultado.es_error
    assert resultado.contenido == directo.content.replace("[F1] ", "")


def test_its_notices_are_the_jobs_notices(tmp_path) -> None:
    avisos = MODULO.avisos(ContextoModulo(perfil=PERFIL, hoy=NOV, case_id="c-fiscal"))
    store = NotificationStore(str(tmp_path / "n.db"))
    assert ejecutar_para_caso(_case(), hoy=NOV, store=store) == {"plazo": 0, "fin_de_ejercicio": 1}
    (nota,) = store.list("c-fiscal")
    (aviso,) = avisos
    assert (aviso.clave, aviso.titulo, aviso.cuerpo, aviso.importe) == (nota.key, nota.title, nota.body, nota.amount)
    assert aviso.sustituye_prefijo == "fin_de_ejercicio:2026:"
    # The inbox keeps writing them exactly as before.
    assert MODULO.ejecutar_en_bandeja(_case(), hoy=NOV, store=store) == {"plazo": 0, "fin_de_ejercicio": 0}
