"""Source-aware ingestion strategies — protocol + base types.

The motivation: Docling's ``HybridChunker`` is great at tokenizer-aware
splitting but degrades catastrophically (effective O(N²) over the serialized
text — see ``hybrid_chunker.py:172-213``) on documents with **giant tables**
like the BOE consolidados that ship 73 k+ ``<tr>`` rows in a single table.
On the cold-start production ingest we observed two workers stuck on such
files for 5 + hours with no observable progress.

Rather than special-casing that one pathology in the generic loader, we
introduce a **strategy registry**: each source — BOE consolidados, AEAT PDF
manuals, JOUE XML, TS rulings, future bulletins — can claim files via a
cheap ``probe`` and own the chunking via ``parse``. The default strategy
keeps the current Docling + HybridChunker path; specialised strategies plug
in without touching ``document_loader.py``.

This pattern mirrors what Unstructured.io does with ``partition_*`` and
LlamaIndex with ``BaseReader`` / ``SimpleDirectoryReader.file_extractor``:
the orchestrator just iterates files; "how to parse this file" is the
strategy's responsibility.

Stability contract for chunks returned by a strategy
----------------------------------------------------

Every ``Document`` a strategy emits **must** carry:

* ``metadata["source_file"]`` — absolute path of the **parent** file. If the
  strategy internally splits the file into temp fragments, those temp paths
  never leak; the chunk always points back at the original source so the
  manifest, FAISS deletion-by-id, and citation rendering all stay coherent.
* ``metadata["chunk_id"]`` — globally unique, deterministic identifier.
  Stamped by the strategy itself; the loader will stamp a fallback if
  missing but strategies should set it explicitly to control collisions.
* ``metadata["title"]`` / ``metadata["source_url"]`` / ``metadata["section"]``
  — the citation-facing fields the grounding gate reads.

The pipeline fingerprint records which strategy handled each file (see
``FileEntry.strategy_id`` in ``manifest.py``) so changes to a strategy's
implementation invalidate only the files it claims, not the whole index.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from langchain_core.documents import Document


class IngestStrategy(ABC):
    """How to turn a file path into citation-ready ``Document`` chunks.

    Implementations are stateless: workers may share a single instance
    across files. Any per-process resource (tokenizer, chunker) should be
    owned by the strategy and lazily initialised in a thread-safe way.
    """

    #: Stable identifier persisted in the manifest. Bump the suffix
    #: (e.g. ``"boe-consolidado-table-v1"`` → ``"...-v2"``) when changing
    #: the strategy's behaviour, so the manifest layer treats already-
    #: indexed files as "modified" and re-ingests them with the new code.
    strategy_id: str

    @abstractmethod
    def probe(self, file_path: str) -> bool:
        """Return True if this strategy should claim ``file_path``.

        Called in registration order during file routing — **first match
        wins**, so register most-specific strategies before generic ones.
        Probe must be cheap: ``os.stat`` + reading at most the first ~4 KB
        of the file. No tokenisation, no full parsing, no network.
        """

    @abstractmethod
    def parse(self, file_path: str) -> Sequence[Document]:
        """Return the chunks for ``file_path``.

        Every returned chunk must carry the metadata contract documented
        at the module level. If parsing fails the strategy may return an
        empty list (preferable) or raise; raising aborts the whole worker
        batch, so prefer empty-with-warning over an exception unless the
        failure is non-recoverable.
        """
