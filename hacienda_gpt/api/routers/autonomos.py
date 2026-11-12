"""Self-employed calculators over HTTP: net income of the activity, modelo 130
and modelo 303.

Thin wrappers around the deterministic calculators of ``hacienda_gpt.tax`` —
no LLM, no case state. Mount with::

    from hacienda_gpt.api.routers.autonomos import router as autonomos_router
    app.include_router(autonomos_router)

Validation errors come back as 422 with a single Spanish sentence per problem
in ``detail`` (a string, like ``/simulate``), so the frontend can show them
as they are.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Sequence
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict

from hacienda_gpt.api.security import require_api_key
from hacienda_gpt.tax.actividad import ActividadInput, ResultadoActividad, calcular_rendimiento_actividad
from hacienda_gpt.tax.autonomos_params import ejercicios_autonomos_disponibles
from hacienda_gpt.tax.iva import Modelo303Input, ResultadoModelo303, calcular_modelo_303
from hacienda_gpt.tax.pagos_fraccionados import Modelo130Input, ResultadoModelo130, calcular_modelo_130
from hacienda_gpt.tax.params import EjercicioNoDisponibleError

# Pydantic error type -> Spanish sentence ({ctx} values of the error are available).
_MENSAJES: dict[str, str] = {
    "missing": "falta este dato",
    "extra_forbidden": "campo desconocido",
    "greater_than_equal": "debe ser mayor o igual que {ge}",
    "less_than_equal": "debe ser menor o igual que {le}",
    "greater_than": "debe ser mayor que {gt}",
    "less_than": "debe ser menor que {lt}",
    "decimal_parsing": "debe ser un número",
    "decimal_type": "debe ser un número",
    "decimal_max_digits": "tiene demasiadas cifras",
    "finite_number": "debe ser un número finito",
    "int_parsing": "debe ser un número entero",
    "int_type": "debe ser un número entero",
    "int_from_float": "debe ser un número entero",
    "bool_parsing": "debe ser verdadero o falso",
    "bool_type": "debe ser verdadero o falso",
    "json_invalid": "el cuerpo de la petición no es JSON válido",
    "model_attributes_type": "debe ser un objeto con los datos del cálculo",
    "dict_type": "debe ser un objeto con los datos del cálculo",
}


def errores_en_espanol(errors: Sequence[Any]) -> str:
    """One readable Spanish sentence for the validation errors of a request."""
    problemas: list[str] = []
    for error in errors:
        campo = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
        tipo = str(error.get("type", ""))
        if tipo == "value_error":
            # Raised by the models' own validators, already written in Spanish.
            mensaje = str(error.get("msg", "")).removeprefix("Value error, ")
        elif tipo in _MENSAJES:
            mensaje = _MENSAJES[tipo].format_map({k: str(v) for k, v in (error.get("ctx") or {}).items()})
        else:
            mensaje = f"valor no válido ({error.get('msg', tipo)})"
        problemas.append(f"{campo}: {mensaje}" if campo else mensaje)
    return "Datos no válidos: " + "; ".join(problemas) + "."


class _RutaConErroresEnEspanol(APIRoute):
    """Turns the body-validation errors of these routes into a Spanish 422."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                return await original(request)
            except RequestValidationError as exc:
                raise HTTPException(status_code=422, detail=errores_en_espanol(exc.errors())) from exc
            except EjercicioNoDisponibleError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        return handler


router = APIRouter(
    prefix="/autonomos",
    tags=["autonomos"],
    dependencies=[Depends(require_api_key)],
    route_class=_RutaConErroresEnEspanol,
)


class EjerciciosAutonomos(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Years with self-employed parameters loaded, ascending.
    ejercicios: list[int]


@router.get("/ejercicios", response_model=EjerciciosAutonomos)
def get_ejercicios() -> EjerciciosAutonomos:
    """Years the three calculators can compute."""
    return EjerciciosAutonomos(ejercicios=ejercicios_autonomos_disponibles())


@router.post("/actividad", response_model=ResultadoActividad)
def post_actividad(payload: ActividadInput) -> ResultadoActividad:
    """Annual net income of the activity in estimación directa simplificada."""
    return calcular_rendimiento_actividad(payload)


@router.post("/modelo-130", response_model=ResultadoModelo130)
def post_modelo_130(payload: Modelo130Input) -> ResultadoModelo130:
    """Quarterly IRPF payment on account (modelo 130) from cumulative figures."""
    return calcular_modelo_130(payload)


@router.post("/modelo-303", response_model=ResultadoModelo303)
def post_modelo_303(payload: Modelo303Input) -> ResultadoModelo303:
    """Quarterly VAT return (modelo 303), general regime, domestic operations."""
    return calcular_modelo_303(payload)
