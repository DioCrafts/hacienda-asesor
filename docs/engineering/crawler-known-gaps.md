# Crawlers: known gaps and follow-ups

Lista corta de limitaciones conocidas en los crawlers actuales (TEAC, CENDOJ,
BOE-CCAA y boletines autonómicos) que no bloquean la primera ingesta pero hay
que cerrar antes de considerar el corpus completo y mantenible. Cada entrada
incluye contexto, el síntoma esperable cuando se ejecute en producción y la
acción concreta para resolverlo.

## CENDOJ — el ROJ no es navegable por URL

**Archivo:** `hacienda_gpt/crawler/cendoj.py`

**Contexto.** El portal del CGPJ (`poderjudicial.es/search`) expone cada
documento detrás de un hash interno (`openDocument/<hash>/<date>`) — el
identificador citable (ROJ `STS NNNN/AAAA` o ECLI) sólo aparece como dato
*dentro* del HTML, nunca como parámetro de URL. No hay endpoint público que
admita ROJ.

**Impacto hoy.** El `CENDOJCrawler` funciona en modo "lista curada": acepta
`--cendoj-urls-file` o `--cendoj-urls` con URLs ya resueltas. Eso sirve para
poblar el índice con una selección manual o exportada, pero no para indexado
masivo.

**Siguiente paso (Fase B del crawler).** Spider con Playwright que:

1. Rellene el formulario de búsqueda avanzada filtrando *Jurisdicción =
   Contencioso*, *Órgano = Tribunal Supremo. Sala Tercera*, y un rango de
   fechas o un término ("tributario", "IRPF", "IVA"…).
2. Recorra la paginación de resultados.
3. Por cada resultado extraiga la URL interna y el ROJ visible en la lista.
4. Encole esas URLs en el `CENDOJCrawler` actual para descargar y parsear.

`scrapy-playwright` ya es dependencia del proyecto, así que el coste es
configuración + reverse-engineering de los selectores del listado, no
infraestructura.

**Riesgo asociado.** El frontend del CGPJ cambia ocasionalmente; conviene
proteger el spider con un *contract test* (Scrapy spider contracts o un
fixture HTML grabada del listado) para detectar drift sin esperar a que
fallen los runs.


## BOE-CCAA — 11 IDs de Código pendientes de verificación

**Archivo:** `hacienda_gpt/crawler/boe_consolidado.py`

**Contexto.** El BOE publica Códigos Electrónicos consolidados por CCAA con
URLs de la forma `codigo.php?id=<N>&modo=2`. Cuatro IDs están verificados
contra el portal real:

| CCAA | Code ID | Filename |
|---|---|---|
| Andalucía | 229 | `229_Codigo_de_Andalucia` |
| Cataluña | 228 | `228_Codigo_de_Cataluna` |
| Comunidad Valenciana | 230 | `230_Codigo_de_la_Comunidad_Valenciana` |
| Galicia | 232 | `232_Codigo_de_Galicia` |

Los 11 restantes (Aragón, Asturias, Baleares, Canarias, Cantabria,
Castilla-La Mancha, Castilla y León, Extremadura, Madrid, Murcia, La Rioja)
están en `DEFAULT_CCAA_CODES` con `known=False` — los IDs y filenames son
una conjetura por extrapolación del patrón.

**Impacto hoy.** Sin verificar, esos 11 fetches devolverán 404 y se omitirán
silenciosamente (el spider está preparado para eso, ver `handle_error`).
El flag `--skip-unknown-ccaa` filtra a los 4 verificados.

**Siguiente paso.**

1. Visitar manualmente `boe.es/biblioteca_juridica/codigos/index.php?coleccion=ccaa`,
   anotar el ID real y el *filename* exacto de cada Código autonómico.
2. Actualizar `DEFAULT_CCAA_CODES` cambiando `known=True` y corrigiendo
   `code_id` / `filename` donde haga falta.
3. Considerar añadir un *catalog crawler* preliminar que descubra los IDs
   leyendo `index.php?coleccion=ccaa` y compare contra la tabla para
   detectar drift cuando el BOE renumere o renombre.

**Decisión arquitectónica relacionada.** Régimen foral (País Vasco, Navarra)
queda *fuera* de este crawler por diseño — su normativa fiscal vive en sus
propios boletines (BOPV, BON) y se ingiere por la vía de `boletin_autonomico`.


## Boletines autonómicos — XPaths piloto sin contraste contra HTML real

**Archivo:** `hacienda_gpt/crawler/boletin_autonomico.py`

**Contexto.** Hay tres specs piloto bundled (`BOCM_MADRID`, `BOJA_ANDALUCIA`,
`DOGC_CATALUNA`). Los XPaths (`item_xpath`, `title_xpath`, `date_xpath`,
`link_xpath`) son aproximaciones razonables del layout que esos portales
suelen exponer, basadas en el patrón general "div.resultado + h3 + span.fecha
+ a/@href" — pero no han sido validados contra el HTML real porque desde
nuestro entorno de desarrollo los hosts autonómicos no respondían.

**Impacto hoy.** En el primer run real es probable que los XPaths devuelvan
0 resultados (cero ítems matcheados) o ruido (matchea bloques que no son
resultados de búsqueda). El spider escribe siempre el `listing_<term>.json`
para que sea evidente.

**Siguiente paso.**

1. Capturar una página de búsqueda real por cada boletín
   (`curl '<URL>' > tests/fixtures/boletines/<key>_listing.html`).
2. Ajustar los XPaths de la spec correspondiente para que `parse_listing()`
   devuelva resultados consistentes.
3. Añadir un test unitario por spec que valide el parsing contra la fixture
   real (siguiendo el patrón de `test_parse_listing_extracts_results_from_bocm_layout`).

La spec es un `dataclass`; añadir una CCAA nueva o ajustar selectores es
una línea, no un commit grande.

**Cuándo ampliar el catálogo.** Las 12 CCAA restantes de régimen común
(Aragón, Asturias, Baleares, Canarias, Cantabria, Castilla-La Mancha,
Castilla y León, Extremadura, Galicia, Comunidad Valenciana, Murcia,
La Rioja) deberían cubrirse en orden de relevancia fiscal — Galicia y
Valenciana primero por volumen ISD/ITP, luego el resto. Las dos forales
(País Vasco, Navarra) requieren su propio spec porque la lógica fiscal es
sustancialmente distinta.
