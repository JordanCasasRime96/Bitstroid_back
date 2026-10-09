import hmac
from hashlib import sha256
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from fastapi import FastAPI
import main
import whatsapp_entrada as wa


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = ("s" * 48, "app-secret", "v" * 48, "1402483696282363", "51926839501")
        p = patch.object(wa, "configuracion", return_value=self.config)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(main, "configuracion_whatsapp", return_value=self.config)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(main, "procesar_mensajes")
        self.procesar = p.start()
        self.addCleanup(p.stop)
        self.app = FastAPI()
        self.app.add_api_route("/api/webhooks/whatsapp", main.confirmar_webhook, methods=["GET"])
        self.app.add_api_route("/api/webhooks/whatsapp", main.recibir_whatsapp, methods=["POST"])

    async def request(self, method="POST", body=b'{"object":"whatsapp_business_account","entry":[]}', signature=True, query=None):
        messages = []
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        headers = [(b"content-type", b"application/json")]
        if signature:
            digest = "sha256=" + hmac.new(b"app-secret", body, sha256).hexdigest()
            headers.append((b"x-hub-signature-256", digest.encode()))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
            "method": method, "path": "/api/webhooks/whatsapp", "raw_path": b"/api/webhooks/whatsapp",
            "root_path": "", "query_string": urlencode(query or {}).encode(), "client": ("127.0.0.1", 1234),
            "server": ("test", 80), "headers": headers}
        await self.app(scope, receive, send)
        return messages[0]["status"], b"".join(m.get("body", b"") for m in messages[1:])

    async def test_get_devuelve_challenge_sin_json(self):
        status, body = await self.request(method="GET", query={"hub.mode": "subscribe", "hub.verify_token": self.config[2], "hub.challenge": "12345"})
        self.assertEqual((status, body), (200, b"12345"))

    async def test_get_rechaza_token_incorrecto(self):
        status, _ = await self.request(method="GET", query={"hub.mode": "subscribe", "hub.verify_token": "otro", "hub.challenge": "12345"})
        self.assertEqual(status, 403)

    async def test_post_firmado_procesa_cuerpo(self):
        status, _ = await self.request()
        self.assertEqual(status, 200)
        self.assertEqual(self.procesar.call_args.args[1], {"object": "whatsapp_business_account", "entry": []})

    async def test_post_sin_firma_no_accede_bd(self):
        status, _ = await self.request(signature=False)
        self.assertEqual(status, 401)
        self.procesar.assert_not_called()

    async def test_post_cuerpo_grande_rechazado(self):
        status, _ = await self.request(body=b"x" * 65537)
        self.assertEqual(status, 413)
        self.procesar.assert_not_called()

    async def test_post_json_invalido_rechazado(self):
        status, _ = await self.request(body=b"not-json")
        self.assertEqual(status, 400)
        self.procesar.assert_not_called()

    def test_ruta_otp_antigua_no_expuesta(self):
        paths = [route.path for route in main.app.routes]
        self.assertNotIn("/api/auth/registro/codigo", paths)
        self.assertIn("/api/auth/registro/solicitud", paths)

    def test_numero_internacional_explicito_no_agrega_peru(self):
        self.assertEqual(main._normalizar_whatsapp("+506 123456"), "506123456")
        self.assertEqual(main._normalizar_whatsapp("+51 943875311"), "51943875311")
        self.assertEqual(main._normalizar_whatsapp("943875311"), "51943875311")


if __name__ == "__main__":
    unittest.main()
