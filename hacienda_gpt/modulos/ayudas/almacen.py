"""Store of the calls detected in the BOE: one JSON file outside the repository.

The directory is ``AYUDAS_DATA_DIR`` (default ``./data/modulos/ayudas``, which ``.gitignore`` keeps out
of git). The file holds public data only (BOE ids, titles, quotes, deadlines): nothing of any user.
Writes are atomic (temporary file + rename).
"""

from __future__ import annotations

from datetime import date, timedelta
import json
import os
from pathlib import Path
import tempfile

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hacienda_gpt.modulos.ayudas.convocatorias import Convocatoria

FICHERO = "convocatorias.json"
# Calls whose last deadline passed this long ago are dropped.
DIAS_CONSERVACION = 120


def directorio() -> Path:
    return Path(os.environ.get("AYUDAS_DATA_DIR", "./data/modulos/ayudas"))


class Almacen(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    # Last BOE day read completely (summary and every candidate's text).
    revisado_hasta: date | None = None
    # Day of the last run (a run per day is enough).
    ultima_revision: date | None = None
    convocatorias: dict[str, Convocatoria] = Field(default_factory=dict)
    errores: list[str] = Field(default_factory=list)

    def purgar(self, hoy: date) -> None:
        limite = hoy - timedelta(days=DIAS_CONSERVACION)
        self.convocatorias = {
            boe_id: conv
            for boe_id, conv in self.convocatorias.items()
            if (max((p.fin for p in conv.plazos), default=conv.fecha_publicacion + timedelta(days=30))) >= limite
        }
        self.errores = self.errores[-50:]


def cargar(dir_: Path | None = None) -> Almacen:
    ruta = (dir_ or directorio()) / FICHERO
    if not ruta.exists():
        return Almacen()
    try:
        return Almacen.model_validate_json(ruta.read_text(encoding="utf-8"))
    except (ValidationError, ValueError, OSError):
        # A damaged store is rebuilt from the BOE on the next run rather than breaking the tools.
        return Almacen()


def guardar(almacen: Almacen, dir_: Path | None = None) -> Path:
    destino = dir_ or directorio()
    destino.mkdir(parents=True, exist_ok=True)
    ruta = destino / FICHERO
    fd, tmp = tempfile.mkstemp(prefix=".convocatorias.", suffix=".json", dir=destino)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(almacen.model_dump(mode="json"), ensure_ascii=False, indent=1))
        os.replace(tmp, ruta)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return ruta
