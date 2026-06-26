---
name: ucp-commerce-protocol
description: >
  Implementation guide for UCP (Universal Commerce Protocol) — the MCP-based
  protocol for agent-to-merchant commerce. Covers the MerchantClient wrapper,
  MCP tool conventions, the checkout-to-order lifecycle, merchant server
  architecture, and patterns for building any commerce domain (e-commerce,
  ticketing, bookings, subscriptions, etc.).
metadata:
  author: Karthikeyan TS
  version: 1.0.0
  requires:
    packages:
      - fastapi
      - httpx
      - ap2
---

# UCP — Universal Commerce Protocol

UCP is the communication layer between a commerce agent and a merchant server.
It uses **MCP (Model Context Protocol)** as the transport so the agent calls
merchant operations as named tools, and the merchant handles checkout, signing,
and mandate verification as a backend service.

```
Commerce Agent (ADK Workflow)
    │
    │  HTTP POST /mcp  (MCP tool call)
    ▼
Merchant Server (FastAPI + MCP)
    │
    ├── search_catalog()      → list of items with IDs + metadata
    ├── get_item_details()    → variants, pricing, availability
    ├── create_checkout()     → CartMandate JWT  ← AP2 starts here
    └── verify_mandate()      → order_id         ← AP2 ends here
```

---

## MerchantClient — The Agent-Side Wrapper

`MerchantClient` wraps the MCP HTTP calls so nodes don't deal with raw HTTP.

```python
# app/merchant_client.py (reference pattern)
import httpx

class MerchantClient:
    def __init__(self, base_url: str = "http://localhost:8001"):
        self.base_url = base_url

    async def mcp_call(self, tool: str, args: dict) -> dict:
        """POST an MCP tool call and return the result dict."""
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self.base_url}/mcp",
                json={"tool": tool, "arguments": args},
            )
            r.raise_for_status()
            return r.json()

    async def fetch_ucp_profile(self) -> dict:
        """Returns merchant metadata including merchant_public_jwk."""
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{self.base_url}/ucp-profile")
            r.raise_for_status()
            return r.json()

    async def verify_mandate(
        self,
        session_id: str,
        cart_mandate: dict,
        payment_sd_jwt: str,
        user_public_jwk: dict,
    ) -> dict:
        """POST both mandates for merchant-side double verification."""
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self.base_url}/verify-mandate",
                json={
                    "session_id":      session_id,
                    "cart_mandate":    cart_mandate,
                    "payment_sd_jwt":  payment_sd_jwt,
                    "user_public_jwk": user_public_jwk,
                },
            )
            r.raise_for_status()
            return r.json()
```

---

## MCP Tool Conventions

UCP defines four standard tools that every merchant server must implement.
**Tool names are domain-specific** (e.g. `search_products`, `search_hotels`)
but the shapes are standardised.

### `search_catalog`

Returns the browsable catalogue for this merchant. Name it after your domain
(e.g. `search_products`, `search_listings`).

```python
rsp   = await _CLIENT.mcp_call("search_catalog", {
    "query":    "",        # empty = return all; keyword for search
    "limit":    20,
    "store_id": "store-001",   # optional domain-specific filter key
})
items = rsp.get("result", {}).get("items", [])
# Each item: {"id": "item-001", "name": "...", "category": "...", "price_cents": 1200, ...}
```

### `get_item_details`

Returns variants, pricing tiers, and availability for a specific item.
Name it after your domain (e.g. `get_product_options`, `get_room_availability`).

```python
rsp   = await _CLIENT.mcp_call("get_item_details", {
    "item_id":  "item-002",
    "store_id": "store-001",
})
data  = rsp.get("result", {})
variants = data.get("variants", [])
# Each variant: {"id": "var-A", "label": "Standard", "price_cents": 1200}
options  = data.get("options", {})
# domain-specific option groups (sizes, dates, tiers, etc.)
```

### `create_checkout`

Creates a checkout session and returns a **signed CartMandate JWT**.
This is the AP2 entry point — always returns `cart_mandate`.

```python
rsp = await _CLIENT.mcp_call("create_checkout", {
    "item_id":   "item-001",
    "variant_id": "var-A",    # whichever selection fields your domain uses
    "qty":        2,
    "store_id":  "store-001",
})
checkout = rsp.get("result", {})
# checkout shape (core fields — your domain adds more):
# {
#   "session_id":   "sess-abc123",     ← tie PaymentMandate to this
#   "total_cents":  2400,
#   "expires_at":   "2026-06-26T...",
#   "item":         {"name": "..."},
#   "qty":          2,
#   "cart_mandate": {"merchant_authorization": "<JWT>", "contents": {...}},
# }
```

### `verify_mandate` (direct HTTP, not MCP)

The double-mandate verification endpoint. Agent POSTs both mandates;
merchant verifies its own CartMandate JWT and the user's SD-JWT.

```python
result = await _CLIENT.verify_mandate(
    session_id    = checkout["session_id"],
    cart_mandate  = checkout["cart_mandate"],
    payment_sd_jwt= sd_jwt_str,
    user_public_jwk = user_public_jwk_dict,
)
# result: {"verified": True, "order_id": "ord-xyz789"}
#      or {"verified": False, "error": "..."}
```

---

## UCP Profile

The merchant publishes metadata at `GET /ucp-profile`. Always fetch this
at session start to get the `merchant_public_jwk` for CartMandate verification.

```python
profile = await _CLIENT.fetch_ucp_profile()
merchant_public_jwk = profile.get("merchant_public_jwk", {})
# Store for use in verify_cart and verify_mandates nodes
ctx.state["merchant_public_jwk"] = merchant_public_jwk
```

---

## Merchant Server Architecture

The merchant server is a standalone FastAPI app. The structure is
domain-agnostic — rename files to match your domain.

```
my-merchant-server/
├── app/
│   ├── main.py           ← FastAPI app, MCP endpoint, /ucp-profile
│   ├── catalog.py        ← item / product / listing data
│   ├── checkout.py       ← creates CartMandate, stores sessions
│   ├── mandate.py        ← /verify-mandate endpoint
│   └── keys.py           ← merchant private/public JWK pair (generated on first run)
└── merchant.db           ← SQLite: sessions, orders
```

**Core MCP endpoint:**

```python
@app.post("/mcp")
async def mcp_handler(req: MCPRequest):
    tool = req.tool
    args = req.arguments
    if tool == "search_catalog":
        return {"result": catalog.search(args.get("query", ""), args.get("limit", 20))}
    elif tool == "get_item_details":
        return {"result": catalog.get_details(args["item_id"])}
    elif tool == "create_checkout":
        return {"result": await checkout.create(args)}
    else:
        raise HTTPException(400, f"Unknown tool: {tool}")
```

**CartMandate signing (merchant side, on every `create_checkout`):**

```python
from jwcrypto.jwk import JWK
from jwcrypto.jwt import JWT

def sign_cart_mandate(contents: dict, private_key: JWK) -> str:
    token = JWT(header={"alg": "RS256"}, claims=contents)
    token.make_signed_token(private_key)
    return token.serialize()
```

---

## Full Agent-Side Session State Flow

Pass state forward through `Event(output=...)` — each node receives its
predecessor's output as `node_input`. This is the generic UCP pipeline;
rename the selection nodes for your domain.

```python
# Generic UCP pipeline state:
browse_catalog     → {item_id, item_name, store_id}
select_variant     → {+variant_id, variant_label}
select_qty         → {+qty, merchant_public_jwk}
                       ↓
create_checkout    → {cart_mandate, checkout, total_cents, session_id, merchant_public_jwk}
verify_cart        → {same + verified CartMandate}
authorize_payment  → {same + user_id}
sign_ap2_mandates  → {same + payment_mandate, payment_sd_jwt}
verify_mandates    → {same + order_id, verified=True}
order_complete     → debit wallet, return {status, order_id}
```

---

## Adapting UCP for Your Domain

UCP is intentionally domain-agnostic. The only parts that change are the
**MCP tool names** and the **selection nodes** before `create_checkout`.
Everything from `create_checkout` onward is identical for every domain.

**Steps to implement a new domain:**

1. **Merchant server** — implement the four standard endpoint shapes:
   - A browsing tool (`search_*`) → list of items with `id` + metadata
   - A details tool (`get_*`) → variants, pricing, availability
   - `create_checkout` → **must** return `{session_id, total_cents, cart_mandate, ...}`
   - `POST /verify-mandate` → **must** return `{verified, order_id}` or `{verified: false, error}`

2. **Agent workflow** — write domain-specific selection nodes, then connect them
   to the standard checkout pipeline:
   ```
   START → [your selection nodes] → create_checkout → verify_cart → authorize_payment
        → sign_ap2_mandates → verify_mandates → order_complete
   ```

3. **AP2 mandates** — `PaymentMandateContents` is domain-agnostic.
   Only `merchant_agent` changes (use your service/store ID).

4. **`/ucp-profile`** — expose `merchant_public_jwk` (required for CartMandate
   verification on the agent side).

**Domain examples:**

| Domain | Browse tool | Details tool | Key checkout params |
|--------|-------------|--------------|---------------------|
| E-commerce | `search_products` | `get_product` | `{product_id, variant_id, qty}` |
| Hotel booking | `search_hotels` | `get_availability` | `{hotel_id, room_type, check_in, nights}` |
| Subscription | `list_plans` | `get_plan_details` | `{plan_id, billing_cycle}` |
| Event tickets | `search_events` | `get_event_details` | `{event_id, section, qty}` |

---

## Environment Variables

| Variable | Required | Example |
|----------|----------|---------|
| `MERCHANT_URL` | Yes | `http://localhost:8001` |
| `GOOGLE_GENAI_API_KEY` | Only if using LLM nodes | `AIza...` |

---

## Gotchas

| # | Symptom | Cause | Fix |
|---|---------|-------|-----|
| 1 | `create_checkout` returns empty `cart_mandate` | Merchant not signing with its private key | Check `keys.py` — key must be generated on first run and persisted to disk |
| 2 | `verify_mandate` always returns `verified: False` | `session_id` in PaymentMandate doesn't match CartMandate | Use `checkout["session_id"]` for `payment_details_id` in PaymentMandateContents |
| 3 | Merchant returns 422 on MCP call | `arguments` key missing in request body | MCP endpoint expects `{"tool": "...", "arguments": {...}}` — not `{"tool": "...", "params": {...}}` |
| 4 | `fetch_ucp_profile` returns empty JWK | Merchant started but keys not yet generated | Keys are generated lazily on first checkout — call `create_checkout` once to trigger |
| 5 | Item details returns empty variants | Variant data missing in catalog fixture | Check catalog data — variants must be a list of objects with `id`, `label`, `price_cents` |
