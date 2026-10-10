# Bot interactivo (sin Flows)

Activacion explicita en el .env del backend:

```env
WHATSAPP_BOT_ENABLED=true
```

Requiere la configuracion de WhatsApp ya usada por el registro. No utiliza Make
ni plantillas. Los mensajes se envian dentro de la ventana de servicio del cliente.
El valor por defecto es false para que desplegar no active respuestas sin configurarlo.

## Meta y pausa humana

En la misma app suscribir `messages` y `smb_message_echoes` al webhook firmado
https://api-bitstroid.yami.pe/api/webhooks/whatsapp. El segundo evento requiere
que el numero opere en coexistencia con la app WhatsApp Business. Comprobar que
al responder desde la app el servidor recibe ese evento; sin el evento, el
backend no puede saber que una persona respondio. `message_echoes` no lo sustituye.

Una respuesta humana pausa la sesion y cancela las respuestas pendientes del bot.
Un mensaje que ya salio a Meta no puede retirarse. El registro sigue independiente.

## Comportamiento

- Primer mensaje: indicador de escritura y saludo/menu tras al menos 3 segundos.
  Si Meta rechaza el indicador opcional, igualmente se envia el saludo.
- Menu principal y modalidades mediante listas (Ver opciones).
- Consultas libres no generan respuestas adicionales, salvo durante la captura
  de datos de envio solicitada expresamente. Cancelar vuelve al menu.
- Tras una hora sin mensajes del cliente o de la persona, el siguiente mensaje
  del cliente abre una sesion nueva. El bot no reinicia por un temporizador solo.
- REGISTRO se excluye del chatbot. Eventos duplicados no repiten solicitudes.
- Opciones vinculadas a una sesion; menus anteriores dejan de funcionar al avanzar.
- Mis compras muestra hasta 10 ventas vinculadas a una cuenta activa y verificada
  con el numero del remitente. Las ventas registradas solo por nombre no se asocian.
  Vuelve al menu tras 5 segundos, salvo que intervenga una persona u otra opcion.
- Contraentrega: Feria Grau, sabados 13:00-18:00; corte viernes 17:00, America/Lima.
  Confirmar genera una solicitud pendiente, no confirma inventario ni despacho.
- Olva: tarifa variable y pago antes del despacho. Shalom: Pago destino en agencia.
- Envio: propone los ultimos datos activos y completos para la modalidad, con
  dos opciones: Usar estos datos o Cambiar datos. Sin datos completos o al cambiar,
  captura el formulario por mensajes y guarda tras confirmacion.
- Ambas modalidades piden nombre completo, documento, departamento, provincia,
  distrito y telefono. Olva pide direccion del destinatario y referencia;
  Shalom pide solo la direccion de la agencia, sin direccion personal ni referencia.
  Direcciones de una modalidad no se reutilizan para otra si faltan sus campos.

## Base de datos y despliegue

Se crean las tablas automaticamente al habilitar el bot, despues del registro.
SQL equivalentes en el backend de gestion: `database/sql/050_whatsapp_entregas.sql`
y `database/sql/051_whatsapp_bot_interactivo.sql`. No se requiere ejecucion manual
en Bitstroid. Los Flows siguen suspendidos; esta alternativa no los utiliza.

La cola guarda temporalmente los datos necesarios para responder, sin registrar
el cuerpo de mensajes en logs. El payload se borra al enviarlo correctamente o
cancelarlo. Cada 10 minutos se limpian eventos/colas de mas de 7 dias y los
borradores de direcciones tras una hora sin actividad; las direcciones confirmadas
y solicitudes no se eliminan.
Las solicitudes y direcciones contienen datos personales; restringir acceso a la BD.

Reconstruir/recrear el servicio despues de copiar el codigo y configurar el .env:

```bash
docker compose up -d --build --force-recreate bitstroid-back
docker logs -f --since 1m bitstroid-back
```

Prueba manual unica, desde el directorio del backend y dentro de una ventana
de servicio abierta por el destinatario:

```bash
python probar_whatsapp_bot.py --to NUMERO_CON_CODIGO_PAIS --enviar
```

La prueba envia una lista ilustrativa; no crea compras/entregas ni modifica la sesion.
Una aceptacion de Meta no confirma entrega: revisar el WhatsApp del destinatario.
