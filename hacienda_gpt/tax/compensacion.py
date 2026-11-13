"""Integration and offsetting of income in the savings base (art. 49 LIRPF).

Two balances are worked out separately: capital income (interest, dividends…)
and capital gains and losses. A negative balance in one may be offset against
the positive balance of the other only up to a share of that positive balance
(25 %); what is left is carried forward for the four following years, in the
same order. Negative balances brought from earlier years are used after the
year's own, first against their own kind without limit and then against the
other kind within what remains of that share.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

ZERO = Decimal(0)


class CompensacionAhorro(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_ahorro: Decimal
    # Net balances of the year before any offsetting.
    saldo_rendimientos: Decimal
    saldo_ganancias: Decimal
    # Offset in this return.
    compensado_del_ejercicio: Decimal
    compensado_de_anteriores: Decimal
    # Left for the following years (the year's own negatives plus unused earlier ones).
    rendimientos_negativos_pendientes: Decimal
    perdidas_pendientes: Decimal


def compensar_ahorro(
    *,
    rendimientos: Decimal,
    ganancias: Decimal,
    rendimientos_negativos_anteriores: Decimal = ZERO,
    perdidas_anteriores: Decimal = ZERO,
    limite_cruzado: Decimal,
) -> CompensacionAhorro:
    """Savings base after offsetting. ``limite_cruzado`` is the share of a positive balance that may absorb the other kind."""
    a, b = rendimientos, ganancias  # capital income / gains and losses
    pend_a, pend_b = rendimientos_negativos_anteriores, perdidas_anteriores
    # How much of each positive balance negatives of the OTHER kind may still take.
    cupo_sobre_a = limite_cruzado * max(a, ZERO)
    cupo_sobre_b = limite_cruzado * max(b, ZERO)
    del_ejercicio = de_anteriores = ZERO

    # 1. The year's own negative balance against the other kind, within the limit.
    if a < 0 < b:
        usado = min(-a, cupo_sobre_b)
        a, b, cupo_sobre_b, del_ejercicio = a + usado, b - usado, cupo_sobre_b - usado, usado
    elif b < 0 < a:
        usado = min(-b, cupo_sobre_a)
        b, a, cupo_sobre_a, del_ejercicio = b + usado, a - usado, cupo_sobre_a - usado, usado
    nuevo_a, nuevo_b = max(-a, ZERO), max(-b, ZERO)
    a, b = max(a, ZERO), max(b, ZERO)

    # 2. Earlier years' negatives: first against their own kind, without limit.
    usado = min(pend_a, a)
    a, pend_a, de_anteriores = a - usado, pend_a - usado, de_anteriores + usado
    usado = min(pend_b, b)
    b, pend_b, de_anteriores = b - usado, pend_b - usado, de_anteriores + usado
    # 3. Then against the other kind, within what is left of the limit.
    usado = min(pend_a, b, cupo_sobre_b)
    b, pend_a, de_anteriores = b - usado, pend_a - usado, de_anteriores + usado
    usado = min(pend_b, a, cupo_sobre_a)
    a, pend_b, de_anteriores = a - usado, pend_b - usado, de_anteriores + usado

    return CompensacionAhorro(
        base_ahorro=a + b,
        saldo_rendimientos=rendimientos,
        saldo_ganancias=ganancias,
        compensado_del_ejercicio=del_ejercicio,
        compensado_de_anteriores=de_anteriores,
        rendimientos_negativos_pendientes=nuevo_a + pend_a,
        perdidas_pendientes=nuevo_b + pend_b,
    )
