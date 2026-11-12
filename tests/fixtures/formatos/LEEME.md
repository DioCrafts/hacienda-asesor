# Muestras de formatos externos

Capturadas de www.boe.es el 12-11-2026 y recortadas con el mismo recortador que usa la vigilancia de
formatos (`hacienda_gpt/ops/formatos_fuentes.py`): primeras entradas de cada lista del sumario, ficha XML
sin el cuerpo de la disposición, primeros artículos del texto consolidado de la Ley 35/2006.

Sirven para comprobar que la spec v1 de cada fuente reproduce lo que el lector hacía antes de que su
conocimiento del formato pasara a ser un dato (`tests/ops/legacy_parsers.py`).

También de www.boe.es, el 12-11-2026:

- `calendario_boe_20261112.json`: lo que guarda la vigilancia del calendario fiscal (`calendario_boe`):
  el texto de los artículos que fijan sus plazos (art. 71 RIVA, art. 111 RIRPF, arts. 42 bis y 42 ter
  RD 1065/2007, art. 12 LIRPF y art. 30 Ley 39/2015), cortado de sus textos consolidados con
  `formatos_calendario.texto_articulo`.
- `boe_bloque_*.html`: el marcado original de dos de esos artículos (el del art. 30 lleva el espacio
  duro de «Artículo 30.»).
- `boe_seed_BOE-A-2013-954_cabecera.html`: la cabecera (`<title>`) de una norma del catálogo de `boe_seed`.

**Sintéticas** (los sitios no son accesibles desde donde se escribieron las pruebas; siguen el marcado
que describen los propios lectores, no una captura real):

- `aeat_sede_irpf_sintetica.html`: página de entrada de la sede de la AEAT.
- `dgt_busqueda_sintetica.html`, `dgt_documento_sintetico.html`: resultados y documento de Petete/Knosys,
  según el marcado documentado en `hacienda_gpt/cli/dgt_seed.py`.

Las sentencias del CENDOJ son las de `tests/fixtures/cendoj/` (también sintéticas): por las condiciones
de reutilización del CENDOJ, ni las pruebas ni la vigilancia descargan nada de poderjudicial.es.
