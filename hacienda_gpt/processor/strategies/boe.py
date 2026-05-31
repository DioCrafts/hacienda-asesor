"""BOE consolidado strategy — splits giant HTMLs by structural blocks +
sub-tables before letting Docling chunk each piece.

Why this strategy exists
------------------------

BOE consolidados (`<title>BOE-A-YYYY-NNNNN ...</title>`) are HTML pages
that on the surface look like other BOE entries — but a tiny minority
(observed: < 1 % of the corpus) carry **giant value tables** with 50 k +
``<tr>`` rows: vehicle price tables for tax assessment, listed-securities
quotes, etc. Docling's ``HybridChunker`` chokes on these because
``_count_chunk_tokens`` re-tokenises the growing serialized window in a
loop, giving effectively O(N²) over the table.

What this strategy does
-----------------------

For files matching the BOE consolidado shape **and** exceeding a size
threshold, we **never feed the whole file to Docling**. Instead we:

1. Stream the HTML with ``lxml.etree.iterparse`` — O(N) memory, never
   loads the whole 43 MB into a DOM tree.
2. Walk to the document-body containers (BOE consolidados use
   ``<div class="bloque">``; about 13 per file).
3. Inside each block, identify any ``<table>`` with more than
   :data:`MAX_ROWS_PER_TABLE` rows and split it into row-bounded
   sub-tables, preserving ``<thead>`` so each fragment is independently
   intelligible.
4. Serialise each block / sub-table to a small temp HTML file (~tens of
   KB) and run the **default** Docling+HybridChunker on those fragments.
5. Re-stamp every emitted ``Document`` so ``source_file`` and ``chunk_id``
   point at the **parent** path, not the temp fragment — citations, the
   manifest, and FAISS deletion-by-id all stay coherent with the rest of
   the index.

Why probe is conservative
-------------------------

The probe needs to be both cheap AND specific: a false positive (claiming
a non-BOE file) would route it through the splitter, which would emit
nothing useful. A false negative (not claiming a BOE file that needed
splitting) just falls through to the default Docling path — which we
know hangs. We bias toward the false-negative-safe side: require BOTH
the file extension/size signal AND a peek that finds the canonical
``<title>BOE-A-...`` marker in the first 4 KB.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

from hacienda_gpt.processor.strategies.base import IngestStrategy

logger = logging.getLogger(__name__)

#: Files smaller than this are handled by the default Docling path —
#: HybridChunker scales fine on small docs and we don't want to pay the
#: split overhead for them. 4 MB picked because (a) the smallest stuck
#: file in production was 30 MB, (b) the next-largest indexed file in
#: the corpus was ~3 MB.
SIZE_THRESHOLD_BYTES = 4 * 1024 * 1024

#: Maximum ``<tr>`` rows per sub-table. Each sub-table becomes its own
#: temp HTML fed to Docling. 500 rows × ~10 cols = 5 000 cells, which
#: HybridChunker handles in seconds. Larger values regress to the
#: O(N²) regime; smaller values multiply the temp-file overhead.
MAX_ROWS_PER_TABLE = 500

#: Probe peek size — large enough to find ``<title>BOE-A-...`` in the
#: HTML head; we do NOT require finding ``<div class="bloque">`` here
#: because in real BOE consolidados that marker sits ~38 KB into the
#: file (after the heavy CSS / link preamble) and we don't want to read
#: 64 KB on every probe. If the structure marker turns out to be absent
#: at parse time, the strategy falls through to the default Docling
#: path — same as if the file failed to claim.
_PROBE_PEEK_BYTES = 8 * 1024

_BOE_TITLE_RE = re.compile(rb"<title>\s*BOE-A-\d{4}-\d+\b", re.IGNORECASE)


class BoeConsolidadoTableStrategy(IngestStrategy):
    """Splits giant BOE consolidados by structural blocks + sub-tables."""

    strategy_id = "boe-consolidado-table-v1"

    def probe(self, file_path: str) -> bool:
        """Cheap two-stage probe: size first, then content sniff.

        Both gates must pass — keeps us conservative (false-negatives
        fall through to the default path; false-positives would emit
        nothing useful for non-BOE files).
        """
        try:
            stat = os.stat(file_path)
        except OSError:
            return False
        if stat.st_size <= SIZE_THRESHOLD_BYTES:
            return False
        if not file_path.lower().endswith((".html", ".htm")):
            return False
        # Peek at the first few KB; bail on any I/O issue without raising.
        try:
            with open(file_path, "rb") as fh:
                head = fh.read(_PROBE_PEEK_BYTES)
        except OSError:
            return False
        return bool(_BOE_TITLE_RE.search(head))

    def parse(self, file_path: str) -> Sequence[Document]:
        from lxml import etree, html as lxml_html

        from hacienda_gpt.processor.strategies.default import (
            _stable_chunk_id_for,
            docling_parse_files,
        )

        logger.info(
            "BOE consolidado strategy splitting %s (%.1f MB)",
            file_path,
            os.path.getsize(file_path) / (1024 * 1024),
        )

        # Streaming parse of <div class="bloque"> elements only — never
        # builds the full DOM of the 43 MB file. iterparse calls
        # ``elem.clear()`` after we emit each block so memory stays O(1)
        # in the number of blocks rather than O(N) in file size.
        bloque_html_fragments: list[str] = []
        try:
            for _event, elem in etree.iterparse(
                file_path,
                events=("end",),
                tag="div",
                recover=True,
                huge_tree=True,
            ):
                klass = elem.get("class") or ""
                if "bloque" in klass.split():
                    # Serialise this block in isolation.
                    bloque_html_fragments.append(
                        lxml_html.tostring(elem, encoding="unicode", method="html")
                    )
                # Free the parsed subtree regardless of whether we kept
                # it — without this, iterparse retains the whole tree.
                elem.clear()
                # Drop preceding siblings too: see lxml's iterparse docs
                # for the canonical streaming idiom.
                while elem.getprevious() is not None:
                    del elem.getparent()[0]
        except etree.XMLSyntaxError as exc:
            logger.warning(
                "BOE split: lxml could not stream-parse %s (%s) — "
                "falling back to default Docling path for this file.",
                file_path,
                exc,
            )
            from hacienda_gpt.processor.strategies.default import parse_with_default

            return parse_with_default(file_path)

        if not bloque_html_fragments:
            logger.warning(
                "BOE split: no <div class='bloque'> found in %s — "
                "falling back to default Docling path.",
                file_path,
            )
            from hacienda_gpt.processor.strategies.default import parse_with_default

            return parse_with_default(file_path)

        logger.info(
            "BOE split: %s yielded %d structural block(s)",
            file_path,
            len(bloque_html_fragments),
        )

        # Inside each block, split big tables. Output: list of small
        # HTML fragments that Docling can chew in seconds.
        sub_fragments: list[str] = []
        for block_html in bloque_html_fragments:
            sub_fragments.extend(_split_block_tables(block_html))

        logger.info(
            "BOE split: produced %d sub-fragment(s) for %s",
            len(sub_fragments),
            file_path,
        )

        # Materialise sub-fragments as temp HTML files. Docling's loader
        # accepts file paths; bytes-IO would also work but the loader's
        # `dl_meta.origin.filename` field plays nicer with a real path.
        parent_basename = Path(file_path).stem
        parent_hash = hashlib.sha256(file_path.encode("utf-8")).hexdigest()[:12]
        # Per-PID subdirectory keeps concurrent workers isolated. Cleaned
        # up in the finally block below.
        tmp_dir = Path("/tmp") / "hacienda-boe-split" / f"{os.getpid()}-{parent_hash}"
        tmp_dir.mkdir(parents=True, exist_ok=True)

        temp_paths: list[str] = []
        try:
            for i, sub_html in enumerate(sub_fragments):
                sub_path = tmp_dir / f"{parent_basename}__{i:05d}.html"
                sub_path.write_text(_wrap_in_minimal_html(sub_html), encoding="utf-8")
                temp_paths.append(str(sub_path))

            # Run Docling+HybridChunker on the small fragments. Each is
            # tens of KB; HybridChunker handles them in seconds.
            sub_chunks = docling_parse_files(temp_paths)

            # Re-stamp metadata so chunks point at the **parent** file,
            # not the temp fragment. ``source_file`` drives FAISS
            # deletion-by-id when the parent is modified, and
            # ``chunk_id`` must be deterministic per (parent, ordinal)
            # so re-ingesting the parent produces idempotent IDs.
            output: list[Document] = []
            for idx, chunk in enumerate(sub_chunks):
                meta = dict(chunk.metadata or {})
                meta["source_file"] = file_path
                meta["chunk_id"] = _stable_chunk_id_for(file_path, idx)
                # Annotate which strategy emitted this chunk — useful for
                # diagnostics and for the manifest's per-file strategy_id.
                meta.setdefault("_strategy_id", self.strategy_id)
                output.append(Document(page_content=chunk.page_content, metadata=meta))
            return output
        finally:
            # Best-effort cleanup. If we crash before cleanup, /tmp is
            # cleared by the OS on reboot.
            for p in temp_paths:
                try:
                    os.unlink(p)
                except OSError:
                    pass
            try:
                tmp_dir.rmdir()
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _split_block_tables(block_html: str) -> list[str]:
    """Given the HTML of a ``<div class="bloque">``, return sub-HTML pieces
    where each piece's tables have at most :data:`MAX_ROWS_PER_TABLE` rows.

    Blocks without big tables are returned unchanged (single element in
    the output list). Blocks with one or more big tables are split into:

    * The block's leading content (everything before the first big table).
    * One fragment per ``MAX_ROWS_PER_TABLE`` chunk of each big table,
      each carrying the original ``<thead>`` so the sub-table is
      self-describing.
    * The block's trailing content (after the last big table).

    Implementation uses lxml DOM ops over **just the block's HTML** —
    safe because a block is at most a few MB even in pathological files.
    """
    from lxml import etree, html as lxml_html

    try:
        root = lxml_html.fragment_fromstring(block_html, create_parent="div")
    except etree.ParserError as exc:
        logger.warning("Could not re-parse a BOE block (%s); emitting as-is.", exc)
        return [block_html]

    big_tables = [
        t for t in root.iter("table") if _count_rows(t) > MAX_ROWS_PER_TABLE
    ]
    if not big_tables:
        # Block is small enough to feed Docling whole.
        return [block_html]

    fragments: list[str] = []
    for table in big_tables:
        # Promote the table to its own sub-fragment(s). We emit each
        # sub-table as a standalone HTML block; the surrounding block
        # text/structure is captured by the block-level emission below.
        fragments.extend(_split_table(table))
        # Replace the big table in the block with a placeholder comment
        # so the block-level emission doesn't include the gigantic data
        # twice. We use a comment node (lxml strips it during serialise
        # if we use method='text', but with 'html' it's preserved as a
        # marker — that's fine, Docling ignores it).
        comment = etree.Comment(f" big-table-split: {_count_rows(table)} rows ")
        table.addprevious(comment)
        table.getparent().remove(table)

    # Emit the residual block (now without the big tables) as one more
    # fragment. Skip it if the block now contains only whitespace.
    residual = lxml_html.tostring(root, encoding="unicode", method="html")
    if re.search(r"\S", _strip_tags(residual)):
        fragments.append(residual)

    return fragments


def _split_table(table) -> list[str]:
    """Split a single big ``<table>`` element into sub-tables of at most
    :data:`MAX_ROWS_PER_TABLE` rows each, each wrapped in a fresh
    ``<table>`` carrying the original ``<thead>`` so the sub-table is
    self-describing."""
    from lxml import etree, html as lxml_html

    thead = table.find(".//thead")
    rows = table.findall(".//tbody/tr") or [
        r for r in table.findall(".//tr") if not _is_in_thead(r)
    ]

    if not rows:
        return [lxml_html.tostring(table, encoding="unicode", method="html")]

    fragments: list[str] = []
    total = len(rows)
    for chunk_start in range(0, total, MAX_ROWS_PER_TABLE):
        chunk_end = min(chunk_start + MAX_ROWS_PER_TABLE, total)
        sub_rows = rows[chunk_start:chunk_end]

        # Build a fresh <table> with thead + this row range. We attach
        # an annotation header so the chunk text mentions "rows X-Y of Z"
        # and Docling-produced chunks can be cross-referenced.
        new_table = etree.Element("table")
        if thead is not None:
            # Deep-copy thead so each fragment owns its own.
            new_table.append(_deepcopy(thead))
        tbody = etree.SubElement(new_table, "tbody")
        for r in sub_rows:
            tbody.append(_deepcopy(r))

        annotation = etree.Element("p")
        annotation.text = (
            f"[Tabla parcial: filas {chunk_start + 1}-{chunk_end} de {total}]"
        )

        wrapper = etree.Element("div")
        wrapper.append(annotation)
        wrapper.append(new_table)
        fragments.append(
            lxml_html.tostring(wrapper, encoding="unicode", method="html")
        )
    return fragments


def _wrap_in_minimal_html(body_fragment: str) -> str:
    """Wrap a fragment in just enough HTML for Docling to parse it as a
    full document — without bringing in BOE's heavy CSS/JS preamble,
    which Docling doesn't need."""
    return (
        '<!DOCTYPE html><html lang="es"><head>'
        '<meta charset="utf-8"/></head><body>'
        f"{body_fragment}"
        "</body></html>"
    )


def _count_rows(table) -> int:
    return sum(1 for _ in table.iter("tr"))


def _is_in_thead(row) -> bool:
    parent = row.getparent()
    while parent is not None:
        if parent.tag == "thead":
            return True
        parent = parent.getparent()
    return False


def _deepcopy(element):
    """Deep-copy an lxml element. ``copy.deepcopy`` doesn't preserve
    namespaces in some lxml builds — using ``fromstring(tostring(...))``
    is the canonical idiom."""
    from lxml import etree, html as lxml_html

    return lxml_html.fragment_fromstring(
        lxml_html.tostring(element, encoding="unicode", method="html"),
        create_parent=False,
    ) if isinstance(element, etree._Element) else element


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html)


__all__: list[Any] = ["BoeConsolidadoTableStrategy", "SIZE_THRESHOLD_BYTES", "MAX_ROWS_PER_TABLE"]
