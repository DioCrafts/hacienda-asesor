# Módulo `caducidades`: caducidades, ITV, renovaciones y garantías

Objetivo: que en el hogar no se pase ninguna fecha. El módulo lleva la cuenta de:

1. **Documentos y obligaciones periódicas**: DNI, pasaporte, permiso o licencia de conducción, certificado
   electrónico (FNMT u otro prestador), otros documentos con fecha impresa (tarjeta sanitaria europea…),
   ITV de cada vehículo, renovación de seguros y de otros contratos.
2. **Compras**: plazo de devolución (desistimiento) y garantía legal, a partir de la fecha de compra o de
   entrega; propuesta de compra leída de un tique o factura.

Cada fecha se calcula de forma determinista con la regla leída del BOE (con cita literal) o se toma de la fecha
impresa en el documento, **que siempre prevalece**.

Código: `hacienda_gpt/modulos/caducidades/` · datos: `data/reglas.json` · pruebas: `tests/modulos/caducidades/` ·
extractos del BOE para las pruebas: `tests/fixtures/modulos/caducidades/`.

## Reglas y fuentes (leídas en www.boe.es el 13/11/2026)

| Grupo | Regla | Norma (texto consolidado) |
|---|---|---|
| `dni` | 2 años si < 5 años; 5 años de 5 a 29; 10 años de 30 a 69; permanente desde 70 — edad **al expedir o renovar** | RD 1553/2005, art. 6.1 (BOE-A-2005-21163) |
| `pasaporte` | 2 años si < 5; 5 años si < 30; 10 años desde 30, edad en la expedición; menores de 14 sin DNI: como máximo hasta los 14 | RD 896/2003, art. 5.1 y 5.2 (BOE-A-2003-13978) |
| `permiso_conduccion` | AM, A1, A2, A, B, B+E y licencias: 10 años (< 65), 5 años (≥ 65). C1…D+E: 5 años (< 65), 3 años (≥ 65). Prórroga hasta 3 meses antes sin perder días | RGC, RD 818/2009, arts. 12, 13.1, 45 y anexo III (BOE-A-2009-9481) |
| `itv` | Tabla por categoría y antigüedad (M1: exento hasta 4 años, bienal, anual desde 10…); si se pasa en los 30 días anteriores a la caducidad, el plazo cuenta desde la caducidad | RD 920/2017, arts. 4.2, 6, 17 y 18.1 (BOE-A-2017-12841) |
| `certificado_electronico` | Vigencia máxima de 5 años (la concreta, la del certificado) | Ley 6/2020, arts. 4.2 y 7 (BOE-A-2020-14046); RD 1553/2005 art. 12.1 |
| `seguro` | Oposición a la prórroga por escrito al menos 1 mes antes (tomador); cambios comunicados 2 meses antes; no en vida (art. 22.5) | Ley 50/1980, art. 22 (BOE-A-1980-22501) |
| `garantia` | Bienes: 3 años desde la entrega; digital: 2 años; segunda mano: pacto ≥ 1 año; presunción: 2 años (bienes) / 1 año (digital); prescripción 5 años | TRLGDCU arts. 114, 120–124 (BOE-A-2007-20555), en vigor desde el 1/1/2022 (RDL 7/2021, DF 8.ª d), BOE-A-2021-6872) |
| `desistimiento` | 14 días naturales (30 en visitas no solicitadas o excursiones) desde la entrega (o el contrato, en contenido digital sin soporte); solo a distancia o fuera del establecimiento; excepciones literales del art. 103 | TRLGDCU arts. 92, 102–105 |
| `computo` | Por días se excluye el inicial; por meses o años, de fecha a fecha; si falta el día, el último del mes | Código Civil, art. 5.1 (BOE-A-1889-4763) |

Cada grupo de `reglas.json` guarda `fuente` (referencia, en vigor desde, verificado, nota), las citas literales
(`evidencia`) y cómo está escrita cada cifra en su cita (`cifras`). Comprobación:

```
uv run python -m hacienda_gpt.modulos.caducidades.reglas <directorio con BOE-A-*.html descargados>
```

comprueba que cada cifra está en su cita y significa el dato guardado, y que cada cita está literal en la ley.
Las pruebas hacen lo mismo contra extractos pequeños; el SHA-256 de cada página completa está en
`fuentes_principales`.

### Interpretaciones (dichas también en los avisos de cada resultado)

- **Edad para el permiso**: «mientras su titular no cumpla los sesenta y cinco años» se aplica a la edad en la
  fecha en que empieza cada período (expedición o prórroga).
- **ITV**: la frecuencia se toma por la antigüedad del vehículo (desde la primera matriculación) en la fecha a la
  que se suma el plazo; cada tramo empieza el día del aniversario («Hasta 4 años: Exento» → primera ITV el día que
  cumple 4; a los 10 años exactos pasa a anual). Sin la fecha de la última ITV se supone que cada inspección se
  pasó a su vencimiento.
- **Pasaporte de menores de 14**: si no se sabe si el menor tenía DNI, se toma por prudencia el tope de los 14.
- **Plazos de compra**: de fecha a fecha y excluyendo el día inicial (art. 5.1 CC); la entrega es la del tique o la
  del albarán si es posterior (art. 123.1 TRLGDCU).

## Hechos del módulo

No se guarda **ningún identificador personal**: ni número de DNI o pasaporte, ni matrícula, ni número de serie.
Solo el tipo, de quién es (`titular`, `cónyuge`, `hijo N`) y las fechas; si se intenta guardar un campo `numero`,
`matricula`, etc., la validación lo rechaza.

| Hecho | Tipo | Sensible | Contenido |
|---|---|---|---|
| `caducidades_documentos` | json | sí (datos de terceros) | `[{tipo, titular, fecha_expedicion?, fecha_caducidad?, clases?, nombre?, tiene_dni?, id?}]` |
| `caducidades_fechas_nacimiento` | json | sí | `{"titular": "AAAA-MM-DD", "cónyuge": …, "hijo 1": …}`; para hijos también se usa `descendientes[n].fecha_nacimiento` |
| `caducidades_vehiculos` | json | no | `[{etiqueta, categoria, fecha_primera_matriculacion, fecha_ultima_itv?, fecha_caducidad_itv_anterior?, fecha_proxima_itv?, historico?}]` |
| `caducidades_contratos` | json | no | `[{tipo: seguro\|seguro_vida\|otro, etiqueta, fecha_vencimiento, periodicidad_meses?, preaviso_dias?}]` |
| `caducidades_compras` | json | sí (hábitos de compra) | `[{descripcion, fecha_compra, fecha_entrega?, tipo_bien, modalidad, garantia_pactada_anos?, garantia_comercial_hasta?, vendedor_empresa?, establecimiento?, importe?}]` |
| `caducidades_antelaciones` | json | no | `{"itv": 45, "pasaporte": 120}`: días del primer aviso por tipo |

Categorías de vehículo: `M1`, `M2_M3`, `N1`, `N2_N3_O`, `O2_caravana`, `L1e`, `L` (motos y quads),
`escuela_M1_L`, `M1_taxi_ambulancia_escolar`.

## Herramientas

- `caducidades_proximas` (`dias`=365, `incluir_vencidos`=true): lista por fecha con qué hacer, dónde y la base legal;
  incluye lo que no se ha podido calcular y por qué.
- `caducidades_calcular` (`regla` = dni | pasaporte | permiso_conduccion | itv | certificado_electronico | seguro |
  garantia | desistimiento, más sus fechas): una sola regla con el cálculo paso a paso y su cita literal.
  Ejemplo: `{"regla": "dni", "fecha_expedicion": "2020-03-14", "fecha_nacimiento": "1990-03-15"}` →
  «DNI: 14/03/2025 … Validez: 5 años [art. 6.1 RD 1553/2005]» con la letra b) del artículo citada.
- `caducidades_registrar_compra` (efecto `prepara`; escribe solo en el expediente del usuario): devuelve hasta
  cuándo se puede devolver y la garantía, y guarda la compra (`guardar`=false para solo calcular).
- `caducidades_leer_ticket` (`texto`, `propuesta` opcional de `{campo, valor, evidencia}`): propone fecha, importe
  (línea TOTAL), descripción y establecimiento; descarta todo dato cuyo fragmento no esté literal en el texto o cuyo
  valor no esté en su fragmento. No guarda nada.

Para un archivo subido, `ticket.leer_ticket(provider, filename=…, data=…)` reutiliza el lector de documentos
fiscales (`extract_text`, `tipo_de_imagen`, y la normalización de pasajes de `tax/documentos.py`): texto → una
lectura comprobada contra el texto; imagen o PDF escaneado → dos lecturas independientes y solo lo que coincide.

## Avisos proactivos

Diarios, deterministas e idempotentes. Por cada fecha se emite solo el umbral más cercano ya alcanzado; clave
`caducidades:<id>:<fecha>:<umbral>d` (o `:vencido`), y cada aviso de un elemento sustituye a los anteriores del
mismo elemento (`sustituye_prefijo = caducidades:<id>:`). Gravedad: info (> 30 días), aviso (8–30), urgente (≤ 7 o
vencido). Umbrales por defecto (decisión de producto, no cifras legales): DNI 60/30/7, pasaporte 90/30/7,
permiso 90/30/7, ITV 30/7, certificado 30/7, otro documento 60/30/7, seguro y contrato 30/7 antes del último día
para darse de baja, desistimiento 7/2, fin de la presunción 30, garantía legal y comercial 30/7. El primero de
cada tipo se cambia con `caducidades_antelaciones`. Las fechas ya pasadas de compras, seguros y contratos no
avisan; los documentos e ITV caducados sí (urgente).

## No modelado

Validez excepcional del DNI y duplicados; validez limitada del pasaporte (art. 5.3–5.8); reducción del permiso
por salud, puntos y permisos extranjeros; ITV de históricos, tractores y vehículos especiales, e inspecciones
extraordinarias; vigencia concreta de cada certificado; compras entregadas antes del 1/1/2022, suministro
continuo de contenido digital, suspensión por reparación y garantías comerciales (salvo la fecha que se
introduzca); ampliación del desistimiento por falta de información (se explica, no se calcula). Las webs de
trámite son solo raíces de sitios oficiales y no se han podido abrir desde este entorno (solo www.boe.es es
accesible).

## Cómo validarlo con la realidad

Con los documentos del hogar en la mano: introducir para cada uno la fecha de expedición y comprobar que la
fecha calculada coincide con la impresa (DNI y pasaporte pueden diferir en días; el permiso y la tarjeta ITV
deben coincidir); para el coche, comparar la próxima ITV calculada con la anotada en la tarjeta ITV; para una
compra online reciente, comparar el plazo de devolución con el que indica la tienda.
