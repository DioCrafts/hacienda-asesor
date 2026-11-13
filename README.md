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
2. **Processor**: convierte y trocea PDF/HTML con **Docling** (análisis de layout, tablas y *chunking* por estructura + presupuesto de tokens) y construye el índice vectorial FAISS.
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
# La cadena de Q&A (`/qa` y el chat de la UI) usa el perfil "explain" por
# defecto, ya que son consultas explicativas. El perfil "decision" (más
# estricto) queda para el motor de decisión y el benchmark de retrieval.

# Embedder local MLX (Apple Silicon, mismo modelo para indexar y consultar):
export EMBEDDING_MODEL="data/models/qwen3-emb-mlx-bf16"
# export EMBEDDING_BATCH_SIZE="32"        # opcional: batch en cada forward pass
# export EMBEDDING_MAX_SEQ_LENGTH="512"   # opcional: cap defensivo de tokens

export FAISS_INDEX_PATH="./data/faiss"
# Seguridad: activa solo si el índice FAISS proviene de una fuente 100% confiable
export FAISS_TRUSTED_INDEX="true"
export DECISION_DEBUG_MODE="false"
export DECISION_STATE_DB_PATH="./data/decision_state.sqlite3"
```

### Embeddings

El proyecto usa **un único embedder local sobre MLX** (runtime nativo de
Apple Silicon, pesos bf16, **sin cuantización**). Modelo base:
`Qwen/Qwen3-Embedding-0.6B` convertido a MLX y servido desde
`data/models/qwen3-emb-mlx-bf16/`. Tanto la indexación como la consulta
construyen el embedder en `hacienda_gpt/llm/embeddings.py`, así que **no hay
riesgo de mismatch** entre el índice y el retrieval.

Por qué MLX y no PyTorch+MPS: medido **~1.35× más rápido** en M-series con
equivalencia numérica (cosine sim 0.9998 vs PyTorch). Apple Silicon-only —
el proyecto se ejecuta en Mac M-series por diseño.

#### Setup inicial (una sola vez)

```bash
# Convierte el modelo HuggingFace a MLX bf16 local.
uv run python scripts/convert_to_mlx.py
# Default: --hf-path Qwen/Qwen3-Embedding-0.6B --mlx-path data/models/qwen3-emb-mlx-bf16
```

Esto produce un directorio de ~1.1 GB que `EMBEDDING_MODEL` apunta por
defecto. Para cambiar de modelo, convierte uno nuevo con
`--hf-path`/`--mlx-path` distintos y exporta `EMBEDDING_MODEL=<nueva ruta>`.

> El LLM de chat sigue siendo OpenAI (`OPENAI_API_KEY`); solo los *embeddings*
> son locales.
>
> ⚠️ Si cambias de modelo de embedding **reconstruye el índice FAISS** (los
> espacios vectoriales no son intercambiables — el sistema incremental detecta
> el cambio vía pipeline fingerprint y fuerza un rebuild automático). Los
> umbrales de similitud viven en `settings.py` y se configuran vía
> `RETRIEVAL_DECISION_THRESHOLD` / `RETRIEVAL_EXPLAIN_THRESHOLD`. Defaults
> actuales: 0.45 / 0.35. **Calíbralos** para tu corpus con
> `uv run python -m hacienda_gpt.cli.benchmark_retrieval`.

> 🍎 **macOS**: si al primer request a `/qa` (uvicorn) o a la UI
> (Streamlit) el proceso muere con
> `OMP: Error #15: Initializing libomp.dylib, but found libomp.dylib
> already initialized`, exporta `KMP_DUPLICATE_LIB_OK=TRUE` antes de
> lanzar el servicio. La causa es que `faiss-cpu` y `docling` enlazan dos
> copias de libomp en el mismo proceso.

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

### Primera vez (full build)

```bash
uv run python -m hacienda_gpt.cli.processor \
  --content-dir ./data/html --output-dir ./data/faiss \
  --full --overwrite-output
```

### Actualizaciones (incremental, por defecto)

El processor escribe junto al índice un `manifest.json` que registra
`(sha256, size, chunk_ids)` por fichero. En runs posteriores, solo se vuelve a
parsear con Docling + embebir los ficheros **nuevos o modificados**:

```bash
uv run python -m hacienda_gpt.cli.processor \
  --content-dir ./data/html --output-dir ./data/faiss
# (--incremental es el default; --full fuerza reconstrucción)
```

- **Ficheros nuevos / modificados** → Docling + embebido + `FAISS.add_documents`.
- **Ficheros eliminados del corpus** → `FAISS.delete(ids=…)` quita sus vectores.
- **Ficheros sin cambios** → se saltan por completo (no Docling, no embebido).
- **Pipeline fingerprint cambiado** (embedder, `max_tokens` o versión de
  Docling) → reconstrucción full automática con warning, porque mezclar
  vectores de espacios distintos rompería la recuperación silenciosamente.

Resultado: un re-índice diario sobre 11k+ docs colapsa de ~12 h a minutos.

Detalles, casos límite y rationale: [`docs/incremental_indexing_plan.md`](docs/incremental_indexing_plan.md).

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

## 🤖 Agente con herramientas (`/agent`)

`/agent` es el asesor agéntico: en lugar de la cadena fija de `/qa`
(recuperar una vez → responder), el modelo decide **qué buscar y cuántas
veces**, cita cada afirmación normativa con el identificador del fragmento
que la respalda y un **verificador independiente** comprueba afirmación por
afirmación antes de etiquetar la respuesta.

```bash
curl -X POST http://127.0.0.1:8000/agent \
  -H 'Content-Type: application/json' \
  -d '{"query": "Vendí mi vivienda habitual en 2025, ¿qué plazo tengo para reinvertir?"}'
# con expediente (el agente lee/actualiza hechos y ejecuta las reglas):
#   -d '{"query": "...", "case_id": "case_..."}'
```

**Flujo de un turno** (`hacienda_gpt/agent/agent.py`):

1. El modelo llama a herramientas (`hacienda_gpt/agent/tools.py`):
   - `buscar_fuentes` — búsqueda en el corpus con filtros por tipo de fuente
     (`normativa` / `jurisprudencia` / `doctrina_administrativa` / `aeat`),
     tributo, ámbito (`estatal` / `autonomico:<ccaa>`) y `solo_vinculante`.
     Cada fragmento vuelve con un id citable (`[F1]`, `[F2]`…), su autoridad,
     fecha y carácter vinculante.
   - Con `case_id`: `consultar_expediente`, `registrar_hechos` y
     `evaluar_obligaciones` (motor de reglas determinista).
2. Redacta la respuesta citando `[F#]` en cada afirmación normativa.
3. El verificador (`hacienda_gpt/agent/verifier.py`) recibe solo la pregunta,
   la respuesta y los fragmentos citados, y dictamina cada afirmación:
   `respaldada` / `parcial` / `no_respaldada`.
4. Si hay afirmaciones sin respaldo, el agente recibe el dictamen y reescribe
   (hasta `AGENT_MAX_REVISIONS` veces).

**`mode` de la respuesta:**

| `mode` | Significado |
|---|---|
| `verified` | Todas las afirmaciones normativas están respaldadas por su fragmento citado. |
| `partially_verified` | Algunas no lo están; se listan en `warnings`. |
| `unverified` | Sin afirmaciones comprobables, o el verificador no pudo ejecutarse. |
| `clarification` | El agente necesita un dato del usuario antes de responder. |
| `abstained` | Sin base suficiente (si ninguna afirmación sobrevive, la respuesta se sustituye y el original queda en `raw_answer`). |

`POST /agent/stream` hace lo mismo emitiendo NDJSON con el progreso en vivo
(búsquedas, texto del borrador token a token, verificación, corrección) y una línea final `result` o `error`;
es lo que usa el frontend.

La respuesta incluye además `citations`, `claims` (dictamen por afirmación) y
`tool_trace` (qué buscó el agente), y con `case_id` todo ello queda en el
audit log del caso.

### Proveedor de LLM

El bucle es neutral respecto al proveedor (`hacienda_gpt/agent/providers/`):

```bash
# Claude (por defecto) — SDK oficial de Anthropic
export LLM_PROVIDER="anthropic"
export ANTHROPIC_API_KEY="sk-ant-..."
export ANTHROPIC_MODEL="claude-opus-5-5"   # por defecto
export ANTHROPIC_EFFORT="high"             # low | medium | high | xhigh | max

# OpenAI
export LLM_PROVIDER="openai"
export OPENAI_API_KEY="sk-..."
export OPENAI_AGENT_MODEL="gpt-4o"         # por defecto: OPENAI_MODEL

# Modelo local: cualquier servidor compatible con la API de OpenAI
# (Ollama, LM Studio, mlx_lm.server, vLLM, llama.cpp…)
export LLM_PROVIDER="local"
export LOCAL_LLM_BASE_URL="http://127.0.0.1:11434/v1"   # Ollama por defecto
export LOCAL_LLM_MODEL="qwen3:14b"                      # obligatorio
```

El verificador puede correr en otro proveedor/modelo que el agente
(`VERIFIER_PROVIDER`, `VERIFIER_MODEL`), p. ej. agente local + verificador
Claude. Otros ajustes: `AGENT_MAX_STEPS` (8), `AGENT_MAX_REVISIONS` (1),
`AGENT_SEARCH_DEFAULT_K` (6).

> ⚠️ Con modelos locales pequeños la calidad del *tool calling* y del
> verificador baja mucho; el bucle tolera argumentos mal formados, pero no
> sustituye a un modelo capaz.

El frontend React usa `/agent` (una llamada por turno, con el expediente
adjunto) y el extractor de hechos de `/cases` sigue a `LLM_PROVIDER`
(`DECISION_EXTRACTOR=regex|openai|provider` para forzarlo). Solo la cadena
legacy `/qa` y la UI de Streamlit siguen dependiendo de OpenAI.

---

### Calculadora del IRPF y optimización

El agente no calcula impuestos: llama a una calculadora determinista
(`hacienda_gpt/tax/irpf.py`) cuyos parámetros son **datos versionados por
ejercicio** (`hacienda_gpt/tax/data/irpf_<año>.json`), cada uno con el
precepto del que sale y un indicador `verificado`.

- **`calcular_irpf`** — rendimientos del trabajo (gastos y reducción de los
  arts. 19 y 20), base del ahorro, tributación conjunta, planes de pensiones,
  mínimo personal y familiar, escalas general y del ahorro, deducciones por
  donativos, vivienda anterior a 2013, rendimientos del trabajo (DA 61.ª) y
  maternidad, y retenciones. Admite **escenarios** para comparar alternativas
  («con y sin plan de pensiones», «individual o conjunta») y devuelve cada
  desglose como un fragmento citable `[F#]`, que el verificador comprueba
  igual que cualquier otra fuente.
- **`listar_beneficios_fiscales`** — catálogo de beneficios estatales
  contrastado con el perfil: qué está aplicado, qué está disponible (con el
  ahorro que estima la calculadora) y qué dato falta.
- **Perfil fiscal** — 20 hechos nuevos (comunidad autónoma, familia, desglose
  de rentas, aportaciones, donativos…) que el extractor reconoce, se editan en
  «Tu expediente» y alimentan la calculadora con el mismo nombre de campo.

Los parámetros de 2026 se leyeron del texto consolidado de la Ley 35/2006
(BOE-A-2006-20764, consolidado a 11-11-2026) y de la Ley 49/2002. **Límites
actuales**, que la calculadora declara en los `avisos` de cada resultado:

- **Parte autonómica: las 15 comunidades de régimen común.** Se aplican la
  escala general y, donde la comunidad los regula, los mínimos personales y
  familiares de Andalucía, Aragón, Asturias, Baleares, Canarias, Cantabria,
  Castilla-La Mancha, Castilla y León, Cataluña, Comunidad Valenciana,
  Extremadura, Galicia, La Rioja, Madrid y Murcia, leídos del texto
  consolidado de cada norma en el BOE (cada entrada guarda su identificador,
  la fecha de consolidación y el hash del HTML). Las cautelas de cada una
  (texto consolidado antiguo, mínimos calculados en Baleares, escalas de 2026
  aprobadas a mitad de año en Extremadura y la Comunidad Valenciana…) salen
  como avisos del cálculo. **Navarra y País Vasco** tienen impuesto foral
  propio y no están cubiertos: se aproximan con la escala estatal y se avisa.
  **Ceuta y Melilla**: no se aplica su deducción del art. 68.4 LIRPF.
- **Deducciones autonómicas con importe: 220 descritas con datos en las 15
  comunidades (209 en 2025), más seis de Madrid programadas a mano.** Las
  primeras viven en `deducciones_calculables` del JSON: importe fijo o
  porcentaje de un gasto, topes, base máxima, límites de renta, edad,
  discapacidad, condiciones alternativas, variantes de importe, importe según
  el orden del hijo y reducción progresiva. El esquema recoge además lo que
  antes obligaba a aproximar: tope aplicado hijo a hijo cuando se da el
  detalle de cada hijo (edad, etapa educativa, gastos), prorrateo por días de
  alquiler, de acogimiento o de desempleo, topes como porcentaje de la cuota
  íntegra autonómica o de la base liquidable, tramos de tipo, reparto del tope
  entre quienes comparten alquiler o vivienda, edad o discapacidad del
  cónyuge, límites de renta ampliados para familias numerosas, habitantes del
  municipio en vez de una casilla «rural», y grupos de deducciones
  incompatibles de los que se aplica la combinación más favorable. Los gastos
  amplios se desglosan (salud, deporte y cultura, donativos por destino,
  compra, rehabilitación e intereses de la vivienda) y cada deducción toma
  solo la parte que su ley cubre. Cada una se codificó desde su artículo en el
  BOE con cita literal comprobada, sin cifras derivadas. Si falta un dato
  decisivo (los días de alquiler, el detalle de los hijos) la deducción no se
  aplica y se dice qué falta. Lo que la calculadora no puede comprobar viaja
  con la deducción como aviso. El resto de deducciones de cada comunidad
  figura solo por título.
  En Madrid, la calculadora resta de la
  cuota autonómica las de nacimiento o adopción (art. 4), alquiler de menores
  de 40 años (art. 8), donativos a fundaciones madrileñas (art. 9), gastos
  educativos (art. 11), cuidado de menores o dependientes (art. 11 bis) y
  familias con dos o más descendientes e ingresos reducidos (art. 14), con
  los límites de renta del art. 18. Cuando una se pide y no cumple un
  requisito, el resultado explica cuál. El resto de deducciones madrileñas
  solo se listan por título. Las deducciones estatales por donativos y
  vivienda se reparten al 50 % entre cuota estatal y autonómica (arts. 67 y
  77 LIRPF) y ninguna de las dos cuotas puede quedar negativa.
- **2025** usa los mismos parámetros salvo la deducción de la DA 61.ª, cuyas
  cuantías de ese año no se pudieron contrastar y por eso no se aplica.
- Rendimientos de actividades económicas y de alquileres entran como cifra
  neta aportada por el usuario; no se calcula la compensación de pérdidas.
- El porcentaje y la base máxima de la deducción por vivienda proceden de la
  redacción de 2012 y están marcados como no verificados.

Para añadir otra comunidad, crea su entrada bajo `comunidades` en el JSON del
ejercicio (escala, mínimos si los tiene, deducciones y `fuente_principal`),
tomando la de Madrid como plantilla. `cuota_hasta_inicio` es la columna de
cuota acumulada que imprime la ley; se usa tal cual para cuadrar al céntimo.

#### Alquiler: la reducción del arrendador y la deducción del inquilino

- **Arrendador** — aporta `rendimiento_alquiler_vivienda_sin_reducir` (rentas menos
  gastos) y `arrendamiento_fecha_contrato`; la calculadora elige la redacción del
  art. 23.2 LIRPF que toca según la fecha (disposición transitoria 38.ª): 60 % para
  contratos anteriores al 26-5-2023; 90/70/60/50 % hasta el 1-12-2026; y la escala
  del Real Decreto-ley 29/2026 (del 100 % al 15 %, según rebaja o subida de la
  renta, índice de referencia, zona tensionada, edad del inquilino y gran tenedor)
  para los posteriores. Los demás datos del contrato son los campos `arrendamiento_*`.
- **Inquilino** — con `alquiler_vivienda_habitual`, la deducción estatal del
  art. 68.6 LIRPF (10 %, base máxima 11.630 €, hasta 33.007,20 € de base imponible),
  desde 2026.

Ambas dependen del Real Decreto-ley 29/2026, que el 12-11-2026 no constaba
convalidado; `irpf_2026.json` guarda en `revisiones` las normas contrastadas.

### Optimizador fiscal

Calcular cuánto puede ahorrar como máximo un hogar, dadas sus circunstancias, es
**determinista** (`hacienda_gpt/tax/optimizador.py`); lo agéntico es todo lo demás.
El catálogo de beneficios valora cada palanca por separado, pero las palancas
interactúan: una aportación a un plan de pensiones baja la base liquidable y puede
dejarla por debajo del límite de renta de una deducción autonómica, vender con
pérdidas baja la base imponible, la declaración conjunta mueve todos los umbrales y
el art. 49 LIRPF limita al 25 % la compensación entre rendimientos y ganancias. Por
eso sumar ahorros calculados por separado se equivoca en los dos sentidos. El
optimizador busca la **mejor combinación** y cada cifra que da sale de
`calcular_irpf` sobre el escenario que describe, con la misma traza de líneas y
referencias legales.

**Palancas que ofrece** — solo elecciones que la ley deja al contribuyente, con los
límites leídos de los datos del ejercicio:

| Palanca | Base legal (de los datos) | Naturaleza del ahorro |
|---|---|---|
| Conjunta o individual (matrimonio con las cifras de los dos; monoparental si `familia_monoparental`) | `tributacion_conjunta` (art. 84.2.3.º y 4.º), arts. 83 y 84.2.1.º LIRPF | ahorro definitivo |
| Aportación adicional a planes de pensiones de cada cónyuge, hasta lo que puede aportar | `prevision_social` (límite general y 30 % de rendimientos netos del trabajo y actividades, art. 52.1), cada cónyuge con su límite también en conjunta (art. 84.2.1.º) | diferimiento (tributa al rescatarlo, art. 17.2.a).3.ª) |
| Destino y donante del donativo que el usuario ya va a hacer (o hasta su presupuesto): entidad de la Ley 49/2002, la misma de los dos años anteriores (45 %), o una que dé además una deducción autonómica de su comunidad | `donativos` (art. 19 Ley 49/2002), deducciones autonómicas con base en donativos | requiere gastar (con su coste neto) |
| Vender inversiones con pérdidas latentes para compensar ganancias o, hasta el 25 %, rendimientos del capital | `compensacion_ahorro` (art. 49.1), con las reglas de recompra del art. 33.5 e), f) y g) | diferimiento |

**Descartadas**, y por qué (salen también en cada plan): aportar al plan del cónyuge
con rentas bajas (art. 51.7: sus límites no están en los datos); ampliar el límite con
aportaciones propias a un plan de empleo (depende de la empresa y de una tabla que la
calculadora no modela); elegir qué progenitor aplica el mínimo por descendientes (la
ley lo prorratea, art. 61.1.ª) o las deducciones autonómicas por hijos o vivienda (se
prorratean o corresponden a quien paga); la deducción por maternidad (corresponde a
la madre); mover cobros del trabajo (se imputan cuando son exigibles, art. 14.1.a);
invertir para deducir (empresas nuevas, eficiencia energética…: decisiones de
inversión que el usuario puede simular, no que el optimizador deba proponer); y los
donativos autonómicos cuya base se cruza con la estatal (Murcia, Cantabria: la
calculadora no hace ese cruce). Las plusvalías latentes no son palanca (nunca bajan
el impuesto del año), pero el plan avisa cuando realizarlas sale gratis porque las
absorben pérdidas pendientes.

**Búsqueda y garantías**
- Para cada palanca continua, una rejilla de candidatos: cero, el máximo permitido y
  cada importe que lleva una base vigilada justo a un umbral de los datos (límites de
  renta de las deducciones autonómicas de la comunidad, extremos de las reducciones
  progresivas, tramos de las escalas, mínimo personal, «otras rentas» de los arts. 20
  y DA 61.ª, umbrales de la deducción estatal por alquiler, tramos y topes de
  donativos, 30 % de rendimientos, compensación del art. 49), ±1 céntimo.
- Por cada modalidad de declaración, si la rejilla es pequeña (hasta 20.000
  combinaciones y dentro del presupuesto de 3.000 cálculos), se recorre **entera**
  (`busqueda.metodo = "exhaustiva"`; en las declaraciones individuales de un
  matrimonio, fijadas las palancas que las acoplan —el donativo y, si hay límites de
  renta de la unidad familiar, las pérdidas—, cada declaración se optimiza por su
  lado, lo que es exacto y mucho más barato); si no, ascenso por coordenadas reiniciado desde
  el óptimo de cada palanca por separado y desde todas al máximo (`"heuristica"`, que
  el plan declara). Después, un refinado con los umbrales recalculados en el óptimo y
  un recorte que baja cada importe al mínimo que da el mismo resultado (nunca
  comprometer más dinero del necesario).
- Desempates: menos impuesto; después menos dinero comprometido; importes redondos;
  la modalidad actual; valores menores. Sin aleatoriedad, `Decimal` en todo y orden
  estable: la misma entrada da el mismo plan (byte a byte, con cualquier
  `PYTHONHASHSEED`).
- Rendimiento medido: `calcular_irpf` tarda 0,1 ms (sin comunidad) a 0,5 ms
  (Comunidad Valenciana, la de más deducciones); un matrimonio con planes, pérdidas y
  donativo se optimiza en 10-100 ms, y el caso más pesado probado (Valenciana, dos
  hijos, los dos cónyuges con varias posiciones en pérdidas, donativo con presupuesto
  y destinos autonómicos: 1.194 cálculos) en unos 0,7 s.

**Honestidad del plan** — cada palanca elegida lleva su acción y plazo, su ahorro
**dentro del plan** (lo que se pagaría de más sin ella) y **por sí sola**, el ahorro
por euro comprometido, el coste neto si exige gastar, su referencia y los requisitos
que la calculadora no puede comprobar (con la cita literal del BOE cuando es una
regla, p. ej. la recompra de valores en dos meses del art. 33.5.f). Cuando la
combinación vale más (o menos) que la suma de las palancas por separado, el plan
explica por qué a partir de la traza: qué deducción solo activa la combinación, qué
umbral deja de superarse o qué palanca no elegiría una búsqueda palanca a palanca.
Lista también lo que no se ha podido usar (por la fecha, por las restricciones del
usuario, por datos que faltan) y los supuestos (si no dice cuánto puede aportar, se
supone el máximo útil y se dice). Después del 31 de diciembre solo quedan las
opciones de la declaración.

**Límites**: la conjunta suma las cifras de los dos cónyuges con las reglas de
`comparar_declaracion_conjunta`: un solo mínimo del contribuyente con la edad y la
discapacidad de cada cónyuge (art. 84.2.2.º), el límite de planes de pensiones de cada
uno (art. 84.2.1.º) y rentas de los hijos a cero; en las individuales, la base imponible del resto de la unidad familiar de
cada cónyuge es la del otro. Un donativo va entero a un destino y lo paga un cónyuge
(no se reparte). Las pérdidas latentes son las que indica el usuario (solo
patrimoniales: las de bonos son rendimientos del capital). Que una deducción
autonómica por donativos sea compatible con la estatal por la misma cantidad no se
ha comprobado en cada ley y se dice como requisito.

**Dónde se usa**
- Herramienta del agente `optimizar_impuestos` (parte del expediente; `datos`,
  `conyuge` y las restricciones como argumentos). Registra el plan y los cálculos
  actual y óptimo de cada declaración como fragmentos citables `[F#]`. El prompt del
  agente la usa para «¿cómo pago menos?» y «¿qué me conviene hacer antes de fin de
  año?», y le prohíbe sumar ahorros calculados por separado.
- El plan de fin de ejercicio de los agentes proactivos es el plan combinado (con el
  ahorro de cada medida dentro del plan); los donativos solo como información.
- `POST /optimize`, sin modelo de lenguaje, con las mismas reglas de acceso que
  `/simulate` (clave de API, sesión y propiedad del expediente; 404 si el
  expediente no es tuyo, 422 con una frase legible si los datos no valen).

Petición (todo opcional salvo lo que necesite el cálculo; con `case_id`, los hechos
del expediente son las cifras del usuario y `datos` las corrige; `fecha` por defecto
es hoy en España):

```json
{
  "ejercicio": 2026,
  "fecha": "2026-11-13",
  "datos": {"rendimiento_trabajo_bruto": 70000, "cotizaciones_seguridad_social": 4445, "retenciones": 15000,
            "comunidad_autonoma": "Madrid", "ganancias_patrimoniales": 1200},
  "conyuge": {"rendimiento_trabajo_bruto": 4000, "cotizaciones_seguridad_social": 254},
  "restricciones": {
    "aportacion_maxima_pensiones": 1500,
    "acepta_iliquidez": true,
    "donativo_previsto": 200,
    "perdidas_latentes": [{"descripcion": "Fondo indexado", "importe": 900, "tipo": "valores_cotizados"}]
  }
}
```

`restricciones` admite además `aportacion_maxima_pensiones_conyuge`,
`presupuesto_donativos` (máximo a donar; sin él nunca se propone donar más de lo
previsto), `donativo_recurrente_importe_anterior` y `ganancias_latentes`; cada
posición latente lleva `importe` (positivo), `tipo` (`valores_cotizados`,
`valores_no_cotizados`, `otro_elemento`), `titular` (`contribuyente` o `conyuge`) y
`descripcion`.

Respuesta (`PlanOptimo`, recortada):

```text
{
  "ejercicio": 2026, "fecha": "2026-11-13", "fase": "antes_del_cierre", "con_conyuge": true,
  "base":   {"tributacion": "individual", "resultado_total": "2874.25", "cuota_liquida_total": "…",
             "declaraciones": [{"titular": "contribuyente", "resultado": {IRPFResult}}, {"titular": "conyuge", …}]},
  "optimo": {"tributacion": "conjunta_biparental", "resultado_total": "1791.79", "declaraciones": [{"titular": "conjunta", …}]},
  "ahorro_total": "1082.46", "suma_ahorros_aislados": "857.22", "interaccion": "225.24",
  "palancas": [
    {"id": "tributacion", "accion": "Presentar declaración conjunta", "naturaleza": "ahorro_definitivo",
     "ahorro_marginal": "76.46", "ahorro_aislado": "-148.78", "referencia": "art. 84.2.3.º y 4.º LIRPF", "requisitos": […], …},
    {"id": "plan_pensiones_contribuyente", "accion": "Aportar 1.500,00 € a tu plan de pensiones antes del 31/12/2026",
     "importe": "1500", "naturaleza": "diferimiento", "compromiso": "1500", "ahorro_marginal": "645.00",
     "ahorro_aislado": "645.00", "ahorro_por_euro": "0.4300", "plazo": "2026-12-31", …},
    {"id": "plan_pensiones_conyuge", "importe": "523.80", "ahorro_marginal": "225.24", "ahorro_aislado": "0.00", …},
    {"id": "perdidas_contribuyente", "importe": "900", "ahorro_marginal": "171.00", …},
    {"id": "donativo", "importe": "200", "opcion": "autonomica:donativos_fundaciones_madrid",
     "naturaleza": "requiere_gastar", "ahorro_marginal": "190.00", "coste_neto": "10.00", …}
  ],
  "explicacion_interaccion": ["«Presentar declaración conjunta» por sí sola costaría 148,78 € más; dentro del plan ahorra 76,46 €: …", …],
  "no_disponibles": [{"id", "nombre", "causa": "fecha|restricciones|faltan_datos|sin_margen|no_calculable", "detalle", "datos_que_faltan"}],
  "descartadas": [{"id", "nombre", "motivo", "referencia"}],
  "oportunidades": […], "cambios_traza": [{"concepto", "antes", "despues", "referencia"}],
  "citas": […], "textos_legales": [{"referencia", "boe_id", "texto"}],
  "avisos": […], "supuestos": […], "datos_que_faltan": […],
  "busqueda": {"metodo": "exhaustiva", "evaluaciones": 89, "limite_evaluaciones": 3000, "combinaciones": 40, "candidatos": {…}, "nota": "…"},
  "explicacion": "Con los datos actuales, tu IRPF de 2026 sale a pagar 2.874,25 € (declaraciones individuales).\nCon el plan óptimo …"
}
```

Los importes son cadenas decimales (euros con céntimos); `ahorro_marginal` de las
palancas no suma `ahorro_total` en general, y `ahorro_aislado` puede ser negativo
(una palanca que por sí sola empeora y dentro del plan ahorra).

### Simulador, calendario y documentos

- **`POST /simulate`** — la misma calculadora, sin modelo de lenguaje: devuelve
  el escenario base, cada alternativa con su ahorro y el catálogo de
  beneficios. Es lo que usa el panel «Simulador» de la interfaz.
- **`GET /cases/{id}/calendar`** y la herramienta `calendario_fiscal` — plazos
  que aplican al perfil (cierre del ejercicio, Renta, IVA trimestral y pagos
  fraccionados si hay actividad económica, modelo 720 si hay bienes en el
  extranjero). Las fechas recurrentes están en
  `hacienda_gpt/tax/data/calendario.json` con el precepto que las fija; se
  ajustan fines de semana pero **no festivos**, y el fin de la campaña de Renta
  (30 de junio) figura como no contrastado porque lo fija una orden anual.
- **`POST /cases/{id}/documents`** — lee un documento (certificado de
  retenciones, datos fiscales, certificado de un plan…) y **propone** datos
  del perfil; nada se guarda hasta que el usuario confirma. El documento no se
  almacena, pero su contenido se envía al proveedor de LLM configurado.
  - *PDF con texto o archivo de texto*: cada propuesta lleva el pasaje literal
    del documento y se descarta si ese pasaje no aparece en él.
  - *PDF escaneado o foto (PNG, JPEG, WebP; hasta 20 páginas)*: lo lee el
    propio modelo, que debe tener visión. Como no hay texto contra el que
    comprobar, el documento se lee **dos veces** de forma independiente y solo
    se proponen los datos en que ambas lecturas coinciden, marcados como
    «leído de la imagen» para que el usuario los revise con el papel. Con
    Claude se envía el PDF tal cual; con OpenAI o un modelo local, cada página
    como imagen. Cuesta dos llamadas por documento.

### Jurisprudencia del Supremo, grafo de vigencia y conflictos

**Qué sentencias se ingieren.** Solo la Sala Tercera, Sección Segunda (la
tributaria), con prioridad para la casación posterior al 22-07-2016 que fija
doctrina. `supremo_seed` no rastrea ni busca en CENDOJ —sus condiciones
prohíben la descarga masiva—: trabaja sobre HTML que ya tengas o sobre una
lista de URLs individuales, con tope por ejecución.

```bash
# Clasificar y filtrar sentencias ya descargadas (sin red)
uv run python -m hacienda_gpt.cli.supremo_seed --from-dir ./data/cendoj --output-dir ./data/html/<snapshot>/ts
# o URLs concretas, una a una (máx. 25 por ejecución, 5 s entre peticiones)
uv run python -m hacienda_gpt.cli.supremo_seed --urls-file urls.txt --output-dir ./data/html/<snapshot>/ts
```

Cada sentencia conservada lleva en su sidecar la sección, el tipo y número de
recurso, si fija doctrina, el pasaje de doctrina y la cuestión casacional. Su
detección está probada con sentencias sintéticas, no con documentos reales de
CENDOJ: revisa el manifiesto de la primera ejecución.

**Grafo de vigencia** (`hacienda_gpt/doctrine/`). Extrae las citas entre
consultas de la DGT, resoluciones del TEAC y sentencias del TS y del TC, y
registra qué hace cada documento con el que cita: lo confirma, lo matiza, lo
supera o lo contradice. De ahí sale el estado de cada documento: `vigente`,
`matizada`, `superada` (lo revoca uno posterior de igual o mayor rango) o
`discutida` (lo contradice otro sin rango para zanjarlo).

```bash
uv run python -m hacienda_gpt.cli.doctrine_graph build              # gratis, por redacción expresa
uv run python -m hacienda_gpt.cli.doctrine_graph build --llm        # relee con el LLM las citas relevantes
uv run python -m hacienda_gpt.cli.doctrine_graph wanted --limit 50  # sentencias citadas que faltan en el corpus
uv run python -m hacienda_gpt.cli.doctrine_graph status "V1234-19"
```

`wanted` es la lista de adquisición: las sentencias en las que la DGT y el TEAC
se apoyan de verdad, ordenadas por número de citas, para conseguirlas una a
una. La lectura con LLM solo se acepta si cita literalmente las palabras del
pasaje que la justifican.

**En el agente.** Con el grafo construido (`DOCTRINE_GRAPH_PATH`):

- cada resultado de búsqueda lleva su vigencia y, si procede, una alerta
  («Superada por STS rec. …»); hay además una herramienta `consultar_vigencia`;
- una respuesta que se apoya en doctrina superada sin citar la resolución
  posterior, o que presenta como seguro un criterio discutido, se devuelve al
  agente para corregir y, si persiste, no se etiqueta como verificada;
- cuando las fuentes discrepan sin que una de mayor rango lo resuelva, el
  agente debe decir que **no hay criterio firme** y exponer las posturas; la
  respuesta lo marca en `sin_criterio_firme` y `conflicts`, y la interfaz lo
  muestra.

El grafo es un indicio deducido del texto, no una fuente: nunca se cita, y sin
`--llm` solo detecta relaciones dichas con redacción expresa.

### Modo equipo: planificador, dos investigadores y un crítico

Para una pregunta sencilla trabaja un solo agente. Solo en consultas complejas
se reparte el trabajo, porque cuesta más llamadas y más tiempo:

1. **Planificador** — descompone la consulta en encargos.
2. **Investigador de normativa** e **investigador de doctrina** — buscan en
   paralelo (solo pueden buscar y consultar la vigencia; no calculan ni tocan el
   expediente) y dejan sus fragmentos en el mismo registro de evidencia.
3. **Asesor** — redacta la respuesta con ese material.
4. **Crítico** — la revisa buscando el fallo; si objeta, el asesor la corrige.
   Después pasa el verificador de siempre.

Se activa cuando la consulta toca **dos o más impuestos** o cuando la primera
respuesta encuentra **conflicto entre fuentes** y no queda verificada.

```bash
export AGENT_TEAM_MODE="auto"       # auto (por defecto) | always | never
export SUBAGENT_MAX_STEPS="6"       # pasos de herramienta por investigador
export SUBAGENTS_PARALLEL="true"    # false si tu servidor local solo atiende una petición a la vez
```

### Agentes proactivos: avisos sin preguntar

El asesor escribe avisos en el expediente (campana de la interfaz, o
`GET /cases/{id}/notifications`). Hay tres tipos:

| Aviso | Qué hace | ¿Usa LLM? |
|-------|----------|-----------|
| `plazo` | Recuerda los vencimientos del calendario fiscal que te tocan según tu perfil | No |
| `fin_de_ejercicio` | Del 1 de octubre al 31 de diciembre recorre tus palancas (plan de pensiones, donativos, deducciones autonómicas…) con la calculadora y dice cuánto ahorra cada una antes del 31 de diciembre | No |
| `cambio_normativo` | Cuando `boe_watch` detecta que una norma ha cambiado, lee qué artículos cambian y avisa solo a los perfiles afectados | Una llamada por norma cambiada, ninguna por usuario |

Los dos primeros se recalculan también al abrir la aplicación y al cambiar un
dato del expediente (`POST /cases/{id}/proactive/run`). Para que lleguen sin
abrirla, prográmalos:

```cron
# Detectar normas cambiadas, avisar a los afectados y, después, plazos y plan de fin de año
30 6 * * *  cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.boe_watch >> logs/boe_watch.log 2>&1
45 6 * * *  cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.proactive boe-impact --changes-log data/html/_boe_changes.jsonl --snapshot-dir data/html --yes >> logs/proactive.log 2>&1
0 7 * * *   cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.proactive run-daily >> logs/proactive.log 2>&1
```

**Normas autonómicas.** La vigilancia cubre también la ley de cada una de las
15 comunidades de las que salen la escala, los mínimos y las deducciones (están
en el catálogo de `boe_watch`). Un cambio en una ley autonómica solo avisa a
quienes residen en esa comunidad, y el aviso recuerda en qué fecha se leyeron
las cifras de la calculadora. Para saber qué parámetros hay que releer:

```bash
# Una petición de metadatos por norma; sin LLM. Con --fail-on-stale termina con código 1 si hay alguna.
uv run python -m hacienda_gpt.cli.proactive params-check
```

Lista las normas que el BOE ha actualizado después de la fecha en que se
leyeron sus parámetros. Que una norma aparezca no significa que sus cifras
hayan cambiado, sino que hay que volver a leerla.

**Normas nuevas.** `boe_watch` solo ve cambios en leyes que ya seguimos. Para
no depender de eso, `boe-novedades` lee el sumario del BOE de cada día y se
queda con las disposiciones —estatales o autonómicas— que tocan impuestos,
según la clasificación por materias del propio BOE y la lista de normas que
cada una modifica. Así encuentra un decreto de vivienda que cambia la Ley del
IRPF aunque su título no lo diga, o la ley de medidas tributarias de una
comunidad cuando llega al BOE. No usa LLM.

```cron
15 7 * * *  cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.proactive boe-novedades >> logs/proactive.log 2>&1
```

Guarda un registro en `data/html/_boe_novedades.json` y retoma desde el último
día revisado. Las de relevancia alta (leyes y decretos-ley, o cualquier norma
que modifique una de las que alimentan la calculadora) generan un aviso: a los
residentes si es autonómica, o al perfil al que toca el impuesto si es estatal.
El aviso dice expresamente que la norma aún no se ha analizado. Límites: una
ley autonómica llega al BOE semanas después de publicarse en su boletín; el
análisis del BOE puede tardar unos días (la norma queda pendiente y se vuelve
a mirar); y decidir si una norma nueva se incorpora al corpus o a los
parámetros sigue siendo manual (`destacadas[].en_catalogo` lo indica).

Repetir una ejecución no duplica avisos. Los importes son estimaciones de la
calculadora con los datos del expediente, y los plazos no tienen en cuenta
festivos. Los avisos solo se guardan en la base de datos: todavía no se envían
por correo ni al móvil.

### Mantenimiento: que los parámetros no caduquen en silencio

Cada cifra de la calculadora se leyó de una ley. Esa procedencia es ahora una
prueba que se puede repetir (todo sin LLM; cron y detalles en
[`docs/operacion.md`](docs/operacion.md)):

| Comando (`python -m hacienda_gpt.cli.proactive …`) | Qué responde |
|---|---|
| `params-check` | ¿Ha actualizado el BOE alguna de las leyes de las que salen los parámetros? |
| `params-verify` | ¿Qué parámetros concretos ya no coinciden con el texto vigente? Busca la cita literal de cada deducción autonómica, la tabla de cada escala y las cifras del resto. |
| `params-rollover 2027` | Crea el ejercicio nuevo copiando el último y reverificando todo: lo que sigue coincidiendo queda verificado; lo demás se marca (`verificado: false`, que la calculadora repite en cada resultado) o se retira. |
| `normas-check` | ¿Se ha convalidado o derogado un real decreto-ley del que dependen parámetros? Si se deroga, la calculadora deja de aplicar lo que introdujo. |
| `boe-novedades` | ¿Qué normas nuevas tocan impuestos? |

Un parámetro que existe solo por un decreto-ley pendiente de convalidación lo
declara en `fuente.condicionada_a`. `python -m hacienda_gpt.cli.ops` entrega los
avisos por correo o webhook (`deliver`), ejecuta cada tarea bajo vigilancia
(`run`) y avisa si alguna falló o no llegó a ejecutarse (`doctor`). El flujo
`.github/workflows/vigilancia.yml` repite `params-check` y `params-verify` cada
semana.

Límites: una comprobación que pasa no demuestra que el parámetro sea correcto,
sino que el texto del que se leyó sigue ahí; las cifras redondas se comprueban
débilmente; y las prórrogas anuales («durante 2026…») hay que revisarlas a mano.

### Recodificación automática: un modelo propone, el control decide

Cuando `params-verify` dice que un grupo de parámetros ya no coincide con su
ley, hasta ahora alguien tenía que pedir que se releyera. `params-recode` lo
hace solo —y `params-autopilot` lo lanza sin que nadie lo pida, desde la norma
publicada en el BOE hasta el dato—, **sin que la calculadora deje de ser
determinista**: la calculadora
sigue leyendo datos; el modelo solo *propone* datos, y lo que propone no llega
a ningún fichero sin pasar controles que no dependen de él
(`hacienda_gpt/tax/recodificacion/`).

El bucle, para cada grupo que falla:

1. **Lectura.** Un modelo recibe el texto del artículo (no la ley entera), la
   codificación anterior con sus citas, el esquema —el código de los modelos
   pydantic, tal cual— y las reglas de codificación, y devuelve una propuesta
   con sus citas literales, o un «no calculable» razonado. Si necesita otro
   artículo de la misma ley (límites de renta comunes, definiciones) lo pide.
2. **Control determinista** (sin modelo): validación del esquema; cada cita,
   literal y de una pieza en el texto legal; **cada cifra codificada, dentro
   de alguna cita** (también las de variantes, tramos y ajustes, y las que
   menciona el resumen que lee el usuario); ninguna nota de «cifra derivada»;
   y la **fecha de efectos** de la redacción, impresa en una cita.
3. **Revisión independiente.** Una segunda llamada, que solo ve el texto legal
   y la propuesta, enumera discrepancias (condición omitida, unidad o base
   equivocada, umbral inclusivo o exclusivo, aproximaciones), lee por su cuenta
   la fecha de efectos y propone contribuyentes de prueba en los límites con el
   importe que da **la ley**. El programa pasa esos casos por la calculadora
   con los datos propuestos y compara.
4. Las objeciones y los casos fallidos vuelven a la lectura. Tras
   `RECODE_MAX_ITER` pasadas (3) sin acuerdo, el resultado es `sin_converger`,
   con las objeciones abiertas, y decide una persona.

El resultado es una **propuesta** en `RECODE_PROPOSALS_PATH` (antes, después,
citas, control, veredicto, casos, iteraciones, modelo, versión y huella del
texto legal) y un aviso al operador por los canales `NOTIFY_*`. Se aplica con
`params-apply <id>`, que relee la ley y repite el control: si el texto ha
cambiado desde la propuesta, o el fichero ya no contiene lo que iba a
sustituir, no escribe nada. Lo aplicado queda marcado y la calculadora lo dice
en cada resultado —«deducción recodificada automáticamente el … a partir del
BOE; pendiente de revisión humana»— hasta `params-confirm`. Con
`RECODE_AUTO_APPLY=true` se aplica sin esperar solo lo que supera el control
sin nada pendiente de revisar y con la conformidad del revisor; por defecto,
nada.

**Ejercicios.** Una propuesta nombra todos los ejercicios cuyo fichero toca, y
los deduce de la fecha de efectos, no de la fecha de publicación ni de la de
entrada en vigor: una ley publicada hoy «con efectos desde el 1 de enero» del
año anterior cambia también el fichero de ese año. El bucle lee el texto
consolidado vigente y **nunca elige por su cuenta una redacción histórica**:
si la redacción vigente no alcanza a un ejercicio, lo deja como está, lo dice,
y ese ejercicio sigue fallando en `params-verify` hasta que alguien indique la
versión aplicable (`--version AAAAMMDD`).

**Qué mantiene: todo.** El núcleo es genérico: un «grupo de parámetros» es
una ruta en un fichero de parámetros, su modelo, sus citas y la norma de la
que sale. Ningún grupo de cifras legales queda sin adaptador:

- **IRPF** (`irpf_<año>.json`): las deducciones autonómicas calculables
  (`deduccion_autonomica`), las escalas (`escala`) y **todos los demás grupos**
  con una sola clase guiada por el modelo pydantic de cada uno (`cifras:<grupo>`):
  gastos y reducción del trabajo, tributación conjunta, previsión social,
  mínimos estatales y autonómicos, deducción por rendimientos del trabajo,
  donativos, maternidad, alquiler estatal, obligación de declarar, compensación
  del ahorro, reparto de deducciones, los regímenes de la reducción por
  arrendamiento y las seis deducciones de Madrid que la calculadora calcula con
  fórmula propia. Un test recorre el modelo y falla si mañana se añade un campo
  sin adaptador.
- **Autónomos, Sociedades, Patrimonio/ITSGF e ISD** (`autonomos_`, `sociedades_`,
  `patrimonio_`, `isd_<año>.json`): un adaptador por familia y modelo
  (`<familia>:<Modelo>`, en `adaptadores_otros.py`), con las citas dentro del
  propio grupo.

Los casos límite del revisor pasan por la calculadora real: para un grupo de
cifras del IRPF, el caso es un contribuyente y **una línea del resultado de
`calcular_irpf`** («Reducción por tributación conjunta», «Mínimo por
descendientes», «Compensación entre rendimientos y ganancias del ahorro»…), y
se compara su importe; el reparto de las deducciones estatales entre las dos
cuotas, que no tiene línea propia, se prueba con **la misma función de la
calculadora** (`repartir_deducciones_estatales`). Solo la obligación de
declarar no tiene casos, porque **ninguna calculadora usa esas cifras**: pasa
el control de cifras y la lectura del revisor y **siempre queda para
confirmación humana**, igual que un grupo con cifras que imprime otra ley (el
límite del 10 % de los donativos) o un mínimo autonómico que deja importes al
Estado.
Si la nueva redacción cambia la *estructura* de la regla (otro tramo, otra
fórmula), el lector debe responder «no calculable»: eso exige cambiar la
calculadora y lo decide una persona. Lo que procede de un real decreto-ley
pendiente de convalidación conserva su `condicionada_a`.

Lo que el bucle no asume por su cuenta, y lo dice (`fuera_de_alcance`): grupos
leídos de una redacción ya no vigente (régimen transitorio de vivienda,
regímenes de arrendamiento de contratos antiguos: solo con `--version`) e
importes calculados a partir de otros (mínimos de Baleares).

**Artículos enormes.** Cinco leyes meten todas sus deducciones en un artículo
(Galicia art. 5, Valencia art. 4, Murcia art. 1, La Rioja art. 32, Cantabria
art. 2: de 45.000 a 78.000 caracteres). Al modelo se le envía solo el apartado
de la deducción («art. 5.Dos»), con el encabezado, las notas del BOE que
fechan la redacción y un índice de los demás apartados, que puede pedir (las
normas comunes suelen estar en otro). Si la numeración no permite aislar el
apartado sin dudas, se envía el artículo entero.

**Deducciones nuevas.** `params-recode --comunidad X --articulo "art. N"
--desglosar` hace que el lector *liste* las deducciones que contiene un
artículo (cada una con su cita literal, que el programa comprueba) y lanza el
bucle normal para cada una que aún no esté codificada.

**Sin que nadie lo pida: `params-autopilot`.** Un único punto de entrada,
pensado para cron diario, encadena todo:

1. **Detecta**: lee el BOE de los días pendientes (lo mismo que
   `boe-novedades`) y se queda con las normas cuyo análisis jurídico dice que
   modifican una ley de la que salen parámetros.
2. **Localiza** los artículos que cambia: los que nombra ese análisis
   («MODIFICA los arts. 5 y 9…»); si dice «determinados preceptos», los que
   las notas del texto consolidado atribuyen a la norma («Se modifica por…
   Ref. BOE-A-…») o, antes de consolidarse, los que nombran las frases de la
   propia norma («Se modifica el artículo…», «Uno. El artículo 9 queda
   redactado…», «Se añade…»), sin modelo; solo si nada de eso da un artículo,
   un modelo los **lista** con una cita literal que el programa comprueba. El
   parte dice cada artículo y por qué vía se supo. Después, **todos** los
   grupos que se leen de esos artículos —por su artículo o porque sus citas
   están allí—, en cualquier fichero, se releen **aunque su comprobación
   pase** (una condición nueva puede estar fuera de los pasajes citados): el
   lector recibe el artículo entero y la codificación vigente y puede
   responder «sin cambios», que solo vale si siguen todas las citas
   anteriores, cada cifra está en una cita y el revisor, que busca una
   condición nueva y prueba sus casos con la codificación vigente, está
   conforme. Si la norma *añade* un artículo a la ley de una comunidad, lo
   desglosa en deducciones nuevas.
3. **Lee de donde puede**: si el BOE ya ha consolidado la norma, del texto
   consolidado, con el análisis como anotación (suele llevar la cláusula de
   efectos). Si aún no —el BOE tarda días—, **del texto de la propia norma
   modificadora**; esa propuesta nunca se auto-aplica y, cuando llega la
   consolidación, el grupo se vuelve a comprobar: si lo aplicado coincide con
   el consolidado queda respaldado sin gastar nada, y si no, se relee.
4. **Verifica** todos los ficheros contra el texto vigente (sin modelo): lo
   que no coincide entra también, lo haya anunciado una norma o no; y si el
   BOE ha actualizado una ley sin que ninguna norma del registro lo explique,
   los grupos de los artículos con redacción posterior a la última lectura se
   releen como en el paso 2.
5. **Recodifica** con el bucle, bajo `RECODE_MAX_PER_RUN` y el presupuesto
   diario, y aplica lo que no necesita a nadie si `RECODE_AUTO_APPLY=true`.
6. **Anota la lectura** de cada ley actualizada que ha quedado al día (abajo).
7. **Avisa** al operador con un parte (y solo cuando algo ha cambiado).

**La fecha de lectura de cada ley.** `params-check --fail-on-stale` compara la
fecha en que el BOE actualizó una ley con `consultado_el`, la fecha en que los
ficheros la leyeron. El piloto (y `params-verify` al terminar) la anota solo,
de forma determinista, en **todos** los ficheros que citan la ley (IRPF,
autónomos, sociedades, patrimonio, ISD): nueva fecha de consolidación, huella
de la página, `consultado_el` de hoy y una línea en `revisiones`. Solo cuando todas las comprobaciones de los grupos que la
citan coinciden, ningún grupo de un artículo modificado desde la lectura
anterior queda sin releer contra el texto de hoy (ni es de los que el bucle no
relee, como unos importes calculados) y no hay nada abierto sobre
ella (propuesta pendiente o sin converger, norma en curso, grupo marcado como
no verificado sin comprobación). Nunca toca una cifra ni una fuente fijada a
una redacción con fecha; con `--snapshot-dir` solo lo simula. En la vigilancia
semanal de GitHub Actions, esas lecturas llegan al repositorio como un **pull
request** (rama `vigilancia/lecturas-boe`) que abre un trabajo aparte, el único
con permiso de escritura, después de comprobar con `params-lecturas` que en los
ficheros solo han cambiado fechas, huellas y el registro: si cambia una cifra,
una cita o una regla, la ejecución sale en rojo y no se abre nada
(`docs/operacion.md`, apartado 4).

Es idempotente (no paga dos veces por el mismo grupo y el mismo texto, ni
repite un parte) y **seguro sin clave de LLM**: los pasos 1, 2 (salvo la lista
de último recurso), 4 y 6 se ejecutan igual y el parte dice qué grupos
esperan; una ley con grupos que esperan no se anota. `boe-novedades --autopilot` (o
`RECODE_AUTOPILOT=true`) lo lanza al terminar la revisión del día.

**Antes de fiarte: `params-eval`.** Toma N codificaciones publicadas, **se las
oculta al lector**, hace que el bucle las recodifique desde la ley y compara
campo a campo con la codificación humana, con el consumo. Es la medida, con tu
modelo real, de cuánto acierta el bucle solo; úsala antes de activar la
auto-aplicación. `params-gate` pasa todas las deducciones publicadas por el
mismo control que una propuesta.

**Nada de esto se ha ejecutado contra un modelo real**: las pruebas usan un
proveedor guionizado y leyes sintéticas; contra las leyes reales solo se han
ejercitado las partes deterministas (localización de artículos, recorte de
apartados, control de cifras, verificación, el control de «sin cambios» —que
superan con sus citas guardadas las 220 deducciones autonómicas y todos los
grupos de los otros cuatro ficheros—, los artículos que una ley autonómica
real modifica según su propio texto y según las notas del consolidado, y la
anotación de la lectura sobre copias de los ficheros).

**Garantías, con cualquier modelo:** nada se escribe sin pasar el control;
toda cifra escrita está impresa en una cita literal del texto leído; el texto
con el que se aplica es el mismo con el que se propuso; la marca de
procedencia no se quita sola. **Lo que no garantiza:** que la cita sea la
pertinente (una cifra puede estar en la ley y significar otra cosa), que no se
haya omitido una condición que ni el lector ni el revisor vieron, ni que la
cláusula de efectos citada sea la que rige; ni, en un grupo de cifras, que la
regla siga teniendo la forma que la calculadora supone. Por eso existe la
revisión humana y por eso la calculadora avisa mientras no se ha hecho. Detalle y pasos en
[`docs/operacion.md`](docs/operacion.md#5-recodificación-automática-de-parámetros).

### Deriva de formatos: que una fuente cambie de forma sin que nadie se entere

Las leyes cambian y el BOE lo publica; lo que nadie publica es que una clave del JSON del sumario
se ha renombrado o que el análisis jurídico ha cambiado de sitio en el XML. El lector devuelve
entonces «nada», que es exactamente lo que devuelve un día sin novedades. `hacienda_gpt/ops/formatos.py`
convierte ese silencio en un estado visible:

- **Canarios deterministas.** Cada lector de un formato externo declara lo que da por supuesto
  (un sumario de día laborable tiene disposiciones en la sección I; una página consolidada tiene
  artículos con fecha de versión; entre quince disposiciones recientes alguna trae análisis…) y lo
  comprueba en cada lectura real. Resultado por fuente: `ok`, `degradada` o `rota`, visible en
  `ops doctor` y por los canales de aviso de siempre. Sin modelo.
- **El formato es un dato.** Las claves, rutas XML, XPath, etiquetas y expresiones regulares de
  los lectores del BOE, del TEAC (ficha y listado), de los boletines autonómicos, del CENDOJ, de la
  DGT y las rutas que sigue el rastreador de la sede de la AEAT están en specs versionadas
  (`hacienda_gpt/ops/data/formatos/*.json`). La versión 1 es el comportamiento de siempre, con
  pruebas que lo comparan con el código anterior.
- **También lo que no es un selector.** Los PDF de los códigos autonómicos (¿llega un PDF?, ¿Docling
  saca texto con encabezados de artículo?, ¿cae el rendimiento?), la ingesta del corpus, las
  ejecuciones de los rastreadores y la cabecera de las sentencias del Supremo informan con recuentos.
  Del CENDOJ solo se observan las lecturas que el operador ya ha pedido: la vigilancia no descarga
  nada de allí ni guarda el texto de ninguna sentencia, solo su esqueleto (marcado, rótulos y forma
  de los valores), que es también lo único que vería el modelo.
- **El calendario fiscal se vigila como dato que caduca.** `ops formatos check` comprueba su
  coherencia (cada plazo existe este año y el siguiente, nada pasado se muestra como próximo, cada
  modelo es conocido, lo no contrastado que se acerca) y `ops formatos probe` contrasta sus plazos con
  citas literales de los artículos del BOE que los fijan. Un cambio de redacción se adapta con una
  propuesta revisada por una persona; un cambio de plazo acaba en informe, porque el dato legal se
  corrige a mano. La campaña de la Renta no tiene fuente legible por máquina en los datos.
- **Diagnóstico agéntico.** Ante una fuente que falla, un modelo recibe los supuestos del lector,
  la última muestra bien leída y la nueva, y debe decir qué ha cambiado citando literalmente la
  muestra. Si propone una spec nueva, un **ensayo determinista** la ejecuta sobre las dos
  muestras: debe reproducir exactamente lo que se extraía de la antigua y cumplir los canarios en
  la nueva. Después, una **segunda llamada independiente** intenta refutar el diagnóstico. Lo que
  no converge, o lo que exige cambiar código, acaba en un informe para un desarrollador con las
  muestras adjuntas; la fuente sigue marcada. El agente nunca puede declarar sana una fuente.
- **Nada se aplica solo** salvo con `FORMAT_AUTO_APPLY=true` (por defecto `false`):
  `ops formatos apply|reject|confirm`.
- **Documentos de usuarios.** Solo contadores por tipo de documento; nunca contenido. El análisis
  de estructura es opcional (`FORMAT_DOCUMENT_SKELETONS`), trabaja sobre un esqueleto anónimo y
  sus propuestas las revisa siempre una persona.

```bash
uv run python -m hacienda_gpt.cli.ops formatos check              # estado, sin modelo
uv run python -m hacienda_gpt.cli.ops formatos probe              # lee ahora del BOE (y las leyes del calendario)
uv run python -m hacienda_gpt.cli.ops formatos check --diagnose   # + diagnóstico si hay modelo
uv run python -m hacienda_gpt.cli.ops formatos proposals
```

Inventario de fuentes y de lo que queda por instrumentar: `docs/formatos_inventario.md`. Operación
paso a paso: `docs/operacion.md`, sección 6. **No se ha ejecutado contra un modelo real**: las
pruebas usan un proveedor guionizado; ver «Antes de fiarte» en la guía de operación.

### Autónomos, pérdidas y declaración conjunta

- **Autónomos en estimación directa simplificada** — `calcular_rendimiento_actividad`
  (ingresos menos gastos por categoría, 5 % de difícil justificación con tope y
  reducciones del art. 32.2), `calcular_modelo_130` (pago fraccionado trimestral,
  con la minoración del art. 110.3.c y la exoneración del 70 %) y
  `calcular_modelo_303` (IVA trimestral en régimen general). Parámetros en
  `autonomos_2026.json`, cada uno con su cita. No cubren módulos, estimación
  directa normal, prorrata ni regímenes especiales de IVA, y no deciden si un
  gasto es deducible.
- **Compensación de pérdidas del ahorro (art. 49 LIRPF)** — un saldo negativo de
  rendimientos o de ganancias se compensa con el otro hasta el 25 %; se aplican
  los saldos negativos de años anteriores (`perdidas_patrimoniales_pendientes`,
  `rendimientos_negativos_pendientes`) y el resultado dice cuánto queda para los
  siguientes.
- **Conjunta o individual** — `comparar_declaracion_conjunta` calcula las dos
  declaraciones individuales y la conjunta de un matrimonio y dice cuál sale
  mejor y por cuánto.

### Otros impuestos

Cada calculadora sigue el mismo patrón que la del IRPF: parámetros en JSON
leídos del BOE, cada cifra con su cita literal y una autocomprobación que
verifica que la cita sigue en la ley. El asesor las usa como herramientas.

| Impuesto | Herramienta del asesor | Cubre | No cubre |
|---|---|---|---|
| Sociedades | `calcular_impuesto_sociedades`, `calcular_pago_fraccionado_sociedades`, `comparar_autonomo_vs_sociedad` | Sociedades pequeñas, ejercicios iniciados en 2025-2028: tipo general, escala de microempresas, reducida dimensión, nueva creación, reserva de capitalización, bases negativas | Reserva de nivelación, pago fraccionado por base con escala de microempresas, regímenes especiales |
| Patrimonio | `calcular_patrimonio` | Estado y las 15 comunidades de régimen común (mínimo exento, escala, bonificación general), límite conjunto con el IRPF e Impuesto de Solidaridad de las Grandes Fortunas | Reglas de valoración, bonificaciones de bienes concretos, no residentes |
| Sucesiones y Donaciones | `calcular_sucesiones_donaciones` | Ley estatal y la normativa de las 15 comunidades de régimen común (reducciones, tarifa, coeficientes y bonificaciones), por beneficiario | Acumulación de donaciones, usufructos, no residentes, y las reducciones autonómicas que dependen de datos que no se piden (figuran por nombre) |

La comparación autónomo/sociedad solo suma impuestos sobre la renta: no
incluye Seguridad Social ni costes de gestión, y no declara un ganador.

### Cuentas, privacidad y coste

- **Cuentas** (`AUTH_MODE=required`; por defecto `off`, modo de un solo
  usuario): registro con correo y contraseña, sesiones, expedientes por
  usuario, consentimientos, exportación y borrado de datos. Qué hace y qué no
  cubre respecto al RGPD: [`docs/privacidad.md`](docs/privacidad.md).
- **Coste**: cada llamada al modelo se registra (`GET /usage`,
  `python -m hacienda_gpt.cli.consumo resumen`). Límites opcionales:
  `LLM_DAILY_BUDGET_USD`, `LLM_DAILY_BUDGET_USD_PER_USER`,
  `LLM_MAX_CALLS_PER_QUESTION`; al agotarse, la API responde 429. Los límites
  en dólares solo cuentan modelos con precio en `ops/data/precios_llm.json`.

### Evaluación del agente con consultas de la DGT

Cada consulta vinculante de la DGT es un caso de prueba ya resuelto por un
experto: hechos, pregunta y contestación. `eval_agent` las convierte en un
conjunto de evaluación y mide el agente contra él:

```bash
# 1) Generar el conjunto (una llamada al LLM por consulta; descarta las no aptas)
uv run python -m hacienda_gpt.cli.eval_agent build \
  --dgt-dir data/html/<snapshot>/dgt-vinculantes --out eval_data/dgt_golden.jsonl \
  --sample 200 --since-year 2022

# 2) Evaluar el agente y, para comparar, la cadena /qa
uv run python -m hacienda_gpt.cli.eval_agent run --dataset eval_data/dgt_golden.jsonl \
  --system agent --out reports/eval_agent.json
uv run python -m hacienda_gpt.cli.eval_agent run --dataset eval_data/dgt_golden.jsonl \
  --system qa --out reports/eval_qa.json
```

Al evaluar el agente, la consulta de origen de cada caso **se oculta del
retrieval** (`--exclude-source`, por defecto), para que tenga que llegar a la
conclusión desde la ley y otra doctrina y no leyendo la solución. Un juez LLM
compara cada respuesta con la conclusión de la DGT (`correcta` / `parcial` /
`incorrecta` / `sin_respuesta`). La métrica a vigilar es
`wrong_when_confident`: respuestas presentadas como verificadas que
contradicen a la DGT. Ambos comandos avisan de cuántas llamadas van a hacer y
piden confirmación (`--yes` para omitirla); `--limit N` permite una prueba
corta antes de la ejecución completa.

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

### 🔁 Refresh dinámico del corpus normativo (`boe_watch`)

Las leyes fiscales **cambian**: BOE re-publica el texto consolidado bajo el mismo ID cada vez que el legislador enmienda algo. Para detectarlo sin re-descargar todo, [`boe_watch`](hacienda_gpt/cli/boe_watch.py) consulta la **API pública de metadatos** del BOE y compara la `fecha_actualizacion` con un estado local.

```bash
# Comprobación periódica (segura para ejecutar a diario; no descarga si nada cambió):
uv run python -m hacienda_gpt.cli.boe_watch

# Mostrar qué pasaría sin tocar nada:
uv run python -m hacienda_gpt.cli.boe_watch --dry-run

# Forzar el chequeo de un subset:
uv run python -m hacienda_gpt.cli.boe_watch --only BOE-A-2006-20764,BOE-A-2014-12328
```

El comando:
- Lee `hacienda_gpt/cli/boe_seed_catalog.json` y filtra entries con `track: true` (default).
- Para cada uno, llama a `https://www.boe.es/datosabiertos/api/legislacion-consolidada/id/<ID>/metadatos` (~1.4 KB JSON).
- Compara `fecha_actualizacion` con el estado local en `data/html/_boe_watch_state.json`.
- Si cambió: re-descarga el HTML consolidado a `data/html/<snapshot>/<subdir>/<ID>.html`.
- Registra el cambio en `data/html/_boe_changes.jsonl` (audit log append-only).

Cuando el script reporta `changed` o `new`, basta con relanzar el processor para que los nuevos vectores entren a FAISS:

```bash
uv run python -m hacienda_gpt.cli.processor --incremental --max-tokens 512 --num-workers 8
```

Marca `track: false` en el catálogo a documentos que **no** se consolidan (sentencias TC, RDs puntuales sin enmiendas). El watcher los ignora; `boe_seed` los descarga una sola vez.

### Automatización con launchd (macOS) / cron (Linux)

**macOS (LaunchAgent diario a las 06:00)**:

```xml
<!-- ~/Library/LaunchAgents/com.haciendagpt.boewatch.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.haciendagpt.boewatch</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/USERNAME/Documents/Repositorios/hacienda-asesor/.venv/bin/python</string>
        <string>-m</string>
        <string>hacienda_gpt.cli.boe_watch</string>
    </array>
    <key>WorkingDirectory</key><string>/Users/USERNAME/Documents/Repositorios/hacienda-asesor</string>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>
    <key>StandardOutPath</key><string>/tmp/hacienda-boe-watch.log</string>
    <key>StandardErrorPath</key><string>/tmp/hacienda-boe-watch.err</string>
</dict>
</plist>
```

Activar: `launchctl load ~/Library/LaunchAgents/com.haciendagpt.boewatch.plist`.

**Linux (crontab — diario a las 06:00)**:

```cron
0 6 * * * cd /path/to/hacienda-asesor && \
    /path/to/hacienda-asesor/.venv/bin/python -m hacienda_gpt.cli.boe_watch >> /var/log/hacienda-boe-watch.log 2>&1
```

Para auto-encadenar `boe_watch` con `processor --incremental` solo cuando hay cambios:

```bash
#!/usr/bin/env bash
set -e
cd /path/to/hacienda-asesor
out=$(.venv/bin/python -m hacienda_gpt.cli.boe_watch)
echo "$out"
if echo "$out" | grep -qE "changed=[^0]|new=[^0]"; then
    .venv/bin/python -m hacienda_gpt.cli.processor \
        --incremental --max-tokens 512 --num-workers 8
fi
```

---

## 📏 5) Evaluación

```bash
uv run python -m hacienda_gpt.cli.eval --output ./eval_results.json
```

La evaluación genera:

- score global promedio
- métricas por dimensión (`keyword_score`, `citation_score`, `grounding_score`)
- detalle por pregunta

### 🤖 Evaluación automatizada con RAGAS (juez LLM local)

Para comparar dos configuraciones de retrieval (p. ej. reranker on/off, BM25 on/off, contextual embeddings on/off) usamos RAGAS con **juez local MLX** — no llama a OpenAI ni cuesta dinero por evaluación.

```bash
# 1) baseline (config actual)
FAISS_INDEX_PATH=./data/faiss-test OPENAI_API_KEY=sk-... \
    uv run python scripts/ragas_eval.py \
    --out /tmp/hacienda-run/ragas_baseline.json --label baseline

# 2) variante (ej. desactivando reranker)
RERANKER_ENABLED=false FAISS_INDEX_PATH=./data/faiss-test OPENAI_API_KEY=sk-... \
    uv run python scripts/ragas_eval.py \
    --out /tmp/hacienda-run/ragas_no_reranker.json --label no-reranker

# 3) comparar agregados (faith / answer_relevancy / context_precision)
jq '.ragas.aggregate' /tmp/hacienda-run/ragas_baseline.json
jq '.ragas.aggregate' /tmp/hacienda-run/ragas_no_reranker.json
```

Métricas reportadas (sin ground-truth, todas judge-LLM):

| Métrica | Pregunta que responde |
|---|---|
| `faithfulness` | ¿La respuesta se apoya en los contextos recuperados? |
| `answer_relevancy` | ¿La respuesta aborda la pregunta del usuario? |
| `context_precision` | ¿Los chunks recuperados eran realmente útiles para responder? |

Notas operativas:

- El juez es **Qwen3-1.7B MLX bf16** (~3 GB). Más lento y menos preciso que `gpt-4o-mini` pero **gratis y consistente entre runs**.
- Latencia: ~15–25 s por escenario (15 escenarios → ~5 min). Tras el primer escenario el modelo queda cacheado en RAM.
- A esta escala (15 muestras) los scores son **ruidosos en términos absolutos**; úsalos como señal de **regresión** entre dos configs, no como número de referencia.
- `--samples-only` salta el paso de juez — útil para iterar el chain sin re-juzgar cada vez.
- Para ejecutar contra el corpus completo: `FAISS_INDEX_PATH=./data/faiss` (asegúrate de que el índice esté construido).

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

