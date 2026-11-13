"""Data shapes shared by the importers, the detector and the tools of the ``suscripciones`` module.

Raw bank movements (:class:`Movimiento`) live only in memory while a statement is analysed; what is
stored in the user's case is the derived list of recurring services (:class:`ServicioGuardado`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# How the money left the account, as far as the statement tells.
Medio = Literal["adeudo", "tarjeta", "transferencia", "prestamo", "efectivo", "comision", "desconocido"]

Periodicidad = Literal["semanal", "mensual", "bimestral", "trimestral", "semestral", "anual"]

# Legal regime the cancellation letter is built on.
Sector = Literal["general", "telecomunicaciones", "seguros", "suministros", "financiero", "no_aplica"]

Categoria = Literal[
    "streaming_video",
    "musica_audio",
    "software_nube",
    "prensa_lectura",
    "videojuegos",
    "telecomunicaciones",
    "electricidad_gas",
    "agua",
    "seguros",
    "prestamos",
    "gimnasio_deporte",
    "transporte",
    "vivienda",
    "cuotas_asociaciones",
    "banca_comisiones",
    "comercio_online",
    "otros",
]
CATEGORIAS: tuple[str, ...] = Categoria.__args__  # type: ignore[attr-defined]

ETIQUETAS_CATEGORIA: dict[str, str] = {
    "streaming_video": "Streaming de vídeo",
    "musica_audio": "Música y audio",
    "software_nube": "Software y almacenamiento en la nube",
    "prensa_lectura": "Prensa y lectura",
    "videojuegos": "Videojuegos",
    "telecomunicaciones": "Telefonía e internet",
    "electricidad_gas": "Electricidad y gas",
    "agua": "Agua",
    "seguros": "Seguros",
    "prestamos": "Préstamos",
    "gimnasio_deporte": "Gimnasio y deporte",
    "transporte": "Transporte",
    "vivienda": "Vivienda (alquiler, comunidad)",
    "cuotas_asociaciones": "Cuotas (asociaciones, colegios, ONG)",
    "banca_comisiones": "Comisiones bancarias",
    "comercio_online": "Comercio online",
    "otros": "Otros",
}

# Charges per year of each periodicity, for the annual cost.
CARGOS_POR_ANO: dict[str, int] = {
    "semanal": 52,
    "mensual": 12,
    "bimestral": 6,
    "trimestral": 4,
    "semestral": 2,
    "anual": 1,
}


class ErrorImportacion(ValueError):
    """The statement cannot be read; the message is Spanish and tells the user what to do."""


@dataclass(frozen=True)
class Movimiento:
    """One booked movement of a bank statement. ``importe`` is negative for charges (cargos)."""

    fecha: date
    importe: Decimal
    descripcion: str
    fecha_valor: date | None = None
    # Counterparty name when the format carries it apart (camt.053 creditor, OFX MEMO, CSV detail column).
    contraparte: str | None = None
    # Mandate id (SEPA direct debit), FITID or N43 reference; never shown to the user.
    referencia: str | None = None
    medio: Medio = "desconocido"
    divisa: str = "EUR"


@dataclass(frozen=True)
class Extracto:
    """A parsed statement. ``cuenta`` keeps only the last four digits of the account."""

    formato: str
    movimientos: tuple[Movimiento, ...]
    avisos: tuple[str, ...] = ()
    cuenta: str | None = None
    banco: str | None = None
    # Column mapping actually used (CSV/XLSX): field -> header as written in the file.
    columnas: dict[str, str] = field(default_factory=dict)

    @property
    def desde(self) -> date | None:
        return min((m.fecha for m in self.movimientos), default=None)

    @property
    def hasta(self) -> date | None:
        return max((m.fecha for m in self.movimientos), default=None)


# --- Derived data stored in the case -----------------------------------------------------------


class _Modelo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CambioPrecio(_Modelo):
    fecha: date
    anterior: Decimal
    nuevo: Decimal
    porcentaje: Decimal  # (nuevo - anterior) / anterior * 100, two decimals


class Duplicado(_Modelo):
    # "cargo_doble": the same service charged twice in one period; "servicio_duplicado": two
    # separate recurring charges for the same service (e.g. card and PayPal).
    tipo: Literal["cargo_doble", "servicio_duplicado"]
    fecha: date
    importe: Decimal
    detalle: str


class PruebaConvertida(_Modelo):
    fecha_prueba: date
    importe_prueba: Decimal
    fecha_primer_pago: date


class ServicioGuardado(_Modelo):
    """One recurring service as stored in the fact ``suscripciones_servicios`` (no raw movements)."""

    id: str = Field(pattern=r"^srv-[0-9a-f]{10}$")
    etiqueta: str = Field(min_length=1, max_length=80)
    categoria: Categoria
    categoria_origen: Literal["catalogo", "extracto", "llm", "sin_clasificar"] = "catalogo"
    sector: Sector
    periodicidad: Periodicidad
    importe: Decimal  # last charge, positive
    importe_variable: bool = False
    medio: Medio = "desconocido"
    primera_fecha: date
    ultima_fecha: date
    proxima_fecha: date | None
    estado: Literal["activa", "inactiva"]
    confianza: Decimal = Field(ge=0, le=1)
    num_cargos: int = Field(ge=1)
    cambio_precio: CambioPrecio | None = None
    duplicado: Duplicado | None = None
    prueba: PruebaConvertida | None = None

    @property
    def coste_anual(self) -> Decimal:
        return (self.importe * CARGOS_POR_ANO[self.periodicidad]).quantize(Decimal("0.01"))


class ServiciosGuardados(_Modelo):
    """Value of the fact ``suscripciones_servicios``."""

    actualizado_el: date
    # Last day covered by the most recent statement analysed.
    extracto_hasta: date | None = None
    servicios: list[ServicioGuardado] = Field(default_factory=list)
