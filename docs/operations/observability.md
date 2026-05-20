# Observabilidad base y alertas

## Métricas incluidas
- latencia por etapa
- tasa de incertidumbre
- tasa de derivación a humano
- errores por módulo

Implementación base en `hacienda_gpt/observability/metrics.py`.

## Uso sugerido
```python
from hacienda_gpt.observability.metrics import MetricsCollector, emit_structured_log

metrics = MetricsCollector()
with metrics.timed_stage("interpreter"):
    ...

metrics.track_turn(uncertainty_count=1, human_handoff=False)
emit_structured_log("turn_metrics", metrics.snapshot())
```

## Dashboard y alertas
Dashboard base: `dashboards/decision_observability_dashboard.json`

Alertas recomendadas:
- incertidumbre > 35%
- derivación a humano > 25%
- pico de errores por módulo
- p95 de latencia en `rules_engine` > 1500ms
