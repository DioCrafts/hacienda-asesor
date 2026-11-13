"""Household consumption: parsing distributor CSV exports and the compact form stored in the case.

Formats accepted (detected from the header, not from the file name):

* Datadis export: ``CUPS;Fecha;Hora;Consumo_kWh;Metodo_obtencion[;Energia_vertida_kWh]`` with the date as
  ``AAAA/MM/DD`` and the hour as ``01:00``…``24:00`` (the hour *ending* at that time);
* e-distribución / i-DE style: ``CUPS;Fecha;Hora;AE_kWh;REAL/ESTIMADO`` with ``DD/MM/AAAA`` and the hour as a
  number 1-24 (1-23 or 1-25 on the days the clocks change);
* quarter-hour files (``Hora`` like ``00:15``) are added up per hour.

These layouts were written from the formats the distributors document, not from files downloaded here
(their sites are not reachable from this machine): parsing is driven by the header names and tolerates
``;``/``,``/tab separators, decimal commas and both hour conventions. Exported energy (autoconsumo) is
ignored and reported.

What is stored in the case (fact ``luz_consumo``) is NOT the whole hourly series: a year is 8.760 values.
It keeps (1) per-day totals by period P1/P2/P3 for the last 400 days, enough to rebuild any bill of a fixed
price tariff exactly; (2) the hourly values of the last 62 days, enough to rebuild the last bill of an
hourly-priced tariff (PVPC); (3) per-month sums by hour of day, for PVPC estimates of older months;
(4) per-month maximum hourly energy; and (5) manual totals and maximeter readings the user typed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import io
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from hacienda_gpt.modulos.luz.datos import PERIODOS
from hacienda_gpt.modulos.luz.periodos import ZONAS, Zona, hora_local, horas_del_dia, periodo_energia

DIAS_GUARDADOS = 400
DIAS_HORARIOS = 62
KWH = Decimal("0.001")


# --- Parsing -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Lectura:
    """Energy of one hour: local date, ordinal hour of the day (0 = first hour) and kWh."""

    fecha: date
    indice: int
    kwh: Decimal
    real: bool | None = None


@dataclass
class Importacion:
    lecturas: list[Lectura]
    cups: str | None
    formato: str
    avisos: list[str] = field(default_factory=list)


class FormatoNoReconocidoError(ValueError):
    """The CSV could not be read. The message is Spanish, for the user."""


def _normaliza(texto: str) -> str:
    texto = texto.strip().lower()
    for a, b in zip("áéíóúü", "aeiouu", strict=True):
        texto = texto.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "_", texto).strip("_")


def _columna(cabecera: list[str], *claves: str, excluir: tuple[str, ...] = ()) -> int | None:
    for i, nombre in enumerate(cabecera):
        if any(c in nombre for c in claves) and not any(x in nombre for x in excluir):
            return i
    return None


def _decimal(texto: str) -> Decimal:
    t = texto.strip().replace(" ", "")
    if "," in t and "." in t:  # 1.234,5 -> 1234.5
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", ".")
    return Decimal(t)


def _fecha(texto: str) -> date:
    t = texto.strip().split(" ")[0].split("T")[0]
    for formato in ("%Y/%m/%d", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(t, formato).date()
        except ValueError:
            continue
    raise ValueError(f"fecha no reconocida: «{texto}»")


def _hora(texto: str) -> tuple[int, int]:
    """(hour, minute) of a 'Hora' cell: '1', '01', '01:00', '00:15', '00:00-01:00' (start of range)."""
    t = texto.strip()
    if "-" in t:
        t = t.split("-")[0].strip()
    if ":" in t:
        h, m = t.split(":")[:2]
        return int(h), int(m)
    return int(t), 0


def importar_csv(texto: str, zona: Zona = "peninsula") -> Importacion:
    """Hourly readings of a distributor CSV export (see the module docstring for the layouts)."""
    lineas = [line for line in texto.replace("﻿", "").splitlines() if line.strip()]
    if len(lineas) < 2:
        raise FormatoNoReconocidoError("El CSV está vacío o solo tiene la cabecera.")
    muestra = lineas[0]
    separador = max((";", "\t", ","), key=muestra.count)
    filas = list(csv.reader(io.StringIO("\n".join(lineas)), delimiter=separador))
    cabecera = [_normaliza(c) for c in filas[0]]
    i_fecha = _columna(cabecera, "fecha", "date", "dia")
    i_hora = _columna(cabecera, "hora", "hour", "time", "periodo_horario")
    i_kwh = _columna(
        cabecera, "consumo", "ae_kwh", "ae", "energia_activa", "consumption", "kwh", excluir=("vertid", "as_")
    )
    i_cups = _columna(cabecera, "cups")
    i_metodo = _columna(cabecera, "metodo", "real", "estimad", "obtencion", "method")
    i_vertida = _columna(cabecera, "vertid", "as_kwh", "excedent", "surplus")
    if i_fecha is None or i_kwh is None:
        raise FormatoNoReconocidoError(
            "No encuentro las columnas de fecha y consumo en la cabecera "
            f"({'; '.join(filas[0])}). Exporta el CSV horario de Datadis o de tu distribuidora."
        )
    if i_hora is None and i_fecha is not None and not any(":" in f[i_fecha] for f in filas[1:3] if len(f) > i_fecha):
        raise FormatoNoReconocidoError("El CSV no tiene columna de hora: hace falta el consumo horario.")

    avisos: list[str] = []
    crudas: list[tuple[date, int, int, Decimal, bool | None]] = []
    cups: str | None = None
    vertida = Decimal(0)
    for n, fila in enumerate(filas[1:], start=2):
        if len(fila) <= max(i for i in (i_fecha, i_kwh, i_hora) if i is not None):
            continue
        try:
            fecha = _fecha(fila[i_fecha])
            celda_hora = fila[i_hora] if i_hora is not None else fila[i_fecha].strip().split(" ")[1]
            hora, minuto = _hora(celda_hora)
            kwh = _decimal(fila[i_kwh]) if fila[i_kwh].strip() else Decimal(0)
        except (ValueError, InvalidOperation, IndexError) as exc:
            raise FormatoNoReconocidoError(f"Línea {n} ilegible: {'; '.join(fila)} ({exc}).") from exc
        real = None
        if i_metodo is not None and i_metodo < len(fila):
            metodo = _normaliza(fila[i_metodo])
            real = False if metodo.startswith("e") else True if metodo.startswith("r") else None
        if i_cups is not None and fila[i_cups].strip():
            cups = fila[i_cups].strip().upper()
        if i_vertida is not None and i_vertida < len(fila) and fila[i_vertida].strip():
            try:
                vertida += _decimal(fila[i_vertida])
            except InvalidOperation:
                pass
        crudas.append((fecha, hora, minuto, kwh, real))

    if not crudas:
        raise FormatoNoReconocidoError("El CSV no tiene lecturas.")
    cuartohorario = any(m not in (0,) for _, _, m, _, _ in crudas)
    horas = {h for _, h, _, _, _ in crudas}
    # Hour-ending convention (1..24, '01:00'..'24:00') unless an hour 0 appears.
    inicio_cero = 0 in horas and 24 not in horas
    por_hora: dict[tuple[date, int], Decimal] = defaultdict(Decimal)
    reales: dict[tuple[date, int], bool | None] = {}
    vistos: dict[date, int] = defaultdict(int)
    for fecha, hora, minuto, kwh, real in crudas:
        if cuartohorario:
            indice = hora  # quarter-hours 00:00, 00:15 ... belong to the hour that starts at hh
            if not inicio_cero and minuto == 0 and hora > 0:
                indice = hora - 1  # '01:00' closes the hour 00-01
        else:
            # Start-of-hour files repeat the clock hour when clocks go back: use the order within the day.
            indice = vistos[fecha] if inicio_cero else hora - 1
            vistos[fecha] += 1
        clave = (fecha, indice)
        por_hora[clave] += kwh
        if real is False or clave not in reales:
            reales[clave] = real
    if cuartohorario:
        avisos.append("El fichero es cuartohorario: se han sumado los cuartos de cada hora.")
    if vertida:
        avisos.append(
            f"El fichero trae {vertida} kWh de energía vertida (autoconsumo): este módulo no calcula su compensación."
        )
    lecturas = [Lectura(f, i, k, reales.get((f, i))) for (f, i), k in sorted(por_hora.items())]
    estimadas = sum(1 for lectura in lecturas if lectura.real is False)
    if estimadas:
        avisos.append(f"{estimadas} hora(s) son estimadas por la distribuidora, no leídas del contador.")
    for dia in sorted({lectura.fecha for lectura in lecturas}):
        n_horas = sum(1 for lectura in lecturas if lectura.fecha == dia)
        esperadas = horas_del_dia(dia, zona)
        if n_horas != esperadas:
            avisos.append(f"El {dia:%d-%m-%Y} tiene {n_horas} horas y deberían ser {esperadas}.")
            if len(avisos) > 12:
                avisos.append("(hay más días incompletos)")
                break
    formato = "datadis" if any("metodo_obtencion" in c for c in cabecera) else "distribuidora"
    return Importacion(lecturas=lecturas, cups=cups, formato=formato, avisos=avisos)


# --- Compact storage (fact luz_consumo) -----------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TotalesPeriodo(_Model):
    """Consumption a bill shows, typed by the user: billing period and kWh per period."""

    desde: date
    hasta: date
    p1: Decimal = Field(ge=0)
    p2: Decimal = Field(ge=0)
    p3: Decimal = Field(ge=0)

    @field_validator("hasta")
    @classmethod
    def _orden(cls, v: date, info: Any) -> date:
        if "desde" in info.data and v < info.data["desde"]:
            raise ValueError("«hasta» es anterior a «desde»")
        return v


class Maximetro(_Model):
    mes: str = Field(pattern=r"^\d{4}-\d{2}$")
    p1_kw: Decimal = Field(ge=0)
    p2_kw: Decimal | None = Field(default=None, ge=0)


class ResumenMes(_Model):
    kwh: Decimal
    max_kwh_hora: Decimal
    # Sum of each hour of the day over the month (24 values), for PVPC estimates.
    perfil: list[Decimal] = Field(min_length=24, max_length=24)


class ConsumoGuardado(_Model):
    fuente: str = "manual"
    zona: Zona = "peninsula"
    importado_el: date | None = None
    # 'AAAA-MM-DD' -> [P1, P2, P3] kWh of that day.
    dias: dict[date, list[Decimal]] = Field(default_factory=dict)
    # 'AAAA-MM-DD' -> kWh of each hour of the day in order (23, 24 or 25 values).
    horario: dict[date, list[Decimal]] = Field(default_factory=dict)
    meses: dict[str, ResumenMes] = Field(default_factory=dict)
    manual: list[TotalesPeriodo] = Field(default_factory=list)
    maximetro: list[Maximetro] = Field(default_factory=list)
    horas_estimadas: int = 0

    @field_validator("dias")
    @classmethod
    def _tres(cls, v: dict[date, list[Decimal]]) -> dict[date, list[Decimal]]:
        for dia, valores in v.items():
            if len(valores) != 3 or any(x < 0 for x in valores):
                raise ValueError(f"el día {dia} debe tener tres consumos (P1, P2, P3) no negativos")
        return v

    def rango(self) -> tuple[date, date] | None:
        if not self.dias:
            return None
        return min(self.dias), max(self.dias)

    def totales(self, desde: date, hasta: date) -> dict[str, Decimal] | None:
        """kWh by period between two dates (inclusive) from the daily data, if every day is there."""
        dias = [desde + timedelta(days=i) for i in range((hasta - desde).days + 1)]
        if not all(d in self.dias for d in dias):
            return None
        return {p: sum((self.dias[d][i] for d in dias), Decimal(0)) for i, p in enumerate(PERIODOS)}

    def manual_para(self, desde: date, hasta: date) -> TotalesPeriodo | None:
        return next((m for m in self.manual if m.desde == desde and m.hasta == hasta), None)

    def lecturas_horarias(self, desde: date, hasta: date) -> list[Lectura] | None:
        dias = [desde + timedelta(days=i) for i in range((hasta - desde).days + 1)]
        if not all(d in self.horario for d in dias):
            return None
        return [Lectura(d, i, kwh) for d in dias for i, kwh in enumerate(self.horario[d])]

    def a_json(self) -> dict[str, Any]:
        def num(x: Decimal) -> str:
            return format(x.normalize(), "f") if x else "0"

        return {
            "fuente": self.fuente,
            "zona": self.zona,
            "importado_el": self.importado_el.isoformat() if self.importado_el else None,
            "dias": {d.isoformat(): [num(x) for x in v] for d, v in sorted(self.dias.items())},
            "horario": {d.isoformat(): [num(x) for x in v] for d, v in sorted(self.horario.items())},
            "meses": {
                m: {"kwh": num(r.kwh), "max_kwh_hora": num(r.max_kwh_hora), "perfil": [num(x) for x in r.perfil]}
                for m, r in sorted(self.meses.items())
            },
            "manual": [
                {
                    "desde": t.desde.isoformat(),
                    "hasta": t.hasta.isoformat(),
                    "p1": num(t.p1),
                    "p2": num(t.p2),
                    "p3": num(t.p3),
                }
                for t in self.manual
            ],
            "maximetro": [
                {"mes": x.mes, "p1_kw": num(x.p1_kw), "p2_kw": num(x.p2_kw) if x.p2_kw is not None else None}
                for x in self.maximetro
            ],
            "horas_estimadas": self.horas_estimadas,
        }


def compactar(
    lecturas: Iterable[Lectura],
    zona: Zona = "peninsula",
    fuente: str = "csv",
    importado_el: date | None = None,
    previo: ConsumoGuardado | None = None,
) -> ConsumoGuardado:
    """The stored form of hourly readings, merged over what was already stored (new days win)."""
    lecturas = list(lecturas)
    dias: dict[date, list[Decimal]] = dict(previo.dias) if previo else {}
    horario: dict[date, list[Decimal]] = dict(previo.horario) if previo else {}
    meses: dict[str, ResumenMes] = dict(previo.meses) if previo else {}
    por_dia: dict[date, list[Lectura]] = defaultdict(list)
    for lectura in lecturas:
        por_dia[lectura.fecha].append(lectura)
    por_mes: dict[str, list[Lectura]] = defaultdict(list)
    for dia, del_dia in por_dia.items():
        del_dia.sort(key=lambda x: x.indice)
        suma = {p: Decimal(0) for p in PERIODOS}
        for lectura in del_dia:
            suma[periodo_energia(dia, lectura.indice, zona)] += lectura.kwh
        dias[dia] = [suma[p] for p in PERIODOS]
        horario[dia] = [lectura.kwh for lectura in del_dia]
        por_mes[f"{dia:%Y-%m}"].extend(del_dia)
    for mes, del_mes in por_mes.items():
        perfil = [Decimal(0)] * 24
        for lectura in del_mes:
            h = hora_local(lectura.fecha, lectura.indice, zona)
            perfil[h] += lectura.kwh
        meses[mes] = ResumenMes(
            kwh=sum((x.kwh for x in del_mes), Decimal(0)),
            max_kwh_hora=max((x.kwh for x in del_mes), default=Decimal(0)),
            perfil=perfil,
        )
    if dias:
        ultimo = max(dias)
        dias = {d: v for d, v in dias.items() if (ultimo - d).days < DIAS_GUARDADOS}
        horario = {d: v for d, v in horario.items() if (ultimo - d).days < DIAS_HORARIOS}
        meses = {m: r for m, r in meses.items() if m >= f"{ultimo - timedelta(days=DIAS_GUARDADOS):%Y-%m}"}
    return ConsumoGuardado(
        fuente=fuente,
        zona=zona,
        importado_el=importado_el,
        dias=dias,
        horario=horario,
        meses=meses,
        manual=list(previo.manual) if previo else [],
        maximetro=list(previo.maximetro) if previo else [],
        horas_estimadas=(previo.horas_estimadas if previo else 0) + sum(1 for x in lecturas if x.real is False),
    )


def validar_consumo(valor: Any) -> dict[str, Any]:
    """``validar`` of the fact ``luz_consumo``: canonical JSON or a Spanish ValueError."""
    if isinstance(valor, ConsumoGuardado):
        return valor.a_json()
    if not isinstance(valor, Mapping):
        raise ValueError("El consumo debe ser un objeto JSON (lo genera luz_importar_consumo).")
    try:
        return ConsumoGuardado.model_validate(dict(valor)).a_json()
    except ValidationError as exc:
        raise ValueError(f"Consumo no válido: {exc.errors()[0]['msg']} en {exc.errors()[0]['loc']}") from exc


def cargar_consumo(valor: Any) -> ConsumoGuardado | None:
    if valor in (None, "", {}):
        return None
    if isinstance(valor, ConsumoGuardado):
        return valor
    return ConsumoGuardado.model_validate(valor)


def zona_valida(valor: Any) -> Zona:
    if valor in ZONAS:
        return valor  # type: ignore[no-any-return]
    raise ValueError(f"zona desconocida «{valor}»: usa una de {', '.join(ZONAS)}")
