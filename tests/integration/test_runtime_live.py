import os
from pathlib import Path

import httpx
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

if os.environ.get("HIMACAL_INTEGRATION") != "1":
    pytest.skip("Run isolated provider acceptance", allow_module_level=True)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("mode,version", [("auto", "2026-07-28"), ("legacy", "2025-11-25")])
async def test_runtime_container_authenticated_mcp(provider_settings, mode, version):
    endpoint = os.environ["HIMACAL_TEST_URL"]
    password = Path(os.environ["HIMACAL_MCP_PASSWORD_FILE"]).read_text().strip()
    headers = {"Host": "mcp.example.test", "Authorization": f"Bearer {password}"}
    async with httpx.AsyncClient(headers=headers) as http:
        assert (await http.get(endpoint + "/healthz")).status_code == 200
        assert (
            await http.get(endpoint + "/healthz", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        async with Client(
            streamable_http_client(endpoint + "/mcp", http_client=http), mode=mode
        ) as client:
            assert client.protocol_version == version
            result = await client.call_tool("accounts_list", {})
            assert result.is_error is False
            assert {account["account_id"] for account in result.structured_content["accounts"]} == {
                "alpha",
                "beta",
            }
            tool_list = await client.list_tools()
            assert any(tool.name.startswith("email_") for tool in tool_list.tools)
            assert any(tool.name.startswith("calendar_") for tool in tool_list.tools)
            mailboxes = await client.call_tool("email_list_mailboxes", {"account_id": "alpha"})
            assert mailboxes.is_error is False
            calendars = await client.call_tool("calendar_list", {"account_id": "alpha"})
            assert calendars.is_error is False
            selected = calendars.structured_content["calendars"][0]["id"]
            event = await client.call_tool(
                "calendar_create_event",
                {
                    "account_id": "alpha",
                    "calendar_id": selected,
                    "event": {
                        "summary": "HTTP acceptance",
                        "start": "2030-04-01",
                        "end": "2030-04-02",
                    },
                },
            )
            assert event.is_error is False
            value = event.structured_content
            deleted = await client.call_tool(
                "calendar_delete_event",
                {
                    "account_id": "alpha",
                    "calendar_id": selected,
                    "uid": value["uid"],
                    "expected_etag": value["etag"],
                    "scope": "series",
                },
            )
            assert deleted.is_error is False
            assert deleted.structured_content["deleted"] is True
