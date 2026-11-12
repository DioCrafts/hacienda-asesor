"""SQLite-backed doctrine graph: documents, aliases and typed relations.

An edge ``origen → destino`` says what the *citing* document does with the
cited one:

* ``cita``        neutral reference
* ``confirma``    follows / reiterates it
* ``matiza``      qualifies or narrows it
* ``supera``      overrules it, or sets a criterion that leaves it without effect
* ``contradice``  reaches the opposite conclusion without the authority to overrule

From those edges each document gets a validity status (:meth:`DoctrineGraph.vigencia`).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
import sqlite3
import threading
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hacienda_gpt.doctrine.citas import RANGO, Documento

Relacion = Literal["cita", "confirma", "matiza", "supera", "contradice"]
RELACIONES: tuple[str, ...] = ("cita", "confirma", "matiza", "supera", "contradice")
Estado = Literal["vigente", "matizada", "superada", "discutida", "desconocido"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodos (
    clave TEXT PRIMARY KEY, tipo TEXT NOT NULL, fecha TEXT, titulo TEXT NOT NULL DEFAULT '',
    locator TEXT NOT NULL DEFAULT '', vinculante INTEGER, fija_doctrina INTEGER NOT NULL DEFAULT 0,
    en_corpus INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS alias (alias TEXT PRIMARY KEY, clave TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS aristas (
    origen TEXT NOT NULL, destino TEXT NOT NULL, relacion TEXT NOT NULL, confianza REAL NOT NULL,
    evidencia TEXT NOT NULL DEFAULT '', metodo TEXT NOT NULL, PRIMARY KEY (origen, destino)
);
CREATE INDEX IF NOT EXISTS aristas_destino ON aristas (destino);
"""


class Nodo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clave: str
    tipo: str
    fecha: date | None = None
    titulo: str = ""
    locator: str = ""
    vinculante: bool | None = None
    fija_doctrina: bool = False
    en_corpus: bool = False

    @property
    def rango(self) -> int:
        return RANGO.get(self.tipo, 0)

    def etiqueta(self, alias: tuple[str, ...] = ()) -> str:
        """Short human label: "STS rec. 1234/2019 (03/06/2020)".

        A Supreme Court ruling is named the way doctrine cites it — by appeal
        number, else judgment number — rather than by its ECLI, when known.
        """
        kind, _, rest = self.clave.partition(":")
        if kind == "TS":
            keys = [self.clave, *alias]
            label = f"STS {rest}"
            for prefix, template in (("TS:REC:", "STS rec. {}"), ("TS:NUM:", "STS {}"), ("TS:ROJ:", "STS (ROJ {})")):
                match = next((key for key in keys if key.startswith(prefix)), None)
                if match:
                    label = template.format(match[len(prefix) :])
                    break
        elif kind == "TC":
            label = f"STC {rest}"
        elif kind == "DGT":
            label = f"consulta DGT {rest}"
        else:
            label = f"resolución TEAC {rest}"
        return f"{label} ({self.fecha.strftime('%d/%m/%Y')})" if self.fecha else label


class Arista(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origen: str
    destino: str
    relacion: Relacion
    confianza: float = Field(ge=0.0, le=1.0)
    evidencia: str = ""
    metodo: str  # "heuristica" | "llm" | "manual"


class Vigencia(BaseModel):
    """Validity of a document, with the relations that determine it."""

    model_config = ConfigDict(extra="forbid")

    clave: str
    estado: Estado
    # One sentence, ready to show: "Superada por STS rec. 1234/2019 (03/06/2020)".
    resumen: str
    motivos: list[Arista] = Field(default_factory=list)


class DoctrineGraph:
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        # One connection, serialised by the lock: the API serves from threads.
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------- writes

    def resolve(self, clave: str) -> str:
        """Canonical node key for ``clave`` (itself when it is not an alias)."""
        with self._lock:
            row = self._conn.execute("SELECT clave FROM alias WHERE alias = ?", (clave,)).fetchone()
        return row["clave"] if row else clave

    def upsert(self, doc: Documento, *, en_corpus: bool) -> str:
        """Insert or enrich a node; returns its canonical key.

        A node first seen as a bare citation ("TS:REC:1234/2019") is merged with
        the full document when it is later ingested under another key.
        """
        keys = [doc.clave, *doc.alias]
        existing = [self.resolve(key) for key in keys]
        with self._lock, self._conn:
            known = [
                key
                for key in dict.fromkeys(existing)
                if self._conn.execute("SELECT 1 FROM nodos WHERE clave = ?", (key,)).fetchone()
            ]
            canonical = known[0] if known else doc.clave
            for other in known[1:]:  # the same ruling was known under two keys: merge
                self._conn.execute("UPDATE OR IGNORE aristas SET origen = ? WHERE origen = ?", (canonical, other))
                self._conn.execute("UPDATE OR IGNORE aristas SET destino = ? WHERE destino = ?", (canonical, other))
                self._conn.execute("DELETE FROM aristas WHERE origen = ? OR destino = ?", (other, other))
                self._conn.execute("UPDATE alias SET clave = ? WHERE clave = ?", (canonical, other))
                self._conn.execute("DELETE FROM nodos WHERE clave = ?", (other,))
                self._conn.execute("INSERT OR REPLACE INTO alias (alias, clave) VALUES (?, ?)", (other, canonical))
            self._conn.execute("INSERT OR IGNORE INTO nodos (clave, tipo) VALUES (?, ?)", (canonical, doc.tipo))
            self._conn.execute(
                """UPDATE nodos SET
                       fecha = COALESCE(?, fecha),
                       titulo = CASE WHEN ? != '' THEN ? ELSE titulo END,
                       locator = CASE WHEN ? != '' THEN ? ELSE locator END,
                       vinculante = COALESCE(?, vinculante),
                       fija_doctrina = MAX(fija_doctrina, ?),
                       en_corpus = MAX(en_corpus, ?)
                   WHERE clave = ?""",
                (
                    doc.fecha.isoformat() if doc.fecha else None,
                    doc.titulo,
                    doc.titulo,
                    doc.locator,
                    doc.locator,
                    None if doc.vinculante is None else int(doc.vinculante),
                    int(doc.fija_doctrina),
                    int(en_corpus),
                    canonical,
                ),
            )
            for key in keys:
                if key != canonical:
                    self._conn.execute("INSERT OR REPLACE INTO alias (alias, clave) VALUES (?, ?)", (key, canonical))
        return canonical

    def add_edge(self, arista: Arista) -> None:
        """Record a relation. A neutral ``cita`` never overwrites a typed one,
        and a lower-confidence reading never overwrites a higher-confidence one."""
        if arista.origen == arista.destino:
            return
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT relacion, confianza FROM aristas WHERE origen = ? AND destino = ?",
                (arista.origen, arista.destino),
            ).fetchone()
            if row is not None:
                weaker = arista.relacion == "cita" and row["relacion"] != "cita"
                if weaker or (arista.confianza < row["confianza"] and row["relacion"] != "cita"):
                    return
            self._conn.execute(
                "INSERT OR REPLACE INTO aristas (origen, destino, relacion, confianza, evidencia, metodo) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (arista.origen, arista.destino, arista.relacion, arista.confianza, arista.evidencia, arista.metodo),
            )

    # -------------------------------------------------------------- reads

    @staticmethod
    def _nodo(row: sqlite3.Row) -> Nodo:
        return Nodo(
            clave=row["clave"],
            tipo=row["tipo"],
            fecha=date.fromisoformat(row["fecha"]) if row["fecha"] else None,
            titulo=row["titulo"],
            locator=row["locator"],
            vinculante=None if row["vinculante"] is None else bool(row["vinculante"]),
            fija_doctrina=bool(row["fija_doctrina"]),
            en_corpus=bool(row["en_corpus"]),
        )

    def etiqueta(self, nodo: Nodo) -> str:
        """Human label of a node, using its aliases to pick the most readable name."""
        with self._lock:
            rows = self._conn.execute("SELECT alias FROM alias WHERE clave = ?", (nodo.clave,)).fetchall()
        return nodo.etiqueta(tuple(row["alias"] for row in rows))

    def get(self, clave: str) -> Nodo | None:
        canonical = self.resolve(clave)
        with self._lock:
            row = self._conn.execute("SELECT * FROM nodos WHERE clave = ?", (canonical,)).fetchone()
        return self._nodo(row) if row else None

    def _edges(self, where: str, value: str) -> list[Arista]:
        with self._lock:
            rows = self._conn.execute(f"SELECT * FROM aristas WHERE {where} = ?", (value,)).fetchall()  # noqa: S608
        return [Arista(**dict(row)) for row in rows]

    def entrantes(self, clave: str) -> list[Arista]:
        """What later documents do with this one."""
        return self._edges("destino", self.resolve(clave))

    def salientes(self, clave: str) -> list[Arista]:
        return self._edges("origen", self.resolve(clave))

    def stats(self) -> dict[str, int]:
        with self._lock:
            out = {
                "nodos": self._conn.execute("SELECT COUNT(*) FROM nodos").fetchone()[0],
                "nodos_en_corpus": self._conn.execute("SELECT COUNT(*) FROM nodos WHERE en_corpus = 1").fetchone()[0],
                "aristas": self._conn.execute("SELECT COUNT(*) FROM aristas").fetchone()[0],
            }
            for row in self._conn.execute("SELECT relacion, COUNT(*) AS n FROM aristas GROUP BY relacion"):
                out[f"aristas_{row['relacion']}"] = row["n"]
        return out

    def mas_citadas_fuera_del_corpus(self, tipo: str = "TS", limit: int = 100) -> list[tuple[Nodo, int]]:
        """Cited-but-missing documents, most cited first: what is worth acquiring."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT n.*, COUNT(a.origen) AS citas FROM nodos n JOIN aristas a ON a.destino = n.clave
                   WHERE n.en_corpus = 0 AND n.tipo = ? GROUP BY n.clave ORDER BY citas DESC, n.clave LIMIT ?""",
                (tipo, limit),
            ).fetchall()
        return [(self._nodo(row), row["citas"]) for row in rows]

    def vigencia(self, clave: str) -> Vigencia:
        """Validity status of a document.

        * ``superada``  — a later document of equal or higher rank overrules it.
        * ``discutida`` — a document contradicts it without the rank (or the
          later date) needed to settle the matter: there is no firm criterion.
        * ``matizada``  — a later document of equal or higher rank qualifies it.
        * ``vigente``   — it is in the graph and nothing above applies.
        * ``desconocido`` — the graph has never seen it.
        """
        nodo = self.get(clave)
        if nodo is None:
            return Vigencia(clave=clave, estado="desconocido", resumen="Sin información de vigencia.")

        superan: list[tuple[Arista, Nodo]] = []
        discuten: list[tuple[Arista, Nodo]] = []
        matizan: list[tuple[Arista, Nodo]] = []
        for arista in self.entrantes(nodo.clave):
            otro = self.get(arista.origen)
            if otro is None or arista.relacion in ("cita", "confirma"):
                continue
            posterior = nodo.fecha is None or otro.fecha is None or otro.fecha >= nodo.fecha
            prevalece = otro.rango >= nodo.rango and posterior
            if arista.relacion == "supera" and prevalece:
                superan.append((arista, otro))
            elif arista.relacion in ("supera", "contradice"):
                discuten.append((arista, otro))
            elif arista.relacion == "matiza" and prevalece:
                matizan.append((arista, otro))
        # A higher court disagreeing with this document from the other side.
        for arista in self.salientes(nodo.clave):
            otro = self.get(arista.destino)
            if otro is not None and arista.relacion == "contradice" and otro.rango >= nodo.rango:
                discuten.append((arista, otro))

        def nombres(pairs: list[tuple[Arista, Nodo]]) -> str:
            return "; ".join(dict.fromkeys(self.etiqueta(otro) for _, otro in pairs))

        if superan:
            return Vigencia(
                clave=nodo.clave,
                estado="superada",
                resumen=f"Superada por {nombres(superan)}.",
                motivos=[a for a, _ in superan],
            )
        if discuten:
            return Vigencia(
                clave=nodo.clave,
                estado="discutida",
                resumen=f"Criterio discutido: discrepa de {nombres(discuten)} sin que conste una resolución que lo zanje.",
                motivos=[a for a, _ in discuten],
            )
        if matizan:
            return Vigencia(
                clave=nodo.clave,
                estado="matizada",
                resumen=f"Matizada por {nombres(matizan)}.",
                motivos=[a for a, _ in matizan],
            )
        return Vigencia(clave=nodo.clave, estado="vigente", resumen="Sin resoluciones posteriores que la contradigan.")
