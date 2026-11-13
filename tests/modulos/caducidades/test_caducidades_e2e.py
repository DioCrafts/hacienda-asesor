"""End to end: a realistic household profile, every tool of the module and its daily job."""

from datetime import date
from typing import Any

from hacienda_gpt.modulos.caducidades import MODULO
from hacienda_gpt.modulos.contrato import ContextoModulo, Modulo

HOY = date(2026, 11, 13)

PERFIL: dict[str, Any] = {
    # Shared facts of the tax profile, read but not redefined.
    "comunidad_autonoma": "Madrid",
    "estado_civil": "casado",
    "edad": 46,
    "num_descendientes": 2,
    "descendientes": [{"edad": 11, "fecha_nacimiento": "2015-03-03"}, {"edad": 4, "fecha_nacimiento": "2022-05-20"}],
    # The module's own facts.
    "caducidades_fechas_nacimiento": {"titular": "1980-04-02", "cónyuge": "1961-01-15"},
    "caducidades_documentos": [
        {"tipo": "dni", "titular": "titular", "fecha_expedicion": "2017-01-20"},
        {"tipo": "dni", "titular": "cónyuge", "fecha_caducidad": "2031-01-15"},
        {"tipo": "pasaporte", "titular": "hijo 1", "fecha_expedicion": "2023-02-01"},
        {"tipo": "pasaporte", "titular": "hijo 2", "fecha_expedicion": "2025-07-01"},
        {"tipo": "permiso_conduccion", "titular": "cónyuge", "fecha_expedicion": "2017-02-10", "clases": ["B"]},
        {"tipo": "certificado_electronico", "titular": "titular", "fecha_caducidad": "2026-12-10"},
        {"tipo": "otro", "titular": "titular", "nombre": "Tarjeta sanitaria europea", "fecha_caducidad": "2027-03-31"},
    ],
    "caducidades_vehiculos": [
        {
            "etiqueta": "coche familiar",
            "categoria": "M1",
            "fecha_primera_matriculacion": "2016-12-01",
            "fecha_ultima_itv": "2024-12-01",
        },
        {"etiqueta": "moto", "categoria": "L", "fecha_primera_matriculacion": "2022-12-05"},
    ],
    "caducidades_contratos": [
        {"tipo": "seguro", "etiqueta": "seguro del coche", "fecha_vencimiento": "2027-01-05"},
        {"tipo": "otro", "etiqueta": "alarma", "fecha_vencimiento": "2027-02-01", "preaviso_dias": 30},
    ],
    "caducidades_compras": [{"descripcion": "Portátil", "fecha_compra": "2026-11-04", "modalidad": "distancia"}],
    "caducidades_antelaciones": {"itv": 45},
}


def _ctx(registrar: Any = None) -> ContextoModulo:
    return ContextoModulo(perfil=PERFIL, hoy=HOY, case_id="caso-1", registrar=registrar)


def _tool(nombre: str):
    return next(h for h in MODULO.herramientas if h.spec.name == nombre)


def test_module_satisfies_the_contract() -> None:
    assert isinstance(MODULO, Modulo)
    assert MODULO.clave == "caducidades"
    assert all(h.nombre.startswith("caducidades_") for h in MODULO.hechos)
    assert all(h.spec.name.startswith("caducidades_") for h in MODULO.herramientas)
    assert {h.spec.name for h in MODULO.herramientas} == {
        "caducidades_proximas",
        "caducidades_calcular",
        "caducidades_registrar_compra",
        "caducidades_leer_ticket",
    }
    assert all(h.efecto != "actua" for h in MODULO.herramientas)
    assert {f.identificador for f in MODULO.fuentes} >= {"BOE-A-2005-21163", "BOE-A-2017-12841", "BOE-A-2007-20555"}
    for hecho in MODULO.hechos:
        assert hecho.validar is not None and hecho.validar(PERFIL[hecho.nombre]) is not None


def test_proximas_lists_everything_by_date_with_what_to_do() -> None:
    r = _tool("caducidades_proximas").ejecutar({}, _ctx())
    assert not r.es_error
    texto = r.contenido
    orden = [
        "Devolución (desistimiento) de «Portátil»",  # 18/11/2026
        "ITV de «moto»",  # 05/12/2026 (first at four years)
        "Renovación de «seguro del coche»",  # last day to oppose 05/12/2026
        "Certificado electrónico (titular)",  # 10/12/2026
        "ITV de «coche familiar»",  # 01/12/2024 at age 8: biennial -> 01/12/2026
        "Renovación de «alarma»",  # 02/01/2027
        "DNI (titular)",  # 20/01/2027 (renewed at 36: ten years)
        "Permiso de conducción (cónyuge)",  # 10/02/2027 (issued at 56: ten years)
        "Tarjeta sanitaria europea (titular)",
    ]
    assert all(t in texto for t in orden)
    # Same-day items keep a stable order; check the ones on distinct dates.
    posiciones = [texto.index(t) for t in orden]
    assert posiciones[0] < posiciones[3] < posiciones[5] < posiciones[6] < posiciones[7] < posiciones[8]
    assert "01/12/2026" in texto and "Jefatura Provincial de Tráfico" in texto
    assert "https://www.boe.es/buscar/act.php?id=BOE-A-2005-21163" in r.citas


def test_calcular_returns_the_rule_with_its_quote() -> None:
    r = _tool("caducidades_calcular").ejecutar(
        {"regla": "dni", "titular": "titular", "fecha_expedicion": "2009-04-01"}, _ctx()
    )
    assert not r.es_error
    assert "01/04/2014" in r.contenido  # renewed at 28: five years
    assert "no haya alcanzado los treinta al momento de la expedición o renovación" in r.contenido
    r = _tool("caducidades_calcular").ejecutar(
        {"regla": "permiso_conduccion", "titular": "cónyuge", "fecha_expedicion": "2026-01-15", "clases": ["B"]}, _ctx()
    )
    assert "15/01/2031" in r.contenido  # 65th birthday: five years
    r = _tool("caducidades_calcular").ejecutar(
        {"regla": "garantia", "descripcion": "Nevera", "fecha_compra": "2026-01-10"}, _ctx()
    )
    assert "10/01/2029" in r.contenido and "tres años desde la entrega" in r.contenido


def test_calcular_errors_are_results_not_exceptions() -> None:
    tool = _tool("caducidades_calcular")
    assert tool.ejecutar({"regla": "yate"}, _ctx()).es_error
    assert tool.ejecutar({"regla": "dni", "fecha_expedicion": "01/02/2020"}, _ctx()).es_error
    assert tool.ejecutar({"regla": "dni", "fecha_expedicion": "2020-02-01"}, _ctx()).es_error
    assert tool.ejecutar({"regla": "itv", "categoria": "M1"}, _ctx()).es_error
    assert tool.ejecutar(
        {"regla": "permiso_conduccion", "clases": ["Z"], "fecha_caducidad": "2030-01-01"}, _ctx()
    ).es_error


def test_registrar_compra_saves_and_returns_the_dates() -> None:
    guardado: dict[str, Any] = {}
    r = _tool("caducidades_registrar_compra").ejecutar(
        {
            "descripcion": "Lavadora",
            "fecha_compra": "2026-11-08",
            "fecha_entrega": "2026-11-10",
            "modalidad": "distancia",
            "importe": "449.00",
        },
        _ctx(registrar=guardado.update),
    )
    assert not r.es_error
    assert "24/11/2026" in r.contenido  # withdrawal: 14 days from 10/11
    assert "10/11/2029" in r.contenido  # guarantee: 3 years from delivery
    compras = guardado["caducidades_compras"]
    assert [c["descripcion"] for c in compras] == ["Portátil", "Lavadora"]
    sin_guardar = _tool("caducidades_registrar_compra").ejecutar(
        {"descripcion": "Lavadora", "fecha_compra": "2026-11-08"}, _ctx()
    )
    assert "No se ha guardado" in sin_guardar.contenido
    assert _tool("caducidades_registrar_compra").ejecutar({"descripcion": "x"}, _ctx()).es_error


def test_leer_ticket_proposes_without_saving() -> None:
    texto = "TIENDA\nFecha 08/11/2026\nAURICULARES BT 59,90\nTOTAL 59,90\n"
    r = _tool("caducidades_leer_ticket").ejecutar(
        {
            "texto": texto,
            "propuesta": [{"campo": "descripcion", "valor": "Auriculares BT", "evidencia": "AURICULARES BT 59,90"}],
        },
        _ctx(),
    )
    assert not r.es_error
    assert "fecha_compra: 2026-11-08" in r.contenido and "descripcion: Auriculares BT" in r.contenido
    assert "caducidades_registrar_compra" in r.contenido
    assert _tool("caducidades_leer_ticket").ejecutar({"texto": ""}, _ctx()).es_error


def test_daily_job() -> None:
    avisos = MODULO.avisos(_ctx())
    assert avisos == MODULO.avisos(_ctx())
    claves = {a.clave for a in avisos}
    # ITV lead time raised to 45 days by the user: the moto (05/12/2026, 22 days) gets the 30-day notice.
    assert "caducidades:vehiculo-moto:itv:2026-12-05:30d" in claves
    assert "caducidades:compra-portatil-2026-11-04:desistimiento:2026-11-18:7d" in claves
    assert all(a.vence is not None for a in avisos)
