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
| `formatos …` | Vigila si los formatos externos (BOE, boletines, documentos) siguen entendiéndose; ver la sección 6. |

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

## 5. Recodificación automática de parámetros

Cuando un grupo de parámetros deja de coincidir con su ley (`params-verify`),
`params-recode` hace que un modelo relea el artículo y **proponga** datos
nuevos. Nada llega a `hacienda_gpt/tax/data/` sin pasar un control determinista
y, salvo que actives la auto-aplicación, sin que tú lo apliques. Explicación y
garantías en el README («Recodificación automática»).

### Ajustes

| Variable | Por defecto | Qué hace |
|---|---|---|
| `RECODE_PROPOSALS_PATH` | `./data/recodificacion` | Carpeta de las propuestas (un JSON por propuesta). Haz copia: es el registro de quién propuso qué. |
| `RECODE_AUTO_APPLY` | `false` | `true`: aplica sin esperar lo que supera el control sin nada por revisar y con la conformidad del revisor. Nunca retiradas ni versiones históricas. |
| `RECODE_MAX_ITER` | `3` | Pasadas del lector por grupo antes de `sin_converger`. |
| `RECODE_MAX_PER_RUN` | `10` | Grupos por ejecución; el resto espera a la siguiente. |
| `RECODE_MODEL` / `RECODE_REVIEWER_MODEL` | (modelo del proveedor) | Modelo del lector y del revisor. Un revisor distinto hace la revisión más independiente. |

Usa el proveedor de `LLM_PROVIDER` y cuenta contra `LLM_DAILY_BUDGET_USD`
(propósitos `recodificacion` y `recodificacion-revisor` en `ops consumo`). Si
el presupuesto se agota a mitad, la ejecución se detiene, lo dice en
`presupuesto_agotado` y termina con código 1; lo que no se procesó figura en
`sin_procesar` y se retoma en la siguiente.

### Paso a paso

```bash
# 1. Qué se releería y cuánto texto se enviaría. No llama a ningún modelo.
python -m hacienda_gpt.cli.proactive params-recode --dry-run

# 2. Lanzar el bucle (todos los ejercicios; --comunidad / --tipo para acotar).
python -m hacienda_gpt.cli.proactive params-recode --comunidad galicia

# 3. Ver las propuestas y leer una entera.
python -m hacienda_gpt.cli.proactive params-proposals --estado propuesta
python -m hacienda_gpt.cli.proactive params-proposals --show <id>

# 4. Decidir.
python -m hacienda_gpt.cli.proactive params-apply <id>
python -m hacienda_gpt.cli.proactive params-reject <id> --motivo "…"

# 5. Tras revisar a mano lo aplicado, quitar la marca «pendiente de revisión humana».
python -m hacienda_gpt.cli.proactive params-proposals --pendientes
python -m hacienda_gpt.cli.proactive params-confirm <id>
```

Qué mirar en una propuesta antes de aplicarla:

- `ejercicios` y `ejercicios_excluidos`: qué ficheros toca y por qué deja los
  demás. Salen de `efectos_desde` y `cita_efectos`; comprueba que la cita es la
  cláusula que rige («con efectos desde…», «períodos impositivos iniciados a
  partir de…») y no solo la entrada en vigor.
- `antes` y `despues`, con `citas`: cada cifra de `despues` está en alguna cita
  (lo garantiza el control); lo que debes juzgar es si la cita dice eso.
- `control.revisar`: lo que el control no pudo decidir con sus reglas. Con algo
  aquí la propuesta nunca se auto-aplica.
- `veredicto.discrepancias` (las `observacion` no bloquean) y `casos`: los
  contribuyentes de prueba del revisor, con lo que da la ley y lo que da la
  calculadora.
- `resultado: "retirada"`: el lector cree que la deducción ya no encaja en el
  esquema (`motivo_lector`). Aplicarla la quita de las calculables y la deja,
  por título, en `otras_deducciones`. Nunca se auto-aplica.
- `estado: "sin_converger"`: `objeciones_abiertas` dice dónde se atascó. No se
  puede aplicar; corrígelo a mano o recházala. No se reintenta sola mientras ni
  la ley ni los datos cambien (`--force` lo fuerza).

`params-apply` relee la ley. Se niega, sin escribir nada, si el texto del
artículo ha cambiado desde la propuesta, si el fichero ya no contiene lo que
iba a sustituir, o si la propuesta no supera hoy el control. Al aplicar añade
una línea a `revisiones` del fichero y actualiza las citas:
`evidencia_deducciones.json` para las deducciones, `evidencia_parametros.json`
para las escalas. Las escalas recodificadas quedan con `fuente.verificado:
false` (la calculadora ya las lista en `parametros_no_verificados`) hasta
`params-confirm`.

### Casos que no son el habitual

```bash
# Releer un artículo aunque su comprobación pase, o codificar uno nuevo.
params-recode --comunidad galicia --articulo "art. 5"

# Un grupo concreto por su ruta (escalas).
params-recode --ruta comunidades/galicia/escala_general
params-recode --ruta escala_general_estatal

# Dar a leer el análisis del BOE de la norma modificadora («MODIFICA…, con efectos de…»).
params-recode --comunidad galicia --norma BOE-A-2026-12345

# Un ejercicio al que la redacción vigente no alcanza: indica TÚ la redacción aplicable.
params-recode --ejercicio 2025 --comunidad galicia --articulo "art. 5" --version 20251231

# Al abrir un ejercicio: intentar recodificar lo que el rollover retira.
params-rollover 2027 --recode
```

Con `--version` la propuesta siempre lleva una nota en `control.revisar` y no
se auto-aplica: que una redacción estuviera vigente el 31 de diciembre no
prueba que sea la aplicable a ese ejercicio (una ley posterior puede haberla
cambiado con efectos retroactivos). Compruébalo antes de aplicar.

### En GitHub Actions

`vigilancia.yml` ejecuta `params-recode` después de `params-verify` solo si el
repositorio tiene el secreto `ANTHROPIC_API_KEY`; sin él, el paso se omite. No
escribe en el repositorio: sube las propuestas como artefacto
(`recode-proposals`) y, si existe el secreto `NOTIFY_WEBHOOK_URL`, avisa por
ese canal. Para aplicarlas, descarga el artefacto en la carpeta de
`RECODE_PROPOSALS_PATH` de tu copia de trabajo y usa `params-apply`. El gasto
de cada ejecución está acotado por la variable de repositorio
`RECODE_BUDGET_USD` (10 por defecto) y por `RECODE_MAX_PER_RUN`.

### Límites

- Dos adaptadores: deducciones autonómicas calculables y escalas. El resto de
  grupos que fallen aparecen en `sin_adaptador` y siguen siendo trabajo manual.
- El modelo solo lee los artículos que se le muestran (el del grupo, aquellos
  donde estaban las citas anteriores y los que pida). Una regla en otra norma
  no la ve.
- El control comprueba que las cifras están en las citas, no que signifiquen lo
  que la codificación dice. Para eso están el revisor, sus casos y tú.

## 6. Deriva de formatos: saber que una fuente ha dejado de entenderse

Una tarea puede correr, terminar bien y no haber leído nada, porque el BOE (o un boletín, o el
TEAC) ha cambiado una clave, una ruta o un rótulo. `ops doctor` no lo veía. Ahora cada lector
informa, en cada lectura real, de si lo que ha leído cumple lo que da por supuesto. El inventario
de fuentes, supuestos y síntomas está en [`docs/formatos_inventario.md`](formatos_inventario.md).

| Subcomando | Qué hace | ¿Usa un modelo? |
|---|---|---|
| `ops formatos check [--diagnose]` | Estado de cada fuente (`ok`, `degradada`, `rota`) según las últimas lecturas; avisa por los canales `NOTIFY_*`. Con `--diagnose`, además diagnostica lo que falla. | Solo con `--diagnose` y si hay modelo configurado |
| `ops formatos probe` | Lee ahora del BOE una muestra de cada formato con los lectores reales. Para CI o para comprobar una spec. | No |
| `ops formatos diagnose [--fuente X] [--force] [--dry-run] [--auto-apply]` | Pide el diagnóstico de las fuentes que fallan y deja una propuesta o un informe. | Sí |
| `ops formatos proposals [--estado E] [--show ID]` | Lista propuestas e informes. | No |
| `ops formatos apply ID` / `reject ID --motivo …` / `confirm ID` | Pone en vigor una spec, la descarta (restaurando la anterior si ya estaba aplicada) o la da por revisada. | No |
| `ops formatos sources` | Qué fuentes se vigilan, qué supone cada lector y con qué versión de spec. | No |

### Puesta en marcha

1. No hay nada que activar: `FORMAT_WATCH` está encendido por defecto y las tareas que ya tienes
   (`boe-novedades`, `boe_watch`, `boe-impact`, `params-verify`, `normas-check`, los rastreadores,
   la subida de documentos) empiezan a dejar observaciones en `FORMAT_STATE_PATH` y muestras en
   `FORMAT_DIR`. Ambas rutas deben estar en un volumen persistente, como `JOBS_STATE_PATH`.
2. Añade una línea de cron **después** de las tareas diarias:

   ```cron
   30 9 * * *  cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.ops formatos check --diagnose >> logs/ops.log 2>&1
   ```

   Sin clave de modelo, `--diagnose` no hace nada salvo decirlo (`"llm": "no configurado…"`); el
   estado determinista se informa igual.
3. `ops doctor` ya incluye las fuentes degradadas o rotas entre sus problemas: si lo tienes
   programado, te llegará por el mismo canal sin más cambios.
4. En GitHub Actions, `.github/workflows/formatos.yml` ejecuta `ops formatos probe
   --fail-on-problem` de lunes a viernes: una ejecución en rojo significa que un formato del BOE
   ya no se entiende (o que no se pudo leer); las muestras quedan como artefacto.

### Ajustes

| Variable | Por defecto | Para qué |
|---|---|---|
| `FORMAT_WATCH` | `true` | Registrar observaciones en cada lectura. |
| `FORMAT_STATE_PATH` | `./data/_formatos_state.json` | Estado y últimas 40 observaciones por fuente. |
| `FORMAT_DIR` | `./data/formatos` | Muestras, specs aplicadas (`specs/`, con historial), propuestas e informes. |
| `FORMAT_AUTO_APPLY` | `false` | Aplicar sin persona lo que supere el ensayo determinista **y** la reflexión. |
| `FORMAT_MAX_ITER` | `3` | Rondas de diagnóstico por fuente (hasta 2 llamadas cada una). |
| `FORMAT_MODEL`, `FORMAT_REVIEWER_MODEL` | vacío | Modelo del diagnóstico y de la reflexión (distintos = revisión más independiente). |
| `FORMAT_DOCUMENT_SKELETONS` | `false` | Guardar esqueletos anónimos de documentos mal leídos (ver `docs/privacidad.md`). |

Las llamadas se miden con el propósito `formatos` / `formatos-revisor` y respetan
`LLM_DAILY_BUDGET_USD`: si el presupuesto está agotado, el diagnóstico lo dice y no deja nada a
medias; se reintenta en la siguiente ejecución.

### Cuando llega un aviso

1. **Mira qué falla**: `ops formatos check --no-notify`. Cada problema nombra el canario y da el
   detalle («No se ha leído ninguna disposición de la sección I…»).
2. **Si hay una propuesta** (`resultado: spec_propuesta`): `ops formatos proposals --show ID`.
   Lee `que_ha_cambiado`, compara `reglas_propuestas` con la spec vigente (`ops formatos sources`)
   y comprueba que el cambio es el mínimo que explica el fallo. El ensayo ya ha comprobado que la
   muestra antigua se lee igual y que la nueva cumple los canarios; lo que te toca a ti es decidir
   si la explicación es verdadera.
3. **Aplica** con `ops formatos apply ID`. La siguiente lectura real de esa fuente la devuelve a
   `ok` por sí sola; hasta entonces sigue marcada. Si no te convence: `ops formatos reject ID
   --motivo "…"`.
4. **Recupera lo perdido**. La spec arregla las lecturas futuras, no las pasadas: si el sumario
   estuvo sin leerse unos días, repite `boe-novedades --desde AAAA-MM-DD`.
5. **Si es un informe** (`informe_para_desarrollador`, estado `sin_converger`): el cambio no se
   resuelve con datos o el agente no ha convergido. En `FORMAT_DIR/informes/<ID>/` están el
   informe, la muestra buena y la nueva. La fuente sigue rota hasta que alguien toque el lector.
   `ops formatos confirm ID` lo da por leído.
6. Con `FORMAT_AUTO_APPLY=true` el paso 3 ocurre solo, pero la propuesta queda en estado
   `aplicada` hasta que la revises (`confirm`) o la deshagas (`reject`).

### Documentos de usuarios

De los documentos subidos solo se cuentan resultados por tipo (cuántos datos se proponen y los
nombres de los descartados). Si un tipo degrada, el diagnóstico sin esqueletos es un informe con
esos contadores y **no llama a ningún modelo**. Con `FORMAT_DOCUMENT_SKELETONS=true` el agente
recibe esqueletos anónimos y puede proponer etiquetas adicionales para el *prompt* del lector;
esa propuesta nunca se aplica sola.

### Antes de fiarte

Nada de esto se ha ejecutado contra un modelo real: las pruebas usan un proveedor guionizado. Con
una clave, valida al menos una vez:

1. Provoca una deriva controlada en un entorno de pruebas (por ejemplo, copia la muestra buena de
   `FORMAT_DIR/muestras/boe_sumario/buena.json` como `mala.json` renombrando una clave en su
   `texto` y añadiendo el fallo correspondiente) y ejecuta `ops formatos diagnose --fuente
   boe_sumario --no-auto-apply`.
2. Comprueba que el modelo devuelve `reglas_json` con **todas** las claves (si las omite, el
   ensayo lo rechaza y verás intentos fallidos: es señal de que hay que afinar el *prompt*), que
   usa alternativas (`["nombre_viejo", "nombre_nuevo"]`) en vez de sustituir, y que la reflexión
   no se limita a asentir.
3. Mira el coste en `consumo` con el propósito `formatos`: cada muestra enviada se recorta a
   12 000 caracteres, y van dos (buena y nueva) por llamada.

### Límites

- Un canario dice que algo no cuadra, no por qué: una caída de la fuente y un cambio de formato
  dan el mismo síntoma. Por eso el diagnóstico distingue causas y solo propone spec para la segunda.
- Los umbrales (dos lecturas seguidas, caída del 80 %, 15 disposiciones sin análisis) son
  conservadores y no están calibrados con historia real.
- Las expresiones regulares propuestas se acotan con una heurística (longitud, sin grupos
  cuantificados anidados, sin retrorreferencias), no con una prueba formal.
- Fuentes aún sin instrumentar: ver «Lo que queda» en el inventario.

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
