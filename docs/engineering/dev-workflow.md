# Developer Workflow — Calidad local

## Objetivo
Estandarizar un pipeline local de calidad para cambios en HaciendaGPT, con foco en compatibilidad incremental y trazabilidad.

## Herramientas habilitadas
- **Format**: `black`.
- **Lint**: `ruff` (incluye import sorting con reglas de isort).
- **Type-check**: `mypy` sobre `hacienda_gpt`.
- **Test**: `pytest`.

## Comandos disponibles
Desde la raíz del repositorio:

```bash
make format
make lint
make type-check
make test
make quality
```

`make quality` ejecuta todo en secuencia (format + lint + type-check + test).

## Configuración en `pyproject.toml`
- `tool.black`: longitud de línea y versión objetivo.
- `tool.ruff`: reglas de lint y orden de imports.
- `tool.pytest.ini_options`: configuración base de test discovery.
- `tool.mypy`: reglas graduales de chequeo de tipos compatibles con el estado actual del código.

## Criterio recomendado para PR
1. Ejecutar `make quality` localmente.
2. Si falla, corregir antes de abrir PR.
3. Si existe limitación de entorno (dependencia externa, API key, etc.), documentar claramente el alcance del fallo y cómo reproducirlo.

## Decisiones de diseño
- Se adoptó una política de **endurecimiento gradual** para no romper la base actual:
  - mypy con `ignore_missing_imports = true` para dependencias externas pesadas,
  - chequeo de tipos sobre paquete principal,
  - tests descubiertos solo en `tests/`.
- `PYTHONPATH=.` en comandos para evitar errores de importación al ejecutar localmente sin instalación editable.
