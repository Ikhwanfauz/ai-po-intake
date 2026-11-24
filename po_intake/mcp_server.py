"""Read-only MCP server for one PO. The API launches it on stdio per chat turn."""
import argparse
import os

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from po_intake.chat_context import ReviewContext, TOOL_NAME


def create_server(selected_po_id, api_url, staff_key, timeout):
    server = MCPServer("Meridian PO review")

    @server.tool(name=TOOL_NAME, annotations=ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False,
    ))
    async def get_po_review_context(po_id: int) -> ReviewContext:
        """Read the active document, checked prices, and unresolved issues for the selected PO."""
        if po_id != selected_po_id:
            raise ValueError("The requested PO is outside this stdio session's selection.")
        headers = {"X-Staff-Key": staff_key} if staff_key else {}
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, headers=headers) as client:
            response = await client.get(f"{api_url.rstrip('/')}/pos/{po_id}/review-context")
            response.raise_for_status()
            context = ReviewContext.model_validate(response.json())
        if context.po_id != selected_po_id:
            raise ValueError("The backend returned a different PO.")
        return context

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--po-id", type=int, required=True)
    args = parser.parse_args()
    if args.po_id <= 0:
        parser.error("--po-id must be positive")
    server = create_server(
        args.po_id, os.environ.get("PO_MCP_API_URL", "http://127.0.0.1:8000"),
        os.environ.get("PO_STAFF_KEY", ""), float(os.environ.get("PO_MCP_TIMEOUT", "12")),
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
