# Módulo `luz`: la factura de la luz del hogar (peaje 2.0TD)

Qué responde:

1. **Reproduce una factura real al céntimo** a partir del consumo (CSV horario de Datadis o de la distribuidora,
   o los kWh de P1/P2/P3 que trae la propia factura), las potencias y los precios del contrato, línea a línea y
   con la fuente de cada importe.
2. **Compara tarifas**: cuánto habría costado cada oferta guardada (y el PVPC si hay precios de REE) con el
   mismo consumo de los últimos 12 meses, impuestos incluidos, y cuál es la más barata.
3. **Avisa** cuando una oferta guardada ahorra más de un umbral al año, y cuando vence el precio o la
   permanencia del contrato actual.
4. **Horas baratas** de mañana: periodos punta/llano/valle y, con precios de REE, el PVPC hora a hora, las horas
   y el bloque de horas seguidas más baratos.
5. **Potencia óptima** con los datos del maxímetro (o, si no hay, un mínimo orientativo con el consumo horario).

Código: `hacienda_gpt/modulos/luz/` · datos regulados: `data/regulados.json` · pruebas: `tests/modulos/luz/` ·
extractos del BOE y respuesta de ejemplo de REE: `tests/fixtures/modulos/luz/`.

## Cómo se calcula una factura 2.0TD

| Línea | Cálculo | Fuente |
|---|---|---|
| Potencia P1 (punta-llano) y P2 (valle) | Oferta libre: kW × días × €/kW·día (o €/kW·año × días ÷ días del año). PVPC: kW × (peaje + cargo) €/kW·año × días ÷ días del año, más el término fijo de comercialización (3,113 €/kW·año) sobre P1 | Contrato; Resoluciones CNMC de peajes; Órdenes de cargos; RD 216/2014 art. 8.1; Orden ETU/1948/2016 |
| Energía P1, P2, P3 | kWh de cada periodo × precio. Si la oferta se da «sin peajes», se suman peajes y cargos de energía. PVPC: Σ kWh de cada hora × precio horario de REE (indicador 1001, ya incluye peajes y cargos de energía) | Contrato; Circular 3/2020 art. 7; RD 216/2014 art. 8.2 |
| Descuentos | % sobre la energía o la potencia propias de la oferta, antes de impuestos | Contrato |
| Financiación del bono social | Valor por CUPS y año × días ÷ días del año (o el €/día de la oferta) | Órdenes TED/1487/2024, TED/1524/2025 y TED/634/2026; RD 216/2014 art. 8.4 |
| Impuesto sobre la electricidad | Tipo × (potencia + energía + bono social), nunca menos de 1 €/MWh (uso doméstico) | Ley 38/1992 arts. 97 y 99 |
| Alquiler del contador | 0,81 €/mes (monofásico) o 1,36 €/mes (trifásico) × 12 ÷ días del año × días (o el €/día de la oferta; nada si es propio) | Orden IET/1491/2013, DT única |
| IVA | Tipo × (todo lo anterior + impuesto eléctrico) | Ley 37/1992 art. 90; RDL 7/2026 art. 42 |

Cada importe se redondea al céntimo (mitad hacia arriba) una sola vez por línea; las líneas con el mismo concepto,
precio y régimen fiscal se suman antes de redondear. El periodo se parte en tramos cuando cambia un valor regulado
(peajes y cargos el 1 de enero, bono social, rebaja temporal de impuestos) y cada tramo se muestra con sus fechas.
Las fechas son las de la factura, **ambas incluidas** (del 15/09 al 14/10 = 30 días).

## Valores regulados (leídos en www.boe.es el 13/11/2026)

| Dato | 2025 | 2026 | Norma |
|---|---|---|---|
| Peaje T&D potencia P1 / P2 (€/kW·año) | 22,958932 / 0,442165 | 23,324952 / 0,443770 | Res. CNMC 4-12-2024 (BOE-A-2024-26218) y 18-12-2025 (BOE-A-2025-26348), anexo I |
| Peaje T&D energía P1 / P2 / P3 (€/kWh) | 0,034234 / 0,016540 / 0,000079 | 0,033261 / 0,016409 / 0,000077 | ídem |
| Cargos potencia P1 / P2 (€/kW·año), segmento 1 = 2.0 TD | 3,971618 / 0,255423 | 4,379461 / 0,281653 | Orden TED/1487/2024 (BOE-A-2024-27289), TED/1524/2025 (BOE-A-2025-26705); RD 148/2021 art. 3 |
| Cargos energía P1 / P2 / P3 (€/kWh) | 0,058305 / 0,011661 / 0,002915 | 0,064292 / 0,012858 / 0,003215 | ídem |
| Bono social (comercialización, €/CUPS·año) | 4,650987 | 6,979247 hasta el 30/06; 9,011295 desde el 1/07 | TED/1487/2024; TED/1524/2025; TED/634/2026 (BOE-A-2026-13759) |
| Impuesto sobre la electricidad | 5,11269632 %, mínimo 1 €/MWh | igual, salvo rebajas | Ley 38/1992 art. 99 (BOE-A-1992-28741) |
| IVA | 21 % | 21 %, salvo rebajas | Ley 37/1992 art. 90 (BOE-A-1992-28740) |

**Rebajas temporales de 2026** (contratos de hasta 10 kW para el IVA; el impuesto eléctrico, todos):

| Días | IVA | Impuesto eléctrico | Estado | Norma |
|---|---|---|---|---|
| 22/03 – 31/05 | 10 % | 0,5 % (mín. 1 €/MWh) | aplicada | RDL 7/2026 arts. 40 y 42 (BOE-A-2026-6544) |
| junio | 21 % | 5,11269632 % | no se aplicó (lo dice el preámbulo del RDL 18/2026) | BOE-A-2026-14112 |
| agosto | 10 % / 0,5 % solo si el IPC de la electricidad de junio subió más del 15 % | | **condicional**: el BOE no dice si se cumplió | RDL 18/2026 arts. 10 y 12 |
| septiembre | 21 % | 5,11269632 % | no se aplicó (preámbulo del RDL 25/2026) | BOE-A-2026-20265 |
| noviembre, diciembre | 10 % / 0,5 % solo si el IPC de septiembre / octubre subió más del 15 % | | **condicional** | RDL 25/2026 arts. 18 a 21 |

En los meses condicionales el módulo usa los tipos generales y lo avisa; si la factura aplicó la rebaja, se repite
el cálculo con `tipos_reducidos="si"`. Para 2025 no se ha encontrado en el BOE ninguna rebaja temporal.

**Periodos 2.0TD** (Circular 3/2020, art. 7.3 y 7.4, BOE-A-2020-1066): días laborables, punta 10-14 y 18-22 h,
llano 8-10, 14-18 y 22-24 h, valle 0-8 h (Ceuta y Melilla, una hora más tarde); sábados, domingos, 6 de enero y
festivos nacionales de fecha fija no sustituibles, todo valle. Los festivos se leen de las resoluciones de fiestas
laborales de 2025, 2026 y 2027 (BOE-A-2024-21316, BOE-A-2025-21667, BOE-A-2026-21254): filas con «*» en todas las
comunidades; el Viernes Santo queda fuera por no tener fecha fija. Los días de cambio de hora (23 y 25 horas) son
domingos: todo valle.

Cada grupo de `regulados.json` guarda `fuente` (referencia, en vigor desde, verificado, nota), las citas literales
(`evidencia`) y cómo está escrita cada cifra en su cita (`cifras`). Comprobación contra las páginas del BOE:

```
uv run python -m hacienda_gpt.modulos.luz.datos <directorio con BOE-*.html descargados>
```

Las pruebas hacen lo mismo contra extractos literales pequeños (y contra las páginas completas si
`LUZ_BOE_PAGINAS` apunta a ellas). El SHA-256 de cada página está en `fuentes_principales`.

## Datos del usuario (hechos del expediente)

| Hecho | Tipo | Sensible | Contenido |
|---|---|---|---|
| `luz_cups` | texto | sí | CUPS (ES + 16 cifras + 2 letras [+ 2]) |
| `luz_potencia_p1_kw`, `luz_potencia_p2_kw` | número | | Potencias contratadas (0–15 kW) |
| `luz_zona` | texto | | peninsula, baleares, canarias, ceuta, melilla (si falta, se deduce de `comunidad_autonoma`) |
| `luz_tarifa_actual` | JSON | sí | La oferta del contrato (formato abajo) |
| `luz_ofertas` | JSON | | Lista de ofertas a comparar |
| `luz_consumo` | JSON | sí | Resumen del consumo (abajo) |

Oferta: `{"nombre", "tipo": "fijo"|"pvpc", "energia_eur_kwh": 0.12 o {"p1","p2","p3"}, "potencia_eur_kw_dia":
{"p1","p2"} (o "potencia_eur_kw_anio"), "incluye_peajes_cargos": true, "descuento_energia_pct",
"descuento_potencia_pct", "bono_social_eur_dia" (si falta: valor regulado; 0: no se cobra), "contador":
"monofasico"|"trifasico"|"propio", "alquiler_contador_eur_dia", "servicios_eur_mes", "precio_valido_hasta",
"permanencia_hasta", "penalizacion_eur", "notas"}`. Los precios de una comercializadora libre son datos del
usuario, no normas: el módulo solo comprueba que sean coherentes.

**Por qué `luz_consumo` no guarda la serie horaria completa.** Un año son 8.760 valores. Se guarda: el consumo de
cada día por periodo P1/P2/P3 de los últimos 400 días (basta para rehacer exactamente cualquier factura de precio
fijo), los valores horarios de los últimos 62 días (basta para rehacer exactamente la última factura del PVPC), la
suma de cada hora del día por mes (para estimar el PVPC de meses anteriores), el máximo horario de cada mes, los
totales de factura tecleados y el maxímetro. Un año de un piso ocupa unos 35 KB.

## Herramientas

| Herramienta | Efecto | Entradas | Salida |
|---|---|---|---|
| `luz_calcular_factura` | lectura | `fecha_inicio`, `fecha_fin` (incluidas), opcionales `consumo_kwh` {p1,p2,p3}, `csv`, `oferta`, potencias, `precios_pvpc` (JSON del indicador 1001), `tipos_reducidos` (auto/si/no), `total_factura_real` | Cada línea con su cálculo y su norma, total, diferencia con la factura real y avisos |
| `luz_comparar_tarifas` | lectura | opcionales `ofertas`, `incluir_pvpc`, `dias` | Ranking con coste del periodo y anual, ahorro frente a la actual, permanencias |
| `luz_importar_consumo` | prepara (escribe en el expediente) | `csv`, o `totales` [{desde,hasta,p1,p2,p3}], o `maximetro` [{mes,p1_kw,p2_kw}], `guardar` | Resumen por meses; guarda `luz_consumo` (y `luz_cups` si faltaba) |
| `luz_horas_baratas` | lectura (consulta REE si hay token) | `fecha` (mañana por defecto), `horas`, `bloque_horas`, `precios_pvpc`, `consultar_pvpc` | Periodos del día y, con precios, el PVPC hora a hora, las horas y el bloque más baratos |
| `luz_potencia_optima` | lectura | `maximos` [{mes,p1_kw,p2_kw}], `margen_pct` (10) | Potencia suficiente, máximo observado, ahorro anual aproximado |

Ejemplo (prueba `test_hand_checked_bill_of_a_fixed_price_offer`): 4,6 kW, del 15/09 al 14/10/2026, 80/70/120 kWh,
0,15/0,12/0,08 €/kWh y 0,089/0,012 €/kW·día → potencia 12,28 + 1,66; energía 12,00 + 8,40 + 9,60; bono social 0,74;
impuesto eléctrico 2,28; contador 0,80; IVA 10,03; **total 57,79 €**.

## Formatos de consumo y precios (no verificados en vivo)

Solo www.boe.es es accesible desde la máquina donde se escribió el módulo: Datadis, las distribuidoras y REE se
han modelado con su formato documentado y se prueban con ficheros construidos a partir de él.

- **CSV de consumo**: la cabecera decide las columnas (`CUPS`, `Fecha`, `Hora`, `Consumo`/`AE_kWh`,
  `Metodo_obtencion`/`REAL/ESTIMADO`, energía vertida). Separador `;`, `,` o tabulador; coma decimal; fechas
  `AAAA/MM/DD`, `AAAA-MM-DD` o `DD/MM/AAAA`; hora 1-24 (1-23/1-25 en los cambios de hora), `01:00`…`24:00`
  (hora que termina) o `00:00`…`23:00` (hora que empieza); ficheros cuartohorarios se suman por hora. Se avisa de
  días incompletos, horas estimadas y energía vertida (el autoconsumo no se calcula).
- **Precios PVPC**: API ESIOS de REE, indicador 1001 (término de energía del PVPC 2.0TD, €/MWh, peajes y cargos de
  energía incluidos), `GET https://api.esios.ree.es/indicators/1001?start_date&end_date&geo_ids[]`, cabecera
  `x-api-key`. Token en la variable de entorno **`LUZ_ESIOS_TOKEN`** (se pide a REE). Zonas: 8741 Península, 8742
  Canarias, 8743 Baleares, 8744 Ceuta, 8745 Melilla. Si REE publica valores cuartohorarios, se promedian por hora
  (aproximación, se avisa). Sin token, el usuario puede pegar el JSON del indicador. No se envía ningún dato
  personal: la petición solo lleva fechas, el indicador y el token.

Variables de entorno: `LUZ_ESIOS_TOKEN` (precios de REE) y `LUZ_UMBRAL_AHORRO_EUR` (umbral del aviso de ahorro,
50 €/año por defecto).

## Avisos proactivos (deterministas, sin red)

| Clave | Cuándo |
|---|---|
| `luz:ahorro:<oferta>:<último día con datos>` | Una oferta de precio fijo guardada ahorra más del umbral al año con al menos 90 días de consumo (sustituye a los `luz:ahorro:` anteriores; menciona la permanencia) |
| `luz:fin_precio:<fecha>`, `luz:fin_permanencia:<fecha>` | El precio garantizado o la permanencia de la tarifa actual vence en 45 días o menos |
| `luz:datos_regulados:<año>` | Empieza un año sin peajes y cargos cargados |

El PVPC no entra en el aviso diario (necesitaría red); sí en `luz_comparar_tarifas`.

## Comprobación con una factura real (fase de aceptación)

Hace falta de la factura: fechas del periodo, potencias P1/P2, kWh de P1/P2/P3 (o el CSV horario de Datadis del
mismo periodo), los precios exactos del contrato con todos sus decimales, si cobran el bono social y a cuánto por
día, el alquiler del contador por día, y el total. Con eso, `luz_calcular_factura` con `total_factura_real` dice si
coincide al céntimo o cuánto se separa. Fuentes conocidas de diferencias de un céntimo: comercializadoras que
redondean el precio diario de la potencia a 6 decimales antes de multiplicar, o que pasan a días el alquiler del
contador de otra forma (el módulo usa 12 meses ÷ días del año). Para el PVPC hacen falta además los precios de REE.

## Lo que no está modelado

Autoconsumo y compensación de excedentes; contratos de más de 15 kW; excesos de potencia y energía reactiva;
descuentos del bono social (42,5 % / 57,5 % en 2026 por el RDL 7/2026); ofertas indexadas distintas del PVPC;
IGIC de Canarias e IPSI de Ceuta y Melilla (allí la factura no se calcula); peajes y cargos de 2027 (no
publicados al escribir esto); regularizaciones de lecturas estimadas; costes de cambiar de potencia.

## Dudas abiertas

- **Término fijo de comercialización del PVPC (3,113 €/kW·año)**: la Orden ETU/1948/2016 lo fija para 2016-2018 y
  no se ha encontrado en el BOE una orden posterior; se usa con `verificado: false` y se avisa en cada factura PVPC.
- **Alquiler del contador**: precio «provisional» de la Orden IET/1491/2013, sin sustituto encontrado.
- **Bono social julio 2026**: la Orden TED/634/2026 surte efectos el 26/06 y se aplica «desde la liquidación 7»; el
  módulo cambia el 1/07.
- **Base del impuesto eléctrico**: se toma potencia + energía + bono social, sin el alquiler del contador (art. 97
  remite a la base del IVA «para un suministro de energía eléctrica»; el alquiler es otro servicio). **IVA reducido y
  contador**: se aplica al alquiler el mismo tipo que al suministro (accesorio) y se avisa.
- **Devengo de las rebajas**: se prorratean por días de consumo; el art. 90.Dos LIVA liga el tipo al devengo (en
  suministros, la exigibilidad del precio), así que una comercializadora podría aplicar un único tipo a toda la factura.
- Meses de rebaja **condicional** (agosto, noviembre y diciembre de 2026): dependen de datos del INE que no están en
  el BOE.
