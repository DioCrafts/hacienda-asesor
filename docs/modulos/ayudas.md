# Módulo `ayudas`: ayudas públicas a las que el hogar tiene derecho

Encuentra ayudas públicas que corresponden al hogar y que no ha pedido, con su importe cuando la ley lo fija, su
plazo, cómo pedirlas y el texto literal de la norma. Lo que no puede comprobar con el perfil lo dice siempre.

Código: `hacienda_gpt/modulos/ayudas/` · datos: `hacienda_gpt/modulos/ayudas/data/prestaciones_2026.json` ·
pruebas: `tests/modulos/ayudas/` · fixtures: `tests/fixtures/modulos/ayudas/`.

## Dos familias de ayudas

### A. Prestaciones estatales estables (`ayudas_comprobar`)

Reguladas por ley y evaluadas de forma determinista, como las deducciones del IRPF: cada cifra está en el JSON
con su fuente y la cita literal de la que se lee, y una prueba comprueba que cada cifra está en su cita y cada
cita en el texto consolidado del BOE.

| Prestación | Norma | Qué se calcula |
|---|---|---|
| Ingreso mínimo vital | Ley 19/2021 (arts. 5, 10, 11, 13, 14, 20, anexos II y III); RDL 3/2026 art. 2.5 | Renta garantizada 2026 (733,60 €/mes para una persona = pensión no contributiva de 8.803,20 € / 12), escala del 30 % por miembro hasta el 220 %, complementos del 22 % (monoparentalidad, discapacidad ≥ 65 %), margen de 10 €, límite de patrimonio (3 veces la renta garantizada × anexo II) e importe mensual |
| Complemento de ayuda para la infancia | Ley 19/2021 arts. 11.6 y 13.2.e) | Límite de ingresos (300 % del anexo I) y de patrimonio (150 % del anexo II). **No calcula el importe**: la ley dice 100/70/50 €/mes por menor, pero el RDL 20/2022 lo incrementó para 2023 y no se ha podido leer la cuantía de 2026 |
| Asignación por hijo o menor a cargo con discapacidad | LGSS arts. 351–355; RDL 3/2026 art. 2.6 | 1.000 €/año (menor con ≥ 33 %), 5.962,80 € (mayor de edad con ≥ 65 %), 8.942,40 € (≥ 75 % y ayuda de otra persona); sin límite de ingresos |
| Prestación por nacimiento o adopción (familia numerosa, monoparental o progenitor con discapacidad ≥ 65 %) | LGSS arts. 53.1, 357, 358; RDL 3/2026 art. 2.7 | 1.000 € por hijo; límite de 15.356 € (+15 % por hijo a cargo desde el segundo) o 23.109 € para familia numerosa (+3.745 € desde el cuarto); importe reducido si se supera en menos de 1.000 € (y no por debajo de 10 €); prescribe a los cinco años del nacimiento |
| Bono social eléctrico | RD 897/2017 arts. 3, 6.3 y 9.2; Orden ETU/943/2017 art. 4; RDL 7/2026 art. 1; Ley 31/2022 disp. adic. 90.ª (IPREM) | Umbral de 1,5 × IPREM de 14 pagas (8.400 €) + 0,3 por adulto y 0,5 por menor, +1 por circunstancias especiales; vulnerable severo al 50 %; vías de familia numerosa, pensionistas con la mínima e ingreso mínimo vital; descuento del 42,5 % / 57,5 % hasta el 31/12/2026 (35 % / 50 % después) |
| Bono social térmico | RDL 15/2018 arts. 6, 8 y anexo I (redacción del RDL 7/2026) | Lo cobra quien tenga el bono social eléctrico a 31 de diciembre: ayuda mínima de 50 € (+60 % si es severo). El importe exacto depende de la zona climática y del presupuesto |
| Título de familia monoparental | RDL 35/2026 arts. 1, 3, 4 y disp. final 6.ª | Solicitable desde el 1/1/2027 en la comunidad autónoma; **pendiente de convalidación** del real decreto-ley |

Se **remite al módulo fiscal** (no se duplica) para la deducción por maternidad (art. 81 LIRPF) y las de familia
numerosa o discapacidad a cargo (art. 81 bis LIRPF), que pueden cobrarse por anticipado.

Cada evaluación devuelve uno de estos estados: `cumple` (datos declarados), `probable` (algún dato estimado o
sin patrimonio declarado), `automatica` (llega sin pedirla), `proximamente`, `ya_solicitada`, `faltan_datos`
(indica qué hechos faltan) o `no_cumple`. Nunca se propone una ayuda solo con estimaciones sin decirlo, y siempre
se listan los **requisitos no comprobados** (residencia legal, convivencia de seis meses, custodia exclusiva,
titularidad del contrato de luz…).

### B. Convocatorias del BOE (`ayudas_convocatorias`)

Cada día se lee el sumario del BOE (`https://www.boe.es/datosabiertos/api/boe/sumario/AAAAMMDD`), secciones III y
V-B, con el mismo lector y la misma especificación de formato que `proactive/novedades.py`. Se quedan los títulos
que anuncian una convocatoria de ayudas, becas, subvenciones o plazas (no subastas, juntas de regantes,
expropiaciones, oposiciones, premios…), se lee el XML de cada una y se extraen, con cita literal:

- **a quién se dirige** (sección «Beneficiarios» o «Requisitos de las personas…») y se clasifica con reglas de
  palabras clave: personas, autónomos y empresas, entidades, investigación, mixta o desconocida; cada decisión
  guarda el fragmento que la provocó;
- **temas** (mayores, familias, discapacidad, jóvenes, estudios, vivienda, víctimas, desempleo, energía) y
  **ámbito territorial** (si el título o los beneficiarios nombran una comunidad);
- **plazo**: fechas explícitas, «N días hábiles», «N días naturales», «un mes», «el tercer día hábil», contados
  según el art. 30 de la Ley 39/2015. Los festivos no se conocen: la fecha de los plazos en días es una estimación
  que puede adelantarse a la real, nunca retrasarse;
- el identificador BDNS, el objeto y la cuantía.

El encaje con el perfil es conservador: `encaja` solo si se dirige a personas, algún tema lo respalda el perfil y
ninguno lo contradice (edad, hijos, discapacidad, comunidad…); `posible` si se dirige a personas pero el perfil no
permite confirmarlo (por ejemplo una beca que exige un título universitario, salvo que la persona haya indicado ese
interés en `ayudas_intereses`); `no` en otro caso. Solo se avisa de las que encajan.

**Clasificador LLM opcional**: con `AYUDAS_CLASIFICADOR_LLM=1` y una función registrada con
`convocatorias.registrar_clasificador_llm(fn)` (el núcleo decide cuál), se consulta solo cuando las reglas dejan la
convocatoria en «desconocida» o «mixta». Su respuesta se descarta salvo que la cita que aporta esté literalmente en
el texto de la convocatoria.

**BDNS** (infosubvenciones.es): el adaptador (`bdns.py`) lee el formato documentado de
`/bdnstrans/api/convocatorias?numConv=<id>` (`fechaFinSolicitud`, `tiposBeneficiarios`…) y añade el plazo exacto
y el tipo de beneficiario. **No se ha podido probar contra el servicio real** (no accesible desde el servidor de
desarrollo): está desactivado salvo `AYUDAS_BDNS=1`, se prueba con fixtures sintéticas y las rutas de los campos son
datos (`bdns.RUTAS`).

**Fuera de alcance**: los boletines autonómicos, provinciales y municipales, donde se publican la mayoría de las
ayudas a familias, al alquiler y a la vivienda. La herramienta lo dice en cada respuesta.

## Hechos del perfil

Se leen los compartidos (`edad`, `estado_civil`, `comunidad_autonoma`, `descendientes`, `num_descendientes`,
`grado_discapacidad`, `familia_numerosa`, `familia_monoparental`, `victima_violencia_genero`, `victima_terrorismo`,
`rendimiento_trabajo_bruto`, `cotizaciones_seguridad_social`, `datos_conyuge`, `alquiler_vivienda_habitual`,
`es_autonomo`, `dias_desempleo`…). Los propios, con prefijo `ayudas_`:

| Hecho | Tipo | Sensible | Para qué |
|---|---|---|---|
| `ayudas_adultos_hogar`, `ayudas_menores_hogar` | número | no | Composición de la unidad de convivencia (si no se dan, se deduce del perfil fiscal y el resultado baja a «probable») |
| `ayudas_ingresos_hogar` | número | sí | Ingresos del año anterior de todo el hogar (IMV, complemento de infancia, nacimiento) |
| `ayudas_base_imponible_hogar` | número | sí | Base imponible general y del ahorro de la última renta del hogar (bono social) |
| `ayudas_patrimonio_neto_hogar` | número | sí | Patrimonio sin vivienda habitual (IMV, complemento de infancia) |
| `ayudas_contrato_pvpc` | sí/no | no | Contrato de luz en el PVPC con comercializadora de referencia |
| `ayudas_ya_solicitadas` | lista | sí | Prestaciones que ya cobra o ha pedido: no se vuelven a proponer |
| `ayudas_pensionista`, `ayudas_pensionistas_cuantia_minima` | sí/no | sí | Convocatorias para mayores; vía de pensionistas del bono social |
| `ayudas_dependencia_grado_2_3`, `ayudas_electrodependencia` | sí/no | sí | Circunstancias especiales del bono social |
| `ayudas_intereses` | lista | no | Temas de convocatorias de los que quiere avisos aunque el perfil no baste para comprobarlos |

Si no se declaran los ingresos, se estiman con los datos fiscales (íntegros menos cotizaciones, del titular y del
cónyuge), que nunca quedan por debajo de la base imponible: la estimación solo puede hacer perder una ayuda, no
inventarla.

## Avisos proactivos

`MODULO.avisos(contexto)` es determinista e idempotente:

- `ayudas:prestacion:<id>:<año>`: prestación que corresponde (o probablemente) y no se ha pedido, con importe y
  vencimiento (31 de diciembre para el bono social, por el térmico del año siguiente; cinco años desde el nacimiento
  para la prestación por nacimiento; 1/1/2027 para el título monoparental, avisado 60 días antes).
- `ayudas:convocatoria:<BOE-ID>`: convocatoria abierta que encaja, con su fecha límite (gravedad «aviso» si quedan
  15 días o menos).

## Revisión diaria del BOE

`hacienda_gpt.modulos.ayudas.actualizar_convocatorias(hoy)` (sin CLI) lee los días pendientes (hasta 7 atrás),
guarda las convocatorias y no repite el trabajo el mismo día. El trabajo diario del núcleo debe llamarla una vez al
día antes de los avisos por caso. Con `AYUDAS_BOE_AUTOMATICO=1` la llama el propio `avisos` (una vez al día).
Nunca lanza excepciones: los errores quedan en el resumen y el día fallido se reintenta.

El almacén es `convocatorias.json` en `AYUDAS_DATA_DIR` (por defecto `./data/modulos/ayudas`, fuera de git). Solo
contiene datos públicos del BOE, nada de ningún usuario.

## Variables de entorno

| Variable | Por defecto | Efecto |
|---|---|---|
| `AYUDAS_DATA_DIR` | `./data/modulos/ayudas` | Directorio del almacén de convocatorias |
| `AYUDAS_BOE_AUTOMATICO` | desactivada | `1`: `avisos` lanza la revisión diaria del BOE |
| `AYUDAS_BDNS` | desactivada | `1`: consulta la BDNS para el plazo exacto |
| `AYUDAS_CLASIFICADOR_LLM` | desactivada | `1`: usa el clasificador LLM registrado en los casos dudosos |

## Fuentes y verificación

Leídas en www.boe.es el 13/11/2026 (textos consolidados): Ley 19/2021 (BOE-A-2021-21007), LGSS
(BOE-A-2015-11724), RDL 3/2026 (BOE-A-2026-2548, convalidado), Ley 31/2022 de PGE 2023 prorrogada
(BOE-A-2022-22128), RD 897/2017 (BOE-A-2017-11505), Orden ETU/943/2017 (BOE-A-2017-11532), RDL 7/2026
(BOE-A-2026-6544, convalidado), RDL 15/2018 (BOE-A-2018-13593), RDL 20/2022 (BOE-A-2022-22685), RDL 35/2026
(BOE-A-2026-23604, pendiente de convalidación), LIRPF (BOE-A-2006-20764) y Ley 39/2015 (BOE-A-2015-10565).

Comprobación: `python -m hacienda_gpt.modulos.ayudas.parametros <directorio con BOE-A-*.html>` y la prueba
`tests/modulos/ayudas/test_ayudas_fuentes.py` (con `HACIENDA_BOE_PAGINAS` apunta a las páginas completas).

Supuestos y límites:

- **IPREM 2026 = 600 €/mes**: es el de la Ley de PGE 2023, prorrogada; no se ha encontrado en el BOE ninguna norma
  que fije otro para 2026. Si se aprueba, hay que actualizar el dato.
- El incremento del 15 % por hijo del límite de la prestación por nacimiento se aplica de forma lineal.
- En el bono social, las circunstancias especiales suman 1 al multiplicador una sola vez aunque concurran varias.
- Para nacimientos de años anteriores se usa el límite de ingresos de 2026 y el resultado baja a «probable».
- No se modelan: compatibilidad del IMV con rentas del trabajo (RD 789/2022), incremento por alquiler, prestación
  por parto múltiple (depende del SMI, no leído), pensiones no contributivas, subsidios de desempleo, ayudas
  autonómicas y locales.

## Cómo validarlo (prueba de aceptación)

Con un hogar real, declare `ayudas_adultos_hogar`, `ayudas_menores_hogar`, `ayudas_ingresos_hogar`,
`ayudas_base_imponible_hogar`, `ayudas_patrimonio_neto_hogar`, `ayudas_contrato_pvpc` y `ayudas_ya_solicitadas`, y
compare `ayudas_comprobar` con el simulador del IMV de la Seguridad Social y con la resolución del bono social de su
comercializadora. Una ayuda que sabe que le corresponde debe aparecer como «te corresponde» o «probablemente»; una
que no le corresponde no debe aparecer.
