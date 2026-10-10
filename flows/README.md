# Menu cliente

## Estado: suspendido (2026-10-09)

Funcion incompleta y no habilitada. Segun la comprobacion del negocio en Meta,
no se puede generar el Flow hasta completar la verificacion del negocio.
Se conserva esta base para retomarla despues; no activa el chatbot ni modifica
el registro actual por WhatsApp.

`menu_cliente.json` contiene el menu y las pantallas de Mis compras, Contraentrega,
Olva y Shalom. Validar en el editor de Meta antes de publicar el Flow.

El JSON termina con `complete`, no usa un endpoint de datos cifrado. La pantalla
Mis compras solo solicita la consulta; no contiene datos reales. Los resultados
deben enviarse al chat desde el backend, identificando al remitente del webhook
firmado y comprobando el token de la sesion del Flow.

Las pantallas de envio comunican el tarifario variable. Olva requiere pago antes
del despacho; Shalom usa Pago destino, al recoger en agencia.

Base preparada en `whatsapp_entregas.py`:
- Reutiliza `cliente.destinatario_envio`, ampliado para contactos sin cuenta web.
- Consulta datos activos del contacto o de su cuenta vinculada/verificada.
- `whatsapp.solicitud_entrega` conserva modalidad, pago, estado y fecha de recogida.
- Deduplica por Phone Number ID y mensaje. Ninguna direccion se confirma automaticamente.
- Contraentrega calcula el sabado con corte viernes 17:00, horario America/Lima.
- Las solicitudes de envio quedan pendientes de datos/confirmacion del destinatario.

Los Flows siguen suspendidos, pero la base de entregas se reutiliza en el bot
de listas y botones: ver `../WHATSAPP_BOT.md`. El SQL equivalente vuelve a
`database/sql/050_whatsapp_entregas.sql` del backend de gestion; ya no depende
de publicar un Flow. Se prepara automaticamente al habilitar el bot interactivo.

Pendiente: publicar el Flow y configurar su ID, el bot hibrido de bienvenida y
pausa humana, validar/consumir respuestas `nfm_reply`, listar compras reales,
enviar datos previos al chat y crear el segundo Flow de formulario/confirmacion.
Este cambio no activa respuestas para conversaciones ordinarias ni envia mensajes.

Para retomar: completar la verificacion, validar/publicar el JSON en Meta,
implementar los pendientes anteriores y conectar la preparacion de tablas
y su migracion al habilitar la funcion. Probar registro, pausa humana,
reinicio tras una hora y las tres modalidades antes de activar en produccion.
