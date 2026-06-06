"""Detect drift in tracked BOE consolidated documents and re-download on change.

The Spanish tax code is *consolidated*: the BOE keeps every published act
under a stable ID (e.g. ``BOE-A-2006-20764`` = LIRPF) and silently
republishes the same URL whenever an amendment lands, replacing the
text. That means the snapshot of LIRPF we ingested last month might
already be stale today — without us noticing.

The BOE publishes a public metadata API:

  GET /datosabiertos/api/legislacion-consolidada/id/<ID>/metadatos

which returns (~1.4 KB JSON) a ``fecha_actualizacion`` field timestamped
to the second. We compare it against a small local state file
(``_boe_watch_state.json``) and only re-download the heavy consolidated
HTML when the timestamp has moved. The downstream FAISS ingest is
manifest-driven, so the new text gets re-chunked automatically on the
next ``processor --incremental`` run.

Usage::

    # Daily / weekly via launchd/cron:
    uv run python -m hacienda_gpt.cli.boe_watch
    uv run python -m hacienda_gpt.cli.processor --incremental --max-tokens 512

Add ``--dry-run`` to inspect what would change without writing anything,
``--catalog`` to point at a non-default catalog, or ``--only ID,ID,…``
to limit the check to a subset of IDs.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import logging
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
import urllib.request

import click

logger = logging.getLogger(__name__)

_BOE_META_URL = (
    "https://www.boe.es/datosabiertos/api/legislacion-consolidada/id/{id}/metadatos"
)
_BOE_HTML_URL = "https://www.boe.es/buscar/act.php?id={id}"
_DEFAULT_CATALOG = Path(__file__).resolve().parent / "boe_seed_catalog.json"
_DEFAULT_OUTPUT = Path("./data/html")
_USER_AGENT = "hacienda-asesor/0.1 (corpus drift watcher; contacto@hacienda-asesor.local)"


# --------------------------------------------------------------------- #
# State file: { schema_version, checked_at, entries: {id: {…}} }
# --------------------------------------------------------------------- #
def _state_path(output_dir: Path) -> Path:
    return output_dir / "_boe_watch_state.json"


def _changes_log_path(output_dir: Path) -> Path:
    return output_dir / "_boe_changes.jsonl"


def _load_state(output_dir: Path) -> dict:
    """Return the persisted state or an empty skeleton. Missing fields
    are treated as "we have never seen this entry" so the first run
    naturally re-syncs everything without special-casing."""
    path = _state_path(output_dir)
    if not path.exists():
        return {"schema_version": 1, "entries": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("State file %s is unreadable (%s); starting fresh.", path, exc)
        return {"schema_version": 1, "entries": {}}


def _save_state(state: dict, output_dir: Path) -> None:
    """Atomic write of the state file via tempfile-then-rename so a
    crash in the middle of writing leaves the previous version intact."""
    path = _state_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["checked_at"] = datetime.now(UTC).isoformat()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _append_change_event(output_dir: Path, event: dict) -> None:
    """Append-only JSONL audit log. Each change in any tracked document
    is recorded as one line so a future report / dashboard can answer
    'what changed since DATE?' without scanning the index."""
    path = _changes_log_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------- #
# BOE API + HTML fetch
# --------------------------------------------------------------------- #
def _fetch_metadata(boe_id: str, timeout: int = 30) -> dict:
    """Fetch ``/metadatos`` for a BOE consolidated id and return the
    parsed ``data[0]`` dict. Raises on non-200 or malformed JSON."""
    req = urllib.request.Request(
        _BOE_META_URL.format(id=boe_id),
        headers={"Accept": "application/json", "User-Agent": _USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("status", {}).get("code") != "200":
        raise RuntimeError(f"BOE metadata status != 200: {payload!r}")
    data = payload.get("data") or []
    if not data:
        raise RuntimeError(f"BOE metadata empty payload for {boe_id}: {payload!r}")
    return data[0] if isinstance(data, list) else data


def _fetch_html(boe_id: str, timeout: int = 60) -> bytes:
    """Fetch the consolidated HTML text via the canonical ``act.php`` URL
    that the seed crawler also uses. Kept as bytes so the SHA-256 we
    record matches what the manifest computes — no normalisation in
    between."""
    req = urllib.request.Request(
        _BOE_HTML_URL.format(id=boe_id),
        headers={"User-Agent": _USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------- #
# Catalog parsing
# --------------------------------------------------------------------- #
def _load_catalog(catalog_path: Path) -> list[dict]:
    raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    return raw.get("documents", [])


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #
@click.command()
@click.option(
    "--catalog",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=_DEFAULT_CATALOG,
    show_default=True,
    help="Catalog file listing the BOE consolidated documents to monitor.",
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=_DEFAULT_OUTPUT,
    show_default=True,
    help="Root directory holding the snapshot dirs. The state file is "
    "kept at <output-dir>/_boe_watch_state.json so it survives across "
    "snapshot rotations.",
)
@click.option(
    "--snapshot-date",
    default=None,
    help="Snapshot folder name (YYYY-MM-DD). Defaults to today UTC. The "
    "redownloaded HTML is written under "
    "<output-dir>/<snapshot>/<subdir>/<ID>.html so the next "
    "``processor --incremental`` run picks it up via SHA-256 diff.",
)
@click.option(
    "--only",
    default=None,
    help="Comma-separated list of BOE IDs to check (subset of catalog).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Compare metadata against state but DO NOT redownload, write "
    "state, or append to the change log.",
)
@click.option(
    "--polite-delay",
    default=0.5,
    type=click.FloatRange(min=0.0),
    show_default=True,
    help="Seconds between API calls. Two requests per ID (metadata + "
    "possibly HTML), times 30 catalog entries, is well within polite "
    "limits even with a zero delay — keep something small for nicety.",
)
def cli(
    catalog: Path,
    output_dir: Path,
    snapshot_date: str | None,
    only: str | None,
    dry_run: bool,
    polite_delay: float,
) -> None:
    """Check every tracked BOE consolidated document for an update and
    re-download those whose ``fecha_actualizacion`` has moved.

    The check is cheap (~1.4 KB per ID for the metadata call), so this
    is safe to run daily. Re-downloads only happen when a real change is
    detected — typically a few times a year per law during budget
    season or after major reforms.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    snapshot = snapshot_date or datetime.now(UTC).strftime("%Y-%m-%d")
    snapshot_root = output_dir / snapshot

    catalog_entries = _load_catalog(catalog)
    if only:
        wanted = {x.strip() for x in only.split(",") if x.strip()}
        catalog_entries = [e for e in catalog_entries if e.get("id") in wanted]
    # Skip entries explicitly marked as non-trackable. These are typically
    # acts that BOE does not consolidate (sentencias TC, RDs puntuales)
    # so the /metadatos endpoint would 404 on them. They get downloaded
    # once by boe_seed and live untouched in the snapshot afterwards.
    catalog_entries = [e for e in catalog_entries if e.get("track", True)]

    state = _load_state(output_dir)
    entries_state: dict = state.setdefault("entries", {})

    click.echo(
        f"Checking {len(catalog_entries)} BOE document(s) against state at "
        f"{_state_path(output_dir)}"
        f"{' (dry-run)' if dry_run else ''}"
    )

    stats = {"checked": 0, "unchanged": 0, "changed": 0, "new": 0, "errors": 0}
    for entry in catalog_entries:
        boe_id = entry.get("id")
        if not boe_id:
            continue
        stats["checked"] += 1

        try:
            meta = _fetch_metadata(boe_id)
        except (URLError, HTTPError, RuntimeError, json.JSONDecodeError) as exc:
            stats["errors"] += 1
            click.echo(f"  ✗ {boe_id}  metadata fetch failed: {exc}")
            time.sleep(polite_delay)
            continue

        remote_actualizacion = meta.get("fecha_actualizacion", "")
        local = entries_state.get(boe_id, {})
        local_actualizacion = local.get("fecha_actualizacion")

        if not local_actualizacion:
            verdict = "NEW"
            stats["new"] += 1
        elif remote_actualizacion == local_actualizacion:
            verdict = "unchanged"
            stats["unchanged"] += 1
        else:
            verdict = "CHANGED"
            stats["changed"] += 1

        # Always update local cache of metadata even when unchanged — it
        # picks up new fields the BOE might start exposing later.
        title = meta.get("titulo", "")[:80]
        click.echo(f"  {verdict:9s}  {boe_id}  ({remote_actualizacion})  {title}")

        if dry_run:
            time.sleep(polite_delay)
            continue

        if verdict in ("NEW", "CHANGED"):
            # Re-download the consolidated HTML and save to the snapshot
            # folder of today. The processor's --incremental pass picks
            # it up via SHA-256 diff against the manifest entry.
            subdir = entry.get("subdir", "boe-seed")
            target = snapshot_root / subdir / f"{boe_id}.html"
            try:
                body = _fetch_html(boe_id)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(body)
            except (URLError, HTTPError) as exc:
                stats["errors"] += 1
                click.echo(f"  ✗ {boe_id}  HTML download failed: {exc}")
                time.sleep(polite_delay)
                continue
            new_sha = _sha256(body)
            _append_change_event(
                output_dir,
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "id": boe_id,
                    "event": verdict.lower(),
                    "previous_actualizacion": local_actualizacion,
                    "new_actualizacion": remote_actualizacion,
                    "previous_sha256": local.get("sha256"),
                    "new_sha256": new_sha,
                    "path": str(target),
                    "title": meta.get("titulo"),
                },
            )
            entries_state[boe_id] = {
                "fecha_actualizacion": remote_actualizacion,
                "last_check_at": datetime.now(UTC).isoformat(),
                "last_download_at": datetime.now(UTC).isoformat(),
                "sha256": new_sha,
                "title": meta.get("titulo"),
                "subdir": subdir,
                "estatus_derogacion": meta.get("estatus_derogacion"),
                "estado_consolidacion": (
                    meta.get("estado_consolidacion", {}).get("texto")
                    if isinstance(meta.get("estado_consolidacion"), dict)
                    else meta.get("estado_consolidacion")
                ),
            }
        else:
            # Refresh last_check_at so the audit shows recency even when
            # nothing changed.
            entries_state.setdefault(boe_id, {})
            entries_state[boe_id]["last_check_at"] = datetime.now(UTC).isoformat()

        time.sleep(polite_delay)

    if not dry_run:
        _save_state(state, output_dir)

    click.echo(
        f"\nSummary: checked={stats['checked']}, unchanged={stats['unchanged']}, "
        f"changed={stats['changed']}, new={stats['new']}, errors={stats['errors']}"
    )
    if not dry_run and (stats["changed"] + stats["new"]) > 0:
        click.echo(
            "\nRe-downloaded one or more documents. Run "
            "`uv run python -m hacienda_gpt.cli.processor --incremental "
            "--max-tokens 512 --num-workers 8` to fold the changes into FAISS."
        )


if __name__ == "__main__":
    cli()
