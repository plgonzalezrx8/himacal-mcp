"""Register synchronous provider operations without blocking the HTTP event loop."""

import inspect
import json
from functools import partial, wraps
from typing import get_type_hints

import anyio
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from himacal.errors import ProviderError


def register_tool(mcp, fn, *, name=None, read_only=False, destructive=False, idempotent=False):
    @wraps(fn)
    async def invoke(*args, **kwargs):
        try:
            result = await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))
            return CallToolResult(
                content=[TextContent(text=json.dumps(result, ensure_ascii=False, allow_nan=False))],
                structured_content=result,
            )
        except ProviderError as exc:
            error = exc.as_dict()
        except Exception:  # noqa: BLE001 - this boundary must not expose provider diagnostics.
            error = {
                "error": {
                    "code": "internal_error" if read_only else "outcome_unknown",
                    "message": (
                        "The operation failed unexpectedly; no provider details are exposed."
                        if read_only
                        else "The operation may have completed; inspect the account before retrying."
                    ),
                }
            }
        return CallToolResult(
            content=[TextContent(text=json.dumps(error))], structured_content=error, is_error=True
        )

    # Resolve annotations in the provider's module before the SDK inspects this wrapper.
    hints = get_type_hints(fn)
    signature = inspect.signature(fn)
    invoke.__signature__ = signature.replace(
        parameters=[
            parameter.replace(annotation=hints.get(parameter.name, parameter.annotation))
            for parameter in signature.parameters.values()
        ],
        return_annotation=CallToolResult,
    )
    invoke.__annotations__ = hints | {"return": CallToolResult}
    mcp.add_tool(
        invoke,
        name=name,
        annotations=ToolAnnotations(
            read_only_hint=read_only,
            destructive_hint=destructive,
            idempotent_hint=idempotent,
            open_world_hint=True,
        ),
        structured_output=False,
    )
