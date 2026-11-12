# Inventario de formatos externos y vigilancia de su deriva

> Estado a 12-11-2026. Qué formatos ajenos lee la aplicación con reglas fijas, qué da por supuesto
> cada lector, cómo se veía hasta ahora un fallo silencioso y qué se ha hecho con cada uno.
> Cómo se opera: `docs/operacion.md`, sección «Deriva de formatos». Código: `hacienda_gpt/ops/formatos*.py`.

El cálculo fiscal es determinista, pero todo lo que lo alimenta se lee de sitios que cambian sin
avisar. Cuando una ley cambia, el BOE lo dice; cuando el BOE cambia **cómo** lo dice (una clave del
JSON, una ruta del XML, la redacción de un rótulo), nadie avisa y el lector devuelve «nada»: cero
disposiciones, cero artículos modificados, norma «pendiente». Ese «nada» es indistinguible de un día
tranquilo. Este inventario existe para que deje de serlo.

## Leyenda

| Columna «Estado» | Significado |
|---|---|
| **Spec + canarios** | El conocimiento del formato es un dato versionado (`hacienda_gpt/ops/data/formatos/<fuente>.json`, versión 1 = comportamiento anterior, con pruebas de identidad). El lector real informa a la vigilancia en cada lectura. El agente puede proponer una versión nueva. |
| **Canarios** | El lector informa y se comprueban invariantes, pero su conocimiento del formato sigue en el código: ante un fallo, el resultado del diagnóstico es un informe para un desarrollador. |
| **Contadores** | Documentos del usuario: solo se cuentan resultados, nunca se guarda contenido. |
| **Sin instrumentar** | Inventariado; no se vigila todavía (ver «Lo que queda»). |

## 1. BOE (alimenta todos los parámetros legales)

| Fuente (clave) | Lector | Qué da por supuesto | Fallo silencioso antes | Estado |
|---|---|---|---|---|
| Sumario diario, JSON `datosabiertos/api/boe/sumario/AAAAMMDD` (`boe_sumario`) | `proactive/novedades.py::items_del_sumario`, llamado por `revisar` (`cli proactive boe-novedades`) | `status.code = "200"`; `data.sumario.diario[].seccion[]` con `codigo` («1», «3»), `departamento`, y dentro `item` directo o bajo `epigrafe`/`texto`; cada item con `identificador`, `titulo`, `url_html`; objeto suelto donde hay un solo elemento | Cualquier clave renombrada → **lista vacía**, sin error: «0 novedades» y `ultimo_dia` avanza, de modo que esos días no se vuelven a leer | **Spec + canarios**: ninguna disposición leída; ninguna en la sección I en día laborable; identificadores con otra forma; API sin `200`; caída >80 % frente a la mediana reciente |
| Disponibilidad del sumario (`boe_sumario_dias`) | `cli/proactive.py::_get` (404 → `None`) y `revisar` | Un 404 significa «ese día no hubo BOE» | Si la URL de la API cambia, todos los días son «sin boletín» (`dias_sin_boletin`), sin error | **Canarios**: día de lunes a viernes sin sumario (uno: degradada; dos seguidos: rota). No se observa el día en curso |
| Ficha XML de una disposición, `diario_boe/xml.php?id=` (`boe_documento_xml`) | `novedades.py::leer_analisis` y `tax/vigencia_normas.py::estado_desde_xml` (`normas-check`) | `<metadatos>` con `rango`, `origen_legislativo`, `departamento`, `estatus_derogacion`, `fecha_derogacion`; `<analisis>/<materias>/<materia>`; `<referencias>/<anteriores>/<anterior referencia=…>` y `<posteriores>/<posterior>` con `<palabra>` y `<texto>`; «SE DEROGA…», «…convalidación» como vocabulario | XML mal formado → `Analisis()` vacío (excepción tragada). Rutas movidas → sin materias ni normas modificadas: la norma se queda «pendiente de análisis» 15 días y luego se clasifica solo por el título. En `estado_desde_xml`, un real decreto-ley derogado se leería como **«pendiente»** y sus parámetros seguirían aplicándose | **Spec + canarios**: XML ilegible; identificador ausente o distinto del pedido; sin rango/departamento (aviso); **ninguna** de ≥15 disposiciones de una ejecución con análisis |
| Metadatos de norma consolidada, JSON `legislacion-consolidada/id/<ID>/metadatos` (`boe_metadatos`) | `cli/boe_watch.py::_fetch_metadata` (también `params-check`) | `status.code = "200"`, `data[0]`, `fecha_actualizacion` como `AAAAMMDDTHHMMSSZ` | Si `fecha_actualizacion` se renombra: en `boe_watch` todas las normas parecen nuevas en cada ejecución (se vuelven a descargar y generan eventos espurios); en `params-check` la fecha es `None` y la norma **no cuenta ni como comprobada ni como error** | **Spec + canarios**: identificador distinto del pedido; fecha ausente o con otra forma; respuesta sin éxito |
| Texto consolidado, HTML `buscar/act.php?id=` (`boe_consolidado_html`) | `proactive/impacto.py::articulos_modificados` (`boe-impact`); `tax/verificacion.py::texto_plano` (`params-verify`, `params-recode`); `cli/boe_watch.py::_fetch_html` | Leído como líneas de texto: encabezados «Artículo N.» / «Disposición adicional…», líneas de versión «publicado el DD/MM/AAAA[, en vigor a partir del…]», notas «Se modifica…», rótulos del selector de redacciones. `texto_plano` solo supone que es HTML | Encabezados o línea de versión con otra redacción → **0 artículos modificados**: el cambio de la ley se marca «sin efecto» y nadie recibe aviso. Página de error en vez de la ley → `params-verify` dice que faltan todas las cifras (ruidoso, pero indistinguible de una derogación) | **Spec + canarios** (las cinco expresiones regulares son datos): ningún bloque; ningún bloque con fecha de versión; ningún bloque con texto; marcado sin `id="textoxslt"`/`class="bloque"` (aviso). Se observa en `articulos_modificados` y en `verificar` |
| Catálogo y descarga inicial (`cli/boe_seed.py`) | `fetch_document` | Que `act.php?id=` devuelve la norma | Guarda lo que llegue, sea o no la norma | **Sin instrumentar** (la misma página se observa después, cuando la leen `boe-impact` o `params-verify`) |
| PDF de códigos autonómicos (`crawler/boe_consolidado.py`) | `BOECCAACrawler.parse_pdf` | Nombre de fichero del código en `abrir_pdf.php`; respuesta `%PDF-` | Lo que no es PDF se descarta con un aviso en el registro de Scrapy; la ejecución termina bien | **Sin instrumentar** |

## 2. Rastreadores de doctrina y boletines

| Fuente (clave) | Lector | Qué da por supuesto | Fallo silencioso antes | Estado |
|---|---|---|---|---|
| Ficha de criterio TEAC, DYCTEA (`teac_criterio`) | `crawler/teac.py::parse_teac_criterio` | Texto «El criterio no existe» para ids vacíos; datos rotulados (dt/dd, th/td, td/td, etiqueta+valor) con una lista fija de etiquetas; número `NN/NNNNN/AAAA`, fecha `DD/MM/AAAA` | Etiquetas cambiadas → sidecar con todos los campos `null`: el documento se indexa sin metadatos de vigencia ni de vinculación | **Spec + canarios** (marcador, etiquetas y patrones son datos; las plantillas XPath siguen en el código): sin número, sin criterio, menos de 4 de 10 campos |
| Listados de boletines autonómicos: BOCM, BOJA, DOGC (`boletin_madrid_bocm`, `boletin_andalucia_boja`, `boletin_cataluna_dogc`) | `crawler/boletin_autonomico.py::parse_listing` | Resultados servidos sin JavaScript; cuatro XPath por boletín | Marcado cambiado → `results: []` en el `listing_*.json`; la araña termina sin error | **Spec + canarios** (los cuatro XPath por boletín son datos): cero resultados; más de la mitad sin título; ninguno con fecha (aviso). **Aviso**: no hay constancia de que los selectores incorporados se hayan validado contra los sitios reales (el único *fixture* es sintético) |
| Buscador de consultas DGT, Petete/Knosys (`dgt_busqueda`) | `cli/dgt_seed.py::_search_month` | `<td id="doc_N">` con `class="NUM-CONSULTA"`; `id="total_pages"`; campo oculto `id="query"` | Sin filas → «mes sin consultas»; sin `query` → cada descarga de documento falla una a una | **Canarios**: página sin filas, paginación ni `query`; filas sin `query`; filas sin número (aviso) |
| Documento de consulta DGT | `cli/dgt_seed.py::_parse_doc_num` y `_fetch_document` | `class="NUM-CONSULTA"` en el documento; número `V0000-00` | Documento sin número reconocible → se omite | **Sin instrumentar** |
| Listado de criterios TEAC por fechas | `cli/teac_seed.py::collect_criterio_ids` | Enlaces `criterio.aspx?id=…`; página vacía = fin de resultados | Enlaces con otra forma → primera página «vacía» → 0 criterios, fin normal | **Sin instrumentar** |
| Sentencias CENDOJ / Tribunal Supremo | `crawler/cendoj.py::parse_cendoj_document`, `doctrine/supremo.py`, `cli/supremo_seed.py` | «ROJ: STS n/aaaa», «ECLI:…», «Ponente:», encabezados de fundamentos | Sin ROJ/ECLI → metadatos `null`; la sentencia puede descartarse por «prioridad» | **Sin instrumentar** |
| Sede de la AEAT (HTML y PDF) | `crawler/crawlers.py` | Rutas `/Sede/irpf`, `/Sede/iva`…; enlaces seguibles; PDF servidos como bytes | Reorganización de la sede → se guardan pocas o ninguna página; la araña termina bien | **Sin instrumentar** |
| Conversión del corpus (Docling) | `processor/document_loader.py` | Que cada HTML/PDF produce fragmentos | Fichero que da 0 fragmentos: consta en el manifiesto, no avisa | **Sin instrumentar** (no es un formato externo con reglas propias) |

## 3. Documentos del usuario

| Fuente (clave) | Lector | Qué da por supuesto | Fallo silencioso antes | Estado |
|---|---|---|---|---|
| Nóminas, certificados de retenciones, datos fiscales, planes de pensiones, donativos, extractos, libros registro, modelo 130, declaración (`doc_<tipo>`) | `tax/documentos.py::analizar_documento` (modelo de lenguaje + comprobación de cita literal) | Que el *prompt* describe los rótulos con los que aparece cada dato; que la cita devuelta está en el texto | El usuario ve «no se han encontrado datos» o «se han descartado…» y lo achaca a su documento; nadie ve que le pasa a **todos** los de ese tipo | **Contadores** por tipo: sin datos; todo descartado por la cita literal; más descartes que datos. Estado por tasa (≥50 % de las últimas lecturas: degradada; ≥80 %: rota) |
| Vocabulario adicional del lector (`documentos`) | `tax/documentos.py::anexo_vocabulario` | — | — | **Spec** vacía en v1 (el *prompt* es el de siempre). Solo cambia por una propuesta revisada por una persona |

Lo que se guarda de un documento: el número de datos propuestos y los **nombres** de los campos
descartados. Nada más, salvo que el operador active `FORMAT_DOCUMENT_SKELETONS` (ver
`docs/privacidad.md`).

## 4. Datos propios que caducan

| Dato | Dónde | Fallo silencioso | Estado |
|---|---|---|---|
| Tabla de precios de modelos | `ops/data/precios_llm.json` | Precios antiguos → costes y límites en USD desfasados; modelo nuevo → coste nulo, no cuenta para el límite | `ops formatos check` avisa (en `otros_avisos`) si `consultado_el` tiene más de 120 días. No hay fuente que consultar automáticamente |
| Calendario del contribuyente | `tax/data/calendario.json` (reglas con `verificado` y `consultado_el`) | Un plazo movido por una orden ministerial no se refleja | **Sin instrumentar**: son datos legales, no un formato; les corresponde el circuito de recodificación de parámetros |

## Cómo funciona la vigilancia

1. **Canarios deterministas** (`hacienda_gpt/ops/formatos_fuentes.py`). Cada fuente declara sus
   invariantes. Los lectores reales llaman a `formatos.observar(...)` en cada lectura; esa llamada
   nunca lanza excepciones ni cambia lo que el lector devuelve.
2. **Estado** (`FORMAT_STATE_PATH`, JSON con las últimas 40 observaciones por fuente): `ok`,
   `degradada` (falló la última lectura, o una métrica cayó frente a la mediana reciente) o `rota`
   (dos lecturas seguidas con un fallo grave, o un fallo grave sin ninguna lectura buena previa).
3. **Muestras** (`FORMAT_DIR/muestras/<fuente>/`): la última muestra buena, recortada, con la
   extracción que produjo; la anterior; y la última mala. Solo de fuentes públicas.
4. **Diagnóstico agéntico** (`hacienda_gpt/ops/formatos_agente.py`): diagnóstico con cita literal
   de la muestra nueva → ensayo determinista de la spec propuesta → reflexión independiente. Si no
   converge o no es un cambio de datos: informe para un desarrollador con las muestras adjuntas.
5. **Aplicación**: `ops formatos apply` (o `FORMAT_AUTO_APPLY=true`); el ensayo se repite al aplicar.

## Garantías

- La versión 1 de cada spec reproduce el lector anterior: `tests/ops/test_formatos_specs.py` lo
  compara con copias literales del código previo (`tests/ops/legacy_parsers.py`) sobre muestras
  reales del BOE capturadas el 12-11-2026 y sobre los *fixtures* existentes.
- Una spec nueva solo entra en vigor si, **sin modelo de por medio**, (a) valida contra el esquema
  de reglas de su fuente (claves desconocidas rechazadas, expresiones regulares acotadas), (b)
  reproduce exactamente la extracción guardada de las muestras buenas y (c) cumple todos los
  canarios en la muestra nueva.
- Una spec aplicada ilegible o inválida no rompe el lector: se usa la incorporada.
- El agente no puede marcar una fuente como sana: el estado solo cambia con lecturas reales.
- Un fallo ya diagnosticado no se vuelve a diagnosticar (ni a pagar) salvo con `--force`.

## Lo que queda

1. Instrumentar: listado de `teac_seed`, documento de la DGT, CENDOJ/Supremo, sede de la AEAT,
   `boe_seed`, PDF de códigos autonómicos. Para cada uno hay arriba el supuesto y el síntoma.
2. Pasar a spec las expresiones de `dgt_seed.py` y `cendoj.py` (hoy un fallo acaba en informe).
3. `cli/proactive.py::_sumario` hace `json.loads` de la respuesta: si la API devuelve HTML de error
   con código 200, la excepción queda en `errores` de `boe-novedades` (visible), pero no llega a la
   vigilancia de formatos porque el lector no llega a ejecutarse.
4. Las plantillas XPath del TEAC y el aplanado de HTML a líneas (`_lineas`) siguen siendo código.
5. Nada se ha ejecutado contra un modelo real: ver `docs/operacion.md`, «Antes de fiarte».
