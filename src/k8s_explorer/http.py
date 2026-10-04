import hmac

import anyio
from starlette.requests import Request
from starlette.responses import JSONResponse

from k8s_explorer.service import Explorer


class AccessMiddleware:
    def __init__(self, app, explorer: Explorer):
        self.app = app
        self.settings = explorer.settings

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp"):
            headers = dict(scope["headers"])
            host = headers.get(b"host", b"").decode("latin1").split(":", 1)[0].lower()
            if host not in self.settings.allowed_http_hosts:
                await JSONResponse({"error": "Host not allowed"}, status_code=421)(scope, receive, send)
                return
            if self.settings.api_token:
                expected = ("Bearer " + self.settings.api_token.get_secret_value()).encode()
                actual = headers.get(b"authorization", b"")
                if not hmac.compare_digest(expected, actual):
                    await JSONResponse({"error": "Unauthorized"}, status_code=401)(scope, receive, send)
                    return
        await self.app(scope, receive, send)


def create_app(explorer: Explorer):
    @explorer.mcp.custom_route("/health/live", methods=["GET"])
    async def live(request: Request):
        return JSONResponse({"status": "ok"})

    @explorer.mcp.custom_route("/health/ready", methods=["GET"])
    async def ready(request: Request):
        try:
            health = await anyio.to_thread.run_sync(explorer.store.health)
            return JSONResponse({"status": "ok", "indexes": health})
        except Exception:
            return JSONResponse({"status": "unavailable"}, status_code=503)

    app = explorer.mcp.streamable_http_app()
    app.add_middleware(AccessMiddleware, explorer=explorer)
    return app
