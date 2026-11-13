# Módulo `suscripciones`: cargos recurrentes en los extractos del banco

Objetivo: que el hogar sepa qué le cobran todos los meses (y todos los años), cuándo, cuánto ha subido y cómo
darse de baja a tiempo. A partir de los **extractos que el usuario exporta de su banco** (sin claves bancarias
ni conexión de banca abierta en esta fase), el módulo:

1. detecta los cargos recurrentes: suscripciones digitales, seguros, luz, gas, agua, telefonía e internet,
   préstamos, gimnasio, alquiler, cuotas, comisiones…, con su periodicidad, el importe, el próximo cargo previsto y
   una confianza;
2. señala **subidas de precio** (fecha, importe anterior y nuevo, porcentaje), **cargos duplicados** (el mismo
   servicio cobrado dos veces en un periodo o pagado por dos vías, p. ej. tarjeta y PayPal), **pruebas convertidas
   en pago** y servicios pequeños y constantes que conviene revisar si se usan;
3. calcula el gasto anual por categoría;
4. avisa antes de las renovaciones con tiempo para darse de baja;
5. prepara un **borrador** de carta o correo de baja con su fundamento legal citado literalmente del BOE.

Código: `hacienda_gpt/modulos/suscripciones/` · datos: `data/derechos_consumidor.json`, `data/comercios.json`,
`data/bancos.json` · pruebas: `tests/modulos/suscripciones/` · extractos del BOE y extractos bancarios sintéticos:
`tests/fixtures/modulos/suscripciones/`.

## Privacidad

Los extractos bancarios son muy sensibles. Por eso:

- **El extracto se procesa en memoria y no se guarda nunca**: ni el fichero, ni los movimientos, ni los saldos,
  ni el número de cuenta (de la cuenta solo se muestran los cuatro últimos dígitos, y no se guardan).
- Lo único que se guarda, y solo si el usuario no lo desactiva (`guardar: false`), es el hecho
  `suscripciones_servicios` (marcado `sensible`): la **lista derivada de servicios recurrentes**. Por servicio:
  identificador, nombre del comercio, categoría (y de dónde salió), régimen legal de la baja, periodicidad,
  importe del último cargo (o la media, si varía), medio de pago, fechas del primer, último y próximo cargo,
  estado (activo/inactivo), confianza, número de cargos, y el último cambio de precio, cargo duplicado y prueba
  detectados. Además de lo mínimo (comercio, importe, periodicidad, fechas, categoría) se guardan esos tres
  eventos porque los avisos diarios se calculan solo con lo guardado.
- No se guardan conceptos bancarios, referencias de recibo, mandatos SEPA ni movimientos que no sean cargos
  recurrentes (supermercado, gasolinera, Bizum, cajero, nómina…).
- Ninguna herramienta hace llamadas de red. El etiquetador opcional por LLM está **apagado por defecto** (ver
  abajo) y, si se activa, solo recibe el nombre normalizado del comercio.
- Un nuevo extracto se fusiona con la lista guardada por identificador de servicio: gana el dato más reciente y
  se conservan los servicios de otros extractos (otra cuenta, otro periodo).

Cuando el núcleo pase el fichero subido a `suscripciones_analizar_extracto`, conviene que lo haga directamente
desde la subida (sin que el contenido pase por el modelo) y que no lo registre en logs ni en la traza.

## Formatos de extracto

| Formato | `formato` | Lectura |
|---|---|---|
| AEB Norma 43 (Cuaderno 43) | `norma43` | Registros de 80 posiciones 11/22/23/24/33/88. Cada fichero se comprueba con su propio registro 33 (número de apuntes, totales al debe y al haber, saldo final): si no cuadran, se rechaza. Admite ficheros sin saltos de línea y varias cuentas. El concepto común (03 recibos, 05 préstamos, 12 tarjetas…) da el medio de pago. |
| CSV del banco | `csv` | Delimitador (`;` `,` tabulador `\|`) y codificación (UTF-8, UTF-8 con BOM, Windows-1252, Latin-1) detectados. |
| Excel XLSX | `xlsx` | Lector propio (sin dependencias) de la primera hoja; fechas por estilo de celda o número de serie. Se envía en base64 (`contenido_base64`). El `.xls` antiguo no se admite. |
| OFX 1.x (SGML) / 2.x (XML), QFX | `ofx`, `qfx` | `STMTTRN` con `TRNTYPE`, `DTPOSTED`, `TRNAMT`, `FITID`, `NAME`, `MEMO`; codificación por la cabecera (`CHARSET:1252`, `encoding="UTF-8"`). |
| ISO 20022 camt.053 (y camt.052/054) | `camt053` | Cualquier versión (`camt.053.001.NN`): `Ntry` con `Amt`, `CdtDbtInd`, `Sts` (solo `BOOK`), `BookgDt`, `BkTxCd`, `TxDtls` (mandato, acreedor `Nm` o `Pty/Nm`, `Ustrd`); los lotes con varios `TxDtls` se separan. Se rechaza el XML con `DOCTYPE` o entidades. |

`formato: "auto"` (por defecto) lo detecta. Los PDF no se leen en esta fase.

**Importes y fechas**: coma o punto decimal decidido **por columna** (si un valor tiene los dos, el último es el
decimal; si no, la coma, salvo que la columna use punto con 1-2 decimales y ninguna coma), separador de miles,
signo delante, detrás o entre paréntesis, menos tipográfico, `€`/`EUR`. Fechas `dd/mm/aaaa`, `dd-mm-aa`,
`aaaa-mm-dd`, número de serie de Excel; el orden día/mes se decide por columna (por defecto día/mes; si mezcla los
dos, error).

**Columnas de CSV/XLSX**: se reconocen **solo por el nombre de su cabecera**, nunca por su posición
(`data/bancos.json`, sin acentos ni signos: «F. Operación» = `f operacion`). Campos: fecha, fecha valor,
concepto, detalle (concepto ampliado, observaciones, movimiento…), importe, o cargo y abono, saldo, divisa,
signo D/H. El resultado dice qué columna se usó para cada campo. El módulo **no adivina**:

- si un campo de fecha o importe coincide con varias columnas, devuelve error y pide `mapeo_columnas`
  (p. ej. `{"importe": "Importe"}`);
- si todos los importes son positivos y no hay columna de signo ni cargo/abono, pide `importes_positivos_son`.

**Perfiles de banco** (Santander, BBVA, CaixaBank, ING, Sabadell, Openbank): solo nombran el banco y avisan si la
cabecera encontrada no coincide con la esperada. Las cabeceras esperadas **no se han podido contrastar con
exportaciones reales** desde esta máquina (`verificado: false`); por eso nunca deciden el mapeo.

## Cómo se detecta un cargo recurrente (sin LLM)

1. Solo cargos (importe negativo), sin retiradas de efectivo ni comercios que nunca son suscripción
   (supermercados, gasolineras, Bizum, cajero, nómina, restauración, Amazon salvo Prime… en `comercios.json`).
2. **Normalización del concepto**: mayúsculas sin acentos; se quitan números de tarjeta (`5540XXXXXXXX1234`,
   `*1234`), fechas, referencias y teléfonos (palabras con dígitos), palabras de la operación (COMPRA, TARJ,
   RECIBO, ADEUDO, SEPA, PAGO MOVIL, APPLE PAY…), sufijos societarios (SA, SL, BV, LTD…) y países o ciudades al
   final. La clave son las tres primeras palabras útiles. En los recibos domiciliados con acreedor (camt.053) se
   usa el nombre del acreedor. Los comercios conocidos (`comercios.json`, ~100 patrones) dan nombre, categoría y
   régimen legal de la baja.
3. Se agrupa por comercio y, aparte, lo pagado por PayPal.
4. Cargos del mismo importe con 3 días o menos de diferencia se apartan como posibles **cargos dobles**.
5. Series de **importe idéntico** y luego grupos por importe con tolerancia del 25 % (+0,50 €) se prueban contra
   las periodicidades: semanal (7 ± 2 días), mensual (30,44 ± 5), bimestral (60,9 ± 8), trimestral (91,3 ± 10),
   semestral (182,6 ± 15), anual (365,25 ± 20), por la mediana de los intervalos; se admiten huecos de uno o dos
   periodos. Hace falta que al menos el 75 % de los intervalos encajen (y la mitad sin hueco) y un mínimo de
   cargos: 4 semanales, 3 mensuales/bimestrales/trimestrales, 2 semestrales/anuales (2 en cualquier periodicidad
   no semanal para comercios conocidos).
6. Un importe que varía más del 10 % solo se acepta en recibos domiciliados o en suministros, telefonía, seguros y
   préstamos (luz y agua varían; el supermercado no es un servicio).
7. Series consecutivas del mismo comercio y periodicidad se unen (**cambio de precio** entre ellas) y se añaden los
   cargos sueltos que continúan una serie (un último cargo ya al precio nuevo).
8. **Cambio de precio**: un importe distinto (más de 0,5 % o 1 céntimo) que se repite en el cargo siguiente o es el
   último; uno que no se repite es un cargo atípico. Con demasiados cambios la serie se trata como importe variable.
9. **Prueba convertida en pago**: un cargo de hasta 1 € o del 20 % del precio entre 1 y 40 días antes del primer
   cargo completo.
10. **Servicio duplicado**: dos series activas del mismo servicio, misma periodicidad, importes a menos de un 25 % y
    fechas que se solapan (p. ej. Spotify por tarjeta y por PayPal). Dos importes distintos tras el mismo
    descriptor (iCloud 0,99 € y Apple Music 10,99 € como «APPLE.COM/BILL») son dos servicios, no un duplicado.
11. **Anual de un solo cargo**: un único cargo de un comercio de suscripción conocido en un extracto de 300 días o
    más (y de hace más de 45 días) se da como posible anual con confianza 0,30.
12. **Próximo cargo**: último cargo + periodo; en mensuales, el día del mes más repetido en los tres últimos cargos
    (ajustado a fin de mes). Un servicio sin cargos en 1,5 periodos + tolerancia al final del extracto queda
    «inactivo».
13. **Confianza** (0 a 1): 50 % regularidad de los intervalos, 30 % número de cargos (6 mensuales, 4 bimestrales,
    3 trimestrales, 2 semestrales/anuales, 8 semanales dan el máximo), 20 % estabilidad del importe; +0,05 si el
    comercio es conocido y +0,05 si es un recibo domiciliado. Alta ≥ 0,80; media ≥ 0,60.
14. **Revisa si lo usas**: suscripción digital activa, 6 cargos o más, 20 €/mes o menos y sin cambios de precio.
    El módulo no sabe si el usuario usa el servicio y lo dice así.

Todos estos números son **heurísticas del módulo**, no cifras legales.

### Etiquetador opcional por LLM

`etiquetador.py`, apagado por defecto. Solo actúa si `SUSCRIPCIONES_ETIQUETADOR_LLM=1` **y** el núcleo registra una
función con `etiquetador.configurar(fn)`, donde `fn(clave, categorias) -> categoria | None`. Puerta determinista:
solo para comercios que el catálogo no conoce; recibe únicamente la clave normalizada (sin importes, fechas ni
cuentas; se rechazan claves con dígitos o de más de 40 caracteres); la respuesta solo se acepta si es una de las
categorías cerradas y no «otros»; los errores se ignoran. La categoría queda marcada `categoria_origen = "llm"` y la
herramienta lo dice.

## Derechos de la persona consumidora (leídos en www.boe.es el 13/11/2026)

| Clave | Qué se usa | Norma (texto consolidado) |
|---|---|---|
| `baja_tracto_sucesivo` | Prohibidas las trabas a la baja en contratos de tracto sucesivo; baja «en la misma forma en que lo celebró», sin penalizaciones desproporcionadas; el contrato debe prever el procedimiento | TRLGDCU art. 62.3 y 62.4 (BOE-A-2007-20555) |
| `permanencia_proporcional` | Penalización por permanencia proporcional a los días no cumplidos | TRLGDCU art. 62.5 |
| `prorroga_automatica` | Abusiva la prórroga automática con fecha límite que no permite oponerse | TRLGDCU art. 85.2 |
| `subida_precio` | Abusiva la subida sin razones objetivas ni derecho a resolver (salvo índice legal) | TRLGDCU art. 85.10 |
| `aviso_renovacion` | En contratos a distancia o fuera del establecimiento sujetos a renovación, aviso con **15 días** de antelación al plazo para no renovar | TRLGDCU art. 97.1.p) (redacción de la Ley 10/2025) |
| `desistimiento` | **14 días naturales** desde la celebración del contrato de servicios (a distancia) | TRLGDCU arts. 102.1 y 104.a) |
| `atencion_cliente` | Clave identificativa y justificante; canales (el de contratación, postal, teléfono, electrónico); respuesta en **15 días** | TRLGDCU art. 21.2 y 21.3 |
| `atencion_clientela_ley_10_2025` | Mismo canal de contratación + postal, teléfono, electrónico; la baja es una «incidencia»; resolución en **15 días hábiles**, **5 días** en cobros indebidos; **12 meses** de adaptación desde el 28/12/2025 | Ley 10/2025, arts. 2.1, 3.4, 7.1, 17.1, 17.3 y DT única (BOE-A-2025-26698) |
| `seguro_oposicion_prorroga` | Prórroga máxima de **un año**; oposición escrita **un mes** antes (tomador), **dos meses** (aseguradora); modificaciones comunicadas **dos meses** antes; no en vida | Ley 50/1980, art. 22 (BOE-A-1980-22501) |
| `telecom_duracion_y_baja` | Contratos de **24 meses** como máximo; tras la prórroga, rescisión con preaviso máximo de **un mes**; baja con **dos días hábiles** de antelación y sin facturar después | Ley 11/2022, art. 67.7 y DT 1.ª (BOE-A-2022-10757); RD 899/2009, art. 7 (BOE-A-2009-8961) |
| `telecom_cambio_condiciones` | Cambios comunicados con **un mes** de antelación; rescisión sin coste en **un mes** desde la comunicación | Ley 11/2022, art. 67.8 |
| `devolucion_adeudos` | Devolución incondicional de recibos SEPA en **ocho semanas**; el banco responde en **diez días hábiles**; operaciones no autorizadas: **trece meses** | Real Decreto-ley 19/2018, arts. 43.1, 48.2 y 49 (BOE-A-2018-16036) |

Cada derecho guarda `fuente` (referencia, en vigor desde, verificado, nota), las citas literales (`evidencia`), los
valores y cómo se escribe cada cifra en su cita (`cifras`). Comprobación contra las páginas descargadas:

```
uv run python -m hacienda_gpt.modulos.suscripciones.derechos <directorio con BOE-A-*.html o .txt>
```

Las pruebas hacen lo mismo con extractos pequeños de los artículos citados; el SHA-256 de cada página completa está
en `fuentes_principales`. Notas:

- La «Ley 7/2022» citada en el encargo es la de residuos y suelos contaminados (BOE-A-2022-5809): no trata de
  suscripciones y no se usa. La norma reciente que sí afecta a las bajas es la **Ley 10/2025** de servicios de
  atención a la clientela, que además modificó los arts. 21, 62.1 y 97.1.p) del TRLGDCU.
- El RD 899/2009 (carta de derechos del usuario de comunicaciones electrónicas) figura en el BOE solo con su texto
  inicial; sigue vigente en lo que no se oponga a la Ley 11/2022 (su disposición transitoria primera).
- El aviso de 15 días del art. 97.1.p) y el desistimiento solo se aplican a contratos a distancia o fuera del
  establecimiento; la Ley 10/2025 solo a servicios básicos (agua, gas, electricidad, transporte, postales,
  comunicaciones electrónicas, financieros) y a empresas grandes (art. 2). El módulo no puede comprobarlo y lo dice.

## Herramientas

| Herramienta | Efecto | Entrada | Salida |
|---|---|---|---|
| `suscripciones_analizar_extracto` | lectura (guarda la lista derivada) | `contenido` (texto) o `contenido_base64` (XLSX), `formato`, `banco`, `mapeo_columnas`, `importes_positivos_son`, `guardar` | Servicios activos e inactivos con importe, periodicidad, medio, fechas, gasto anual, confianza e id; subidas, duplicados, pruebas y observaciones; qué puede hacer el usuario con su base legal; columnas usadas y avisos de lectura; nota de privacidad |
| `suscripciones_listado` | lectura | `incluir_inactivas`, `categoria` | La lista guardada |
| `suscripciones_gasto_anual` | lectura | — | Total anual y mensual, por categoría con su peso y el desglose `importe × cargos al año` |
| `suscripciones_preparar_baja` | **prepara** | `servicio` (id o nombre), `motivo` (`baja`, `no_renovar`, `subida_precio`, `desistimiento`, `cobro_tras_baja`), `canal` (`email`, `carta`), `sector` (si no está en la lista), `fecha_contratacion`, `fecha_vencimiento` | Borrador con asunto, petición, fundamento (citas literales con artículo y norma), y plazos calculados: oposición a la prórroga de un seguro (vencimiento − 1 mes), efecto de la baja en telefonía (+2 días hábiles, sin festivos), fin del desistimiento (+14 días), devolución del recibo (+8 semanas), plazo de respuesta de la Ley 10/2025 y fin de su periodo de adaptación. Para alquiler, préstamos, comisiones o transferencias explica que no aplica una carta |

Ejemplo (extracto sintético, resumido):

```
Servicios recurrentes activos: 20; gasto anual estimado 18.073,16 € (1.506,10 € al mes).
• Netflix — 13,99 € mensual (tarjeta); 13 cargo(s) del 05/10/2025 al 05/10/2026; próximo previsto el 05/11/2026;
  gasto anual 167,88 €; confianza alta (1,00). Categoría: Streaming de vídeo. [id srv-f5871804ef]
   Subida de precio el 05/04/2026: 12,99 € → 13,99 € (+7,70 %).
• Max (HBO) — 9,99 € mensual (tarjeta); … Cargo duplicado el 15/09/2026: dos cargos de 9,99 € en 3 días o menos.
• DAZN — 19,99 € mensual … Prueba convertida en pago: cargo de 0,99 € el 03/06/2026 y cobro completo desde el 03/07/2026.
```

Ninguna herramienta envía, cancela ni contrata nada; los errores se devuelven como resultado de error con un
mensaje en español.

## Avisos proactivos (diarios, deterministas e idempotentes)

| Clave | Cuándo | Gravedad |
|---|---|---|
| `suscripciones:renovacion:<id>:<próxima fecha>` | Servicio **anual o semestral** activo, desde `SUSCRIPCIONES_DIAS_AVISO_RENOVACION` días (30 por defecto) antes de la fecha límite hasta la renovación. Fecha límite: seguros, renovación − 1 mes (art. 22.2 LCS); telefonía, − 2 días hábiles (art. 7 RD 899/2009); resto, la fecha estimada del cargo (con la indicación de mirar el contrato y el aviso de 15 días del art. 97.1.p) | urgente si quedan 7 días o menos |
| `suscripciones:subida:<id>:<fecha>` | Subida de precio en los últimos 120 días, con el coste extra anual y el derecho según el sector | aviso |
| `suscripciones:duplicado:<id>:<tipo>:<fecha>` | Cargo doble en los últimos 120 días (con plazo de devolución de 8 semanas si fue un recibo) o servicio pagado dos veces mientras sigan activos los dos | aviso |
| `suscripciones:prueba:<id>:<fecha del primer pago>` | Prueba convertida en pago en los últimos 60 días | info |

Los mensuales no tienen aviso de renovación (avisaría cada mes). Las fechas de renovación son estimaciones a partir
de los cargos: el aviso lo dice.

## Configuración

| Variable | Por defecto | Uso |
|---|---|---|
| `SUSCRIPCIONES_DIAS_AVISO_RENOVACION` | `30` | Días de antelación del aviso de renovación (1 a 120) |
| `SUSCRIPCIONES_ETIQUETADOR_LLM` | apagado | `1` activa el etiquetador opcional si el núcleo registra la función |

## Pruebas

`tests/modulos/suscripciones/sintetico.py` genera un año realista (oct-2025 a oct-2026, 353 movimientos) de una
cuenta ficticia con 21 servicios recurrentes (subidas de Netflix, Orange y Mapfre; Spotify por tarjeta y PayPal;
cargo doble de Max; prueba de DAZN; Filmin dado de baja; iCloud y Apple Music tras el mismo descriptor; luz y agua
bimestrales variables; seguros anual y trimestral; préstamo, alquiler, comisión, colegio semestral, prensa semanal,
Amazon Prime anual) y ruido (nómina, supermercado, gasolinera, comida a domicilio, compras en Amazon, Bizum,
cajero), y lo escribe en nueve ficheros: Norma 43, CSV con cabeceras tipo Santander, BBVA, ING y cargo/abono, XLSX
tipo Openbank, OFX 1.x, QFX 2.x y camt.053.001.08 (con un lote y un apunte pendiente). Las pruebas comprueban que
cada fichero se lee movimiento a movimiento y que **en los nueve se detectan exactamente los 21 servicios** con sus
cambios, duplicados y prueba; además, la autocomprobación de las citas, cada herramienta, los avisos y un recorrido
completo con un perfil de hogar.

## Validación con un extracto real (criterio de aceptación)

El usuario debe aportar un extracto real de al menos 12 meses (mejor 13, para ver los anuales dos veces) en el
formato que permita su banco y la lista de las suscripciones que sabe que tiene. Se considera validado si se
detectan todas. Lo más probable que haya que ajustar: cabeceras de CSV no previstas (se resuelven con
`mapeo_columnas` y se añaden a `bancos.json`), comercios no catalogados (se añaden a `comercios.json`) y anuales con
un solo cargo en el periodo.

## Qué no hace (todavía)

- Conexión con el banco (PSD2 / banca abierta) ni lectura de PDF o `.xls`.
- Comprobar si un contrato se celebró a distancia, si la empresa está en el ámbito de la Ley 10/2025 o si el
  contrato concreto tiene cláusulas más favorables.
- Festivos en los días hábiles; normativa sectorial de luz y gas; normativa autonómica de consumo; amortización
  anticipada de préstamos; LAU para el alquiler.
- Detectar suscripciones pagadas desde otra cuenta o tarjeta de crédito que no esté en el extracto.
