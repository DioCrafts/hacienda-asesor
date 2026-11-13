"""A realistic synthetic year (Oct 2025 - Oct 2026) of a Spanish current account, and writers that
serialise it in every supported statement format from their documented layouts.

``python -m tests.modulos.suscripciones.sintetico`` regenerates the fixtures under
``tests/fixtures/modulos/suscripciones/extractos``. The tests read the committed files, so a change in a
writer shows up as a fixture diff. All names are businesses or invented; no real person or account.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
import random

DESDE = date(2025, 10, 1)
HASTA = date(2026, 10, 31)
SALDO_INICIAL = Decimal("3500.00")
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "modulos" / "suscripciones" / "extractos"
IBAN = "ES9121000418450200051332"


@dataclass(frozen=True)
class Mov:
    fecha: date
    importe: Decimal  # negative = charge
    concepto: str
    medio: str  # tarjeta | adeudo | transferencia | prestamo | comision | efectivo | abono
    acreedor: str | None = None  # creditor / merchant name (camt, OFX memo)
    mandato: str | None = None


def _laborable(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _meses(dia: int, desde: date, hasta: date, cada: int = 1, laborable: bool = False) -> list[date]:
    fechas = []
    ano, mes = desde.year, desde.month
    while True:
        d = date(ano, mes, min(dia, calendar.monthrange(ano, mes)[1]))
        if laborable:
            d = _laborable(d)
        if d > hasta:
            break
        if d >= desde:
            fechas.append(d)
        mes += cada
        while mes > 12:
            mes -= 12
            ano += 1
    return fechas


def _euros(texto: str) -> Decimal:
    return Decimal(texto)


def movimientos() -> list[Mov]:
    r = random.Random(20261113)
    m: list[Mov] = []

    def cargo(f: date, imp: str, concepto: str, medio: str, acreedor: str | None = None, mandato: str | None = None):
        m.append(Mov(f, -_euros(imp), concepto, medio, acreedor, mandato))

    # Netflix: price rise from April 2026
    for f in _meses(5, DESDE, HASTA):
        imp = "12.99" if f < date(2026, 4, 1) else "13.99"
        cargo(f, imp, "COMPRA TARJ. 5540XXXXXXXX1234 NETFLIX.COM 866-579-7172 NL", "tarjeta", "NETFLIX.COM")
    # Spotify by card the whole year, and again through PayPal from June 2026 (service paid twice)
    for f in _meses(12, DESDE, HASTA):
        cargo(f, "10.99", "PAGO MOVIL EN SPOTIFY P2B7C9D1E3 STOCKHOLM SE", "tarjeta", "SPOTIFY AB")
    for f in _meses(20, date(2026, 6, 1), HASTA):
        cargo(f, "10.99", "PAYPAL *SPOTIFY 4029357733 LU", "tarjeta", "PAYPAL EUROPE S.A.R.L.")
    # Max: double charge in September 2026
    for f in _meses(14, DESDE, HASTA):
        cargo(f, "9.99", "COMPRA INTERNET EN HBO MAX 9.99 EUR", "tarjeta", "HBO MAX")
    cargo(date(2026, 9, 15), "9.99", "COMPRA INTERNET EN HBO MAX 9.99 EUR", "tarjeta", "HBO MAX")
    # DAZN: 0,99 trial then full price
    cargo(date(2026, 6, 3), "0.99", "COMPRA TARJ. 5540XXXXXXXX1234 DAZN LIMITED LONDON GB", "tarjeta", "DAZN")
    for f in _meses(3, date(2026, 7, 1), HASTA):
        cargo(f, "19.99", "COMPRA TARJ. 5540XXXXXXXX1234 DAZN LIMITED LONDON GB", "tarjeta", "DAZN")
    # Filmin: stopped after March 2026
    for f in _meses(20, DESDE, date(2026, 3, 31)):
        cargo(f, "7.99", "COMPRA TARJ. 5540XXXXXXXX1234 FILMIN SL MADRID ES", "tarjeta", "FILMIN")
    # Apple: two different subscriptions behind the same descriptor
    for f in _meses(8, DESDE, HASTA):
        cargo(f, "0.99", "APPLE.COM/BILL ITUNES.COM 800-275-2273 IE", "tarjeta", "APPLE.COM/BILL")
    for f in _meses(22, DESDE, HASTA):
        cargo(f, "10.99", "APPLE.COM/BILL ITUNES.COM 800-275-2273 IE", "tarjeta", "APPLE.COM/BILL")
    # Gym and a neighbours' association, both direct debits on the 1st (working day)
    for i, f in enumerate(_meses(1, DESDE, HASTA, laborable=True)):
        cargo(
            f,
            "29.99",
            f"RECIBO BASIC FIT SPAIN SAU N RECIBO 0049{i:04d} REF. MANDATO BF-000123",
            "adeudo",
            "BASIC FIT SPAIN SAU",
            "BF-000123",
        )
        cargo(
            f,
            "5.00",
            f"RECIBO ASOC VECINAL LOS ALAMOS CUOTA {f.month:02d}/{f.year}",
            "adeudo",
            "ASOC VECINAL LOS ALAMOS",
            "AVLA-77",
        )
    # Orange: price rise from August 2026
    for f in _meses(10, DESDE, HASTA, laborable=True):
        imp = "45.00" if f < date(2026, 8, 1) else "48.50"
        cargo(
            f,
            imp,
            f"ADEUDO RECIBO ORANGE ESPAGNE S.A.U. FACTURA M{f:%y%m}-123456",
            "adeudo",
            "ORANGE ESPAGNE SAU",
            "ORG-55501",
        )
    # Utilities every two months, variable
    for f, imp in zip(
        _meses(15, DESDE, HASTA, cada=2, laborable=True),
        ["58.40", "71.25", "83.10", "64.75", "52.30", "68.90", "61.15"],
        strict=True,
    ):
        cargo(
            f, imp, "RECIBO IBERDROLA CLIENTES SAU LUZ FRA 2025/88231", "adeudo", "IBERDROLA CLIENTES SAU", "IBE-9001"
        )
    for f, imp in zip(
        _meses(3, date(2025, 11, 1), HASTA, cada=2, laborable=True),
        ["32.10", "35.40", "29.85", "38.20", "41.05", "33.60"],
        strict=True,
    ):
        cargo(f, imp, "RECIBO CANAL DE ISABEL II AGUA FRA 7731", "adeudo", "CANAL DE ISABEL II SA", "CYII-31")
    # Insurance: yearly car (price rise) and quarterly home
    cargo(
        date(2025, 10, 20),
        "380.00",
        "RECIBO MAPFRE ESPANA SEGURO AUTO POLIZA 0981234",
        "adeudo",
        "MAPFRE ESPANA",
        "MAP-1",
    )
    cargo(
        date(2026, 10, 20),
        "412.30",
        "RECIBO MAPFRE ESPANA SEGURO AUTO POLIZA 0981234",
        "adeudo",
        "MAPFRE ESPANA",
        "MAP-1",
    )
    for f in _meses(5, date(2025, 11, 1), HASTA, cada=3, laborable=True):
        cargo(f, "78.40", "RECIBO MUTUA MADRILENA HOGAR POL 556677", "adeudo", "MUTUA MADRILENA", "MM-2")
    # Loan, rent, bank fee, professional association, weekly digital press, Amazon Prime once a year
    for f in _meses(28, DESDE, HASTA):
        cargo(f, "350.21", "AMORTIZACION PRESTAMO 0182-1234-55", "prestamo", "BANCO EJEMPLO")
    for f in _meses(1, DESDE, HASTA):
        cargo(
            f,
            "850.00",
            "TRANSFERENCIA A FAVOR DE INMOBILIARIA SOL SL CONCEPTO ALQUILER",
            "transferencia",
            "INMOBILIARIA SOL SL",
        )
    for f in [date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30), date(2026, 9, 30)]:
        cargo(f, "15.00", "COMISION MANTENIMIENTO CUENTA", "comision", "BANCO EJEMPLO")
    for f in [date(2025, 12, 15), date(2026, 6, 15)]:
        cargo(f, "95.00", "CUOTA COLEGIO OFICIAL INGENIEROS 2S", "adeudo", "COLEGIO OFICIAL INGENIEROS", "COI-3")
    d = date(2026, 7, 4)
    while d <= HASTA:
        cargo(d, "1.99", "COMPRA TARJ. 5540XXXXXXXX1234 KIOSKO Y MAS SUSCRIPCION", "tarjeta", "KIOSKO Y MAS")
        d += timedelta(days=7)
    cargo(date(2025, 12, 1), "49.90", "AMAZON PRIME*2K4LM8 AMZN.COM/BILL LU", "tarjeta", "AMAZON EU SARL")

    # Noise: salary, groceries, fuel, food delivery, Amazon purchases, Bizum, cash
    for f in _meses(27, DESDE, HASTA, laborable=True):
        m.append(Mov(f, Decimal("2150.00"), "NOMINA EMPRESA EJEMPLO SA", "abono", "EMPRESA EJEMPLO SA"))
    d = DESDE
    while d <= HASTA:
        cargo(
            d,
            f"{r.uniform(35, 140):.2f}",
            "COMPRA TARJ. 5540XXXXXXXX1234 MERCADONA AV PRINCIPAL",
            "tarjeta",
            "MERCADONA",
        )
        d += timedelta(days=r.randint(5, 9))
    d = DESDE + timedelta(days=3)
    while d <= HASTA:
        cargo(d, f"{r.uniform(40, 70):.2f}", "COMPRA TARJ. 5540XXXXXXXX1234 E.S. REPSOL CTRA N-VI", "tarjeta", "REPSOL")
        d += timedelta(days=r.randint(11, 21))
    for _ in range(25):
        f = DESDE + timedelta(days=r.randint(0, (HASTA - DESDE).days))
        cargo(f, f"{r.uniform(12, 45):.2f}", "COMPRA TARJ. 5540XXXXXXXX1234 GLOVO APP", "tarjeta", "GLOVO")
    for _ in range(18):
        f = DESDE + timedelta(days=r.randint(0, (HASTA - DESDE).days))
        cargo(f, f"{r.uniform(6, 80):.2f}", "COMPRA INTERNET EN AMAZON MKTP ES*1A2B3C", "tarjeta", "AMAZON EU SARL")
    for _ in range(10):
        f = DESDE + timedelta(days=r.randint(0, (HASTA - DESDE).days))
        cargo(f, f"{r.randint(5, 60)}.00", "BIZUM ENVIADO CENA AMIGOS", "transferencia")
    for f in _meses(9, DESDE, HASTA):
        cargo(f, "60.00", "REINTEGRO CAJERO 4B AV PRINCIPAL", "efectivo")
    m.sort(key=lambda x: (x.fecha, x.importe, x.concepto))
    return m


# --- Expected detection ---------------------------------------------------------------------------

# label -> (periodicity, status)
ESPERADOS: dict[str, tuple[str, str]] = {
    "Netflix": ("mensual", "activa"),
    "Spotify": ("mensual", "activa"),
    "Spotify (PayPal)": ("mensual", "activa"),
    "Max (HBO)": ("mensual", "activa"),
    "DAZN": ("mensual", "activa"),
    "Filmin": ("mensual", "inactiva"),
    "Apple (App Store / iCloud)": ("mensual", "activa"),  # twice: 0,99 and 10,99
    "Basic-Fit": ("mensual", "activa"),
    "Asoc Vecinal Alamos": ("mensual", "activa"),
    "Orange": ("mensual", "activa"),
    "Iberdrola": ("bimestral", "activa"),
    "Canal de Isabel II": ("bimestral", "activa"),
    "Mapfre": ("anual", "activa"),
    "Mutua Madrileña": ("trimestral", "activa"),
    "Préstamo": ("mensual", "activa"),
    "Alquiler": ("mensual", "activa"),
    "Comisión bancaria": ("trimestral", "activa"),
    "Cuota colegial": ("semestral", "activa"),
    "Kiosko y Más": ("semanal", "activa"),
    "Amazon Prime": ("anual", "activa"),
}


# --- Writers --------------------------------------------------------------------------------------


def _saldos(movs: list[Mov]) -> list[Decimal]:
    saldo, salida = SALDO_INICIAL, []
    for mov in movs:
        saldo += mov.importe
        salida.append(saldo)
    return salida


_CONCEPTO_N43 = {
    "tarjeta": "12",
    "adeudo": "03",
    "transferencia": "04",
    "prestamo": "05",
    "comision": "17",
    "efectivo": "11",
    "abono": "15",
}


def norma43(movs: list[Mov]) -> str:
    def imp(v: Decimal) -> str:
        return f"{int(abs(v) * 100):014d}"

    def d6(f: date) -> str:
        return f.strftime("%y%m%d")

    lineas = [
        "11"
        + "2100"
        + "0418"
        + "0200051332"
        + d6(DESDE)
        + d6(HASTA)
        + "2"
        + imp(SALDO_INICIAL)
        + "978"
        + "3"
        + "CUENTA EJEMPLO".ljust(26)
        + "   "
    ]
    for i, mov in enumerate(movs):
        lineas.append(
            "22"
            + "    "
            + "0418"
            + d6(mov.fecha)
            + d6(mov.fecha)
            + _CONCEPTO_N43[mov.medio]
            + "000"
            + ("1" if mov.importe < 0 else "2")
            + imp(mov.importe)
            + f"{i:010d}"
            + (mov.mandato or "").ljust(12)[:12]
            + "".ljust(16)
        )
        texto = mov.concepto
        trozos = [texto[j : j + 38] for j in range(0, len(texto), 38)][:10]
        for n in range(0, len(trozos), 2):
            par = trozos[n : n + 2] + [""]
            lineas.append("23" + f"{n // 2 + 1:02d}" + par[0].ljust(38) + par[1].ljust(38))
    debe = [x.importe for x in movs if x.importe < 0]
    haber = [x.importe for x in movs if x.importe > 0]
    final = SALDO_INICIAL + sum(x.importe for x in movs)
    lineas.append(
        "33"
        + "2100"
        + "0418"
        + "0200051332"
        + f"{len(debe):05d}"
        + imp(sum(debe))
        + f"{len(haber):05d}"
        + imp(sum(haber))
        + ("2" if final >= 0 else "1")
        + imp(final)
        + "978"
        + "    "
    )
    lineas.append("88" + "9" * 18 + f"{len(lineas):06d}" + " " * 54)
    assert all(len(linea) == 80 for linea in lineas)
    return "\r\n".join(lineas) + "\r\n"


def _es(v: Decimal, miles: bool = True) -> str:
    texto = f"{abs(v):,.2f}" if miles else f"{abs(v):.2f}"
    texto = texto.replace(",", "X").replace(".", ",").replace("X", ".")
    return ("-" if v < 0 else "") + texto


def csv_santander(movs: list[Mov]) -> bytes:
    lineas = [
        "Movimientos de la cuenta ES91 2100 **** **** ****1332;;;;",
        "Banco Santander;;;;",
        ";;;;",
        "Fecha Operación;Fecha Valor;Concepto;Importe;Saldo",
    ]
    for mov, saldo in zip(movs, _saldos(movs), strict=True):
        f = mov.fecha.strftime("%d/%m/%Y")
        lineas.append(f"{f};{f};{mov.concepto};{_es(mov.importe)};{_es(saldo)}")
    return ("\r\n".join(lineas) + "\r\n").encode("cp1252")


def csv_bbva(movs: list[Mov]) -> bytes:
    lineas = ["Últimos movimientos", "", "F.Valor;Fecha;Concepto;Movimiento;Importe;Divisa;Disponible;Divisa"]
    for mov, saldo in zip(movs, _saldos(movs), strict=True):
        f = mov.fecha.strftime("%d/%m/%Y")
        tipo = {"tarjeta": "Pago con tarjeta", "adeudo": "Adeudo de recibo", "transferencia": "Transferencia"}.get(
            mov.medio, "Otros"
        )
        lineas.append(f"{f};{f};{mov.concepto};{tipo};{_es(mov.importe, False)};EUR;{_es(saldo, False)};EUR")
    return ("\n".join(lineas) + "\n").encode("utf-8-sig")


def csv_ing(movs: list[Mov]) -> bytes:
    lineas = ['"F. VALOR","CATEGORÍA","SUBCATEGORÍA","DESCRIPCIÓN","COMENTARIO","IMAGEN","IMPORTE (€)","SALDO (€)"']
    for mov, saldo in zip(movs, _saldos(movs), strict=True):
        lineas.append(
            f'"{mov.fecha:%d/%m/%Y}","Otros gastos","Otros","{mov.concepto}","","","{mov.importe:.2f}","{saldo:.2f}"'
        )
    return ("\n".join(lineas) + "\n").encode("utf-8")


def csv_cargo_abono(movs: list[Mov]) -> bytes:
    lineas = ["Fecha\tConcepto\tConcepto complementario\tCargo\tAbono\tSaldo"]
    for mov, saldo in zip(movs, _saldos(movs), strict=True):
        cargo = _es(-mov.importe) if mov.importe < 0 else ""
        abono = _es(mov.importe) if mov.importe > 0 else ""
        lineas.append(f"{mov.fecha:%d-%m-%y}\t{mov.concepto}\t{mov.acreedor or ''}\t{cargo}\t{abono}\t{_es(saldo)}")
    return ("\n".join(lineas) + "\n").encode("latin-1")


def xlsx_openbank(movs: list[Mov]) -> bytes:
    import io

    import openpyxl

    libro = openpyxl.Workbook()
    hoja = libro.active
    hoja.append(["Openbank"])
    hoja.append(["Cuenta", "ES91 **** **** **** **** 1332"])
    hoja.append([])
    hoja.append(["Fecha Operación", "Fecha Valor", "Concepto", "Importe", "Saldo"])
    for mov, saldo in zip(movs, _saldos(movs), strict=True):
        hoja.append([mov.fecha, mov.fecha, mov.concepto, float(mov.importe), float(saldo)])
    for fila in hoja.iter_rows(min_row=5, max_col=2):
        for celda in fila:
            celda.number_format = "DD/MM/YYYY"
    salida = io.BytesIO()
    libro.save(salida)
    return salida.getvalue()


_TRNTYPE = {
    "tarjeta": "POS",
    "adeudo": "DIRECTDEBIT",
    "transferencia": "XFER",
    "prestamo": "PAYMENT",
    "comision": "FEE",
    "efectivo": "ATM",
    "abono": "CREDIT",
}


def _stmttrn(movs: list[Mov], cierre: bool) -> list[str]:
    salida = []
    for i, mov in enumerate(movs):
        nombre = mov.concepto[:32].replace("&", "&amp;")
        partes = [
            "<STMTTRN>",
            f"<TRNTYPE>{_TRNTYPE[mov.medio]}" + ("</TRNTYPE>" if cierre else ""),
            f"<DTPOSTED>{mov.fecha:%Y%m%d}120000" + ("</DTPOSTED>" if cierre else ""),
            f"<TRNAMT>{mov.importe:.2f}" + ("</TRNAMT>" if cierre else ""),
            f"<FITID>{mov.fecha:%Y%m%d}{i:05d}" + ("</FITID>" if cierre else ""),
            f"<NAME>{nombre}" + ("</NAME>" if cierre else ""),
            f"<MEMO>{mov.concepto.replace('&', '&amp;')}" + ("</MEMO>" if cierre else ""),
            "</STMTTRN>",
        ]
        salida.extend(partes)
    return salida


def ofx_sgml(movs: list[Mov]) -> bytes:
    cabecera = [
        "OFXHEADER:100",
        "DATA:OFXSGML",
        "VERSION:102",
        "SECURITY:NONE",
        "ENCODING:USASCII",
        "CHARSET:1252",
        "COMPRESSION:NONE",
        "OLDFILEUID:NONE",
        "NEWFILEUID:NONE",
        "",
    ]
    cuerpo = [
        "<OFX>",
        "<BANKMSGSRSV1>",
        "<STMTTRNRS>",
        "<TRNUID>1",
        "<STMTRS>",
        "<CURDEF>EUR",
        "<BANKACCTFROM>",
        "<BANKID>2100",
        f"<ACCTID>{IBAN}",
        "<ACCTTYPE>CHECKING",
        "</BANKACCTFROM>",
        "<BANKTRANLIST>",
        f"<DTSTART>{DESDE:%Y%m%d}",
        f"<DTEND>{HASTA:%Y%m%d}",
    ]
    cuerpo += _stmttrn(movs, cierre=False)
    cuerpo += ["</BANKTRANLIST>", "</STMTRS>", "</STMTTRNRS>", "</BANKMSGSRSV1>", "</OFX>"]
    return ("\r\n".join(cabecera + cuerpo) + "\r\n").encode("cp1252")


def qfx_xml(movs: list[Mov]) -> bytes:
    cabecera = [
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
        '<?OFX OFXHEADER="200" VERSION="220" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>',
    ]
    cuerpo = [
        "<OFX>",
        "<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>",
        "<DTSERVER>20261101</DTSERVER><LANGUAGE>SPA</LANGUAGE><INTU.BID>99999</INTU.BID></SONRS></SIGNONMSGSRSV1>",
        "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1</TRNUID><STMTRS><CURDEF>EUR</CURDEF>",
        f"<BANKACCTFROM><BANKID>2100</BANKID><ACCTID>{IBAN}</ACCTID><ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTFROM>",
        f"<BANKTRANLIST><DTSTART>{DESDE:%Y%m%d}</DTSTART><DTEND>{HASTA:%Y%m%d}</DTEND>",
    ]
    cuerpo += _stmttrn(movs, cierre=True)
    cuerpo += ["</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1>", "</OFX>"]
    return ("\n".join(cabecera + cuerpo) + "\n").encode("utf-8")


_BKTX = {
    "tarjeta": ("PMNT", "CCRD", "POSD"),
    "adeudo": ("PMNT", "RDDT", "ESDD"),
    "transferencia": ("PMNT", "ICDT", "ESCT"),
    "prestamo": ("LDAS", "CSLN", "PPAY"),
    "comision": ("ACMT", "MDOP", "CHRG"),
    "efectivo": ("PMNT", "CCRD", "CWDL"),
    "abono": ("PMNT", "RCDT", "SALA"),
}


def _xml(texto: str) -> str:
    return texto.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tx(mov: Mov, con_importe: bool) -> str:
    rol = "Cdtr" if mov.importe < 0 else "Dbtr"
    partes = ["<TxDtls>"]
    partes.append(
        "<Refs>"
        + (f"<MndtId>{mov.mandato}</MndtId>" if mov.mandato else "")
        + "<EndToEndId>NOTPROVIDED</EndToEndId></Refs>"
    )
    if con_importe:
        partes.append(f'<AmtDtls><TxAmt><Amt Ccy="EUR">{abs(mov.importe):.2f}</Amt></TxAmt></AmtDtls>')
    if mov.acreedor:
        partes.append(f"<RltdPties><{rol}><Pty><Nm>{_xml(mov.acreedor)}</Nm></Pty></{rol}></RltdPties>")
    partes.append(f"<RmtInf><Ustrd>{_xml(mov.concepto)}</Ustrd></RmtInf></TxDtls>")
    return "".join(partes)


def _ntry(movs: list[Mov], estado: str = "BOOK") -> str:
    total = sum((abs(x.importe) for x in movs), Decimal(0))
    primero = movs[0]
    dominio, familia, subfamilia = _BKTX[primero.medio]
    return (
        f'<Ntry><Amt Ccy="EUR">{total:.2f}</Amt><CdtDbtInd>{"DBIT" if primero.importe < 0 else "CRDT"}</CdtDbtInd>'
        f"<Sts><Cd>{estado}</Cd></Sts><BookgDt><Dt>{primero.fecha.isoformat()}</Dt></BookgDt>"
        f"<ValDt><Dt>{primero.fecha.isoformat()}</Dt></ValDt>"
        f"<BkTxCd><Domn><Cd>{dominio}</Cd><Fmly><Cd>{familia}</Cd><SubFmlyCd>{subfamilia}</SubFmlyCd></Fmly></Domn></BkTxCd>"
        f"<NtryDtls>{''.join(_tx(x, len(movs) > 1) for x in movs)}</NtryDtls></Ntry>"
    )


def camt053(movs: list[Mov]) -> bytes:
    # The gym and the association are collected in one batch entry each month.
    grupos: list[list[Mov]] = []
    lotes: dict[date, list[Mov]] = {}
    for mov in movs:
        if mov.mandato in {"BF-000123", "AVLA-77"}:
            if mov.fecha in lotes:
                lotes[mov.fecha].append(mov)
                continue
            lotes[mov.fecha] = [mov]
            grupos.append(lotes[mov.fecha])
        else:
            grupos.append([mov])
    entradas = [_ntry(grupo) for grupo in grupos]
    pendiente = Mov(HASTA, Decimal("-24.99"), "COMPRA TARJ. PENDIENTE TIENDA ONLINE", "tarjeta", "TIENDA ONLINE")
    entradas.append(_ntry([pendiente], "PDNG"))
    documento = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08">'
        "<BkToCstmrStmt><GrpHdr><MsgId>EXTRACTO-2026-10</MsgId><CreDtTm>2026-11-01T08:00:00</CreDtTm></GrpHdr>"
        "<Stmt><Id>STMT-1</Id><CreDtTm>2026-11-01T08:00:00</CreDtTm>"
        f"<Acct><Id><IBAN>{IBAN}</IBAN></Id><Ccy>EUR</Ccy></Acct>"
        + "\n".join(entradas)
        + "</Stmt></BkToCstmrStmt></Document>\n"
    )
    return documento.encode("utf-8")


FICHEROS = {
    "anual.n43": lambda m: norma43(m).encode("latin-1"),
    "santander.csv": csv_santander,
    "bbva.csv": csv_bbva,
    "ing.csv": csv_ing,
    "cargo_abono.tsv": csv_cargo_abono,
    "openbank.xlsx": xlsx_openbank,
    "anual.ofx": ofx_sgml,
    "anual.qfx": qfx_xml,
    "anual_camt053.xml": camt053,
}


def generar(destino: Path = FIXTURES) -> None:
    destino.mkdir(parents=True, exist_ok=True)
    movs = movimientos()
    for nombre, escritor in FICHEROS.items():
        (destino / nombre).write_bytes(escritor(movs))


if __name__ == "__main__":
    generar()
