"""Contrato HTTP y ensamblado de routers, sin clientes HTTP adicionales."""
import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import urlsplit
import unittest
from unittest.mock import patch

from app.main import app
from app.core.config import BACKEND_DIR
from app.services import catalogo, visitas
import main


class ApiContractTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, url, method='GET', body=b'', headers=None):
        mensajes = []
        entregado = False
        partes = urlsplit(url)
        cabeceras = {'content-type': 'application/json', 'content-length': str(len(body)), **(headers or {})}
        scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
            'http_version': '1.1', 'scheme': 'http', 'method': method,
            'path': partes.path, 'raw_path': partes.path.encode(), 'root_path': '',
            'query_string': partes.query.encode(), 'client': ('127.0.0.50', 1234),
            'server': ('test', 80), 'headers': [(k.encode(), v.encode()) for k, v in cabeceras.items()]}
        async def receive():
            nonlocal entregado
            if not entregado:
                entregado = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            await asyncio.Event().wait()
        async def send(mensaje):
            mensajes.append(mensaje)
        await asyncio.wait_for(app(scope, receive, send), timeout=5)
        return mensajes[0]['status'], b''.join(m.get('body', b'') for m in mensajes[1:])

    def test_contrato_original_completo(self):
        esperado = json.loads((Path(__file__).parent / 'fixtures/openapi.json').read_text(encoding='utf-8'))
        self.assertEqual(app.openapi(), esperado)
        self.assertIs(main.app, app)

    async def test_raiz_y_rutas_protegidas_sin_conexion_a_bd(self):
        estado, body = await self.request('/')
        self.assertEqual((estado, json.loads(body)), (200, {'ok': True, 'message': 'Hola Bitstroid'}))
        self.assertEqual((await self.request('/docs'))[0], 404)
        for ruta in ['/api/auth/me', '/api/cliente/almacen', '/api/cliente/destinatarios', '/api/interacciones/preferencias']:
            with self.subTest(ruta=ruta):
                self.assertEqual((await self.request(ruta))[0], 401)
        self.assertEqual((await self.request('/api/auth/login', 'POST', b'{}'))[0], 422)
        self.assertEqual((await self.request('/api/publico/catalogo?pagina=0'))[0], 422)
        self.assertEqual((await self.request('/api/publico/catalogo?limite=21'))[0], 422)

    async def test_catalogo_delega_a_servicio_y_preserva_detalle(self):
        with patch.object(catalogo, 'pagina_catalogo', return_value={'items': [], 'hayMas': False}) as pagina:
            estado, body = await self.request('/api/publico/catalogo?paginado=true&pagina=2')
            self.assertEqual((estado, json.loads(body)), (200, {'items': [], 'hayMas': False}))
            self.assertEqual(pagina.call_args.args[1:3], (2, 20))
        with patch.object(catalogo, 'connect') as db:
            db.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value.fetchall.return_value = []
            self.assertEqual((await self.request('/api/publico/catalogo')), (200, b'[]'))
            db.assert_called_once()
        with patch.object(catalogo, 'connect'), patch.object(catalogo, 'detalle_items', return_value=[]):
            self.assertEqual((await self.request('/api/publico/publicaciones/no-existe'))[0], 404)

    async def test_visitas_validan_firma_antes_de_persistir(self):
        import time
        secreto = 'a' * 64
        body = json.dumps({'id': 'aef099b1-fafe-4bd2-90cb-e1d1902ae868', 'pais': 'PE'}).encode()
        tiempo = str(int(time.time()))
        firma = hmac.new(secreto.encode(), tiempo.encode() + b'.' + body, hashlib.sha256).hexdigest()
        with patch.dict('os.environ', {'VISITAS_SECRET': secreto}), patch.object(visitas, 'guardar') as guardar:
            self.assertEqual((await self.request('/api/publico/visitas', 'POST', body))[0], 401)
            guardar.assert_not_called()
            respuesta = await self.request('/api/publico/visitas', 'POST', body, {'x-visita-tiempo': tiempo, 'x-visita-firma': firma})
            self.assertEqual(respuesta, (204, b''))
            guardar.assert_called_once()
            self.assertEqual(guardar.call_args.args[1][1], 'PE')

    def test_env_anclado_a_la_raiz_del_backend(self):
        from inspect import signature
        from app.core.config import load_env
        self.assertEqual(BACKEND_DIR, Path(__file__).resolve().parents[1])
        self.assertEqual(signature(load_env).parameters['path'].default, BACKEND_DIR / '.env')