"""Discover the review tool and read a selected PO over real MCP stdio."""
import argparse
import json
import logging
import sys
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

from po_intake.chat_context import ReviewContext, TOOL_NAME
from po_intake.config import Settings

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


def server_parameters(settings, po_id):
    return StdioServerParameters(
        command=sys.executable, args=["-m", "po_intake.mcp_server", "--po-id", str(po_id)], cwd=ROOT,
        env={"PO_MCP_API_URL": settings.mcp_api_url, "PO_STAFF_KEY": settings.staff_key,
             "PO_MCP_TIMEOUT": str(settings.mcp_timeout)},
    )


async def read_selected_po(settings, po_id):
    trace = {"transport": "stdio", "discovered_tools": [], "tool": None, "arguments": None,
             "result": None, "error": None}
    try:
        with anyio.fail_after(settings.mcp_timeout):
            async with Client(server_parameters(settings, po_id)) as client:
                tools = (await client.list_tools()).tools
                trace["discovered_tools"] = [tool.name for tool in tools]
                target = next((tool for tool in tools if tool.name == TOOL_NAME), None)
                if target is None or not target.annotations or target.annotations.read_only_hint is not True:
                    raise ValueError("The read-only PO context tool was unavailable.")
                trace["tool"], trace["arguments"] = target.name, {"po_id": po_id}
                response = await client.call_tool(target.name, trace["arguments"])
                if response.is_error or response.structured_content is None:
                    raise ValueError("The MCP tool could not read the selected PO.")
                result = ReviewContext.model_validate(response.structured_content)
                if result.po_id != po_id:
                    raise ValueError("The MCP result did not match the selected PO.")
                trace["result"] = result.model_dump(mode="json")
                return result, trace
    except Exception as exc:
        log.info("Selected PO MCP read unavailable (%s)", type(exc).__name__)
        trace["result"] = None
        trace["error"] = "MCP could not verify the selected PO context."
        return None, trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--po-id", required=True, type=int)
    args = parser.parse_args()
    if args.po_id <= 0:
        parser.error("--po-id must be positive")
    result, trace = anyio.run(read_selected_po, Settings(), args.po_id)
    print(json.dumps(trace, indent=2, ensure_ascii=True))
    if result is None:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
