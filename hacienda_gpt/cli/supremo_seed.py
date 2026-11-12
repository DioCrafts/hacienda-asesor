"""Selective seeding of Tribunal Supremo tax case law (Sala Tercera, Sección Segunda).

This CLI never crawls or searches the CENDOJ portal. The reuse conditions of
poderjudicial.es forbid mass downloading and building databases without CGPJ
authorisation, so the tool only works on:

- ``--from-dir``: HTML files the user already has on disk (no network), and/or
- ``--urls-file``: an explicit list of individual document URLs, fetched one
  by one with a hard cap (``--max-downloads``) and a pause between requests.

Links found inside the fetched pages are never followed. Each document is
analysed with :mod:`hacienda_gpt.doctrine.supremo`; only tax rulings of
Sala Tercera, Sección Segunda with enough priority are kept, as
``<slug>.html`` plus a ``<slug>.json`` sidecar that the indexer merges into
every chunk. A manifest records what was kept, what was discarded and why.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
import hashlib
import json
import logging
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import urlsplit

import click
import requests

from hacienda_gpt.doctrine.supremo import analizar_documento, es_objetivo_prioritario, sidecar_metadata

logger = logging.getLogger(__name__)

# Generic on purpose: no e-mail address or any other personal data.
_USER_AGENT = "hacienda-asesor/0.1 (consulta individual de resoluciones; sin descarga masiva)"
_ALLOWED_HOSTS = frozenset({"www.poderjudicial.es", "poderjudicial.es"})
_TIMEOUT_S = 30
# HTTP statuses that mean the portal is throttling or refusing us: stop asking.
_BLOCKING_STATUSES = frozenset({403, 429})

DEFAULT_MAX_DOWNLOADS = 25
HARD_MAX_DOWNLOADS = 100
MANIFEST_NAME = "supremo-seed-manifest.json"

AVISO_CENDOJ = (
    "AVISO: las condiciones de reutilización del CENDOJ (poderjudicial.es) solo permiten la consulta individual "
    "de resoluciones. Están prohibidas la descarga masiva y la creación de bases de datos sin autorización del "
    "Consejo General del Poder Judicial (CGPJ); cualquier reutilización masiva requiere esa autorización. Esta "
    "herramienta no rastrea ni busca en el portal: descarga, una a una y con pausas, únicamente las URL de "
    "documentos concretos que usted le indique, hasta el límite fijado."
)


class DescargaError(Exception):
    """A single document could not be fetched."""

    def __init__(self, mensaje: str, *, bloqueo: bool = False) -> None:
        super().__init__(mensaje)
        # True when the portal is refusing or throttling requests.
        self.bloqueo = bloqueo


def _http_get(url: str) -> bytes:
    """Fetch one document. Tests replace this function; it is the only network access."""
    try:
        response = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=_TIMEOUT_S)
    except requests.RequestException as exc:
        raise DescargaError(f"no se pudo conectar ({exc.__class__.__name__})") from exc
    if response.status_code != 200:
        raise DescargaError(
            f"respuesta HTTP {response.status_code}",
            bloqueo=response.status_code in _BLOCKING_STATUSES,
        )
    return response.content


def _pausa(segundos: float) -> None:
    """Wait between two downloads. Tests replace this function."""
    time.sleep(segundos)


def es_url_permitida(url: str) -> bool:
    """True for https URLs whose host is exactly the CENDOJ portal."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return False
    return parts.scheme == "https" and host in _ALLOWED_HOSTS


def leer_urls(path: Path) -> list[str]:
    """Read one URL per line, ignoring blank lines, ``#`` comments and repeats."""
    urls: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        url = line.strip()
        if url and not url.startswith("#") and url not in urls:
            urls.append(url)
    return urls


def slug_documento(roj: str | None, ecli: str | None, contenido: bytes) -> str:
    """File stem for a ruling: ROJ, else ECLI, else a hash of the content."""
    for identifier in (roj, ecli):
        slug = re.sub(r"[^A-Za-z0-9]+", "_", identifier or "").strip("_")
        if slug:
            return slug
    return f"doc_{hashlib.sha256(contenido).hexdigest()[:16]}"


def _decodificar(contenido: bytes) -> str:
    try:
        return contenido.decode("utf-8")
    except UnicodeDecodeError:
        return contenido.decode("cp1252", errors="replace")


def _archivos_locales(from_dir: Path, output_dir: Path) -> Iterator[Path]:
    """HTML files under ``from_dir``, leaving out what a previous run wrote to ``output_dir``."""
    salida = output_dir.resolve()
    for path in sorted(from_dir.rglob("*.html")):
        if salida != from_dir.resolve() and salida in path.resolve().parents:
            continue
        yield path


def _validar_max_descargas(ctx: click.Context, param: click.Parameter, value: int) -> int:
    if value > HARD_MAX_DOWNLOADS:
        raise click.BadParameter(
            f"el máximo admitido es {HARD_MAX_DOWNLOADS}. Las condiciones de uso del CENDOJ prohíben la descarga "
            "masiva de resoluciones sin autorización del CGPJ; esta herramienta solo sirve para consultar un "
            "número reducido de documentos concretos."
        )
    if value < 1:
        raise click.BadParameter("debe ser al menos 1.")
    return value


def _error_entry(origen: str, mensaje: str) -> dict[str, Any]:
    return {
        "origen": origen,
        "slug": None,
        "roj": None,
        "fecha": None,
        "prioridad": 0,
        "fija_doctrina": False,
        "conservada": False,
        "error": True,
        "motivos": [mensaje],
    }


def procesar_documento(
    contenido: bytes,
    *,
    origen: str,
    source_url: str | None,
    output_dir: Path,
    min_priority: int,
    dry_run: bool,
    slugs_vistos: set[str],
) -> dict[str, Any]:
    """Analyse one document, write it if it qualifies and return its manifest entry."""
    sentencia, base = analizar_documento(_decodificar(contenido), source_url=source_url)
    slug = slug_documento(sentencia.roj, sentencia.ecli, contenido)
    motivos = list(sentencia.motivos)

    conservada = es_objetivo_prioritario(sentencia) and sentencia.prioridad >= min_priority
    if es_objetivo_prioritario(sentencia) and not conservada:
        motivos.append(f"Se descarta: prioridad {sentencia.prioridad} inferior al mínimo exigido ({min_priority}).")
    if conservada and slug in slugs_vistos:
        conservada = False
        motivos.append("Se descarta: duplicada, la misma resolución ya se ha tratado en esta ejecución.")
    if conservada:
        slugs_vistos.add(slug)
        if not dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)
            # Bytes are stored untouched so the charset declared in the page stays true.
            (output_dir / f"{slug}.html").write_bytes(contenido)
            (output_dir / f"{slug}.json").write_text(
                json.dumps(sidecar_metadata(sentencia, base), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    return {
        "origen": origen,
        "slug": slug,
        "roj": sentencia.roj,
        "fecha": sentencia.fecha.isoformat() if sentencia.fecha else None,
        "prioridad": sentencia.prioridad,
        "fija_doctrina": sentencia.fija_doctrina,
        "conservada": conservada,
        "error": False,
        "motivos": motivos,
    }


@click.command()
@click.option(
    "--from-dir",
    "from_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Directorio con ficheros *.html de sentencias ya descargados (no usa la red).",
)
@click.option(
    "--urls-file",
    "urls_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Fichero con una URL de documento del CENDOJ por línea (se ignoran líneas en blanco y comentarios #).",
)
@click.option(
    "--output-dir",
    "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    required=True,
    help="Directorio donde se guardan las sentencias conservadas (<slug>.html y <slug>.json) y el manifiesto.",
)
@click.option(
    "--min-priority",
    "min_priority",
    type=click.IntRange(0, 100),
    default=1,
    show_default=True,
    help="Prioridad doctrinal mínima (0-100) para conservar una sentencia.",
)
@click.option(
    "--max-downloads",
    "max_downloads",
    type=int,
    default=DEFAULT_MAX_DOWNLOADS,
    show_default=True,
    callback=_validar_max_descargas,
    help=f"Número máximo de URL que se descargan en una ejecución (nunca más de {HARD_MAX_DOWNLOADS}).",
)
@click.option(
    "--delay",
    type=click.FloatRange(min=1.0),
    default=5.0,
    show_default=True,
    help="Segundos de espera entre dos descargas consecutivas (mínimo 1).",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Analiza (y descarga, si hay --urls-file) pero no escribe ningún fichero.",
)
def cli(
    from_dir: Path | None,
    urls_file: Path | None,
    output_dir: Path,
    min_priority: int,
    max_downloads: int,
    delay: float,
    dry_run: bool,
) -> None:
    """Incorpora de forma selectiva sentencias tributarias del Tribunal Supremo (Sala 3ª, Sección 2ª)."""
    if from_dir is None and urls_file is None:
        raise click.UsageError("Indique al menos una fuente: --from-dir o --urls-file.")

    documentos: list[dict[str, Any]] = []
    rechazadas: list[str] = []
    omitidas: list[str] = []
    slugs_vistos: set[str] = set()

    def procesar(contenido: bytes, origen: str, source_url: str | None) -> None:
        entry = procesar_documento(
            contenido,
            origen=origen,
            source_url=source_url,
            output_dir=output_dir,
            min_priority=min_priority,
            dry_run=dry_run,
            slugs_vistos=slugs_vistos,
        )
        documentos.append(entry)
        estado = "conservada" if entry["conservada"] else "descartada"
        click.echo(f"  [{estado}] {entry['slug']} (prioridad {entry['prioridad']}) ← {origen}")

    if urls_file is not None:
        click.echo(AVISO_CENDOJ)

    if from_dir is not None:
        click.echo(f"Analizando los ficheros HTML de {from_dir}...")
        for path in _archivos_locales(from_dir, output_dir):
            procesar(path.read_bytes(), str(path), None)

    if urls_file is not None:
        permitidas: list[str] = []
        for url in leer_urls(urls_file):
            if es_url_permitida(url):
                permitidas.append(url)
            else:
                rechazadas.append(url)
                click.echo(f"  [rechazada] {url}: solo se admiten URL https de www.poderjudicial.es.", err=True)
        permitidas, omitidas = permitidas[:max_downloads], permitidas[max_downloads:]
        if omitidas:
            click.echo(
                f"Se han indicado más URL que el límite de {max_downloads} descargas: "
                f"{len(omitidas)} se omiten en esta ejecución.",
                err=True,
            )
        click.echo(f"Descargando {len(permitidas)} documentos, uno a uno, con {delay:g} s de pausa...")
        for index, url in enumerate(permitidas):
            if index:
                _pausa(delay)
            try:
                contenido = _http_get(url)
            except DescargaError as exc:
                logger.warning("Download failed for %s: %s", url, exc)
                documentos.append(_error_entry(url, f"Error de descarga: {exc}."))
                click.echo(f"  [error] {url}: {exc}", err=True)
                if exc.bloqueo:
                    omitidas.extend(permitidas[index + 1 :])
                    click.echo(
                        "El portal ha rechazado o limitado la petición: se interrumpen las descargas restantes.",
                        err=True,
                    )
                    break
                continue
            procesar(contenido, url, url)

    conservadas = sum(1 for doc in documentos if doc["conservada"])
    errores = sum(1 for doc in documentos if doc["error"])
    descartadas = len(documentos) - conservadas - errores
    fijan_doctrina = sum(1 for doc in documentos if doc["conservada"] and doc["fija_doctrina"])

    if not dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = output_dir / MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(
                {
                    "generado_en": datetime.now(UTC).isoformat(),
                    "from_dir": str(from_dir) if from_dir else None,
                    "urls_file": str(urls_file) if urls_file else None,
                    "min_priority": min_priority,
                    "max_downloads": max_downloads,
                    "totales": {
                        "analizadas": len(documentos) - errores,
                        "conservadas": conservadas,
                        "descartadas": descartadas,
                        "fijan_doctrina": fijan_doctrina,
                        "errores_descarga": errores,
                        "urls_rechazadas": len(rechazadas),
                        "urls_omitidas": len(omitidas),
                    },
                    "urls_rechazadas": rechazadas,
                    "urls_omitidas": omitidas,
                    "documentos": documentos,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    prefijo = "[simulación] " if dry_run else ""
    click.echo(
        f"{prefijo}Resumen: {conservadas} conservadas ({fijan_doctrina} fijan doctrina), "
        f"{descartadas} descartadas, {errores} errores de descarga."
    )
    if rechazadas or omitidas:
        click.echo(f"{prefijo}URL rechazadas: {len(rechazadas)}; URL omitidas: {len(omitidas)}.")
    if dry_run:
        click.echo("[simulación] No se ha escrito ningún fichero.")
    else:
        click.echo(f"Manifiesto: {output_dir / MANIFEST_NAME}")


if __name__ == "__main__":
    cli()
