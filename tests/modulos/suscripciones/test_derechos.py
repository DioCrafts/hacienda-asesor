"""Self-check of the consumer-rights data against the BOE extracts it was read from."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from hacienda_gpt.modulos.suscripciones.derechos import (
    DerechosConsumidor,
    cargar,
    cifra_a_decimal,
    comprobar,
    main,
)
from hacienda_gpt.tax.autonomos_params import textos_de_directorio

EXTRACTOS = Path(__file__).resolve().parents[2] / "fixtures" / "modulos" / "suscripciones" / "boe"


def test_cada_cita_es_literal_y_cada_cifra_esta_en_su_cita() -> None:
    datos = cargar()
    assert comprobar(datos, textos_de_directorio(EXTRACTOS)) == []


def test_los_extractos_cubren_todas_las_fuentes() -> None:
    datos = cargar()
    assert set(textos_de_directorio(EXTRACTOS)) == set(datos.fuentes_principales)
    for fuente in datos.fuentes_principales.values():
        assert fuente.url == f"https://www.boe.es/buscar/act.php?id={fuente.boe_id}"
        assert len(fuente.sha256_html) == 64


def test_cifras_leidas() -> None:
    datos = cargar()
    assert datos.entero("desistimiento", "plazo_dias") == 14
    assert datos.entero("aviso_renovacion", "antelacion_dias") == 15
    assert datos.entero("seguro_oposicion_prorroga", "preaviso_tomador_meses") == 1
    assert datos.entero("seguro_oposicion_prorroga", "aviso_modificacion_meses") == 2
    assert datos.entero("telecom_duracion_y_baja", "antelacion_baja_dias_habiles") == 2
    assert datos.entero("telecom_duracion_y_baja", "duracion_maxima_meses") == 24
    assert datos.entero("devolucion_adeudos", "plazo_devolucion_semanas") == 8
    assert datos.entero("atencion_clientela_ley_10_2025", "plazo_cobros_indebidos_dias") == 5
    assert datos.entero("atencion_clientela_ley_10_2025", "meses_adaptacion") == 12


def test_la_comprobacion_detecta_errores() -> None:
    datos = cargar()
    crudo = datos.model_dump(mode="json")
    crudo["derechos"]["desistimiento"]["valores"]["plazo_dias"] = "15"
    crudo["derechos"]["prorroga_automatica"]["evidencia"][1]["cita"] += " (inventado)"
    crudo["derechos"]["aviso_renovacion"]["valores"]["sin_cita"] = "3"
    alterado = DerechosConsumidor.model_validate(crudo)
    problemas = comprobar(alterado, textos_de_directorio(EXTRACTOS))
    assert any("desistimiento.plazo_dias" in p for p in problemas)
    assert any("prorroga_automatica" in p and "literal" in p for p in problemas)
    assert any("sin_cita: cifra sin evidencia" in p for p in problemas)


def test_cifra_a_decimal() -> None:
    assert cifra_a_decimal("catorce días naturales") == Decimal(14)
    assert cifra_a_decimal("un mes") == Decimal(1)
    assert cifra_a_decimal("veinticuatro meses") == Decimal(24)
    assert cifra_a_decimal("250 personas") == Decimal(250)


def test_cli(capsys) -> None:  # type: ignore[no-untyped-def]
    assert main([str(EXTRACTOS)]) == 0
    assert "coherentes" in capsys.readouterr().out
    assert main([]) == 2
