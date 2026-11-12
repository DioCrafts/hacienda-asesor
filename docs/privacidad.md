# Privacidad y datos personales en HaciendaGPT

> **Qué es este documento y qué no es.** Describe lo que hace el *software* con los datos
> personales y lo que queda en manos de quien lo despliega (el «operador»). Es trabajo de
> ingeniería: cuentas, aislamiento entre usuarios, consentimientos, exportación y borrado.
> **No es un aviso de privacidad, no es asesoramiento jurídico y no convierte un despliegue en
> conforme con el RGPD ni con la LOPDGDD.** La sección [Lo que el operador tiene que hacer](#lo-que-el-operador-tiene-que-hacer)
> enumera lo que ningún programa puede resolver por él.

Las referencias a artículos remiten al Reglamento (UE) 2016/679 (RGPD,
[DOUE-L-2016-80807](https://www.boe.es/buscar/doc.php?id=DOUE-L-2016-80807)) y a la Ley Orgánica
3/2018 (LOPDGDD, [BOE-A-2018-16673](https://www.boe.es/buscar/act.php?id=BOE-A-2018-16673)). Se
han cotejado los números y títulos de los artículos citados con esos textos en boe.es (12-11-2026);
sirven para orientar la lectura, no para sustituirla.

## Por qué importa aquí

Un expediente de HaciendaGPT contiene datos fiscales (ingresos, retenciones, patrimonio), datos
familiares (estado civil, hijos, ascendientes a cargo, que son datos de terceros) y, cuando el
usuario los aporta para una deducción o un mínimo, **datos de discapacidad**, que son datos
relativos a la salud: una categoría especial cuyo tratamiento está prohibido salvo las excepciones
del art. 9 RGPD.

## Dos modos de funcionamiento

`AUTH_MODE` (variable de entorno, ver `hacienda_gpt/settings.py`):

| Valor | Comportamiento |
|---|---|
| `off` (por defecto) | Sin cuentas. **Cualquiera que conozca el identificador de un expediente puede leerlo y modificarlo.** Solo es aceptable en un equipo de un único usuario o en desarrollo. Nada de lo descrito abajo sobre cuentas, consentimientos, exportación o borrado está activo. |
| `required` | Cada petición sobre un expediente exige sesión; un expediente solo es visible para la cuenta que lo creó. Es el modo para cualquier despliegue con datos de personas reales. |

Un valor desconocido impide arrancar el servicio (una errata no debe dejar los expedientes abiertos).

Lo que sigue describe el modo `required`.

## Qué datos se guardan y dónde

Todo lo persistente está en **un único fichero SQLite** (`DECISION_STATE_DB_PATH`, por defecto
`./data/decision_state.sqlite3`), más sus ficheros `-wal` y `-shm`.

| Tabla | Contenido | Observaciones |
|---|---|---|
| `users` | Identificador, correo normalizado, huella de la contraseña, fecha de alta | La contraseña nunca se guarda: se deriva con scrypt (sal aleatoria por usuario; el coste se guarda con la huella). |
| `auth_sessions` | SHA-256 del token de sesión, usuario, alta, caducidad, revocación | El token solo lo tiene el navegador. Caduca a las `AUTH_SESSION_TTL_HOURS` horas (24 por defecto). |
| `user_consents` | Tipo, concedido/retirado, fecha y hora, versión del texto | Solo se añaden filas: el historial es la prueba del consentimiento. |
| `case_owners` | Qué cuenta es dueña de cada expediente | |
| `case_states` | El expediente: hechos fiscales, familiares y de discapacidad, datos pendientes, obligaciones candidatas | JSON en claro. |
| `audit_events` | Rastro de cada expediente: **el texto de cada pregunta del usuario**, los hechos editados, las herramientas usadas, las afirmaciones verificadas, el nombre de los documentos analizados | Contiene, por tanto, datos personales en texto libre. |
| `notifications` | Avisos generados para cada expediente (plazos, plan de fin de año, cambios normativos), con importes estimados | |
| `notification_deliveries` | Qué aviso se envió por qué canal y cuándo | Solo existe si se ha usado la entrega fuera de la aplicación. |

Además:

- **Navegador del usuario** (`localStorage`): el texto de las conversaciones, el token de sesión y
  el tema visual. Las conversaciones no se envían al servidor salvo como contexto de cada pregunta.
  Al cerrar sesión o eliminar la cuenta la aplicación web las borra de ese navegador; si la sesión
  simplemente caduca, permanecen hasta el siguiente inicio de sesión (y se descartan si entra otra
  cuenta). El token en `localStorage` es legible por cualquier script que se ejecute en la página.
- **Documentos subidos** (nóminas, certificados…): se procesan en memoria y **no se almacenan**. En
  el rastro de auditoría queda el nombre del fichero, el tipo de documento detectado y los nombres
  (no los valores) de los datos propuestos.
- **Vigilancia del lector de documentos** (`hacienda_gpt/ops/formatos_documentos.py`): por cada
  documento analizado se anota, en `FORMAT_STATE_PATH` y sin vínculo con el expediente ni con el
  usuario, el tipo de documento detectado, cuántos datos se propusieron y los nombres (no los
  valores) de los descartados. Sirve para detectar que un tipo de documento ha dejado de leerse
  bien. Si el operador activa `FORMAT_DOCUMENT_SKELETONS` (desactivado por defecto), de cada
  documento **mal leído** se guarda además un *esqueleto anónimo* (los tres últimos por tipo, en
  `FORMAT_DIR/esqueletos/`): la disposición de las líneas, conservando solo las palabras de una
  lista cerrada de rótulos fiscales genéricos («importe», «retenciones», «líquido»…) y
  sustituyendo cualquier otra palabra por `_` y cualquier cifra por `#`. Nombres, NIF,
  direcciones, razones sociales e importes no pueden sobrevivir a ese filtro salvo que coincidan
  con una de esas palabras genéricas. Ese esqueleto es lo único que se envía al proveedor del
  modelo cuando se diagnostica el lector (`ops formatos diagnose`). No está ligado a una cuenta,
  así que **no se borra al eliminar la cuenta** ni figura en la exportación: quien active la
  opción debe valorar si ese residuo es admisible en su despliegue.
- **Vigilancia del lector de sentencias del CENDOJ** (`hacienda_gpt/ops/formatos_esqueleto.py`): las
  sentencias no son datos de los usuarios, pero las condiciones de reutilización del CENDOJ prohíben
  la descarga masiva y la creación de bases de datos de resoluciones sin autorización del CGPJ. La
  vigilancia de formatos no hace ninguna petición al CENDOJ y **no guarda el texto de ninguna
  resolución**: de cada sentencia que el operador ya ha descargado o leído de su disco se guarda,
  como mucho (la última buena, la anterior y la última mala), un *esqueleto* estructural en
  `FORMAT_DIR/muestras/cendoj_documento/`: las etiquetas y nombres de atributo del marcado, los
  rótulos de una lista cerrada («ROJ», «ECLI», «Ponente», «Fecha», «Sala», «Sección», «Nº de
  Recurso»…), `_` en lugar de cualquier otra palabra y `1` en lugar de cada cifra, con un máximo de
  20 000 caracteres. Nombres de ponentes, partes y lugares, y el razonamiento de la sentencia, no
  sobreviven al filtro. Lo que se espera de cada muestra se guarda como forma («STS ####/####»), no
  como valor; la observación anota la URL, nunca la ruta de un fichero local. Ese esqueleto es lo
  único que recibe el proveedor del modelo cuando se diagnostica el lector. De las ejecuciones de
  `supremo_seed` solo se guardan recuentos.
- **Registros del servidor**: el registro estructurado de la aplicación anota método, plantilla de
  ruta, estado y latencia; no anota cuerpos, identificadores de expediente ni direcciones IP. Pero
  el registro de accesos de uvicorn, activo por defecto, sí escribe la IP del cliente y la ruta
  completa (con el identificador del expediente), y el proxy inverso suele hacer lo mismo: su
  contenido y su conservación dependen del despliegue.
- **Límite de intentos de acceso**: los correos y direcciones con intentos fallidos se guardan solo
  en memoria del proceso, como mucho durante la ventana configurada (15 minutos por defecto).

Nada de lo anterior está cifrado por la aplicación.

## Qué se envía al proveedor del modelo de lenguaje y cuándo

El proveedor se elige con `LLM_PROVIDER` (`anthropic`, `openai` o `local`). Con `local` los datos
no salen de la máquina que ejecuta el modelo; con los otros dos salen hacia un tercero.

| Acción del usuario | Qué recibe el proveedor |
|---|---|
| Hacer una pregunta al asesor (`/agent`, `/agent/stream`) | La pregunta, los turnos anteriores de la conversación y, si hay expediente, los hechos de ese expediente que las herramientas del asesor lean o escriban, junto con los fragmentos de normativa recuperados. Puede incluir datos de discapacidad y de familiares si están en el expediente o en la conversación. |
| Turno del motor de decisión (`/cases/{id}/turn`) | La frase del usuario y el historial reciente, para extraer hechos (cuando el extractor configurado es un modelo de lenguaje). |
| Pregunta al flujo antiguo (`/qa`) | La pregunta, el historial y los fragmentos recuperados, a OpenAI. |
| Subir un documento (`/cases/{id}/documents`) | El texto extraído del documento, o el propio fichero si es un escaneado o una foto. **Solo si el usuario ha dado el consentimiento `envio_documentos_a_proveedor_llm`**; sin él el servidor responde 403 antes de leer el fichero. |
| Simulador, calendario, avisos proactivos, edición manual del expediente | Nada. Son cálculos deterministas locales. |
| Diagnóstico de un formato externo (`ops formatos diagnose`, lo lanza el operador) | Muestras de fuentes públicas (BOE, boletines, TEAC, DGT, sede de la AEAT). De los documentos de usuarios, solo esqueletos anónimos y solo si el operador los ha activado; de las sentencias del CENDOJ, solo su esqueleto estructural. Ningún dato de expedientes. |

Los *embeddings* y el reordenado de resultados se calculan en local.

Lo que el proveedor haga con lo recibido (conservación, uso para entrenamiento, ubicación de los
servidores) lo fijan sus condiciones y el contrato que el operador tenga con él, no este código.

### Otros destinatarios

- **Entrega de avisos fuera de la aplicación** (`NOTIFY_WEBHOOK_URL`, `NOTIFY_SMTP_*`): si el
  operador la configura, el texto de los avisos de **todos** los expedientes (con importes
  estimados y el identificador del expediente) se envía a un único destino del operador, a través
  del servicio de correo o del *webhook* elegido. **Hoy ese envío no consulta el consentimiento
  `avisos_por_correo` ni escribe a cada usuario**: el consentimiento se registra, pero todavía no
  gobierna ningún envío. Con usuarios reales, no active estos canales hasta decidir si ese destino
  y ese intermediario son admisibles.
- **Vigilancia de tareas** (`HEALTHCHECK_URL`): solo indica si una tarea programada ha terminado
  bien; no lleva datos personales.

## Consentimientos

Se registran como hechos fechados (tipo, concedido o retirado, instante, versión del texto):

| Tipo | Para qué | Efecto en el software |
|---|---|---|
| `tratamiento_datos` | Guardar y tratar los datos del expediente | Obligatorio para registrarse. No se puede retirar sin eliminar la cuenta. |
| `envio_documentos_a_proveedor_llm` | Enviar documentos al proveedor del modelo | Sin él, subir documentos devuelve 403. Se puede dar y retirar en cualquier momento. |
| `avisos_por_correo` | Recibir avisos por correo | Solo se registra; ver «Otros destinatarios». |

La versión del texto mostrado es `CONSENT_TEXT_VERSION` (`hacienda_gpt/auth/store.py`). Hay que
cambiarla cada vez que cambie la redacción que ve el usuario (`frontend/src/components/AuthScreen.tsx`
y `PrivacyPanel.tsx`).

Dos advertencias:

1. Que el software pida una casilla no decide que el consentimiento sea la base jurídica adecuada,
   ni que el texto de la casilla sea suficiente para el consentimiento explícito que exige el
   art. 9.2.a) RGPD para datos de salud. Eso lo decide el operador con su asesor jurídico.
2. **Las preguntas al asesor envían datos del expediente al proveedor sin un consentimiento
   separado**: forman parte del servicio que el usuario pide. Si el operador concluye que necesita
   un consentimiento específico también para eso, hay que añadirlo.

## Conservación

**El software no borra nada por antigüedad.** Expedientes, rastro de auditoría, avisos y
consentimientos permanecen hasta que el usuario elimina su cuenta o el operador los borra. Las
únicas caducidades automáticas son las sesiones (dejan de valer al caducar; sus filas se eliminan
pasados 30 días, cuando alguien vuelve a iniciar sesión) y los contadores de intentos de acceso. Fijar plazos de conservación y aplicarlos
(por ejemplo, cuentas inactivas) es tarea pendiente del operador.

Los expedientes creados con `AUTH_MODE=off` no tienen dueño: al pasar a `required` dejan de ser
accesibles por la API, pero **siguen en la base de datos** hasta que el operador los elimine.

## Exportar mis datos

`GET /me/export` (en la web: «Privacidad y datos» → «Exportar mis datos») devuelve un JSON con:
perfil (identificador, correo, fecha de alta), sesiones (fechas, sin credenciales), historial
completo de consentimientos y, por cada expediente, su estado con todos los hechos, su rastro de
auditoría, sus avisos y los registros de entrega de esos avisos. La aplicación web añade al fichero
descargado las conversaciones guardadas en ese navegador.

No incluye lo que el servidor no tiene (los documentos subidos) ni lo que haya quedado en poder de
terceros (proveedor del modelo, correo ya enviado).

## Eliminar mi cuenta

`DELETE /me` con la contraseña (en la web: «Privacidad y datos» → «Eliminar mi cuenta») borra, por
este orden, los avisos y registros de entrega de cada expediente, cada expediente con su rastro de
auditoría y, al final, la cuenta con sus sesiones, consentimientos y relaciones de propiedad. Es
irreversible. Si el proceso falla a medias, la cuenta sigue existiendo y basta repetirlo.

Lo que **no** alcanza ese borrado:

- **Copias de seguridad** y réplicas del fichero SQLite hechas por el operador.
- **Restos en el propio fichero**: SQLite marca las páginas como libres pero no las sobrescribe.
  Hasta un `VACUUM` (o con `PRAGMA secure_delete=ON`, que este código no activa) los datos pueden
  recuperarse con herramientas forenses; también pueden quedar en el fichero `-wal`.
- **El proveedor del modelo de lenguaje**: lo ya enviado se rige por su política de conservación.
- **Correos o mensajes de *webhook* ya enviados** al operador con avisos de ese usuario.
- **Registros** del proxy o del sistema que el operador conserve.
- La prueba de los consentimientos se borra con la cuenta. Si el operador necesita conservarla para
  acreditar que el tratamiento fue lícito, tiene que decidirlo y cambiar este comportamiento.

## Medidas de seguridad que sí implementa el software

- Contraseñas: mínimo 10 caracteres; scrypt de la biblioteca estándar con sal por usuario
  (`AUTH_SCRYPT_N`, 2^17 por defecto); comparación en tiempo constante.
- Sesiones: token aleatorio de 256 bits entregado una sola vez; en la base de datos solo su
  SHA-256; caducidad y revocación al cerrar sesión.
- Aislamiento: el acceso a un expediente ajeno responde 404, igual que uno inexistente, en todos
  los puntos de la API que tocan un expediente (incluidos avisos, auditoría, simulador y asesor).
  El dueño de un expediente es siempre la sesión que lo crea, nunca un dato enviado por el cliente.
- Acceso: mismo mensaje y mismo coste de cálculo para «correo desconocido» y «contraseña
  incorrecta»; límite de intentos fallidos por correo y dirección (`AUTH_LOGIN_MAX_ATTEMPTS` en
  `AUTH_LOGIN_WINDOW_SECONDS`), también al registrar y al confirmar el borrado.
- La clave de API compartida (`HACIENDA_API_KEY`) sigue funcionando igual y se suma a la sesión.

### Límites conocidos

- **No se verifica el correo.** Cualquiera puede registrar una dirección que no es suya, y no hay
  recuperación de contraseña: quien la olvida pierde el acceso y el operador tiene que intervenir.
- El registro revela si una dirección ya tiene cuenta (responde 409). Sin verificación por correo
  no hay forma limpia de evitarlo; el límite de intentos solo lo frena.
- No hay segundo factor, ni cambio de contraseña, ni «cerrar todas mis sesiones» (eliminar la
  cuenta sí las cierra todas).
- El límite de intentos vive en la memoria de un proceso. Con varios procesos, o detrás de un proxy
  sin `--proxy-headers`, deja de ser fiable: hace falta un limitador en el proxy.
- **Los accesos que no pasan por la API no tienen control de acceso**: las tareas proactivas y los
  CLI leen todos los expedientes (es necesario para que funcionen sin sesión), y la interfaz
  Streamlit (`hacienda_gpt/ui`) abre la base de datos directamente. Con `AUTH_MODE=required` no
  debe exponerse Streamlit a usuarios; con `UI_USE_API=true` tampoco funciona, porque no inicia sesión.
- Quien tenga acceso al fichero SQLite o a sus copias lo lee todo.
- Este diseño no ha pasado una auditoría de seguridad externa ni una prueba de intrusión.

## Lo que el operador tiene que hacer

Nada de esto lo resuelve el código. Sin ello, el despliegue no debería recibir datos de personas reales.

1. **Identificarse como responsable del tratamiento y fijar la base jurídica** (art. 6 RGPD) y,
   para los datos de discapacidad, la excepción del art. 9 que le ampara. Decidir si las casillas
   que muestra la aplicación son la vía adecuada y si su redacción vale (art. 7 RGPD).
2. **Redactar y publicar el aviso de privacidad** (arts. 12 y 13 RGPD): quién es el responsable,
   fines, base jurídica, destinatarios, transferencias internacionales, plazos de conservación,
   derechos y cómo ejercerlos, derecho a reclamar ante la autoridad de control. Indicar su dirección
   en `PRIVACY_NOTICE_URL`; mientras esté vacía la aplicación avisa al usuario de que no existe.
   El resumen incorporado en la aplicación describe el software, no sustituye ese aviso.
3. **Contrato de encargo con el proveedor del modelo de lenguaje** (art. 28 RGPD) y con cualquier
   otro encargado (alojamiento, correo, *webhook*). Comprobar dónde trata los datos y, si es fuera
   del Espacio Económico Europeo, las garantías de la transferencia (arts. 44 y siguientes).
   Revisar su conservación de datos y si los usa para entrenar modelos. Valorar `LLM_PROVIDER=local`.
4. **Evaluación de impacto** (art. 35 RGPD): hay datos de salud, perfilado fiscal y una tecnología
   nueva. Valorarla y, si procede, hacerla **antes** de tratar datos reales.
5. **Registro de actividades de tratamiento** (art. 30 RGPD) y valorar si hay que designar delegado
   de protección de datos (art. 37 RGPD).
6. **Procedimiento ante brechas de seguridad** (arts. 33 y 34 RGPD): quién decide, cómo se detecta,
   cómo se notifica a la autoridad de control —el art. 33 habla de hacerlo sin dilación indebida y,
   de ser posible, en un máximo de 72 horas desde que se tiene constancia— y a los afectados. El
   software no detecta ni notifica brechas.
7. **Atender los derechos por una vía humana** (arts. 12, 15 y siguientes RGPD): acceso y supresión tienen
   autoservicio, pero rectificación de la cuenta, oposición, limitación o un usuario que ha perdido
   la contraseña necesitan un contacto y un procedimiento.
8. **Seguridad del despliegue** (art. 32 RGPD):
   - **HTTPS obligatorio.** El token de sesión y las contraseñas viajan en la petición; sin TLS
     cualquiera en la red los lee. El servidor no lo impone: hay que terminar TLS en un proxy.
   - **Cifrado en reposo** del disco o volumen donde están el fichero SQLite y sus copias.
   - **Copias de seguridad** cifradas, con plazo de retención, y un procedimiento para que un
     borrado de cuenta se propague a ellas (o caduquen en un plazo que se comunique al usuario).
   - Permisos del fichero SQLite, quién puede ejecutar los CLI y las tareas proactivas, y no
     exponer la interfaz Streamlit.
   - `AUTH_MODE=required`, `HACIENDA_API_KEY` si procede, `API_RATE_LIMIT_PER_MIN` y un limitador
     en el proxy; orígenes CORS acordes con el dominio real (`_ALLOWED_ORIGINS` en `hacienda_gpt/api/api.py`).
   - Cabeceras de seguridad en el proxy (como mínimo una política de contenido que limite los
     scripts), porque el token de sesión está al alcance del JavaScript de la página.
9. **Plazos de conservación** y su aplicación (cuentas inactivas, expedientes sin dueño heredados
   del modo `off`, registros).
10. **Menores**: el software no pregunta la edad de quien se registra (art. 7 LOPDGDD).
11. **Datos de terceros**: el expediente incluye datos de familiares que no han aceptado nada.
    El aviso de privacidad y la base jurídica tienen que contemplarlo.

## Dónde está cada cosa en el código

| Qué | Dónde |
|---|---|
| Usuarios, sesiones, consentimientos, propiedad | `hacienda_gpt/auth/store.py` |
| Contraseñas (scrypt) | `hacienda_gpt/auth/passwords.py` |
| Límite de intentos | `hacienda_gpt/auth/ratelimit.py` |
| Comprobación de sesión, propiedad y consentimiento | `hacienda_gpt/auth/deps.py` |
| Endpoints de cuenta, exportación y borrado | `hacienda_gpt/api/routers/auth.py` |
| Pruebas | `tests/auth/` |
| Pantalla de acceso y panel de privacidad | `frontend/src/components/Auth*.tsx`, `PrivacyPanel.tsx` |
