"""Reduction of the net income from letting a dwelling (art. 23.2 LIRPF).

Which wording of art. 23.2 applies depends on when the contract was signed
(transitional provision 38):

* before the Housing Act (Ley 12/2023) came into force: the wording in force
  on 31-12-2021, a flat percentage;
* from then until 1 December 2026: the wording in force on 31-12-2025;
* afterwards: the wording given by Real Decreto-ley 29/2026.

Every percentage comes from the versioned parameters; this module only holds
the decision logic. When several reductions fit, the largest applies.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from hacienda_gpt.tax.params import ReduccionArrendamiento

ZERO = Decimal(0)


class DatosArrendamiento(BaseModel):
    """The facts of one letting contract that decide the reduction."""

    model_config = ConfigDict(extra="forbid")

    fecha_contrato: date
    gran_tenedor: bool = False
    # First time the owner lets this dwelling (otherwise: a new contract after a previous one).
    primer_alquiler: bool = False
    # Initial rent against the last rent of the previous contract, in percent (-6 = lowered by 6 %).
    variacion_renta: Decimal | None = None
    # New contract with the same tenant once the minimum term or its extensions ended.
    mismo_inquilino: bool = False
    # Initial rent below the cap of the reference price index (None = unknown).
    renta_bajo_indice: bool | None = None
    zona_tensionada: bool = False
    inquilino_joven: bool = False  # tenant aged 18 to 35
    alquiler_social: bool = False  # public body / non-profit social letting, or rent-capped public scheme
    vivienda_rehabilitada: bool = False  # works finished in the two years before the contract
    prorroga_tacita: bool = False


class Reduccion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    porcentaje: Decimal
    regimen: str
    motivo: str
    referencia: str
    avisos: list[str] = []


def _mejor(candidatos: list[tuple[Decimal, str]]) -> tuple[Decimal, str]:
    return max(candidatos, key=lambda c: c[0]) if candidatos else (ZERO, "ningún supuesto de reducción se cumple")


def reduccion_arrendamiento(d: DatosArrendamiento, p: ReduccionArrendamiento) -> Reduccion:
    """Percentage of the positive net income that is not taxed."""
    avisos: list[str] = []

    if d.fecha_contrato < p.fecha_ley_vivienda:
        r = p.regimen_2021
        return Reduccion(
            porcentaje=r.porcentaje,
            regimen="contrato anterior a la Ley 12/2023",
            motivo="porcentaje único para contratos anteriores al 26 de mayo de 2023",
            referencia=r.fuente.referencia,
        )

    nuevo = p.regimen_2026
    if nuevo is None or d.fecha_contrato <= nuevo.contratos_posteriores_a:
        r23 = p.regimen_2023
        candidatos = [(r23.resto, "supuesto general")]
        rebaja = d.variacion_renta is not None and d.variacion_renta < -r23.rebaja_minima
        if not d.primer_alquiler and d.zona_tensionada and rebaja:
            candidatos.append((r23.rebaja_zona_tensionada, "nuevo contrato en zona tensionada con rebaja de la renta"))
        if d.primer_alquiler and d.zona_tensionada and d.inquilino_joven:
            candidatos.append((r23.joven_o_social, "primer alquiler en zona tensionada a inquilino de 18 a 35 años"))
        if d.alquiler_social:
            candidatos.append((r23.joven_o_social, "alquiler social o vivienda con renta limitada"))
        if d.vivienda_rehabilitada:
            candidatos.append((r23.rehabilitada, "vivienda rehabilitada en los dos años anteriores"))
        porcentaje, motivo = _mejor(candidatos)
        if nuevo is not None and d.fecha_contrato == nuevo.contratos_posteriores_a:
            avisos.append(
                "El contrato es del mismo 1 de diciembre de 2026, la fecha frontera de la disposición transitoria 38.ª: "
                "se ha tratado como anterior."
            )
        return Reduccion(
            porcentaje=porcentaje,
            regimen="contrato entre la Ley 12/2023 y el 1 de diciembre de 2026",
            motivo=motivo,
            referencia=r23.fuente.referencia,
            avisos=avisos,
        )

    candidatos = []
    if d.alquiler_social:
        candidatos.append((nuevo.social, "alquiler social o vivienda con renta limitada"))
    if d.vivienda_rehabilitada:
        candidatos.append((nuevo.rehabilitada, "vivienda rehabilitada en los dos años anteriores"))
    if d.gran_tenedor:
        avisos.append(
            "Como gran tenedor no puedes aplicar las reducciones de las letras a) y b) del art. 23.2 ni la de la "
            "prórroga tácita: solo las de alquiler social y vivienda rehabilitada."
        )
    else:
        bajo_indice = d.renta_bajo_indice is True
        if d.renta_bajo_indice is None:
            avisos.append(
                "No se sabe si la renta inicial queda por debajo del límite del índice de precios de referencia: se ha "
                "supuesto que no. Si queda por debajo, la reducción puede llegar al 100 %."
            )
        escalera = []
        if d.zona_tensionada and d.inquilino_joven:
            escalera.append((nuevo.zona_tensionada_joven, "zona tensionada e inquilino de 18 a 35 años"))
        if d.zona_tensionada:
            escalera.append((nuevo.zona_tensionada, "zona tensionada"))
        if d.inquilino_joven:
            escalera.append((nuevo.joven, "inquilino de 18 a 35 años"))
        if d.inquilino_joven:
            avisos.append(
                "Si hay varios inquilinos, la reducción ligada a la edad solo alcanza a la parte del rendimiento que "
                "corresponde a los que tienen entre 18 y 35 años; aquí se ha aplicado a todo."
            )
        if d.primer_alquiler:
            candidatos += [(pct, f"primer alquiler: {motivo}") for pct, motivo in escalera]
            if bajo_indice:
                candidatos.append((nuevo.maxima, "primer alquiler con renta inferior al índice de referencia"))
            else:
                candidatos.append(
                    (nuevo.primer_alquiler_sobre_indice, "primer alquiler con renta igual o superior al índice")
                )
        elif d.variacion_renta is None:
            avisos.append(
                "Falta saber cuánto varía la renta inicial respecto de la última del contrato anterior: sin ese dato "
                "no se puede aplicar la letra a) del art. 23.2."
            )
        elif d.variacion_renta < -nuevo.rebaja_minima:
            candidatos.append((nuevo.rebaja_resto, f"nuevo contrato con rebaja superior al {nuevo.rebaja_minima} %"))
            candidatos += [(pct, f"nuevo contrato con rebaja: {motivo}") for pct, motivo in escalera]
            if d.mismo_inquilino:
                candidatos.append((nuevo.maxima, "nuevo contrato con rebaja y el mismo inquilino al acabar el plazo"))
            elif bajo_indice:
                candidatos.append((nuevo.maxima, "nuevo contrato con rebaja y renta inferior al índice de referencia"))
        elif d.variacion_renta <= 0:
            candidatos.append((nuevo.renta_no_superior, "nuevo contrato sin subir la renta"))
        else:
            for hasta, pct in nuevo.incrementos:
                if hasta is None or d.variacion_renta <= hasta:
                    candidatos.append((pct, f"nuevo contrato con subida del {d.variacion_renta} %"))
                    break
        if d.prorroga_tacita and bajo_indice:
            candidatos.append((nuevo.prorroga_tacita, "prórroga tácita con renta dentro del índice de referencia"))
            avisos.append(
                "La reducción de la prórroga tácita solo alcanza al rendimiento del periodo de prórroga; aquí se ha "
                "aplicado a todo el rendimiento indicado."
            )
    porcentaje, motivo = _mejor(candidatos)
    return Reduccion(
        porcentaje=porcentaje,
        regimen="contrato posterior al 1 de diciembre de 2026",
        motivo=motivo,
        referencia=nuevo.fuente.referencia,
        avisos=avisos,
    )
