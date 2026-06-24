"""Mock UCP profile endpoint and MCP JSON-RPC 2.0 server for local testing.

In production these would be real HTTP calls to merchant infrastructure.
All functions are synchronous and return pre-baked fixtures so the
workflow can run fully offline.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.crypto import (
    MERCHANT_PRIVATE_KEY,
    MERCHANT_PUBLIC_KEY,
    USER_PUBLIC_KEY,
    sign_payload,
    verify_signature,
)

# ── Mock UCP Profile (served at /.well-known/ucp) ─────────────────────────

MOCK_UCP_PROFILE: dict[str, Any] = {
    "merchant_id":   "merchant-acme-001",
    "merchant_name": "Acme Electronics Store",
    "capabilities": [
        {
            "name": "catalog_search",
            "description": "Full-text search over the product catalog",
            "parameters": {"max_results": 20, "supports_filters": True},
        },
        {
            "name": "checkout",
            "description": "Create a checkout session from selected product IDs",
            "parameters": {"max_line_items": 50},
        },
        {
            "name": "cart_manifest",
            "description": "Return a price-locked CartMandate for the active checkout session",
            "parameters": {"lock_duration_minutes": 15},
        },
    ],
    "services": [
        {"type": "mcp",  "endpoint": "http://localhost:8999/mcp",     "version": "2.0"},
        {"type": "rest", "endpoint": "http://localhost:8999/api/v1",  "version": "1.0"},
        {"type": "a2a",  "endpoint": "http://localhost:8999/a2a",     "version": "1.0"},
    ],
    "extensions": {
        "ap2": {
            "version": "1.0",
            "payment_protocols": ["AP2-v1"],
            "mandate_endpoint": "http://localhost:8999/mandates/verify",
            "supported_currencies": ["USD", "EUR", "GBP"],
            "double_signature_required": True,
        },
        "ucp_version": "1.2",
    },
    "public_key": MERCHANT_PUBLIC_KEY,
}

# ── Product Catalog ────────────────────────────────────────────────────────

_PRODUCTS: list[dict[str, Any]] = [
    {"product_id": "prod-001", "name": "Wireless Headphones Pro",    "price": 149.99, "currency": "USD", "in_stock": True,  "category": "audio"},
    {"product_id": "prod-002", "name": "USB-C Hub 7-in-1",          "price":  49.99, "currency": "USD", "in_stock": True,  "category": "accessories"},
    {"product_id": "prod-003", "name": "Mechanical Keyboard RGB",   "price":  89.99, "currency": "USD", "in_stock": False, "category": "input"},
    {"product_id": "prod-004", "name": "4K Webcam 60fps",           "price":  79.99, "currency": "USD", "in_stock": True,  "category": "video"},
    {"product_id": "prod-005", "name": "Noise-Cancelling Earbuds",  "price":  99.99, "currency": "USD", "in_stock": True,  "category": "audio"},
]


# ── Public API ─────────────────────────────────────────────────────────────

def fetch_ucp_profile(merchant_url: str) -> dict[str, Any]:
    """Simulate HTTP GET {merchant_url}/.well-known/ucp"""
    return dict(MOCK_UCP_PROFILE)


def call_mcp(method: str, params: dict[str, Any], rpc_id: int = 1) -> dict[str, Any]:
    """Dispatch a JSON-RPC 2.0 call to the mock MCP endpoint."""
    handlers = {
        "search_products":  _search_products,
        "create_checkout":  _create_checkout,
        "get_cart_manifest": _get_cart_manifest,
    }
    if method not in handlers:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return {"jsonrpc": "2.0", "id": rpc_id, "result": handlers[method](params)}


def submit_and_verify_mandates(
    cart: dict[str, Any],
    payment: dict[str, Any],
) -> dict[str, Any]:
    """Simulate the AP2 merchant mandate-verification endpoint.

    Checks:
      1. Merchant CartMandate signature (MERCHANT_PUBLIC_KEY)
      2. User PaymentMandate signature  (USER_PUBLIC_KEY)
      3. Mandate ID cross-reference (cart.mandate_id == payment.cart_mandate_id)
    """
    issues: list[str] = []

    # 1. Verify merchant CartMandate signature
    cart_signable = {
        "mandate_id": cart["mandate_id"],
        "merchant_id": cart["merchant_id"],
        "items": cart["items"],
        "total": cart["total"],
    }
    cart_ok = verify_signature(cart_signable, cart.get("merchant_signature", ""), MERCHANT_PUBLIC_KEY)
    if not cart_ok:
        issues.append("Merchant CartMandate signature INVALID")

    # 2. Verify user PaymentMandate signature
    pay_signable = {
        "mandate_id": payment["mandate_id"],
        "cart_mandate_id": payment["cart_mandate_id"],
        "user_id": payment["user_id"],
    }
    pay_ok = verify_signature(pay_signable, payment.get("user_signature", ""), USER_PUBLIC_KEY)
    if not pay_ok:
        issues.append("User PaymentMandate signature INVALID")

    # 3. Cross-reference mandate IDs
    if cart.get("mandate_id") != payment.get("cart_mandate_id"):
        issues.append(
            f"Mandate ID mismatch: cart='{cart.get('mandate_id')}' "
            f"≠ payment.cart_mandate_id='{payment.get('cart_mandate_id')}'"
        )
        cart_ok = False

    return {
        "cart_signature_valid":    cart_ok,
        "payment_signature_valid": pay_ok,
        "both_valid":              cart_ok and pay_ok,
        "details": "; ".join(issues) if issues else "All signatures verified successfully",
    }


# ── Private RPC Handlers ───────────────────────────────────────────────────

def _search_products(params: dict[str, Any]) -> dict[str, Any]:
    query = params.get("query", "").lower()
    limit = min(int(params.get("limit", 5)), 20)
    results = [
        p for p in _PRODUCTS
        if not query or query in p["name"].lower() or query in p["category"].lower()
    ]
    return {"products": results[:limit], "total_matches": len(results)}


def _create_checkout(params: dict[str, Any]) -> dict[str, Any]:
    product_ids = params.get("product_ids", ["prod-001", "prod-002"])
    items = [p for p in _PRODUCTS if p["product_id"] in product_ids]
    return {
        "session_id": "checkout-sess-Xf8kLm92",
        "items": items,
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
    }


def _get_cart_manifest(params: dict[str, Any]) -> dict[str, Any]:
    cart_items = [
        {"product_id": "prod-001", "name": "Wireless Headphones Pro",
         "quantity": 1, "unit_price": 149.99, "currency": "USD"},
        {"product_id": "prod-002", "name": "USB-C Hub 7-in-1",
         "quantity": 2, "unit_price":  49.99, "currency": "USD"},
    ]
    total = sum(i["unit_price"] * i["quantity"] for i in cart_items)
    price_locked_until = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()

    # The signable payload mirrors what CartMandate verification will check
    signable = {
        "mandate_id": "cart-mandate-Bc7RnQ01",
        "merchant_id": MOCK_UCP_PROFILE["merchant_id"],
        "items": cart_items,
        "total": round(total, 2),
    }
    signature = sign_payload(signable, MERCHANT_PRIVATE_KEY)

    return {
        **signable,
        "merchant_name":      MOCK_UCP_PROFILE["merchant_name"],
        "currency":           "USD",
        "price_locked_until": price_locked_until,
        "merchant_signature": signature,
    }
