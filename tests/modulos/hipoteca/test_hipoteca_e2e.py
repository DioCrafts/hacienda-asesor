"""End to end: the module as the core sees it, a realistic household profile, the real BOE series."""

from __future__ import annotations

from datetime import date
import json

from hacienda_gpt.modulos.contrato import ContextoModulo, Modulo
from hacienda_gpt.modulos.hipoteca import MODULO

PERFIL = {
    "comunidad_autonoma": "Madrid",
    "estado_civil": "casado",
    "num_descendientes": 2,
    "hipoteca_prestamo": {
        "capital_inicial": "180.000,00",
        "fecha_firma": "2019-09-20",
        "plazo_meses": 300,
        "tipo": "variable",
        "tipo_inicial": "1,25",
        "meses_tipo_inicial": 12,
        "indice": "euribor_12m",
        "diferencial": "0,89",
        "revision_meses": 12,
        "bonificaciones": [{"concepto": "Nómina", "reduccion_diferencial": "0,10", "coste_anual": "0"}],
        "amortizaciones_realizadas": [{"fecha": "2024-03-20", "importe": "6000", "modalidad": "plazo"}],
        "vivienda_habitual": True,
        "adquisicion_antes_2013": False,
        "comprobacion_banco": [{"fecha": "2025-10-20", "cuota": "700"}],
    },
}
ARGS = {
    "hipoteca_cuadro": {"cuotas": 3},
    "hipoteca_revision": {},
    "hipoteca_amortizar": {"importe": 15000, "rentabilidad_alternativa_pct": 2.5},
    "hipoteca_cambiar_banco": {
        "modalidad": "subrogacion",
        "oferta": {"tipo": "mixto", "tipo_interes": 2.4, "meses_tipo_inicial": 60, "diferencial": 0.7},
        "gastos": {"tasacion": 350},
    },
}


def test_modulo_cumple_el_contrato():
    assert isinstance(MODULO, Modulo)
    assert MODULO.clave == "hipoteca"
    nombres = [x.spec.name for x in MODULO.herramientas]
    assert nombres == ["hipoteca_cuadro", "hipoteca_revision", "hipoteca_amortizar", "hipoteca_cambiar_banco"]
    assert all(x.efecto == "lectura" for x in MODULO.herramientas)
    assert all(h.nombre.startswith("hipoteca_") for h in MODULO.hechos)
    assert next(h for h in MODULO.hechos if h.nombre == "hipoteca_prestamo").sensible
    for x in MODULO.herramientas:
        json.dumps(x.spec.input_schema)
        assert x.spec.input_schema["type"] == "object"
    assert {f.identificador for f in MODULO.fuentes} >= {"BOE-A-2019-3814", "BOE-A-1994-7556", "BOE-A-2011-17015"}


def test_validar_el_hecho_desde_el_perfil():
    hecho = next(h for h in MODULO.hechos if h.nombre == "hipoteca_prestamo")
    assert hecho.validar is not None
    canon = hecho.validar(PERFIL["hipoteca_prestamo"])
    assert canon["capital_inicial"] == "180000.00" and canon["diferencial"] == "0.89"


def test_todas_las_herramientas_con_un_perfil_real():
    contexto = ContextoModulo(perfil=PERFIL, hoy=date(2026, 2, 10), case_id="caso-1")
    for x in MODULO.herramientas:
        r = x.ejecutar(ARGS[x.spec.name], contexto)
        assert not r.es_error, (x.spec.name, r.contenido)
        assert r.resumen and r.contenido
        assert all(c.startswith("https://www.boe.es/") for c in r.citas)
    cuadro = MODULO.herramientas[0].ejecutar(ARGS["hipoteca_cuadro"], contexto).contenido
    assert "Comprobación con las cifras de tu banco" in cuadro
    revision = MODULO.herramientas[1].ejecutar({}, contexto).contenido
    # Review 20/09/2026 uses the Euríbor of July 2026, not published on 10/02/2026 -> an estimate.
    assert "todavía no está publicado" in revision
    amortizar = MODULO.herramientas[2].ejecutar(ARGS["hipoteca_amortizar"], contexto).contenido
    assert "pasados 5 años desde la firma: la ley no permite comisión" in amortizar


def test_avisos_del_modulo_deterministas():
    contexto = ContextoModulo(perfil=PERFIL, hoy=date(2026, 8, 20))
    avisos = MODULO.avisos(contexto)
    assert avisos == MODULO.avisos(contexto)
    revision = [a for a in avisos if a.clave.startswith("hipoteca:revision:2026-09-20:")]
    assert len(revision) == 1
    # The rule takes July 2026 (two months before September), published at the start of August.
    assert "julio de 2026" in revision[0].cuerpo and revision[0].fuentes[0].startswith("https://www.boe.es/")
