import json
import unittest
from time import time
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from limits import RateLimitItemPerSecond

from login_security import LoginGuard, SecurityMiddleware, client_ip


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = LoginGuard()

    def test_cinco_fallos_bloquean_sin_extender_espera(self):
        for _ in range(4):
            self.guard.failed("ip1")
        with self.assertRaises(HTTPException) as error:
            self.guard.failed("ip1")
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(error.exception.headers["Retry-After"], "30")
        with self.assertRaises(HTTPException):
            self.guard.check("ip1", True)
        self.guard.check("ip2", True)
        with patch("limits.storage.memory.time.time", return_value=time() + 31):
            self.guard.check("ip1", True)

    def test_exito_limpia_fallos_no_limites(self):
        for _ in range(4):
            self.guard.failed("ip1")
        self.guard.succeeded("ip1")
        self.guard.failed("ip1")
        self.assertEqual(self.guard.storage.get(self.guard.key("ip1", "failures")), 1)
        for _ in range(15):
            self.guard.check("ip1", True)
        self.guard.succeeded("ip1")
        with self.assertRaises(HTTPException):
            self.guard.check("ip1", True)

    def test_limite_global_login(self):
        for index in range(60):
            self.guard.check(f"ip{index}", True)
        with self.assertRaises(HTTPException):
            self.guard.check("otra-ip", True)

    def test_limite_api_por_ip(self):
        self.guard.api_global = RateLimitItemPerSecond(10000)
        for _ in range(180):
            self.guard.check("ip1", False)
        with self.assertRaises(HTTPException):
            self.guard.check("ip1", False)

    def test_no_confia_en_header_de_ip(self):
        self.assertEqual(client_ip({"client": ("real", 1), "headers": [(b"x-forwarded-for", b"falsa")]}), "real")


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.guard = LoginGuard()
        self.calls = 0
        self.app = FastAPI()
        self.app.add_middleware(SecurityMiddleware, guard=self.guard)
        self.app.add_middleware(CORSMiddleware, allow_origins=["https://tienda.test"], expose_headers=["Retry-After"])

        @self.app.post("/api/auth/login")
        async def login(request: Request):
            self.calls += 1
            data = await request.json()
            if data.get("password") != "correcta":
                self.guard.failed(client_ip(request.scope))
                raise HTTPException(401, "Credenciales incorrectas")
            self.guard.succeeded(client_ip(request.scope))
            return {"ok": True}

    async def request(self, body=b'{"password":"incorrecta"}', path="/api/auth/login"):
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

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "http",
                 "method": "POST", "path": path, "raw_path": path.encode(), "root_path": "",
                 "query_string": b"", "client": ("127.0.0.1", 1234), "server": ("test", 80),
                 "headers": [(b"content-type", b"application/json"), (b"origin", b"https://tienda.test")]}
        await self.app(scope, receive, send)
        return messages[0]["status"], dict(messages[0]["headers"])

    async def test_quinto_fallo_y_siguientes_no_llegan_al_login(self):
        for _ in range(4):
            self.assertEqual((await self.request())[0], 401)
        status, headers = await self.request()
        self.assertEqual(status, 429)
        self.assertEqual(headers[b"retry-after"], b"30")
        self.assertEqual(headers[b"access-control-allow-origin"], b"https://tienda.test")
        self.assertIn(b"Retry-After", headers[b"access-control-expose-headers"])
        self.assertEqual((await self.request())[0], 429)
        self.assertEqual(self.calls, 5)

    async def test_rechaza_cuerpo_grande_antes_de_procesarlo(self):
        self.assertEqual((await self.request(b"x" * 4097))[0], 413)
        self.assertEqual(self.calls, 0)

    async def test_registro_tambien_limita_cuerpo_antes_de_json(self):
        for path in ["/api/auth/registro", "/api/auth/registro/solicitud"]:
            self.assertEqual((await self.request(b"x" * 4097, path=path))[0], 413)

    async def test_registro_comparte_limite_de_concurrencia(self):
        for _ in range(8):
            self.guard.slots.acquire()
        try:
            self.assertEqual((await self.request(path="/api/auth/registro/solicitud"))[0], 429)
        finally:
            for _ in range(8):
                self.guard.slots.release()

    async def test_rechaza_saturacion_sin_esperar(self):
        for _ in range(8):
            self.guard.slots.acquire()
        self.assertEqual((await self.request())[0], 429)
        self.assertEqual(self.calls, 0)
        for _ in range(8):
            self.guard.slots.release()
        self.assertEqual((await self.request(json.dumps({"password": "correcta"}).encode()))[0], 200)


if __name__ == "__main__":
    unittest.main()
