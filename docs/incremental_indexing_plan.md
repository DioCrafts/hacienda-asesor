# Incremental Indexing — Plan de implementación

> **Estado**: planning. No implementado. Esperando luz verde para abordarlo.
>
> **Tamaño estimado**: 5-7h efectivas de trabajo + 1-2h de pruebas reales.
>
> **Motivación**: la ingesta full sobre 11.345 docs tarda **~12h en Mac M-series**
> (Docling parsing CPU-bound + Qwen3 embedding en MPS). Tras la primera ingesta,
> el caso de uso típico es **añadir 100-200 BOEs nuevos al día**, no reindexar
> todo. Sin incremental, cada update diario = 12h. Con incremental, los mismos
> 200 BOEs nuevos = ~1-2 minutos.

---

## 1. Objetivo y scope

### Qué hace

Reusar trabajo previo cuando se relanza `python -m hacienda_gpt.cli.processor`
sobre un `content-dir` cuyo contenido se solapa con un índice ya existente.

Concretamente, para cada archivo en `content-dir`:

| Estado | Acción |
|---|---|
| Hash igual al del manifest anterior | **Skip total** (no Docling, no embedding, no FAISS write) |
| Hash distinto al del manifest (archivo modificado) | Reprocesar archivo, **borrar chunks viejos** de FAISS, añadir los nuevos |
| No existe en manifest (archivo nuevo) | Procesar y **append** a FAISS |
| Estaba en manifest pero ya no en disco | **Borrar chunks** de FAISS, quitar del manifest |

### Qué NO hace

- **No** salta la primera ingesta. La primera vez sigue tardando 12h.
- **No** reaprovecha vectores si cambia el embedder, `max_tokens`, o versión de
  Docling — el "pipeline fingerprint" detecta esos cambios y fuerza reindex
  completo automáticamente.
- **No** permite múltiples procesos escribiendo a la misma carpeta FAISS
  simultáneamente (file lock simple basta).
- **No** versiona chunks históricos. Si reprocesas un archivo, los chunks
  antiguos desaparecen. (Si se quiere mantener histórico, sería una funcionalidad
  adicional fuera de scope.)

### Casos de uso que cubre

| Caso | Antes | Después |
|---|---|---|
| Añadir 200 BOEs nuevos al día | ~12h | ~1-2 min |
| Recuperarse de crash a mitad de ingesta | Reempezar desde 0 | Continuar desde donde se quedó (excepto el batch en flight) |
| Re-indexar tras borrar 50 BOEs obsoletos | ~12h | <30 seg (solo FAISS delete) |
| Cambiar `max_tokens` 512→1024 | ~12h | ~12h (full reindex automático: chunking distinto) |
| Cambiar embedder Qwen3-0.6B → Qwen3-8B | ~12h | ~12h (full reindex automático: espacio vectorial distinto) |

---

## 2. Diseño

### 2.1 Manifest

Un archivo `manifest.json` junto a `index.faiss` e `index.pkl` en `output-dir`:

```json
{
  "schema_version": 1,
  "created_at": "2026-05-30T08:00:00Z",
  "updated_at": "2026-05-30T08:00:00Z",
  "pipeline_fingerprint": "qwen3-0.6b|dim=auto|max_tokens=512|docling=2.96.0|loader_schema=1",
  "stats": {
    "n_files": 11345,
    "n_chunks": 574024
  },
  "files": {
    "data/html/2026-05-28/abc123.html": {
      "sha256": "ab12ef...",
      "size_bytes": 4823,
      "n_chunks": 8,
      "chunk_ids": [
        "f5c2-0",
        "f5c2-1",
        "f5c2-2",
        "f5c2-3",
        "f5c2-4",
        "f5c2-5",
        "f5c2-6",
        "f5c2-7"
      ],
      "ingested_at": "2026-05-30T08:01:23Z"
    }
  }
}
```

**Decisiones de diseño**:
- **JSON vs SQLite**: JSON para 11K archivos es ~5-8 MB, lectura completa en <100ms.
  Suficiente. SQLite sería overkill por ahora.
- **Path relativo desde `content-dir`** para portabilidad: si mueves la carpeta
  `data/html/` a otro disco, el manifest sigue siendo válido.
- **`sha256` del contenido**, no del path: detecta modificaciones de contenido
  aunque mtime esté tocado por un rsync.
- **`size_bytes`** como sanity check rápido antes de calcular SHA (si tamaño
  difiere, el SHA va a diferir → saltar el hashing).
- **`chunk_ids`**: identificadores estables de los chunks de ese archivo en
  FAISS. Necesarios para el delete.
- **`schema_version`**: para evolucionar el formato sin romper instalaciones
  viejas.

### 2.2 Pipeline fingerprint

String determinístico que captura todas las variables que invalidarían el
índice si cambian:

```python
def compute_pipeline_fingerprint(
    embedder_model: str,
    embedder_dim: int | None,
    max_tokens: int | None,
    docling_version: str,
    loader_schema: int = 1,
) -> str:
    parts = [
        f"embedder={embedder_model}",
        f"dim={embedder_dim or 'auto'}",
        f"max_tokens={max_tokens or 'auto'}",
        f"docling={docling_version}",
        f"loader_schema={loader_schema}",
    ]
    return "|".join(parts)
```

Si el fingerprint del run actual difiere del guardado en el manifest, **se
ignora el manifest entero y se hace full reindex** (con log de aviso).

### 2.3 Chunk ID

Cada chunk necesita un identificador estable para poder añadirlo/eliminarlo de
FAISS. Hoy, langchain-docling no genera uno → debemos asignarlo nosotros en
`docling_chunk_metadata()`:

```python
def docling_chunk_metadata(doc: Document, source_file: str, chunk_index: int) -> dict[str, Any]:
    file_id = sha256(source_file.encode()).hexdigest()[:8]
    chunk_id = f"{file_id}-{chunk_index}"
    metadata = dict(doc.metadata or {})
    metadata["chunk_id"] = chunk_id
    # ... resto de metadata Docling
    return metadata
```

**Decisión**: derivar el `chunk_id` del nombre del archivo + índice ordinal
del chunk dentro del archivo. Estable mientras el archivo no cambie.

FAISS soporta `add_documents(docs, ids=[...])` y `delete(ids=[...])`. Hay que
verificar que langchain-community lo expone — si no, usar el `index_to_docstore_id`
directamente.

### 2.4 Operaciones FAISS

| Operación | Llamada langchain | Comentario |
|---|---|---|
| Cargar índice existente | `FAISS.load_local(output_dir, embeddings, allow_dangerous_deserialization=True)` | Requiere `FAISS_TRUSTED_INDEX=true` o equivalente |
| Append chunks nuevos | `db.add_documents(new_chunks, ids=[c.metadata["chunk_id"] for c in new_chunks])` | Si `ids` no soportado, usar wrappers manuales |
| Eliminar chunks viejos | `db.delete(ids=removed_chunk_ids)` | Necesario antes del rebuild si el archivo fue modificado |
| Persistir | `db.save_local(output_dir)` | Atomic: salvar a `output_dir.tmp` y renombrar |

### 2.5 Atomicidad

Crash a mitad del save dejaría índice + manifest desincronizados. Para evitar:

1. Salvar FAISS a `output_dir.tmp/`
2. Salvar manifest a `output_dir/manifest.json.tmp`
3. Renombrar `output_dir.tmp/index.{faiss,pkl}` → `output_dir/`
4. Renombrar `manifest.json.tmp` → `manifest.json`

Si crash en (1)-(3), el manifest viejo sigue válido y el próximo run reprocesa
los que tenían cambios pendientes.

### 2.6 Concurrency

**No soportar concurrency en v1**. Un solo proceso escribe a la vez. Mecanismo:

```python
# Al inicio:
lock_file = output_dir / ".indexing.lock"
if lock_file.exists():
    raise RuntimeError(
        f"Another indexing process is running (lock at {lock_file}). "
        f"Delete manually if you're sure no other process is active."
    )
lock_file.write_text(str(os.getpid()))
# Al final (try/finally):
lock_file.unlink(missing_ok=True)
```

---

## 3. Implementación paso a paso

### Fase 1 — Manifest módulo

**Archivo nuevo**: `hacienda_gpt/processor/manifest.py`

```python
class ManifestFileEntry(BaseModel):
    sha256: str
    size_bytes: int
    n_chunks: int
    chunk_ids: list[str]
    ingested_at: datetime

class Manifest(BaseModel):
    schema_version: int = 1
    created_at: datetime
    updated_at: datetime
    pipeline_fingerprint: str
    stats: dict[str, int]
    files: dict[str, ManifestFileEntry]

def load_manifest(output_dir: Path) -> Manifest | None: ...
def save_manifest(output_dir: Path, manifest: Manifest) -> None: ...
def compute_file_hash(path: Path) -> tuple[str, int]:
    """Returns (sha256_hex, size_bytes)."""
def compute_pipeline_fingerprint(...) -> str: ...

@dataclass
class FileDiff:
    new: list[str]
    modified: list[str]
    removed: list[str]
    unchanged: list[str]

def diff_files_against_manifest(
    current_files: list[str], manifest: Manifest, content_dir: Path,
) -> FileDiff: ...
```

**Lógica clave del diff**:

```python
def diff_files_against_manifest(current_files, manifest, content_dir):
    current_rel = {str(Path(f).relative_to(content_dir)): f for f in current_files}
    manifest_rel = set(manifest.files.keys())
    new = []
    modified = []
    unchanged = []
    for rel_path, abs_path in current_rel.items():
        if rel_path not in manifest_rel:
            new.append(abs_path)
            continue
        cur_sha, cur_size = compute_file_hash(Path(abs_path))
        prev = manifest.files[rel_path]
        if cur_size != prev.size_bytes or cur_sha != prev.sha256:
            modified.append(abs_path)
        else:
            unchanged.append(abs_path)
    removed = [str(content_dir / p) for p in manifest_rel - set(current_rel.keys())]
    return FileDiff(new=new, modified=modified, removed=removed, unchanged=unchanged)
```

### Fase 2 — Chunk ID estable

**Archivo a modificar**: `hacienda_gpt/processor/document_loader.py`

Cambios:
1. `docling_chunk_metadata()` recibe `source_file` y `chunk_index`, devuelve
   metadata con `chunk_id`.
2. `_process_one_file()` enumera chunks para pasar el índice.
3. Documentar el formato de `chunk_id`: `{file_hash_8chars}-{chunk_index}`.

```python
def _process_one_file(file_path: str) -> tuple[str, list[Document]]:
    loader = DoclingLoader(...)
    raw_chunks = loader.load()
    docs = []
    file_id = hashlib.sha256(file_path.encode()).hexdigest()[:8]
    for idx, chunk in enumerate(raw_chunks):
        meta = docling_chunk_metadata(chunk)
        meta["chunk_id"] = f"{file_id}-{idx}"
        meta["source_file"] = file_path
        docs.append(Document(page_content=chunk.page_content, metadata=meta))
    return (file_path, docs)
```

### Fase 3 — Diff-aware load_chunks

**Archivo a modificar**: `hacienda_gpt/processor/document_loader.py`

Nueva señal de entrada: `incremental: bool` (default True si manifest existe).

```python
class DocumentProcessor:
    def __init__(self, ..., incremental: bool = True):
        ...
        self.incremental = incremental

    def load_chunks_and_diff(self) -> tuple[list[Document], list[str], FileDiff | None]:
        """Returns (new_chunks_to_add, chunk_ids_to_remove, diff_or_None)."""
        files = self.discover_files()
        manifest = None
        if self.incremental:
            manifest = load_manifest(Path(self.output_dir))
            if manifest and manifest.pipeline_fingerprint != self._fingerprint():
                logging.warning(
                    "Pipeline fingerprint changed (was=%s now=%s). Forcing full reindex.",
                    manifest.pipeline_fingerprint, self._fingerprint(),
                )
                manifest = None

        if manifest is None:
            # Full reindex path
            new_chunks = self._process_files(files)
            return new_chunks, [], None

        diff = diff_files_against_manifest(files, manifest, Path(self.content_dir))
        logging.info(
            "Incremental diff: new=%d modified=%d removed=%d unchanged=%d",
            len(diff.new), len(diff.modified), len(diff.removed), len(diff.unchanged),
        )
        files_to_process = diff.new + diff.modified
        ids_to_remove = []
        for path in diff.modified + diff.removed:
            rel = str(Path(path).relative_to(Path(self.content_dir)))
            if rel in manifest.files:
                ids_to_remove.extend(manifest.files[rel].chunk_ids)
        new_chunks = self._process_files(files_to_process) if files_to_process else []
        return new_chunks, ids_to_remove, diff
```

### Fase 4 — Process documents con append/delete

```python
def process_documents(self) -> None:
    from langchain_community.vectorstores import FAISS
    new_chunks, ids_to_remove, diff = self.load_chunks_and_diff()

    if diff is None:
        # First run or fingerprint changed → full build
        if not new_chunks:
            raise RuntimeError("Nothing to index.")
        db = FAISS.from_documents(new_chunks, self.embeddings)
    else:
        try:
            db = FAISS.load_local(
                self.output_dir, self.embeddings, allow_dangerous_deserialization=True
            )
        except Exception as exc:
            logging.error("Failed to load existing FAISS index (%s); falling back to full rebuild.", exc)
            db = FAISS.from_documents(self.discover_and_process_all(), self.embeddings)
        else:
            if ids_to_remove:
                db.delete(ids=ids_to_remove)
                logging.info("Deleted %d stale chunks.", len(ids_to_remove))
            if new_chunks:
                ids = [c.metadata["chunk_id"] for c in new_chunks]
                db.add_documents(new_chunks, ids=ids)
                logging.info("Appended %d new chunks.", len(new_chunks))

    # Persist atomically.
    _atomic_save(db, self.output_dir)
    _save_manifest(self._build_manifest(new_chunks, diff), self.output_dir)
```

### Fase 5 — CLI flags

**Archivo a modificar**: `hacienda_gpt/cli/processor.py`

```python
@click.option(
    "--incremental/--full",
    default=True,
    help="Reuse work from a previous index if it matches the current pipeline. "
         "--full forces a fresh build, discarding any existing manifest.",
)
def cli(..., incremental: bool):
    args["incremental"] = incremental
    build_index(args)
```

Si no existe manifest y `--full` no se pasa: sin cambio (es la primera vez,
da igual). El flag importa solo cuando hay índice previo.

---

## 4. Tests

### 4.1 Tests unitarios — `tests/test_manifest.py`

| Caso | Espera |
|---|---|
| `load_manifest()` sin archivo previo | Devuelve `None` |
| `load_manifest()` con manifest válido | Devuelve `Manifest` poblado |
| `load_manifest()` con manifest corrupto (JSON inválido, schema malo) | Log de warning + devuelve `None` |
| `save_manifest()` después de cambio de fingerprint | El fingerprint guardado coincide |
| `compute_file_hash()` mismo contenido | SHA y tamaño consistentes entre llamadas |
| `compute_file_hash()` archivo inexistente | Raises |
| `diff_files_against_manifest()` corpus sin cambios | `new=[], modified=[], removed=[], unchanged=all` |
| `diff_files_against_manifest()` archivo añadido | Aparece en `new` |
| `diff_files_against_manifest()` archivo modificado (mismo path, distinto SHA) | Aparece en `modified` |
| `diff_files_against_manifest()` archivo eliminado del disco | Aparece en `removed` |
| `compute_pipeline_fingerprint()` con misma config | Siempre mismo string |
| `compute_pipeline_fingerprint()` cambio en `max_tokens` | Distinto string |

### 4.2 Tests de integración — `tests/test_document_loader_incremental.py`

Stubs del loader Docling para evitar parsing real.

| Caso | Espera |
|---|---|
| Primer run sin manifest previo | Full build; manifest creado con todos los archivos |
| Segundo run sin cambios en disco | `new_chunks=[]`, `ids_to_remove=[]`; FAISS sin modificar; manifest `updated_at` actualizado |
| Segundo run con 1 archivo añadido | Solo se procesa ese; FAISS crece en sus N chunks; manifest incluye la nueva entry |
| Segundo run con 1 archivo modificado | Chunks viejos eliminados de FAISS, nuevos añadidos; manifest entry actualizada |
| Segundo run con 1 archivo eliminado del disco | Chunks eliminados de FAISS; manifest entry removida |
| Segundo run con `--full` | Manifest ignorado; full rebuild; manifest sobrescrito |
| Segundo run con cambio de `max_tokens` | Fingerprint difiere → full rebuild automático + warning |
| Manifest corrupto | Fallback graceful a full rebuild + warning |
| Lock file presente | Raise con mensaje claro |

### 4.3 Tests E2E manuales

1. Indexar corpus de 20 archivos pequeños sintéticos → manifest correcto.
2. Borrar 1 → reindex → confirmar chunks de ese desaparecen de FAISS.
3. Modificar contenido de 1 → reindex → confirmar chunks viejos fuera, nuevos dentro.
4. Cambiar `max_tokens` y reindexar sin `--full` → debe disparar full rebuild automático.

---

## 5. Edge cases y mitigaciones

| Edge case | Mitigación |
|---|---|
| Mismo path, contenido distinto, mismo size | SHA detecta. |
| Path con caracteres Unicode raros | Normalizar a NFC, hash sobre bytes UTF-8. |
| Archivo borrado entre `discover_files()` y `compute_file_hash()` | `try/except FileNotFoundError`, tratar como removido. |
| Manifest existe pero index FAISS no | Logging + full rebuild (manifest ignorado). |
| Index FAISS existe pero manifest no | Logging + full rebuild (perdimos el tracking, rehacer). |
| `chunk_id` colisión entre archivos distintos | Improbable con sha256[:8] = 4M combinaciones para 11K archivos. Si pasa, usar 12 chars o el hash completo. |
| File_path absoluto en máquina A, relativo cargado en B | Manifest almacena **relativo a content_dir** → portable. |
| Múltiples instancias intentando indexar | Lock file. Sin file lock OS-level por simplicidad (no tenemos múltiples writers reales). |
| Docling produce 0 chunks para un archivo | Manifest entry con `n_chunks=0`, `chunk_ids=[]`. Próximo run con mismo hash skipa. |
| Embedder devuelve dimensiones distintas en runs sucesivos | No debería: misma config, mismo modelo → mismo dim. Si pasa, FAISS reportará shape mismatch en add → fallback a full rebuild con log. |

---

## 6. Rollout

### Migración del corpus actual

Cuando se mergee la feature:
1. **Primer run después del merge**: no hay manifest todavía → full reindex (12h). Una vez.
2. **A partir de ahí**: manifest creado, runs incrementales rápidos.

Para evitar el primer reindex de 12h al merger:
- Implementar un comando auxiliar `python -m hacienda_gpt.cli.bootstrap_manifest`
  que escanea el `output-dir` existente, calcula los `chunk_ids` desde FAISS
  metadata y construye un manifest desde cero.
- Solo funciona si los chunks actuales tienen `source_file` en metadata (hay
  que añadir eso ANTES en el código actual o aceptar que la primera vez se
  pierde).

**Recomendación pragmática**: simplemente correr una ingesta full más con el
nuevo código antes de empezar a beneficiarse del incremental. No vale la pena
el código de migración para un proyecto solo.

### Feature flag temporal

Mientras se prueba, dejar `--incremental` opcional (default False durante 1
release) para validar en producción. Luego promover a default True en versión
siguiente.

### Deprecación

Ninguna: el flag `--full` se queda permanentemente para forzar reindex cuando
se sospecha que el índice está corrupto.

---

## 7. Riesgos

| Riesgo | Probabilidad | Impacto | Mitigación |
|---|---|---|---|
| `db.delete(ids=...)` no soportado en langchain-community FAISS | Medio | Alto | Verificar antes de implementar. Fallback: cargar índice, filtrar metadata, rebuild parcial. |
| Chunk IDs colisionan entre runs | Bajo | Alto | Usar sha256[:8] o más chars. Tests con corpus grande. |
| Manifest corrupto deja FAISS huérfano | Bajo | Medio | Graceful fallback a full rebuild + log. |
| Performance del SHA en archivos grandes | Bajo | Bajo | Streaming hash, sha256 sobre 500KB es <50ms. |
| FAISS save no atómico → corrupción tras crash | Medio | Alto | Salvar a `.tmp` + rename. |
| Lock file zombie tras crash | Medio | Bajo | Mensaje claro indicando cómo borrarlo manualmente. |
| Append a FAISS no respeta orden → resultados de búsqueda cambian | Bajo | Bajo | FAISS no garantiza orden. Solo importa que los chunks existan. |

---

## 8. Acceptance criteria

La feature está terminada cuando:

- [ ] `tests/test_manifest.py` pasa con cobertura > 90% sobre `manifest.py`.
- [ ] `tests/test_document_loader_incremental.py` pasa todos los casos de la
      tabla 4.2 con stubs de Docling.
- [ ] Test E2E manual con 20 archivos sintéticos demuestra los 4 escenarios
      de la sección 4.3.
- [ ] Suite completa de tests del repo sigue pasando (220+ tests actuales).
- [ ] `python -m hacienda_gpt.cli.processor --help` documenta `--incremental`/`--full`.
- [ ] README actualizado con sección "Incremental indexing" explicando el flag.
- [ ] Crash a mitad de un run incremental + relanzar → continúa sin corrupción
      del FAISS.
- [ ] Smoke real: añadir 50 BOEs nuevos al corpus existente → procesa solo esos
      50, FAISS crece en ~1500 chunks, manifest actualizado. Wall time < 5 min.

---

## 9. Próximos pasos (fuera de scope v1, considerar después)

- **Locking distribuido** (file lock o leader election) si se quiere ingestar
  desde múltiples máquinas a un FAISS compartido.
- **Versionado de chunks**: en vez de eliminar chunks viejos al modificar un
  archivo, marcarlos como "superseded" y conservar histórico para auditoría.
  Útil si la app necesita responder consultas tipo "¿qué decía la Ley X antes
  de la reforma de 2022?".
- **Manifest diff API**: endpoint o CLI para responder "¿qué archivos nuevos /
  modificados están pendientes de indexar?" antes de lanzar la ingesta.
- **Storage SQLite del manifest** si el corpus crece a 100K+ archivos y el JSON
  empieza a doler en lecturas (improbable, pero abierto).
- **Notificación post-ingesta**: integración con Slack/email con un resumen de
  diff (qué se añadió/modificó/eliminó) — útil para auditoría regulatoria.
- **Backfill manifiesto desde índice existente** (sección 6) si se quiere evitar
  el reindex full inicial.
- **Detección de duplicados**: si dos `.html` distintos tienen el mismo SHA
  (poco probable pero posible si BOE expone la misma ley por dos rutas), o
  flagear en el manifest o deduplicar antes de indexar.

---

## 10. Apéndice: dependencias verificables antes de empezar

Antes de tocar código, validar:

```python
# ¿FAISS.delete(ids=[...]) está disponible en langchain-community 0.4.1?
from langchain_community.vectorstores import FAISS
help(FAISS.delete)  # debe aceptar `ids: list[str]`

# ¿FAISS.add_documents(docs, ids=[...]) acepta ids?
help(FAISS.add_documents)

# Si NO los acepta directamente, ¿se puede usar FAISS.from_documents para
# nuevos + merge_from para combinar con existente?
help(FAISS.merge_from)
```

Si alguna de estas APIs no existe en la versión pinneada, replantear la sección
2.4 (operaciones FAISS) — quizá usando `docstore` y `index_to_docstore_id`
directamente, o bumpeando la versión de langchain-community.
