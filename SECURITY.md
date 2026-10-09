# Protección del acceso

El backend rechaza solicitudes sin dormir ni mantener conexiones a PostgreSQL durante la espera:

- Cinco fallos por IP dentro de cinco minutos activan una espera de 30 segundos.
- Login: 15 solicitudes por minuto por IP y 60 por minuto en total.
- API: 180 solicitudes por minuto por IP y 100 por segundo en total.
- Como máximo ocho solicitudes de login simultáneas por proceso.
- Cuerpo del login limitado a 4 KB y cinco segundos para recibirlo.
- Respuestas 429 con `Retry-After`; el frontend muestra la cuenta regresiva.
- Un acceso correcto limpia los fallos, pero no los límites de solicitudes.

Los fallos se cuentan por IP para no permitir bloquear la cuenta de otro cliente desde una IP diferente. Usuarios que comparten una IP comparten el límite.

## Producción

Instalar `requirements.txt`. Para compartir contadores entre procesos, réplicas y reinicios, configurar un Redis privado persistente:

```dotenv
RATE_LIMIT_STORAGE_URL=redis://127.0.0.1:6379/2
```

No publicar Redis en Internet. Usar credenciales y TLS si atraviesa una red no confiable.
Sin esa variable se usa memoria local: adecuado para desarrollo o un único proceso; los contadores se reinician al reiniciar ese proceso. No se hace fallback a memoria si el Redis configurado falla: se devuelve 503.

El limitador usa la IP del cliente resuelta por Uvicorn, no lee `X-Forwarded-For` directamente. Si se utiliza un proxy, confiar exclusivamente en sus IPs:

```text
uvicorn main:app --host 127.0.0.1 --port 7200 --proxy-headers --forwarded-allow-ips=127.0.0.1,::1
```

Adaptar esas IPs a la infraestructura real. No usar `*` si el backend puede recibir conexiones directas de Internet. El proxy debe sobrescribir o sanear los encabezados de IP. Bloquear el acceso público directo al puerto del backend.

Los límites de aplicación no evitan ataques que saturen la red. Configurar también límites de conexiones/solicitudes en el proxy y protección WAF/DDoS antes del backend. Estas opciones de infraestructura no se han desplegado desde este proyecto.

## Pruebas

```text
python -m unittest test_login_security -v
```
