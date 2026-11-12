"""Scenario simulation shared by the agent tool and the ``/simulate`` endpoint."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from hacienda_gpt.tax.beneficios import Beneficio, evaluar_beneficios
from hacienda_gpt.tax.irpf import IRPFInput, IRPFResult, calcular_irpf
from hacienda_gpt.tax.params import load_params
from hacienda_gpt.tax.profile import CORE_FIELDS, DatosInvalidosError, build_input

MAX_ESCENARIOS = 6
BASE_NAME = "Situación actual"


class EscenarioResultado(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nombre: str
    resultado: IRPFResult
    # Tax saved versus the first scenario (positive: this one is cheaper).
    ahorro_frente_a_base: Decimal


class Simulacion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ejercicio: int
    escenarios: list[EscenarioResultado]
    beneficios: list[Beneficio]
    # Core figures nobody supplied: the simulation silently used zero for them.
    datos_no_aportados: list[str]


def simular(
    *,
    ejercicio: int,
    facts: dict[str, Any] | None = None,
    datos: dict[str, Any] | None = None,
    escenarios: list[dict[str, Any]] | None = None,
    con_beneficios: bool = True,
    beneficios_sobre_ultimo: bool = False,
) -> Simulacion:
    """Run the base scenario plus alternatives. Raises ``DatosInvalidosError``
    or ``EjercicioNoDisponibleError`` on bad input."""
    facts, datos = facts or {}, datos or {}
    if not isinstance(datos, dict):
        raise DatosInvalidosError("'datos' debe ser un objeto {campo: valor}.")
    params = load_params(ejercicio)
    raw = escenarios or [{"nombre": BASE_NAME, "cambios": {}}]
    if not isinstance(raw, list) or len(raw) > MAX_ESCENARIOS:
        raise DatosInvalidosError(f"'escenarios' debe ser una lista de hasta {MAX_ESCENARIOS} alternativas.")

    inputs: list[tuple[str, IRPFInput]] = []
    cambios_ultimo: dict[str, Any] = {}
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("nombre"), str) or not item["nombre"].strip():
            raise DatosInvalidosError("Cada escenario necesita un 'nombre' y un objeto 'cambios'.")
        cambios = item.get("cambios") or {}
        if not isinstance(cambios, dict):
            raise DatosInvalidosError("'cambios' debe ser un objeto {campo: valor}.")
        cambios_ultimo = cambios
        inputs.append(
            (item["nombre"].strip(), build_input(ejercicio=ejercicio, facts=facts, datos=datos, cambios=cambios))
        )

    results = [(nombre, calcular_irpf(inp, params)) for nombre, inp in inputs]
    base = results[0][1]
    provided = {name for name in (*facts, *datos) if name in IRPFInput.model_fields or name == "estado_civil"}
    return Simulacion(
        ejercicio=ejercicio,
        escenarios=[
            EscenarioResultado(nombre=nombre, resultado=result, ahorro_frente_a_base=base.resultado - result.resultado)
            for nombre, result in results
        ],
        beneficios=(
            # Levers are normally judged against the saved profile; an interactive
            # simulator wants them for the scenario being edited instead.
            evaluar_beneficios(
                inputs[-1][1] if beneficios_sobre_ultimo else inputs[0][1],
                conocidos=provided | (set(cambios_ultimo) if beneficios_sobre_ultimo else set()),
                params=params,
            )
            if con_beneficios
            else []
        ),
        datos_no_aportados=[name for name in CORE_FIELDS if name not in provided],
    )
