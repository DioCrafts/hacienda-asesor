# Glosario de dominio — Asesor Fiscal Inteligente

## Propósito
Este glosario define términos canónicos para reducir ambigüedad entre producto, ingeniería y evaluación.

## Términos clave

### Hecho fiscal
Dato estructurado y verificable que describe una circunstancia relevante para análisis tributario.

- Ejemplos: residencia fiscal declarada, tipo de renta, fecha de devengo, régimen aplicable.
- Debe incluir, cuando sea posible: fuente (usuario/sistema/documento), confianza, timestamp.
- Si no hay evidencia suficiente, debe marcarse como incierto o pendiente de confirmación.

### Obligación candidata
Hipótesis de obligación tributaria potencial detectada por reglas y/o inferencia controlada, pendiente de confirmación total o parcial.

- No implica obligación definitiva si faltan hechos críticos.
- Debe incluir: razón de activación, hechos faltantes, riesgo, confianza y evidencia asociada.

### Confianza
Estimación cuantitativa o cualitativa de robustez de un hecho, inferencia o recomendación.

- Se representa inicialmente en rango numérico `[0.0, 1.0]`.
- `0.0` implica nula confianza; `1.0` implica máxima confianza operacional.
- No sustituye verificación normativa ni validación humana en casos críticos.

### Evidencia
Referencia trazable a material de soporte (normativa, criterio administrativo, documento recuperado, entrada del usuario) que sustenta un hecho, obligación o acción.

- Debe contener identificador estable, tipo de fuente y localizador (URL, doc_id, sección, etc.).
- En recomendaciones críticas, la ausencia de evidencia debe marcar incertidumbre explícita.

### Acción
Paso concreto recomendado al usuario para reducir riesgo, cumplir obligación o completar información faltante.

- Debe ser ejecutable y priorizable.
- Suele incluir: prioridad, plazo objetivo, precondiciones, impacto esperado.

### Riesgo
Nivel de exposición esperado (sancionador, económico, de cumplimiento o de incertidumbre) asociado a no actuar, actuar incorrectamente o decidir con información incompleta.

- Se representa inicialmente con niveles discretos: `low`, `medium`, `high`, `critical`.
- Debe acompañarse de explicación breve y factores determinantes.

## Notas de uso
- Este glosario no constituye interpretación normativa vinculante.
- Si la evidencia fiscal es insuficiente, debe prevalecer el estado de incertidumbre sobre afirmaciones categóricas.
