import httpx
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import MCPServer
from starlette.testclient import TestClient

from himacal.config import Settings
from himacal.errors import ProviderError
from himacal.http import make_http_app
from himacal.tools import register_tool

PASSWORD = "test-mcp-password-with-more-than-32-characters"
ORIGIN = "http://localhost:8000"
AUTH = {"Authorization": f"Bearer {PASSWORD}"}


@pytest.fixture
def service():
    mcp = MCPServer("test", version="0.1.0")
    calls = []

    def echo(message: str) -> dict:
        """Echo a typed value."""
        calls.append(message)
        return {"message": message}

    def fail() -> dict:
        """Return a sanitized provider failure."""
        raise ProviderError("conflict", "The event changed. Fetch it again.")

    register_tool(mcp, echo, read_only=True)
    register_tool(mcp, fail)
    settings = Settings(accounts={}, mcp_password=PASSWORD)
    return make_http_app(mcp, settings), calls


@pytest.mark.parametrize(
    "path,method",
    [
        ("/mcp", "POST"),
        ("/mcp", "GET"),
        ("/mcp", "DELETE"),
        ("/mcp/", "POST"),
        ("/healthz", "GET"),
        ("/unknown", "GET"),
    ],
)
@pytest.mark.parametrize(
    "authorization", [None, "Bearer wrong", "Basic invalid", "Bearer " + "é" * 40]
)
def test_all_routes_require_valid_secret(service, path, method, authorization):
    app, calls = service
    headers = {} if authorization is None else {"authorization": authorization.encode("latin-1")}
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.request(method, path, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert calls == []


def test_health_requires_auth_and_host_origin_are_enforced(service):
    app, _ = service
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/healthz", headers=AUTH).json() == {"status": "ok"}
        assert (
            client.get("/healthz", headers=AUTH | {"Origin": "https://evil.test"}).status_code
            == 403
        )
        assert client.get("/healthz", headers=AUTH | {"Host": "evil.test"}).status_code == 421


def test_rotation_revokes_previous_secret():
    replacement = "replacement-mcp-password-with-over-32-characters"
    app = make_http_app(MCPServer("rotation"), Settings(accounts={}, mcp_password=replacement))
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/healthz", headers=AUTH).status_code == 401
        assert (
            client.get("/healthz", headers={"Authorization": f"Bearer {replacement}"}).status_code
            == 200
        )


def test_oversized_request_rejected_before_tool_execution(service):
    app, calls = service
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.post(
            "/mcp", headers=AUTH | {"Content-Length": str(40 * 1024 * 1024 + 1)}, content=b"{}"
        )
    assert response.status_code == 413
    assert calls == []


def test_duplicate_authorization_rejected(service):
    app, _ = service
    with TestClient(app, base_url=ORIGIN) as client:
        response = client.get(
            "/healthz",
            headers=[("Authorization", AUTH["Authorization"]), ("Authorization", "Bearer invalid")],
        )
    assert response.status_code == 401


def rpc(client, method, params=None, version="2025-11-25"):
    message = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        message["params"] = params
    return client.post(
        "/mcp",
        json=message,
        headers=AUTH
        | {
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": version,
        },
    )


def test_legacy_discovery_and_typed_tools(service):
    app, calls = service
    with TestClient(app, base_url=ORIGIN) as client:
        init = rpc(
            client,
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        assert init.status_code == 200
        assert init.json()["result"]["serverInfo"]["name"] == "test"
        listing = rpc(client, "tools/list").json()["result"]
        echo = next(tool for tool in listing["tools"] if tool["name"] == "echo")
        assert echo["inputSchema"]["properties"]["message"]["type"] == "string"
        assert echo["annotations"]["readOnlyHint"] is True
        result = rpc(
            client, "tools/call", {"name": "echo", "arguments": {"message": "hello"}}
        ).json()["result"]
        assert result["structuredContent"] == {"message": "hello"}
        failure = rpc(client, "tools/call", {"name": "fail", "arguments": {}}).json()["result"]
        assert failure["isError"] is True
        assert failure["structuredContent"]["error"]["code"] == "conflict"
    assert calls == ["hello"]


@pytest.mark.parametrize("mode,version", [("auto", "2026-07-28"), ("legacy", "2025-11-25")])
async def test_official_http_client_negotiates_and_calls_tools(service, mode, version):
    app, calls = service
    async with (
        app.app.router.lifespan_context(app.app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers=AUTH) as http,
        Client(streamable_http_client(ORIGIN + "/mcp", http_client=http), mode=mode) as client,
    ):
        assert client.protocol_version == version
        result = await client.call_tool("echo", {"message": "through HTTP"})
        assert result.structured_content == {"message": "through HTTP"}
    assert calls == ["through HTTP"]


async def test_chunked_oversized_body_is_bounded_by_transport(service):
    app, calls = service

    async def chunks():
        for _ in range(41):
            yield b" " * (1024 * 1024)

    async with (
        app.app.router.lifespan_context(app.app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), headers=AUTH) as http,
    ):
        response = await http.post(
            ORIGIN + "/mcp",
            content=chunks(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
    assert response.status_code == 413
    assert calls == []
