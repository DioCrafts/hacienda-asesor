import functools
import logging
from pathlib import Path
import re
from typing import Any

from langchain_community.document_loaders import DirectoryLoader
from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_text_splitters import HTMLHeaderTextSplitter, RecursiveCharacterTextSplitter

HEADER_SPLITTER = HTMLHeaderTextSplitter(headers_to_split_on=[("h1", "section"), ("h2", "section"), ("h3", "section")])

# --- Title extraction helpers ---------------------------------------------- #
# The vanilla "first non-empty body line" heuristic loses badly on BOE and
# DYCTEA snapshots: BOE pages start with "Agencia Estatal Boletín Oficial del
# Estado", DYCTEA pages start with the literal string "DYCTEA". Both leak
# through to the grounding gate as the chunk title and make the model hedge
# even when retrieval is good. The helpers below read the raw HTML once per
# source file (LRU-cached) and apply per-domain rules.

_BOE_ID_PREFIX_RE = re.compile(r"^BOE-[A-Z]-\d+-\d+\s+(.+)$")
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.+?)</title>", re.IGNORECASE | re.DOTALL)
_TEAC_ASUNTO_RE = re.compile(
    r"Asunto\s*:\s*(?P<asunto>.+?)\s*(?:Referencias\s+normativas|Conceptos\s*:|Texto\s+de\s+la\s+resoluci|Volver\b)",
    re.IGNORECASE | re.DOTALL,
)
_TEAC_CRITERIO_RE = re.compile(
    r"de la resoluci[oó]n\s*:\s*([^\s<]+)", re.IGNORECASE
)
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_whitespace(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value).strip()


def _decode_basic_entities(value: str) -> str:
    # The few HTML entities we routinely see in BOE/DYCTEA titles.
    return (
        value.replace("&amp;", "&")
        .replace("&quot;", '"')
        .replace("&#34;", '"')
        .replace("&aacute;", "á")
        .replace("&eacute;", "é")
        .replace("&iacute;", "í")
        .replace("&oacute;", "ó")
        .replace("&uacute;", "ú")
        .replace("&ntilde;", "ñ")
        .replace("&Ntilde;", "Ñ")
    )


def _classify_source(source_path: str) -> str:
    lower = source_path.lower()
    if "/boe-" in lower or "boe.es/" in lower:
        return "boe"
    if "/teac/" in lower or "/dyctea/" in lower:
        return "teac"
    return "default"


@functools.lru_cache(maxsize=4096)
def _read_source_html(source_path: str) -> str | None:
    """Read a source HTML file from disk; cached so chunks share the IO cost."""
    try:
        path = Path(source_path)
    except (TypeError, ValueError):
        return None
    if not path.is_file() or path.suffix.lower() not in {".html", ".htm"}:
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _title_from_boe(html: str) -> str | None:
    match = _TITLE_TAG_RE.search(html)
    if not match:
        return None
    raw = _normalize_whitespace(_decode_basic_entities(match.group(1)))
    stripped = _BOE_ID_PREFIX_RE.match(raw)
    if stripped:
        return stripped.group(1).strip()
    return raw or None


def _title_from_teac(html: str) -> str | None:
    asunto = _TEAC_ASUNTO_RE.search(html)
    if asunto:
        cleaned = _normalize_whitespace(_decode_basic_entities(asunto.group("asunto")))
        if cleaned:
            return f"TEAC — {cleaned}"
    criterio = _TEAC_CRITERIO_RE.search(html)
    if criterio:
        return f"TEAC criterio {criterio.group(1).strip()}"
    return None


def _title_from_source(source_path: str) -> str | None:
    html = _read_source_html(source_path)
    if html is None:
        return None
    kind = _classify_source(source_path)
    if kind == "boe":
        return _title_from_boe(html)
    if kind == "teac":
        return _title_from_teac(html)
    # Generic fallback: <title> tag if present and informative.
    match = _TITLE_TAG_RE.search(html)
    if not match:
        return None
    raw = _normalize_whitespace(_decode_basic_entities(match.group(1)))
    return raw or None


class DocumentProcessor:
    def __init__(
        self,
        embeddings: Embeddings,
        content_dir: str,
        output_dir: str,
        chunk_size: int = 1000,
        chunk_overlap: int = 0,
        glob: str = "**/*.html",
    ) -> None:
        self.embeddings = embeddings
        self.content_dir = content_dir
        self.output_dir = output_dir
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.glob = glob

    def _create_text_splitter(self) -> RecursiveCharacterTextSplitter:
        return RecursiveCharacterTextSplitter(chunk_size=self.chunk_size, chunk_overlap=self.chunk_overlap)

    def _create_loader(self) -> DirectoryLoader:
        return DirectoryLoader(path=self.content_dir, glob=self.glob, use_multithreading=True, show_progress=True)

    def _parse_document_type(self, source_url: str) -> str:
        url = source_url.lower()
        if "faq" in url or "preguntas-frecuentes" in url:
            return "faq"
        if "manual" in url or "folletos" in url:
            return "manual"
        if "modelo" in url or "normativa" in url or "ley" in url:
            return "normativa"
        return "tramite"

    def _extract_last_updated(self, text: str) -> str | None:
        match = re.search(r"(\d{2}/\d{2}/\d{4})", text)
        return match.group(1) if match else None

    def _extract_title(self, text: str, source_path: str = "") -> str:
        if source_path:
            from_source = _title_from_source(source_path)
            if from_source:
                return from_source[:180]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return lines[0][:180] if lines else "sin_titulo"

    def _detect_normative_document_type(self, text: str, source_url: str) -> str | None:
        corpus = f"{text}\n{source_url}".lower()
        if "real decreto" in corpus:
            return "real_decreto"
        if re.search(r"\bley\s+\d+/\d{4}\b", corpus) or "ley " in corpus:
            return "ley"
        if "orden " in corpus:
            return "orden"
        if "resolución" in corpus or "resolucion" in corpus:
            return "resolucion"
        if "instrucción" in corpus or "instruccion" in corpus:
            return "instruccion"
        if "reglamento" in corpus:
            return "reglamento"
        return None

    def _detect_effective_date(self, text: str) -> str | None:
        patterns = [
            r"vigencia\s*:?\s*(\d{2}/\d{2}/\d{4})",
            r"en\s+vigor\s*(?:desde)?\s*(\d{2}/\d{2}/\d{4})",
            r"efectos\s+desde\s*(\d{2}/\d{2}/\d{4})",
        ]
        lowered = text.lower()
        for pattern in patterns:
            match = re.search(pattern, lowered)
            if match:
                return match.group(1)
        return None

    def _detect_scope(self, text: str, source_url: str) -> str:
        corpus = f"{text}\n{source_url}".lower()
        if "unión europea" in corpus or "union europea" in corpus or "ue" in corpus:
            return "eu"
        if "comunidad autónoma" in corpus or "comunidad autonoma" in corpus or "autonómico" in corpus:
            return "regional"
        if "españa" in corpus or "agencia tributaria" in corpus or "aeat" in corpus:
            return "nacional"
        return "unknown"

    def _detect_source_hierarchy(self, text: str, source_url: str) -> str:
        corpus = f"{text}\n{source_url}".lower()
        if "constitución" in corpus or "constitucion" in corpus:
            return "constitucion"
        if "ley " in corpus:
            return "ley"
        if "real decreto" in corpus or "decreto" in corpus:
            return "reglamento"
        if "orden" in corpus or "resolución" in corpus or "resolucion" in corpus:
            return "acto_administrativo"
        return "guia_administrativa"

    def _enrich_metadata(self, doc: Document, section: str | None = None) -> dict[str, Any]:
        source_url = doc.metadata.get("source", "")
        text = doc.page_content
        return {
            **doc.metadata,
            "source_url": source_url,
            "title": self._extract_title(text, source_url),
            "section": section or doc.metadata.get("section") or "general",
            "last_updated": self._extract_last_updated(text),
            "document_type": self._parse_document_type(source_url),
            "normative_document_type": self._detect_normative_document_type(text, source_url),
            "effective_date": self._detect_effective_date(text),
            "scope": self._detect_scope(text, source_url),
            "source_hierarchy": self._detect_source_hierarchy(text, source_url),
        }

    def _inject_legal_context(self, content: str, section: str | None) -> str:
        if not section:
            return content
        return f"[LEGAL_SECTION_CONTEXT] {section}\n\n{content}"

    def _semantic_split(self, doc: Document) -> list[Document]:
        source = doc.metadata.get("source", "")
        if source.lower().endswith(".html"):
            try:
                sem_chunks = HEADER_SPLITTER.split_text(doc.page_content)
                chunks: list[Document] = []
                for chunk in sem_chunks:
                    section = chunk.metadata.get("section")
                    content = self._inject_legal_context(chunk.page_content, section)
                    meta = self._enrich_metadata(doc, section)
                    meta["legal_context_header"] = section or "general"
                    chunks.append(Document(page_content=content, metadata=meta))
                if not chunks:
                    raise ValueError("empty semantic chunks")
                return chunks
            except Exception:
                logging.warning("Semantic HTML split failed for %s, falling back to recursive splitter", source)

        recursive_chunks = self._create_text_splitter().split_documents([doc])
        contextual_chunks: list[Document] = []
        for chunk in recursive_chunks:
            section = chunk.metadata.get("section") or doc.metadata.get("section") or "general"
            content = self._inject_legal_context(chunk.page_content, section)
            meta = self._enrich_metadata(chunk, section)
            meta["legal_context_header"] = section
            contextual_chunks.append(Document(page_content=content, metadata=meta))
        return contextual_chunks

    def _load_and_split(self) -> list[Document]:
        loaded_docs = self._create_loader().load()
        chunks: list[Document] = []
        for doc in loaded_docs:
            chunks.extend(self._semantic_split(doc))
        return chunks

    def process_documents(self) -> None:
        logging.info("Loading documents from %s", self.content_dir)
        documents = self._load_and_split()
        logging.info("Loaded %d chunks for indexing", len(documents))
        db = FAISS.from_documents(documents, self.embeddings)
        db.save_local(self.output_dir)
        logging.info("Local FAISS index successfully saved")


def _process(args: dict, embedder: str) -> None:
    # Lazy import keeps the processor module (and its CLI) light, and routes
    # every backend through the single embedder factory so the index always
    # matches the query path.
    from hacienda_gpt.llm.embeddings import create_embeddings

    processor = DocumentProcessor(create_embeddings(embedder), **args)
    processor.process_documents()


def process_with_qwen3(args: dict) -> None:
    """Index using the local multilingual Qwen3-Embedding model (default)."""
    _process(args, "qwen3")


def process_with_openai(args: dict) -> None:
    _process(args, "openai")


def process_with_gpt4all(args: dict) -> None:
    _process(args, "gpt4all")
