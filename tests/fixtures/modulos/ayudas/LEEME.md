# Fixtures del módulo `ayudas`

- `boe/BOE-A-*.txt`: extractos literales de los textos consolidados de www.boe.es descargados el 2026-11-13
  (solo los párrafos que citan los datos de `hacienda_gpt/modulos/ayudas/data/prestaciones_2026.json`).
- `sumarios/AAAAMMDD.json`: sumarios reales de la API de datos abiertos del BOE
  (`/datosabiertos/api/boe/sumario/AAAAMMDD`) descargados el 2026-11-13 y **recortados**: se conserva la
  estructura y solo algunos elementos de las secciones I, III y V-B (las convocatorias de interés y algo de ruido).
- `documentos/BOE-*.xml`: XML reales de `/diario_boe/xml.php?id=…`. `BOE-A-2026-23163.xml` (Programa de
  Termalismo) está recortado: solo los artículos 1 a 6 y sin tablas.
- `bdns/*.json`: **sintéticos**. La API de infosubvenciones.es no era accesible; se han construido con el formato
  documentado de `/bdnstrans/api/convocatorias?numConv=<id>` y datos coherentes con el extracto del BOE.
