"""Fetch a curated set of BOE consolidated documents into the RAG corpus.

The general `boe-ccaa` crawler downloads the CCAA codes; this CLI is a thin
companion for the long tail of *individual* legislative acts that we want
indexed by ID — e.g. the Modelo 720 trio (Ley 7/2012, RD 1558/2012, Orden
HAP/72/2013). Maintainers add new IDs to ``rules/boe_seed_catalog.json`` and
re-run ``poetry run python -m hacienda_gpt.cli.boe_seed`` to refresh the
local snapshot.

We deliberately go through ``https://www.boe.es/buscar/act.php?id=<ID>``
rather than the original ``/diario_boe/txt.php`` so that consolidated text
(post-amendments) is what gets indexed.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError

import click

logger = logging.getLogger(__name__)


_BOE_CONSOLIDATED_URL = "https://www.boe.es/buscar/act.php?id={id}"
# Resource-relative default so the CLI can be run from anywhere, and so the
# catalog never collides with `hacienda_gpt.decision.rules.load_rules_from_directory`
# which globs every JSON in the top-level ``rules/`` folder.
_DEFAULT_CATALOG = Path(__file__).resolve().parent / "boe_seed_catalog.json"
_DEFAULT_OUTPUT = Path("./data/html")
_DEFAULT_SUBDIR = "boe-seed"
_USER_AGENT = "hacienda-asesor/0.1 (RAG corpus refresh; contacto@hacienda-asesor.local)"


def load_catalog(catalog_path: Path) -> list[dict[str, str]]:
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    entries: list[dict[str, str]] = []
    for entry in raw.get("documents", []):
        boe_id = entry.get("id")
        if not boe_id:
            raise click.ClickException(f"Catalog entry missing 'id': {entry!r}")
        entries.append(
            {
                "id": boe_id,
                "label": entry.get("label", boe_id),
                "topic": entry.get("topic", ""),
                "subdir": entry.get("subdir", _DEFAULT_SUBDIR),
            }
        )
    return entries


def fetch_document(boe_id: str, target_path: Path, retries: int = 2, backoff_s: float = 1.0) -> int:
    """Download a single consolidated BOE document; return bytes written."""
    url = _BOE_CONSOLIDATED_URL.format(id=boe_id)
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                body = response.read()
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(body)
                return len(body)
        except (HTTPError, URLError, TimeoutError) as exc:
            last_exc = exc
            logger.warning("Attempt %d for %s failed: %s", attempt + 1, boe_id, exc)
            if attempt < retries:
                time.sleep(backoff_s * (attempt + 1))
    raise click.ClickException(f"Could not fetch {boe_id}: {last_exc}")


@click.command()
@click.option(
    "--catalog",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=_DEFAULT_CATALOG,
    show_default=True,
    help="JSON file with the BOE IDs to fetch.",
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=_DEFAULT_OUTPUT,
    show_default=True,
    help="Root directory for the local HTML snapshot (each ID is written under"
    " <output-dir>/<snapshot-date>/<subdir>/<ID>.html).",
)
@click.option(
    "--snapshot-date",
    default=None,
    help="Snapshot folder name (YYYY-MM-DD). Defaults to today (UTC).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would be downloaded without making HTTP calls.",
)
def cli(catalog: Path, output_dir: Path, snapshot_date: str | None, dry_run: bool) -> None:
    """Fetch every BOE ID listed in CATALOG into OUTPUT_DIR."""
    snapshot = snapshot_date or datetime.now(UTC).strftime("%Y-%m-%d")
    entries = load_catalog(catalog)
    if not entries:
        raise click.ClickException("Catalog has no documents.")

    snapshot_root = output_dir / snapshot
    summary: list[dict[str, str | int]] = []
    for entry in entries:
        target = snapshot_root / entry["subdir"] / f"{entry['id']}.html"
        if dry_run:
            click.echo(f"[dry-run] {entry['id']:18} → {target}")
            summary.append({"id": entry["id"], "path": str(target), "bytes": 0})
            continue
        click.echo(f"Fetching {entry['id']} ({entry['label']}) → {target}")
        size = fetch_document(entry["id"], target)
        summary.append({"id": entry["id"], "path": str(target), "bytes": size})

    manifest = snapshot_root / "boe-seed-manifest.json"
    if not dry_run:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {
                    "snapshot_date": snapshot,
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "catalog_path": str(catalog),
                    "documents": summary,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        click.echo(f"Wrote manifest: {manifest}")


if __name__ == "__main__":
    cli()
