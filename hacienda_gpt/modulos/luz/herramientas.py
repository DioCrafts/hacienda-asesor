"""Tools of the ``luz`` module: deterministic, Spanish output with figures, sources and caveats."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import json
from typing import Any

from pydantic import ValidationError

from hacienda_gpt.agent.providers.base import ToolSpec
from hacienda_gpt.modulos.contrato import ContextoModulo, HerramientaModulo, ResultadoHerramienta
from hacienda_gpt.modulos.luz import esios
from hacienda_gpt.modulos.luz.analisis import (
    comparar,
    dia_valle_texto,
    horas_baratas,
    horas_por_periodo,
    potencia_optima,
)
from hacienda_gpt.modulos.luz.consumo import (
    ConsumoGuardado,
    FormatoNoReconocidoError,
    Lectura,
    Maximetro,
    cargar_consumo,
    compactar,
    importar_csv,
)
from hacienda_gpt.modulos.luz.datos import PERIODOS, DatoNoDisponibleError, cargar_regulados
from hacienda_gpt.modulos.luz.factura import CERO, Contrato, FacturaError, calcular_factura, euros, fmt
from hacienda_gpt.modulos.luz.ofertas import Oferta, cargar_oferta, cargar_ofertas
from hacienda_gpt.modulos.luz.periodos import ZONAS, Zona, describir_periodos

HECHO_CUPS = "luz_cups"
HECHO_P1 = "luz_potencia_p1_kw"
HECHO_P2 = "luz_potencia_p2_kw"
HECHO_ZONA = "luz_zona"
HECHO_TARIFA = "luz_tarifa_actual"
HECHO_OFERTAS = "luz_ofertas"
HECHO_CONSUMO = "luz_consumo"

# Hook for tests and for the core: how hourly PVPC prices are fetched (ESIOS by default).
Fetch = esios.Fetch
_FETCH: dict[str, esios.Fetch | None] = {"fetch": None}


class _EntradaError(ValueError):
    pass


def _error(mensaje: str) -> ResultadoHerramienta:
    return ResultadoHerramienta(contenido=mensaje, resumen=mensaje[:120], es_error=True)


def _fecha(args: Mapping[str, Any], clave: str, defecto: date | None = None) -> date | None:
    valor = args.get(clave)
    if valor in (None, ""):
        return defecto
    try:
        return date.fromisoformat(str(valor))
    except ValueError as exc:
        raise _EntradaError(f"«{clave}» debe ser una fecha AAAA-MM-DD (recibido: {valor}).") from exc


def _dec(valor: Any, que: str) -> Decimal:
    try:
        return Decimal(str(valor).replace(",", "."))
    except InvalidOperation as exc:
        raise _EntradaError(f"«{que}» debe ser un número (recibido: {valor}).") from exc


def _zona(ctx: ContextoModulo) -> Zona:
    zona = ctx.perfil.get(HECHO_ZONA)
    if zona in ZONAS:
        return zona  # type: ignore[no-any-return]
    ccaa = str(ctx.perfil.get("comunidad_autonoma") or "").lower()
    for clave, valor in (("canari", "canarias"), ("balear", "baleares"), ("ceuta", "ceuta"), ("melilla", "melilla")):
        if clave in ccaa:
            return valor  # type: ignore[return-value]
    return "peninsula"


def _contrato(args: Mapping[str, Any], ctx: ContextoModulo) -> Contrato:
    p1 = args.get("potencia_p1_kw", ctx.perfil.get(HECHO_P1))
    p2 = args.get("potencia_p2_kw", ctx.perfil.get(HECHO_P2, p1))
    if p1 in (None, ""):
        raise _EntradaError("Falta la potencia contratada (luz_potencia_p1_kw): viene en la factura, en kW.")
    return Contrato(_dec(p1, "potencia_p1_kw"), _dec(p2 if p2 not in (None, "") else p1, "potencia_p2_kw"), _zona(ctx))


def _oferta(valor: Any, que: str) -> Oferta:
    if isinstance(valor, str):
        try:
            valor = json.loads(valor)
        except json.JSONDecodeError as exc:
            raise _EntradaError(f"«{que}» no es un JSON válido.") from exc
    try:
        oferta = cargar_oferta(valor)
    except ValidationError as exc:
        err = exc.errors()[0]
        raise _EntradaError(f"{que} no válida: {'.'.join(str(x) for x in err['loc'])}: {err['msg']}") from exc
    if oferta is None:
        raise _EntradaError(f"Falta {que}.")
    return oferta


def _consumo(ctx: ContextoModulo) -> ConsumoGuardado | None:
    try:
        return cargar_consumo(ctx.perfil.get(HECHO_CONSUMO))
    except ValidationError as exc:
        raise _EntradaError(
            f"El consumo guardado no es válido ({exc.errors()[0]['msg']}): vuelve a importarlo."
        ) from exc


def _precios_pegados(valor: Any, zona: Zona, avisos: list[str]) -> dict[tuple[date, int], Decimal] | None:
    if valor in (None, ""):
        return None
    try:
        return esios.interpretar(valor if isinstance(valor, str | bytes) else json.dumps(valor), zona, avisos)
    except esios.PreciosNoDisponiblesError as exc:
        raise _EntradaError(str(exc)) from exc


def _proveedor(zona: Zona, avisos: list[str]) -> Callable[[date, date], dict[tuple[date, int], Decimal]]:
    def obtener(desde: date, hasta: date) -> dict[tuple[date, int], Decimal]:
        return esios.obtener_precios(desde, hasta, zona, fetch=_FETCH["fetch"], avisos=avisos)

    return obtener


def _fuentes_texto(citas: tuple[str, ...]) -> str:
    return "Fuentes: " + "; ".join(citas)


# --- luz_calcular_factura --------------------------------------------------------------------------


def calcular(args: Mapping[str, Any], ctx: ContextoModulo) -> ResultadoHerramienta:
    try:
        desde = _fecha(args, "fecha_inicio")
        hasta = _fecha(args, "fecha_fin")
        if desde is None or hasta is None:
            return _error(
                "Indica «fecha_inicio» y «fecha_fin» del periodo de facturación, tal como salen en la factura."
            )
        contrato = _contrato(args, ctx)
        oferta = _oferta(args.get("oferta") or ctx.perfil.get(HECHO_TARIFA), "la tarifa (luz_tarifa_actual)")
        tipos = str(args.get("tipos_reducidos") or "auto")
        if tipos not in ("auto", "si", "no"):
            raise _EntradaError("«tipos_reducidos» debe ser auto, si o no.")
        avisos: list[str] = []
        lecturas: list[Lectura] | None = None
        dias_kwh = None
        totales: dict[str, Decimal] | None = None
        origen = ""
        if args.get("csv"):
            lecturas = importar_csv(str(args["csv"]), contrato.zona).lecturas
            origen = "CSV horario aportado"
        elif args.get("consumo_kwh"):
            c = args["consumo_kwh"]
            if not isinstance(c, Mapping):
                raise _EntradaError("«consumo_kwh» debe ser un objeto {p1, p2, p3} en kWh.")
            totales = {p: _dec(c.get(p, 0), f"consumo_kwh.{p}") for p in PERIODOS}
            origen = "kWh por periodo indicados"
        else:
            consumo = _consumo(ctx)
            if consumo is None:
                raise _EntradaError(
                    "No hay consumo: importa el CSV horario (luz_importar_consumo) o indica los kWh de P1, P2 y P3."
                )
            lecturas = consumo.lecturas_horarias(desde, hasta) if oferta.tipo == "pvpc" else None
            manual = consumo.manual_para(desde, hasta)
            if lecturas is not None:
                origen = "consumo horario guardado"
            elif consumo.totales(desde, hasta) is not None:
                dias_kwh = consumo.dias
                origen = "consumo diario por periodos guardado"
            elif manual is not None:
                totales = {"p1": manual.p1, "p2": manual.p2, "p3": manual.p3}
                origen = "totales de la factura guardados"
            else:
                rango = consumo.rango()
                cubre = f" (hay datos del {rango[0]:%d-%m-%Y} al {rango[1]:%d-%m-%Y})" if rango else ""
                raise _EntradaError(f"El consumo guardado no cubre ese periodo{cubre}: indica los kWh por periodo.")
        precios = None
        if oferta.tipo == "pvpc":
            precios = _precios_pegados(args.get("precios_pvpc"), contrato.zona, avisos)
            if precios is None:
                precios = esios.obtener_precios(desde, hasta, contrato.zona, fetch=_FETCH["fetch"], avisos=avisos)
        factura = calcular_factura(
            oferta,
            contrato,
            desde,
            hasta,
            lecturas=lecturas,
            dias_kwh=dias_kwh,
            totales=totales,
            precios_pvpc=precios,
            tipos_reducidos=tipos,  # type: ignore[arg-type]
        )
    except (_EntradaError, FacturaError, DatoNoDisponibleError, FormatoNoReconocidoError) as exc:
        return _error(str(exc))
    except esios.PreciosNoDisponiblesError as exc:
        return _error(str(exc))
    partes = [factura.texto(), "", f"Consumo usado: {origen}."]
    resumen = f"Factura {factura.desde:%d-%m-%Y}–{factura.hasta:%d-%m-%Y}: {fmt(factura.total)} €"
    real = args.get("total_factura_real")
    if real not in (None, ""):
        try:
            real_d = _dec(real, "total_factura_real")
        except _EntradaError as exc:
            return _error(str(exc))
        diferencia = factura.total - real_d
        if diferencia == 0:
            partes.append(f"Coincide al céntimo con el total de tu factura ({fmt(real_d)} €).")
            resumen += " (coincide con la real)"
        else:
            partes.append(
                f"Tu factura dice {fmt(real_d)} €: diferencia de {fmt(diferencia)} €. Revisa línea a línea: días del "
                "periodo, kWh por periodo, precios exactos (con todos sus decimales), si cobran el bono social y "
                "cuánto, el alquiler del contador por día y si hubo tipos reducidos de IVA o del impuesto eléctrico."
            )
            resumen += f" (real {fmt(real_d)} €, dif. {fmt(diferencia)} €)"
    partes.extend(f"Aviso: {a}" for a in avisos)
    partes.append(_fuentes_texto(factura.citas))
    return ResultadoHerramienta(contenido="\n".join(partes), resumen=resumen, citas=factura.citas)


# --- luz_comparar_tarifas --------------------------------------------------------------------------


def comparar_tarifas(args: Mapping[str, Any], ctx: ContextoModulo) -> ResultadoHerramienta:
    try:
        contrato = _contrato(args, ctx)
        consumo = _consumo(ctx)
        if consumo is None:
            raise _EntradaError("No hay consumo guardado: importa el CSV de Datadis (luz_importar_consumo).")
        actual = cargar_oferta(ctx.perfil.get(HECHO_TARIFA)) if ctx.perfil.get(HECHO_TARIFA) else None
        ofertas = cargar_ofertas(ctx.perfil.get(HECHO_OFERTAS))
        extra = args.get("ofertas")
        if extra:
            if not isinstance(extra, list):
                raise _EntradaError("«ofertas» debe ser una lista de tarifas.")
            ofertas = ofertas + [_oferta(o, "oferta") for o in extra]
        if args.get("incluir_pvpc", True) and not any(o.tipo == "pvpc" for o in ofertas):
            ofertas.append(Oferta(nombre="PVPC (tarifa regulada)", tipo="pvpc"))
        if actual is None and not ofertas:
            raise _EntradaError("No hay tarifas que comparar: guarda tu tarifa actual y las ofertas.")
        avisos: list[str] = []
        dias_max = int(args.get("dias", 365))
        resultado = comparar(
            contrato,
            consumo,
            ofertas,
            actual,
            dias_max=max(28, min(dias_max, 400)),
            proveedor_precios=_proveedor(contrato.zona, avisos),
        )
    except (_EntradaError, FacturaError, DatoNoDisponibleError) as exc:
        return _error(str(exc))
    except ValidationError as exc:
        return _error(f"Tarifa guardada no válida: {exc.errors()[0]['msg']}")
    partes = [
        f"Coste con el mismo consumo del {resultado.desde:%d-%m-%Y} al {resultado.hasta:%d-%m-%Y} "
        f"({resultado.dias} días), impuestos incluidos, de más barata a más cara:"
    ]
    act = resultado.actual()
    for n, c in enumerate(resultado.costes, start=1):
        marca = " (tu tarifa actual)" if c.es_actual else ""
        dif = ""
        if act is not None and not c.es_actual:
            d = c.total - act.total
            dif = f"; {'ahorras' if d < 0 else 'pagarías'} {fmt(abs(d))} € respecto a la actual"
        partes.append(f"{n}. {c.oferta.nombre}{marca}: {fmt(c.total)} € (≈ {fmt(c.anual)} €/año){dif}.")
        if c.oferta.permanencia_hasta:
            partes.append(
                f"   Permanencia hasta {c.oferta.permanencia_hasta:%d-%m-%Y}"
                + (f", penalización {fmt(c.oferta.penalizacion_eur)} €" if c.oferta.penalizacion_eur else "")
                + "."
            )
    mejor = resultado.mejor()
    if mejor is not None and act is not None:
        if mejor.es_actual:
            partes.append("Tu tarifa actual es la más barata de las comparadas.")
        else:
            ahorro = euros((act.total - mejor.total) * 365 / resultado.dias)
            partes.append(f"La más barata es «{mejor.oferta.nombre}»: unos {fmt(ahorro)} € al año menos que la actual.")
    partes.extend(f"No comparada: {d}" for d in resultado.descartadas)
    partes.extend(f"Aviso: {a}" for a in resultado.avisos + avisos)
    datos = cargar_regulados()
    citas = datos.citas(
        "BOE-A-2024-26218",
        "BOE-A-2025-26348",
        "BOE-A-2024-27289",
        "BOE-A-2025-26705",
        "BOE-A-1992-28741",
        "BOE-A-1992-28740",
    )
    partes.append(_fuentes_texto(citas))
    resumen = (
        f"Más barata: {mejor.oferta.nombre} ({fmt(mejor.total)} € en {resultado.dias} días)" if mejor else "Sin datos"
    )
    return ResultadoHerramienta(contenido="\n".join(partes), resumen=resumen, citas=citas)


# --- luz_importar_consumo --------------------------------------------------------------------------


def importar(args: Mapping[str, Any], ctx: ContextoModulo) -> ResultadoHerramienta:
    try:
        zona = _zona(ctx)
        previo = _consumo(ctx)
        nuevo: ConsumoGuardado
        resumen_lectura = ""
        avisos: list[str] = []
        cups = None
        if args.get("csv"):
            imp = importar_csv(str(args["csv"]), zona)
            avisos = imp.avisos
            cups = imp.cups
            nuevo = compactar(imp.lecturas, zona, fuente=imp.formato, importado_el=ctx.hoy, previo=previo)
            dias = sorted({x.fecha for x in imp.lecturas})
            total = sum((x.kwh for x in imp.lecturas), CERO)
            resumen_lectura = (
                f"Leídas {fmt(Decimal(len(imp.lecturas)), 0)} horas del {dias[0]:%d-%m-%Y} al {dias[-1]:%d-%m-%Y} "
                f"({fmt(total, 3)} kWh, formato {imp.formato})."
            )
        else:
            nuevo = previo or ConsumoGuardado(zona=zona)
        if args.get("totales"):
            datos = args["totales"]
            if not isinstance(datos, list):
                raise _EntradaError("«totales» debe ser una lista de {desde, hasta, p1, p2, p3}.")
            nuevo = ConsumoGuardado.model_validate(
                {**nuevo.a_json(), "manual": [m.model_dump(mode="json") for m in nuevo.manual] + list(datos)}
            )
            resumen_lectura += f" Guardados {len(datos)} periodo(s) con totales de factura."
        if args.get("maximetro"):
            datos = args["maximetro"]
            if not isinstance(datos, list):
                raise _EntradaError("«maximetro» debe ser una lista de {mes, p1_kw, p2_kw}.")
            nuevos = [Maximetro.model_validate(x) for x in datos]
            meses = {m.mes for m in nuevos}
            nuevo = ConsumoGuardado.model_validate(
                {
                    **nuevo.a_json(),
                    "maximetro": [m.model_dump(mode="json") for m in nuevo.maximetro if m.mes not in meses]
                    + [m.model_dump(mode="json") for m in nuevos],
                }
            )
            resumen_lectura += f" Guardadas potencias máximas de {len(nuevos)} mes(es)."
        if not resumen_lectura:
            raise _EntradaError("Pega el CSV horario («csv»), los totales de tus facturas («totales») o el maxímetro.")
    except (_EntradaError, FormatoNoReconocidoError, DatoNoDisponibleError) as exc:
        return _error(str(exc))
    except ValidationError as exc:
        err = exc.errors()[0]
        return _error(f"Datos no válidos: {'.'.join(str(x) for x in err['loc'])}: {err['msg']}")
    partes = [resumen_lectura.strip()]
    rango = nuevo.rango()
    if rango:
        partes.append(f"Consumo diario guardado del {rango[0]:%d-%m-%Y} al {rango[1]:%d-%m-%Y}.")
        for mes, r in list(sorted(nuevo.meses.items()))[-12:]:
            dias_mes = [d for d in nuevo.dias if f"{d:%Y-%m}" == mes]
            por_p = [sum((nuevo.dias[d][i] for d in dias_mes), CERO) for i in range(3)]
            partes.append(
                f"- {mes}: {fmt(r.kwh, 1)} kWh (P1 {fmt(por_p[0], 1)}, P2 {fmt(por_p[1], 1)}, P3 {fmt(por_p[2], 1)}; "
                f"máx. {fmt(r.max_kwh_hora, 3)} kWh en una hora)"
            )
    partes.append(
        "Se guarda un resumen (consumo diario por periodos de 400 días, horario de los últimos 62 días y perfil "
        "mensual), no la serie horaria completa."
    )
    guardar = args.get("guardar", True) is not False
    hechos: dict[str, Any] = {HECHO_CONSUMO: nuevo.a_json()}
    if cups and not ctx.perfil.get(HECHO_CUPS):
        hechos[HECHO_CUPS] = cups
    if guardar and ctx.registrar is not None:
        try:
            ctx.registrar(hechos)
        except ValueError as exc:
            return _error(f"No se ha podido guardar: {exc}")
        partes.append(
            "Guardado en tu expediente" + (" (incluido el CUPS del fichero)." if HECHO_CUPS in hechos else ".")
        )
    else:
        partes.append("No se ha guardado (no hay permiso de escritura en esta llamada).")
    partes.extend(f"Aviso: {a}" for a in avisos)
    return ResultadoHerramienta(contenido="\n".join(partes), resumen=partes[0][:120])


# --- luz_horas_baratas -----------------------------------------------------------------------------


def _horas_txt(horas: list[int]) -> str:
    if not horas:
        return "ninguna"
    tramos: list[list[int]] = []
    for h in sorted(horas):
        if tramos and tramos[-1][-1] + 1 == h:
            tramos[-1].append(h)
        else:
            tramos.append([h])
    return ", ".join(f"{t[0]}-{t[-1] + 1} h" for t in tramos)


def baratas(args: Mapping[str, Any], ctx: ContextoModulo) -> ResultadoHerramienta:
    try:
        dia = _fecha(args, "fecha", ctx.hoy + timedelta(days=1))
        assert dia is not None
        zona = _zona(ctx)
        n = int(args.get("horas", 6))
        bloque = int(args.get("bloque_horas", 3))
        tarifa = ctx.perfil.get(HECHO_TARIFA)
        oferta = cargar_oferta(tarifa) if tarifa else None
        avisos: list[str] = []
        periodos = horas_por_periodo(dia, zona)
        valle = dia_valle_texto(dia)
        partes = [f"Horas del {dia:%d-%m-%Y} ({describir_periodos(zona)})"]
        if valle:
            partes.append(f"Es {valle}: todas las horas son valle (P3) en los peajes y cargos.")
        else:
            partes.append(
                f"Valle (P3): {_horas_txt(periodos['p3'])}; llano (P2): {_horas_txt(periodos['p2'])}; "
                f"punta (P1): {_horas_txt(periodos['p1'])}."
            )
        if oferta is not None and oferta.tipo == "fijo" and oferta.energia_eur_kwh is not None:
            e = oferta.energia_eur_kwh
            if e.p1 == e.p2 == e.p3:
                partes.append(
                    f"Tu tarifa «{oferta.nombre}» tiene precio único ({fmt(e.p1, 6)} €/kWh): la hora no cambia "
                    "lo que pagas por la energía."
                )
            else:
                partes.append(
                    f"Con tu tarifa «{oferta.nombre}»: punta {fmt(e.p1, 6)}, llano {fmt(e.p2, 6)}, valle "
                    f"{fmt(e.p3, 6)} €/kWh. Lo más barato es concentrar consumo en valle."
                )
        precios = _precios_pegados(args.get("precios_pvpc"), zona, avisos)
        if precios is None and (oferta is None or oferta.tipo == "pvpc" or args.get("consultar_pvpc")):
            try:
                precios = esios.obtener_precios(dia, dia, zona, fetch=_FETCH["fetch"], avisos=avisos)
            except esios.PreciosNoDisponiblesError as exc:
                avisos.append(str(exc))
        resumen = f"Periodos del {dia:%d-%m-%Y}"
        if precios:
            r = horas_baratas(dia, precios, zona, max(1, min(n, 24)), max(1, min(bloque, 24)))
            partes.append("Precio PVPC hora a hora (€/kWh, incluye peajes y cargos, sin impuestos):")
            partes.append("; ".join(f"{h:02d} h {fmt(p, 5)}" for h, p in r.precios))
            partes.append(
                "Horas más baratas: " + ", ".join(f"{h:02d}-{h + 1:02d} h ({fmt(p, 5)})" for h, p in r.baratas)
            )
            partes.append("Horas más caras: " + ", ".join(f"{h:02d}-{h + 1:02d} h ({fmt(p, 5)})" for h, p in r.caras))
            if r.bloque_inicio is not None and r.bloque_media is not None:
                partes.append(
                    f"Mejor bloque de {bloque} horas seguidas: desde las {r.bloque_inicio:02d} h "
                    f"(media {fmt(r.bloque_media, 5)} €/kWh)."
                )
            resumen = f"Horas más baratas del {dia:%d-%m-%Y}: " + ", ".join(f"{h:02d} h" for h, _ in r.baratas)
            if oferta is not None and oferta.tipo == "fijo":
                partes.append("Estos precios son los del PVPC; tu tarifa es de precio fijo y no depende de ellos.")
    except (_EntradaError, FacturaError, DatoNoDisponibleError) as exc:
        return _error(str(exc))
    except (ValueError, ValidationError) as exc:
        return _error(f"Datos no válidos: {exc}")
    partes.extend(f"Aviso: {a}" for a in avisos)
    citas = cargar_regulados().citas("BOE-A-2020-1066")
    partes.append(_fuentes_texto(citas) + "; precios PVPC: REE, indicador 1001 de ESIOS.")
    return ResultadoHerramienta(contenido="\n".join(partes), resumen=resumen, citas=citas)


# --- luz_potencia_optima ---------------------------------------------------------------------------


def potencia(args: Mapping[str, Any], ctx: ContextoModulo) -> ResultadoHerramienta:
    try:
        contrato = _contrato(args, ctx)
        consumo = _consumo(ctx)
        maximos: list[tuple[str, Decimal, Decimal | None]] = []
        entrada = args.get("maximos")
        if entrada:
            if not isinstance(entrada, list):
                raise _EntradaError("«maximos» debe ser una lista de {mes, p1_kw, p2_kw}.")
            for x in entrada:
                m = Maximetro.model_validate(x)
                maximos.append((m.mes, m.p1_kw, m.p2_kw))
        elif consumo is not None and consumo.maximetro:
            maximos = [(m.mes, m.p1_kw, m.p2_kw) for m in consumo.maximetro]
        tarifa = ctx.perfil.get(HECHO_TARIFA)
        actual = cargar_oferta(tarifa) if tarifa else None
        margen = _dec(args.get("margen_pct", 10), "margen_pct")
        r = potencia_optima(contrato, consumo, maximos, actual, margen, ctx.hoy)
    except (_EntradaError, FacturaError, DatoNoDisponibleError) as exc:
        return _error(str(exc))
    except ValidationError as exc:
        return _error(f"Datos no válidos: {exc.errors()[0]['msg']}")
    partes = [
        f"Potencia contratada: P1 {fmt(contrato.potencia_p1_kw, 3)} kW, P2 {fmt(contrato.potencia_p2_kw, 3)} kW.",
        f"Máximo observado ({r.fuente_datos}): P1 {fmt(r.max_p1, 3)} kW, P2 {fmt(r.max_p2, 3)} kW.",
    ]
    if maximos:
        partes.append(
            f"Potencia suficiente con margen: P1 {fmt(r.recomendada_p1, 1)} kW, P2 {fmt(r.recomendada_p2, 1)} kW."
        )
        if r.ahorro_anual is not None:
            partes.append(f"Bajando a esa potencia ahorrarías unos {fmt(r.ahorro_anual)} € al año con tu tarifa.")
        if r.recomendada_p1 > contrato.potencia_p1_kw:
            partes.append("Tu máximo supera la potencia contratada en P1: si salta el limitador, valora subirla.")
    else:
        partes.append(f"Potencia media horaria máxima: {fmt(r.max_p1, 3)} kW (los picos reales son mayores).")
    partes.extend(f"Aviso: {a}" for a in r.avisos)
    citas = cargar_regulados().citas("BOE-A-2020-1066")
    partes.append(_fuentes_texto(citas))
    return ResultadoHerramienta(
        contenido="\n".join(partes),
        resumen=f"Potencia: máximo P1 {fmt(r.max_p1, 2)} kW ({r.fuente_datos})",
        citas=citas,
    )


# --- specs -------------------------------------------------------------------------------------------

_OFERTA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Tarifa: {nombre, tipo: 'fijo'|'pvpc', energia_eur_kwh: número o {p1,p2,p3}, potencia_eur_kw_dia: {p1,p2} "
        "(o potencia_eur_kw_anio), incluye_peajes_cargos (por defecto true), descuento_energia_pct, "
        "descuento_potencia_pct, bono_social_eur_dia, contador: monofasico|trifasico|propio, "
        "alquiler_contador_eur_dia, servicios_eur_mes, precio_valido_hasta, permanencia_hasta, penalizacion_eur}."
    ),
}


def herramientas() -> list[HerramientaModulo]:
    fecha = {"type": "string", "description": "Fecha AAAA-MM-DD."}
    return [
        HerramientaModulo(
            spec=ToolSpec(
                name="luz_calcular_factura",
                description=(
                    "Reproduce una factura de luz 2.0TD línea a línea (potencia, energía, bono social, impuesto "
                    "eléctrico, alquiler del contador, IVA) con la fuente de cada importe. Usa la tarifa y el consumo "
                    "guardados salvo que se indiquen. Con total_factura_real compara al céntimo."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "fecha_inicio": {**fecha, "description": "Primer día del periodo facturado."},
                        "fecha_fin": {**fecha, "description": "Último día del periodo facturado (incluido)."},
                        "consumo_kwh": {
                            "type": "object",
                            "description": "kWh de la factura por periodo {p1, p2, p3}, si no hay CSV.",
                            "properties": {p: {"type": "number"} for p in PERIODOS},
                        },
                        "csv": {"type": "string", "description": "CSV horario de Datadis o de la distribuidora."},
                        "oferta": _OFERTA_SCHEMA,
                        "potencia_p1_kw": {"type": "number"},
                        "potencia_p2_kw": {"type": "number"},
                        "precios_pvpc": {
                            "type": "string",
                            "description": "JSON del indicador 1001 de ESIOS (opcional).",
                        },
                        "tipos_reducidos": {
                            "type": "string",
                            "enum": ["auto", "si", "no"],
                            "description": "Meses con rebaja condicional de IVA/impuesto eléctrico: auto = tipos generales.",
                        },
                        "total_factura_real": {
                            "type": "number",
                            "description": "Total de la factura real, para comparar.",
                        },
                    },
                    "required": ["fecha_inicio", "fecha_fin"],
                },
            ),
            ejecutar=calcular,
        ),
        HerramientaModulo(
            spec=ToolSpec(
                name="luz_comparar_tarifas",
                description=(
                    "Calcula cuánto habría costado cada tarifa (la actual, las ofertas guardadas y el PVPC si hay "
                    "precios) con el mismo consumo de los últimos meses, impuestos incluidos, y dice cuál es la más barata."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "ofertas": {
                            "type": "array",
                            "items": _OFERTA_SCHEMA,
                            "description": "Ofertas extra a comparar.",
                        },
                        "incluir_pvpc": {"type": "boolean"},
                        "dias": {"type": "integer", "description": "Días hacia atrás (por defecto 365)."},
                    },
                },
            ),
            ejecutar=comparar_tarifas,
        ),
        HerramientaModulo(
            spec=ToolSpec(
                name="luz_importar_consumo",
                description=(
                    "Lee el CSV horario de Datadis, e-distribución o i-DE (o los totales por periodo de tus facturas, "
                    "o el maxímetro) y guarda un resumen del consumo en el expediente."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "csv": {"type": "string", "description": "Texto del CSV (CUPS;Fecha;Hora;Consumo;Metodo…)."},
                        "totales": {
                            "type": "array",
                            "description": "Totales de factura: [{desde, hasta, p1, p2, p3}] en kWh.",
                            "items": {"type": "object"},
                        },
                        "maximetro": {
                            "type": "array",
                            "description": "Potencias máximas: [{mes: 'AAAA-MM', p1_kw, p2_kw}].",
                            "items": {"type": "object"},
                        },
                        "guardar": {"type": "boolean", "description": "false para solo leer."},
                    },
                },
            ),
            ejecutar=importar,
            efecto="prepara",
        ),
        HerramientaModulo(
            spec=ToolSpec(
                name="luz_horas_baratas",
                description=(
                    "Dice qué horas de un día (mañana por defecto) son más baratas: periodos punta/llano/valle de "
                    "la 2.0TD y, si hay precios de REE, el PVPC hora a hora, las horas y el bloque más baratos."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "fecha": fecha,
                        "horas": {"type": "integer", "description": "Cuántas horas baratas listar (6)."},
                        "bloque_horas": {"type": "integer", "description": "Horas seguidas a programar (3)."},
                        "precios_pvpc": {
                            "type": "string",
                            "description": "JSON del indicador 1001 de ESIOS (opcional).",
                        },
                        "consultar_pvpc": {"type": "boolean"},
                    },
                },
            ),
            ejecutar=baratas,
        ),
        HerramientaModulo(
            spec=ToolSpec(
                name="luz_potencia_optima",
                description=(
                    "Estima la potencia contratada necesaria a partir del maxímetro (o, como mínimo orientativo, del "
                    "consumo horario) y el ahorro anual de ajustarla."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "maximos": {
                            "type": "array",
                            "description": "Potencias máximas por mes: [{mes: 'AAAA-MM', p1_kw, p2_kw}].",
                            "items": {"type": "object"},
                        },
                        "margen_pct": {"type": "number", "description": "Margen sobre el máximo (10 %)."},
                    },
                },
            ),
            ejecutar=potencia,
        ),
    ]
