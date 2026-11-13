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

# Piloto de parámetros: normas que modifican leyes de las que salen parámetros → grupos
# afectados → verificación de todos los ficheros → recodificación (si hay clave de LLM) →
# anotación de la lectura de las leyes que quedan al día → parte al operador. Escribe en
# hacienda_gpt/tax/data/ (fechas de lectura; y lo aplicado, con RECODE_AUTO_APPLY=true).
# --no-detect porque la línea anterior ya ha leído el BOE del día; sin ella, quita la opción
# y el piloto lo lee él mismo. Ver el apartado 5.
55 6 * * *  cd /srv/hacienda-asesor && uv run python -m hacienda_gpt.cli.ops run params-autopilot -- uv run python -m hacienda_gpt.cli.proactive params-autopilot --no-detect >> logs/proactive.log 2>&1

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

Después ejecuta `params-verify --fail-on-mismatch` (todos los ficheros de parámetros: IRPF,
autónomos, sociedades, patrimonio e ISD) y, siempre, `params-autopilot` (apartado 5): sin el
secreto `ANTHROPIC_API_KEY` solo hace su parte determinista y dice qué grupos esperan.

`params-check` compara la fecha en que el BOE actualizó cada ley con `consultado_el`, la fecha en
que los ficheros la leyeron. Esa fecha ya no hay que editarla a mano: `params-verify` y
`params-autopilot` la anotan solos, sin modelo, cuando la ley está al día (apartado 5, «La fecha
de lectura de cada ley»). En Actions eso ocurre en la copia de trabajo del runner, y el
flujo lo convierte en un **pull request**:

1. `params-lecturas` compara los ficheros de parámetros con el commit revisado: lista cada ley
   cuya lectura se anotó y **falla si un fichero cambió en algo más** que sus fuentes y su
   registro `revisiones` (una cifra, una cita, una regla). Ese caso pone la ejecución en rojo y
   no abre nada.
2. Si solo hay lecturas, un segundo trabajo —el único con permiso de escritura, que no ejecuta
   ningún modelo ni descarga nada del BOE— recibe esos ficheros, repite la comprobación y abre
   (o actualiza) un PR desde la rama `vigilancia/lecturas-boe`, con la etiqueta `vigilancia` y
   una tabla de leyes, fecha de consolidación anterior y nueva y ficheros tocados.
3. Al fusionarlo, `params-check --fail-on-stale` deja de señalar esas leyes.

Dos ajustes del repositorio:

- **Settings → Actions → General → Workflow permissions**: marca «Allow GitHub Actions to
  create and approve pull requests»; sin eso el paso que abre el PR falla.
- Un PR abierto con el token por defecto **no lanza otros flujos** (la CI no correría sobre
  él). Si quieres CI en esos PR, crea un secreto `VIGILANCIA_PR_TOKEN` con un token de grano
  fino (o de una GitHub App) con permiso de escritura en contenidos y pull requests; el flujo
  lo usa si existe.

Las propuestas abiertas de semanas anteriores se restauran de la caché antes de
`params-verify`, así que una ley con una propuesta pendiente no se anota. Una fuente fijada a una redacción con fecha (el fichero de
autónomos de un ejercicio cerrado lee la ley «a 31-12-2025») ya no retiene la alarma: su texto
no cambia.

## 5. Recodificación automática de parámetros

Cuando un grupo de parámetros deja de coincidir con su ley (`params-verify`),
`params-recode` hace que un modelo relea el artículo y **proponga** datos
nuevos. Nada llega a `hacienda_gpt/tax/data/` sin pasar un control determinista
y, salvo que actives la auto-aplicación, sin que tú lo apliques. Explicación y
garantías en el README («Recodificación automática»).

**Nada de esto se ha probado contra un modelo real**: las pruebas usan un
proveedor guionizado. Antes de activar la auto-aplicación, mide el bucle con tu
modelo (`params-eval`, más abajo).

### El piloto: de la norma publicada al dato, sin que nadie lo pida

`params-autopilot` es el punto de entrada único para cron (línea en el
apartado 1). Cada ejecución:

1. **Detecta** (salvo `--no-detect`): lee el BOE de los días pendientes, como
   `boe-novedades`, y actualiza su registro (`_boe_novedades.json`).
2. **Normas → artículos → grupos.** Para cada norma del registro cuyo análisis
   del BOE dice que modifica una ley de la que salen parámetros, averigua qué
   artículos cambia y lo deja anotado, artículo a artículo, con la vía por la
   que lo supo (`articulos` de cada norma en el parte y en el estado):
   - `análisis`: los que nombra el análisis («MODIFICA los arts. 5 y 9 bis…»);
   - `versión del consolidado` / `anotaciones del consolidado`: una vez
     consolidada, los bloques del texto consolidado cuya nota la nombra («Se
     modifica por el art. 2 de la Ley 3/2026… Ref. BOE-A-…», o un «Téngase en
     cuenta…»), con o sin versión fechada ese día;
   - `texto de la norma`: antes de la consolidación, las frases de la propia
     norma («Se modifica el artículo 5…», «Uno. El apartado 2 del artículo 9
     queda redactado…», «Se añade una disposición adicional tercera…»), sin
     leer la nueva redacción entre «» y solo las que se refieren a esa ley por
     su número (o están dentro de un artículo de la norma que la nombra);
   - `modelo`: solo si nada de lo anterior da ningún artículo (el análisis dice
     «determinados preceptos» y la norma usa una fórmula que el programa no
     reconoce), un modelo **lista** los artículos (una llamada por norma), cada
     uno con una cita literal de la norma que lo nombra; lo que no se comprueba
     se descarta. El modelo nunca codifica aquí.

   Luego busca, en cualquier fichero, **todos** los grupos que se leen de esos
   artículos, por su propio artículo o porque sus citas están en ellos, y los
   relee **aunque su comprobación pase**: una condición nueva puede estar fuera
   de los pasajes citados, donde ninguna verificación mira. Esa relectura recibe
   el artículo entero y la codificación vigente, y el lector puede responder
   `sin_cambios`: el control exige que todas las citas anteriores sigan en el
   texto y que cada cifra de la codificación vigente esté en alguna cita, y el
   revisor busca expresamente una condición, excepción o límite nuevo y lanza
   sus casos con la codificación vigente. Si no, propone, como siempre.
   - Si el identificador de la norma ya aparece en el texto consolidado de la
     ley, los relee del consolidado, con el análisis como anotación.
   - Si aún no (el BOE consolida días después), los relee **del texto de la
     propia norma** (`diario_boe/txt.php`). Esa propuesta lleva siempre una
     nota en `control.revisar` y **nunca se auto-aplica**. Cuando llega la
     consolidación: si la aplicaste y sus citas están ya en el consolidado,
     queda respaldada sin llamar al modelo; si no, el grupo se relee del
     consolidado.
   - Si la norma **añade** un artículo a la ley de una comunidad y nada lo
     codifica, el lector lista las deducciones que contiene (una llamada) y
     cada una entra en el bucle como deducción nueva.
   - Una norma anterior al texto del que se leyeron los parámetros
     (`fuente_principal.consolidado_a`) ya está en los datos y no se relee: al
     activar el piloto sobre un registro antiguo no se paga por el pasado. Una
     norma que a los 90 días sigue sin aparecer en el consolidado se cierra con
     aviso.
3. **Cambios que nadie anunció.** Si el BOE ha actualizado una ley después de
   la lectura que anotan los ficheros, los grupos de los artículos cuya
   redacción se publicó después de esa lectura se releen igual que en el paso
   2 (salvo que ya haya una propuesta aceptada hecha con el texto de hoy).
4. **Verifica** todos los ficheros de parámetros contra el texto vigente (sin
   modelo). Lo que no coincide entra como objetivo, lo anuncie una norma o no.
5. **Recodifica** con el bucle, con los topes de `RECODE_MAX_PER_RUN` y del
   presupuesto diario; lo que no cabe queda para la siguiente ejecución. Con
   `RECODE_AUTO_APPLY=true` aplica lo que no necesita a nadie (también un
   `sin_cambios` conforme, que no escribe nada).
6. **Anota la lectura** de cada ley que ha quedado al día (ver «La fecha de
   lectura de cada ley», más abajo).
7. **Parte** al operador por los canales `NOTIFY_*` (asunto «parte del piloto
   de parámetros»): normas nuevas, estado de cada una y sus artículos, grupos
   que no coinciden o que se releen, lo que el bucle no puede releer,
   propuestas de la ejecución, leyes anotadas y leyes que aún no se pueden
   anotar y por qué. No se repite mientras la situación no cambie.

Su estado está en `<output-dir>/_params_autopilot.json` (una entrada por norma
y ley: `sin_consolidar` → `consolidada` → `cerrada`). Bórralo solo si quieres
que vuelva a tratar las normas del registro.

Sin credenciales de LLM (o con `--no-llm`) hace los pasos 1 a 4, el 6 y el
parte, y dice en `omitido` cuántos grupos esperan (una ley con grupos que
esperan no se anota). Termina con código 0 salvo con
`--fail-on-error` (errores o presupuesto agotado → 1). `boe-novedades
--autopilot`, o la variable `RECODE_AUTOPILOT=true`, lanza el piloto al
terminar la revisión del día, si prefieres una sola línea de cron.

Qué sigue necesitando a una persona, aunque actives la auto-aplicación:

- propuestas con algo en `control.revisar`: leídas de una norma sin
  consolidar, de una versión histórica, de un grupo sin casos de prueba
  ejecutables o con cifras que imprime otra ley, o con la cláusula de efectos
  dudosa;
- retiradas y `sin_converger` (entre ellas, un cambio de *estructura* en un
  grupo de cifras: hay que cambiar la calculadora);
- lo listado en `fuera_de_alcance` y `sin_adaptador`;
- `params-confirm` de lo aplicado;
- una ley que no se puede anotar (ver abajo): `params-check` sigue en rojo
  hasta que se resuelva lo que impide anotarla.

### La fecha de lectura de cada ley

Cada fichero de parámetros dice de qué texto de cada ley salen sus cifras
(`fuente_principal` en el IRPF y en cada comunidad; `fuentes_principales`,
`fuentes_complementarias` y la `fuente_principal` de cada comunidad en los
demás) con `consolidado_a`, `consultado_el` y `sha256_html`. `params-autopilot`
(paso 6) y `params-verify` (al terminar) **anotan solos la lectura de hoy** de
cada ley que el BOE ha actualizado después de la lectura anterior (por la fecha
de sus metadatos, la misma que mira `params-check`, o por la «Última
actualización» de la página), sin modelo y solo si:

- todas las comprobaciones de todos los grupos que la citan, en todos los
  ficheros, coinciden con el texto de hoy;
- ningún grupo leído de un artículo cuya redacción se publicó después de la
  lectura anterior queda sin releer contra el texto de hoy (con una propuesta
  aceptada, `recodificada` o `sin_cambios`, hecha con ese mismo texto), ni
  cuelga de uno de esos artículos un grupo que el bucle no relee (importes
  calculados a partir de otros, como los mínimos de Baleares): ese caso lo
  revisa una persona, que anota la lectura a mano;
- no hay sobre ella ninguna propuesta pendiente de decisión ni `sin_converger`,
  ningún grupo marcado como no verificado que no compruebe nada, ni —en el
  piloto— una norma en curso o un grupo de la ejecución sin resolver.

Entonces escribe en **todos** los ficheros que la citan la nueva fecha de
consolidación (nunca hacia atrás), `consultado_el` de hoy y la huella de la
página, y una línea en `revisiones` (los ficheros de autónomos, sociedades,
patrimonio e ISD la empiezan la primera vez). Valida cada fichero con su modelo antes de escribir ninguno. Nunca
toca una cifra ni una fuente fijada a una redacción con fecha o al PDF de una
orden. La salida (`procedencia`) dice qué leyes se anotaron, cuáles están al
día y, de las demás, todos los motivos. Con `--snapshot-dir` solo se simula
(esos textos no se han leído hoy del BOE); `--no-anotar-lecturas` lo desactiva.

### Ajustes

| Variable | Por defecto | Qué hace |
|---|---|---|
| `RECODE_PROPOSALS_PATH` | `./data/recodificacion` | Carpeta de las propuestas (un JSON por propuesta). Haz copia: es el registro de quién propuso qué. |
| `RECODE_AUTO_APPLY` | `false` | `true`: aplica sin esperar lo que supera el control sin nada por revisar y con la conformidad del revisor. Nunca retiradas ni versiones históricas. |
| `RECODE_MAX_ITER` | `3` | Pasadas del lector por grupo antes de `sin_converger`. |
| `RECODE_MAX_PER_RUN` | `10` | Grupos por ejecución; el resto espera a la siguiente. |
| `RECODE_MODEL` / `RECODE_REVIEWER_MODEL` | (modelo del proveedor) | Modelo del lector y del revisor. Un revisor distinto hace la revisión más independiente. |
| `RECODE_AUTOPILOT` | `false` | `true`: `boe-novedades` lanza el piloto al terminar (equivale a `--autopilot`). |

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
- `resultado: "sin_cambios"`: tras una modificación del artículo, el lector
  sostiene que el grupo no cambia; el control comprobó que siguen todas las
  citas anteriores y el revisor no encontró ninguna condición nueva.
  `params-apply` repite el control y no escribe nada en los ficheros: solo
  deja la propuesta como aceptada (eso permite anotar la lectura de la ley).
- `estado: "sin_converger"`: `objeciones_abiertas` dice dónde se atascó. No se
  puede aplicar; corrígelo a mano o recházala. No se reintenta sola mientras ni
  la ley ni los datos cambien (`--force` lo fuerza).

`params-apply` relee la ley. Se niega, sin escribir nada, si el texto del
artículo ha cambiado desde la propuesta, si el fichero ya no contiene lo que
iba a sustituir, o si la propuesta no supera hoy el control. Al aplicar añade
una línea a `revisiones` del fichero y actualiza las citas:
`evidencia_deducciones.json` para las deducciones, `evidencia_parametros.json`
para las escalas y los grupos de cifras del IRPF (los demás ficheros llevan las
citas dentro del grupo). Las escalas y los grupos de cifras recodificados
quedan con `fuente.verificado: false` (la calculadora ya los lista en
`parametros_no_verificados`) hasta `params-confirm`.

Desde ese momento `params-verify` comprueba el grupo **por esas citas**, contra
la ley y la versión de las que se leyeron, mientras el grupo conserve las
cifras para las que se leyeron (`sha256_valor`): si alguien edita el grupo a
mano, las citas dejan de valer y vuelve la comprobación por cifras.

### Qué grupos hay y cómo se nombran

| Fichero | `--tipo` | `--ruta` |
|---|---|---|
| `irpf_<año>.json` | `deduccion_autonomica`, `escala`, `cifras` (todos los grupos de cifras) o `cifras:<grupo>` (`cifras:minimos`, `cifras:trabajo`, `cifras:madrid_alquiler`…) | `minimos`, `trabajo`, `reduccion_arrendamiento/regimen_2026`, `comunidades/galicia/escala_general`, `comunidades/madrid/deducciones_madrid/alquiler`… |
| `autonomos_`, `sociedades_`, `patrimonio_`, `isd_<año>.json` | `<familia>:<Modelo>` (`sociedades:TiposGravamen`) | `<familia>:<ruta>` (`sociedades:tipos_gravamen`, `isd:comunidades/madrid/…`) |

`params-recode --dry-run` lista tipo y ruta de cada grupo que revisaría;
`fuera_de_alcance` dice qué grupos no asume el bucle y por qué. Para los cuatro
ficheros que no son del IRPF hay además una comprobación sin modelo de lo
publicado, grupo a grupo, con sus propias citas:

```bash
python -m hacienda_gpt.tax.recodificacion.adaptadores_otros <autonomos|sociedades|patrimonio|isd> <ejercicio> <carpeta con <ID>.html>
```

y para las deducciones autonómicas del IRPF, el mismo control que debe superar
una propuesta (termina con código 1 si alguna falla):

```bash
python -m hacienda_gpt.cli.proactive params-gate              # lee las leyes del BOE
python -m hacienda_gpt.cli.proactive params-gate --offline    # solo modelo y cifras dentro de las citas
```

### Casos que no son el habitual

```bash
# Releer un artículo aunque su comprobación pase, o codificar uno nuevo.
params-recode --comunidad galicia --articulo "art. 5"

# Un artículo que crea deducciones: el lector las lista y cada una que no esté codificada se propone.
params-recode --comunidad galicia --articulo "art. 5" --desglosar

# Un grupo concreto por su ruta (cualquier fichero).
params-recode --ruta comunidades/galicia/escala_general
params-recode --ruta minimos
params-recode --ruta sociedades:tipos_gravamen

# Una norma que el BOE aún no ha consolidado: leer la nueva redacción de su propio texto.
params-recode --comunidad galicia --articulo "art. 5" --norma BOE-A-2026-12345 --sin-consolidar

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
cambiado con efectos retroactivos). Compruébalo antes de aplicar. Lo mismo
vale para `--sin-consolidar`: compárala con el texto consolidado cuando salga
(el piloto lo hace solo).

En las leyes que meten todas sus deducciones en un artículo (Galicia art. 5,
Valencia art. 4, Murcia art. 1, La Rioja art. 32, Cantabria art. 2) el modelo
recibe solo el apartado de la deducción: `articulos_ley` de la propuesta lo
dice («5.dos», «4.uno.n», «32.12»). Si ahí figura el artículo entero («5») es
que la numeración no permitió aislar el apartado sin dudas.

### Medir el bucle antes de fiarte (`params-eval`)

```bash
# Qué grupos se evaluarían y cuánto texto se enviaría. Sin modelo.
python -m hacienda_gpt.cli.proactive params-eval -n 20 --semilla 1 --dry-run

# 20 deducciones autonómicas publicadas, sorteadas con esa semilla.
python -m hacienda_gpt.cli.proactive params-eval -n 20 --semilla 1 --usd-por-millon 3

# Otros tipos, una comunidad, o grupos concretos.
python -m hacienda_gpt.cli.proactive params-eval -n 10 --tipo escala --tipo cifras
python -m hacienda_gpt.cli.proactive params-eval --ruta comunidades/galicia/deducciones_calculables/articulo="art. 5.Siete"
```

Oculta al lector la codificación publicada y sus citas, lanza el bucle normal
y compara. No escribe en los parámetros ni guarda propuestas. Qué mirar:

- `convergieron` / `evaluados`: cuántas veces lector y revisor llegan a una
  propuesta. `identicos`: de esas, cuántas igualan a la humana en todos los
  campos comparados.
- **`aplicables_sin_persona_con_diferencias`**: propuestas que se habrían
  auto-aplicado y difieren de la codificación humana. Es la cifra que decide
  si activar `RECODE_AUTO_APPLY`: lee cada una en `grupos[].campos_distintos`
  (`humano` frente a `bucle`); a veces quien se equivoca es la codificación
  publicada.
- `acuerdo` por grupo y `acuerdo_medio`: proporción de campos iguales. Los
  textos (título, resumen, requisitos no comprobados) se listan aparte y no
  cuentan.
- `llamadas_llm`, `tokens_entrada_aprox` y, con `--usd-por-millon`, el coste
  de entrada estimado; el coste real con salida está en `ops consumo`.

Cuenta contra el presupuesto diario como cualquier otra ejecución.

### En GitHub Actions

`vigilancia.yml` ejecuta `params-autopilot` después de `params-verify`, siempre:
sin el secreto `ANTHROPIC_API_KEY` hace solo su parte determinista. Lo único que
llega al repositorio es el PR de lecturas del apartado 4; ninguna propuesta se
escribe sola (`RECODE_AUTO_APPLY` va a `false`): sube las propuestas, el
registro de novedades y el estado del piloto como artefacto
(`recode-proposals`, que además se conserva en caché de una semana a otra) y,
si existe el secreto `NOTIFY_WEBHOOK_URL`, envía el parte por ese canal. Para
aplicar una propuesta, descarga el artefacto en la carpeta de
`RECODE_PROPOSALS_PATH` de tu copia de trabajo y usa `params-apply`. El gasto
de cada ejecución está acotado por la variable de repositorio
`RECODE_BUDGET_USD` (10 por defecto) y por `RECODE_MAX_PER_RUN`. Al ser
semanal, una norma puede tardar hasta siete días en verse ahí: el cron diario
del servidor es el camino normal.

### Límites

- El modelo solo lee los artículos que se le muestran (el del grupo —o su
  apartado—, aquellos donde estaban las citas anteriores y los que pida). Una
  regla en otra norma no la ve.
- El control comprueba que las cifras están en las citas, no que signifiquen lo
  que la codificación dice. Para eso están el revisor, sus casos y tú.
- Un grupo de cifras tiene la forma que su modelo y la calculadora le dan. Si
  la ley cambia la forma de la regla, el bucle debe rendirse («no calculable»);
  no puede cambiar la calculadora.
- De una norma sin consolidar solo se lee lo que su texto dice del artículo.
  Si no imprime la fecha de efectos («entrará en vigor el día siguiente al de
  su publicación») la propuesta no supera el control y hay que esperar al
  texto consolidado, que sí la imprime.
- Si el análisis del BOE dice «determinados preceptos», los artículos salen
  del texto consolidado o del de la propia norma; el análisis sintáctico de
  la norma reconoce las fórmulas habituales («Se modifica…», «… queda
  redactado…», «Se añade…») y, si no reconoce ninguna, decide un modelo que
  solo lista. Una norma que cambia un artículo sin nombrarlo en ninguna parte
  queda para la verificación, que ve cifras y citas que cambian pero no una
  condición nueva.
- Antes de la consolidación no se puede probar que un grupo «no cambia»: el
  texto de la norma solo trae el apartado que reescribe. Hay que esperar al
  consolidado o decidir a mano.
- Una relectura tras una modificación envía el artículo entero, aunque mida
  70.000 caracteres: es más cara que una relectura por verificación.
- El paso 3 y la anotación de lecturas comparan fechas de publicación de
  versiones con la fecha de lectura. Una norma publicada antes de la lectura y
  consolidada después la cubre el registro de novedades (paso 2), no el 3.
- Los artículos que una norma añade solo se desglosan en deducciones nuevas en
  las leyes autonómicas del IRPF; un grupo estatal nuevo exige código.
- Grupos leídos de una redacción ya no vigente, o con importes calculados:
  fuera del alcance del bucle (`fuera_de_alcance`).

## 6. Deriva de formatos: saber que una fuente ha dejado de entenderse

Una tarea puede correr, terminar bien y no haber leído nada, porque el BOE (o un boletín, o el
TEAC) ha cambiado una clave, una ruta o un rótulo. `ops doctor` no lo veía. Ahora cada lector
informa, en cada lectura real, de si lo que ha leído cumple lo que da por supuesto. El inventario
de fuentes, supuestos y síntomas está en [`docs/formatos_inventario.md`](formatos_inventario.md).

| Subcomando | Qué hace | ¿Usa un modelo? |
|---|---|---|
| `ops formatos check [--diagnose]` | Comprueba el calendario fiscal (coherencia, sin red) y da el estado de cada fuente (`ok`, `degradada`, `rota`) según las últimas lecturas; avisa por los canales `NOTIFY_*`. En `otros_avisos`, la tabla de precios. Con `--diagnose`, además diagnostica lo que falla. | Solo con `--diagnose` y si hay modelo configurado |
| `ops formatos probe` | Lee ahora del BOE una muestra de cada formato con los lectores reales, y los artículos que fijan los plazos del calendario. Para CI o para comprobar una spec. | No |
| `ops formatos diagnose [--fuente X] [--force] [--dry-run] [--auto-apply]` | Pide el diagnóstico de las fuentes que fallan y deja una propuesta o un informe. | Sí |
| `ops formatos proposals [--estado E] [--show ID]` | Lista propuestas e informes. | No |
| `ops formatos apply ID` / `reject ID --motivo …` / `confirm ID` | Pone en vigor una spec, la descarta (restaurando la anterior si ya estaba aplicada) o la da por revisada. | No |
| `ops formatos sources` | Qué fuentes se vigilan, qué supone cada lector y con qué versión de spec. | No |

### Puesta en marcha

1. No hay nada que activar: `FORMAT_WATCH` está encendido por defecto y las tareas que ya tienes
   (`boe-novedades`, `boe_watch`, `boe-impact`, `params-verify`, `normas-check`, los rastreadores
   —TEAC, boletines, CENDOJ, sede y PDF de la AEAT, códigos autonómicos—, `supremo_seed`,
   `teac_seed`, `dgt_seed`, `boe_seed`, la ingesta del corpus y la subida de documentos) empiezan a
   dejar observaciones en `FORMAT_STATE_PATH` y muestras en `FORMAT_DIR`. Ambas rutas deben estar en
   un volumen persistente, como `JOBS_STATE_PATH`. La ingesta (Docling) informa desde varios procesos
   a la vez: el fichero de estado se escribe con bloqueo.
   Del CENDOJ solo se observa lo que ya se lee por orden tuya (`supremo_seed --from-dir` o
   `--urls-file`, el rastreador con su lista de URL): la vigilancia no hace ninguna petición propia
   y de cada sentencia guarda solo un esqueleto estructural, sin una palabra de la resolución fuera de
   los rótulos ni una cifra (ver `docs/privacidad.md`); el diagnóstico trabaja sobre ese esqueleto.
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
   ya no se entiende, que un artículo que fija un plazo del calendario ha cambiado o que no se pudo
   leer; las muestras quedan como artefacto. Las fuentes que no son del BOE (CENDOJ, AEAT, DGT,
   TEAC, boletines) solo se vigilan donde corren sus rastreadores.

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

### El calendario fiscal

`tax/data/calendario.json` se mantiene a mano. Dos fuentes lo vigilan:

- `calendario_fiscal` (en cada `check`): si avisa de `fecha_imposible`, `anio_sin_plazo`,
  `vencido_como_proximo` o `modelo_desconocido`, el dato está mal y hay que corregirlo; con
  `sin_verificar`, un plazo marcado `verificado: false` vence en 90 días o menos: compruébalo en la
  fuente que cita (la Renta: la orden ministerial de la campaña, que publica el BOE) y actualiza el
  dato y su `fuente`; con `antiguedad`, el conjunto no se revisa desde hace un año.
- `calendario_boe` (en cada `probe`): las citas de su spec deben seguir en los artículos del BOE. Si
  falla `cita` o `plazo`, lee el artículo (el detalle dice cuál). Si solo ha cambiado la redacción, el
  diagnóstico puede proponer la redacción nueva como alternativa de la cita; esa propuesta **nunca se
  aplica sola**. Si ha cambiado el plazo, no hay propuesta posible (el ensayo exige que la muestra
  antigua se siga leyendo igual y que los días de `calendario.json` estén en lo que dice el BOE): en un
  mismo cambio revisado, corrige el plazo en `calendario.json` (dato legal, con su fuente) y la cita
  en `hacienda_gpt/ops/data/formatos/calendario_boe.json`; el siguiente `probe` lo da por bueno.

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
- Fuera del BOE no hay muestras reales (CENDOJ por sus condiciones de reutilización; sede de la AEAT,
  DGT y TEAC no accesibles desde donde se escribió esto): sus pruebas usan páginas sintéticas.
- Lo que aún no es spec o no se vigila: ver «Lo que queda» en el inventario.

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
