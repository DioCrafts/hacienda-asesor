# 🚀 HaciendaGPT

> **Asistente RAG en español especializado en consultas sobre la Agencia Tributaria de España (AEAT).**

HaciendaGPT combina recuperación semántica (RAG), reglas de decisión fiscal y una interfaz simple para ayudar a responder consultas tributarias con mayor trazabilidad, contexto y foco en seguridad.

---

## ✨ ¿Qué ofrece?

- 📚 **RAG sobre contenido fiscal** (HTML/PDF) con FAISS.
- 🧠 **Motor de reglas** para evaluación de obligaciones y casos.
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

- Python `>=3.12,<3.14`
- Poetry
- (Opcional para crawler web) Playwright/Chromium

---

## ⚙️ Setup rápido

```bash
poetry install
# opcional: cp .env.example .env
```

Variables de entorno recomendadas:

```bash
export OPENAI_API_KEY="sk-..."
export OPENAI_MODEL="gpt-4o-mini"
export OPENAI_TEMPERATURE="0"
export TOP_K="3"
export FAISS_INDEX_PATH="./data/faiss"
# Seguridad: activa solo si el índice FAISS proviene de una fuente 100% confiable
export FAISS_TRUSTED_INDEX="true"
export DECISION_DEBUG_MODE="false"
export DECISION_STATE_DB_PATH="./data/decision_state.sqlite3"
```

---

## 🕸️ 1) Crawling de contenidos

### HTML (sitio AEAT)

```bash
poetry run python -m hacienda_gpt.cli.crawler --crawler web --folder ./data/html --depth 1 --mode flat
```

### PDF

```bash
poetry run python -m hacienda_gpt.cli.crawler --crawler pdf --folder ./data/pdf --depth 1
```

> El crawler guarda por defecto en carpetas con `snapshot_date` (YYYY-MM-DD).

---

## 🧩 2) Construcción del índice FAISS

Con embeddings locales (GPT4All):

```bash
poetry run python -m hacienda_gpt.cli.processor --content-dir ./data/html --output-dir ./data/faiss --embedder gpt4all --overwrite-output
```

Con embeddings de OpenAI:

```bash
poetry run python -m hacienda_gpt.cli.processor --content-dir ./data/html --output-dir ./data/faiss --embedder openai --overwrite-output
```

---

## 💬 3) Ejecutar UI (Streamlit)

```bash
poetry run streamlit run hacienda_gpt/ui/app.py
```

---

## 🔌 4) Ejecutar API (FastAPI)

```bash
poetry run uvicorn hacienda_gpt.api.api:app --reload --host 127.0.0.1 --port 8000
```

---

## 📏 5) Evaluación

```bash
poetry run python -m hacienda_gpt.cli.eval --output ./eval_results.json
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
poetry run pytest -q
```

También puedes ejecutar suites concretas, por ejemplo:

```bash
poetry run pytest -q tests/decision/test_rules_engine.py
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

