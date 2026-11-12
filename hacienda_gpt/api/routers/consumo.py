"""``GET /usage``: what the service has spent on LLM calls."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

# The dependency lives in api.security (api.api only re-imports it), so there is
# no import cycle with the module that includes this router.
from hacienda_gpt.api.security import require_api_key
from hacienda_gpt.ops.consumo import (
    ConsumoStore,
    Presupuesto,
    ResumenConsumo,
    almacen_por_defecto,
    hoy_madrid,
)

router = APIRouter(tags=["consumo"])

DIAS = 30


class LimitesConsumo(BaseModel):
    """The configured limits; None = no limit."""

    model_config = ConfigDict(extra="forbid")

    diario_usd: Decimal | None = None
    diario_por_usuario_usd: Decimal | None = None
    max_llamadas_por_pregunta: int | None = None


class UsageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Calendar days in Europe/Madrid.
    hoy: ResumenConsumo
    ultimos_30_dias: ResumenConsumo
    limites: LimitesConsumo
    precios_consultados_el: date
    nota: str


def get_consumo_store() -> ConsumoStore:
    """FastAPI dependency (overridable in tests); a failed open is a 503."""
    try:
        return almacen_por_defecto()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"El registro de consumo no está disponible: {exc}") from exc


@router.get("/usage", response_model=UsageResponse, dependencies=[Depends(require_api_key)])
def get_usage(store: ConsumoStore = Depends(get_consumo_store)) -> UsageResponse:
    """Totals of today and of the last 30 days (today included), overall and by purpose.

    Operator-level figures for the whole service, not per user: keep the route
    behind the API key.
    """
    hoy = hoy_madrid()
    try:
        presupuesto = Presupuesto.desde_settings(store)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return UsageResponse(
        hoy=store.resumen(desde=hoy, hasta=hoy, por=("proposito",)),
        ultimos_30_dias=store.resumen(desde=hoy - timedelta(days=DIAS - 1), hasta=hoy, por=("proposito",)),
        limites=LimitesConsumo(
            diario_usd=presupuesto.diario_usd,
            diario_por_usuario_usd=presupuesto.diario_por_usuario_usd,
            max_llamadas_por_pregunta=presupuesto.max_llamadas_por_pregunta,
        ),
        precios_consultados_el=store.precios().consultado_el,
        nota=(
            "Los costes son una estimación con la tabla de precios de la fecha indicada. Las llamadas a modelos "
            "sin precio conocido no suman coste (llamadas_sin_coste) ni cuentan para los límites diarios."
        ),
    )
