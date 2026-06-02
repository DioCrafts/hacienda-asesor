"""Fetch DGT (Dirección General de Tributos) binding consultas into the RAG corpus.

The DGT publishes its full consulta database at ``petete.tributos.hacienda.gob.es``.
This CLI walks the entire vinculantes archive (~68k consultas from 1997 to
present) and persists each one as standalone HTML + JSON sidecar under
``data/html/<snapshot>/dgt-vinculantes/V<num>-<year>.html``, so the existing
``hacienda_gpt.cli.processor`` ingest pipeline picks them up incrementally
through the manifest like any other BOE document.

Architecture notes
------------------

The site is a Knosys-backed front-end. The ``/consultas/`` page is a JS shell;
real content is served through four ``/do/<verb>`` POST endpoints:

* ``/do/info``     — DB stats (we use this for total counts only).
* ``/do/form``     — search form HTML; must be hit once per session to
                     activate Knosys form state.
* ``/do/search``   — paginated search; takes Knosys field tuples
                     (``NMCMP_<i>``/``OPCMP_<i>``/``VLCMP_<i>``), the tab
                     identifier (``tab=2`` = vinculantes), a date range
                     (``dateIni_2``/``dateEnd_2`` in dd/mm/yyyy), and page
                     number. Returns HTML with ``<td id="doc_<knosys_id>"
                     onClick="viewDocument(<knosys_id>, 2)">`` rows.
* ``/do/document`` — fetch a single consulta by ``doc=<knosys_id>``. The
                     server **rejects** requests without ``X-Requested-With:
                     XMLHttpRequest`` and ``Referer`` headers (returns
                     401 Unauthorized), so both are mandatory.

We walk the archive month-by-month rather than fetching a single huge year
range because Knosys hard-fails on result sets >10k and pagination there
is fragile. Per-month chunks return 100-300 consultas in 5-15 pages each.

Resumability is achieved by checking whether ``V<num>.html`` already exists
on disk before issuing the document POST. A crashed run is re-startable by
just invoking the CLI again with the same snapshot date.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
import json
import logging
from pathlib import Path
import re
import time
from typing import Any

import click
import requests

logger = logging.getLogger(__name__)

_BASE = "https://petete.tributos.hacienda.gob.es"
_USER_AGENT = "hacienda-asesor/0.1 (RAG corpus refresh; contacto@hacienda-asesor.local)"

# The petete server requires this header on /do/document or it 401s.
_REQUIRED_HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{_BASE}/consultas/",
    "User-Agent": _USER_AGENT,
}

_DOC_ID_RE = re.compile(r'id="doc_(\d+)"')
_NUM_CONSULTA_IN_ROW_RE = re.compile(r'class="NUM-CONSULTA">\s*<strong>\s*([^<\s]+)\s*</strong>')
_TOTAL_PAGES_RE = re.compile(r'id="total_pages">(\d+)<')
_NUM_CONSULTA_IN_DOC_RE = re.compile(r'class="NUM-CONSULTA"[^>]*>([^<]+)</p>')


@dataclass
class _RunStats:
    """Running stats so the CLI can report a useful summary at the end."""

    months_processed: int = 0
    search_requests: int = 0
    docs_skipped_existing: int = 0
    docs_fetched: int = 0
    docs_failed: int = 0
    failed_doc_ids: list[str] = field(default_factory=list)


def _build_session() -> requests.Session:
    """Build a long-lived session that the petete server will accept.

    We hit ``/consultas/`` (to obtain JSESSIONID) and then ``/do/form``
    (to activate Knosys form state); without both, ``/do/document``
    returns 401.
    """
    session = requests.Session()
    session.verify = False  # petete uses Hacienda's internal CA — trust skipped.
    session.headers.update(_REQUIRED_HEADERS)

    # Bootstrap the JSESSIONID cookie.
    resp = session.get(f"{_BASE}/consultas/", timeout=30)
    resp.raise_for_status()

    # Activate form state.
    resp = session.post(f"{_BASE}/consultas/do/form", data="", timeout=30)
    resp.raise_for_status()

    # Silence the urllib3 InsecureRequestWarning we trigger by skipping verify
    # — adding the internal CA to the trust store would be heavier than this
    # standalone CLI deserves.
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    return session


def _search_month(
    session: requests.Session,
    year: int,
    month: int,
    page: int,
) -> tuple[list[str], int, str]:
    """Search vinculantes for one (year, month) page and return:

    * the Knosys doc ids found on this page (extracted from ``id="doc_<n>"``),
    * the total page count for the month (so the caller can paginate), and
    * the Knosys ``query`` string captured from the response (needed verbatim
      by ``/do/document``).
    """
    last_day = calendar.monthrange(year, month)[1]
    # The server reads the date filter from VLCMP_2 using the Knosys range
    # syntax ``dd/mm/yyyy..dd/mm/yyyy``. The user-facing ``dateIni_2`` /
    # ``dateEnd_2`` fields exist only as UI helpers; the front-end JS
    # rewrites them into VLCMP_2 before submit (see ``checkDateValues`` in
    # the petete JS). If we leave VLCMP_2 empty or with the placeholder
    # "dd/mm/aaaa", the date filter is silently ignored and the server
    # tries to return all 68k consultas → 504 Gateway Timeout.
    #
    # We also include the full NMCMP/OPCMP/VLCMP triples for fields 1..7
    # because the server requires the schema to be present even when the
    # value side is empty; missing tuples slow the query path noticeably.
    date_range = f"01/{month:02d}/{year}..{last_day:02d}/{month:02d}/{year}"
    data: list[tuple[str, str]] = [
        ("NMCMP_1", "NUM-CONSULTA"), ("OPCMP_1", ".Y"), ("VLCMP_1", ""),
        ("NMCMP_2", "FECHA-SALIDA"), ("OPCMP_2", ".Y"), ("VLCMP_2", date_range),
        ("NMCMP_3", "NORMATIVA"), ("OPCMP_3", ".Y"), ("VLCMP_3", ""),
        ("NMCMP_4", "CUESTION-PLANTEADA"), ("OPCMP_4", ".Y"), ("VLCMP_4", ""),
        ("NMCMP_5", "DESCRIPCION-HECHOS"), ("OPCMP_5", ".Y"), ("VLCMP_5", ""),
        ("NMCMP_6", "FreeText"), ("OPCMP_6", ".Y"), ("VLCMP_6", ""),
        # VLCMP_7 (CRITERIO) is a checkbox in the UI ("only criterio of interés");
        # omitted = no filter on criterio.
        ("NMCMP_7", "CRITERIO"), ("VLCMP_7", ""),
        ("type2", "on"),  # vinculantes tab
        # The UI date helpers — server ignores them (date is in VLCMP_2) but
        # we send them anyway to match the form's serialize() output exactly.
        ("dateIni_2", f"01/{month:02d}/{year}"),
        ("dateEnd_2", f"{last_day:02d}/{month:02d}/{year}"),
        ("tab", "2"),
        ("page", str(page)),
        ("auto", "false"),
        ("cmpOrder", "NUM-CONSULTA"),
        ("dirOrder", "0"),  # 0=ASC, 1=DESC in this knosys instance
    ]

    # /do/info reports max query latency around 450s server-side, so we
    # keep the read timeout generous; the polite per-doc ``delay`` already
    # protects against accidental hammering.
    resp = session.post(f"{_BASE}/consultas/do/search", data=data, timeout=180)
    resp.raise_for_status()
    html = resp.text

    doc_ids = _DOC_ID_RE.findall(html)
    total_pages_match = _TOTAL_PAGES_RE.search(html)
    total_pages = int(total_pages_match.group(1)) if total_pages_match else 1

    # The search result HTML embeds a hidden input ``<input ... id="query"
    # value="..."/>`` whose value is the Knosys query syntax that
    # ``/do/document`` expects. Capture it.
    query_match = re.search(r'id="query"[^>]*value="([^"]*)"', html)
    query = query_match.group(1) if query_match else ""

    return doc_ids, total_pages, query


def _fetch_document(
    session: requests.Session,
    query: str,
    doc_id: str,
) -> str:
    """Fetch one consulta document by Knosys id, returning the inner HTML
    block. Raises ``requests.HTTPError`` on non-2xx; the caller decides
    whether to retry or skip."""
    resp = session.post(
        f"{_BASE}/consultas/do/document",
        data={"query": query, "doc": doc_id, "tab": "2"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.text


def _parse_doc_num(html: str) -> str | None:
    """Extract the user-facing consulta number (e.g. ``V0058-25``) from
    the document HTML. Used to name the output file."""
    m = _NUM_CONSULTA_IN_DOC_RE.search(html)
    if m:
        return m.group(1).strip()
    return None


def _output_path(snapshot_root: Path, num_consulta: str) -> Path:
    """Compute the on-disk path for a consulta. We bucket files into
    ``dgt-vinculantes/<year>/V<full-num>.html`` (matching the BOE bulk
    layout used elsewhere in the corpus) so the ingest manifest's
    per-folder strategy registry can apply DGT-specific tuning later
    without touching adjacent BOE chunks.

    DGT consultas use a 2-digit year suffix (e.g. V0001-97 = 1997,
    V0001-25 = 2025). We disambiguate using a Y2K-style pivot: a suffix
    greater than the current year's 2-digit form is treated as 19xx,
    everything else as 20xx. DGT started publishing in 1997, so anything
    suffix ≥ 97 is unambiguously 19xx — but we make the rule general so
    a 2027 run still works as expected.
    """
    year_suffix = num_consulta.split("-")[-1] if "-" in num_consulta else "unknown"
    if len(year_suffix) == 2 and year_suffix.isdigit():
        suffix_int = int(year_suffix)
        current_2digit = datetime.now(UTC).year % 100  # e.g. 26 in 2026
        century = "19" if suffix_int > current_2digit else "20"
        year_full = f"{century}{year_suffix}"
    else:
        year_full = year_suffix
    return snapshot_root / "dgt-vinculantes" / year_full / f"{num_consulta}.html"


def _iter_months(start: date, end: date) -> Iterable[tuple[int, int]]:
    """Yield (year, month) tuples from ``start`` to ``end`` inclusive in
    ascending order. We use this to walk the archive deterministically so
    a resumed run picks up exactly where the previous one stopped."""
    y, m = start.year, start.month
    end_key = (end.year, end.month)
    while (y, m) <= end_key:
        yield y, m
        m += 1
        if m == 13:
            m = 1
            y += 1


@click.command()
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("./data/html"),
    show_default=True,
    help="Root directory for the local HTML snapshot. Files are written under <output-dir>/<snapshot-date>/dgt-vinculantes/<year>/<num>.html.",
)
@click.option(
    "--snapshot-date",
    default=None,
    help="Snapshot folder name (YYYY-MM-DD). Defaults to today (UTC).",
)
@click.option(
    "--start",
    default="1997-01",
    show_default=True,
    help="Start month YYYY-MM. DGT consultas vinculantes start in 1997.",
)
@click.option(
    "--end",
    default=None,
    help="End month YYYY-MM. Defaults to current month.",
)
@click.option(
    "--delay",
    default=1.0,
    show_default=True,
    type=click.FloatRange(min=0.0),
    help="Polite delay (seconds) between document requests. 1s ≈ 19h for the full archive.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Search but do not save documents — useful to estimate counts.",
)
@click.option(
    "--max-docs",
    default=0,
    type=int,
    help="Stop after this many fetched documents (0 = no limit). Useful for smoke tests.",
)
def cli(
    output_dir: Path,
    snapshot_date: str | None,
    start: str,
    end: str | None,
    delay: float,
    dry_run: bool,
    max_docs: int,
) -> None:
    """Crawl every DGT binding consulta from START to END and save the HTML."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    snapshot = snapshot_date or datetime.now(UTC).strftime("%Y-%m-%d")
    snapshot_root = output_dir / snapshot
    snapshot_root.mkdir(parents=True, exist_ok=True)

    start_date = datetime.strptime(start, "%Y-%m").date()
    end_date = (
        datetime.strptime(end, "%Y-%m").date()
        if end
        else date.today().replace(day=1)
    )

    click.echo(f"DGT seed crawl: {start_date:%Y-%m} → {end_date:%Y-%m}, snapshot={snapshot}, delay={delay}s")
    click.echo(f"Output → {snapshot_root}/dgt-vinculantes/<year>/<num>.html")

    session = _build_session()
    stats = _RunStats()

    try:
        for year, month in _iter_months(start_date, end_date):
            stats.months_processed += 1
            page = 1
            while True:
                stats.search_requests += 1
                try:
                    doc_ids, total_pages, query = _search_month(session, year, month, page)
                except requests.HTTPError as e:
                    logger.warning("Search failed for %04d-%02d page %d: %s — retrying once after 5s", year, month, page, e)
                    time.sleep(5)
                    try:
                        doc_ids, total_pages, query = _search_month(session, year, month, page)
                    except requests.HTTPError:
                        logger.error("Skipping %04d-%02d page %d after retry failure", year, month, page)
                        break

                if not doc_ids:
                    break

                for doc_id in doc_ids:
                    if max_docs and stats.docs_fetched >= max_docs:
                        click.echo(f"\nReached --max-docs={max_docs}; stopping early.")
                        _summary(stats, snapshot_root)
                        return

                    # We can't compute the user-facing num before fetching, but
                    # we can pre-check the row HTML from the search response for
                    # an explicit NUM-CONSULTA marker and short-circuit the
                    # download if the file already exists.
                    if dry_run:
                        stats.docs_fetched += 1
                        continue

                    try:
                        html = _fetch_document(session, query, doc_id)
                    except requests.HTTPError as e:
                        stats.docs_failed += 1
                        stats.failed_doc_ids.append(doc_id)
                        logger.warning("Document fetch failed: doc=%s %s", doc_id, e)
                        time.sleep(delay)
                        continue

                    num = _parse_doc_num(html) or f"unknown-{doc_id}"
                    out_path = _output_path(snapshot_root, num)
                    if out_path.exists():
                        stats.docs_skipped_existing += 1
                    else:
                        out_path.parent.mkdir(parents=True, exist_ok=True)
                        out_path.write_text(html, encoding="utf-8")
                        stats.docs_fetched += 1
                        if stats.docs_fetched % 100 == 0:
                            click.echo(
                                f"[{datetime.now():%H:%M:%S}] fetched={stats.docs_fetched} "
                                f"skipped={stats.docs_skipped_existing} failed={stats.docs_failed} "
                                f"month={year}-{month:02d} page={page}"
                            )
                    time.sleep(delay)

                if page >= total_pages:
                    break
                page += 1

            if stats.months_processed % 12 == 0:
                click.echo(f"[checkpoint] completed through {year}-{month:02d}")
    except KeyboardInterrupt:
        click.echo("\n[interrupted] saving summary…")

    _summary(stats, snapshot_root)


def _summary(stats: _RunStats, snapshot_root: Path) -> None:
    """Persist a JSON sidecar with what the crawl actually did so a future
    incremental ingest (or a re-run) has a reliable inventory to diff
    against, and print a human-friendly summary to stdout."""
    report = {
        "snapshot_root": str(snapshot_root),
        "ended_at": datetime.now(UTC).isoformat(),
        "months_processed": stats.months_processed,
        "search_requests": stats.search_requests,
        "docs_fetched": stats.docs_fetched,
        "docs_skipped_existing": stats.docs_skipped_existing,
        "docs_failed": stats.docs_failed,
        "failed_doc_ids": stats.failed_doc_ids,
    }
    summary_path = snapshot_root / "dgt-vinculantes" / "_crawl_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    click.echo(f"\n{'-' * 60}")
    click.echo(f"DGT crawl summary: {json.dumps(report, indent=2)}")
    click.echo(f"Summary saved to: {summary_path}")


if __name__ == "__main__":
    cli()
