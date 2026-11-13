"""The household as the benefits see it, read from the profile facts.

Shared facts (estado_civil, descendientes, rendimiento_trabajo_bruto…) are read, never redefined; this
module's own facts are prefixed ``ayudas_``. Whatever is estimated rather than declared is said in
``notas`` and the evaluators lower their verdict to "probable".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
import json
from typing import Any

from hacienda_gpt.tax.profile import DatosInvalidosError, normalizar_datos_conyuge, normalizar_descendientes

# Identifiers of the stable benefits (family A), also the values of ``ayudas_ya_solicitadas``.
PRESTACIONES: dict[str, str] = {
    "imv": "Ingreso mínimo vital",
    "capi": "Complemento de ayuda para la infancia",
    "hijo_discapacidad": "Asignación por hijo o menor a cargo con discapacidad",
    "nacimiento": "Prestación por nacimiento o adopción (familia numerosa, monoparental o progenitor con discapacidad)",
    "bono_social_electrico": "Bono social eléctrico",
    "bono_social_termico": "Bono social térmico",
    "titulo_monoparental": "Título de familia monoparental",
}

# Income facts of the fiscal profile used to estimate the household's income when it is not declared.
_INGRESOS_PERFIL = (
    "rendimiento_trabajo_bruto",
    "rendimiento_actividad_neto",
    "rendimiento_alquiler_neto",
    "rendimiento_capital_mobiliario",
    "ganancias_patrimoniales",
)


def numero(valor: Any) -> Decimal | None:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        return Decimal(str(valor).strip().replace(",", ".")) if str(valor).strip() else None
    except InvalidOperation:
        return None


def entero(valor: Any) -> int | None:
    n = numero(valor)
    return int(n) if n is not None else None


def booleano(valor: Any) -> bool | None:
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, str) and valor.strip().lower() in {"true", "sí", "si", "1"}:
        return True
    if isinstance(valor, str) and valor.strip().lower() in {"false", "no", "0"}:
        return False
    return None


def _edad_en(nacimiento: date, hoy: date) -> int:
    return hoy.year - nacimiento.year - ((hoy.month, hoy.day) < (nacimiento.month, nacimiento.day))


@dataclass(frozen=True)
class Hijo:
    numero: int
    edad: int | None  # at ``hoy`` when the date of birth is known, else as declared for the tax year
    fecha_nacimiento: date | None
    grado_discapacidad: int  # percentage, as the profile stores it
    necesita_asistencia: bool


@dataclass
class Hogar:
    hoy: date
    edad: int | None = None
    adultos: int | None = None
    menores: int | None = None
    hijos: list[Hijo] = field(default_factory=list)
    conyuge: bool = False
    familia_numerosa: bool | None = None
    familia_monoparental: bool | None = None
    # Someone in the household with a recognised disability of at least 33 % / 65 % (None: unknown).
    discapacidad_33: bool | None = None
    discapacidad_65: bool | None = None
    # Children with a disability counted in the fiscal profile (art. 60.2 LIRPF facts).
    descendientes_discapacidad: int = 0
    victima_violencia_genero: bool = False
    victima_terrorismo: bool = False
    dependencia_grado_2_3: bool = False
    electrodependencia: bool = False
    pensionistas_cuantia_minima: bool | None = None
    pvpc: bool | None = None
    ingresos: Decimal | None = None
    ingresos_estimados: bool = False
    base_imponible: Decimal | None = None
    base_imponible_estimada: bool = False
    patrimonio: Decimal | None = None
    comunidad: str | None = None
    alquiler: bool = False
    ya_solicitadas: frozenset[str] = frozenset()
    composicion_estimada: bool = False
    notas: list[str] = field(default_factory=list)

    @property
    def miembros(self) -> int | None:
        if self.adultos is None or self.menores is None:
            return None
        return self.adultos + self.menores


def _hijos(perfil: Mapping[str, Any], hoy: date, notas: list[str]) -> list[Hijo]:
    valor = perfil.get("descendientes")
    if valor in (None, "", []):
        return []
    try:
        detalle = normalizar_descendientes(valor)
    except DatosInvalidosError as exc:
        notas.append(f"No se ha podido leer el detalle de los hijos: {exc}")
        return []
    hijos: list[Hijo] = []
    for numero_hijo, item in enumerate(detalle, start=1):
        nacimiento = date.fromisoformat(item["fecha_nacimiento"]) if item.get("fecha_nacimiento") else None
        edad = _edad_en(nacimiento, hoy) if nacimiento else entero(item.get("edad"))
        hijos.append(
            Hijo(
                numero=numero_hijo,
                edad=edad,
                fecha_nacimiento=nacimiento,
                grado_discapacidad=int(item.get("grado_discapacidad") or 0),
                necesita_asistencia=bool(item.get("necesita_asistencia")),
            )
        )
    return hijos


def _conyuge(perfil: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
    estado = str(perfil.get("estado_civil") or "").strip().lower()
    con_pareja = estado.startswith("casad") or "pareja" in estado
    datos: dict[str, Any] = {}
    if perfil.get("datos_conyuge"):
        try:
            datos = normalizar_datos_conyuge(perfil["datos_conyuge"])
        except (DatosInvalidosError, ValueError):
            datos = {}
    return con_pareja, datos


def _ingresos_estimados(perfil: Mapping[str, Any], conyuge: Mapping[str, Any]) -> Decimal | None:
    """Gross income minus social contributions of the user (and spouse) from the fiscal facts, or None."""
    total = Decimal(0)
    alguno = False
    for fuente in (perfil, conyuge):
        for nombre in _INGRESOS_PERFIL:
            valor = numero(fuente.get(nombre))
            if valor is not None:
                alguno = True
                total += max(valor, Decimal(0)) if nombre == "ganancias_patrimoniales" else valor
        cotizaciones = numero(fuente.get("cotizaciones_seguridad_social"))
        if cotizaciones is not None:
            total -= cotizaciones
    return max(total, Decimal(0)) if alguno else None


def _ya_solicitadas(valor: Any) -> frozenset[str]:
    if isinstance(valor, str):
        try:
            valor = json.loads(valor) if valor.strip().startswith("[") else [valor]
        except json.JSONDecodeError:
            return frozenset()
    if not isinstance(valor, list):
        return frozenset()
    return frozenset(str(item) for item in valor if str(item) in PRESTACIONES)


def hogar_del_perfil(perfil: Mapping[str, Any], hoy: date) -> Hogar:
    notas: list[str] = []
    hijos = _hijos(perfil, hoy, notas)
    con_pareja, datos_conyuge = _conyuge(perfil)
    hogar = Hogar(hoy=hoy, edad=entero(perfil.get("edad")), hijos=hijos, conyuge=con_pareja, notas=notas)

    # Composition: declared, else estimated from the fiscal facts.
    adultos = entero(perfil.get("ayudas_adultos_hogar"))
    menores = entero(perfil.get("ayudas_menores_hogar"))
    if adultos is None:
        mayores = sum(1 for h in hijos if h.edad is not None and h.edad >= 18)
        ascendientes = entero(perfil.get("num_ascendientes_mayores_65")) or 0
        adultos = 1 + int(con_pareja) + mayores + ascendientes
        hogar.composicion_estimada = True
    if menores is None:
        num_desc = entero(perfil.get("num_descendientes"))
        if hijos and all(h.edad is not None for h in hijos):
            menores = sum(1 for h in hijos if h.edad is not None and h.edad < 18)
            hogar.composicion_estimada = True
        elif not hijos and not num_desc:
            # No children stated: counting none can only lower the thresholds (conservative).
            menores = 0
            hogar.composicion_estimada = hogar.composicion_estimada or num_desc is None
        # Children declared without their ages: the number of minors stays unknown.
    hogar.adultos, hogar.menores = adultos, menores
    if hogar.composicion_estimada:
        notas.append(
            "La composición del hogar se ha deducido del perfil fiscal (tú, tu cónyuge o pareja, los hijos y los "
            "ascendientes que conviven): indica «ayudas_adultos_hogar» y «ayudas_menores_hogar» si no es así."
        )

    hogar.familia_numerosa = booleano(perfil.get("familia_numerosa"))
    hogar.familia_monoparental = booleano(perfil.get("familia_monoparental"))

    grados = [entero(perfil.get("grado_discapacidad")), entero(datos_conyuge.get("grado_discapacidad"))]
    grados.append(entero(perfil.get("grado_discapacidad_conyuge")))
    grados.extend(h.grado_discapacidad for h in hijos)
    conocidos = [g for g in grados if g is not None]
    contadores_33 = ("num_descendientes_discapacidad", "num_ascendientes_discapacidad")
    contadores_65 = ("num_descendientes_discapacidad_65", "num_ascendientes_discapacidad_65")
    hay_65 = any(g >= 65 for g in conocidos) or any((entero(perfil.get(c)) or 0) > 0 for c in contadores_65)
    hay_33 = hay_65 or any(g >= 33 for g in conocidos) or any((entero(perfil.get(c)) or 0) > 0 for c in contadores_33)
    hogar.discapacidad_65 = True if hay_65 else (False if conocidos else None)
    hogar.discapacidad_33 = True if hay_33 else (False if conocidos else None)
    hogar.descendientes_discapacidad = entero(perfil.get("num_descendientes_discapacidad")) or 0

    hogar.victima_violencia_genero = booleano(perfil.get("victima_violencia_genero")) is True
    hogar.victima_terrorismo = booleano(perfil.get("victima_terrorismo")) is True
    hogar.dependencia_grado_2_3 = booleano(perfil.get("ayudas_dependencia_grado_2_3")) is True
    hogar.electrodependencia = booleano(perfil.get("ayudas_electrodependencia")) is True
    hogar.pensionistas_cuantia_minima = booleano(perfil.get("ayudas_pensionistas_cuantia_minima"))
    hogar.pvpc = booleano(perfil.get("ayudas_contrato_pvpc"))
    hogar.alquiler = (numero(perfil.get("alquiler_vivienda_habitual")) or Decimal(0)) > 0
    comunidad = perfil.get("comunidad_autonoma")
    hogar.comunidad = comunidad.strip() if isinstance(comunidad, str) and comunidad.strip() else None

    declarado = numero(perfil.get("ayudas_ingresos_hogar"))
    estimado = _ingresos_estimados(perfil, datos_conyuge) if declarado is None else None
    hogar.ingresos = declarado if declarado is not None else estimado
    hogar.ingresos_estimados = declarado is None and estimado is not None
    base = numero(perfil.get("ayudas_base_imponible_hogar"))
    if base is not None:
        hogar.base_imponible = base
    elif hogar.ingresos is not None:
        # Gross income minus contributions is never below the taxable base: a conservative stand-in.
        hogar.base_imponible = hogar.ingresos
        hogar.base_imponible_estimada = True
    if hogar.ingresos_estimados or hogar.base_imponible_estimada:
        notas.append(
            "Los ingresos del hogar se han estimado con los datos fiscales del perfil (ingresos íntegros menos "
            "cotizaciones tuyos y de tu cónyuge), que pueden ser de otro año y no incluyen a los demás miembros: "
            "indica «ayudas_ingresos_hogar» y «ayudas_base_imponible_hogar» para un resultado firme."
        )
    hogar.patrimonio = numero(perfil.get("ayudas_patrimonio_neto_hogar"))
    hogar.ya_solicitadas = _ya_solicitadas(perfil.get("ayudas_ya_solicitadas"))
    return hogar
