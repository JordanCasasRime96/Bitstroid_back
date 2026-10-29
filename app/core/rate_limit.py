from hashlib import sha256
from math import ceil
from threading import BoundedSemaphore
from time import monotonic, time
import asyncio

from fastapi import HTTPException
from limits import RateLimitItemPerMinute, RateLimitItemPerSecond
from limits.errors import StorageError
from limits.storage import storage_from_string
from limits.strategies import MovingWindowRateLimiter
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse


class LoginGuard:
    def __init__(self, storage_url="memory://"):
        self.storage = storage_from_string(storage_url, wrap_exceptions=True)
        self.limiter = MovingWindowRateLimiter(self.storage)
        self.slots = BoundedSemaphore(8)
        self.api_ip = RateLimitItemPerMinute(180)
        self.api_global = RateLimitItemPerSecond(100)
        self.login_ip = RateLimitItemPerMinute(15)
        self.login_global = RateLimitItemPerMinute(60)

    def key(self, ip, kind):
        return f"bitstroid:{kind}:{sha256(ip.encode()).hexdigest()}"

    def wait(self, seconds):
        return HTTPException(429, "Demasiados intentos. Espera antes de volver a intentarlo.",
                             headers={"Retry-After": str(max(1, ceil(seconds)))})

    def limit(self, item, namespace, ip):
        if not self.limiter.hit(item, "bitstroid", namespace, ip):
            reset, _ = self.limiter.get_window_stats(item, "bitstroid", namespace, ip)
            raise self.wait(reset - time())

    def check(self, ip, login):
        self.limit(self.api_global, "api-global", "all")
        self.limit(self.api_ip, "api-ip", ip)
        if login:
            key = self.key(ip, "cooldown")
            if self.storage.get(key):
                raise self.wait(self.storage.get_expiry(key) - time())
            self.limit(self.login_ip, "login-ip", ip)
            self.limit(self.login_global, "login-global", "all")

    def failed(self, ip):
        key = self.key(ip, "failures")
        if self.storage.incr(key, 300) >= 5:
            self.storage.incr(self.key(ip, "cooldown"), 30)
            self.storage.clear(key)
            raise self.wait(30)

    def succeeded(self, ip):
        self.storage.clear(self.key(ip, "failures"))


def client_ip(scope):
    # Uvicorn resolves forwarded IPs only from explicitly trusted proxies.
    return (scope.get("client") or ("unknown", 0))[0]


class SecurityMiddleware:
    def __init__(self, app, guard):
        self.app = app
        self.guard = guard

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/api/") or scope["method"] == "OPTIONS":
            return await self.app(scope, receive, send)
        login = scope["path"].rstrip("/") == "/api/auth/login" and scope["method"] == "POST"
        registro = scope["path"].rstrip("/") in {"/api/auth/registro", "/api/auth/registro/solicitud"} and scope["method"] == "POST"
        held = False
        try:
            await run_in_threadpool(self.guard.check, client_ip(scope), login)
            if login or registro:
                held = self.guard.slots.acquire(blocking=False)
                if not held:
                    raise self.guard.wait(2)
                # Bound authentication payloads before JSON validation or password hashing.
                body = bytearray()
                deadline = monotonic() + 5
                while True:
                    try:
                        message = await asyncio.wait_for(receive(), max(0, deadline - monotonic()))
                    except TimeoutError:
                        raise HTTPException(408, "La solicitud de acceso tardó demasiado")
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > 4096:
                        raise HTTPException(413, "La solicitud de acceso es demasiado grande")
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
                delivered = False

                async def replay():
                    nonlocal delivered
                    if not delivered:
                        delivered = True
                        return {"type": "http.request", "body": bytes(body), "more_body": False}
                    return await receive()

                await self.app(scope, replay, send)
            else:
                await self.app(scope, receive, send)
        except HTTPException as exc:
            await JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)(scope, receive, send)
        except StorageError:
            await JSONResponse({"detail": "Acceso temporalmente no disponible. Intenta nuevamente."},
                               status_code=503, headers={"Retry-After": "5"})(scope, receive, send)
        finally:
            if held:
                self.guard.slots.release()
