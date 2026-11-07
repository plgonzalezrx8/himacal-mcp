from mcp import Client
from mcp.server import MCPServer

from himacal.tools import register_tool


async def test_unexpected_error_after_a_write_has_unknown_outcome_and_is_not_replayed():
    mcp = MCPServer("write-boundary")
    effects = []

    def change_then_fail() -> dict:
        """Simulate a failure handling the acknowledgement of an accepted write."""
        effects.append("accepted")
        raise RuntimeError("private-provider-diagnostic")

    register_tool(mcp, change_then_fail)
    async with Client(mcp) as client:
        result = await client.call_tool("change_then_fail", {})
    assert result.is_error is True
    assert result.structured_content["error"]["code"] == "outcome_unknown"
    assert "private-provider-diagnostic" not in str(result)
    assert effects == ["accepted"]
