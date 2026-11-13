"""Family B: calls read from real BOE summaries and documents (trimmed fixtures), no network."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import json
from pathlib import Path
from typing import Any

import pytest

from hacienda_gpt.modulos.ayudas import almacen as almacen_mod
from hacienda_gpt.modulos.ayudas import bdns, convocatorias
from hacienda_gpt.modulos.ayudas.convocatorias import (
    Convocatoria,
    aceptar_respuesta_llm,
    analizar,
    candidatas,
    encaje,
    leer_documento,
    numero_texto,
    sumar_dias_habiles,
    sumar_meses,
)
from hacienda_gpt.modulos.ayudas.hogar import hogar_del_perfil
from hacienda_gpt.modulos.ayudas.revision import actualizar_convocatorias, enriquecer_con_bdns
from hacienda_gpt.proactive.novedades import ItemSumario

from .conftest import FIXTURES

HOY = date(2026, 11, 13)
SUMARIOS = FIXTURES / "sumarios"
DOCUMENTOS = FIXTURES / "documentos"


def _sumario(dia: date) -> Mapping[str, Any] | None:
    ruta = SUMARIOS / f"{dia:%Y%m%d}.json"
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else None


def _xml(boe_id: str) -> bytes | None:
    ruta = DOCUMENTOS / f"{boe_id}.xml"
    return ruta.read_bytes() if ruta.exists() else None


def _conv(dia: str, boe_id: str) -> Convocatoria:
    fecha = date(int(dia[:4]), int(dia[4:6]), int(dia[6:]))
    item = next(i for i in candidatas(_sumario(fecha) or {}, fecha) if i.boe_id == boe_id)
    xml = _xml(boe_id)
    assert xml is not None
    return analizar(item, xml, HOY)


def _encaje(conv: Convocatoria, perfil: dict[str, Any]) -> str:
    return encaje(conv, hogar_del_perfil(perfil, HOY), perfil).nivel


def test_candidates_of_each_day() -> None:
    esperado = {
        "20260603": ["BOE-B-2026-18498"],
        "20261022": ["BOE-B-2026-34368"],  # the prizes for Ceuta and Melilla are not aid
        "20261104": ["BOE-A-2026-23163"],
        "20261106": ["BOE-B-2026-36326"],
        "20261111": ["BOE-B-2026-36910"],
        "20261113": ["BOE-B-2026-37127"],
    }
    for dia, ids in esperado.items():
        fecha = date(int(dia[:4]), int(dia[4:6]), int(dia[6:]))
        assert [i.boe_id for i in candidatas(_sumario(fecha) or {}, fecha)] == ids, dia


def test_spa_programme_for_pensioners() -> None:
    conv = _conv("20261104", "BOE-A-2026-23163")
    assert conv.destinatario == "personas" and "mayores" in conv.temas
    assert conv.seccion == "3" and conv.ambito == []
    fines = [(p.tipo, p.fin) for p in conv.plazos]
    assert ("meses", date(2026, 12, 4)) in fines  # one month from the day after 4 November
    assert ("fecha", date(2027, 5, 12)) in fines and ("fecha", date(2027, 10, 31)) in fines
    espera = next(p for p in conv.plazos if p.fin == date(2027, 5, 11))
    assert "lista de espera" in espera.contexto
    assert conv.proximo_fin(HOY) == date(2026, 12, 4)
    assert _encaje(conv, {"edad": 70, "comunidad_autonoma": "Madrid"}) == "encaja"
    assert _encaje(conv, {"edad": 40, "comunidad_autonoma": "Madrid"}) == "no"
    assert _encaje(conv, {"comunidad_autonoma": "Madrid"}) == "posible"


def test_training_grants_for_graduates() -> None:
    conv = _conv("20261022", "BOE-B-2026-34368")
    assert conv.bdns == "936148" and conv.destinatario == "personas" and conv.temas == ["estudios"]
    assert [(p.tipo, p.fin, p.estimado) for p in conv.plazos] == [("dias_habiles", date(2026, 11, 19), True)]
    assert "veinte días hábiles" in conv.plazos[0].cita
    assert "1.200 euros" in conv.cuantia
    assert _encaje(conv, {"edad": 25}) == "posible"  # a degree cannot be checked
    assert _encaje(conv, {"edad": 25, "ayudas_intereses": ["estudios"]}) == "encaja"


def test_regional_scope_and_mixed_beneficiaries() -> None:
    conv = _conv("20260603", "BOE-B-2026-18498")
    assert conv.ambito == ["Ceuta", "Melilla"] and conv.destinatario == "mixto"
    assert [(p.tipo, p.fin) for p in conv.plazos] == [("meses", date(2026, 7, 3))]
    hoy = date(2026, 6, 10)
    madrid = {"edad": 70, "comunidad_autonoma": "Madrid"}
    ceuta = {"edad": 70, "comunidad_autonoma": "Ceuta"}
    assert encaje(conv, hogar_del_perfil(madrid, hoy), madrid).nivel == "no"
    assert encaje(conv, hogar_del_perfil(ceuta, hoy), ceuta).nivel == "posible"


def test_calls_for_companies_and_bodies_are_never_proposed() -> None:
    mineria = _conv("20261106", "BOE-B-2026-36326")
    assert mineria.destinatario == "entidades"
    assert [(p.tipo, p.fin) for p in mineria.plazos] == [("meses", date(2026, 12, 7))]
    edge = _conv("20261111", "BOE-B-2026-36910")
    assert [(p.tipo, p.fin) for p in edge.plazos] == [("dias_habiles", date(2026, 11, 16))]  # "tercer día hábil"
    centros = _conv("20261113", "BOE-B-2026-37127")
    assert [(p.tipo, p.fin) for p in centros.plazos] == [("dias_habiles", date(2026, 12, 4))]  # "Quince días (15) hábiles"
    for conv in (mineria, edge, centros):
        assert _encaje(conv, {"edad": 40, "es_autonomo": True, "comunidad_autonoma": "Madrid"}) == "no"


def test_disability_programme_needs_the_studies_confirmed() -> None:
    item = ItemSumario(
        boe_id="BOE-B-2026-21272",
        fecha=date(2026, 6, 22),
        seccion="5B",
        departamento="MINISTERIO DE DERECHOS SOCIALES, CONSUMO Y AGENDA 2030",
        titulo="Extracto de la Resolución de 18 de junio de 2026 (Programa Reina Letizia para la Inclusión).",
    )
    conv = analizar(item, _xml("BOE-B-2026-21272") or b"", date(2026, 6, 23))
    assert conv.destinatario == "personas" and {"discapacidad", "estudios"} <= set(conv.temas)
    perfil = {"edad": 30, "grado_discapacidad": 45}
    hoy = date(2026, 6, 23)
    assert encaje(conv, hogar_del_perfil(perfil, hoy), perfil).nivel == "posible"
    con_interes = {**perfil, "ayudas_intereses": ["estudios"]}
    assert encaje(conv, hogar_del_perfil(con_interes, hoy), con_interes).nivel == "encaja"
    sin_discapacidad = {"edad": 30, "grado_discapacidad": 0, "ayudas_intereses": ["estudios"]}
    assert encaje(conv, hogar_del_perfil(sin_discapacidad, hoy), sin_discapacidad).nivel == "no"


def test_every_quote_is_literally_in_the_document() -> None:
    for ruta in sorted(DOCUMENTOS.glob("*.xml")):
        doc = leer_documento(ruta.read_bytes())
        texto = " ".join(" ".join(doc.parrafos).split())
        item = ItemSumario(boe_id=ruta.stem, fecha=date(2026, 11, 2), seccion="5B", departamento="", titulo=doc.titulo)
        conv = analizar(item, ruta.read_bytes(), HOY)
        for plazo in conv.plazos:
            assert " ".join(plazo.cita.split()) in texto, ruta.stem
            assert " ".join(plazo.contexto.split()) in texto
        for senal in conv.senales:
            assert " ".join(senal.fragmento.split()) in " ".join([doc.titulo, texto]), ruta.stem
        assert " ".join(conv.beneficiarios.split()) in texto


def test_deadline_arithmetic() -> None:
    assert sumar_dias_habiles(date(2026, 10, 22), 20) == date(2026, 11, 19)
    assert sumar_dias_habiles(date(2026, 11, 13), 1) == date(2026, 11, 16)  # Friday -> Monday
    # No 31 February: the last day of the month (art. 30.4), a Saturday, moves to Monday (art. 30.5).
    assert sumar_meses(date(2026, 1, 31), 1) == date(2026, 3, 2)
    assert sumar_meses(date(2026, 10, 31), 1) == date(2026, 11, 30)
    assert sumar_meses(date(2026, 11, 7), 1) == date(2026, 12, 7)
    assert numero_texto("veinte") == 20 and numero_texto("treinta y cinco") == 35 and numero_texto("15") == 15
    assert numero_texto("muchos") is None


def test_llm_answer_gate() -> None:
    texto = "Primero. Beneficiarios. Podrán ser beneficiarias las personas físicas residentes en España."
    buena = {"destinatario": "personas", "temas": ["estudios", "inventado"], "cita": "las personas físicas residentes en España"}
    assert aceptar_respuesta_llm(buena, texto) == ("personas", ["estudios"], "las personas físicas residentes en España")
    assert aceptar_respuesta_llm({**buena, "cita": "las familias numerosas de toda España"}, texto) is None
    assert aceptar_respuesta_llm({**buena, "destinatario": "todos"}, texto) is None
    assert aceptar_respuesta_llm(None, texto) is None


def test_llm_classifier_only_behind_the_flag_and_only_when_undecided(monkeypatch: pytest.MonkeyPatch) -> None:
    xml = (
        "<documento><metadatos><titulo>Extracto por el que se convocan ayudas de 2027</titulo></metadatos>"
        "<texto><p>Primero. Beneficiarios.</p><p>Podrán solicitarlas quienes acrediten residir en España.</p>"
        "<p>Quinto. Plazo de presentación de solicitudes.</p><p>Diez días hábiles desde el día siguiente a la "
        "publicación de este extracto.</p></texto></documento>"
    )
    item = ItemSumario(boe_id="BOE-B-2026-1", fecha=HOY, seccion="5B", departamento="X", titulo="Extracto ... convocan ayudas")
    llamadas: list[str] = []

    def falso(titulo: str, texto: str) -> dict[str, Any]:
        llamadas.append(titulo)
        return {"destinatario": "personas", "temas": [], "cita": "quienes acrediten residir en España"}

    convocatorias.registrar_clasificador_llm(falso)
    try:
        assert analizar(item, xml, HOY).destinatario == "desconocido"  # flag off
        monkeypatch.setenv("AYUDAS_CLASIFICADOR_LLM", "1")
        conv = analizar(item, xml, HOY)
        assert conv.destinatario == "personas" and conv.clasificador == "llm" and llamadas
        assert conv.plazos[0].fin == date(2026, 11, 27)
        convocatorias.registrar_clasificador_llm(lambda t, x: {"destinatario": "personas", "cita": "texto que no está en la convocatoria"})
        assert analizar(item, xml, HOY).destinatario == "desconocido"
    finally:
        convocatorias.registrar_clasificador_llm(None)


# --- BDNS adapter ----------------------------------------------------------------------------


def test_bdns_documented_format() -> None:
    datos = bdns.leer(json.loads((FIXTURES / "bdns" / "convocatoria_936148.json").read_text(encoding="utf-8")))
    assert datos is not None and datos.codigo == "936148"
    assert datos.fecha_fin == date(2026, 11, 19) and datos.abierto is True and datos.para_personas
    pagina = bdns.leer(json.loads((FIXTURES / "bdns" / "convocatoria_lista_sin_beneficiarios.json").read_text(encoding="utf-8")))
    assert pagina is not None and pagina.fecha_fin is None and pagina.fecha_inicio == date(2026, 1, 10)
    assert pagina.texto_fin == "Hasta agotar el crédito" and not pagina.solo_entidades
    assert bdns.leer({}) is None and bdns.leer({"content": []}) is None


def test_bdns_is_off_by_default_and_enriches_when_given() -> None:
    assert bdns.consultar("936148") is None  # disabled: no network access attempted
    carga = json.loads((FIXTURES / "bdns" / "convocatoria_936148.json").read_text(encoding="utf-8"))
    datos = bdns.consultar("936148", lambda _id: carga)
    assert datos is not None
    conv = _conv("20261022", "BOE-B-2026-34368").model_copy(update={"destinatario": "desconocido"})
    enriquecida = enriquecer_con_bdns(conv, datos)
    assert enriquecida.destinatario == "personas"
    assert any(p.tipo == "bdns" and p.fin == date(2026, 11, 19) and not p.estimado for p in enriquecida.plazos)
    assert bdns.consultar("1", lambda _id: (_ for _ in ()).throw(OSError("caído"))) is None


# --- Daily review and store ------------------------------------------------------------------


def test_daily_review_fills_the_store_once_a_day(_aislado: Path) -> None:
    resumen = actualizar_convocatorias(HOY, sumario_de=_sumario, xml_de=_xml, dias_max=20)
    assert resumen.errores == [] and resumen.dias_leidos[0] == date(2026, 10, 25)
    assert sorted(resumen.nuevas) == ["BOE-A-2026-23163", "BOE-B-2026-36326", "BOE-B-2026-36910", "BOE-B-2026-37127"]
    almacen = almacen_mod.cargar()
    assert almacen.revisado_hasta == HOY and almacen.ultima_revision == HOY
    assert (_aislado / almacen_mod.FICHERO).exists()
    otra = actualizar_convocatorias(HOY, sumario_de=_sumario, xml_de=_xml)
    assert otra.omitida and otra.nuevas == []
    manana = actualizar_convocatorias(date(2026, 11, 14), sumario_de=_sumario, xml_de=_xml)
    assert manana.dias_leidos == [date(2026, 11, 14)] and manana.nuevas == []


def test_a_failed_document_is_retried(_aislado: Path) -> None:
    def roto(boe_id: str) -> bytes | None:
        raise TimeoutError("lento")

    resumen = actualizar_convocatorias(date(2026, 11, 5), sumario_de=_sumario, xml_de=roto, dias_max=2)
    assert resumen.errores and date(2026, 11, 4) not in resumen.dias_leidos
    almacen = almacen_mod.cargar()
    assert almacen.ultima_revision is None and almacen.revisado_hasta is None
    otra = actualizar_convocatorias(date(2026, 11, 5), sumario_de=_sumario, xml_de=_xml, dias_max=2)
    assert otra.nuevas == ["BOE-A-2026-23163"]


def test_a_damaged_store_is_rebuilt(_aislado: Path) -> None:
    _aislado.mkdir(parents=True)
    (_aislado / almacen_mod.FICHERO).write_text("{no es json", encoding="utf-8")
    assert almacen_mod.cargar().convocatorias == {}
