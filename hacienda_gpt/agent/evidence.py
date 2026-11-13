"""Evidence ledger: every passage the agent retrieved, under a citable id.

The agent may only assert tax law that it can point at. Each retrieved chunk is
registered here as ``F1``, ``F2``… and the answer cites those ids inline
(``[F3]``). Because ids are minted by us — never by the model — a citation can
be checked mechanically: an id that is not in the ledger is a fabricated source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
import threading
from typing import Any

# [F3], [F3, F7], [F3][F7] — tolerant about spacing and separators.
_CITATION_GROUP_RE = re.compile(r"\[\s*(F\d+(?:\s*[,;y]\s*F\d+)*)\s*\]", re.IGNORECASE)
_FRAGMENT_ID_RE = re.compile(r"F\d+", re.IGNORECASE)

# source_authority (crawler sidecar) → coarse source category the agent filters by.
SOURCE_TYPES = ("normativa", "jurisprudencia", "doctrina_administrativa", "aeat")

# Path / URL hints for documents ingested without a sidecar (AEAT pages, BOE
# seeds, DGT consultas). Order matters: the TC rulings live under a BOE subdir.
_AUTHORITY_HINTS: tuple[tuple[str, str], ...] = (
    ("jurisprudencia-tc", "TC"),
    ("tribunalconstitucional", "TC"),
    ("poderjudicial", "TS"),
    ("cendoj", "TS"),
    ("dgt-vinculantes", "DGT"),
    ("petete.tributos", "DGT"),
    ("teac", "TEAC"),
    ("dyctea", "TEAC"),
    ("boe.es", "BOE"),
    ("/boe", "BOE"),
    ("agenciatributaria", "AEAT"),
    ("aeat", "AEAT"),
)


def infer_authority(metadata: dict[str, Any]) -> str | None:
    """Issuing authority of a chunk: sidecar value, else inferred from its path."""
    explicit = metadata.get("source_authority")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    haystack = " ".join(str(metadata.get(key) or "") for key in ("source_url", "source", "source_file")).lower()
    for hint, authority in _AUTHORITY_HINTS:
        if hint in haystack:
            return authority
    return None


def source_type_for(authority: str | None, metadata: dict[str, Any] | None = None) -> str | None:
    """Map an authority onto one of :data:`SOURCE_TYPES` (None when unknown)."""
    if not authority:
        return None
    upper = authority.upper()
    if upper == "CALCULADORA":
        return "calculo"
    if upper.startswith("TS") or upper in ("TC", "AN", "TSJ", "TJUE"):
        return "jurisprudencia"
    if upper in ("DGT", "TEAC", "TEAR"):
        return "doctrina_administrativa"
    if upper == "AEAT":
        return "aeat"
    if upper == "BOE" or upper.startswith("BOLETIN"):
        # Constitutional Court rulings are published in the BOE.
        haystack = " ".join(str((metadata or {}).get(key) or "") for key in ("source_url", "source", "source_file"))
        if "jurisprudencia-tc" in haystack.lower():
            return "jurisprudencia"
        return "normativa"
    return None


@dataclass(frozen=True)
class Fragment:
    fragment_id: str
    title: str
    locator: str
    text: str
    section: str | None = None
    authority: str | None = None
    source_type: str | None = None
    binding: bool | None = None
    date: str | None = None
    jurisdiction_scope: str | None = None
    reference: str | None = None  # ROJ / ECLI / nº consulta / nº resolución
    # Validity according to the doctrine graph (None when the graph has no opinion).
    vigencia: str | None = None  # vigente | matizada | superada | discutida
    vigencia_resumen: str | None = None
    doc_key: str | None = None
    # A Supreme Court cassation ruling whose doctrine passage was detected.
    fija_doctrina: bool = False

    def header(self) -> str:
        parts = [f"[{self.fragment_id}]", self.title]
        if self.section and self.section not in ("general", self.title):
            parts.append(f"§ {self.section}")
        details = [
            f"autoridad={self.authority}" if self.authority else None,
            f"tipo={self.source_type}" if self.source_type else None,
            f"ref={self.reference}" if self.reference else None,
            f"fecha={self.date}" if self.date else None,
            ("vinculante=sí" if self.binding else "vinculante=no") if self.binding is not None else None,
            f"ámbito={self.jurisdiction_scope}" if self.jurisdiction_scope else None,
            "fija_doctrina=sí" if self.fija_doctrina else None,
            f"VIGENCIA={self.vigencia.upper()}" if self.vigencia in ("superada", "discutida", "matizada") else None,
        ]
        rendered = " | ".join(d for d in details if d)
        head = " — ".join(parts)
        return f"{head} ({rendered})" if rendered else head

    def render(self) -> str:
        return f"{self.header()}\nfuente: {self.locator}\n{self.text}"


def _first_str(metadata: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


@dataclass
class EvidenceLedger:
    _fragments: dict[str, Fragment] = field(default_factory=dict)
    _by_key: dict[str, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def register(self, *, text: str, metadata: dict[str, Any]) -> Fragment:
        """Register a retrieved chunk; the same chunk always gets the same id.

        Thread-safe: researcher subagents share one ledger and search in parallel.
        """
        with self._lock:
            return self._register(text=text, metadata=metadata)

    def _register(self, *, text: str, metadata: dict[str, Any]) -> Fragment:
        locator = _first_str(metadata, ("source_url", "source", "locator", "source_file")) or "fuente_desconocida"
        chunk_id = metadata.get("chunk_id")
        key = str(chunk_id) if chunk_id else hashlib.sha256(f"{locator}\x00{text}".encode()).hexdigest()
        existing = self._by_key.get(key)
        if existing is not None:
            return self._fragments[existing]

        authority = infer_authority(metadata)
        binding = metadata.get("vinculante")
        fragment = Fragment(
            fragment_id=f"F{len(self._fragments) + 1}",
            title=_first_str(metadata, ("title", "section")) or "sin título",
            locator=locator,
            text=" ".join(text.split()),
            section=_first_str(metadata, ("section",)),
            authority=authority,
            source_type=source_type_for(authority, metadata),
            binding=binding if isinstance(binding, bool) else None,
            date=_first_str(metadata, ("fecha_resolucion", "fecha_actualizacion", "last_updated", "fecha")),
            jurisdiction_scope=_first_str(metadata, ("jurisdiction_scope",)),
            reference=_first_str(metadata, ("roj", "ecli", "num_consulta", "numero_resolucion", "criterio_id")),
            vigencia=_first_str(metadata, ("_vigencia_estado",)),
            vigencia_resumen=_first_str(metadata, ("_vigencia_resumen",)),
            doc_key=_first_str(metadata, ("_doc_key",)),
            fija_doctrina=metadata.get("fija_doctrina") is True,
        )
        self._fragments[fragment.fragment_id] = fragment
        self._by_key[key] = fragment.fragment_id
        return fragment

    def get(self, fragment_id: str) -> Fragment | None:
        return self._fragments.get(fragment_id.upper())

    def all(self) -> list[Fragment]:
        return list(self._fragments.values())

    def __len__(self) -> int:
        return len(self._fragments)


def extract_cited_ids(answer: str) -> list[str]:
    """Fragment ids cited in ``answer``, in first-appearance order, deduplicated."""
    seen: dict[str, None] = {}
    for group in _CITATION_GROUP_RE.finditer(answer):
        for fragment_id in _FRAGMENT_ID_RE.findall(group.group(1)):
            seen.setdefault(fragment_id.upper(), None)
    return list(seen)
