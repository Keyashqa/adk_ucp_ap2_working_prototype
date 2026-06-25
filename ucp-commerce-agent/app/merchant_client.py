"""HTTP client for communicating with the UCP merchant server."""
from __future__ import annotations

from typing import Any

import httpx

from app.config import MERCHANT_BASE_URL


class MerchantClient:
    def __init__(self, base_url: str = MERCHANT_BASE_URL):
        self.base_url = base_url.rstrip("/")

    async def fetch_ucp_profile(self, theater_id: str = "pvr-001") -> dict[str, Any]:
        """GET /.well-known/ucp — returns {ucp, merchant_public_jwk, theaters}."""
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                f"{self.base_url}/.well-known/ucp",
                params={"theater_id": theater_id},
            )
            resp.raise_for_status()
            return resp.json()

    async def mcp_call(
        self,
        method: str,
        params: dict[str, Any],
        rpc_id: int = 1,
    ) -> dict[str, Any]:
        """POST /mcp — JSON-RPC 2.0 call. Returns the full response dict."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.base_url}/mcp",
                json={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params},
            )
            resp.raise_for_status()
            data = resp.json()
            if "error" in data and data["error"]:
                raise RuntimeError(f"MCP error ({method}): {data['error']}")
            return data

    async def verify_mandate(
        self,
        session_id: str,
        cart_mandate: dict[str, Any],
        payment_sd_jwt: str,
        user_public_jwk: dict[str, Any],
    ) -> dict[str, Any]:
        """POST /mandates/verify — verify both mandates with the merchant."""
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.base_url}/mandates/verify",
                json={
                    "session_id": session_id,
                    "cart_mandate": cart_mandate,
                    "payment_sd_jwt": payment_sd_jwt,
                    "user_public_jwk": user_public_jwk,
                },
            )
            resp.raise_for_status()
            return resp.json()
