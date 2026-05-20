# Runbook: crawling periódico, snapshot, reindexado y promoción segura

## Objetivo
Automatizar ciclo de actualización de conocimiento:
1. crawling periódico
2. versionado por snapshot
3. reindexado controlado
4. validación de calidad
5. promoción entre entornos (`dev`, `staging`, `prod`)

## Flujo automatizado recomendado

### 1) Ejecutar pipeline de snapshot
```bash
./scripts/run_snapshot_pipeline.sh dev 2026-05-19
```

Esto hace:
- crawling web
- construcción de índice FAISS para snapshot
- evaluación de calidad (`eval_pipeline`)
- chequeo crítico de regresiones

Salida esperada en:
- `data/<env>/snapshots/<YYYY-MM-DD>/`

### 2) Promoción controlada
```bash
python -m hacienda_gpt.cli.promote_snapshot --env dev --snapshot 2026-05-19
python -m hacienda_gpt.cli.promote_snapshot --env staging --snapshot 2026-05-19
python -m hacienda_gpt.cli.promote_snapshot --env prod --snapshot 2026-05-19
```

La promoción aplica quality gates por entorno (`ops/promotion_config.json`) y mueve puntero simbólico:
- `current` -> snapshot activo
- `previous` -> snapshot previo

## Programación periódica (ejemplo)
- Cron diario en `dev`
- Promoción manual a `staging/prod` tras revisión humana

## Rollback seguro

### Rollback rápido al snapshot anterior
Si una promoción degrada calidad:
```bash
cd data/<env>
rm current
ln -s "$(readlink previous)" current
```

### Rollback con promoción explícita
Si se conoce snapshot estable:
```bash
python -m hacienda_gpt.cli.promote_snapshot --env <env> --snapshot <snapshot_estable>
```

## Checklist pre-promoción a prod
- [ ] `eval_pipeline` sin regresión relevante
- [ ] `check_critical_regressions` OK
- [ ] `unsafe_recommendation_rate` dentro de umbral prod
- [ ] Revisión manual de muestras de respuestas

## Notas de seguridad
- No promover snapshots construidos con fuentes no confiables.
- Mantener `FAISS_TRUSTED_INDEX` restringido a artefactos internos/controlados.
