"""Deterministic calculators of expiries, inspections, renewals, warranties and withdrawal periods.

Each calculator takes the dates the user gave (or printed on the document) and the rules read from the BOE
(:mod:`.reglas`) and returns a :class:`Vencimiento`: the date, what to do and where, a trace of lines
(concept, value, legal reference), the literal quotes it relied on and the caveats (``avisos``) of what is
not modelled. A date printed on the document always prevails over the computed one.

Periods are counted as art. 5.1 of the Código Civil says (quoted in ``reglas.json``, group ``computo``):
by days excluding the first one; by months or years "de fecha a fecha", and when the final month has no
equivalent day, the period ends on its last day.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.modulos.caducidades.reglas import (
    CategoriaITV,
    Evidencia,
    ReglaDocumentoPorEdad,
    Reglas,
    Tramite,
)

TipoVencimiento = Literal[
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
]
Origen = Literal["impresa", "calculada", "introducida"]


class CalculoError(ValueError):
    """Missing or inconsistent input: the message is Spanish and meant for the user."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Linea(_Model):
    concepto: str
    valor: str
    referencia: str = ""


class Vencimiento(_Model):
    """One dated obligation of the household: an expiry, a due inspection, a deadline."""

    id: str
    tipo: TipoVencimiento
    titulo: str
    # The expiry / due date; None when permanent or not computable (see ``avisos``).
    fecha: date | None
    # The last day to act, when earlier than ``fecha`` (e.g. to oppose an insurance renewal).
    accion_hasta: date | None = None
    permanente: bool = False
    origen: Origen = "calculada"
    que_hacer: str
    tramite: Tramite | None = None
    lineas: list[Linea] = Field(default_factory=list)
    citas: list[Evidencia] = Field(default_factory=list)
    avisos: list[str] = Field(default_factory=list)

    @property
    def fecha_aviso(self) -> date | None:
        """The date the notices count down to: the action deadline if any, else the expiry."""
        return self.accion_hasta or self.fecha


# --- Dates (art. 5.1 Código Civil) -----------------------------------------


def sumar_meses(inicio: date, meses: int) -> date:
    """``inicio`` plus whole months, "de fecha a fecha"; no equivalent day -> last day of that month."""
    total = inicio.year * 12 + inicio.month - 1 + meses
    year, month = divmod(total, 12)
    month += 1
    day = min(inicio.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def sumar_anos(inicio: date, anos: int) -> date:
    return sumar_meses(inicio, 12 * anos)


def sumar_dias(inicio: date, dias: int) -> date:
    """Last day of a period of ``dias`` natural days counted from ``inicio`` (which is excluded)."""
    return inicio + timedelta(days=dias)


def edad_en(nacimiento: date, fecha: date) -> int:
    """Whole years of age on ``fecha`` (a birthday counts from that very day)."""
    if fecha < nacimiento:
        raise CalculoError("La fecha es anterior a la fecha de nacimiento.")
    return fecha.year - nacimiento.year - ((fecha.month, fecha.day) < (nacimiento.month, nacimiento.day))


def fmt(fecha: date | None) -> str:
    return fecha.strftime("%d/%m/%Y") if fecha else "—"


def _anos_texto(anos: int) -> str:
    return "1 año" if anos == 1 else f"{anos} años"


def _meses_texto(meses: int) -> str:
    if meses % 12 == 0:
        return _anos_texto(meses // 12)
    return "1 mes" if meses == 1 else f"{meses} meses"


def _cita(grupo_evidencia: list[Evidencia], *articulos: str) -> list[Evidencia]:
    """The quotes of a group whose article starts with any of ``articulos``."""
    return [ev for ev in grupo_evidencia if any(ev.articulo.startswith(a) for a in articulos)]


def _comparar_impresa(
    impresa: date, calculada: date | None, permanente: bool, lineas: list[Linea], avisos: list[str]
) -> None:
    if permanente:
        avisos.append(
            f"Según la regla, este documento sería permanente, pero el documento indica {fmt(impresa)}: "
            "vale la fecha impresa."
        )
    elif calculada is not None and calculada != impresa:
        avisos.append(
            f"La fecha impresa ({fmt(impresa)}) no coincide con la calculada por la regla ({fmt(calculada)}): "
            "vale la impresa; si la diferencia es grande, revisa las fechas introducidas."
        )
    lineas.append(Linea(concepto="Fecha de caducidad impresa en el documento", valor=fmt(impresa)))


# --- DNI and passport -------------------------------------------------------


def _documento_por_edad(
    regla: ReglaDocumentoPorEdad,
    *,
    referencia: str,
    fecha_expedicion: date | None,
    fecha_nacimiento: date | None,
) -> tuple[date | None, bool, int, list[Linea]]:
    if fecha_expedicion is None or fecha_nacimiento is None:
        raise CalculoError(
            "Para calcular la caducidad hacen falta la fecha de expedición (o de la última renovación) y la fecha "
            "de nacimiento del titular; o bien introduce la fecha de caducidad impresa en el documento."
        )
    edad = edad_en(fecha_nacimiento, fecha_expedicion)
    tramo = regla.tramo(edad)
    lineas = [
        Linea(concepto="Fecha de expedición o renovación", valor=fmt(fecha_expedicion)),
        Linea(concepto="Edad del titular en esa fecha", valor=f"{edad} años", referencia=referencia),
    ]
    if tramo.anos is None:
        lineas.append(Linea(concepto="Validez", valor="permanente", referencia=referencia))
        return None, True, edad, lineas
    fecha = sumar_anos(fecha_expedicion, tramo.anos)
    lineas.append(Linea(concepto="Validez", valor=_anos_texto(tramo.anos), referencia=referencia))
    lineas.append(Linea(concepto="Caduca", valor=fmt(fecha), referencia="art. 5.1 Código Civil (de fecha a fecha)"))
    return fecha, False, edad, lineas


def calcular_dni(
    reglas: Reglas,
    *,
    id: str = "dni",
    titulo: str = "DNI",
    fecha_expedicion: date | None = None,
    fecha_nacimiento: date | None = None,
    fecha_caducidad: date | None = None,
) -> Vencimiento:
    regla = reglas.dni
    avisos = list(regla.avisos)
    lineas: list[Linea] = []
    calculada: date | None = None
    permanente = False
    if fecha_expedicion and fecha_nacimiento:
        calculada, permanente, _, lineas = _documento_por_edad(
            regla,
            referencia="art. 6.1 RD 1553/2005",
            fecha_expedicion=fecha_expedicion,
            fecha_nacimiento=fecha_nacimiento,
        )
    elif fecha_caducidad is None:
        _documento_por_edad(regla, referencia="", fecha_expedicion=fecha_expedicion, fecha_nacimiento=fecha_nacimiento)
    if fecha_caducidad is not None:
        _comparar_impresa(fecha_caducidad, calculada, permanente, lineas, avisos)
    return Vencimiento(
        id=id,
        tipo="dni",
        titulo=titulo,
        fecha=fecha_caducidad or calculada,
        permanente=permanente and fecha_caducidad is None,
        origen="impresa" if fecha_caducidad else "calculada",
        que_hacer="Renovar el DNI en una oficina de expedición (cita previa, en persona, foto y tasa).",
        tramite=regla.tramite,
        lineas=lineas,
        citas=_cita(regla.evidencia, "art. 6.1", "art. 7", "art. 5.1.b"),
        avisos=avisos,
    )


def calcular_pasaporte(
    reglas: Reglas,
    *,
    id: str = "pasaporte",
    titulo: str = "Pasaporte",
    fecha_expedicion: date | None = None,
    fecha_nacimiento: date | None = None,
    fecha_caducidad: date | None = None,
    tiene_dni: bool | None = None,
) -> Vencimiento:
    regla = reglas.pasaporte
    avisos = list(regla.avisos)
    lineas: list[Linea] = []
    calculada: date | None = None
    citas = _cita(regla.evidencia, "art. 5.1", "art. 4.1", "art. 3.2")
    if fecha_expedicion and fecha_nacimiento:
        calculada, _, edad, lineas = _documento_por_edad(
            regla,
            referencia="art. 5.1 RD 896/2003",
            fecha_expedicion=fecha_expedicion,
            fecha_nacimiento=fecha_nacimiento,
        )
        limite = regla.edad_menores_sin_dni
        if limite is not None and edad < limite and tiene_dni is not True and calculada is not None:
            tope = sumar_anos(fecha_nacimiento, limite)
            citas += _cita(regla.evidencia, "art. 5.2")
            if tope < calculada:
                lineas.append(
                    Linea(
                        concepto=f"Tope para menores de {limite} años sin DNI",
                        valor=fmt(tope),
                        referencia="art. 5.2 RD 896/2003",
                    )
                )
                avisos.append(
                    f"Si el menor no tenía DNI al expedirse el pasaporte, este vale como máximo hasta que cumple "
                    f"{limite} años ({fmt(tope)}); se toma esa fecha por prudencia. Si tenía DNI, vale hasta "
                    f"{fmt(calculada)}."
                )
                calculada = tope
    elif fecha_caducidad is None:
        _documento_por_edad(regla, referencia="", fecha_expedicion=fecha_expedicion, fecha_nacimiento=fecha_nacimiento)
    if fecha_caducidad is not None:
        _comparar_impresa(fecha_caducidad, calculada, False, lineas, avisos)
    return Vencimiento(
        id=id,
        tipo="pasaporte",
        titulo=titulo,
        fecha=fecha_caducidad or calculada,
        origen="impresa" if fecha_caducidad else "calculada",
        que_hacer="Renovar el pasaporte en una oficina de expedición (cita previa, en persona, DNI en vigor, foto y tasa).",
        tramite=regla.tramite,
        lineas=lineas,
        citas=citas,
        avisos=avisos,
    )


# --- Driving licence --------------------------------------------------------


def normalizar_clase(clase: str) -> str:
    return "".join(clase.upper().split())


def calcular_permiso(
    reglas: Reglas,
    *,
    id: str = "permiso_conduccion",
    titulo: str = "Permiso de conducción",
    clases: list[str] | None = None,
    fecha_inicio_vigencia: date | None = None,
    fecha_nacimiento: date | None = None,
    fecha_caducidad: date | None = None,
    es_licencia: bool = False,
) -> Vencimiento:
    """Art. 12 RGC. ``fecha_inicio_vigencia``: issue date or start of the last extension (prórroga)."""
    regla = reglas.permiso_conduccion
    avisos = list(regla.avisos)
    lineas: list[Linea] = []
    citas = _cita(regla.evidencia, "art. 12", "art. 13.1", "anexo III")
    clases_norm = [normalizar_clase(c) for c in (clases or [])]
    desconocidas = [c for c in clases_norm if c not in regla.grupo1_clases + regla.grupo2_clases]
    if desconocidas:
        raise CalculoError(
            f"Clase de permiso desconocida: {', '.join(desconocidas)}. Clases válidas: "
            f"{', '.join(regla.grupo1_clases + regla.grupo2_clases)}."
        )
    if not es_licencia and not clases_norm and fecha_caducidad is None:
        raise CalculoError("Indica las clases del permiso (por ejemplo B, o B y C1).")
    calculada: date | None = None
    if fecha_inicio_vigencia and fecha_nacimiento:
        edad = edad_en(fecha_nacimiento, fecha_inicio_vigencia)
        lineas.append(Linea(concepto="Inicio del período de vigencia", valor=fmt(fecha_inicio_vigencia)))
        lineas.append(Linea(concepto="Edad del titular en esa fecha", valor=f"{edad} años"))
        grupos: list[tuple[str, str, list[str]]] = []
        grupo2 = [c for c in clases_norm if c in regla.grupo2_clases]
        resto = [c for c in clases_norm if c not in regla.grupo2_clases]
        if grupo2:
            grupos.append(("grupo2", "art. 12.1 RGC", grupo2))
        if resto or es_licencia or not clases_norm:
            grupos.append(("resto", "art. 12.2 RGC", resto or ["licencia de conducción"]))
        for nombre, referencia, cls in grupos:
            vig = regla.grupo2 if nombre == "grupo2" else regla.resto
            anos = vig.anos if edad < vig.edad else vig.anos_desde_edad
            fecha = sumar_anos(fecha_inicio_vigencia, anos)
            lineas.append(
                Linea(
                    concepto=f"Vigencia de {', '.join(cls)} ({'menos de' if edad < vig.edad else 'desde los'} {vig.edad} años)",
                    valor=f"{_anos_texto(anos)}, hasta {fmt(fecha)}",
                    referencia=referencia,
                )
            )
            calculada = fecha if calculada is None else min(calculada, fecha)
            # The next period, if the extension is requested within the window before expiry (art. 13.1).
            edad_siguiente = edad_en(fecha_nacimiento, fecha)
            anos_sig = vig.anos if edad_siguiente < vig.edad else vig.anos_desde_edad
            lineas.append(
                Linea(
                    concepto=f"Siguiente prórroga de {', '.join(cls)} (con {edad_siguiente} años al caducar)",
                    valor=f"{_anos_texto(anos_sig)} desde {fmt(fecha)}, hasta {fmt(sumar_anos(fecha, anos_sig))}",
                    referencia=f"{referencia}; art. 13.1 RGC",
                )
            )
        if len(grupos) > 1:
            avisos.append(
                "Tienes clases de los dos grupos: la prórroga del permiso de las clases del grupo 2 implica la de las "
                "del grupo 1 por los plazos que les correspondan (art. 13.1 RGC)."
            )
    elif fecha_caducidad is None:
        raise CalculoError(
            "Para calcular la caducidad del permiso hacen falta la fecha de expedición o de la última prórroga y la "
            "fecha de nacimiento del titular; o bien introduce la fecha de caducidad impresa en el permiso."
        )
    if fecha_caducidad is not None:
        _comparar_impresa(fecha_caducidad, calculada, False, lineas, avisos)
    fecha_final = fecha_caducidad or calculada
    if fecha_final is not None:
        lineas.append(
            Linea(
                concepto="Desde cuándo se puede pedir la prórroga sin perder días",
                valor=fmt(sumar_meses(fecha_final, -regla.antelacion_maxima_meses)),
                referencia="art. 13.1 RGC",
            )
        )
    return Vencimiento(
        id=id,
        tipo="permiso_conduccion",
        titulo=titulo,
        fecha=fecha_final,
        origen="impresa" if fecha_caducidad else "calculada",
        que_hacer="Pedir la prórroga de vigencia: informe de aptitud psicofísica y solicitud ante la Jefatura de Tráfico.",
        tramite=regla.tramite,
        lineas=lineas,
        citas=citas,
        avisos=avisos,
    )


# --- Vehicle inspection (ITV) -----------------------------------------------


def _antiguedad(primera_matriculacion: date, fecha: date) -> int:
    return edad_en(primera_matriculacion, fecha) if fecha >= primera_matriculacion else 0


def _fin_exencion(categoria: CategoriaITV, primera_matriculacion: date) -> date | None:
    """First inspection of a vehicle never inspected: when it leaves the exempt band (or the first period)."""
    primero = categoria.tramos[0]
    if primero.meses is None:
        return sumar_anos(primera_matriculacion, primero.hasta_anos or 0)
    return sumar_meses(primera_matriculacion, primero.meses)


def _siguiente_itv(categoria: CategoriaITV, primera_matriculacion: date, base: date) -> tuple[date, int, int | None]:
    """Next due date counted from ``base``: frequency by the vehicle's age on ``base``."""
    antiguedad = _antiguedad(primera_matriculacion, base)
    tramo = categoria.tramo(antiguedad)
    if tramo.meses is None:  # still exempt (a voluntary inspection): next is when the exemption ends
        fin = _fin_exencion(categoria, primera_matriculacion)
        assert fin is not None
        return fin, antiguedad, None
    return sumar_meses(base, tramo.meses), antiguedad, tramo.meses


def calcular_itv(
    reglas: Reglas,
    *,
    hoy: date,
    categoria: str,
    fecha_primera_matriculacion: date,
    id: str = "itv",
    titulo: str = "ITV",
    fecha_ultima_itv: date | None = None,
    fecha_caducidad_anterior: date | None = None,
    fecha_proxima_itv: date | None = None,
) -> Vencimiento:
    """Art. 6 RD 920/2017: next periodic inspection of a vehicle."""
    regla = reglas.itv
    if categoria not in regla.categorias:
        raise CalculoError(f"Categoría de vehículo desconocida: {categoria}. Válidas: {', '.join(regla.categorias)}.")
    cat = regla.categorias[categoria]
    avisos = list(regla.avisos)
    citas = [ev for ev in regla.evidencia if categoria_en_cita(categoria, ev.articulo)]
    citas += _cita(regla.evidencia, "art. 6.5", "art. 17")
    citas += [ev for ev in regla.evidencia if ev.articulo == "art. 6.1 RD 920/2017"]
    lineas = [
        Linea(concepto="Categoría", valor=cat.descripcion, referencia="art. 6.1 RD 920/2017"),
        Linea(concepto="Primera matriculación", valor=fmt(fecha_primera_matriculacion), referencia="art. 6.1"),
    ]
    if fecha_primera_matriculacion > hoy:
        raise CalculoError("La fecha de primera matriculación es posterior a hoy.")
    calculada: date
    if fecha_ultima_itv is not None:
        if fecha_ultima_itv < fecha_primera_matriculacion:
            raise CalculoError("La última ITV es anterior a la primera matriculación.")
        base = fecha_ultima_itv
        lineas.append(Linea(concepto="Última inspección favorable", valor=fmt(fecha_ultima_itv)))
        if (
            fecha_caducidad_anterior is not None
            and sumar_dias(fecha_caducidad_anterior, -regla.margen_anticipo_dias)
            <= fecha_ultima_itv
            <= fecha_caducidad_anterior
        ):
            base = fecha_caducidad_anterior
            lineas.append(
                Linea(
                    concepto=f"Pasada en los {regla.margen_anticipo_dias} días anteriores a la caducidad: se cuenta desde",
                    valor=fmt(base),
                    referencia="art. 6.5 RD 920/2017",
                )
            )
        calculada, antiguedad, meses = _siguiente_itv(cat, fecha_primera_matriculacion, base)
        lineas.append(
            Linea(
                concepto=f"Antigüedad del vehículo el {fmt(base)}",
                valor=_anos_texto(antiguedad),
                referencia="art. 6.1 RD 920/2017",
            )
        )
        lineas.append(
            Linea(
                concepto="Frecuencia aplicable",
                valor=_meses_texto(meses) if meses else "exento: la siguiente es al terminar la exención",
                referencia="art. 6.1 RD 920/2017",
            )
        )
    else:
        primera = _fin_exencion(cat, fecha_primera_matriculacion)
        assert primera is not None
        lineas.append(Linea(concepto="Primera inspección", valor=fmt(primera), referencia="art. 6.1 RD 920/2017"))
        calculada = primera
        pasos = 0
        while calculada < hoy and pasos < 200:
            siguiente, antiguedad, meses = _siguiente_itv(cat, fecha_primera_matriculacion, calculada)
            lineas.append(
                Linea(
                    concepto=f"Tras la del {fmt(calculada)} (antigüedad {_anos_texto(antiguedad)})",
                    valor=f"{_meses_texto(meses) if meses else 'exento'} -> {fmt(siguiente)}",
                    referencia="art. 6.1 y 6.5 RD 920/2017",
                )
            )
            calculada = siguiente
            pasos += 1
        if pasos:
            avisos.append(
                "Sin la fecha de la última ITV, se supone que cada inspección se pasó a su vencimiento (o en los 30 días "
                "anteriores, que mantienen el calendario según el art. 6.5). Si pasaste alguna tarde, la fecha cambia: "
                "introduce la de la última inspección favorable."
            )
    if fecha_proxima_itv is not None:
        _comparar_impresa(fecha_proxima_itv, calculada, False, lineas, avisos)
    fecha = fecha_proxima_itv or calculada
    lineas.append(
        Linea(
            concepto="Desde cuándo se puede pasar sin perder días",
            valor=fmt(sumar_dias(fecha, -regla.margen_anticipo_dias)),
            referencia="art. 6.5 RD 920/2017",
        )
    )
    return Vencimiento(
        id=id,
        tipo="itv",
        titulo=titulo,
        fecha=fecha,
        origen="impresa" if fecha_proxima_itv else "calculada",
        que_hacer="Pasar la ITV en la estación que elijas (cita previa; tarjeta ITV y permiso de circulación).",
        tramite=regla.tramite,
        lineas=lineas,
        citas=citas,
        avisos=avisos,
    )


_CITA_CATEGORIA = {
    "M1": "(M1)",
    "M2_M3": "(M2, M3)",
    "N1": "(N1)",
    "N2_N3_O": "(N2, N3, O2, O3, O4)",
    "O2_caravana": "(O2 caravanas)",
    "L1e": "(L1e)",
    "L": "(resto L)",
    "escuela_M1_L": "(escuela)",
    "M1_taxi_ambulancia_escolar": "(taxis",
}


def categoria_en_cita(categoria: str, articulo: str) -> bool:
    marca = _CITA_CATEGORIA.get(categoria)
    return (marca is not None and marca in articulo) or (categoria == "L" and "art. 6.4" in articulo)


# --- Electronic certificate, other documents --------------------------------


def calcular_certificado(
    reglas: Reglas,
    *,
    id: str = "certificado_electronico",
    titulo: str = "Certificado electrónico",
    fecha_caducidad: date | None = None,
    fecha_expedicion: date | None = None,
) -> Vencimiento:
    regla = reglas.certificado_electronico
    avisos = list(regla.avisos)
    lineas: list[Linea] = []
    if fecha_expedicion is not None:
        maximo = sumar_anos(fecha_expedicion, regla.vigencia_maxima_anos)
        lineas.append(
            Linea(
                concepto="Vigencia máxima legal desde la expedición",
                valor=f"{_anos_texto(regla.vigencia_maxima_anos)}: no después del {fmt(maximo)}",
                referencia="art. 4.2 Ley 6/2020",
            )
        )
        if fecha_caducidad is not None and fecha_caducidad > maximo:
            avisos.append(
                "La fecha de caducidad introducida supera la vigencia máxima legal desde la expedición: revisa las fechas."
            )
    if fecha_caducidad is not None:
        lineas.append(Linea(concepto="Caducidad indicada en el certificado", valor=fmt(fecha_caducidad)))
    else:
        avisos.append(
            "Sin la fecha de caducidad del propio certificado no se programa ningún aviso: la ley solo fija un máximo "
            "y el prestador puede darle una vigencia menor."
        )
    return Vencimiento(
        id=id,
        tipo="certificado_electronico",
        titulo=titulo,
        fecha=fecha_caducidad,
        origen="impresa" if fecha_caducidad else "calculada",
        que_hacer="Renovar el certificado con su prestador antes de que caduque (para la FNMT, desde su sede electrónica).",
        tramite=regla.tramite,
        lineas=lineas,
        citas=list(regla.evidencia),
        avisos=avisos,
    )


def calcular_otro_documento(*, id: str, titulo: str, fecha_caducidad: date, que_hacer: str = "") -> Vencimiento:
    """A document whose expiry the user typed from the document itself (no legal rule applied)."""
    return Vencimiento(
        id=id,
        tipo="otro_documento",
        titulo=titulo,
        fecha=fecha_caducidad,
        origen="introducida",
        que_hacer=que_hacer or "Renovarlo antes de la fecha indicada en el documento.",
        lineas=[Linea(concepto="Caducidad indicada en el documento", valor=fmt(fecha_caducidad))],
        avisos=["Fecha introducida por ti desde el propio documento; no se aplica ninguna regla legal."],
    )


# --- Insurance and other contracts ------------------------------------------


def _proxima(fecha: date, periodicidad_meses: int | None, hoy: date) -> tuple[date, int]:
    """Roll a renewal date forward by its period until it is today or later; returns (date, periods rolled)."""
    if not periodicidad_meses:
        return fecha, 0
    n = 0
    while fecha < hoy and n < 1200:
        n += 1
        fecha = sumar_meses(fecha, periodicidad_meses)
    return fecha, n


def calcular_seguro(
    reglas: Reglas,
    *,
    hoy: date,
    fecha_vencimiento: date,
    id: str = "seguro",
    titulo: str = "Seguro",
    periodicidad_meses: int = 12,
    es_vida: bool = False,
) -> Vencimiento:
    """Art. 22 Ley 50/1980: renewal date and the last day to oppose the extension (policyholder: one month)."""
    regla = reglas.seguro
    avisos = list(regla.avisos)
    fecha, rodadas = _proxima(fecha_vencimiento, periodicidad_meses, hoy)
    lineas = [Linea(concepto="Vencimiento del período en curso", valor=fmt(fecha))]
    if rodadas:
        avisos.append(
            f"La fecha introducida ({fmt(fecha_vencimiento)}) ya pasó: se supone que la póliza se ha prorrogado cada "
            f"{_meses_texto(periodicidad_meses)}. Si no es así, actualiza la fecha."
        )
    accion: date | None = None
    if es_vida:
        avisos.append(
            "En el seguro de vida, las reglas de oposición a la prórroga del art. 22 no se aplican en lo que sean "
            "incompatibles con su regulación (art. 22.5): mira tu póliza."
        )
        que_hacer = "Revisar la renovación del seguro de vida según su póliza."
        citas = _cita(regla.evidencia, "art. 22.5")
    else:
        accion = sumar_meses(fecha, -regla.oposicion_tomador_meses)
        lineas.append(
            Linea(
                concepto="Último día para comunicar por escrito que no quieres renovar",
                valor=fmt(accion),
                referencia="art. 22.2 Ley 50/1980 (al menos un mes antes)",
            )
        )
        lineas.append(
            Linea(
                concepto="La aseguradora debe comunicarte cambios del contrato como tarde",
                valor=fmt(sumar_meses(fecha, -regla.comunicacion_modificacion_meses)),
                referencia="art. 22.3 Ley 50/1980",
            )
        )
        que_hacer = f"Decidir si renuevas: si no, notifícalo por escrito a la aseguradora como tarde el {fmt(accion)}."
        citas = _cita(regla.evidencia, "art. 22.2", "art. 22.3")
    return Vencimiento(
        id=id,
        tipo="seguro",
        titulo=titulo,
        fecha=fecha,
        accion_hasta=accion,
        origen="introducida",
        que_hacer=que_hacer,
        tramite=None if es_vida else regla.tramite,
        lineas=lineas,
        citas=citas,
        avisos=avisos,
    )


def calcular_contrato(
    *,
    hoy: date,
    fecha_vencimiento: date,
    id: str = "contrato",
    titulo: str = "Contrato",
    periodicidad_meses: int | None = None,
    preaviso_dias: int | None = None,
) -> Vencimiento:
    """A contract with a renewal date and, optionally, the notice period its own terms set (user-entered)."""
    fecha, rodadas = _proxima(fecha_vencimiento, periodicidad_meses, hoy)
    avisos = ["Fechas y preaviso introducidos por ti según tu contrato; no se aplica ninguna regla legal."]
    if rodadas:
        avisos.append(f"Se supone que el contrato se ha renovado cada {_meses_texto(periodicidad_meses or 0)}.")
    lineas = [Linea(concepto="Vencimiento o renovación", valor=fmt(fecha))]
    accion = None
    if preaviso_dias:
        accion = sumar_dias(fecha, -preaviso_dias)
        lineas.append(Linea(concepto=f"Último día para avisar ({preaviso_dias} días de preaviso)", valor=fmt(accion)))
    return Vencimiento(
        id=id,
        tipo="contrato",
        titulo=titulo,
        fecha=fecha,
        accion_hasta=accion,
        origen="introducida",
        que_hacer="Revisar la renovación: compararla y, si no te interesa, darla de baja dentro del preaviso.",
        lineas=lineas,
        avisos=avisos,
    )


# --- Purchases: legal guarantee and withdrawal --------------------------------

TipoBien = Literal["bien", "digital", "segunda_mano"]
Modalidad = Literal["tienda", "distancia", "fuera_establecimiento", "visita_no_solicitada", "excursion"]


class ResultadoCompra(_Model):
    descripcion: str
    fecha_entrega: date
    fin_desistimiento: date | None
    fin_presuncion: date | None
    fin_garantia: date | None
    fin_garantia_comercial: date | None = None
    vencimientos: list[Vencimiento] = Field(default_factory=list)
    lineas: list[Linea] = Field(default_factory=list)
    citas: list[Evidencia] = Field(default_factory=list)
    avisos: list[str] = Field(default_factory=list)


def calcular_compra(
    reglas: Reglas,
    *,
    hoy: date,
    descripcion: str,
    fecha_compra: date,
    id: str = "compra",
    fecha_entrega: date | None = None,
    tipo_bien: TipoBien = "bien",
    modalidad: Modalidad = "tienda",
    garantia_pactada_anos: int | None = None,
    garantia_comercial_hasta: date | None = None,
    vendedor_empresa: bool = True,
) -> ResultadoCompra:
    """Arts. 102-105 and 120-124 TRLGDCU: withdrawal and legal guarantee dates of a consumer purchase."""
    g = reglas.garantia
    d = reglas.desistimiento
    if fecha_entrega is not None and fecha_entrega < fecha_compra:
        raise CalculoError("La fecha de entrega no puede ser anterior a la de compra.")
    entrega = fecha_entrega or fecha_compra
    lineas = [
        Linea(concepto="Compra", valor=fmt(fecha_compra)),
        Linea(
            concepto="Entrega (la del tique o factura, o la del albarán si es posterior)",
            valor=fmt(entrega),
            referencia="art. 123.1 TRLGDCU",
        ),
    ]
    avisos: list[str] = []
    citas: list[Evidencia] = []
    vencimientos: list[Vencimiento] = []
    fin_desistimiento: date | None = None
    fin_presuncion: date | None = None
    fin_garantia: date | None = None

    if not vendedor_empresa:
        avisos.append(
            "Entre particulares no se aplica la garantía legal ni el desistimiento del TRLGDCU, que protegen al "
            "consumidor frente a un empresario: solo las reglas generales del Código Civil (no modeladas)."
        )
        return ResultadoCompra(
            descripcion=descripcion,
            fecha_entrega=entrega,
            fin_desistimiento=None,
            fin_presuncion=None,
            fin_garantia=None,
            lineas=lineas,
            avisos=avisos,
        )

    # Withdrawal (distance and off-premises contracts only).
    if modalidad == "tienda":
        lineas.append(
            Linea(
                concepto="Desistimiento legal",
                valor="no aplica a compras en tienda física (solo la política de devoluciones de la tienda)",
                referencia="art. 92 TRLGDCU",
            )
        )
        citas += _cita(d.evidencia, "art. 92")
    else:
        dias = d.dias_ampliado if modalidad in ("visita_no_solicitada", "excursion") else d.dias
        inicio = fecha_compra if tipo_bien == "digital" else entrega
        fin_desistimiento = sumar_dias(inicio, dias)
        lineas.append(
            Linea(
                concepto=f"Desistimiento: {dias} días naturales desde la "
                + ("celebración del contrato" if tipo_bien == "digital" else "entrega"),
                valor=f"hasta el {fmt(fin_desistimiento)}",
                referencia="arts. 102.1 y 104 TRLGDCU; art. 5.1 Código Civil",
            )
        )
        citas += _cita(d.evidencia, "art. 102.1", "art. 104", "art. 105.1", "art. 5.1")
        avisos.extend(d.avisos)
        avisos.append(
            "Hay excepciones al desistimiento (art. 103 TRLGDCU): por ejemplo, bienes hechos a medida o personalizados, "
            "bienes que caducan rápido, bienes precintados por higiene o grabaciones y programas desprecintados, y el "
            "contenido digital sin soporte ya empezado con tu consentimiento."
        )
        avisos.append(
            f"Si el vendedor no te informó del derecho de desistimiento, el plazo se amplía {d.prorroga_sin_informacion_meses} "
            "meses (art. 105.1 TRLGDCU)."
        )
        vencimientos.append(
            Vencimiento(
                id=f"{id}:desistimiento",
                tipo="desistimiento",
                titulo=f"Devolución (desistimiento) de «{descripcion}»",
                fecha=fin_desistimiento,
                que_hacer="Si quieres devolverlo, comunica al vendedor que desistes antes de que acabe el plazo.",
                tramite=d.tramite,
                lineas=[lineas[-1]],
                citas=_cita(d.evidencia, "art. 102.1", "art. 104"),
            )
        )

    # Legal guarantee.
    if entrega < g.en_vigor_desde:
        avisos.append(
            f"Entregado antes del {fmt(g.en_vigor_desde)}: rige la redacción anterior del TRLGDCU, que no se modela."
        )
        if sumar_anos(entrega, g.bienes_anos) < hoy:
            avisos.append("Incluso con el plazo actual de tres años, la garantía legal ya habría terminado.")
    else:
        citas += _cita(g.evidencia, "art. 120.1", "art. 121.1", "art. 123.1", "art. 124")
        if tipo_bien == "digital":
            anos, presuncion = g.digital_anos, g.presuncion_digital_anos
            ref = "art. 120.1 TRLGDCU (contenidos o servicios digitales)"
        else:
            anos, presuncion = g.bienes_anos, g.presuncion_bienes_anos
            ref = "art. 120.1 TRLGDCU (bienes)"
        if tipo_bien == "segunda_mano" and garantia_pactada_anos is not None:
            if garantia_pactada_anos < g.segunda_mano_minimo_anos:
                avisos.append(
                    f"En bienes de segunda mano el plazo pactado no puede ser inferior a {_anos_texto(g.segunda_mano_minimo_anos)}"
                    " (art. 120.1): se toma ese mínimo."
                )
                garantia_pactada_anos = g.segunda_mano_minimo_anos
            if garantia_pactada_anos < anos:
                anos = garantia_pactada_anos
                ref = "art. 120.1 TRLGDCU (segunda mano, plazo pactado)"
                avisos.append(
                    "Segunda mano: también se puede haber pactado una presunción más corta, nunca inferior al plazo de "
                    "garantía pactado (art. 121.1)."
                )
                presuncion = min(presuncion, anos)
        elif tipo_bien == "segunda_mano":
            avisos.append(
                "Segunda mano: si en el contrato se pactó una garantía más corta (mínimo un año), indícalo; si no, rige "
                "el plazo general."
            )
        fin_garantia = sumar_anos(entrega, anos)
        fin_presuncion = sumar_anos(entrega, presuncion)
        lineas.append(
            Linea(
                concepto="Garantía legal: falta de conformidad que se manifieste en",
                valor=f"{_anos_texto(anos)}, hasta el {fmt(fin_garantia)}",
                referencia=ref,
            )
        )
        lineas.append(
            Linea(
                concepto="Se presume que el defecto ya existía (la prueba recae en el vendedor) hasta",
                valor=fmt(fin_presuncion),
                referencia="art. 121.1 TRLGDCU",
            )
        )
        lineas.append(
            Linea(
                concepto="La acción para reclamar prescribe",
                valor=f"{_anos_texto(g.prescripcion_anos)} desde que se manifiesta el defecto",
                referencia="art. 124 TRLGDCU",
            )
        )
        avisos.extend(g.avisos)
        avisos.append("Guarda el tique o la factura: fija la fecha de entrega (art. 123.1 TRLGDCU).")
        if fin_presuncion < fin_garantia:
            vencimientos.append(
                Vencimiento(
                    id=f"{id}:presuncion",
                    tipo="presuncion",
                    titulo=f"Fin de la presunción a tu favor de «{descripcion}»",
                    fecha=fin_presuncion,
                    que_hacer=(
                        "Revisa que funciona bien: después de esta fecha tendrías que probar que el defecto existía "
                        "al entregártelo."
                    ),
                    lineas=[lineas[-2]],
                    citas=_cita(g.evidencia, "art. 121.1"),
                )
            )
        vencimientos.append(
            Vencimiento(
                id=f"{id}:garantia",
                tipo="garantia",
                titulo=f"Fin de la garantía legal de «{descripcion}»",
                fecha=fin_garantia,
                que_hacer="Si tiene algún defecto, reclama al vendedor antes de esta fecha (reparación o sustitución).",
                lineas=[lineas[-3]],
                citas=_cita(g.evidencia, "art. 120.1"),
            )
        )
    if garantia_comercial_hasta is not None:
        lineas.append(
            Linea(
                concepto="Garantía comercial adicional (según su documento)",
                valor=f"hasta el {fmt(garantia_comercial_hasta)}",
            )
        )
        vencimientos.append(
            Vencimiento(
                id=f"{id}:garantia_comercial",
                tipo="garantia_comercial",
                titulo=f"Fin de la garantía comercial de «{descripcion}»",
                fecha=garantia_comercial_hasta,
                origen="introducida",
                que_hacer="Si tiene algún defecto cubierto, usa la garantía comercial antes de esta fecha.",
                lineas=[lineas[-1]],
            )
        )
    return ResultadoCompra(
        descripcion=descripcion,
        fecha_entrega=entrega,
        fin_desistimiento=fin_desistimiento,
        fin_presuncion=fin_presuncion,
        fin_garantia=fin_garantia,
        fin_garantia_comercial=garantia_comercial_hasta,
        vencimientos=vencimientos,
        lineas=lineas,
        citas=citas,
        avisos=avisos,
    )
