# Contratos de dominio (v0) — JSON implementable con Pydantic

## Objetivo
Definir contratos iniciales para el pipeline de decisión fiscal con esquemas JSON claros, versionables y compatibles con modelos Pydantic.

## Convenciones generales

- Todos los objetos raíz incluyen `schema_version` con formato semver (`major.minor.patch`).
- Timestamps en ISO-8601 UTC (`YYYY-MM-DDTHH:MM:SSZ`).
- Campos `confidence` usan rango `[0.0, 1.0]`.
- IDs estables en formato string (UUID recomendado, no obligatorio en esta fase).
- Los ejemplos son guía de estructura, no reglas normativas.

---

## 1) `CaseState`

Representa el estado acumulado de un caso de usuario.

```json
{
  "schema_version": "1.0.0",
  "case_id": "case_2026_0001",
  "user_id": "user_123",
  "status": "open",
  "jurisdiction": "ES",
  "tax_period": "2025",
  "facts": [
    {
      "fact_id": "fact_residency",
      "name": "residencia_fiscal",
      "value": "ES",
      "value_type": "string",
      "source": "user_input",
      "confidence": 0.95,
      "updated_at": "2026-05-19T10:30:00Z"
    }
  ],
  "missing_facts": [
    {
      "fact_name": "tipo_renta",
      "reason": "bloqueante_para_clasificacion",
      "priority": "high"
    }
  ],
  "obligation_candidates": [],
  "created_at": "2026-05-19T10:25:00Z",
  "updated_at": "2026-05-19T10:30:00Z"
}
```

Campos clave esperados para Pydantic:
- `status`: enum sugerido `open | in_review | closed`.
- `facts[].value_type`: enum sugerido `string | number | boolean | date | object | array`.
- `missing_facts[].priority`: enum sugerido `low | medium | high | critical`.

---

## 2) `ObligationCandidate`

Hipótesis de obligación detectada a partir de hechos y reglas.

```json
{
  "schema_version": "1.0.0",
  "obligation_id": "obl_irpf_declaracion_anual",
  "title": "Posible obligación de presentar IRPF",
  "description": "Se detectan indicios de obligación anual sujeto a validación de umbrales.",
  "jurisdiction": "ES",
  "tax_period": "2025",
  "status": "candidate",
  "risk_level": "medium",
  "confidence": 0.78,
  "trigger_facts": ["residencia_fiscal", "rendimientos_trabajo"],
  "blocking_missing_facts": ["importe_rendimientos"],
  "evidence_refs": [
    {
      "evidence_id": "ev_aeat_001",
      "source_type": "retrieved_document",
      "title": "Campaña Renta: obligación de declarar",
      "locator": "https://sede.agenciatributaria.gob.es/...",
      "snippet": "...",
      "retrieved_at": "2026-05-19T10:31:00Z",
      "confidence": 0.83
    }
  ],
  "created_at": "2026-05-19T10:31:00Z",
  "updated_at": "2026-05-19T10:31:00Z"
}
```

Enums sugeridos:
- `status`: `candidate | likely | confirmed | dismissed`.
- `risk_level`: `low | medium | high | critical`.

---

## 3) `ActionPlan`

Plan accionable priorizado para el usuario.

```json
{
  "schema_version": "1.0.0",
  "plan_id": "plan_case_2026_0001_v1",
  "case_id": "case_2026_0001",
  "summary": "Plan inicial para confirmar obligación y preparar presentación.",
  "actions": [
    {
      "action_id": "act_01",
      "title": "Confirmar tipo e importe de rendimientos",
      "description": "Recopilar certificados y clasificar ingresos por categoría fiscal.",
      "priority": 1,
      "risk_level": "high",
      "due_date": "2026-06-10",
      "depends_on": [],
      "expected_outcome": "Permite resolver hechos bloqueantes y confirmar obligación.",
      "confidence": 0.88
    },
    {
      "action_id": "act_02",
      "title": "Validar obligación de presentar IRPF",
      "description": "Reevaluar reglas con hechos completos y evidencia actualizada.",
      "priority": 2,
      "risk_level": "medium",
      "due_date": "2026-06-15",
      "depends_on": ["act_01"],
      "expected_outcome": "Determinación más confiable de obligación candidata.",
      "confidence": 0.8
    }
  ],
  "created_at": "2026-05-19T10:33:00Z",
  "updated_at": "2026-05-19T10:33:00Z"
}
```

Notas Pydantic:
- `actions[].priority` entero positivo (1 = máxima prioridad).
- `actions[].depends_on` referencia IDs de acciones existentes.

---

## 4) `EvidenceRef`

Referencia normalizada a evidencia usada en decisión.

```json
{
  "schema_version": "1.0.0",
  "evidence_id": "ev_aeat_001",
  "source_type": "retrieved_document",
  "title": "Campaña Renta: obligación de declarar",
  "locator": "https://sede.agenciatributaria.gob.es/...",
  "document_type": "normativa",
  "section": "Quién está obligado",
  "snippet": "...",
  "retrieved_at": "2026-05-19T10:31:00Z",
  "confidence": 0.83,
  "hash": "sha256:abc123..."
}
```

Enum sugerido para `source_type`:
- `retrieved_document | user_provided_document | user_statement | rule_catalog`.

---

## 5) `QuestionPrompt`

Pregunta adaptativa para cerrar hechos faltantes con mínima fricción.

```json
{
  "schema_version": "1.0.0",
  "question_id": "q_missing_income_type",
  "case_id": "case_2026_0001",
  "question_text": "¿Qué tipo de rendimientos has percibido en 2025 (trabajo, actividades económicas, capital, otros)?",
  "target_fact": "tipo_renta",
  "reason": "facto_bloqueante_para_obligacion",
  "priority": "high",
  "answer_type": "single_choice",
  "choices": ["trabajo", "actividades_economicas", "capital", "otros"],
  "is_blocking": true,
  "created_at": "2026-05-19T10:34:00Z"
}
```

Enums sugeridos:
- `priority`: `low | medium | high | critical`.
- `answer_type`: `free_text | number | date | single_choice | multi_choice | boolean`.

---

## Política de versionado de esquemas

Se adopta semver para cada contrato y para sus subestructuras críticas:

- **MAJOR** (`X.0.0`): cambio incompatible (el consumidor existente puede romperse).
  - Requiere guía de migración y ventana de compatibilidad de lectura.
- **MINOR** (`1.X.0`): campos nuevos opcionales o ampliaciones backward-compatible.
- **PATCH** (`1.0.X`): correcciones no estructurales (descripciones, restricciones no rompedoras).

### Reglas operativas

1. Todo payload persistido debe incluir `schema_version`.
2. Lectores deben, como mínimo, rechazar versiones mayores desconocidas con error explícito.
3. En cambios MAJOR, mantener estrategia de migración (por ejemplo, adapter de lectura) durante al menos una versión menor.
4. Evitar reutilizar nombres de campo con semántica distinta entre versiones.
5. Registrar en ADR o changelog de dominio cualquier cambio MAJOR/MINOR.

## Compatibilidad y alcance

- Estos contratos son iniciales (`v1.0.0`) y están orientados a implementación progresiva en Pydantic.
- No constituyen interpretación normativa; cuando falte evidencia suficiente, la capa de decisión debe expresar incertidumbre explícita.
