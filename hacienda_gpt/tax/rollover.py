"""Open a new tax year from the previous one, keeping only what still checks out.

Most IRPF parameters do not change from one year to the next. Instead of
re-reading sixteen laws every January, the new year's file starts as a copy of
the last one and every group of parameters is re-verified against the current
text of its law (see ``verificacion``). What still matches stays verified; what
no longer matches is flagged — parameter groups are marked ``verificado: false``
(the calculator then says so in every result that uses them) and regional
deductions that fail are left out — so the maintainer reviews a short list.
"""

from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.tax.params import DATA_DIR, IRPFParams
from hacienda_gpt.tax.verificacion import Resultado, TextoDe, comprobaciones, verificar


class InformeRollover(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origen: int
    destino: int
    comprobaciones: int
    conformes: int
    # Parameter groups now marked as not verified, with what was missing.
    sin_verificar: list[str] = Field(default_factory=list)
    # Regional deductions dropped because their quote is no longer in the law.
    deducciones_retiradas: list[str] = Field(default_factory=list)
    # Things no check can decide.
    revisar_a_mano: list[str] = Field(default_factory=list)


def _nodo(data: dict[str, Any], ruta: list[str]) -> Any:
    node: Any = data
    for paso in ruta:
        node = node[int(paso)] if isinstance(node, list) else node[paso]
    return node


def preparar_ejercicio(
    origen: int, destino: int, texto_de: TextoDe, *, hoy: date
) -> tuple[dict[str, Any], InformeRollover]:
    """The JSON of year ``destino`` built from ``origen``, and what must be reviewed."""
    raw: dict[str, Any] = json.loads((DATA_DIR / f"irpf_{origen}.json").read_text(encoding="utf-8"))
    checks = comprobaciones(IRPFParams(**raw))
    resultados = verificar(checks, texto_de)
    informe = InformeRollover(
        origen=origen, destino=destino, comprobaciones=len(resultados), conformes=sum(r.ok for r in resultados)
    )

    retirar: dict[tuple[str, ...], list[int]] = {}
    for resultado in resultados:
        _aplicar(raw, resultado, origen, informe, retirar)
    for ruta_lista, indices in retirar.items():
        lista = _nodo(raw, list(ruta_lista))
        for indice in sorted(indices, reverse=True):
            del lista[indice]

    # Refresh the provenance of every law that could be read.
    fuentes = [raw["fuente_principal"], *(c["fuente_principal"] for c in raw.get("comunidades", {}).values())]
    for fuente in fuentes:
        pagina = texto_de(fuente["boe_id"], None)
        if pagina:
            fuente["consultado_el"] = hoy.isoformat()
            fuente["sha256_html"] = hashlib.sha256(pagina.encode("utf-8")).hexdigest()
        else:
            informe.revisar_a_mano.append(
                f"No se pudo leer {fuente['boe_id']}: su procedencia sigue siendo la de {origen}."
            )

    raw["ejercicio"] = destino
    raw["nota_ejercicio"] = (
        f"Creado el {hoy.isoformat()} a partir del ejercicio {origen} y reverificado contra el texto vigente de cada ley."
    )
    raw["revisiones"] = [
        {
            "norma": f"Apertura del ejercicio {destino}",
            "boe_id": "varios",
            "revisado_el": hoy.isoformat(),
            "resultado": f"{informe.conformes} de {informe.comprobaciones} comprobaciones conformes; "
            f"{len(informe.sin_verificar)} grupos sin verificar y {len(informe.deducciones_retiradas)} deducciones retiradas.",
        }
    ]
    informe.revisar_a_mano += [
        f"Disposiciones de vigencia anual (prórrogas «para {origen}», deducciones temporales, deflactaciones): ninguna "
        "comprobación sabe si se han prorrogado.",
        "Calendario fiscal del nuevo ejercicio y, si los hay, parámetros leídos de una redacción histórica.",
        "Normas publicadas y aún no consolidadas en el BOE (consulta el registro de boe-novedades).",
    ]
    IRPFParams(**raw)  # the result must still be a valid parameter file
    return raw, informe


def _aplicar(
    raw: dict[str, Any],
    resultado: Resultado,
    origen: int,
    informe: InformeRollover,
    retirar: dict[tuple[str, ...], list[int]],
) -> None:
    check = resultado.comprobacion
    ruta = check.ruta
    es_deduccion = len(ruta) >= 2 and ruta[-2] == "deducciones_calculables"
    historica = check.version is not None
    if resultado.ok and not historica:
        return
    motivo = (
        f"se leyó de una redacción histórica ({check.version}) válida para {origen}"
        if historica and resultado.ok
        else "ya no coincide con el texto vigente: " + ", ".join(resultado.faltan[:5])
    )
    if es_deduccion:
        retirar.setdefault(tuple(ruta[:-1]), []).append(int(ruta[-1]))
        informe.deducciones_retiradas.append(f"{check.descripcion} — {motivo}")
        return
    nodo = _nodo(raw, ruta)
    fuente = nodo.get("fuente") if isinstance(nodo, dict) else None
    if isinstance(fuente, dict):
        fuente["verificado"] = False
        fuente["nota"] = f"Sin verificar para este ejercicio: {motivo}."
        fuente.pop("version_historica", None)
    informe.sin_verificar.append(f"{check.descripcion} — {motivo}")


def heredar_citas(origen: int, destino: int, data: dict[str, Any], *, data_dir: Path = DATA_DIR) -> list[str]:
    """Carry the quotes of ``evidencia_parametros.json`` from year ``origen`` to ``destino`` for every group that
    is still in the new file. Returns the paths carried.

    Each entry is bound to its group's figures by ``sha256_valor``, so a quote never vouches for a group whose
    figures changed: ``verificacion`` falls back to the figures check for it. Without this, every group checked by
    quote in ``origen`` would silently go back to the weaker figures check in ``destino``.
    """
    path = data_dir / "evidencia_parametros.json"
    if not path.exists():
        return []
    evidencia: dict[str, dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    previas = evidencia.get(f"irpf_{origen}.json", {})
    heredadas: dict[str, Any] = {}
    for ruta, entrada in previas.items():
        try:
            _nodo(data, ruta.split("/"))
        except (KeyError, IndexError, TypeError):
            continue
        heredadas[ruta] = entrada
    if not heredadas:
        return []
    evidencia[f"irpf_{destino}.json"] = {**heredadas, **evidencia.get(f"irpf_{destino}.json", {})}
    path.write_text(json.dumps(evidencia, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return sorted(heredadas)
