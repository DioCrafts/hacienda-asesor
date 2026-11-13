"""Reader of AEB Norma 43 (Cuaderno 43) statements: fixed-width records of 80 characters.

Layout (Asociación Española de Banca, «Información normalizada de cuenta corriente», serie normas y
procedimientos bancarios n.º 43). Positions are 1-based and inclusive, as the specification writes them:

- ``11`` cabecera de cuenta: 3-6 entidad, 7-10 oficina, 11-20 cuenta, 21-26 fecha inicial (AAMMDD),
  27-32 fecha final, 33 clave debe/haber del saldo inicial (1 deudor, 2 acreedor), 34-47 saldo inicial
  (12 enteros + 2 decimales), 48-50 divisa (ISO 4217 numérica), 51 modalidad, 52-77 nombre abreviado.
- ``22`` movimiento: 7-10 oficina origen, 11-16 fecha operación, 17-22 fecha valor, 23-24 concepto común,
  25-27 concepto propio, 28 clave debe/haber (1 debe = cargo, 2 haber = abono), 29-42 importe,
  43-52 número de documento, 53-64 referencia 1, 65-80 referencia 2.
- ``23`` complementario de concepto (hasta cinco por movimiento): 3-4 código de dato, 5-42 y 43-80 concepto.
- ``24`` complementario de equivalencia de importe (divisa original).
- ``33`` final de cuenta: 21-25 número de apuntes al debe, 26-39 total debe, 40-44 número de apuntes al
  haber, 45-58 total haber, 59 clave del saldo final, 60-73 saldo final, 74-76 divisa.
- ``88`` fin de fichero: 3-20 nueves, 21-26 número de registros.

The specification could not be downloaded from this machine (only boe.es is reachable): the layout is
encoded from the published Cuaderno 43 and every file is checked against its own control record 33
(number of entries, totals and final balance). A file whose totals do not add up is rejected, so a
misread layout can never pass silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from hacienda_gpt.modulos.suscripciones.lectura import enmascarar_cuenta
from hacienda_gpt.modulos.suscripciones.modelos import Extracto, ImportacionError, Medio, Movimiento

LONGITUD = 80

# Conceptos comunes of the specification (positions 23-24 of record 22).
CONCEPTOS_COMUNES: dict[str, str] = {
    "01": "Talones - reintegros",
    "02": "Abonarés - entregas - ingresos",
    "03": "Domiciliados - recibos - letras - pagos por su cuenta",
    "04": "Giros - transferencias - traspasos - cheques",
    "05": "Amortizaciones préstamos, créditos, etc.",
    "06": "Remesas efectos",
    "07": "Suscripciones - dividendos - venta de derechos",
    "08": "Dividendos - cupones - prima junta - amortizaciones",
    "09": "Operaciones de bolsa y/o compra/venta valores",
    "10": "Cheques gasolina",
    "11": "Cajero automático",
    "12": "Tarjetas de crédito - tarjetas de débito",
    "13": "Operaciones extranjero",
    "14": "Devoluciones e impagados",
    "15": "Nóminas - seguros sociales",
    "16": "Timbres - corretaje - póliza",
    "17": "Intereses - comisiones - custodia - gastos e impuestos",
    "98": "Anulaciones - correcciones asiento",
    "99": "Varios",
}

_MEDIO_POR_CONCEPTO: dict[str, Medio] = {
    "03": "adeudo",
    "04": "transferencia",
    "05": "prestamo",
    "11": "efectivo",
    "12": "tarjeta",
    "17": "comision",
}

_DIVISAS = {"978": "EUR", "840": "USD", "826": "GBP", "756": "CHF"}


def _campo(linea: str, desde: int, hasta: int) -> str:
    """Positions ``desde``..``hasta`` (1-based, inclusive) of a record."""
    return linea[desde - 1 : hasta]


def _fecha(texto: str, linea: int) -> date:
    try:
        ano, mes, dia = int(texto[0:2]), int(texto[2:4]), int(texto[4:6])
        return date(2000 + ano if ano < 80 else 1900 + ano, mes, dia)
    except ValueError as exc:
        raise ImportacionError(f"Norma 43: fecha ilegible «{texto}» en el registro {linea}.") from exc


def _importe(texto: str, linea: int) -> Decimal:
    if not texto.isdigit():
        raise ImportacionError(f"Norma 43: importe ilegible «{texto}» en el registro {linea}.")
    return Decimal(int(texto)).scaleb(-2)


def _numero(texto: str, linea: int) -> int:
    if not texto.strip().isdigit():
        raise ImportacionError(f"Norma 43: número ilegible «{texto}» en el registro {linea}.")
    return int(texto)


@dataclass
class _Apunte:
    fecha: date
    fecha_valor: date
    concepto_comun: str
    debe: bool
    importe: Decimal
    referencias: list[str]
    conceptos: list[str] = field(default_factory=list)


@dataclass
class _Cuenta:
    cuenta: str
    divisa: str
    saldo_inicial: Decimal
    apuntes: list[_Apunte] = field(default_factory=list)
    cerrada: bool = False


def es_norma43(texto: str) -> bool:
    lineas = [linea for linea in _registros(texto) if linea.strip()]
    return bool(lineas) and lineas[0].startswith("11") and any(linea.startswith("22") for linea in lineas)


def _registros(texto: str) -> list[str]:
    lineas = texto.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # Some exports drop the line breaks: one long line that is a whole number of records.
    if len(lineas) <= 2 and len(lineas[0]) > LONGITUD and len(lineas[0]) % LONGITUD == 0:
        return [lineas[0][i : i + LONGITUD] for i in range(0, len(lineas[0]), LONGITUD)]
    return lineas


def leer_norma43(texto: str) -> Extracto:
    cuentas: list[_Cuenta] = []
    actual: _Cuenta | None = None
    apunte: _Apunte | None = None
    avisos: list[str] = []
    registros = 0
    fin_declarado: int | None = None
    for numero, bruta in enumerate(_registros(texto), start=1):
        if not bruta.strip():
            continue
        if len(bruta.rstrip()) > LONGITUD:
            raise ImportacionError(f"Norma 43: el registro {numero} tiene más de 80 caracteres.")
        linea = bruta.ljust(LONGITUD)
        codigo = linea[:2]
        registros += 1
        if codigo == "11":
            signo = -1 if _campo(linea, 33, 33) == "1" else 1
            actual = _Cuenta(
                cuenta=_campo(linea, 3, 20),
                divisa=_DIVISAS.get(_campo(linea, 48, 50), _campo(linea, 48, 50)),
                saldo_inicial=signo * _importe(_campo(linea, 34, 47), numero),
            )
            cuentas.append(actual)
            apunte = None
        elif codigo == "22":
            if actual is None or actual.cerrada:
                raise ImportacionError(f"Norma 43: movimiento fuera de una cuenta en el registro {numero}.")
            clave = _campo(linea, 28, 28)
            if clave not in {"1", "2"}:
                raise ImportacionError(f"Norma 43: clave debe/haber «{clave}» no válida en el registro {numero}.")
            apunte = _Apunte(
                fecha=_fecha(_campo(linea, 11, 16), numero),
                fecha_valor=_fecha(_campo(linea, 17, 22), numero),
                concepto_comun=_campo(linea, 23, 24),
                debe=clave == "1",
                importe=_importe(_campo(linea, 29, 42), numero),
                referencias=[r.strip() for r in (_campo(linea, 53, 64), _campo(linea, 65, 80)) if r.strip()],
            )
            actual.apuntes.append(apunte)
        elif codigo == "23":
            if apunte is None:
                raise ImportacionError(f"Norma 43: concepto complementario sin movimiento en el registro {numero}.")
            apunte.conceptos.extend(
                parte for parte in (_campo(linea, 5, 42).strip(), _campo(linea, 43, 80).strip()) if parte
            )
        elif codigo == "24":
            avisos.append(f"Registro {numero}: movimiento en otra divisa; se usa el importe en la divisa de la cuenta.")
        elif codigo == "33":
            if actual is None:
                raise ImportacionError(f"Norma 43: registro final de cuenta sin cabecera (registro {numero}).")
            _comprobar_totales(actual, linea, numero)
            actual.cerrada = True
            apunte = None
        elif codigo == "88":
            fin_declarado = _numero(_campo(linea, 21, 26), numero)
            registros -= 1  # the count does not include the end-of-file record itself
            break
        else:
            raise ImportacionError(f"Norma 43: tipo de registro «{codigo}» desconocido en el registro {numero}.")
    if not cuentas:
        raise ImportacionError("Norma 43: el fichero no tiene ninguna cuenta (registro 11).")
    if any(not cuenta.cerrada for cuenta in cuentas):
        raise ImportacionError("Norma 43: falta el registro final de cuenta (33); el fichero parece incompleto.")
    if fin_declarado is None:
        avisos.append("Falta el registro de fin de fichero (88).")
    elif fin_declarado not in {registros, registros + 1}:
        avisos.append(f"El registro 88 declara {fin_declarado} registros y el fichero tiene {registros}.")

    movimientos = []
    for cuenta in cuentas:
        for a in cuenta.apuntes:
            descripcion = " ".join(a.conceptos) or CONCEPTOS_COMUNES.get(a.concepto_comun, "Movimiento")
            movimientos.append(
                Movimiento(
                    fecha=a.fecha,
                    fecha_valor=a.fecha_valor,
                    importe=-a.importe if a.debe else a.importe,
                    descripcion=descripcion,
                    referencia=a.referencias[0] if a.referencias else None,
                    medio=_MEDIO_POR_CONCEPTO.get(a.concepto_comun, "desconocido"),
                    divisa=cuenta.divisa,
                )
            )
    if len(cuentas) > 1:
        avisos.append(f"El fichero contiene {len(cuentas)} cuentas; se analizan juntas.")
    return Extracto(
        formato="norma43",
        movimientos=tuple(sorted(movimientos, key=lambda m: m.fecha)),
        avisos=tuple(avisos),
        cuenta=enmascarar_cuenta(cuentas[0].cuenta),
    )


def _comprobar_totales(cuenta: _Cuenta, linea: str, numero: int) -> None:
    n_debe = _numero(_campo(linea, 21, 25), numero)
    total_debe = _importe(_campo(linea, 26, 39), numero)
    n_haber = _numero(_campo(linea, 40, 44), numero)
    total_haber = _importe(_campo(linea, 45, 58), numero)
    signo = -1 if _campo(linea, 59, 59) == "1" else 1
    saldo_final = signo * _importe(_campo(linea, 60, 73), numero)
    debe = [a.importe for a in cuenta.apuntes if a.debe]
    haber = [a.importe for a in cuenta.apuntes if not a.debe]
    problemas = []
    if (len(debe), sum(debe, Decimal(0))) != (n_debe, total_debe):
        problemas.append(f"cargos: {len(debe)} por {sum(debe, Decimal(0))} € frente a {n_debe} por {total_debe} €")
    if (len(haber), sum(haber, Decimal(0))) != (n_haber, total_haber):
        problemas.append(f"abonos: {len(haber)} por {sum(haber, Decimal(0))} € frente a {n_haber} por {total_haber} €")
    calculado = cuenta.saldo_inicial + sum(haber, Decimal(0)) - sum(debe, Decimal(0))
    if calculado != saldo_final:
        problemas.append(f"saldo final calculado {calculado} € frente a {saldo_final} € declarado")
    if problemas:
        raise ImportacionError(
            "Norma 43: los totales de control del registro 33 no cuadran con los movimientos leídos ("
            + "; ".join(problemas)
            + "). El fichero está incompleto o no sigue el Cuaderno 43."
        )
