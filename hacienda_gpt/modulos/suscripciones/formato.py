"""Spanish formatting of amounts, percentages and dates for the tool outputs."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal


def euros(valor: Decimal) -> str:
    """Decimal('1234.5') -> '1.234,50 €'."""
    redondeado = valor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    signo = "-" if redondeado < 0 else ""
    entero, decimales = f"{abs(redondeado):.2f}".split(".")
    grupos = []
    while len(entero) > 3:
        grupos.insert(0, entero[-3:])
        entero = entero[:-3]
    grupos.insert(0, entero)
    return f"{signo}{'.'.join(grupos)},{decimales} €"


def porcentaje(valor: Decimal) -> str:
    texto = f"{valor.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):+.2f}".replace(".", ",")
    return f"{texto} %"


def fecha(valor: date | None) -> str:
    return valor.strftime("%d/%m/%Y") if valor else "—"


def sumar_dias_habiles(base: date, dias: int) -> date:
    """``dias`` working days after ``base`` counting Monday to Friday (public holidays are not modelled)."""
    actual = base
    while dias > 0:
        actual += timedelta(days=1)
        if actual.weekday() < 5:
            dias -= 1
    return actual


PERIODICIDAD_TEXTO = {
    "semanal": "semanal",
    "mensual": "mensual",
    "bimestral": "cada dos meses",
    "trimestral": "trimestral",
    "semestral": "semestral",
    "anual": "anual",
}

MEDIO_TEXTO = {
    "adeudo": "recibo domiciliado",
    "tarjeta": "tarjeta",
    "transferencia": "transferencia",
    "prestamo": "cargo de préstamo",
    "efectivo": "efectivo",
    "comision": "comisión",
    "desconocido": "medio no indicado en el extracto",
}
