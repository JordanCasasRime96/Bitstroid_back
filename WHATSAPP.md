# Registro por mensaje entrante de WhatsApp

Sin Make ni plantilla. No envia un codigo OTP: la web reconoce la confirmacion
del numero mediante un mensaje entrante firmado por Meta. Una prueba de envio
saliente exitosa no configura por si sola la recepcion del webhook.

## Variables del servidor

Agregar las siguientes variables al entorno privado:

- `WHATSAPP_PHONE_NUMBER_ID=1402483696282363`: ID probado en Make.
- `WHATSAPP_BUSINESS_NUMBER=51926839501`: destino del enlace wa.me.
- `WHATSAPP_APP_SECRET`: secreto de la aplicacion de Meta que recibe los webhooks,
  en Configuracion / Basica. No es el access token ni JWT_SECRET.
- `WHATSAPP_WEBHOOK_VERIFY_TOKEN`: secreto propio de al menos 32 caracteres,
  exactamente igual en Meta y en el servidor.
- `WHATSAPP_OTP_SECRET`: otro secreto propio de al menos 32 caracteres, protege
  solicitudes temporales. No reutilizar JWT_SECRET ni APP_SECRET.
- `WHATSAPP_WEBHOOK_URL=https://TU_DOMINIO/api/webhooks/whatsapp`: referencia de
  la URL publica para Meta. Esta variable no registra el webhook ni cambia su ruta.
- `RATE_LIMIT_STORAGE_URL`: `memory://` solo para un proceso local; Redis
  compartido en produccion con multiples instancias. Ver `SECURITY.md`.

Generar cada secreto propio por separado:
`python -c "import secrets; print(secrets.token_urlsafe(48))"`.
No publicar secretos ni versionar `.env`. Reiniciar tras configurar.
No hacen falta `WHATSAPP_AUTH_TEMPLATE` ni `WHATSAPP_AUTH_LANGUAGE`.
Para las respuestas automaticas tambien son necesarios `WHATSAPP_ACCESS_TOKEN`
y `WHATSAPP_API_VERSION=v21.0`, correspondientes a la misma aplicacion/cuenta.
No hay nuevas dependencias: instalar `pip install -r requirements.txt`.

## Meta y dominio

1. Publicar GET/POST `/api/webhooks/whatsapp` del backend bajo HTTPS en el dominio
   propio. Meta no puede llegar a localhost. Preservar cuerpo y cabecera
   `X-Hub-Signature-256` en el proxy.
2. Configurar en la aplicacion de Meta el callback publico y el mismo
   `WHATSAPP_WEBHOOK_VERIFY_TOKEN`. El GET responde `hub.challenge` tras validar.
3. Suscribirse al campo WhatsApp `messages` y vincular/suscribir esa aplicacion
   a la cuenta de WhatsApp Business correspondiente. La conexion de Make usa su
   propia aplicacion y no demuestra que la tuya este suscrita.
4. Probar con el formulario real y comprobar la llegada del POST firmado.

Referencia oficial de firma/handshake:
https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/webhooks/start/

## Flujo

1. `POST /api/auth/registro/solicitud`: apodo, WhatsApp y contrasena obligatorios;
   nombres/correo opcionales. Revisa duplicados, guarda contrasena con bcrypt.
2. Devuelve ID, token privado del navegador y enlace con palabras aleatorias.
   Abre WhatsApp y ofrece enlace de respaldo si se bloquea la ventana emergente.
3. El cliente envia el mensaje sin modificar la linea `REGISTRO: ABCD2345`,
   desde exactamente el numero indicado.
4. El webhook comprueba firma HMAC-SHA256 con APP_SECRET, Phone Number ID,
   remitente, palabras y tiempos. Tanto mensaje como recepcion deben estar en
   los cinco minutos. Otros mensajes no confirman registros; los estados se ignoran. No responde con OTP
   ni crea cuentas automaticamente desde WhatsApp.
5. `GET /api/auth/registro/estado/{id}`, con `Authorization: Bearer <token privado>`,
   informa la confirmacion. La web consulta cada cuatro segundos mientras visible.
6. Crear cuenta se habilita. `POST /api/auth/registro`, con `verificacionId` y
   `verificacionToken`, revisa otra vez confirmacion/caducidad/duplicados y consume
   la solicitud atomicamente antes de entregar sesion.

Mensaje y token del navegador tienen secretos distintos: compartir el mensaje
no permite terminar el registro en otro navegador. Token/palabras se guardan
como HMAC, nunca en claro. El token no va en URL ni localStorage.
El codigo visible tiene ocho caracteres aleatorios sin letras/digitos ambiguos,
vinculado criptograficamente al numero. El UUID y token largo siguen siendo
internos de la web y no se incluyen en WhatsApp. Solicitudes generadas antes
de este cambio deben renovarse; no hay cambio de estructura ni SQL adicional.
La firma acredita el numero de WhatsApp remitente, no el dispositivo fisico:
tambien pueden enviar mensajes sus dispositivos vinculados. La finalizacion
solo acepta el token privado del navegador que inicio la solicitud.
Nueva solicitud invalida la anterior; espera 60 segundos, limites por IP/numero/global.
Se crea idempotentemente `cliente.registro_whatsapp_entrada`.

No registrar cuerpos, Authorization, contrasenas ni query strings del handshake
en el proxy. Mantener reloj sincronizado y limites de trafico/WAF en produccion.

## Respuestas automaticas y diagnostico

Aplicar `046_whatsapp_respuestas_registro.sql` de
`Proyeccion de costeo/backend/database/sql` y reconstruir/reiniciar el backend.
El webhook encola una confirmacion o una respuesta de solicitud invalida/vencida.
Tras crear realmente el usuario en la web se encola la confirmacion de cuenta.
El envio sucede despues del commit: un fallo de Meta no invalida el registro.
El worker del backend revisa la cola cada 2 segundos, reintenta hasta 5 veces
con 60 segundos de espera y no envia solicitudes de mas de 23 horas.
No requiere plantilla ni nuevas dependencias. Mantener activo el lifespan de Uvicorn.
Los reintentos del webhook no generan nuevas filas para el mismo mensaje.
Una interrupcion tras enviar a Meta pero antes de guardar el resultado podria
repetir una respuesta; la validacion y creacion de cuenta siguen siendo unicas.

Si no hay respuesta, revisar en Meta la suscripcion `messages` de TU aplicacion
y su suscripcion a la cuenta WhatsApp Business, no solo la conexion de Make.
Comprobar llegada del POST y errores de firma (401) en los logs del servidor.
Los errores de envio muestran solo ID interno y codigo HTTP/Meta, nunca tokens.

```sql
SELECT id, tipo, intentos, creado_en, enviado_en
FROM whatsapp.respuesta_registro ORDER BY id DESC LIMIT 20;
```

Sin filas ni contactos nuevos, revisar recepcion del webhook. Con filas pendientes,
revisar token/permisos y logs de envio. HTTP 200 de Meta significa aceptado,
no confirma entrega al celular.

En Docker, las variables deben existir dentro del contenedor, no solamente en el
`.env` usado por Compose para sustituciones. Configurar `env_file` o `environment`
en el servicio, y recrearlo tras cambiarlas. No publicar el contenido del `.env`.
Los logs de Uvicorn indican token ausente, POST firmado recibido, validacion
procesada y envio aceptado/rechazado. Consultar `docker logs --since 10m bitstroid-back`.
Para Peru, `+51 943875311` se normaliza a `51943875311`; en el campo junto al
selector +51 solo introducir los nueve digitos nacionales, sin repetir el prefijo.

## Pruebas aisladas

`python -m unittest test_whatsapp_contactos test_whatsapp_entrada test_whatsapp_webhook test_whatsapp_respuestas test_login_security -v`
Pruebas aisladas: no envian mensajes ni crean clientes reales. La prueba completa
requiere configurar y desplegar el webhook publico en Meta.

## Contactos del negocio

Para desplegar la estructura en otro servidor, ejecutar
`042_whatsapp_contactos.sql` y `043_registro_whatsapp_entrada.sql` de
`Proyeccion de costeo/backend/database/sql`.

Cada mensaje entrante firmado, destinado al Phone Number ID configurado, registra
un contacto en `whatsapp.contacto`: numero unico, nombre de perfil si se dispone,
primer/ultimo mensaje, total de mensajes y usuario web vinculado opcional.
Acepta texto, imagen, audio y otros tipos sin guardar sus contenidos ni medios.
No importa automaticamente historiales anteriores a la activacion del webhook.

`whatsapp.mensaje_entrante` guarda solo IDs, tipo y fechas para evitar contar dos
veces los reintentos de Meta. No guarda texto, claves temporales ni contrasenas.
Un contacto nuevo es quien no existia por numero; no equivale a un cliente web.
Solo se vincula automaticamente si existe exactamente un cliente activo con ese
numero verificado. Al terminar el registro verificado tambien se vincula en la
misma transaccion. No se crea una cuenta ni una contrasena por recibir un saludo.

El nombre del perfil no es necesariamente un nombre legal ni un apodo unico.
Para convertir un contacto en usuario sigue haciendo falta elegir apodo y
contrasena, confirmar el numero mediante el registro y aceptar las condiciones
que correspondan. Nombres/correo siguen siendo opcionales.

Consultar sin exponer una API publica de contactos:

```sql
SELECT numero, nombre_perfil, primer_mensaje_en, ultimo_mensaje_en,
       total_mensajes, usuario_id,
       CASE WHEN usuario_id IS NULL THEN 'Contacto sin cuenta' ELSE 'Cliente web' END AS estado
FROM whatsapp.contacto
ORDER BY ultimo_mensaje_en DESC;
```

No se agrega interfaz administrativa en esta etapa. Restringir acceso al esquema
al personal autorizado y definir retencion/eliminacion de datos antes de produccion.
