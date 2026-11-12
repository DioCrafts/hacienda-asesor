"""The self-employed router, mounted on a bare FastAPI app (the main app wires it separately)."""

from decimal import Decimal

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from hacienda_gpt.api.routers.autonomos import errores_en_espanol, router

D = Decimal


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("HACIENDA_API_KEY", raising=False)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_available_years(client: TestClient) -> None:
    assert client.get("/autonomos/ejercicios").json() == {"ejercicios": [2026]}


def test_actividad_returns_the_net_income_with_lines_and_warnings(client: TestClient) -> None:
    response = client.post(
        "/autonomos/actividad",
        json={"ejercicio": 2026, "ingresos_integros": 40000, "seguridad_social_autonomo": 3600, "alquileres": 6400},
    )
    assert response.status_code == 200
    body = response.json()
    # 40,000 - 10,000 = 30,000; 5 % = 1,500; net 28,500
    assert D(body["rendimiento_actividad_neto"]) == D("28500.00")
    assert D(body["gastos_dificil_justificacion"]) == D("1500.00")
    assert any(line["concepto"].startswith("Provisiones deducibles") for line in body["lineas"])
    assert any("no valora si son deducibles" in aviso for aviso in body["avisos"])
    assert "BOE-A-2006-20764" in body["fuente"]


def test_modelo_130_and_303(client: TestClient) -> None:
    response = client.post(
        "/autonomos/modelo-130",
        json={
            "ejercicio": 2026,
            "trimestre": 1,
            "ingresos_acumulados": "10000",
            "gastos_acumulados": "3000",
            "retenciones_acumuladas": "600",
        },
    )
    assert response.status_code == 200
    # 7,000 - 350 = 6,650; 20 % = 1,330; - 600 = 730
    assert D(response.json()["resultado"]) == D("730.00")
    assert response.json()["obligado_a_pago_fraccionado"] is None

    response = client.post(
        "/autonomos/modelo-303",
        json={"ejercicio": 2026, "trimestre": 4, "base_tipo_general": 1000, "cuotas_soportadas_deducibles": 500},
    )
    assert response.status_code == 200
    body = response.json()
    # 210 - 500 = -290, last quarter: refund can be requested
    assert D(body["resultado"]) == D("-290.00")
    assert body["tipo_resultado"] == "a_compensar" and body["puede_solicitar_devolucion"] is True


def test_validation_errors_are_422_in_spanish(client: TestClient) -> None:
    response = client.post("/autonomos/actividad", json={"ejercicio": 2026, "ingresos_integros": -5, "sueldo": 1})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "ingresos_integros: debe ser mayor o igual que 0" in detail
    assert "sueldo: campo desconocido" in detail

    detail = client.post("/autonomos/modelo-130", json={"ejercicio": 2026}).json()["detail"]
    assert "trimestre: falta este dato" in detail
    detail = client.post("/autonomos/modelo-303", json={"ejercicio": 2026, "trimestre": 5}).json()["detail"]
    assert "trimestre: debe ser menor o igual que 4" in detail
    detail = client.post(
        "/autonomos/modelo-303", json={"ejercicio": 2026, "trimestre": 1, "base_tipo_general": "mucho"}
    ).json()["detail"]
    assert "base_tipo_general: debe ser un número" in detail
    # The models' own rules arrive as written
    response = client.post(
        "/autonomos/modelo-130", json={"ejercicio": 2026, "trimestre": 1, "pagos_trimestres_anteriores": 100}
    )
    assert response.status_code == 422
    assert "En el primer trimestre no hay pagos fraccionados" in response.json()["detail"]
    assert "Value error" not in response.json()["detail"]
    # Not JSON at all
    response = client.post("/autonomos/actividad", content="{", headers={"Content-Type": "application/json"})
    assert response.status_code == 422 and "no es JSON válido" in response.json()["detail"]


def test_year_without_parameters_is_a_readable_422(client: TestClient) -> None:
    response = client.post("/autonomos/actividad", json={"ejercicio": 2019, "ingresos_integros": 1000})
    assert response.status_code == 422
    assert "No hay parámetros de autónomos cargados para 2019" in response.json()["detail"]


def test_api_key_is_enforced_when_configured(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HACIENDA_API_KEY", "s3cret")
    payload = {"ejercicio": 2026, "trimestre": 1}
    assert client.post("/autonomos/modelo-303", json=payload).status_code == 401
    assert client.get("/autonomos/ejercicios").status_code == 401
    assert client.post("/autonomos/modelo-303", json=payload, headers={"X-API-Key": "s3cret"}).status_code == 200


def test_unknown_error_types_still_give_a_sentence() -> None:
    text = errores_en_espanol([{"type": "weird", "loc": ("body", "x"), "msg": "nope"}])
    assert text == "Datos no válidos: x: valor no válido (nope)."
