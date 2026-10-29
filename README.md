# Bitstroid · Backend

API de la tienda, registro de clientes y automatización de WhatsApp.

## Estructura

```text
app/
  main.py                Ensamblado de FastAPI y middlewares
  api/                   Rutas: auth, catálogo, clientes, pagos,
                         interacciones, visitas, webhooks y estado
  core/                  Configuración, JWT, límites y worker de WhatsApp
  db/                    Conexión PostgreSQL, adaptador y preparación de tablas
  schemas/               Modelos de solicitudes y sus validaciones
  services/              Catálogo, perfil, estadísticas y autenticación
    whatsapp/            Registro, contactos, respuestas, bot y entregas

tests/                   Pruebas unitarias, HTTP y contrato de API
  fixtures/openapi.json  Contrato previo a la reorganización
scripts/                 Herramientas manuales
  probar_whatsapp_bot.py Prueba de envío, solo con --enviar explícito
docs/                    Seguridad, WhatsApp y documentación de Flows pendientes
main.py                  Entrada compatible con uvicorn main:app
requirements.txt         Dependencias
.env                     Configuración del entorno (no mover)
```

Los módulos usan imports desde `app.*`. Las rutas validan las solicitudes y llaman a los servicios; la conexión y preparación de tablas permanecen en `app/db/`. No importar `main.py` desde los servicios.

## Ejecutar

Desde esta carpeta, con el entorno virtual activado:

```console
pip install -r requirements.txt
uvicorn main:app --host 127.0.0.1 --port 7200
```

También se admite `uvicorn app.main:app`. Los comandos Docker que usan `main:app` siguen siendo válidos; reconstruir la imagen para incluir las nuevas carpetas. Si el Dockerfile copiaba únicamente `*.py`, debe copiar también `app/`.

El `.env` se busca siempre en la raíz de este backend, aunque Uvicorn arranque desde otra carpeta. Las variables ya definidas en el entorno tienen prioridad. No se han cambiado sus valores ni agregado dependencias.

## Pruebas

```console
python -B -m unittest discover -s tests -v
```

La suite normal utiliza mocks y no envía WhatsApp. Dos pruebas de integración con PostgreSQL son opcionales: requieren `BITSTROID_TEST_DATABASE=1`, solo permiten una BD local y revierten los cambios mediante rollback.

```console
python -m scripts.probar_whatsapp_bot --help
```

La herramienta manual solo envía mensajes al añadir `--enviar` y un destinatario explícito.

## Base de datos

No hay cambios de estructura por esta reorganización. Los SQL siguen en el repositorio de gestión:

`C:\Users\jrdcr\Documents\Proyectos\Proyeccion de costeo\backend\database\sql`

`app/db/schema.py` y los servicios conservan la preparación automática de tablas. `tests/test_sql_migrations.py` comprueba que los cambios DDL tengan su SQL equivalente; admite `SQL_MIGRATIONS_DIR` cuando los repositorios están en otra ubicación.

## Documentación

- [Seguridad y límites](docs/SECURITY.md)
- [Registro y webhook de WhatsApp](docs/WHATSAPP.md)
- [Bot y entregas](docs/WHATSAPP_BOT.md)
- [Flows pendientes](docs/flows/README.md)