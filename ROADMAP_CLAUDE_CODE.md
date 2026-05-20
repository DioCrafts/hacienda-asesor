# Roadmap Ultra Detallado (Formato Prompt Claude Code)

## Contexto del proyecto
Eres Claude Code trabajando sobre el repositorio `hacienda-gpt`.
Objetivo final: transformar la app actual (RAG conversacional sobre AEAT) en un **Asesor Fiscal Inteligente de Decisión** capaz de:
- Entender lenguaje natural en español.
- Extraer hechos fiscales estructurados.
- Detectar obligaciones tributarias potenciales (con incertidumbre explícita).
- Solicitar información faltante de forma adaptativa.
- Recomendar el mejor plan de acción para el usuario (riesgo/beneficio/plazo).
- Justificar recomendaciones con trazabilidad normativa y fuentes.

La solución debe ser profesional, escalable, auditable y segura.

---

## Reglas globales para todas las tareas (OBLIGATORIAS)

### Prompt global para Claude Code
```text
Actúa como principal engineer + staff AI architect + tax-tech product engineer.

Trabaja únicamente dentro del repositorio actual.
Antes de cada cambio:
1) Lee el código relevante y explica qué existe hoy.
2) Propón enfoque y riesgos.
3) Implementa incrementalmente en commits atómicos.
4) Añade/actualiza tests.
5) Ejecuta validaciones.
6) Documenta decisiones de diseño.

No rompas compatibilidad salvo que sea estrictamente necesario.
Si rompes compatibilidad, documenta migración.

Cada PR debe incluir:
- Qué se hizo.
- Por qué.
- Riesgos.
- Cómo probar.
- Qué queda pendiente.

No uses placeholders vacíos.
No dejes TODOs sin ticket o tarea asociada.
No inventes normativa fiscal: si falta evidencia, marca incertidumbre.
```

### Criterios de calidad transversales
- Tipado estático consistente.
- Arquitectura modular por capas.
- Tests unitarios + integración + evaluación funcional.
- Observabilidad (logs estructurados + métricas).
- Seguridad (prompt injection, deserialización, controles de confianza).
- Trazabilidad de decisiones (audit log).

---

## Estado inicial (resumen técnico)
1. Existe una cadena RAG con LangChain (`create_retrieval_chain`) y retriever contextual con FAISS + MultiQuery + compresión por embeddings.
2. La app Streamlit conversa por texto y guarda historial, pero no mantiene un estado fiscal estructurado por caso.
3. El procesamiento documental enriquece metadata útil (`document_type`, `section`, `last_updated`, etc.).
4. Hay tests básicos del retriever y piezas de UI/settings.

---

## Meta final verificable (Definition of Done global)
La aplicación final se considera completada cuando:
1. Existe `CaseState` persistente por usuario/caso.
2. Existe pipeline multi-etapa: interpretación -> reglas -> evidencia -> optimización -> respuesta.
3. Las recomendaciones incluyen confianza, hechos faltantes, y plan accionable priorizado.
4. Toda recomendación crítica está sustentada por fuentes recuperadas.
5. Existe evaluación automatizada de precisión de extracción, detección de obligaciones y grounding.
6. Hay panel/API para trazabilidad y auditoría de decisiones.
7. Existen runbooks de operación, seguridad y actualización normativa.

---

# FASE 0 — Fundaciones de ingeniería y gobernanza

## Tarea 0.1 — Crear arquitectura objetivo y ADR inicial
### Prompt Claude Code
```text
Crea un documento ADR (Architecture Decision Record) en `docs/adr/0001-decision-assistant-architecture.md` con:
- Contexto actual.
- Problemas detectados de la arquitectura vigente.
- Arquitectura objetivo por capas (UI/API, Orquestación, NLU, Reglas, Retrieval, Planner, Explainability, Storage).
- Trade-offs.
- Riesgos.
- Plan incremental de migración sin detener funcionalidad actual.

Incluye un diagrama textual (ASCII o Mermaid).
```

### Entregables
- ADR 0001.

### Validación
- Revisión de consistencia con código actual.
- Checklist de riesgos completado.

---

## Tarea 0.2 — Definir convenciones de dominio y contratos
### Prompt Claude Code
```text
Crea `docs/domain/glossary.md` y `docs/domain/contracts.md`.

Incluye:
- Glosario de términos: hecho fiscal, obligación candidata, confianza, evidencia, acción, riesgo.
- Contratos JSON iniciales para `CaseState`, `ObligationCandidate`, `ActionPlan`, `EvidenceRef`, `QuestionPrompt`.
- Política de versionado de esquemas.

Asegura que contratos sean implementables con Pydantic.
```

### Entregables
- `docs/domain/glossary.md`
- `docs/domain/contracts.md`

---

## Tarea 0.3 — Endurecer tooling de calidad
### Prompt Claude Code
```text
Configura y/o refuerza pipeline de calidad local:
- lint
- format
- type-check
- test

Si faltan herramientas (mypy/ruff/pytest config más estricto), añádelas de forma compatible con el proyecto.
Crea `Makefile` o scripts equivalentes para ejecutar todo en un comando.
Documenta en `docs/engineering/dev-workflow.md`.
```

### Entregables
- Config de calidad unificada.
- Workflow documentado.

---

# FASE 1 — Modelo de dominio y estado del caso

## Tarea 1.1 — Introducir módulo `hacienda_gpt/decision/schemas.py`
### Prompt Claude Code
```text
Implementa modelos Pydantic robustos para:
- CaseState
- Fact
- MissingFact
- ObligationCandidate
- EvidenceRef
- ActionItem
- DecisionOutput

Incluye:
- enums para estados y niveles de riesgo
- validaciones de integridad
- timestamps
- version de esquema

Añade tests unitarios exhaustivos en `tests/decision/test_schemas.py`.
```

---

## Tarea 1.2 — Diseñar repositorio de estado
### Prompt Claude Code
```text
Implementa capa de persistencia para CaseState con interfaz abstracta:
- get_case(case_id)
- save_case(case_state)
- list_cases(user_id)
- append_audit_event(case_id, event)

Crea implementación inicial SQLite en `hacienda_gpt/decision/state_store_sqlite.py`.
Mantén posibilidad de futura migración a Postgres.

Añade tests de integración para operaciones CRUD y concurrencia básica.
```

---

## Tarea 1.3 — Integrar CaseState en UI actual
### Prompt Claude Code
```text
Integra CaseState en flujo Streamlit sin romper chat existente.

Requisitos:
- generar case_id por sesión
- persistir cada turno
- mostrar (modo debug) facts detectados y facts faltantes

Añade bandera de configuración para activar/desactivar debug.
Añade tests donde sea viable y documentación de uso.
```

---

# FASE 2 — Interpretación semántica (NLU + extracción)

## Tarea 2.1 — Crear extractor de intención y hechos
### Prompt Claude Code
```text
Implementa `hacienda_gpt/decision/interpreter.py` con función principal:
`interpret_turn(user_input, chat_history, current_case_state) -> InterpretationResult`

Debe:
- detectar intención principal
- extraer hechos fiscales canónicos
- identificar incertidumbres
- proponer siguientes preguntas mínimas

Usa salida estructurada con validación fuerte.
No mezcles aquí recomendaciones finales.

Añade tests con casos variados en español coloquial.
```

---

## Tarea 2.2 — Catálogo de intents y taxonomía de hechos
### Prompt Claude Code
```text
Crea catálogo explícito en `hacienda_gpt/decision/taxonomy.py`:
- intents soportadas
- facts requeridos por intención
- facts críticos bloqueantes

Añade `docs/domain/intent-taxonomy.md` con ejemplos reales de lenguaje natural.
```

---

## Tarea 2.3 — Evaluación del extractor
### Prompt Claude Code
```text
Crea dataset inicial de evaluación en `eval_data/intent_fact_extraction.jsonl`.
Incluye al menos 100 casos sintéticos realistas.

Implementa script `hacienda_gpt/cli/eval_interpreter.py` que calcule:
- intent accuracy
- fact precision/recall/F1
- missing-fact detection recall

Exporta resultados a JSON y markdown.
```

---

# FASE 3 — Motor de reglas fiscales

## Tarea 3.1 — DSL de reglas declarativas
### Prompt Claude Code
```text
Diseña e implementa DSL de reglas en YAML/JSON en `rules/`.

Cada regla debe soportar:
- id
- jurisdicción
- periodo de vigencia
- condiciones
- hechos requeridos
- obligación candidata generada
- confianza base
- nivel de riesgo

Incluye parser y validador de reglas con tests.
```

---

## Tarea 3.2 — Evaluador de reglas
### Prompt Claude Code
```text
Implementa `hacienda_gpt/decision/rules_engine.py`.

Entrada: CaseState + facts recientes.
Salida:
- obligaciones candidatas
- motivos de activación de reglas
- facts faltantes para confirmar cada obligación

Asegura explicabilidad (rule trace).
Añade tests unitarios e integración.
```

---

## Tarea 3.3 — Versionado temporal y selección por ejercicio
### Prompt Claude Code
```text
Añade soporte para vigencia temporal:
- seleccionar reglas aplicables por año fiscal
- resolver conflictos de reglas
- registrar versión exacta usada en auditoría

Añade tests de edge cases temporales.
```

---

# FASE 4 — Retrieval orientado a evidencia legal

## Tarea 4.1 — Mejorar metadatos y filtros del índice
### Prompt Claude Code
```text
Extiende pipeline de procesamiento documental para enriquecer metadata legal:
- tipo de documento normativo
- fecha de vigencia detectada
- ámbito
- jerarquía de fuente

Ajusta chunking para preservar contexto legal útil.
Añade pruebas sobre metadata enriquecida.
```

---

## Tarea 4.2 — Retriever por modo (decisión vs explicación)
### Prompt Claude Code
```text
Implementa dos perfiles de retrieval:
1) decision_retriever: prioriza normativa/instrucciones aplicables
2) explain_retriever: prioriza claridad didáctica para usuario

Permite filtros por metadata y por año fiscal.
Añade benchmark comparativo básico de calidad de recuperación.
```

---

## Tarea 4.3 — Protección contra prompt injection documental
### Prompt Claude Code
```text
Añade hardening en cadena RAG para ignorar instrucciones maliciosas incrustadas en documentos.
Documenta estrategia y añade tests de seguridad con ejemplos de inyección.
```

---

# FASE 5 — Planificador de acciones (optimización riesgo/beneficio)

## Tarea 5.1 — Función objetivo configurable
### Prompt Claude Code
```text
Implementa `hacienda_gpt/decision/planner.py` con scoring multicriterio:
- riesgo sancionador
- urgencia/plazo
- esfuerzo usuario
- impacto de cumplimiento

Permite pesos configurables por entorno.
Añade tests de ranking y sensibilidad a pesos.
```

---

## Tarea 5.2 — Generar plan accionable priorizado
### Prompt Claude Code
```text
Dado un conjunto de obligaciones candidatas + evidencia + facts faltantes,
genera ActionPlan priorizado con:
- next best action
- checklist documental
- bloqueos
- dependencia entre pasos
- fecha objetivo

Añade tests end-to-end de planificación.
```

---

# FASE 6 — Respuesta explicable y UX conversacional avanzada

## Tarea 6.1 — Plantilla de respuesta estructurada
### Prompt Claude Code
```text
Implementa compositor de respuesta final en `hacienda_gpt/decision/explainer.py`.
Formato obligatorio:
1) Hechos detectados
2) Incertidumbres
3) Obligaciones candidatas + confianza
4) Plan recomendado
5) Fuentes
6) Próxima pregunta óptima

Debe ser claro para no expertos y trazable para auditoría.
```

---

## Tarea 6.2 — Estrategia de preguntas adaptativas mínimas
### Prompt Claude Code
```text
Implementa módulo de `question_policy` que minimice fricción:
- pregunta solo facts críticos
- evita repetir preguntas ya resueltas
- prioriza preguntas con mayor ganancia de información

Añade tests de no-redundancia y reducción de turnos.
```

---

## Tarea 6.3 — UI de evidencia y transparencia
### Prompt Claude Code
```text
Mejora Streamlit para mostrar:
- tarjetas de obligaciones candidatas
- nivel de confianza
- fuentes usadas
- qué dato falta para confirmar

Mantén diseño limpio y accesible.
```

---

# FASE 7 — API profesional y separación front/back

## Tarea 7.1 — Implementar API REST/JSON de decisión
### Prompt Claude Code
```text
Crea API backend (FastAPI recomendado) con endpoints:
- POST /cases
- POST /cases/{id}/turn
- GET /cases/{id}
- GET /cases/{id}/audit
- GET /health

Define OpenAPI y validaciones estrictas.
Añade tests de contrato de API.
```

---

## Tarea 7.2 — Adaptar UI para consumir API
### Prompt Claude Code
```text
Refactoriza UI Streamlit para consumir API en vez de lógica local directa.
Mantén fallback local opcional para desarrollo.
```

---

# FASE 8 — Evaluación integral, benchmarks y seguridad

## Tarea 8.1 — Suite de evaluación integral
### Prompt Claude Code
```text
Implementa pipeline de evaluación completa con métricas:
- intent accuracy
- fact extraction F1
- obligation precision/recall
- grounded citation rate
- unsafe recommendation rate
- turn efficiency

Genera reporte HTML/MD con tendencias.
```

---

## Tarea 8.2 — Pruebas de regresión normativa
### Prompt Claude Code
```text
Crea conjunto de casos críticos por tipo de trámite y año fiscal.
Integra en CI para detectar regresiones tras cambios de reglas o prompt.
```

---

## Tarea 8.3 — Security review técnica
### Prompt Claude Code
```text
Realiza revisión de seguridad:
- secretos
- deserialización
- inyección
- abuso de prompts
- DOS en inputs grandes

Entrega `docs/security/review.md` + plan de mitigación priorizado.
```

---

# FASE 9 — Operación, MLOps/LangOps y actualización normativa

## Tarea 9.1 — Pipeline de actualización de fuentes AEAT
### Prompt Claude Code
```text
Implementa flujo automatizado:
- crawling periódico
- versionado por snapshot
- reindexado controlado
- validación de calidad del nuevo índice
- promoción entre entornos (dev/staging/prod)

Documenta rollback seguro.
```

---

## Tarea 9.2 — Observabilidad productiva
### Prompt Claude Code
```text
Añade métricas y logs estructurados:
- latencia por etapa
- tasa de incertidumbre
- tasa de derivación a humano
- errores por módulo

Incluye dashboard base y alertas.
```

---

## Tarea 9.3 — Auditoría y cumplimiento
### Prompt Claude Code
```text
Implementa audit trail completo por recomendación:
- facts usados
- reglas disparadas
- evidencias citadas
- versión de modelos/prompts/reglas

Asegura exportación para revisión legal.
```

---

# FASE 10 — Preparación enterprise y escalado

## Tarea 10.1 — Multiusuario y control de acceso
### Prompt Claude Code
```text
Diseña autenticación/autorización para múltiples usuarios/roles.
Separa datos por tenant/usuario de forma segura.
```

---

## Tarea 10.2 — Internationalización y extensibilidad jurisdiccional
### Prompt Claude Code
```text
Estructura arquitectura para soportar nuevas jurisdicciones sin reescribir core.
Define interfaz de plugin de reglas por país.
```

---

## Tarea 10.3 — Performance & cost optimization
### Prompt Claude Code
```text
Optimiza coste/latencia:
- cachés por etapa
- batching retrieval
- selección dinámica de modelo
- límites de contexto

Entrega benchmark antes/después.
```

---

# Backlog transversal permanente (ejecutar en paralelo durante todas las fases)

## Tarea T1 — Testing continuo
### Prompt Claude Code
```text
Por cada feature nueva:
- tests unitarios
- tests de integración
- test de regresión

No merges sin cobertura razonable.
```

## Tarea T2 — Documentación viva
### Prompt Claude Code
```text
Cada módulo nuevo debe incluir:
- README técnico
- ejemplos de uso
- límites conocidos
```

## Tarea T3 — Gestión de deuda técnica
### Prompt Claude Code
```text
Mantén `docs/engineering/tech-debt.md` actualizado con:
- deuda
- impacto
- prioridad
- plan de pago
```

---

# Plan de ejecución sugerido (orden recomendado)

1. Fase 0 completa.
2. Fase 1 completa.
3. Fase 2 + 3 (iterando).
4. Fase 4.
5. Fase 5 + 6.
6. Fase 7.
7. Fase 8.
8. Fase 9.
9. Fase 10.

Regla: no avanzar fase sin criterios mínimos de calidad en la fase actual.

---

# Plantilla de prompt operativo por tarea (para copiar/pegar)

```text
TAREA: <id y título>

Objetivo:
<qué se quiere lograr>

Contexto actual:
<qué existe hoy>

Cambios requeridos:
- ...
- ...

Entregables:
- ...

Criterios de aceptación:
- ...

Validación obligatoria:
- Ejecuta tests relevantes.
- Muestra comandos ejecutados y resultado.

Restricciones:
- No romper compatibilidad sin migración documentada.
- No introducir código sin tests.
- No inventar hechos legales sin evidencia.

Salida esperada:
1) Resumen técnico
2) Archivos modificados
3) Riesgos
4) Cómo probar
5) Pendientes
```

---

# Cierre
Este roadmap está diseñado para llevar el proyecto desde su estado actual a una plataforma de asesoría fiscal conversacional de decisión con estándar profesional alto, manteniendo trazabilidad, calidad y escalabilidad.
