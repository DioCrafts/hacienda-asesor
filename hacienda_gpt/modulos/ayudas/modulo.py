"""The ``ayudas`` module: public aid the household is entitled to and has not asked for.

Two families:

- stable benefits regulated by State law (ingreso mínimo vital, complemento de ayuda para la infancia,
  asignación por hijo con discapacidad, prestación por nacimiento, bonos sociales eléctrico y térmico,
  título de familia monoparental), evaluated deterministically from versioned data with literal quotes
  (``prestaciones``);
- calls for aid published in the BOE, detected daily and matched conservatively against the profile
  (``convocatorias``, ``revision``, ``almacen``).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import json
import os
from typing import Any

from hacienda_gpt.agent.providers.base import ToolSpec
from hacienda_gpt.modulos.ayudas import almacen as almacen_mod
from hacienda_gpt.modulos.ayudas.convocatorias import TEMAS, Convocatoria, Encaje, encaje
from hacienda_gpt.modulos.ayudas.hogar import PRESTACIONES, Hogar, entero, hogar_del_perfil
from hacienda_gpt.modulos.ayudas.parametros import PrestacionesParams, ejercicio_para, load_prestaciones
from hacienda_gpt.modulos.ayudas.prestaciones import ORDEN_ESTADOS, Evaluacion, eur, evaluar_todas, fecha
from hacienda_gpt.modulos.ayudas.revision import actualizar_convocatorias
from hacienda_gpt.modulos.contrato import (
    AvisoModulo,
    ContextoModulo,
    FuenteModulo,
    HechoModulo,
    HerramientaModulo,
    ModuloBase,
    ResultadoHerramienta,
)

CLAVE = "ayudas"
ETIQUETAS_ESTADO = {
    "cumple": "Te corresponde (con los datos del perfil)",
    "probable": "Probablemente te corresponde",
    "automatica": "La recibirás sin pedirla",
    "proximamente": "Podrás pedirlo pronto",
    "ya_solicitada": "Ya la cobras o la has pedido",
    "faltan_datos": "Faltan datos para saberlo",
    "no_cumple": "No te corresponde",
}
# A notice for something that opens later is only sent this many days before.
DIAS_ANTELACION_PROXIMAMENTE = 60
DIAS_URGENTE = 15


# --- Facts -------------------------------------------------------------------------


def _entero_entre(minimo: int, maximo: int, que: str) -> Any:
    def validar(valor: Any) -> int:
        n = entero(valor)
        if n is None or not minimo <= n <= maximo:
            raise ValueError(f"{que}: debe ser un número entero entre {minimo} y {maximo}.")
        return n

    return validar


def _importe(que: str) -> Any:
    def validar(valor: Any) -> float:
        try:
            n = float(str(valor).replace(",", "."))
        except ValueError as exc:
            raise ValueError(f"{que}: debe ser un importe en euros.") from exc
        if n < 0 or n > 10_000_000:
            raise ValueError(f"{que}: debe ser un importe entre 0 y 10.000.000 euros.")
        return n

    return validar


def _lista_de(validos: Mapping[str, str] | Mapping[str, Any], que: str) -> Any:
    def validar(valor: Any) -> list[str]:
        if isinstance(valor, str):
            texto = valor.strip()
            valor = json.loads(texto) if texto.startswith("[") else [v.strip() for v in texto.split(",") if v.strip()]
        if not isinstance(valor, list):
            raise ValueError(f"{que}: debe ser una lista.")
        desconocidos = sorted({str(v) for v in valor} - set(validos))
        if desconocidos:
            raise ValueError(
                f"{que}: valores desconocidos ({', '.join(desconocidos)}). Admitidos: {', '.join(validos)}."
            )
        return sorted({str(v) for v in valor})

    return validar


def _booleano(valor: Any) -> bool:
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, str) and valor.strip().lower() in {"true", "sí", "si"}:
        return True
    if isinstance(valor, str) and valor.strip().lower() in {"false", "no"}:
        return False
    raise ValueError("Debe ser sí o no.")


HECHOS: tuple[HechoModulo, ...] = (
    HechoModulo(
        "ayudas_adultos_hogar",
        "number",
        "Adultos que conviven en el hogar",
        "Personas mayores de edad que viven en el domicilio y son familia (cónyuge o pareja, hijos, padres, "
        "hermanos, abuelos…), incluido tú.",
        _entero_entre(1, 20, "Adultos del hogar"),
    ),
    HechoModulo(
        "ayudas_menores_hogar",
        "number",
        "Menores que conviven en el hogar",
        "Menores de 18 años que viven en el domicilio y son familia.",
        _entero_entre(0, 20, "Menores del hogar"),
    ),
    HechoModulo(
        "ayudas_ingresos_hogar",
        "number",
        "Ingresos del hogar del año anterior",
        "Lo que ingresaron el año pasado todos los que conviven (salarios, pensiones, prestaciones, rentas), "
        "restando el IRPF y las cotizaciones sociales. Es la cifra del ingreso mínimo vital y de las prestaciones "
        "familiares.",
        _importe("Ingresos del hogar"),
        sensible=True,
    ),
    HechoModulo(
        "ayudas_base_imponible_hogar",
        "number",
        "Base imponible del hogar (última renta)",
        "Suma de la base imponible general y del ahorro de la última declaración de la renta de todos los que "
        "conviven. Es la cifra del bono social.",
        _importe("Base imponible del hogar"),
        sensible=True,
    ),
    HechoModulo(
        "ayudas_patrimonio_neto_hogar",
        "number",
        "Patrimonio neto del hogar sin la vivienda habitual",
        "Inmuebles distintos de la vivienda habitual, cuentas, depósitos, fondos y acciones de todos los que "
        "conviven, menos las deudas asociadas.",
        _importe("Patrimonio neto del hogar"),
        sensible=True,
    ),
    HechoModulo(
        "ayudas_contrato_pvpc",
        "boolean",
        "Contrato de luz en el PVPC",
        "La luz de la vivienda habitual está contratada a tu nombre en el precio regulado (PVPC) con una "
        "comercializadora de referencia.",
        _booleano,
    ),
    HechoModulo(
        "ayudas_ya_solicitadas",
        "json",
        "Ayudas que ya cobras o has pedido",
        "Lista de identificadores: " + ", ".join(f"{k} ({v})" for k, v in PRESTACIONES.items()) + ".",
        _lista_de(PRESTACIONES, "Ayudas ya solicitadas"),
        sensible=True,
    ),
    HechoModulo(
        "ayudas_pensionista",
        "boolean",
        "Eres pensionista",
        "Cobras una pensión de jubilación, incapacidad o viudedad de la Seguridad Social.",
        _booleano,
        sensible=True,
    ),
    HechoModulo(
        "ayudas_pensionistas_cuantia_minima",
        "boolean",
        "Pensionistas con la cuantía mínima",
        "Todos los que tienen ingresos en el hogar son pensionistas de jubilación o incapacidad permanente con la "
        "cuantía mínima (vía del bono social).",
        _booleano,
        sensible=True,
    ),
    HechoModulo(
        "ayudas_dependencia_grado_2_3",
        "boolean",
        "Dependencia de grado II o III en el hogar",
        "Alguien del hogar tiene reconocida una situación de dependencia de grado II o III.",
        _booleano,
        sensible=True,
    ),
    HechoModulo(
        "ayudas_electrodependencia",
        "boolean",
        "Electrodependencia en el hogar",
        "Alguien del hogar necesita equipos médicos eléctricos y tiene el certificado de electrodependencia.",
        _booleano,
        sensible=True,
    ),
    HechoModulo(
        "ayudas_intereses",
        "json",
        "Convocatorias que te interesan",
        "Temas de convocatorias del BOE de los que quieres avisos aunque el perfil no baste para comprobarlos: "
        + ", ".join(TEMAS)
        + ".",
        _lista_de(TEMAS, "Intereses"),
    ),
)


# --- Formatting ------------------------------------------------------------------


def _texto_evaluacion(e: Evaluacion, con_citas: bool = True) -> str:
    partes = [f"### {e.nombre}: {ETIQUETAS_ESTADO[e.estado]}", e.motivo]
    if e.importe_mensual is not None:
        partes.append(
            f"**Importe:** {eur(e.importe_mensual)} al mes ({eur(e.importe_anual or e.importe_mensual * 12)} al año)."
        )
    elif e.importe_anual is not None:
        partes.append(f"**Importe:** {eur(e.importe_anual)}.")
    if e.importe_texto:
        partes.append(f"**Cuantía:** {e.importe_texto}")
    if e.lineas:
        partes.append(
            "**Cálculo:**\n"
            + "\n".join(
                f"- {li.concepto}: {eur(li.importe) if li.importe is not None else '—'}"
                + (f" ({li.referencia})" if li.referencia else "")
                for li in e.lineas
            )
        )
    if e.plazo:
        partes.append(f"**Plazo:** {e.plazo}")
    if e.como_solicitar and e.estado in ("cumple", "probable", "proximamente", "faltan_datos"):
        partes.append(f"**Cómo:** {e.como_solicitar}")
    if e.requisitos_comprobados:
        partes.append("**Comprobado:**\n" + "\n".join(f"- {r}" for r in e.requisitos_comprobados))
    if e.requisitos_no_comprobados:
        partes.append(
            "**No comprobado (confírmalo antes de pedirla):**\n"
            + "\n".join(f"- {r}" for r in e.requisitos_no_comprobados)
        )
    if e.faltan:
        partes.append("**Datos que faltan:** " + "; ".join(e.faltan))
    if e.avisos:
        partes.append("**Avisos:** " + " ".join(e.avisos))
    if con_citas and e.citas:
        partes.append(
            "**Texto de la norma:**\n" + "\n".join(f"- {c.articulo} ({c.boe_id}): «{c.cita}»" for c in e.citas[:4])
        )
    if e.fuentes:
        partes.append("**Fuentes:** " + "; ".join(e.fuentes))
    return "\n".join(partes)


def _remisiones(p: PrestacionesParams, perfil: Mapping[str, Any], h: Hogar) -> str:
    lineas = []
    pequenos = (
        any(hijo.edad is not None and hijo.edad < 3 for hijo in h.hijos)
        or (entero(perfil.get("num_descendientes_menores_3")) or 0) > 0
    )
    if pequenos:
        lineas.append(
            "- Deducción por maternidad (art. 81 LIRPF), que puede cobrarse por anticipado: «"
            + p.remisiones_fiscales.evidencia[0].cita
            + "»"
        )
    if h.familia_numerosa or h.descendientes_discapacidad or h.discapacidad_33:
        lineas.append(
            "- Deducciones por familia numerosa o personas con discapacidad a cargo (art. 81 bis LIRPF): «"
            + p.remisiones_fiscales.evidencia[1].cita
            + "»"
        )
    if not lineas:
        return ""
    return "## En el módulo fiscal\nNo se calculan aquí; pídeselo al asesor fiscal:\n" + "\n".join(lineas)


def _evaluar(perfil: Mapping[str, Any], hoy: date) -> tuple[PrestacionesParams, Hogar, list[Evaluacion]]:
    p = load_prestaciones(ejercicio_para(hoy))
    h = hogar_del_perfil(perfil, hoy)
    grado = entero(perfil.get("grado_discapacidad"))
    conyuge = entero(perfil.get("grado_discapacidad_conyuge"))
    if conyuge is not None and (grado is None or conyuge > grado):
        grado = conyuge
    return p, h, evaluar_todas(p, h, grado)


# --- Tools -----------------------------------------------------------------------

SPEC_COMPROBAR = ToolSpec(
    name="ayudas_comprobar",
    description=(
        "Comprueba con el perfil del hogar las prestaciones públicas estatales estables: ingreso mínimo vital, "
        "complemento de ayuda para la infancia, asignación por hijo con discapacidad, prestación por nacimiento "
        "(familia numerosa, monoparental o progenitor con discapacidad), bono social eléctrico y térmico y título "
        "de familia monoparental. Devuelve para cada una si corresponde, el importe cuando la ley lo fija, el "
        "cálculo, el plazo, cómo pedirla, el texto literal de la norma y lo que no se ha podido comprobar."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "prestacion": {
                "type": "string",
                "enum": list(PRESTACIONES),
                "description": "Solo esta prestación (opcional).",
            },
            "incluir_descartadas": {
                "type": "boolean",
                "description": "Incluir también las que no corresponden o ya se cobran (por defecto, no).",
            },
        },
        "additionalProperties": False,
    },
)

SPEC_CONVOCATORIAS = ToolSpec(
    name="ayudas_convocatorias",
    description=(
        "Lista las convocatorias de ayudas publicadas en el BOE (secciones III y V-B) que siguen abiertas y "
        "pueden encajar con el hogar, con su plazo, a quién se dirigen (cita literal) y el enlace al BOE. "
        "No incluye boletines autonómicos."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "incluir_posibles": {
                "type": "boolean",
                "description": "Incluir también las dirigidas a personas que el perfil no permite confirmar.",
            },
            "incluir_cerradas": {"type": "boolean", "description": "Incluir las de plazo vencido."},
        },
        "additionalProperties": False,
    },
)


def _error(mensaje: str) -> ResultadoHerramienta:
    return ResultadoHerramienta(contenido=mensaje, resumen=mensaje[:120], es_error=True)


def ejecutar_comprobar(args: Mapping[str, Any], contexto: ContextoModulo) -> ResultadoHerramienta:
    try:
        solo = args.get("prestacion")
        if solo is not None and solo not in PRESTACIONES:
            return _error(f"Prestación desconocida «{solo}». Opciones: {', '.join(PRESTACIONES)}.")
        incluir = bool(args.get("incluir_descartadas"))
        p, h, evaluaciones = _evaluar(contexto.perfil, contexto.hoy)
        if solo:
            evaluaciones = [e for e in evaluaciones if e.id == solo]
        visibles = [e for e in evaluaciones if incluir or solo or e.estado not in ("no_cumple", "ya_solicitada")]
        visibles.sort(key=lambda e: ORDEN_ESTADOS[e.estado])
        propuestas = [e for e in evaluaciones if e.proponible]
        partes = [f"# Ayudas públicas estatales ({p.ejercicio})"]
        if propuestas:
            partes.append(
                "Encontradas: "
                + "; ".join(f"{e.nombre} ({ETIQUETAS_ESTADO[e.estado].lower()})" for e in propuestas)
                + "."
            )
        else:
            partes.append(
                "Con los datos del perfil no se ha encontrado ninguna prestación que te corresponda y no cobres ya."
            )
        partes.extend(_texto_evaluacion(e) for e in visibles)
        descartadas = [e for e in evaluaciones if e not in visibles]
        if descartadas:
            partes.append("Descartadas: " + "; ".join(f"{e.nombre} ({e.motivo})" for e in descartadas))
        remision = _remisiones(p, contexto.perfil, h)
        if remision:
            partes.append(remision)
        if h.notas:
            partes.append("## Sobre los datos usados\n" + "\n".join(f"- {n}" for n in h.notas))
        partes.append("## No se comprueba\n" + "\n".join(f"- {n}" for n in p.no_modelado))
        citas = tuple(dict.fromkeys(c for e in visibles for c in e.fuentes))
        resumen = (
            f"{len(propuestas)} ayuda(s) posible(s): " + ", ".join(e.id for e in propuestas)
            if propuestas
            else "Ninguna ayuda estatal nueva con los datos del perfil"
        )
        return ResultadoHerramienta(contenido="\n\n".join(partes), resumen=resumen, citas=citas)
    except Exception as exc:  # the agent gets a readable error, never an exception
        return _error(f"No se han podido comprobar las ayudas: {exc}")


def _texto_convocatoria(conv: Convocatoria, enc: Encaje, hoy: date) -> str:
    partes = [
        f"### {conv.titulo}",
        f"Publicada en el BOE el {fecha(conv.fecha_publicacion)} ({conv.boe_id}): {conv.url}",
    ]
    if conv.bdns:
        partes.append(f"Base de Datos Nacional de Subvenciones: {conv.bdns}")
    abiertos = conv.plazos_abiertos(hoy)
    if abiertos:
        partes.append(
            "**Plazo:**\n"
            + "\n".join(
                f"- Hasta el {fecha(pl.fin)}"
                + (" (estimado sin contar festivos: puede ser algo más tarde)" if pl.estimado else "")
                + (f". «{pl.contexto}» … «{pl.cita}»" if pl.contexto else f". «{pl.cita}»")
                for pl in abiertos
            )
        )
    elif conv.plazos:
        partes.append(f"**Plazo vencido** el {fecha(conv.plazos[-1].fin)}.")
    else:
        partes.append("**Plazo:** no se ha podido leer en el texto; consúltalo en el BOE.")
    if conv.beneficiarios:
        partes.append(f"**A quién se dirige (texto del BOE):** «{conv.beneficiarios[:600]}»")
    if conv.cuantia:
        partes.append(f"**Cuantía (texto del BOE):** «{conv.cuantia[:400]}»")
    partes.append(f"**Por qué encaja:** {'; '.join(enc.motivos)}.")
    if enc.requisitos_no_comprobados:
        partes.append("**No comprobado:**\n" + "\n".join(f"- {r}" for r in enc.requisitos_no_comprobados))
    return "\n".join(partes)


def ejecutar_convocatorias(args: Mapping[str, Any], contexto: ContextoModulo) -> ResultadoHerramienta:
    try:
        almacen = almacen_mod.cargar()
        if almacen.revisado_hasta is None:
            return ResultadoHerramienta(
                contenido="Todavía no se ha revisado el BOE en busca de convocatorias (la revisión diaria no se ha "
                "ejecutado en este servidor). No puedo listar convocatorias abiertas.",
                resumen="Sin revisión del BOE todavía",
            )
        incluir_posibles = bool(args.get("incluir_posibles"))
        incluir_cerradas = bool(args.get("incluir_cerradas"))
        h = hogar_del_perfil(contexto.perfil, contexto.hoy)
        filas: list[tuple[Convocatoria, Encaje]] = []
        for conv in almacen.convocatorias.values():
            if not incluir_cerradas and not conv.abierta(contexto.hoy):
                continue
            enc = encaje(conv, h, contexto.perfil)
            if enc.nivel == "encaja" or (incluir_posibles and enc.nivel == "posible"):
                filas.append((conv, enc))
        filas.sort(key=lambda f: (f[1].nivel != "encaja", f[0].proximo_fin(contexto.hoy) or date.max, f[0].boe_id))
        cabecera = (
            f"# Convocatorias del BOE (revisado hasta el {fecha(almacen.revisado_hasta)})\n"
            "Solo se leen convocatorias estatales del BOE; las de comunidades autónomas y ayuntamientos se publican "
            "en sus boletines y no se revisan."
        )
        if not filas:
            texto = "Ninguna convocatoria abierta encaja con tu perfil."
            if not incluir_posibles:
                texto += (
                    " Puedes pedir también las «posibles» (dirigidas a personas, sin poder comprobar los requisitos)."
                )
            return ResultadoHerramienta(contenido=f"{cabecera}\n\n{texto}", resumen="Ninguna convocatoria encaja")
        bloques = [cabecera]
        for nivel, titulo in (
            ("encaja", "## Encajan con tu perfil"),
            ("posible", "## Posibles (sin poder comprobarlo)"),
        ):
            grupo = [_texto_convocatoria(c, e, contexto.hoy) for c, e in filas if e.nivel == nivel]
            if grupo:
                bloques.append(titulo)
                bloques.extend(grupo)
        encajan = sum(1 for _, e in filas if e.nivel == "encaja")
        return ResultadoHerramienta(
            contenido="\n\n".join(bloques),
            resumen=f"{encajan} convocatoria(s) encajan, {len(filas) - encajan} posible(s)",
            citas=tuple(dict.fromkeys(c.url for c, _ in filas)),
        )
    except Exception as exc:
        return _error(f"No se han podido consultar las convocatorias: {exc}")


HERRAMIENTAS: tuple[HerramientaModulo, ...] = (
    HerramientaModulo(spec=SPEC_COMPROBAR, ejecutar=ejecutar_comprobar),
    HerramientaModulo(spec=SPEC_CONVOCATORIAS, ejecutar=ejecutar_convocatorias),
)


# --- Notices ---------------------------------------------------------------------


def avisos_prestaciones(perfil: Mapping[str, Any], hoy: date) -> list[AvisoModulo]:
    p, _, evaluaciones = _evaluar(perfil, hoy)
    out: list[AvisoModulo] = []
    for e in evaluaciones:
        if e.estado not in ("cumple", "probable", "proximamente"):
            continue
        if e.estado == "proximamente" and e.vence and (e.vence - hoy).days > DIAS_ANTELACION_PROXIMAMENTE:
            continue
        importe = e.importe_anual if e.importe_anual is not None else None
        cuerpo = [e.motivo]
        if e.importe_mensual is not None:
            cuerpo.append(f"Importe estimado: {eur(e.importe_mensual)} al mes.")
        elif e.importe_anual is not None:
            cuerpo.append(f"Importe: {eur(e.importe_anual)}.")
        if e.importe_texto:
            cuerpo.append(e.importe_texto)
        if e.plazo:
            cuerpo.append(f"Plazo: {e.plazo}")
        if e.requisitos_no_comprobados:
            cuerpo.append("Antes de pedirla confirma: " + " ".join(e.requisitos_no_comprobados))
        cerca = e.vence is not None and 0 <= (e.vence - hoy).days <= DIAS_URGENTE
        anio = e.vence.year if e.estado == "proximamente" and e.vence else p.ejercicio
        out.append(
            AvisoModulo(
                clave=f"{CLAVE}:prestacion:{e.id}:{anio}",
                titulo=(
                    f"Puede corresponderte: {e.nombre}" if e.estado != "proximamente" else f"Próximamente: {e.nombre}"
                ),
                cuerpo="\n\n".join(cuerpo),
                gravedad="aviso" if cerca or e.estado == "cumple" else "info",
                acciones=(e.como_solicitar,) if e.como_solicitar else (),
                fuentes=tuple(e.fuentes),
                importe=importe,
                vence=e.vence,
            )
        )
    return out


def avisos_convocatorias(perfil: Mapping[str, Any], hoy: date) -> list[AvisoModulo]:
    almacen = almacen_mod.cargar()
    h = hogar_del_perfil(perfil, hoy)
    out: list[AvisoModulo] = []
    for conv in sorted(almacen.convocatorias.values(), key=lambda c: c.boe_id):
        if not conv.abierta(hoy):
            continue
        enc = encaje(conv, h, perfil)
        if enc.nivel != "encaja":
            continue
        fin = conv.proximo_fin(hoy)
        cuerpo = [f"Publicada en el BOE el {fecha(conv.fecha_publicacion)}.", f"Por qué: {'; '.join(enc.motivos)}."]
        if fin:
            cuerpo.append(
                f"Plazo hasta el {fecha(fin)}" + (" (estimado)." if conv.plazos_abiertos(hoy)[0].estimado else ".")
            )
        if conv.beneficiarios:
            cuerpo.append(f"A quién se dirige: «{conv.beneficiarios[:400]}»")
        cuerpo.append("Comprueba todos los requisitos en el texto de la convocatoria antes de solicitarla.")
        out.append(
            AvisoModulo(
                clave=f"{CLAVE}:convocatoria:{conv.boe_id}",
                titulo=f"Convocatoria abierta: {conv.titulo[:110].rstrip()}{'…' if len(conv.titulo) > 110 else ''}",
                cuerpo="\n\n".join(cuerpo),
                gravedad="aviso" if fin is not None and (fin - hoy).days <= DIAS_URGENTE else "info",
                acciones=(f"Lee la convocatoria en el BOE: {conv.url}",),
                fuentes=(f"{conv.titulo} ({conv.boe_id})", conv.url),
                vence=fin,
            )
        )
    return out


def revision_automatica() -> bool:
    return os.environ.get("AYUDAS_BOE_AUTOMATICO", "").strip() in {"1", "true"}


class ModuloAyudas(ModuloBase):
    def avisos(self, contexto: ContextoModulo) -> list[AvisoModulo]:
        if revision_automatica():
            actualizar_convocatorias(contexto.hoy)  # once a day; never raises
        avisos = avisos_prestaciones(contexto.perfil, contexto.hoy) + avisos_convocatorias(
            contexto.perfil, contexto.hoy
        )
        return sorted(avisos, key=lambda a: a.clave)


def _fuentes() -> tuple[FuenteModulo, ...]:
    p = load_prestaciones()
    leyes = tuple(
        FuenteModulo(identificador=fp.boe_id, descripcion=fp.norma, url=fp.url) for fp in p.fuentes_principales.values()
    )
    return leyes + (
        FuenteModulo(
            identificador="boe_sumario",
            descripcion="Sumario diario del BOE (API de datos abiertos), secciones III y V-B",
            url="https://www.boe.es/datosabiertos/api/boe/sumario/AAAAMMDD",
        ),
        FuenteModulo(
            identificador="bdns",
            descripcion="Base de Datos Nacional de Subvenciones (no accesible desde el servidor de desarrollo)",
            url="https://www.infosubvenciones.es/bdnstrans/GE/es/convocatorias",
        ),
    )


INSTRUCCIONES = """Eres el especialista en ayudas públicas del hogar. Tu trabajo es encontrar ayudas a las que el \
hogar tiene derecho y no ha pedido, con su plazo y sus requisitos citados del BOE.

- Usa `ayudas_comprobar` para las prestaciones estatales estables y `ayudas_convocatorias` para las convocatorias \
abiertas del BOE. No calcules nada por tu cuenta ni cites cifras que no salgan de las herramientas.
- Distingue siempre «te corresponde» (datos declarados), «probablemente» (datos estimados) y «faltan datos». Repite \
los requisitos que no se han podido comprobar: nunca digas que alguien tiene derecho sin esa advertencia.
- Si faltan datos, pide exactamente los hechos que indica la herramienta (por ejemplo «ayudas_ingresos_hogar») y \
guárdalos; son datos sensibles: explica para qué se usan.
- Las deducciones del IRPF (maternidad, familia numerosa, discapacidad a cargo) son del módulo fiscal: remite a él.
- Solo se revisa el BOE: las ayudas autonómicas y locales no se ven; dilo si preguntan por ellas.
- Solicitar una ayuda lo hace la persona en la sede de cada organismo: explica cómo, no lo hagas tú."""

MODULO = ModuloAyudas(
    clave=CLAVE,
    nombre="Ayudas públicas",
    descripcion=(
        "Ayudas y prestaciones públicas para el hogar: ingreso mínimo vital, complemento de infancia, prestaciones "
        "por hijo con discapacidad o por nacimiento, bono social eléctrico y térmico, título de familia monoparental "
        "y convocatorias de ayudas, becas o plazas publicadas en el BOE que encajan con el hogar, con plazo y requisitos."
    ),
    instrucciones=INSTRUCCIONES,
    hechos=HECHOS,
    herramientas=HERRAMIENTAS,
    fuentes=_fuentes(),
)
