"""Facts of the ``caducidades`` module: documents, vehicles, contracts and purchases of the household.

Stored as JSON facts of the user's case and validated here. No personal identifier is stored or needed: no
document number, licence plate or certificate serial — only the type of document, whose it is (titular,
cónyuge, hijo n) and its dates. Birth dates are needed only to apply the age rules (DNI, passport, driving
licence) when the expiry printed on the document is not given.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal
import json
import re
from typing import Any, Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from hacienda_gpt.modulos.caducidades.calculo import Modalidad, TipoBien, TipoVencimiento
from hacienda_gpt.modulos.caducidades.reglas import cargar_reglas

HECHO_DOCUMENTOS = "caducidades_documentos"
HECHO_VEHICULOS = "caducidades_vehiculos"
HECHO_CONTRATOS = "caducidades_contratos"
HECHO_COMPRAS = "caducidades_compras"
HECHO_FECHAS_NACIMIENTO = "caducidades_fechas_nacimiento"
HECHO_ANTELACIONES = "caducidades_antelaciones"

MAX_ELEMENTOS = 100
_TITULAR = re.compile(r"^(titular|conyuge|hijo_[1-9][0-9]?)$")
# Fields someone may try to store that the module deliberately does not keep.
_IDENTIFICADORES = {"numero", "número", "num", "dni", "nif", "nie", "matricula", "matrícula", "bastidor", "serie"}

TipoDocumento = Literal[
    "dni", "pasaporte", "permiso_conduccion", "licencia_conduccion", "certificado_electronico", "otro"
]
TipoContrato = Literal["seguro", "seguro_vida", "otro"]


def _sin_acentos(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(c))


def slug(texto: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", _sin_acentos(texto).lower()).strip("-")
    return base[:40].strip("-") or "x"


def normalizar_titular(valor: Any) -> str:
    """'Titular' -> 'titular'; 'cónyuge' -> 'conyuge'; 'hijo 2' / 'Hijo_2' -> 'hijo_2'."""
    if not isinstance(valor, str):
        raise ValueError("el titular debe ser «titular», «cónyuge» o «hijo N»")
    texto = re.sub(r"[\s-]+", "_", _sin_acentos(valor).strip().lower())
    if not _TITULAR.match(texto):
        raise ValueError(f"titular no válido ({valor}): usa «titular», «cónyuge» o «hijo N» (hijo 1, hijo 2…)")
    return texto


def titular_texto(titular: str) -> str:
    return {"titular": "titular", "conyuge": "cónyuge"}.get(titular, titular.replace("_", " "))


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _sin_identificadores(cls, data: Any) -> Any:
        if isinstance(data, dict):
            prohibidos = sorted(k for k in data if isinstance(k, str) and _sin_acentos(k.lower()) in _IDENTIFICADORES)
            if prohibidos:
                raise ValueError(
                    f"no guardes {', '.join(prohibidos)}: el número del documento, la matrícula u otros identificadores "
                    "no hacen falta (basta con el tipo, de quién es y las fechas)"
                )
        return data


class Documento(_Model):
    id: str | None = Field(default=None, max_length=60)
    tipo: TipoDocumento
    titular: str = "titular"
    nombre: str | None = Field(default=None, max_length=80)  # for "otro": what document it is
    fecha_expedicion: date | None = None  # issue date or start of the last renewal / extension
    fecha_caducidad: date | None = None  # as printed on the document
    clases: list[str] = Field(default_factory=list, max_length=14)  # driving licence classes
    tiene_dni: bool | None = None  # passport of a child under 14

    @field_validator("titular", mode="before")
    @classmethod
    def _titular(cls, value: Any) -> str:
        return normalizar_titular(value)

    @model_validator(mode="after")
    def _coherente(self) -> Documento:
        if self.tipo == "otro" and (not self.nombre or self.fecha_caducidad is None):
            raise ValueError("para otro documento indica su nombre y la fecha de caducidad que figura en él")
        if self.tipo in ("dni", "pasaporte", "permiso_conduccion", "licencia_conduccion"):
            if self.fecha_caducidad is None and self.fecha_expedicion is None:
                raise ValueError("indica la fecha de caducidad impresa o la de expedición (o última renovación)")
        if self.tipo == "certificado_electronico" and self.fecha_caducidad is None and self.fecha_expedicion is None:
            raise ValueError("indica la fecha de caducidad del certificado")
        if self.fecha_expedicion and self.fecha_caducidad and self.fecha_caducidad <= self.fecha_expedicion:
            raise ValueError("la fecha de caducidad debe ser posterior a la de expedición")
        if self.clases:
            regla = cargar_reglas().permiso_conduccion
            validas = regla.grupo1_clases + regla.grupo2_clases
            clases = ["".join(c.upper().split()) for c in self.clases]
            malas = [c for c in clases if c not in validas]
            if malas:
                raise ValueError(f"clases de permiso desconocidas: {', '.join(malas)} (válidas: {', '.join(validas)})")
            self.clases = list(dict.fromkeys(clases))
        if self.id is None:
            sufijo = f"-{slug(self.nombre)}" if self.tipo == "otro" and self.nombre else ""
            self.id = f"{self.tipo}-{self.titular}{sufijo}"
        return self


class Vehiculo(_Model):
    id: str | None = Field(default=None, max_length=60)
    etiqueta: str = Field(min_length=1, max_length=60)  # "coche familiar", "moto" — not the licence plate
    categoria: str
    fecha_primera_matriculacion: date
    fecha_ultima_itv: date | None = None  # last favourable inspection
    fecha_caducidad_itv_anterior: date | None = None  # the expiry that inspection renewed (art. 6.5)
    fecha_proxima_itv: date | None = None  # as written on the ITV card / sticker
    historico: bool = False

    @field_validator("categoria")
    @classmethod
    def _categoria(cls, value: str) -> str:
        categorias = cargar_reglas().itv.categorias
        if value not in categorias:
            raise ValueError(f"categoría desconocida ({value}); válidas: {', '.join(categorias)}")
        return value

    @model_validator(mode="after")
    def _coherente(self) -> Vehiculo:
        if self.fecha_ultima_itv and self.fecha_ultima_itv < self.fecha_primera_matriculacion:
            raise ValueError("la última ITV no puede ser anterior a la primera matriculación")
        if self.id is None:
            self.id = f"vehiculo-{slug(self.etiqueta)}"
        return self


class Contrato(_Model):
    id: str | None = Field(default=None, max_length=60)
    tipo: TipoContrato
    etiqueta: str = Field(min_length=1, max_length=80)  # "seguro del coche", "alarma"
    fecha_vencimiento: date  # end of the current period / next renewal
    periodicidad_meses: int | None = Field(default=None, ge=1, le=120)
    preaviso_dias: int | None = Field(default=None, ge=0, le=365)  # "otro": notice period set by the contract

    @model_validator(mode="after")
    def _coherente(self) -> Contrato:
        if self.tipo != "otro" and self.periodicidad_meses is None:
            self.periodicidad_meses = 12
        if self.id is None:
            self.id = f"contrato-{slug(self.etiqueta)}"
        return self


class Compra(_Model):
    id: str | None = Field(default=None, max_length=80)
    descripcion: str = Field(min_length=1, max_length=120)
    fecha_compra: date
    fecha_entrega: date | None = None
    tipo_bien: TipoBien = "bien"
    modalidad: Modalidad = "tienda"
    garantia_pactada_anos: int | None = Field(default=None, ge=0, le=10)  # second-hand only
    garantia_comercial_hasta: date | None = None
    vendedor_empresa: bool = True
    establecimiento: str | None = Field(default=None, max_length=80)
    importe: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)

    @model_validator(mode="after")
    def _coherente(self) -> Compra:
        if self.fecha_entrega and self.fecha_entrega < self.fecha_compra:
            raise ValueError("la fecha de entrega no puede ser anterior a la de compra")
        if self.garantia_pactada_anos is not None and self.tipo_bien != "segunda_mano":
            raise ValueError("solo en bienes de segunda mano se puede pactar una garantía más corta")
        if self.id is None:
            self.id = f"compra-{slug(self.descripcion)}-{self.fecha_compra.isoformat()}"
        return self


def _problemas(exc: ValidationError) -> str:
    partes = []
    for error in exc.errors():
        donde = ".".join(str(p) for p in error["loc"])
        mensaje = str(error["msg"]).removeprefix("Value error, ")
        partes.append(f"{donde}: {mensaje}" if donde else mensaje)
    return "; ".join(partes)


def _lista(valor: Any, modelo: type[_Model], nombre: str) -> list[dict[str, Any]]:
    if isinstance(valor, str):
        try:
            valor = json.loads(valor) if valor.strip() else []
        except json.JSONDecodeError as exc:
            raise ValueError(f"{nombre}: debe ser una lista JSON de objetos.") from exc
    if not isinstance(valor, list):
        raise ValueError(f"{nombre}: debe ser una lista de objetos.")
    if len(valor) > MAX_ELEMENTOS:
        raise ValueError(f"{nombre}: como máximo {MAX_ELEMENTOS} elementos.")
    salida: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for numero, item in enumerate(valor, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{nombre}, elemento {numero}: debe ser un objeto {{campo: valor}}.")
        try:
            modelo_item = modelo.model_validate(item)
        except ValidationError as exc:
            raise ValueError(f"{nombre}, elemento {numero}: {_problemas(exc)}.") from exc
        dato = modelo_item.model_dump(mode="json", exclude_none=True, exclude_defaults=False)
        if dato["id"] in vistos:
            raise ValueError(
                f"{nombre}: hay dos elementos con el mismo identificador «{dato['id']}»; dale un «id» distinto a uno."
            )
        vistos.add(dato["id"])
        salida.append(dato)
    return salida


def validar_documentos(valor: Any) -> list[dict[str, Any]]:
    return _lista(valor, Documento, "Documentos")


def validar_vehiculos(valor: Any) -> list[dict[str, Any]]:
    return _lista(valor, Vehiculo, "Vehículos")


def validar_contratos(valor: Any) -> list[dict[str, Any]]:
    return _lista(valor, Contrato, "Contratos y seguros")


def validar_compras(valor: Any) -> list[dict[str, Any]]:
    return _lista(valor, Compra, "Compras")


def _objeto(valor: Any, nombre: str) -> dict[str, Any]:
    if isinstance(valor, str):
        try:
            valor = json.loads(valor) if valor.strip() else {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"{nombre}: debe ser un objeto JSON.") from exc
    if not isinstance(valor, dict):
        raise ValueError(f"{nombre}: debe ser un objeto {{clave: valor}}.")
    return valor


def validar_fechas_nacimiento(valor: Any) -> dict[str, str]:
    """{"titular": "1980-05-02", "cónyuge": "...", "hijo 1": "..."} -> canonical holders and ISO dates."""
    salida: dict[str, str] = {}
    for clave, fecha in _objeto(valor, "Fechas de nacimiento").items():
        titular = normalizar_titular(clave)
        try:
            leida = date.fromisoformat(str(fecha))
        except ValueError as exc:
            raise ValueError(f"Fechas de nacimiento, {clave}: debe ser una fecha AAAA-MM-DD.") from exc
        salida[titular] = leida.isoformat()
    return dict(sorted(salida.items()))


TIPOS_ANTELACION: tuple[str, ...] = (
    "dni",
    "pasaporte",
    "permiso_conduccion",
    "certificado_electronico",
    "otro_documento",
    "itv",
    "seguro",
    "contrato",
    "desistimiento",
    "presuncion",
    "garantia",
    "garantia_comercial",
)


def validar_antelaciones(valor: Any) -> dict[str, int]:
    """{"itv": 45, "pasaporte": 120}: days before the date of the first notice, per type (1 to 365)."""
    salida: dict[str, int] = {}
    for clave, dias in _objeto(valor, "Antelación de los avisos").items():
        if clave not in TIPOS_ANTELACION:
            raise ValueError(
                f"Antelación de los avisos: tipo desconocido ({clave}); válidos: {', '.join(TIPOS_ANTELACION)}."
            )
        if isinstance(dias, bool) or not isinstance(dias, int | float) or int(dias) != dias or not 1 <= dias <= 365:
            raise ValueError(f"Antelación de los avisos, {clave}: debe ser un número entero de días entre 1 y 365.")
        salida[clave] = int(dias)
    return dict(sorted(salida.items()))


def leer_hecho(perfil: Mapping[str, Any], nombre: str, validar: Any, defecto: Any) -> tuple[Any, str | None]:
    """The validated value of a fact, or the default and a Spanish error if the stored value is invalid."""
    valor = perfil.get(nombre)
    if valor is None or valor == "":
        return defecto, None
    try:
        return validar(valor), None
    except ValueError as exc:
        return defecto, str(exc)


# Default lead times (days before the date) of the notices of each type. Product choices, NOT legal figures:
# long enough to book an appointment for the procedure. The first is configurable per type
# (``caducidades_antelaciones``); the shorter reminders below it are kept.
UMBRALES_POR_DEFECTO: dict[TipoVencimiento, tuple[int, ...]] = {
    "dni": (60, 30, 7),
    "pasaporte": (90, 30, 7),
    "permiso_conduccion": (90, 30, 7),
    "certificado_electronico": (30, 7),
    "otro_documento": (60, 30, 7),
    "itv": (30, 7),
    "seguro": (30, 7),
    "contrato": (30, 7),
    "desistimiento": (7, 2),
    "presuncion": (30,),
    "garantia": (30, 7),
    "garantia_comercial": (30, 7),
}


def umbrales(tipo: TipoVencimiento, antelaciones: Mapping[str, int]) -> tuple[int, ...]:
    base = UMBRALES_POR_DEFECTO[tipo]
    primero = antelaciones.get(tipo)
    if primero is None:
        return base
    return (primero, *(u for u in base if u < primero))
