"""Bridges from household modules to the tax profile: figures a module knows that a tax fact needs.

A bridge never writes anything. It PROPOSES a value for a tax fact the case does not have yet, with the
figures it comes from; the user accepts it (``PUT /cases/{id}/facts/{nombre}``) or ignores it. What the
calculator does with the value (limits, which payments enter the base) stays the tax module's job.

Bridges today:

* ``hipoteca`` → ``pagos_vivienda_habitual_pre2013`` (what the IRPF calculator uses for the transitional
  deduction for the main home bought before 2013): offered when the loan in the profile says it financed the
  main home (``vivienda_habitual``) bought before 2013 (``adquisicion_antes_2013`` exactly true) and the
  fact is missing. The figure is what the module's amortisation table says was paid in the case's fiscal
  year (capital + interest of the instalments + early repayments), shown broken down.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import importlib
import logging
from typing import Any

from hacienda_gpt.decision.schemas import CaseState, parse_fiscal_year
from hacienda_gpt.modulos.contrato import ContextoModulo, Modulo
from hacienda_gpt.modulos.perfil import perfil_de

logger = logging.getLogger(__name__)

HECHO_VIVIENDA_PRE2013 = "pagos_vivienda_habitual_pre2013"


@dataclass(frozen=True)
class PropuestaHecho:
    nombre: str
    valor: float
    titulo: str
    explicacion: str
    modulo: str
    desglose: dict[str, str] = field(default_factory=dict)


def _eur(amount: Decimal) -> str:
    text = f"{abs(amount):,.2f}".replace(",", " ").replace(".", ",").replace(" ", ".")
    return f"{text} €"


def _hipoteca(case: CaseState, perfil: dict[str, Any], hoy: date) -> PropuestaHecho | None:
    if HECHO_VIVIENDA_PRE2013 in perfil:
        return None
    prestamo = perfil.get("hipoteca_prestamo")
    if not isinstance(prestamo, dict) or prestamo.get("adquisicion_antes_2013") is not True:
        return None
    if prestamo.get("vivienda_habitual") is False:
        return None
    ejercicio = parse_fiscal_year(case.tax_period) or hoy.year
    pagos_del_ejercicio = importlib.import_module("hacienda_gpt.modulos.hipoteca").pagos_del_ejercicio
    pagos = pagos_del_ejercicio(ContextoModulo(perfil=perfil, hoy=hoy, case_id=case.case_id), ejercicio)
    if not pagos:
        return None
    capital = Decimal(pagos.get("capital") or 0)
    intereses = Decimal(pagos.get("intereses") or 0)
    anticipadas = Decimal(pagos.get("amortizaciones_anticipadas") or 0)
    total = (capital + intereses + anticipadas).quantize(Decimal("0.01"))
    if total <= 0:
        return None
    hasta = " (las cuotas que vencen hasta el 31 de diciembre, según el cuadro)" if ejercicio >= hoy.year else ""
    return PropuestaHecho(
        nombre=HECHO_VIVIENDA_PRE2013,
        valor=float(total),
        titulo=f"Lo que pagas de hipoteca en {ejercicio}: {_eur(total)}",
        explicacion=(
            f"Tu préstamo es de la vivienda habitual comprada antes de 2013. Según su cuadro de amortización, en "
            f"{ejercicio} pagas {_eur(total)}{hasta}. Si lo confirmas, la calculadora del IRPF lo usará para la "
            "deducción por vivienda habitual del régimen transitorio y aplicará ella sus límites. Comprueba la "
            "cifra con el certificado de tu banco: si pagas otros conceptos ligados al préstamo, puedes corregirla."
        ),
        modulo="hipoteca",
        desglose={
            "capital": _eur(capital),
            "intereses": _eur(intereses),
            "amortizaciones_anticipadas": _eur(anticipadas),
        },
    )


Puente = Callable[[CaseState, dict[str, Any], date], PropuestaHecho | None]
PUENTES: dict[str, Puente] = {"hipoteca": _hipoteca}


def propuestas_para(case: CaseState, modulos: Sequence[Modulo], *, hoy: date) -> list[PropuestaHecho]:
    """Proposals for this case from the active modules' bridges. A failing bridge is logged and skipped."""
    perfil = perfil_de(case)
    out: list[PropuestaHecho] = []
    for modulo in modulos:
        puente = PUENTES.get(modulo.clave)
        if puente is None:
            continue
        try:
            propuesta = puente(case, perfil, hoy)
        except Exception:
            logger.exception("Tax bridge of module %s failed for case %s", modulo.clave, case.case_id)
            continue
        if propuesta is not None:
            out.append(propuesta)
    return out


__all__ = ["HECHO_VIVIENDA_PRE2013", "PUENTES", "PropuestaHecho", "propuestas_para"]
