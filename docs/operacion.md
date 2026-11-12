# Operación: tareas programadas, avisos fuera de la aplicación y vigilancia

Esta guía cubre tres cosas:

1. cómo programar las tareas diarias de forma que quede constancia de cada ejecución;
2. cómo recibir por correo o webhook los avisos que hoy solo se ven en la web;
3. cómo enterarse de que una tarea **no** ha corrido o ha fallado.

Todo se maneja con un único comando:

```bash
uv run python -m hacienda_gpt.cli.ops --help
```

| Subcomando | Qué hace |
|---|---|
| `run <nombre> -- <comando...>` | Ejecuta el comando bajo vigilancia: apunta el resultado y avisa al *healthcheck*. |
| `deliver` | Envía por los canales configurados los avisos que aún no han salido. |
| `doctor` | Revisa que las tareas han corrido y han terminado bien; avisa de las que no. |

## 1. Líneas de cron

Cada tarea se envuelve con `ops run <nombre> --`. El nombre es el que usa `doctor`
para saber qué tareas espera (`run-daily`, `boe-watch`, `boe-impact`, `boe-novedades`, `deliver`).

```cron
# Variables: cron no carga .envrc ni el perfil de la shell. Defínelas aquí o en un
# fichero que solo pueda leer el usuario del servicio y que cada línea cargue antes.
SHELL=/bin/bash

# Normas del catálogo que han cambiado en el BOE, y aviso a los usuarios afectados.
30 6 * * *  cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.ops run boe-watch -- uv run python -m hacienda_gpt.cli.boe_watch >> logs/boe_watch.log 2>&1
45 6 * * *  cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.ops run boe-impact -- uv run python -m hacienda_gpt.cli.proactive boe-impact --changes-log data/html/_boe_changes.jsonl --snapshot-dir data/html --yes >> logs/proactive.log 2>&1

# Normas nuevas con contenido tributario en el BOE de cada día.
50 6 * * *  cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.ops run boe-novedades -- uv run python -m hacienda_gpt.cli.proactive boe-novedades >> logs/proactive.log 2>&1

# Recordatorios de plazos y plan de fin de ejercicio.
0 7 * * *   cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.ops run run-daily -- uv run python -m hacienda_gpt.cli.proactive run-daily >> logs/proactive.log 2>&1

# Entrega de los avisos generados arriba. Se registra sola con el nombre «deliver»;
# envuélvela con «ops run deliver --» solo si quieres además su ping al healthcheck
# cuando la variable genérica HEALTHCHECK_URL no está definida (ver apartado 3).
15 7 * * *  cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.ops deliver >> logs/ops.log 2>&1

# Revisión: ¿ha corrido todo? Una vez al día (cada ejecución con problemas envía un mensaje).
0 9 * * *   cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.ops run doctor -- uv run python -m hacienda_gpt.cli.ops doctor >> logs/ops.log 2>&1
```

Detalles de `ops run`:

- Lo que va detrás de `--` se ejecuta tal cual, sin shell intermedia: no admite tuberías ni
  redirecciones (las de la línea de cron se aplican al conjunto, como en el ejemplo).
- No toca la salida del comando y termina con su mismo código de salida.
- Si el comando no existe o no se puede lanzar, también queda apuntado como fallo.
- Las opciones propias (`--state-path`) van **antes** de `--`.

## 2. Avisos fuera de la aplicación (`ops deliver`)

Las tareas proactivas guardan los avisos en SQLite y la web los muestra. `ops deliver`
los envía además por los canales que estén configurados.

### Qué hace

- Un mensaje por aviso y por canal. Asunto: el título del aviso. Cuerpo: el texto del aviso
  en texto plano (sin las marcas `**`), los pasos a seguir, las fuentes y el identificador del caso.
- **Nunca envía dos veces** el mismo aviso por el mismo canal: cada entrega se apunta en la
  tabla `notification_deliveries` (misma base de datos que los avisos) solo después de que el
  canal la acepte. Si un envío falla, se reintenta en la siguiente ejecución.
- Los canales son independientes: si el correo falla, el webhook recibe igualmente todo.
  Tras tres fallos seguidos de un canal, se deja ese canal para la siguiente ejecución.
- Imprime un resumen en JSON y termina con código 1 si algún envío ha fallado.
- Sin ningún canal configurado no hace nada, lo dice y termina con código 0.

### Qué no hace

- **No envía a cada usuario.** Todavía no hay cuentas de usuario y los casos no tienen
  correo propio: todos los avisos de todos los casos van al único destinatario configurado
  (el propietario u operador de la instalación). Está pensado para uso personal o de un solo
  despacho.
- No marca los avisos como leídos en la web; son registros distintos.
- No agrupa varios avisos en un solo mensaje ni filtra por gravedad.
- No retira lo ya enviado si un aviso se sustituye por otro más reciente (por ejemplo, un
  plan de fin de ejercicio recalculado): el nuevo se envía como un aviso más.
- La entrega se apunta por nombre de canal (`webhook`, `email`), no por destino: si cambias la
  URL o la dirección, lo ya entregado no se vuelve a enviar al destino nuevo.

### Primera activación

Si la base de datos ya tiene avisos antiguos, la primera ejecución los enviaría todos.
Para darlos por entregados sin enviarlos:

```bash
uv run python -m hacienda_gpt.cli.ops deliver --marcar-entregadas
```

### Variables de entorno

Todas son opcionales; vacía = canal apagado. Los secretos (la URL del webhook, la contraseña
SMTP) van solo en el entorno, nunca en el código ni en el repositorio.

| Variable | Para qué | Por defecto |
|---|---|---|
| `NOTIFY_WEBHOOK_URL` | URL `http(s)` a la que se hace POST de cada mensaje. | vacía |
| `NOTIFY_WEBHOOK_FORMAT` | `json` o `text` (ver abajo). | `json` |
| `NOTIFY_SMTP_HOST` | Servidor SMTP. | vacía |
| `NOTIFY_SMTP_PORT` | Puerto. Con 465 se usa TLS implícito. | `587` |
| `NOTIFY_SMTP_USER` | Usuario. Vacío = sin autenticación. | vacía |
| `NOTIFY_SMTP_PASSWORD` | Contraseña (o contraseña de aplicación). | vacía |
| `NOTIFY_SMTP_STARTTLS` | STARTTLS antes de autenticarse. | `true` |
| `NOTIFY_EMAIL_FROM` | Remitente. | vacía |
| `NOTIFY_EMAIL_TO` | Destinatario (uno). | vacía |
| `DECISION_STATE_DB_PATH` | Base de datos de casos y avisos (ya existente). | `./data/decision_state.sqlite3` |

El correo necesita a la vez `NOTIFY_SMTP_HOST`, `NOTIFY_EMAIL_FROM` y `NOTIFY_EMAIL_TO`. Si
solo hay alguna, `deliver` falla con un mensaje que dice cuál falta, en lugar de callar.

### Formato del webhook

- `json` (por defecto): `POST` con `Content-Type: application/json` y cuerpo
  `{"title": "<asunto>", "text": "<cuerpo>"}`. Sirve para webhooks entrantes compatibles con
  Slack (Slack, Mattermost, Rocket.Chat…), que muestran `text`, y para receptores genéricos,
  que reciben ambos campos. En los compatibles con Slack el título no se muestra.
- `text`: `POST` con el cuerpo en `text/plain` y el asunto en la cabecera `Title`. Es lo que
  espera un tema de ntfy.sh (`NOTIFY_WEBHOOK_URL=https://ntfy.sh/<tema>`).

## 3. Saber que una tarea no ha corrido o ha fallado

Hay dos mecanismos y hacen falta los dos.

### El fichero de estado y `ops doctor`

Cada tarea envuelta con `ops run` (y `ops deliver`) apunta en `data/_jobs_state.json`
(`JOBS_STATE_PATH`) su última ejecución, el último éxito y el último error:

```json
{
  "jobs": {
    "boe-novedades": {
      "last_run": "2026-11-12T05:50:04+00:00",
      "last_ok": true,
      "last_detail": "",
      "last_success": "2026-11-12T05:50:04+00:00",
      "last_error": "2026-11-09T05:50:03+00:00",
      "last_error_detail": "código de salida 1"
    }
  }
}
```

`ops doctor` lee ese fichero y señala:

- tareas esperadas que no se han ejecutado nunca;
- tareas cuyo último éxito es demasiado antiguo;
- tareas cuya última ejecución falló (cualquiera que figure en el fichero).

Imprime los problemas en JSON, los envía por los canales de `NOTIFY_*` con el asunto
«HaciendaGPT: tarea programada con problemas» y termina con código 1 si hay alguno.

Por defecto espera, con un máximo de 36 horas desde el último éxito: `run-daily`,
`boe-novedades`, `boe-impact`, `boe-watch` y `deliver`. Para cambiarlo:

```bash
# Otra antigüedad, una tarea más, y dejar de exigir una que no usas.
uv run python -m hacienda_gpt.cli.ops doctor \
    --max-horas boe-watch=72 --max-horas mi-tarea-semanal=200 --max-horas boe-impact=0
```

`--no-notify` solo imprime, sin enviar nada.

### El interruptor de hombre muerto (*healthcheck*)

`doctor` es otra tarea de cron. Si lo que falla es cron (la máquina está apagada, se ha
perdido el crontab, el disco está lleno), `doctor` tampoco corre y nadie avisa. La única
forma de enterarse de que algo **no** ha ocurrido es que lo vigile alguien de fuera: un
servicio que espera una señal periódica y avisa cuando deja de llegar.

Funciona con la convención de [healthchecks.io](https://healthchecks.io) (o cualquier
servicio compatible, también autoalojado):

1. En el servicio se crea una comprobación por tarea, con su periodo (1 día) y un margen.
   El servicio da una URL.
2. Esa URL se pone en la variable `HEALTHCHECK_URL_<NOMBRE>`, con el nombre de la tarea en
   mayúsculas y los guiones como guiones bajos.
3. Al terminar la tarea, `ops run` llama a la URL si ha ido bien y a `<URL>/fail` si ha
   fallado (con el motivo en el cuerpo de la petición).
4. Si la tarea falla, el servicio avisa enseguida. Si la señal no llega a tiempo —porque
   la tarea no ha corrido—, avisa también. Ese segundo caso es el que cubre el hueco.

| Variable | Para qué |
|---|---|
| `HEALTHCHECK_URL_RUN_DAILY`, `HEALTHCHECK_URL_BOE_NOVEDADES`, `HEALTHCHECK_URL_BOE_IMPACT`, `HEALTHCHECK_URL_BOE_WATCH`, `HEALTHCHECK_URL_DELIVER`, `HEALTHCHECK_URL_DOCTOR`… | URL de la comprobación de esa tarea. |
| `HEALTHCHECK_URL` | URL genérica para las tareas sin variable propia. Todas comparten entonces una sola comprobación: el servicio no distingue cuál ha dejado de correr y un éxito de una tapa el silencio de otra. Úsala solo si vigilas una única tarea. |
| `JOBS_STATE_PATH` | Fichero de estado. Por defecto, `./data/_jobs_state.json`. |

Un fallo de red al avisar al healthcheck nunca hace fallar la tarea: se anota en el log y
se sigue. Lo mismo si no se puede escribir el fichero de estado.

Lo mínimo recomendable: una comprobación para `doctor` (`HEALTHCHECK_URL_DOCTOR`, con la
línea de cron del apartado 1). Así, si cron muere, lo sabrás por el servicio externo; y si
cron vive pero falla una tarea, lo sabrás por `doctor`.

### Desde código

Para vigilar una función en lugar de un comando:

```python
from hacienda_gpt.ops.vigia import vigilado

@vigilado("mi-tarea")
def mi_tarea() -> None:
    ...

# o bien
with vigilado("mi-tarea"):
    ...
```

Apunta el resultado, avisa al healthcheck y deja pasar la excepción sin tocarla.

## 4. Vigilancia en GitHub Actions

`.github/workflows/vigilancia.yml` ejecuta cada lunes
`uv run python -m hacienda_gpt.cli.proactive params-check --fail-on-stale`. Una ejecución en
rojo significa que el BOE ha actualizado alguna de las normas de las que salen los parámetros
de la calculadora (hay que releerla; no implica que las cifras hayan cambiado), o que no se
pudo consultar el BOE. Solo necesita acceso a `www.boe.es`.

## Consumo de modelos de IA: medirlo y limitarlo

Una sola pregunta puede costar muchas llamadas al modelo: el agente da varias vueltas, en modo
equipo intervienen además un planificador, dos investigadores y un crítico, después pasa el
verificador, y un documento escaneado se lee dos veces. `hacienda_gpt/ops/consumo.py` apunta
cada llamada y, si se configuran límites, corta antes de hacer la siguiente.

### Qué se guarda

Una fila por llamada en la tabla `llm_usage` de la misma base de datos SQLite que los casos
(`DECISION_STATE_DB_PATH`): fecha y hora (UTC), usuario y caso si se conocen, propósito
(`agente`, `verificador`, `documentos`…), proveedor, modelo, tokens de entrada, de salida, leídos
de caché y escritos en caché, y el coste en USD. No se guarda ningún texto de la consulta ni de
la respuesta.

Los tokens son los que devuelve la propia API. Si la API no los devuelve (pasa con algunos
servidores locales, sobre todo en respuestas en streaming), la llamada se apunta igualmente, con
los tokens y el coste vacíos.

### De dónde sale el coste

De una tabla de precios que es un fichero de datos, `hacienda_gpt/ops/data/precios_llm.json`: USD
por millón de tokens de entrada, salida, lectura de caché y escritura de caché para cada modelo,
con la fecha en que se consultaron (`consultado_el`) y la fuente.

- **Los precios cambian.** La tabla es una foto de esa fecha: revísala contra la página de precios
  del proveedor. Para actualizarla sin tocar el código, copia el fichero, corrígelo y apunta
  `LLM_PRICES_PATH` a la copia.
- **Nunca se estima.** Un modelo que no está en la tabla, o con el precio a `null`, se apunta sin
  coste. Hoy solo tiene precio `claude-opus-5-5`; `gpt-4o-mini` está a `null` (no se pudo
  consultar) y los modelos locales no figuran. Si cambias `ANTHROPIC_MODEL`, `VERIFIER_MODEL` u
  `OPENAI_AGENT_MODEL`, añade el modelo a la tabla.
- El coste es una estimación para operar, no una factura: no incluye descuentos, modo rápido ni
  las tarifas de Bedrock o Vertex.

En los informes, `llamadas_sin_coste` dice cuántas llamadas no suman coste: si no es cero, el
total en USD es un mínimo.

### Límites

Todos opcionales; vacío significa sin límite. Un valor que no sea un número mayor que cero impide
arrancar el agente (mejor eso que un límite que no limita).

| Variable | Qué limita |
|---|---|
| `LLM_DAILY_BUDGET_USD` | Gasto total del servicio en un día. |
| `LLM_DAILY_BUDGET_USD_PER_USER` | Gasto de cada usuario en un día. Solo se aplica a las llamadas atribuidas a un usuario. |
| `LLM_MAX_CALLS_PER_QUESTION` | Llamadas al modelo en una misma pregunta (agente, equipo y verificador juntos). |
| `LLM_PRICES_PATH` | Tabla de precios alternativa. |

El día es el día natural de la península (Europe/Madrid): los límites diarios se restablecen a las
00:00. Al llegar a un límite, la consulta recibe un error 429 con un mensaje en español que dice
qué límite es y cuándo se restablece.

Lo que conviene saber antes de fiarse de ellos:

- Se comprueban **antes** de cada llamada, con lo ya gastado. La llamada que cruza el límite se
  completa, y si hay varias a la vez pueden cruzarlo todas: el exceso es de unas pocas llamadas,
  no de un día entero.
- Los límites en USD solo cuentan las llamadas con coste conocido. Con un modelo sin precio en la
  tabla **no protegen nada** (queda un aviso en el log la primera vez); el tope de llamadas por
  pregunta sí funciona siempre.
- Si el límite salta a mitad de una pregunta, lo ya gastado no se recupera. Cuando salta en el
  verificador, la respuesta se entrega marcada como no verificada, igual que si el verificador
  hubiera fallado.
- Un tope de llamadas por pregunta demasiado bajo empeora las respuestas. Como referencia, sin
  modo equipo una pregunta usa como mucho `AGENT_MAX_STEPS` vueltas por intento, más una
  verificación por intento.

### Consultarlo

```bash
# Total y desglose por día, propósito, usuario y caso de los últimos 7 días, en JSON.
uv run python -m hacienda_gpt.cli.consumo resumen --dias 7
```

`GET /usage` (con la cabecera `X-API-Key` si `HACIENDA_API_KEY` está definida) devuelve los
totales de hoy y de los últimos 30 días, en conjunto y por propósito, junto con los límites
configurados y la fecha de la tabla de precios. Son cifras de todo el servicio, pensadas para
quien lo opera.
