"""Fetch TEAC (DYCTEA) criterios into the local RAG corpus.

The original `TEACCrawler` in `hacienda_gpt.crawler.teac` iterates over integer
IDs (``criterio.aspx?id=N`` for N in a range), but real DYCTEA criterio IDs
are compound strings like ``54/00218/2024/00/0/1``. As a result the scrapy
spider produced zero items in practice. This CLI replaces it with a small
pure-Python pipeline:

1. Hit ``criterios.aspx?fd=…&fh=…&pg=N`` for each result page.
2. Collect every ``criterio.aspx?id=<compound>`` link in the listing.
3. Download each criterio page individually and store HTML + parsed metadata
   under ``data/html/<snapshot>/teac/<safe_id>.{html,json}``.
4. Emit a per-snapshot manifest so the processor (and any audit step) can
   tell which criterios were ingested in this snapshot.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError

import click

from hacienda_gpt.crawler.teac import parse_teac_criterio

logger = logging.getLogger(__name__)


_USER_AGENT = "hacienda-asesor/0.1 (TEAC corpus refresh; contacto@hacienda-asesor.local)"
_LIST_URL_TMPL = (
    "https://serviciostelematicosext.hacienda.gob.es/TEAC/DYCTEA/criterios.aspx"
    "?s=1&fd={fd}&fh={fh}&tc=1&c=2&pg={pg}"
)
_CRITERIO_URL_TMPL = (
    "https://serviciostelematicosext.hacienda.gob.es/TEAC/DYCTEA/criterio.aspx?id={id}"
)

# DYCTEA emits its markup with single quotes and entity-encoded ampersands:
#   <a href='criterio.aspx?id=54/00218/2024/00/0/1&amp;q=...'>
# Accept either quote style; stop the id capture at the first '&' (the
# `&amp;q=` query suffix is search context we discard).
_CRITERIO_LINK_RE = re.compile(
    r"""href=['"](criterio\.aspx\?id=([^'"&]+))[^'"]*['"]""", re.IGNORECASE
)
_DDMMYYYY_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
_DEFAULT_PAGE_LIMIT = 50


def _http_get(url: str, *, retries: int = 2, backoff_s: float = 1.0) -> bytes:
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.read()
        except (HTTPError, URLError, TimeoutError) as exc:
            last = exc
            logger.warning("Attempt %d for %s failed: %s", attempt + 1, url, exc)
            if attempt < retries:
                time.sleep(backoff_s * (attempt + 1))
    raise click.ClickException(f"Could not fetch {url}: {last}")


def collect_criterio_ids(
    *,
    fd: str,
    fh: str,
    page_limit: int = _DEFAULT_PAGE_LIMIT,
    fetcher=None,
) -> list[str]:
    """Walk pagination, returning ordered unique criterio IDs for the date range."""
    # Late-bind to the module-level fetcher so tests can monkeypatch
    # `teac_seed._http_get` without having to reach into defaults.
    if fetcher is None:
        fetcher = _http_get
    seen: list[str] = []
    seen_set: set[str] = set()
    for pg in range(1, page_limit + 1):
        body = fetcher(_LIST_URL_TMPL.format(fd=fd, fh=fh, pg=pg))
        text = body.decode("utf-8", errors="replace")
        ids = [match.group(2) for match in _CRITERIO_LINK_RE.finditer(text)]
        new = [cid for cid in ids if cid not in seen_set]
        if not new:
            # Empty page → we've exhausted results. (DYCTEA returns a blank
            # listing past the last page rather than a 404.)
            break
        for cid in new:
            seen.append(cid)
            seen_set.add(cid)
    return seen


def _safe_filename(criterio_id: str) -> str:
    # Compound IDs contain '/'. We replace with '_' so the file maps 1:1.
    return urllib.parse.quote(criterio_id, safe="").replace("%2F", "_").replace("%2f", "_")


def fetch_criterio(criterio_id: str, target_dir: Path, *, fetcher=None) -> tuple[Path, Path | None]:
    if fetcher is None:
        fetcher = _http_get
    target_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_filename(criterio_id)
    html_path = target_dir / f"{safe}.html"
    body = fetcher(_CRITERIO_URL_TMPL.format(id=urllib.parse.quote(criterio_id, safe="/")))
    html = body.decode("utf-8", errors="replace")
    html_path.write_text(html, encoding="utf-8")

    metadata = parse_teac_criterio(html, criterio_id=criterio_id)
    if metadata is None:
        # DYCTEA's "criterio no existe" placeholder — discard the html too so
        # we don't index garbage.
        html_path.unlink(missing_ok=True)
        return html_path, None
    metadata_path = target_dir / f"{safe}.json"
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return html_path, metadata_path


def _validate_date(ctx, param, value: str | None) -> str | None:  # noqa: ANN001
    if value is None:
        return value
    # Accept ISO (YYYY-MM-DD) but emit the DD/MM/YYYY form expected by DYCTEA.
    try:
        dt = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        if _DDMMYYYY_RE.match(value):
            return value
        raise click.BadParameter(f"Invalid date '{value}'. Use YYYY-MM-DD or DD/MM/YYYY.") from None
    return dt.strftime("%d/%m/%Y")


@click.command()
@click.option(
    "--from-date",
    "from_date",
    callback=_validate_date,
    required=True,
    help="Lower bound for fecha_resolucion (YYYY-MM-DD or DD/MM/YYYY).",
)
@click.option(
    "--to-date",
    "to_date",
    callback=_validate_date,
    required=True,
    help="Upper bound for fecha_resolucion (YYYY-MM-DD or DD/MM/YYYY).",
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./data/html"),
    show_default=True,
    help="Root for the local snapshot (results go under <output>/<snapshot>/teac/).",
)
@click.option(
    "--snapshot-date",
    default=None,
    help="Snapshot folder name (YYYY-MM-DD). Defaults to today (UTC).",
)
@click.option(
    "--page-limit",
    default=_DEFAULT_PAGE_LIMIT,
    show_default=True,
    type=int,
    help="Hard cap on result pages to walk; protects against runaway requests.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Discover IDs but don't download criterio pages.",
)
def cli(
    from_date: str,
    to_date: str,
    output_dir: Path,
    snapshot_date: str | None,
    page_limit: int,
    dry_run: bool,
) -> None:
    """Download every TEAC criterio resolved between --from-date and --to-date."""
    snapshot = snapshot_date or datetime.now(UTC).strftime("%Y-%m-%d")
    teac_dir = output_dir / snapshot / "teac"

    click.echo(f"Walking DYCTEA pagination ({from_date} → {to_date}, max {page_limit} pages)...")
    ids = collect_criterio_ids(fd=from_date, fh=to_date, page_limit=page_limit)
    click.echo(f"  → {len(ids)} criterios discovered")

    if dry_run:
        for cid in ids:
            click.echo(f"[dry-run] {cid}")
        return

    documents: list[dict[str, str | bool]] = []
    skipped: list[str] = []
    for cid in ids:
        html_path, metadata_path = fetch_criterio(cid, teac_dir)
        if metadata_path is None:
            skipped.append(cid)
            continue
        documents.append(
            {
                "criterio_id": cid,
                "html": str(html_path),
                "metadata": str(metadata_path),
            }
        )

    manifest_path = output_dir / snapshot / "teac-seed-manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "snapshot_date": snapshot,
                "fetched_at": datetime.now(UTC).isoformat(),
                "from_date": from_date,
                "to_date": to_date,
                "page_limit": page_limit,
                "documents": documents,
                "skipped_not_existing": skipped,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    click.echo(f"Saved {len(documents)} criterios (skipped {len(skipped)} non-existing).")
    click.echo(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    cli()
