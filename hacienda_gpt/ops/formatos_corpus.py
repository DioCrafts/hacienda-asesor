"""Format watch for the corpus conversion (Docling), and for the BOE's autonomic codes in particular.

The ingest (:mod:`hacienda_gpt.processor.document_loader`) records each file in
its manifest with however many chunks Docling produced, zero included: a PDF
that downloaded fine but has no text layer, or whose layout Docling no longer
understands, is indexed as "nothing" and nobody is told. :class:`RecuentoCorpus`
counts what each file yielded during one ingest run and reports it to the
format watch once, at the end:

* ``corpus_docling``: every file of the run (counters only, no content);
* ``boe_ccaa_texto``: the consolidated autonomic codes the ``boe-ccaa``
  crawler downloads (``.../boe_ccaa/<ccaa>/<código>.pdf``): text extracted,
  article or disposition headings found, and characters per KB of PDF.

Nothing here may break the ingest: every entry point swallows its own errors.
Kept free of heavy imports (the loader imports it in the parent process).
"""

from __future__ import annotations

from collections.abc import Sequence
import json
import logging
import os
from pathlib import Path
import re
import statistics
from typing import Any

logger = logging.getLogger(__name__)

CARPETA_CCAA = "boe_ccaa"
# A heading of the BOE's codes as Docling serialises it: a line that starts with "Artículo N" or
# "Disposición adicional/transitoria/derogatoria/final" (Markdown "#" marks allowed).
ENCABEZADO = re.compile(
    r"^\s*(?:#+\s*)?(?:Art[ií]culo\s+(?:\d+|[úu]nico)|Disposici[oó]n\s+(?:adicional|transitoria|derogatoria|final))",
    re.MULTILINE,
)
MAX_MUESTRA_FICHERO = 1500


def _texto(chunk: Any) -> str:
    return str(getattr(chunk, "page_content", "") or "")


def _encabezados(chunk: Any) -> str:
    metadata = getattr(chunk, "metadata", None) or {}
    return "\n".join(str(metadata.get(clave) or "") for clave in ("section", "title"))


class RecuentoCorpus:
    """Counters of one ingest run, reported to the format watch by :meth:`informar`."""

    def __init__(self, raiz: str | Path | None = None) -> None:
        self.raiz = Path(raiz) if raiz is not None else None
        self.ficheros = 0
        self.fragmentos = 0
        self.sin_fragmentos: list[str] = []
        self.codigos: list[dict[str, Any]] = []
        self._muestras: dict[str, str] = {}

    def _nombre(self, file_path: str) -> str:
        path = Path(file_path)
        if self.raiz is not None:
            try:
                return str(path.relative_to(self.raiz))
            except ValueError:
                pass
        return path.name

    def anotar(self, file_path: str, chunks: Sequence[Any]) -> None:
        """One file converted. Never raises."""
        try:
            self._anotar(file_path, chunks)
        except Exception:
            logger.warning("Format watch: could not count %s", file_path, exc_info=True)

    def _anotar(self, file_path: str, chunks: Sequence[Any]) -> None:
        nombre = self._nombre(file_path)
        self.ficheros += 1
        self.fragmentos += len(chunks)
        if not chunks:
            self.sin_fragmentos.append(nombre)
        path = Path(file_path)
        if CARPETA_CCAA not in path.parts or path.suffix.lower() != ".pdf":
            return
        texto = "\n".join(_texto(chunk) for chunk in chunks).strip()
        kb = os.path.getsize(file_path) / 1024 if path.exists() else 0.0
        con_encabezado = sum(
            1 for chunk in chunks if ENCABEZADO.search(_texto(chunk)) or ENCABEZADO.search(_encabezados(chunk))
        )
        self.codigos.append(
            {
                "fichero": nombre,
                "ccaa": path.parent.name,
                "fragmentos": len(chunks),
                "caracteres": len(texto),
                "kb": round(kb, 1),
                "con_encabezado": con_encabezado,
            }
        )
        self._muestras[nombre] = texto[:MAX_MUESTRA_FICHERO]

    def informar(self, contexto: str = "") -> None:
        """Report the run (one observation per source). Never raises; does nothing for an empty run."""
        try:
            from hacienda_gpt.ops import formatos

            if self.ficheros:
                formatos.observar(
                    "corpus_docling",
                    None,
                    extraccion={
                        "ficheros": self.ficheros,
                        "fragmentos": self.fragmentos,
                        "sin_fragmentos": self.sin_fragmentos[:50],
                        "n_sin_fragmentos": len(self.sin_fragmentos),
                    },
                    contexto=contexto,
                )
            if self.codigos:
                # The sample is the start of each code's text (public law), for the developer report.
                formatos.observar(
                    "boe_ccaa_texto",
                    json.dumps(self._muestras, ensure_ascii=False, indent=1),
                    extraccion=self.codigos,
                    contexto=contexto,
                )
        except Exception:
            logger.warning("Format watch: could not report the ingest run", exc_info=True)


def rendimiento(codigos: Sequence[dict[str, Any]]) -> float:
    """Median characters of text per KB of PDF among the codes that have size (0 when none)."""
    ratios = [c["caracteres"] / c["kb"] for c in codigos if c.get("kb")]
    return float(statistics.median(ratios)) if ratios else 0.0
