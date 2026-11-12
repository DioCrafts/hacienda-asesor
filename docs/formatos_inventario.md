# Inventario de formatos externos y vigilancia de su deriva

> Estado a 12-11-2026 (segunda tanda: CENDOJ, AEAT, códigos autonómicos, Docling, calendario, semillas). Qué formatos ajenos lee la aplicación con reglas fijas, qué da por supuesto
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
| **Contadores** | Solo se cuentan resultados de una ejecución (o de los documentos del usuario, sin guardar nunca su contenido). |
| **Sin instrumentar** | Inventariado; no se vigila todavía (ver «Lo que queda»). |

## 1. BOE (alimenta todos los parámetros legales)

| Fuente (clave) | Lector | Qué da por supuesto | Fallo silencioso antes | Estado |
|---|---|---|---|---|
| Sumario diario, JSON `datosabiertos/api/boe/sumario/AAAAMMDD` (`boe_sumario`) | `proactive/novedades.py::items_del_sumario`, llamado por `revisar` (`cli proactive boe-novedades`) | `status.code = "200"`; `data.sumario.diario[].seccion[]` con `codigo` («1», «3»), `departamento`, y dentro `item` directo o bajo `epigrafe`/`texto`; cada item con `identificador`, `titulo`, `url_html`; objeto suelto donde hay un solo elemento | Cualquier clave renombrada → **lista vacía**, sin error: «0 novedades» y `ultimo_dia` avanza, de modo que esos días no se vuelven a leer | **Spec + canarios**: ninguna disposición leída; ninguna en la sección I en día laborable; identificadores con otra forma; API sin `200`; caída >80 % frente a la mediana reciente |
| Disponibilidad del sumario (`boe_sumario_dias`) | `cli/proactive.py::_get` (404 → `None`) y `revisar` | Un 404 significa «ese día no hubo BOE» | Si la URL de la API cambia, todos los días son «sin boletín» (`dias_sin_boletin`), sin error | **Canarios**: día de lunes a viernes sin sumario (uno: degradada; dos seguidos: rota). No se observa el día en curso |
| Ficha XML de una disposición, `diario_boe/xml.php?id=` (`boe_documento_xml`) | `novedades.py::leer_analisis` y `tax/vigencia_normas.py::estado_desde_xml` (`normas-check`) | `<metadatos>` con `rango`, `origen_legislativo`, `departamento`, `estatus_derogacion`, `fecha_derogacion`; `<analisis>/<materias>/<materia>`; `<referencias>/<anteriores>/<anterior referencia=…>` y `<posteriores>/<posterior>` con `<palabra>` y `<texto>`; «SE DEROGA…», «…convalidación» como vocabulario | XML mal formado → `Analisis()` vacío (excepción tragada). Rutas movidas → sin materias ni normas modificadas: la norma se queda «pendiente de análisis» 15 días y luego se clasifica solo por el título. En `estado_desde_xml`, un real decreto-ley derogado se leería como **«pendiente»** y sus parámetros seguirían aplicándose | **Spec + canarios**: XML ilegible; identificador ausente o distinto del pedido; sin rango/departamento (aviso); **ninguna** de ≥15 disposiciones de una ejecución con análisis |
| Metadatos de norma consolidada, JSON `legislacion-consolidada/id/<ID>/metadatos` (`boe_metadatos`) | `cli/boe_watch.py::_fetch_metadata` (también `params-check`) | `status.code = "200"`, `data[0]`, `fecha_actualizacion` como `AAAAMMDDTHHMMSSZ` | Si `fecha_actualizacion` se renombra: en `boe_watch` todas las normas parecen nuevas en cada ejecución (se vuelven a descargar y generan eventos espurios); en `params-check` la fecha es `None` y la norma **no cuenta ni como comprobada ni como error** | **Spec + canarios**: identificador distinto del pedido; fecha ausente o con otra forma; respuesta sin éxito |
| Texto consolidado, HTML `buscar/act.php?id=` (`boe_consolidado_html`) | `proactive/impacto.py::articulos_modificados` (`boe-impact`); `tax/verificacion.py::texto_plano` (`params-verify`, `params-recode`); `cli/boe_watch.py::_fetch_html` | Leído como líneas de texto: encabezados «Artículo N.» / «Disposición adicional…», líneas de versión «publicado el DD/MM/AAAA[, en vigor a partir del…]», notas «Se modifica…», rótulos del selector de redacciones. `texto_plano` solo supone que es HTML | Encabezados o línea de versión con otra redacción → **0 artículos modificados**: el cambio de la ley se marca «sin efecto» y nadie recibe aviso. Página de error en vez de la ley → `params-verify` dice que faltan todas las cifras (ruidoso, pero indistinguible de una derogación) | **Spec + canarios** (las cinco expresiones regulares son datos): ningún bloque; ningún bloque con fecha de versión; ningún bloque con texto; marcado sin `id="textoxslt"`/`class="bloque"` (aviso). Se observa en `articulos_modificados` y en `verificar` |
| Catálogo y descarga inicial (`boe_seed`) | `cli/boe_seed.py::fetch_document` | Que `act.php?id=` devuelve la norma (una no consolidada redirige a `doc.php`, que también vale) | Guarda lo que llegue, sea o no la norma | **Canarios**: el `<title>` de lo guardado no lleva el identificador pedido. No se le aplican los canarios de `boe_consolidado_html`: una norma nunca modificada o no consolidada daría falsas alarmas |
| PDF de códigos autonómicos (`boe_ccaa_pdf`) | `crawler/boe_consolidado.py::BOECCAACrawler` (`parse_pdf`, `handle_error`, `closed`) | Nombre de fichero del código en `abrir_pdf.php`; respuesta `%PDF-` con estado 200 | Lo que no es PDF se descarta con un aviso en el registro de Scrapy; un 404 solo queda en el registro; la ejecución termina bien | **Canarios** (una observación por ejecución): respuesta que no es PDF (se guarda su comienzo); 404 de un código con nombre verificado (grave) o sin verificar (aviso); ningún código guardado. Una ejecución sin red no se registra |
| Texto de esos PDF (`boe_ccaa_texto`) | `processor/document_loader.py` (Docling), contado en `ops/formatos_corpus.py` | Que cada PDF da fragmentos con texto y conserva los encabezados de artículo | Un PDF sin capa de texto o que Docling ya no entiende se indexa con 0 fragmentos; consta en el manifiesto y nadie lo ve | **Canarios** (una observación por ingesta): «PDF descargado pero sin texto extraído»; «texto sin ningún encabezado *Artículo N* / *Disposición …*»; caída de más del 50 % de los caracteres por KB de PDF frente a la mediana reciente. La muestra es el comienzo del texto de cada código |

## 2. Rastreadores de doctrina y boletines

| Fuente (clave) | Lector | Qué da por supuesto | Fallo silencioso antes | Estado |
|---|---|---|---|---|
| Ficha de criterio TEAC, DYCTEA (`teac_criterio`) | `crawler/teac.py::parse_teac_criterio`, llamado por `cli/teac_seed.py::fetch_criterio` (la vía real) y por el rastreador Scrapy | Texto «El criterio no existe» para ids vacíos; datos rotulados (dt/dd, th/td, td/td, etiqueta+valor) con una lista fija de etiquetas; número `NN/NNNNN/AAAA`, fecha `DD/MM/AAAA` | Etiquetas cambiadas → sidecar con todos los campos `null`: el documento se indexa sin metadatos de vigencia ni de vinculación | **Spec + canarios** (marcador, etiquetas, patrones y las cinco plantillas XPath, en grupos, son datos): sin número, sin criterio, menos de 4 de 10 campos. Antes solo informaba el rastreador Scrapy, que en la práctica no descarga nada; ahora también `teac_seed` |
| Listado de criterios TEAC por fechas (`teac_listado`) | `cli/teac_seed.py::collect_criterio_ids` | Enlaces `criterio.aspx?id=…`; página vacía = fin de resultados | Enlaces con otra forma → primera página «vacía» → 0 criterios, fin normal | **Spec + canarios** (la expresión de los enlaces es un dato): la primera página no enlaza ningún criterio (grave si el rango es de 60 días o más; aviso si es más corto) |
| Listados de boletines autonómicos: BOCM, BOJA, DOGC (`boletin_madrid_bocm`, `boletin_andalucia_boja`, `boletin_cataluna_dogc`) | `crawler/boletin_autonomico.py::parse_listing` | Resultados servidos sin JavaScript; cuatro XPath por boletín | Marcado cambiado → `results: []` en el `listing_*.json`; la araña termina sin error | **Spec + canarios** (los cuatro XPath por boletín son datos): cero resultados; más de la mitad sin título; ninguno con fecha (aviso). **Aviso**: no hay constancia de que los selectores incorporados se hayan validado contra los sitios reales (el único *fixture* es sintético) |
| Buscador de consultas DGT, Petete/Knosys (`dgt_busqueda`) | `cli/dgt_seed.py::_search_month` | `<td id="doc_N">` con `class="NUM-CONSULTA"`; `id="total_pages"`; campo oculto `id="query"` | Sin filas → «mes sin consultas»; sin `query` → cada descarga de documento falla una a una | **Spec + canarios** (las cuatro expresiones son datos): página sin filas, paginación ni `query`; filas sin `query`; filas sin número (aviso) |
| Documento de consulta DGT (`dgt_documento`) | `cli/dgt_seed.py::_fetch_document` y `_parse_doc_num` | `class="NUM-CONSULTA"` en el documento; número `V0000-00` | Documento sin número reconocible → se guarda como `unknown-<id>` y no se deduplica | **Spec + canarios** (las dos expresiones son datos). Se observa un documento por página de resultados (el archivo tiene ~68 000): sin número, número de otra consulta, documento casi vacío |
| Sentencias CENDOJ (`cendoj_documento`) | `crawler/cendoj.py::parse_cendoj_document`, llamado por `cli/supremo_seed.py` (ficheros locales o URL indicadas una a una) y por `CENDOJCrawler` | «ROJ: STS n/aaaa», «ECLI:…», una fecha `DD/MM/AAAA`, «Ponente:», «Sala Tercera…» | Sin ROJ/ECLI/fecha → metadatos `null`; la sentencia se guarda con otro nombre o pierde prioridad | **Spec + canarios** (XPath del texto, patrones de ROJ, ECLI, fecha y sala, y la etiqueta del ponente son datos): página casi vacía (el armazón de JavaScript del portal); ni ROJ ni ECLI; sin fecha (grave); sin ROJ, sala o ponente (avisos). **Solo se observan lecturas que el operador ya ha pedido: la vigilancia no añade ningún acceso al CENDOJ** (sus condiciones prohíben la descarga masiva sin autorización del CGPJ) |
| Cabecera de las sentencias del Supremo (`supremo_lote`) | `doctrine/supremo.py` (sección, tipo de recurso, doctrina), contado en `cli/supremo_seed.py` | «Sección: N» o «Sección Segunda» o el Id Cendoj; «Procedimiento: …»; frases con que se fija doctrina | Sin sección reconocible ninguna sentencia es «objetivo»: todas se descartan, como en una ejecución sin nada de interés | **Canarios** (recuentos por ejecución, sin muestras): con 3 o más leídas, ninguna con fecha o con tipo de recurso; con 3 o más de la Sala Tercera, ninguna con sección; con 5 o más casaciones nuevas, ninguna fija doctrina (aviso). Las ~20 expresiones de `supremo.py` siguen en el código: un fallo acaba en informe para un desarrollador |
| Sede de la AEAT, páginas de entrada (`aeat_sede`) | `crawler/crawlers.py::AgenciaTributariaWebCrawler.parse` | Rutas `/Sede/irpf`, `/Sede/iva`…; enlaces seguibles | Reorganización de la sede → se guardan pocas o ninguna página; la araña termina bien | **Spec + canarios** (rutas permitidas y dominio son datos; el rastreador las lee de la spec): página de entrada casi vacía; ninguno de sus enlaces casa con las rutas. La muestra guarda la dirección (`<base>`) para repetir la extracción |
| Ejecución del rastreador de la sede (`aeat_rastreo`) | `AgenciaTributariaWebCrawler` (`_semilla_fallida`, `closed`) | Que las páginas de entrada existen | Un 404 de una página de entrada solo queda en el registro de Scrapy | **Canarios** (recuentos): página de entrada que ya no existe (aviso; grave si son más de la mitad); ninguna página guardada; caída de más del 50 % de las páginas frente a la mediana. Las páginas de entrada siguen en el código |
| PDF de la sede (`aeat_pdf`) | `crawler/crawlers.py::AgenciaTributariaPDFCrawler` | PDF servidos como bytes (`%PDF`) | Un visor HTML en lugar del PDF se descarta con un aviso en el registro | **Canarios** (recuentos): ninguna respuesta a un `.pdf` era un PDF; más de la mitad no lo eran (aviso); muchas páginas sin ningún `.pdf` (aviso); caída de los guardados |
| Conversión del corpus (`corpus_docling`) | `processor/document_loader.py::process_documents` | Que cada HTML/PDF produce fragmentos | Fichero que da 0 fragmentos: consta en el manifiesto, no avisa | **Canarios** (recuentos por ingesta, sin muestras): ningún fragmento en toda la ingesta; más de un cuarto de los ficheros sin fragmentos (aviso); caída de fragmentos por fichero |

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
| Tabla de precios de modelos | `ops/data/precios_llm.json` | Precios antiguos → costes y límites en USD desfasados; modelo configurado sin precio → sus llamadas cuestan «nada» y no cuentan para el límite | `ops formatos check` avisa (en `otros_avisos`): `consultado_el` de más de 120 días o en el futuro; **cada modelo configurado** (`ANTHROPIC_MODEL` u `OPENAI_AGENT_MODEL` según `LLM_PROVIDER`, y `FORMAT_*`, `RECODE_*`, `VERIFIER_MODEL`) sin precio de entrada y salida. No hay fuente que consultar automáticamente (la página de precios no es accesible desde aquí) |
| Calendario del contribuyente: coherencia (`calendario_fiscal`) | `tax/data/calendario.json` y `tax/calendario.py` | Una fecha que no existe en un año (p. ej. 30 de febrero, o 29 en año no bisiesto) se omite sin aviso; un modelo mal escrito; un plazo sin contrastar que se acerca | **Canarios** (en cada `ops formatos check` y `probe`, sin red): cada obligación tiene vencimiento en el año en curso y en el siguiente; cada fecha existe; nada pasado se presenta como próximo y la cuenta atrás cuadra; cada modelo es uno de los que conoce la aplicación; periodo con `{anio}`; ids únicos; `consultado_el` no futuro y de menos de un año (aviso); plazo con `verificado: false` a 90 días o menos (aviso con lo que hay que comprobar) |
| Calendario del contribuyente: contraste con el BOE (`calendario_boe`) | `ops/formatos_calendario.py` (`texto_articulo`, `extraer_contraste`), en `ops formatos probe` | Que el artículo del BOE que fija cada plazo sigue diciéndolo | Una reforma del reglamento del IVA o del IRPF movería un plazo y el calendario seguiría mostrando el antiguo | **Spec + canarios** (citas literales leídas de los textos consolidados el 12-11-2026: art. 71.4 RIVA, art. 111.1 RIRPF, arts. 42 bis.5 y 42 ter.5 RD 1065/2007, art. 12.2 LIRPF, art. 30.5 Ley 39/2015): artículo no encontrado; cita que ya no está; día o mes de `calendario.json` que lo que dice hoy el BOE no menciona. Un cambio de redacción se adapta añadiendo la redacción nueva como alternativa (propuesta revisada **siempre** por una persona); un cambio de plazo no pasa el ensayo y acaba en informe: el dato legal se corrige a mano en `calendario.json`, junto con la cita de la spec incorporada. **La campaña de la Renta no tiene fuente legible por máquina en los datos** (la fija cada año una orden ministerial que `calendario.json` no identifica): solo el aviso «sin verificar» de la coherencia |

## Cómo funciona la vigilancia

1. **Canarios deterministas** (`hacienda_gpt/ops/formatos_fuentes.py`). Cada fuente declara sus
   invariantes. Los lectores reales llaman a `formatos.observar(...)` en cada lectura; esa llamada
   nunca lanza excepciones ni cambia lo que el lector devuelve.
2. **Estado** (`FORMAT_STATE_PATH`, JSON con las últimas 40 observaciones por fuente): `ok`,
   `degradada` (falló la última lectura, o una métrica cayó frente a la mediana reciente) o `rota`
   (dos lecturas seguidas con un fallo grave, o un fallo grave sin ninguna lectura buena previa).
   Las fuentes que solo pueden juzgarse por ejecución (rastreadores, ingesta, lotes del Supremo)
   informan una vez al terminar, con recuentos; una ejecución que no alcanzó a leer nada (sin red) no
   se registra, para no confundir «no pude leer» con «ha cambiado».
   Los datos propios que caducan (calendario, tabla de precios) los comprueba `ops formatos check`;
   `ops formatos probe` añade el contraste del calendario con el BOE.
3. **Muestras** (`FORMAT_DIR/muestras/<fuente>/`): la última muestra buena, recortada, con la
   extracción que produjo; la anterior; y la última mala. Solo de fuentes públicas (de una sentencia
   del CENDOJ, solo su comienzo sin *scripts*: tres como mucho, nunca una colección).
4. **Diagnóstico agéntico** (`hacienda_gpt/ops/formatos_agente.py`): diagnóstico con cita literal
   de la muestra nueva → ensayo determinista de la spec propuesta → reflexión independiente. Si no
   converge o no es un cambio de datos: informe para un desarrollador con las muestras adjuntas.
5. **Aplicación**: `ops formatos apply` (o `FORMAT_AUTO_APPLY=true`); el ensayo se repite al aplicar.
   Las propuestas de `documentos` y `calendario_boe` nunca se aplican solas.

## Garantías

- La versión 1 de cada spec reproduce el lector anterior: `tests/ops/test_formatos_specs.py` y
  `tests/ops/test_formatos_fuentes.py` lo comparan con copias literales del código previo
  (`tests/ops/legacy_parsers.py`) sobre muestras reales del BOE capturadas el 12-11-2026 y sobre los
  *fixtures* existentes (sintéticos para CENDOJ, AEAT, DGT y TEAC: esos sitios no son accesibles desde
  aquí; ver `tests/fixtures/formatos/LEEME.md`).
- Instrumentar un lector no cambia lo que devuelve ni lo que escribe: las pruebas ejecutan cada uno
  con la vigilancia encendida y apagada y comparan la salida (ficheros, manifiestos, peticiones).
- Una spec nueva solo entra en vigor si, **sin modelo de por medio**, (a) valida contra el esquema
  de reglas de su fuente (claves desconocidas rechazadas, expresiones regulares acotadas), (b)
  reproduce exactamente la extracción guardada de las muestras buenas y (c) cumple todos los
  canarios en la muestra nueva.
- Una spec aplicada ilegible o inválida no rompe el lector: se usa la incorporada.
- El agente no puede marcar una fuente como sana: el estado solo cambia con lecturas reales.
- Un fallo ya diagnosticado no se vuelve a diagnosticar (ni a pagar) salvo con `--force`.

## Lo que queda

1. `cli/proactive.py::_sumario` hace `json.loads` de la respuesta: si la API devuelve HTML de error
   con código 200, la excepción queda en `errores` de `boe-novedades` (visible), pero no llega a la
   vigilancia de formatos porque el lector no llega a ejecutarse. (Ese fichero es de otro equipo.)
2. El aplanado de HTML a líneas (`proactive/impacto.py::_lineas`) sigue siendo código: es un
   recorrido de etiquetas, no un selector, y `impacto.py` es de otro equipo. Para hacerlo dato habría
   que extraer la lista de etiquetas de bloque y las que se descartan a `ReglasConsolidado`, con prueba
   de identidad contra `legacy_parsers._lineas`.
3. Las ~20 expresiones de cabecera y doctrina de `doctrine/supremo.py` siguen en el código (solo hay
   canarios por ejecución). Las semillas de la sede de la AEAT y el catálogo de códigos autonómicos
   (`DEFAULT_CCAA_CODES`) también: un fallo es un informe, porque la muestra de un 404 no dice cuál es
   la dirección nueva.
4. El marcado del BOE del que depende `formatos_calendario.texto_articulo` (`<div class="bloque"`,
   `class="articulo"`) no es una spec; si cambia, el contraste dice «artículo no encontrado» y acaba
   en informe. Un artículo renumerado tampoco se adapta con datos (la muestra no contiene el nuevo).
5. Fuera del BOE no hay muestras reales: CENDOJ (por sus condiciones), sede de la AEAT, DGT y TEAC no
   son accesibles desde donde se escribió esto. Sus *fixtures* son sintéticos y los umbrales no están
   calibrados con lecturas reales.
6. Nada se ha ejecutado contra un modelo real: ver `docs/operacion.md`, «Antes de fiarte».
