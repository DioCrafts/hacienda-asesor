# ADR 0002: Contextual Embeddings (Anthropic Sep-2024) — pospuesto tras evaluación empírica

- **Estado**: Decidido — pospuesto
- **Fecha**: 2026-06-01
- **Decisores**: Plataforma HaciendaGPT
- **Ámbito**: Pipeline de ingesta y retrieval del RAG

## 1) Contexto

Anthropic publicó en septiembre de 2024 la técnica de *Contextual Embeddings* (CE): antes de embedder cada chunk, se le antepone una frase de 1–2 líneas generada por un LLM que sitúa al chunk dentro de su documento padre. El paper original reporta una mejora de hasta 49% en preguntas técnicas en inglés.

Durante sesiones previas de este proyecto:

- Se implementó el código en [`hacienda_gpt/processor/contextualizer.py`](../../hacienda_gpt/processor/contextualizer.py) con Qwen3-1.7B-Instruct (MLX bf16).
- El prompt fue iterado tres veces hasta llegar a 57.3% prefixes "good", 42.7% "generic", 0% hallucination sobre una muestra de 1031 chunks.
- La feature se dejó **opt-in** vía `CONTEXTUAL_EMBEDDINGS_ENABLED` (default `False`), de modo que activarla bumpea el `pipeline_fingerprint` del manifest y fuerza una re-ingesta completa del corpus.

Quedaba decidir si **pagar esa re-ingesta** (estimada en ~140-180 h sobre nuestro corpus de ~650K chunks) merecía la pena.

## 2) Decisión

**No** activar Contextual Embeddings en producción. **Sí** mantener el código en el repositorio (opt-in, deshabilitado por defecto) para poder reactivarlo si las condiciones cambian.

## 3) Evidencia empírica

Se construyó un baseline RAGAS sobre el corpus completo (11 345 archivos / 649 839 chunks) con juez local Qwen3-4B-Instruct-2507, y se compararon dos cohortes:

| Cohorte | n | `context_precision` | `answer_relevancy` | `faithfulness` |
|---|---|---|---|---|
| Baseline general (15 escenarios variados) | 8 | **0.91** | 0.27 | 0.31 |
| CE-stress (10 queries diseñadas como peor caso para no-CE) | 7 | **0.74** | 0.44 | 0.33 |

Las CE-stress queries fueron diseñadas explícitamente para forzar el modo de fallo que CE arregla: queries deliberadamente *under-specified* por el usuario (ej. "¿Qué plazo tengo para todo el papeleo?" sin nombrar el impuesto), donde el chunk ideal en el corpus también es *under-anchored* (un plazo suelto, un porcentaje en una tabla).

**Lectura**:

1. **CE sí movería la aguja en algunos casos** — la cohorte CE-stress degrada `context_precision` 17 pp respecto al baseline general (0.74 vs 0.91). Hay un efecto medible.

2. **El efecto es modesto y concentrado** — sólo 2 de 10 escenarios CE-stress tienen `ctxp` claramente bajo (`ce_umbral_extranjero`=0.45, `ce_iva_intracomunitario_modelo`=0.25). El resto rinde aceptable (≥0.7) o pasable (0.6-0.7). CE no compraría una mejora generalizada.

3. **El cuello de botella real es otro** — 3 de los 10 CE-stress fueron *abstenciones evitables* (la gate / el LLM se calló pese a tener material en corpus). Junto a las 4 abstenciones evitables del baseline general, son **7 abstenciones recuperables** sin ninguna relación con CE. Atacar ese problema es más rentable.

4. **El coste de CE es desproporcionado para el beneficio** — re-ingesta completa de 140-180 h con Qwen3-1.7B (~0.5-0.8 s por chunk × 650K chunks) para mover una métrica agregada de 0.74 → ~0.85 estimado en una cohorte sintética de 10 queries.

## 4) Implicaciones

- El código de CE queda en el repositorio (`hacienda_gpt/processor/contextualizer.py` y settings `CONTEXTUAL_EMBEDDINGS_ENABLED`, `CONTEXTUAL_MODEL_PATH`, `CONTEXTUAL_MAX_TOKENS`).
- `CONTEXTUAL_EMBEDDINGS_ENABLED` permanece `False` por defecto. Activarlo bumpea el `pipeline_fingerprint` y fuerza re-ingesta — comportamiento esperado.
- No se introducen flujos automáticos que dependan de CE.
- Las queries CE-stress (`ce_*` en [`scripts/qa_battery_test.py`](../../scripts/qa_battery_test.py)) se quedan en la batería como **regresión-tests**: si en el futuro alguien activa CE, debe medir cuánto sube `context_precision` específicamente en esa cohorte para justificar el coste.
- El esfuerzo de mejora de calidad del retrieval se redirige a:
  1. Investigar las 7 abstenciones evitables (system prompt + grounding gate).
  2. Endurecer el prompt del LLM contra el "rellenado" (faith bajo en respuestas cited).
  3. Posibles mejoras de retrieval específicas (BM25 selectivo, query rewriting) si las dos anteriores se agotan.

## 5) Cuándo revisitar esta decisión

Reabrir si **cualquiera** de las siguientes ocurre:

- El corpus crece a un orden de magnitud donde la dispersión semántica sea estructuralmente mayor (ej. añadir jurisprudencia masiva o reglamentación autonómica completa).
- Aparece una versión del contextualizer significativamente más rápida (ej. un Qwen ~0.6B con calidad de instrucción equivalente al 1.7B actual, o un modelo nativo para contextualización).
- El conjunto de CE-stress queries sube en número y diversidad y el patrón de fallo del retrieval cambia.
- Se quiere atacar un caso de uso específico donde las 2 queries problemáticas (`ce_umbral_extranjero`, `ce_iva_intracomunitario_modelo`) representan el flujo principal del usuario — en ese caso, vale la pena probar CE sobre un slice de los docs relevantes (modelo 720 + modelo 349) en vez del corpus completo.

## 6) Alternativas consideradas y descartadas

- **Activar CE inmediatamente sobre el corpus completo**: coste 140-180 h de cómputo, ROI no demostrado.
- **Activar CE sobre un slice de 500-1000 docs**: viable técnicamente, pero la decisión arriba pospone hasta que aparezca una palanca de mayor ROI o un caso de uso concreto.
- **Sustituir el embedder por uno mayor (ej. Qwen3-Embedding-8B)**: hubiera mejorado los mismos 2 casos posiblemente, pero a costa de re-ingestar igualmente Y aumentar memoria y latencia en producción.

## 7) Métricas de referencia para futuras comparaciones

Si se reabre esta decisión, comparar contra estos números (RAGAS, juez Qwen3-4B-Instruct-2507, sin CE, 2026-06-01):

- CE-stress cohort (10 queries): `ctxp=0.74` (n=7), `relev=0.44` (n=10), `faith=0.33` (n=9).
- Baseline general (15 queries): `ctxp=0.91` (n=8), `relev=0.27` (n=15), `faith=0.31` (n=12).

Una activación de CE solo está justificada si mueve `ctxp` ≥ **+10 pp** en la cohorte CE-stress sin degradar `faith` en el baseline general.
