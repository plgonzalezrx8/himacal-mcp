"""HTTP trust boundary, outside every MCP and custom route."""

import secrets
from urllib.parse import urlsplit

from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from himacal.config import Settings

MAX_REQUEST_BYTES = 40 * 1024 * 1024


class AuthenticatedHTTP:
    def __init__(self, app, settings: Settings, allowed_hosts: set[str], allowed_origins: set[str]):
        self.app = app
        self.password = settings.mcp_password.encode("ascii")
        self.allowed_hosts = allowed_hosts
        self.allowed_origins = allowed_origins

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])

        def values(name):
            return [value for key, value in headers if key.lower() == name]

        auth = values(b"authorization")
        parts = auth[0].split(b" ", 1) if len(auth) == 1 else []
        if (
            len(parts) != 2
            or parts[0].lower() != b"bearer"
            or not secrets.compare_digest(parts[1], self.password)
        ):
            await JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="Himacal-MCP"'},
            )(scope, receive, send)
            return
        hosts = values(b"host")
        if len(hosts) != 1 or hosts[0].decode("latin-1").lower() not in self.allowed_hosts:
            await JSONResponse({"error": "invalid_host"}, status_code=421)(scope, receive, send)
            return
        origins = values(b"origin")
        if len(origins) > 1 or (
            origins and origins[0].decode("latin-1").lower() not in self.allowed_origins
        ):
            await JSONResponse({"error": "invalid_origin"}, status_code=403)(scope, receive, send)
            return
        lengths = values(b"content-length")
        try:
            if len(lengths) > 1:
                raise ValueError
            length = int(lengths[0]) if lengths else 0
            if length < 0:
                raise ValueError
        except ValueError:
            await JSONResponse({"error": "invalid_content_length"}, status_code=400)(
                scope, receive, send
            )
            return
        if length > MAX_REQUEST_BYTES:
            await JSONResponse({"error": "request_too_large"}, status_code=413)(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)


def make_http_app(mcp, settings: Settings):
    @mcp.custom_route("/healthz", methods=["GET"])
    async def health(request):
        return JSONResponse({"status": "ok"})

    local_hosts = {"localhost:8000", "127.0.0.1:8000", "[::1]:8000"}
    public = urlsplit(settings.public_url)
    allowed_hosts = local_hosts | {public.netloc.lower()}
    allowed_origins = {f"http://{host}" for host in local_hosts} | {settings.public_url.lower()}
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_REQUEST_BYTES,
        transport_security=TransportSecuritySettings(
            allowed_hosts=sorted(allowed_hosts), allowed_origins=sorted(allowed_origins)
        ),
    )
    return AuthenticatedHTTP(app, settings, allowed_hosts, allowed_origins)
