"""Transport only: every dashboard shop read/write is an MCP tool call."""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from fastmcp import Client

MCP_URL = os.getenv('MCP_URL', 'http://127.0.0.1:8001/mcp')
ADMIN_TOOLS = frozenset({'approve_payment', 'reset_database'})


class MCPError(Exception):
    def __init__(self, message: str, code: str = 'mcp_error'):
        super().__init__(message)
        self.code = code


def unpack_result(result: Any) -> dict[str, Any]:
    for attribute in ('data', 'structured_content', 'structuredContent'):
        value = getattr(result, attribute, None)
        if isinstance(value, dict):
            return value
    content = getattr(result, 'content', [])
    for block in content:
        if getattr(block, 'type', '') == 'text':
            parsed = json.loads(block.text)
            if isinstance(parsed, dict):
                return parsed
    if isinstance(result, dict):
        return result
    raise MCPError('MCP did not return the expected JSON object.')


async def call_shop_tool(name: str, arguments: dict[str, Any] | None = None,
                         *, human_action: bool = False) -> dict[str, Any]:
    args = dict(arguments or {})
    if name in ADMIN_TOOLS:
        if not human_action:
            raise MCPError('Only a human dashboard action may approve money or reset the shop.', 'human_required')
        token = os.getenv('HUMAN_APPROVAL_TOKEN', '')
        if not token:
            raise MCPError('The local human approval credential is missing.', 'configuration')
        args['approval_token'] = token
    async with asyncio.timeout(20):
        async with Client(MCP_URL) as client:
            result = unpack_result(await client.call_tool(name, args, timeout=15))
    if result.get('ok') is False:
        error = result.get('error', {})
        raise MCPError(error.get('message', 'The shop could not perform this action.'), error.get('code', 'shop_error'))
    return result
