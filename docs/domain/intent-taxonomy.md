# Taxonomía de intents y facts (v0)

## Objetivo
Definir un catálogo explícito de intents soportadas y los facts requeridos para evaluación incremental del caso.

> Esta taxonomía es de producto/ingeniería y **no constituye interpretación normativa vinculante**.
> Si falta evidencia suficiente, el sistema debe marcar incertidumbre y solicitar aclaraciones.

## Intents soportadas

- `declaracion_irpf`
- `iva`
- `autonomo`
- `generic_tributary`
- `unknown`

## Facts requeridos y facts críticos bloqueantes

### 1) `declaracion_irpf`
- Facts requeridos:
  - `residencia_fiscal`
  - `periodo_fiscal`
  - `tipo_renta`
  - `importe_renta_aproximado`
- Facts críticos bloqueantes:
  - `residencia_fiscal`
  - `periodo_fiscal`
  - `tipo_renta`

Ejemplos de lenguaje natural:
- "Oye, con lo que gané en 2025, ¿me toca hacer la renta?"
- "Soy residente en España, ¿tengo que presentar IRPF este año?"
- "No sé si estoy obligado a declarar la renta por ingresos del trabajo."

### 2) `iva`
- Facts requeridos:
  - `residencia_fiscal`
  - `alta_actividad_economica`
  - `periodicidad_iva`
  - `periodo_fiscal`
- Facts críticos bloqueantes:
  - `residencia_fiscal`
  - `alta_actividad_economica`
  - `periodicidad_iva`

Ejemplos de lenguaje natural:
- "Estoy dado de alta y no sé cuándo presentar el IVA."
- "¿El modelo 303 lo presento cada mes o por trimestre?"
- "Tengo actividad económica, ¿qué me toca de IVA ahora?"

### 3) `autonomo`
- Facts requeridos:
  - `residencia_fiscal`
  - `alta_actividad_economica`
  - `fecha_inicio_actividad`
  - `regimen_cotizacion`
- Facts críticos bloqueantes:
  - `residencia_fiscal`
  - `alta_actividad_economica`
  - `fecha_inicio_actividad`

Ejemplos de lenguaje natural:
- "Me hice autónomo hace poco, ¿qué tengo que presentar primero?"
- "Empecé actividad en marzo, ¿cómo me afecta fiscalmente?"
- "Soy autónomo nuevo y voy perdido con Hacienda."

### 4) `generic_tributary`
- Facts requeridos:
  - `residencia_fiscal`
  - `periodo_fiscal`
  - `tema_tributario`
- Facts críticos bloqueantes:
  - `residencia_fiscal`
  - `tema_tributario`

Ejemplos de lenguaje natural:
- "Tengo una duda tributaria general sobre un impuesto."
- "Quiero entender qué obligación tengo con Hacienda."

### 5) `unknown`
- Facts requeridos:
  - `tema_tributario`
- Facts críticos bloqueantes:
  - `tema_tributario`

Ejemplos de lenguaje natural:
- "Ayúdame con unos papeles, no sé por dónde empezar."
- "Tengo un problema con Hacienda pero no sé explicarlo bien."

## Notas de implementación
- El catálogo fuente está en `hacienda_gpt/decision/taxonomy.py`.
- La capa `interpreter` debe mapear cada turno a una intent y generar preguntas mínimas para cubrir facts bloqueantes.
- En caso de conflicto entre input del usuario y facts previos, debe priorizarse la trazabilidad (registrar incertidumbre y pedir confirmación).
