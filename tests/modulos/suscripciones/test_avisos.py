"""Proactive notices: renewals, price increases, duplicates and trials; deterministic and idempotent."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from hacienda_gpt.modulos.contrato import ContextoModulo
from hacienda_gpt.modulos.suscripciones import MODULO
from hacienda_gpt.modulos.suscripciones.almacen import NOMBRE_HECHO
from hacienda_gpt.modulos.suscripciones.avisos import dias_aviso
from hacienda_gpt.modulos.suscripciones.modelos import ServicioGuardado, ServiciosGuardados


def _servicio(**cambios: Any) -> ServicioGuardado:
    base: dict[str, Any] = {
        "id": "srv-00000000aa",
        "etiqueta": "Seguro Hogar",
        "categoria": "seguros",
        "sector": "seguros",
        "periodicidad": "anual",
        "importe": Decimal("300.00"),
        "medio": "adeudo",
        "primera_fecha": date(2025, 12, 20),
        "ultima_fecha": date(2025, 12, 20),
        "proxima_fecha": date(2026, 12, 20),
        "estado": "activa",
        "confianza": Decimal("0.90"),
        "num_cargos": 2,
    }
    base.update(cambios)
    return ServicioGuardado.model_validate(base)


def _avisos(hoy: date, *servicios: ServicioGuardado):
    lista = ServiciosGuardados(actualizado_el=hoy, servicios=list(servicios)).model_dump(mode="json")
    return MODULO.avisos(ContextoModulo(perfil={NOMBRE_HECHO: lista}, hoy=hoy))


def test_renovacion_de_seguro_un_mes_antes() -> None:
    servicio = _servicio()
    assert _avisos(date(2026, 10, 20), servicio) == []  # 31 days before the deadline
    (aviso,) = _avisos(date(2026, 10, 25), servicio)
    assert aviso.clave == "suscripciones:renovacion:srv-00000000aa:2026-12-20"
    assert aviso.vence == date(2026, 11, 20) and aviso.gravedad == "aviso"
    assert "al menos 1 mes antes" in aviso.cuerpo and "art. 22.2 Ley 50/1980" in aviso.cuerpo
    (urgente,) = _avisos(date(2026, 11, 15), servicio)
    assert urgente.gravedad == "urgente" and urgente.clave == aviso.clave
    (pasado,) = _avisos(date(2026, 12, 1), servicio)
    assert "ya ha pasado" in pasado.cuerpo
    assert _avisos(date(2026, 12, 21), servicio) == []


def test_renovacion_general_y_telecom() -> None:
    general = _servicio(sector="general", categoria="comercio_online", etiqueta="Amazon Prime")
    (aviso,) = _avisos(date(2026, 11, 25), general)
    assert aviso.vence == date(2026, 12, 20) and "15 días de antelación" in aviso.cuerpo
    telecom = _servicio(sector="telecomunicaciones", categoria="telecomunicaciones", etiqueta="Fibra")
    (aviso_t,) = _avisos(date(2026, 12, 1), telecom)
    assert aviso_t.vence == date(2026, 12, 17)  # 20/12/2026 is a Sunday: Friday 18 and Thursday 17


def test_mensuales_sin_aviso_de_renovacion() -> None:
    mensual = _servicio(periodicidad="mensual", proxima_fecha=date(2026, 11, 20), sector="general")
    assert _avisos(date(2026, 11, 13), mensual) == []


def test_subida_de_precio_reciente() -> None:
    subida = {"fecha": date(2026, 10, 5), "anterior": "12.99", "nuevo": "13.99", "porcentaje": "7.70"}
    servicio = _servicio(
        periodicidad="mensual", sector="general", categoria="streaming_video", etiqueta="Netflix", cambio_precio=subida
    )
    (aviso,) = _avisos(date(2026, 11, 13), servicio)
    assert aviso.clave == "suscripciones:subida:srv-00000000aa:2026-10-05"
    assert aviso.importe == Decimal("12.00") and "+7,70 %" in aviso.titulo and "art. 85.10" in aviso.cuerpo
    assert _avisos(date(2027, 3, 1), servicio) == []  # more than 120 days later
    bajada = dict(subida, nuevo="11.99", porcentaje="-7.70")
    con_bajada = _servicio(periodicidad="mensual", sector="general", cambio_precio=bajada)
    assert _avisos(date(2026, 11, 13), con_bajada) == []


def test_duplicados() -> None:
    doble = {"tipo": "cargo_doble", "fecha": date(2026, 10, 2), "importe": "29.99", "detalle": "dos cargos"}
    servicio = _servicio(periodicidad="mensual", sector="general", etiqueta="Gimnasio", duplicado=doble)
    (aviso,) = _avisos(date(2026, 11, 13), servicio)
    assert aviso.vence == date(2026, 11, 27)  # 8 weeks for a direct debit
    assert "art. 49.1" in aviso.cuerpo
    tarjeta = _servicio(periodicidad="mensual", sector="general", medio="tarjeta", duplicado=doble)
    (aviso_t,) = _avisos(date(2026, 11, 13), tarjeta)
    assert aviso_t.vence is None and "art. 43.1" in aviso_t.cuerpo
    viejo = {"tipo": "servicio_duplicado", "fecha": date(2025, 1, 1), "importe": "10.99", "detalle": "también x"}
    (sigue,) = _avisos(date(2026, 11, 13), _servicio(periodicidad="mensual", sector="general", duplicado=viejo))
    assert sigue.clave.endswith("servicio_duplicado:2025-01-01")


def test_prueba_convertida() -> None:
    prueba = {"fecha_prueba": date(2026, 9, 1), "importe_prueba": "0.99", "fecha_primer_pago": date(2026, 10, 1)}
    servicio = _servicio(periodicidad="mensual", sector="general", etiqueta="DAZN", prueba=prueba)
    (aviso,) = _avisos(date(2026, 11, 13), servicio)
    assert aviso.gravedad == "info" and aviso.clave == "suscripciones:prueba:srv-00000000aa:2026-10-01"


def test_inactivas_y_perfiles_vacios_o_corruptos() -> None:
    assert _avisos(date(2026, 11, 25), _servicio(estado="inactiva", proxima_fecha=None)) == []
    assert MODULO.avisos(ContextoModulo(perfil={}, hoy=date(2026, 11, 13))) == []
    assert MODULO.avisos(ContextoModulo(perfil={NOMBRE_HECHO: "{"}, hoy=date(2026, 11, 13))) == []


def test_idempotentes() -> None:
    servicio = _servicio()
    primero = _avisos(date(2026, 11, 1), servicio)
    assert primero == _avisos(date(2026, 11, 1), servicio)
    assert [a.clave for a in primero] == [a.clave for a in _avisos(date(2026, 11, 5), servicio)]


def test_dias_de_aviso_configurables(monkeypatch: pytest.MonkeyPatch) -> None:
    assert dias_aviso() == 30
    monkeypatch.setenv("SUSCRIPCIONES_DIAS_AVISO_RENOVACION", "60")
    assert dias_aviso() == 60
    assert len(_avisos(date(2026, 9, 25), _servicio())) == 1
    monkeypatch.setenv("SUSCRIPCIONES_DIAS_AVISO_RENOVACION", "mucho")
    assert dias_aviso() == 30
