"""Fact validation and proactive notices of the ``caducidades`` module."""

from datetime import date, timedelta

import pytest

from hacienda_gpt.modulos.caducidades.agenda import aviso_de, avisos_de_agenda, construir_agenda
from hacienda_gpt.modulos.caducidades.calculo import calcular_otro_documento
from hacienda_gpt.modulos.caducidades.hechos import (
    normalizar_titular,
    umbrales,
    validar_antelaciones,
    validar_compras,
    validar_contratos,
    validar_documentos,
    validar_fechas_nacimiento,
    validar_vehiculos,
)

HOY = date(2026, 11, 13)


@pytest.mark.parametrize(
    ("entrada", "salida"),
    [("Titular", "titular"), ("cónyuge", "conyuge"), ("hijo 2", "hijo_2"), ("Hijo-1", "hijo_1")],
)
def test_holders_are_normalised(entrada: str, salida: str) -> None:
    assert normalizar_titular(entrada) == salida


def test_unknown_holder_is_rejected() -> None:
    with pytest.raises(ValueError, match="titular no válido"):
        normalizar_titular("vecino")


def test_documents_get_stable_ids_and_no_identifiers_are_accepted() -> None:
    docs = validar_documentos('[{"tipo": "dni", "titular": "cónyuge", "fecha_caducidad": "2030-01-01"}]')
    assert docs == [
        {"id": "dni-conyuge", "tipo": "dni", "titular": "conyuge", "fecha_caducidad": "2030-01-01", "clases": []}
    ]
    with pytest.raises(ValueError, match="no hacen falta"):
        validar_documentos([{"tipo": "dni", "numero": "12345678Z", "fecha_caducidad": "2030-01-01"}])
    with pytest.raises(ValueError, match="no hacen falta"):
        validar_vehiculos(
            [
                {
                    "etiqueta": "coche",
                    "categoria": "M1",
                    "matricula": "0000XXX",
                    "fecha_primera_matriculacion": "2020-01-01",
                }
            ]
        )


def test_document_rules() -> None:
    with pytest.raises(ValueError, match="mismo identificador"):
        validar_documentos(
            [{"tipo": "dni", "fecha_caducidad": "2030-01-01"}, {"tipo": "dni", "fecha_caducidad": "2031-01-01"}]
        )
    with pytest.raises(ValueError, match="nombre y la fecha de caducidad"):
        validar_documentos([{"tipo": "otro", "nombre": "Tarjeta sanitaria europea"}])
    with pytest.raises(ValueError, match="clases de permiso desconocidas"):
        validar_documentos([{"tipo": "permiso_conduccion", "clases": ["Q"], "fecha_caducidad": "2030-01-01"}])
    with pytest.raises(ValueError, match="posterior a la de expedición"):
        validar_documentos([{"tipo": "pasaporte", "fecha_expedicion": "2030-01-01", "fecha_caducidad": "2029-01-01"}])
    permiso = validar_documentos(
        [{"tipo": "permiso_conduccion", "clases": ["b", "c1 + e"], "fecha_caducidad": "2030-01-01"}]
    )
    assert permiso[0]["clases"] == ["B", "C1+E"]


def test_vehicles_contracts_purchases_and_settings() -> None:
    assert (
        validar_vehiculos(
            [{"etiqueta": "Coche familiar", "categoria": "M1", "fecha_primera_matriculacion": "2020-01-01"}]
        )[0]["id"]
        == "vehiculo-coche-familiar"
    )
    with pytest.raises(ValueError, match="categoría desconocida"):
        validar_vehiculos([{"etiqueta": "x", "categoria": "Z9", "fecha_primera_matriculacion": "2020-01-01"}])
    assert (
        validar_contratos([{"tipo": "seguro", "etiqueta": "Hogar", "fecha_vencimiento": "2027-01-01"}])[0][
            "periodicidad_meses"
        ]
        == 12
    )
    compra = validar_compras([{"descripcion": "Nevera", "fecha_compra": "2026-10-01", "importe": "499.90"}])[0]
    assert compra["id"] == "compra-nevera-2026-10-01" and compra["importe"] == "499.90"
    with pytest.raises(ValueError, match="segunda mano"):
        validar_compras([{"descripcion": "x", "fecha_compra": "2026-10-01", "garantia_pactada_anos": 1}])
    assert validar_fechas_nacimiento({"Cónyuge": "1980-01-02"}) == {"conyuge": "1980-01-02"}
    with pytest.raises(ValueError):
        validar_fechas_nacimiento({"titular": "02/01/1980"})
    assert validar_antelaciones({"itv": 45}) == {"itv": 45}
    with pytest.raises(ValueError):
        validar_antelaciones({"itv": 0})
    with pytest.raises(ValueError):
        validar_antelaciones({"yate": 10})


def test_lead_times_are_configurable() -> None:
    assert umbrales("itv", {}) == (30, 7)
    assert umbrales("itv", {"itv": 45}) == (45, 30, 7)
    assert umbrales("pasaporte", {"pasaporte": 20}) == (20, 7)


def _doc(fecha: date):
    return calcular_otro_documento(id="otro-titular-tse", titulo="Tarjeta sanitaria europea", fecha_caducidad=fecha)


def test_only_the_closest_threshold_reached_is_notified_with_stable_keys() -> None:
    lejos = _doc(HOY + timedelta(days=61))
    assert aviso_de(lejos, HOY, {}) is None
    a60 = aviso_de(_doc(HOY + timedelta(days=60)), HOY, {})
    assert a60 is not None and a60.clave == f"caducidades:otro-titular-tse:{(HOY + timedelta(days=60)).isoformat()}:60d"
    assert a60.gravedad == "info"
    a10 = aviso_de(_doc(HOY + timedelta(days=10)), HOY, {})
    assert a10 is not None and a10.clave.endswith(":30d") and a10.gravedad == "aviso"
    a3 = aviso_de(_doc(HOY + timedelta(days=3)), HOY, {})
    assert a3 is not None and a3.clave.endswith(":7d") and a3.gravedad == "urgente"
    assert a3.sustituye_prefijo == "caducidades:otro-titular-tse:"
    vencido = aviso_de(_doc(HOY - timedelta(days=1)), HOY, {})
    assert vencido is not None and vencido.clave.endswith(":vencido") and vencido.gravedad == "urgente"


PERFIL = {
    "caducidades_documentos": [
        {"tipo": "pasaporte", "titular": "titular", "fecha_caducidad": "2027-01-20"},
        {"tipo": "dni", "titular": "hijo 1", "fecha_expedicion": "2022-01-10"},
        {"tipo": "dni", "titular": "titular", "fecha_expedicion": "2026-06-01"},
    ],
    "caducidades_fechas_nacimiento": {"titular": "1956-06-01"},
    "descendientes": [{"edad": 11, "fecha_nacimiento": "2015-03-03"}],
    "caducidades_vehiculos": [
        {
            "etiqueta": "coche",
            "categoria": "M1",
            "fecha_primera_matriculacion": "2016-05-10",
            "fecha_ultima_itv": "2025-12-01",
        }
    ],
    "caducidades_compras": [
        {"descripcion": "Auriculares", "fecha_compra": "2026-11-06", "modalidad": "distancia"},
        {"descripcion": "Horno", "fecha_compra": "2023-12-01"},
    ],
}


def test_household_notices_are_deterministic_and_idempotent() -> None:
    primera = avisos_de_agenda(PERFIL, HOY)
    segunda = avisos_de_agenda(PERFIL, HOY)
    assert primera == segunda
    claves = {a.clave for a in primera}
    assert len(claves) == len(primera)
    # Passport printed date 20/01/2027: 68 days -> the 90-day notice.
    assert "caducidades:pasaporte-titular:2027-01-20:90d" in claves
    # Child's DNI from descendientes' birth date: issued at 6 -> five years -> 10/01/2027 (58 days, 60-day notice).
    assert "caducidades:dni-hijo_1:2027-01-10:60d" in claves
    # Online purchase delivered 06/11/2026: withdrawal until 20/11/2026 (7 days).
    assert "caducidades:compra-auriculares-2026-11-06:desistimiento:2026-11-20:7d" in claves
    # Oven delivered 01/12/2023: guarantee until 01/12/2026 (18 days).
    assert "caducidades:compra-horno-2023-12-01:garantia:2026-12-01:30d" in claves
    # Permanent DNI (renewed at 70) and a car whose ITV is a year away: no notice.
    assert not any("dni-titular" in c for c in claves)
    assert not any("vehiculo-coche" in c for c in claves)


def test_invalid_stored_facts_are_reported_not_raised() -> None:
    agenda = construir_agenda(
        {"caducidades_documentos": "no es json", "caducidades_vehiculos": [{"etiqueta": "x"}]}, HOY
    )
    assert len(agenda.pendientes) == 2 and agenda.vencimientos == []


def test_missing_birth_date_is_a_pending_item() -> None:
    agenda = construir_agenda(
        {"caducidades_documentos": [{"tipo": "dni", "titular": "conyuge", "fecha_expedicion": "2020-01-01"}]}, HOY
    )
    assert agenda.vencimientos == [] and "cónyuge" in agenda.pendientes[0]
