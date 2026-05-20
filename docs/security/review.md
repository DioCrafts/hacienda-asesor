# Revisión de seguridad — HaciendaGPT (2026-05-19)

## Alcance
Revisión estática del repositorio enfocada en:
- secretos
- deserialización
- inyección
- abuso de prompts
- DOS por inputs grandes

## Resumen ejecutivo
Estado actual: **riesgo medio** con controles iniciales útiles, pero con huecos relevantes en hardening operativo.

Prioridades inmediatas:
1. Limitar tamaño/frecuencia de inputs (API/UI) para reducir DOS.
2. Endurecer sanitización de contexto y trazabilidad de inyecciones detectadas.
3. Aislar y validar rutas de almacenamiento/carga de índices FAISS.
4. Definir políticas de secretos y rotación para despliegues.

---

## 1) Secretos

### Hallazgos
- Se usa `OPENAI_API_KEY` desde entorno (correcto), sin hardcode en código fuente.
- No se observan secretos embebidos en archivos de código/documentación revisados.
- No hay validación de “origen seguro” de variables en runtime (normal en apps self-hosted, pero requiere guía operativa).

### Riesgo
- **Medio**: riesgo de exposición en logs/configuración de entorno mal gestionada.

### Mitigaciones recomendadas
- No loggear valores de variables sensibles en ningún nivel de logging.
- Añadir `.env.example` explícito sin valores reales y política de rotación.
- Integrar escaneo de secretos en CI (por ejemplo, Gitleaks/Trufflehog).

---

## 2) Deserialización

### Hallazgos
- `FAISS.load_local(..., allow_dangerous_deserialization=True)` se usa por compatibilidad.
- Existe guardrail `FAISS_TRUSTED_INDEX` para bloquear carga si no está en `true`.

### Riesgo
- **Alto** si se habilita con índices no confiables.

### Mitigaciones recomendadas
- Mantener `FAISS_TRUSTED_INDEX=false` por defecto (ya implementado).
- Restringir path de índice a directorios permitidos y validar ownership/permisos.
- Añadir hash/firma de índice antes de carga en entornos productivos.

---

## 3) Inyección (documental/API)

### Hallazgos
- Se incorporó sanitización de contexto recuperado para patrones comunes de prompt injection.
- Se añadió guardrail en system prompt para ignorar instrucciones en documentos.
- Riesgo residual: variantes ofuscadas y ataques semánticos no cubiertos por regex.

### Riesgo
- **Medio** (defensa parcial).

### Mitigaciones recomendadas
- Añadir scoring/flag por documento sospechoso + auditoría de patrones activados.
- Extender tests adversariales multilingües/ofuscados.
- Separar claramente “contenido recuperado” de “instrucciones de sistema” en composición final.

---

## 4) Abuso de prompts

### Hallazgos
- Prompt de sistema robusto en intención, pero largo y con múltiples reglas; susceptible a drift al crecer.
- Aún no hay política centralizada de restricciones por endpoint (API/UI) para prompts de usuario.

### Riesgo
- **Medio**.

### Mitigaciones recomendadas
- Consolidar guardrails en módulo versionado (policy-as-code).
- Añadir evaluación continua de “unsafe recommendation rate” (ya medido en pipeline).
- Bloquear respuestas críticas sin evidencia (`grounded citation`) en pasos finales de decisión.

---

## 5) DOS por inputs grandes

### Hallazgos
- No hay límites estrictos de longitud en `POST /cases/{id}/turn` (`user_input` solo `min_length`).
- No hay rate limit en API.
- UI/API pueden procesar entradas grandes con coste LLM/retrieval alto.

### Riesgo
- **Alto** en despliegue público.

### Mitigaciones recomendadas
- Añadir `max_length` para `user_input` (por ejemplo 2000-4000 chars según caso).
- Añadir rate limiting por IP/usuario (middleware/proxy).
- Configurar timeouts y circuit breakers en llamadas externas.
- Rechazar payloads excesivos temprano (HTTP 413/422).

---

## Plan de mitigación priorizado

## P0 (inmediato, 1-3 días)
1. **Límites anti-DOS** en API/UI:
   - `max_length` en inputs.
   - payload caps y validación temprana.
2. **Controles de deserialización FAISS**:
   - whitelist de rutas + verificación hash opcional.
3. **Escaneo de secretos en CI**:
   - job de secret scanning obligatorio.

## P1 (corto plazo, 1-2 semanas)
1. **Rate limiting** por endpoint crítico (`/cases/{id}/turn`).
2. **Telemetry de seguridad**:
   - eventos de sanitización/flags de inyección.
3. **Suite adversarial ampliada** para prompt injection.

## P2 (medio plazo, 2-6 semanas)
1. **Policy-as-code** para guardrails de prompt y grounding.
2. **Firma de artefactos** de índice/documentos para cadena de confianza.
3. **Model risk review** periódica con métricas de seguridad y regresión.

---

## Checklist de verificación recomendada para release
- [ ] `FAISS_TRUSTED_INDEX` desactivado por defecto en producción.
- [ ] Límite de tamaño de `user_input` activo.
- [ ] Rate limiting habilitado en API gateway/reverse proxy.
- [ ] Secret scanning en CI activo y obligatorio.
- [ ] Reporte de evaluación (`eval_pipeline`) sin regresión en seguridad.
- [ ] Registro de auditoría operativo para eventos de sanitización/inyección.
