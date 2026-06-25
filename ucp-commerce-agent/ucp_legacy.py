"""UCP client — discovers merchants and calls their MCP tools."""

import uuid
import httpx


class UCPClient:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30)
        self.merchants = {}  # url -> merchant info dict

    async def discover(self, merchant_url: str) -> dict:
        """Fetch a merchant's UCP profile from /.well-known/ucp."""
        resp = await self.client.get(f"{merchant_url}/.well-known/ucp")
        resp.raise_for_status()
        profile = resp.json()
        ucp = profile["ucp"]
        info = {
            "name": merchant_url.split("//")[-1],
            "mcp_endpoint": ucp["services"]["dev.ucp.shopping"][0]["endpoint"],
            "capabilities": list(ucp.get("capabilities", {}).keys()),
            "payment_handlers": list(ucp.get("payment_handlers", {}).keys()),
        }
        self.merchants[merchant_url] = info
        return info

    async def mcp_call(
        self, merchant_url: str, tool_name: str, arguments: dict
    ) -> dict:
        """Call a merchant's MCP tool via JSON-RPC 2.0."""
        merchant = self.merchants[merchant_url]
        resp = await self.client.post(
            merchant["mcp_endpoint"],
            json={
                "jsonrpc": "2.0",
                "id": uuid.uuid4().hex,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise Exception(f"MCP error: {data['error']}")
        return data.get("result", {})

    async def close(self):
        await self.client.aclose()