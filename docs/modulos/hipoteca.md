# Módulo `hipoteca`: la hipoteca del hogar

Qué responde:

1. **Cuadro de amortización y capital pendiente** a cualquier fecha (sistema francés; préstamo fijo, variable o
   mixto), con el tipo vigente, las próximas cuotas, los intereses que quedan y lo pagado en un año.
2. **Próxima revisión del tipo**: con el índice oficial publicado en el BOE (Euríbor u otro), la nueva cuota y su
   sensibilidad si el índice fuera distinto.
3. **Amortizar anticipadamente**: reducir plazo frente a reducir cuota, intereses ahorrados, comisión máxima según
   la ley que rige el préstamo, y una comparación aritmética con una rentabilidad alternativa que da el usuario.
4. **Cambiar de banco** (subrogación), novar con el banco actual o cancelar y firmar otra hipoteca: comisión al
   banco actual, costes que introduce el usuario, nueva cuota, total a pagar y mes en que se recuperan los costes.
5. **Avisos**: revisión próxima con la cuota estimada, índice nuevo que mueve la estimación, y fecha en que baja la
   comisión por amortizar.

Código: `hacienda_gpt/modulos/hipoteca/` · datos: `data/normativa.json` (ley) y `data/indices_referencia.json`
(índices) · pruebas: `tests/modulos/hipoteca/` · extractos del BOE y resoluciones del Banco de España:
`tests/fixtures/modulos/hipoteca/`.

## Índices oficiales: lector de las resoluciones del Banco de España en el BOE

El art. 27.2 de la Orden EHA/2899/2011 (BOE-A-2011-17015) dice que «los tipos de interés oficiales se publicarán
mensualmente por el Banco de España en el “Boletín Oficial del Estado”». Hay dos resoluciones al mes:

- a principios de mes (día 1-4): Euríbor a una semana, uno, tres, seis meses y un año; €STR a esos plazos; IRS a
  cinco años; Míbor (y hasta 2023, el rendimiento de la deuda pública de 2 a 6 años);
- hacia el día 18: IRPH de entidades en España y tipo medio de los préstamos a la vivienda en la zona euro (hasta
  2013 también IRPH de bancos y de cajas y el índice CECA).

`indices.py` las busca con el buscador del BOE (`/buscar/boe.php`, por palabras del título y fechas), descarga el
XML (`/diario_boe/xml.php?id=<BOE id>`) y lee la tabla. Tolera todos los formatos desde 2004 (mes en el título de
la tabla o en un párrafo, «Mayo 2025» sin «de», filas de 1, 2 o 3 celdas, secciones «Euríbor. Plazos:» y «€STR.
Plazos:», guion largo como signo menos, «%» tras la cifra) y aplica las **correcciones de erratas**: las que
reimprimen la tabla (gana la publicación posterior y se anota qué sustituye) y las escritas en prosa («donde dice
«5,148», debe decir «5,418»»; «antes del punto 1, debe decir «Mayo 2006»»). Una fila que no reconoce no se adivina:
se informa (`desconocidas`).

La serie guardada tiene **510 resoluciones de enero de 2004 a noviembre de 2026** (consultadas el 13/11/2026).
Cada valor lleva el mes al que se refiere, la cifra literal del BOE, el BOE id y la fecha de publicación; cada
documento, el sha256 de su XML. El Euríbor a un año está completo de diciembre de 2003 a octubre de 2026.

Dos meses se han **inferido** (anotado en cada valor, `MES_INFERIDO`): la resolución de 18-1-2013
(BOE-A-2013-531) no imprime el mes (se toma diciembre de 2012, como todas las de mitad de mes, que dan el IRPH del
mes anterior al anterior) y la de 18-5-2023 (BOE-A-2023-11879) imprime «Abril de 2003» (errata evidente: abril de
2023). Sin publicar en el BOE dentro de la serie: IRPH de mayo a noviembre de 2003 y el tipo medio de la zona euro
antes de 2012 (no se publicaba como índice oficial).

Mantenimiento: `python -m hacienda_gpt.modulos.hipoteca.indices actualizar` añade las resoluciones publicadas
desde la última guardada (red: solo www.boe.es); `… reconstruir <dir>` regenera la serie desde XML descargados.

### Qué valor del índice usa cada revisión

La escritura lo dice. Se admiten dos reglas (`indice_regla`):

- `meses_antes` (por defecto, con `indice_meses_antes = 2`): el valor del mes que está N meses antes del mes de la
  revisión (revisión en marzo → Euríbor de enero). Es la cláusula más habitual, pero **compruébala en tu
  escritura**;
- `ultimo_publicado`: el último valor publicado en el BOE en la fecha de la revisión.

Si el valor que pide la regla aún no está publicado, la revisión se **estima** con el último publicado (y se marca
«estimado» en el cuadro). Lo más fiable es indicar en `revisiones_conocidas` el tipo que el banco te aplicó en
cada revisión: entonces se usa ese y no el índice.

## Cómo se calcula el cuadro (convenciones de redondeo)

| Paso | Cálculo |
|---|---|
| Tipo del periodo | TIN anual ÷ 12 × meses entre cuotas (meses de 30/360, sin contar días reales) |
| Cuota | Anualidad francesa sobre el capital pendiente y las cuotas que faltan, **redondeada al céntimo, mitad hacia arriba** |
| Intereses de cada cuota | Capital pendiente × tipo del periodo, redondeado al céntimo (mitad hacia arriba) |
| Capital amortizado | Cuota − intereses; la **última cuota** amortiza todo lo que queda (puede diferir unos céntimos) |
| Recalcular la cuota | Solo cuando cambia el tipo (revisión) o tras una amortización anticipada |
| Revisión | Se aplica a las cuotas cuyo periodo empieza en la fecha de revisión o después |
| Tipo variable | Índice + diferencial − bonificaciones cumplidas; redondeo al múltiplo de la escritura (al más cercano); suelo/techo de la escritura; **nunca negativo** (art. 21.4 Ley 5/2019) |
| Amortización anticipada | Tras la última cuota vencida en su fecha; no se calculan los intereses del periodo roto. Reducir plazo: misma cuota, se recalcula el número de cuotas (redondeado hacia arriba) y la cuota sobre ese número entero. Reducir cuota: mismas fechas, cuota menor |

**Qué hacen los bancos**: casi todos usan el sistema francés con cuota redondeada al céntimo, pero varían en el
cálculo de intereses (algunos por días reales sobre 360 o 365), en el redondeo (truncar en vez de redondear, o la
cuota al céntimo superior), en la fecha efectiva de la revisión y en cómo ajustan la última cuota. Por eso **el
resultado puede diferir en céntimos por cuota** y acumular algunos euros en el capital pendiente tras muchos años.
El módulo lo dice en cada respuesta y permite contrastarlo (`comprobacion_banco`).

## Comisiones por amortización anticipada (leídas en www.boe.es el 13/11/2026)

Lo que calcula el módulo es siempre un **máximo**: la comisión no puede superar la pérdida financiera del banco
(art. 23.8 Ley 5/2019), que el módulo no calcula y puede ser menor o nula. Si la escritura fija menos, se aplica lo
pactado (`comisiones_pactadas`; `[]` = sin comisión; sin el dato se muestra el máximo legal y se avisa).

| Préstamo | Máximo | Norma (BOE) |
|---|---|---|
| Firmado desde el 16/06/2019, tramo variable | 0,25 % los 3 primeros años (opción b) **o** 0,15 % los 5 primeros (opción a); después, nada | art. 23.5 Ley 5/2019 (BOE-A-2019-3814) |
| Firmado desde el 16/06/2019, tramo fijo | 2 % los 10 primeros años; 1,5 % después | art. 23.7 Ley 5/2019 |
| Novación o subrogación de variable a fijo (o a un primer periodo fijo ≥ 3 años), cualquier fecha de firma | 0,05 % los 3 primeros años; después, nada; sin amortización de capital en la novación, nada | art. 23.6 Ley 5/2019 (redacción RDL 8/2023) y DT 1.ª.3 |
| Variables, amortizaciones del 24/11/2022 al 31/12/2024 | Nada (suspensión temporal, ya terminada) | DA 1.ª RDL 19/2022 (BOE-A-2022-19403) |
| Firmado del 09/12/2007 al 15/06/2019 (vivienda, persona física) | Desistimiento: 0,5 % los 5 primeros años, 0,25 % después. Riesgo de tipo: nada si la revisión es ≤ 12 meses; si no, lo pactado | arts. 8 y 9 Ley 41/2007 (BOE-A-2007-21086) |
| Firmado antes del 09/12/2007, variable | 1 % (o lo pactado si es menor; nada si se pactó amortizar sin comisión) | art. 3 y DA 1.ª Ley 2/1994 (BOE-A-1994-7556) |
| Firmado antes del 09/12/2007, fijo | Lo pactado (la ley no fija tope) | — |

Otras reglas que se muestran: preaviso de como máximo un mes e información en tres días hábiles (art. 23.1-2),
extorno del seguro vinculado (art. 23.3); en la subrogación, oferta vinculante y derecho del banco a igualarla en
15 días naturales (art. 2 Ley 2/1994); exención de AJD gradual de la subrogación y de la novación de tipo o plazo
(arts. 7 y 9 Ley 2/1994) y aranceles reducidos (art. 8; registro con reducción del 90 %); comisión de novación por
ampliar plazo de como máximo el 0,1 % (art. 10 Ley 2/1994); reparto de gastos del art. 14.1.e) Ley 5/2019.

**Medidas temporales para deudores vulnerables** (comprobado su estado el 13/11/2026):
- Código de Buenas Prácticas del RDL 19/2022 (clase media en riesgo de vulnerabilidad): duración y plazo de
  solicitud de 36 meses desde la publicación del Acuerdo (24/11/2022), 42 meses para residentes en municipios de la
  DANA (RDL 6/2024). Por tanto **cerrado** a nuevas solicitudes (nov. 2025, y mayo de 2026 para la DANA). Solo
  préstamos constituidos hasta el 31/12/2022 sobre vivienda de precio ≤ 300.000 €.
- Suspensión de comisiones de la DA 1.ª RDL 19/2022: terminó el 31/12/2024; el texto consolidado (actualizado a
  12/11/2024) no recoge prórrogas.
- Código de Buenas Prácticas permanente del RDL 6/2012 (deudores en el umbral de exclusión): sigue vigente, adhesión
  voluntaria de las entidades; sus requisitos (art. 3) **no se comprueban**.

Todas las cifras están en `data/normativa.json` con su cita literal; `test_hipoteca_normativa.py` comprueba que
cada cifra está en su cita y que cada cita está, palabra por palabra, en el texto consolidado (extractos en
`tests/fixtures/modulos/hipoteca/BOE-A-*.txt`; con `HIPOTECA_BOE_DIR=<dir con las páginas completas>` se comprueba
contra las páginas enteras). También a mano: `python -m hacienda_gpt.modulos.hipoteca.normativa <dir>`.

## Datos del usuario (hechos del expediente)

| Hecho | Tipo | Contenido |
|---|---|---|
| `hipoteca_prestamo` | json, **sensible** | Ver abajo |
| `hipoteca_umbral_aviso` | number | Euros/mes de cambio de la cuota estimada a partir de los que avisar al publicarse un índice (por defecto 20) |

Campos de `hipoteca_prestamo` (los porcentajes, como se escriben: «2,5» = 2,5 % TIN; importes con coma o punto):

- obligatorios: `capital_inicial`, `fecha_firma`, `plazo_meses`, `tipo` (`fijo`/`variable`/`mixto`),
  `tipo_inicial` (el fijo, o el del primer periodo, ya con bonificaciones);
- variable o mixto: `meses_tipo_inicial` (variable: hasta la primera revisión; mixto: el tramo fijo), `indice`
  (`euribor_12m`, `euribor_6m`, `irph_entidades`, …), `diferencial`, `revision_meses`, `indice_regla`,
  `indice_meses_antes`, `redondeo_tipo_multiplo`, `tipo_minimo`, `tipo_maximo`;
- `periodicidad_meses` (1, 3, 6, 12), `fecha_primera_cuota` (por defecto, un periodo después de la firma);
- `bonificaciones`: `[{concepto, reduccion_diferencial, coste_anual, cumplida}]`;
- `revisiones_conocidas`: `[{fecha, tipo}]` — tipos que el banco ya aplicó (mandan sobre el índice);
- `amortizaciones_realizadas`: `[{fecha, importe, modalidad: plazo|cuota, comision_pagada}]`;
- `comisiones_pactadas`: `[{porcentaje, desde_anos, hasta_anos, tramo: todo|fijo|variable}]`, `[]` si no hay;
  `compensacion_riesgo_tipo` (Ley 41/2007, art. 9);
- `vivienda_habitual`, `adquisicion_antes_2013`, `entidad`;
- `comprobacion_banco`: `[{fecha, cuota, capital_pendiente}]` — cifras de un recibo para contrastar.

Se leen también, sin redefinirlos, los hechos compartidos del perfil; este módulo no escribe ninguno.

## Herramientas (todas de lectura)

| Herramienta | Entrada | Devuelve |
|---|---|---|
| `hipoteca_cuadro` | `fecha?`, `ejercicio?`, `cuotas?` | Capital pendiente, tipo vigente con su índice y BOE, próxima cuota y revisión, intereses pendientes, próximas cuotas, pagos del año, comparación con el banco |
| `hipoteca_revision` | `fecha_revision?`, `desplazamientos_pp?` | Índice aplicable (publicado o estimado, con BOE), nuevo tipo, cuota antes/después, sensibilidad, últimos valores |
| `hipoteca_amortizar` | `importe`, `fecha?`, `rentabilidad_alternativa_pct?` | Comisión máxima y su fundamento; reducir plazo (nueva fecha fin, intereses ahorrados) frente a reducir cuota (nueva cuota, ahorro); valor final de amortizar e invertir lo liberado frente a invertir el importe a la rentabilidad del usuario |
| `hipoteca_cambiar_banco` | `modalidad` (`subrogacion`/`novacion`/`cancelacion_y_nueva`), `oferta` {tipo, tipo_interes, diferencial, indice, meses_tipo_inicial, revision_meses, plazo_meses}, `gastos` {tasacion, notaria, registro, gestoria, comision_apertura, comision_novacion, impuestos, otros}, `vinculaciones_coste_anual_*` | Comisión al banco actual, costes, cuota nueva, total a pagar con cada préstamo, intereses, mes de equilibrio y lo que dice la ley |

Ejemplo (salida resumida de `hipoteca_revision`, préstamo variable de 2019 a 10/02/2026):

```
Revisión del 20/09/2026 (se aplica desde la cuota n.º 85).
Regla del contrato: el valor del mes que está 2 mes(es) antes del mes de la revisión.
El índice que corresponde todavía no está publicado. Estimación con el último publicado:
Euríbor a un año de enero de 2026: 2,245 % (BOE-A-2026-2546).
Nuevo tipo: 2,245 % (índice) + 0,89 % (diferencial) − 0,1 % (bonificaciones) = 3,035 %.
Cuota: de 833,17 € a 843,85 € (+10,68 € al mes), sobre un capital de 132.328,93 € y 200 cuotas restantes.
```

La comparación con una alternativa es **aritmética con el supuesto del usuario**, no asesoramiento: con el mismo
dinero (importe + comisión) y hasta el fin actual del préstamo, compara invertirlo a la rentabilidad indicada con
amortizar e invertir cada mes lo que se deja de pagar. Recuerda que amortizar «rinde» el tipo del préstamo sin
riesgo y no se puede deshacer, y que los rendimientos de la alternativa tributan (módulo fiscal).

## Integración con el módulo fiscal (no duplicada)

La deducción por inversión en vivienda habitual del régimen transitorio (compras anteriores a 2013, disposición
transitoria 18.ª LIRPF) es del módulo fiscal (`hacienda_gpt/tax/irpf.py`, dato `pagos_vivienda_habitual_pre2013`,
parámetros `vivienda_habitual_transitoria`). Este módulo solo la menciona y ofrece lo que hace falta para
calcularla:

- `hacienda_gpt.modulos.hipoteca.pagos_del_ejercicio(contexto, ejercicio)` →
  `{"capital", "intereses", "amortizaciones_anticipadas"}` del año (Decimal), o `None` sin hipoteca;
- `hipoteca_cuadro` con `ejercicio` muestra esas cifras.

Propuesta para el núcleo: cuando `hipoteca_prestamo.adquisicion_antes_2013` sea verdadero y falte
`pagos_vivienda_habitual_pre2013`, ofrecer al usuario rellenarlo con `capital + intereses + amortizaciones
anticipadas` del ejercicio (más los gastos y seguros que el módulo fiscal considere parte de la base: lo decide ese
módulo, no este). El dato lo escribe el núcleo o el módulo fiscal con confirmación del usuario.

## Avisos proactivos (diarios, deterministas e idempotentes)

| Clave | Cuándo | Contenido |
|---|---|---|
| `hipoteca:revision:<fecha>:<BOE id>` | Revisión en los próximos `HIPOTECA_DIAS_AVISO_REVISION` días (45) | Cuota nueva estimada o ya conocida, diferencia, índice y BOE. `sustituye_prefijo` = `hipoteca:revision:<fecha>:` (una estimación nueva sustituye la anterior) |
| `hipoteca:indice:<BOE id>` | Índice publicado en los últimos 35 días que cambia la cuota estimada de la próxima revisión al menos el umbral (`hipoteca_umbral_aviso` o `HIPOTECA_UMBRAL_AVISO_EUR`, 20 €) respecto al valor del mes anterior | Cuota estimada y diferencia |
| `hipoteca:comision:<fecha>` | En los próximos 60 días baja el máximo de la comisión por amortizar | Porcentaje antes y después y lo que ahorra esperar |

Configuración (variables de entorno, opcionales): `HIPOTECA_DIAS_AVISO_REVISION`, `HIPOTECA_UMBRAL_AVISO_EUR`.

## Comprobación con el banco (fase de aceptación)

Para validar contra la realidad, el usuario debe aportar:

1. De la escritura: capital, fecha de firma, plazo, tipo inicial y su duración, índice, diferencial, periodicidad
   de revisión, **regla del índice** (qué mes se toma) y redondeo del tipo, bonificaciones, comisiones pactadas.
2. De la carta o el recibo de cada revisión: el **tipo aplicado** (`revisiones_conocidas`).
3. Las amortizaciones anticipadas hechas (fecha, importe, plazo o cuota).
4. De un recibo reciente: fecha, **cuota** y **capital pendiente** (`comprobacion_banco`).

`hipoteca_cuadro` muestra las diferencias. Si la cuota cuadra y el capital difiere en céntimos, es el redondeo; si
difiere en euros desde una fecha, revisar la regla del índice, la fecha efectiva de la revisión o una amortización.

## Lo que no está modelado

- Pérdida financiera del banco (art. 23.8 Ley 5/2019) y compensación por riesgo de tipo calculada (IRS por plazos
  de la resolución mensual del BdE «índices y tipos de referencia aplicables para el cálculo del valor de mercado»,
  que sí está en el BOE: posible ampliación).
- Intereses del periodo roto en amortizaciones fuera de fecha de cuota; carencias; cuotas crecientes o sistemas
  distintos del francés; días reales/360.
- Aranceles concretos de notaría y registro (RD 1426/1989 y 1427/1989): los introduce el usuario.
- Requisitos de vulnerabilidad de los Códigos de Buenas Prácticas.
- Préstamos en divisa, hipotecas inversas, créditos hipotecarios con disposición.

## Dudas abiertas

- Desde cuándo se cuentan los 10 años del tramo fijo de un préstamo mixto que empezara en variable: se toma la
  firma (el art. 23.7 dice «o desde el día que resulta aplicable el tipo fijo»; los mixtos habituales empiezan en
  fijo).
- La suspensión de la DA 1.ª RDL 19/2022 se aplica solo a préstamos sujetos a la Ley 5/2019 y a los cambios a fijo
  del art. 23.6; su efecto sobre préstamos anteriores es discutible (es historia: terminó en 2024).
- Plazo del Código de 2022: el art. 3.3 cuenta desde la publicación del Acuerdo y el Acuerdo desde la entrada en
  vigor del RDL; ambas fechas son el 24/11/2022, así que no cambia el resultado.
