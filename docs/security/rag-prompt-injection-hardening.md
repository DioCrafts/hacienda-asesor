# Hardening RAG contra prompt injection en documentos

## Estrategia
Se implementó defensa en profundidad en dos capas:

1. **Política explícita en system prompt**
   - El modelo recibe instrucción prioritaria para **ignorar instrucciones embebidas en documentos**.

2. **Sanitización de contexto recuperado**
   - Antes de pasar chunks al modelo, se redaccionan patrones comunes de inyección:
     - "ignore previous instructions"
     - "reveal system prompt"
     - "developer message"
     - "you are now"
     - "bypass", etc.

## Alcance
- Esta mitigación reduce riesgo, pero no elimina todas las variantes de prompt injection.
- Debe complementarse con auditoría continua de consultas y tests de seguridad.

## Recomendaciones operativas
- Revisar periódicamente `MALICIOUS_PATTERNS` con ejemplos reales observados.
- Mantener trazas de documentos recuperados y decisiones de filtrado para análisis forense.
