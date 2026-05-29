# 🚀 HaciendaGPT

> **Asistente RAG en español especializado en consultas sobre la Agencia Tributaria de España (AEAT).**

HaciendaGPT combina recuperación semántica (RAG), reglas de decisión fiscal y una interfaz simple para ayudar a responder consultas tributarias con mayor trazabilidad, contexto y foco en seguridad.

---

## ✨ ¿Qué ofrece?

- 📚 **RAG sobre contenido fiscal** (HTML/PDF) con FAISS.
- 🧠 **Motor de reglas** para evaluación de obligaciones y casos.
- 🛡️ **Grounding gate**: abstención forzada cuando no hay citas normativas suficientes (modos `cited` / `uncited` / `abstained`).
- 🧭 **Detector de drift normativo**: cada regla declara las fuentes BOE/AEAT que la sustentan; un CLI compara los hashes contra el último snapshot.
- 🔐 **Hardening de seguridad** frente a prompt injection en contexto recuperado.
- 🖥️ **Interfaz Streamlit** para uso interactivo.
- ⚡ **API FastAPI** para integración en servicios.
- 🧪 **Suite de tests** para validar comportamiento funcional y documental.

---

## 🧱 Arquitectura (alto nivel)

1. **Crawler**: descarga contenido fuente (web/PDF).
2. **Processor**: normaliza documentos y construye índice vectorial FAISS.
3. **Retrieval + LLM**: recupera contexto relevante y genera respuesta.
4. **Decision Engine**: evalúa reglas por periodo fiscal y hechos detectados.
5. **UI/API**: expone experiencia de usuario e integración.

---

## ✅ Requisitos

- Python `>=3.13,<3.14`
- [uv](https://docs.astral.sh/uv/)
- (Opcional para crawler web) Playwright/Chromium

---

## ⚙️ Setup rápido

```bash
uv sync                  # crea .venv e instala deps (main + dev) desde uv.lock
# opcional: cp .env.example .env
```

Variables de entorno recomendadas:

```bash
export OPENAI_API_KEY="sk-..."          # solo para el LLM de chat (ChatOpenAI)
export OPENAI_MODEL="gpt-4o-mini"
export OPENAI_TEMPERATURE="0"
export TOP_K="3"

# Umbrales de similitud (coseno) del EmbeddingsFilter. Defaults pensados para
# Qwen3 (coseno normalizado); calíbralos con benchmark_retrieval. Subirlos =
# más precisión/menos recall (riesgo de abstención); bajarlos = lo contrario.
export RETRIEVAL_DECISION_THRESHOLD="0.45"   # perfil "decision" (más estricto)
export RETRIEVAL_EXPLAIN_THRESHOLD="0.35"    # perfil "explain" (más recall)

# Embedder local (mismo para indexar y consultar; ver sección "Embeddings"):
export EMBEDDING_MODEL="Qwen/Qwen3-Embedding-8B"
export EMBEDDING_DEVICE="cuda"           # cuda | cpu | mps
export EMBEDDING_NORMALIZE="true"
# export EMBEDDING_DIM="1024"            # opcional: truncado MRL para un índice más pequeño

export FAISS_INDEX_PATH="./data/faiss"
# Seguridad: activa solo si el índice FAISS proviene de una fuente 100% confiable
export FAISS_TRUSTED_INDEX="true"
export DECISION_DEBUG_MODE="false"
export DECISION_STATE_DB_PATH="./data/decision_state.sqlite3"
```

### Embeddings

El proyecto usa **un único embedder local**: `Qwen/Qwen3-Embedding-8B`
(multilingüe, nº1 en MMTEB, 100+ idiomas con español incluido, sin coste por
llamada). Se construye en `hacienda_gpt/llm/embeddings.py` y lo comparten la
indexación y la consulta, así que **no hay riesgo de mismatch** entre el índice
y el retrieval.

- Genera vectores de 4096 dims, truncables con `EMBEDDING_DIM` (MRL).
- Recomienda GPU con ≥16 GB de VRAM; en CPU es lento.
- Puedes apuntar a otro modelo *sentence-transformers* con `EMBEDDING_MODEL`
  (p.ej. `intfloat/multilingual-e5-large`).

> El LLM de chat sigue siendo OpenAI (`OPENAI_API_KEY`); solo los *embeddings*
> son locales.
>
> ⚠️ Si cambias de modelo de embedding **reconstruye el índice FAISS** (los
> espacios vectoriales no son intercambiables). Los umbrales de similitud ahora
> viven en `settings.py` y son configurables por entorno
> (`RETRIEVAL_DECISION_THRESHOLD` / `RETRIEVAL_EXPLAIN_THRESHOLD`). Los valores
> históricos (0.82 / 0.75) estaban pensados para OpenAI y sobre-filtraban con
> Qwen3 (coseno normalizado), provocando abstenciones aun con buen retrieval;
> los defaults se bajaron a 0.45 / 0.35. **Calíbralos** con
> `uv run python -m hacienda_gpt.cli.benchmark_retrieval`.

> 🍎 **macOS**: si al primer request a `/qa` (uvicorn) o a la UI
> (Streamlit) el proceso muere con
> `OMP: Error #15: Initializing libomp.dylib, but found libomp.dylib
> already initialized`, exporta `KMP_DUPLICATE_LIB_OK=TRUE` antes de
> lanzar el servicio. La causa es que `faiss-cpu` y las dependencias de
> `sentence-transformers`/`unstructured` enlazan dos copias de libomp en el mismo
> proceso.

---

## 📜 1.b) Documentos BOE dirigidos (seed catalog)

Para temas concretos cuya página AEAT no es accesible o que necesitan la
base normativa oficial (ej. Modelo 720), la lista canónica vive en
[hacienda_gpt/cli/boe_seed_catalog.json](hacienda_gpt/cli/boe_seed_catalog.json).
Cada entrada referencia un ID de BOE, su tema y el subdirectorio destino
dentro del snapshot. Vive junto al CLI a propósito: si lo dejásemos bajo
`rules/`, `load_rules_from_directory` lo intentaría parsear como reglas
de decisión.

```bash
uv run python -m hacienda_gpt.cli.boe_seed
# o, para previsualizar sin descargar:
uv run python -m hacienda_gpt.cli.boe_seed --dry-run
```

El CLI escribe los HTML consolidados (`/buscar/act.php?id=...`) en
`data/html/<snapshot_date>/<subdir>/<ID>.html` junto a un
`boe-seed-manifest.json` con los tamaños descargados. El reindexado
posterior (`hacienda_gpt.cli.processor`) los recoge igual que el resto
del corpus HTML.

---

## 🏛️ 1.c) TEAC (Doctrina del Tribunal Económico-Administrativo Central)

DYCTEA usa identificadores compuestos para sus criterios
(`54/00218/2024/00/0/1`, no enteros). El CLI `teac_seed` recorre el
buscador real por rango de fechas y baja cada criterio individualmente:

```bash
uv run python -m hacienda_gpt.cli.teac_seed --from-date 2024-01-01 --to-date 2024-12-31
# previsualización (sin descargar los detalles):
uv run python -m hacienda_gpt.cli.teac_seed --from-date 2024-01-01 --to-date 2024-06-30 --dry-run
```

Salida: `data/html/<snapshot>/teac/<safe_id>.html` + `.json` por
criterio, más un `teac-seed-manifest.json` por snapshot. La estrategia
de paginación se detiene en cuanto una página vuelve sin enlaces
nuevos.

> Nota: la spider scrapy `TEACCrawler` original itera `range(start_id, end_id)`
> sobre enteros, lo cual nunca encajó con el esquema real de DYCTEA. Se
> mantiene para no romper sus tests pero `teac_seed` es el camino
> recomendado.

## ⚖️ 1.d) CENDOJ (jurisprudencia)

> ⚠️ **Aviso legal CENDOJ.** El portal poderjudicial.es publica un
> aviso legal vinculante antes de cada búsqueda:
> *"El usuario de la base de datos podrá consultar los documentos
> siempre que lo haga para su uso particular. No está permitida la
> utilización de la base de datos para usos comerciales, ni la
> descarga masiva de información. La reutilización de esta información
> para la elaboración de bases de datos o con fines comerciales debe
> seguir el procedimiento y las condiciones establecidas por el CGPJ
> a través de su Centro de Documentación Judicial."*
>
> Por eso este repo **no incluye** un crawler automatizado de CENDOJ.
> Construir uno y volcar resultados en este índice FAISS sería
> "elaboración de base de datos" y, salvo autorización expresa del
> CGPJ, infringiría las condiciones. Si necesitas cobertura de
> jurisprudencia a escala, gestiona la autorización con el Centro de
> Documentación Judicial antes de tocar código.

Para usos individuales y selectivos (consultas puntuales en el
contexto de tu propio análisis), la spider scrapy existente
(`--crawler cendoj`) sigue valiendo: tú aportas las URLs
explícitamente. La plantilla versionada está en
[hacienda_gpt/cli/cendoj_seed_urls.txt](hacienda_gpt/cli/cendoj_seed_urls.txt):

```bash
uv run python -m hacienda_gpt.cli.crawler \
  --crawler cendoj --folder ./data/cendoj \
  --cendoj-urls-file hacienda_gpt/cli/cendoj_seed_urls.txt
```

## 📄 1.e) PDFs

> AEAT ha completado la migración de folletos, manuales y guías a HTML
> interactivo. Probamos `--crawler pdf` con `depth=2` desde la raíz de
> Sede y desde `www.agenciatributaria.es`, y los puntos típicamente
> ricos en PDF (calendario contribuyente, manual Renta, manual
> actividades económicas, modelos y formularios) — **cero PDFs
> encontrados en ninguno**. La spider `AgenciaTributariaPDFCrawler` no
> es funcional contra AEAT hoy; se mantiene en el código para no
> romper sus tests pero **no se debe esperar que produzca corpus**.

Las únicas fuentes PDF que sí funcionan integradas en el repo son:

```bash
# Códigos consolidados autonómicos del BOE (4 PDFs verificados):
uv run python -m hacienda_gpt.cli.crawler --crawler boe-ccaa --folder ./data/boe --skip-unknown-ccaa
```

Si necesitas ingerir PDFs externos (manuales de despacho propio,
boletines forales, etc.), añade su URL al spider que corresponda
o crea una mini-CLI siguiendo el patrón de
[hacienda_gpt/cli/boe_seed.py](hacienda_gpt/cli/boe_seed.py).

## 🕸️ 1) Crawling de contenidos

### HTML (sitio AEAT)

```bash
uv run python -m hacienda_gpt.cli.crawler --crawler web --folder ./data/html --depth 1 --mode flat
```

### PDF

```bash
uv run python -m hacienda_gpt.cli.crawler --crawler pdf --folder ./data/pdf --depth 1
```

> El crawler guarda por defecto en carpetas con `snapshot_date` (YYYY-MM-DD).

---

## 🧩 2) Construcción del índice FAISS

> ℹ️ El índice y la consulta comparten el mismo embedder local (Qwen3-Embedding,
> ver sección **Embeddings**), así que no hay nada que elegir. Reconstruye el
> índice solo si cambias `EMBEDDING_MODEL`.

```bash
uv run python -m hacienda_gpt.cli.processor --content-dir ./data/html --output-dir ./data/faiss --overwrite-output
```

---

## 💬 3) Ejecutar UI (Streamlit)

```bash
uv run streamlit run hacienda_gpt/ui/app.py
```

---

## 🔌 4) Ejecutar API (FastAPI)

```bash
uv run uvicorn hacienda_gpt.api.api:app --reload --host 127.0.0.1 --port 8000
```

### Endpoints: `/qa` vs `/cases`

La API expone **dos pipelines distintos** con capacidades muy diferentes.

#### `/qa` — Q&A retrieval-augmented (madura)

RAG completo: FAISS + LLM + grounding gate.

- Query libre en español.
- Recupera top-K chunks del índice FAISS y compone la respuesta citando
  documentos AEAT (`mode: cited`).
- Abstención automática (`mode: abstained`) si no hay citas suficientes o
  el modelo expresa duda.
- Resistente a prompt injection y preguntas fuera de dominio.

Úsalo para **consultas explicativas** sobre normativa tributaria.

```bash
curl -X POST http://127.0.0.1:8000/qa \
  -H 'Content-Type: application/json' \
  -d '{"query": "¿Tengo que declarar IRPF si soy residente?"}'
```

#### `/cases` + `/cases/{id}/turn` — Decision engine (en desarrollo)

Motor de decisión conversacional con estado (case + facts + obligations).

- Cada `/turn` extrae hechos del usuario vía `OpenAIFactExtractor`
  (structured output con JSON-schema; ver `decision/fact_extractor.py`).
- Mantiene `missing_facts` y `next_questions`. La `QuestionPolicy`
  reformula la pregunta después de 1-2 intentos sin respuesta.
- Tras `MAX_ASKS_PER_FACT = 3` intentos seguidos sin que el usuario aporte
  el hecho, el campo se añade a `gave_up_facts` y la respuesta marca
  `degraded: true`, evitando bucles.
- Evalúa `rules_engine.py` y devuelve `candidate_obligation_ids`.

Úsalo para **resolución estructurada** de obligaciones fiscales por
contribuyente.

```bash
CASE=$(curl -sS -X POST http://127.0.0.1:8000/cases \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"demo","jurisdiction":"ES","tax_period":"2024"}' \
  | python -c "import sys,json; print(json.load(sys.stdin)['case_id'])")
curl -X POST "http://127.0.0.1:8000/cases/$CASE/turn" \
  -H 'Content-Type: application/json' \
  -d '{"user_input":"Soy autónomo en Madrid, facturo 45000€"}'
```

> ⚠️ **Capacidad actual**: el motor de decisión es más reciente que `/qa`
> y aún no cubre todas las obligaciones tributarias — las reglas en
> `rules/` cubren IRPF residencia, IVA básico y autónomos. Para preguntas
> fuera de ese alcance, `/qa` es hoy la opción más sólida.

---

## 🛡️ Grounding gate (abstención por defecto)

La cadena de respuesta envuelve la salida del LLM en un `AnswerEnvelope` con
modo `cited`, `uncited` o `abstained`. El gate decide en función del contexto
recuperado:

- **`cited`**: al menos `GROUNDING_MIN_CITATIONS` documentos exponen `title` +
  `source_url` citables. Se muestran las fuentes.
- **`uncited`**: hay contexto pero sin metadata válida. Se marca como
  orientativo.
- **`abstained`**: no hay contexto, o el modelo emitió expresiones de duda
  ("no estoy seguro", "no tengo información…"). Se sustituye por un mensaje
  de abstención.

Variables de entorno:

```bash
export GROUNDING_MIN_CITATIONS="1"
export GROUNDING_SNIPPET_CHARS="240"
```

Uso programático:

```python
from hacienda_gpt.llm.chain import answer_with_grounding, create_openai_chain

chain = create_openai_chain(openai_api_key=...)
envelope = answer_with_grounding(chain, query="¿Tengo que declarar IRPF?")
print(envelope.mode, envelope.citations)
```

Endpoint API equivalente:

```bash
curl -X POST http://127.0.0.1:8000/qa \
  -H 'Content-Type: application/json' \
  -d '{"query": "¿Tengo que declarar IRPF si soy residente?"}'
```

---

## 🧭 Gobernanza normativa (drift detector)

Cada `DecisionRule` puede declarar las fuentes BOE/AEAT que la sustentan:

```json
"source_refs": [
  {
    "source_id": "BOE-A-2006-20764",
    "locator": "boe://BOE-A-2006-20764",
    "content_hash": "<sha256 al último review>",
    "last_reviewed_at": "2024-12-01",
    "notes": "Ley 35/2006 IRPF - residencia fiscal"
  }
]
```

El detector de drift recalcula los hashes contra el snapshot actual y emite un
reporte con findings clasificados (`ok`, `changed`, `missing`, `unverified`):

```bash
uv run python -m hacienda_gpt.cli.drift_check \
  --rules-dir rules \
  --snapshot-root ./data \
  --output reports/drift.json \
  --fail-on-changed
```

Con `--fail-on-changed`, el comando sale con código `2` cuando hay drift
crítico — ideal para gates de CI antes de promover un snapshot.

Convención de locators: `scheme://relative/path`, donde el `scheme` es el
nombre del crawler (`boe`, `aeat`, `teac`, …) y se mapea a `<snapshot-root>/<scheme>/<path>`.

---

## 📏 5) Evaluación

```bash
uv run python -m hacienda_gpt.cli.eval --output ./eval_results.json
```

La evaluación genera:

- score global promedio
- métricas por dimensión (`keyword_score`, `citation_score`, `grounding_score`)
- detalle por pregunta

---

## 🔐 Notas de seguridad

- Se usa `FAISS.load_local(..., allow_dangerous_deserialization=True)` por compatibilidad con índices serializados por LangChain/FAISS.
- Para reducir riesgo, la carga está protegida por `FAISS_TRUSTED_INDEX`.
- Si `FAISS_TRUSTED_INDEX` no está en `true`, la app rechaza cargar el índice.
- Activa `FAISS_TRUSTED_INDEX=true` **solo** con índices creados por ti o por fuentes plenamente confiables.

---

## 🧪 Tests rápidos

```bash
uv run pytest -q
```

También puedes ejecutar suites concretas, por ejemplo:

```bash
uv run pytest -q tests/decision/test_rules_engine.py
```

---

## 🛠️ Modo debug de CaseState

Si activas `DECISION_DEBUG_MODE=true`, la UI muestra en un expander:

- `facts` detectados
- `facts` faltantes por turno

El estado de cada sesión se persiste en SQLite usando `DECISION_STATE_DB_PATH`.

---

## 📌 Roadmap sugerido

- Mejorar observabilidad y métricas de retrieval/grounding.
- Extender cobertura de reglas por campañas fiscales.
- Añadir perfiles de respuesta por tipo de contribuyente.
- Consolidar pipeline CI para smoke tests de extremo a extremo.

---

## 🤝 Contribuir

1. Crea una rama desde `main`.
2. Realiza cambios pequeños y testeables.
3. Ejecuta tests locales.
4. Abre PR con contexto, alcance y riesgos.

---

## ⚠️ Descargo

HaciendaGPT es una herramienta de apoyo. **No sustituye asesoramiento fiscal profesional ni criterio jurídico oficial.**

