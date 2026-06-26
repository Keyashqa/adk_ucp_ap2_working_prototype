---
name: ap2-payment-protocol
description: >
  Complete implementation guide for AP2 (Agent Payment Protocol v2).
  Covers the double-mandate architecture (CartMandate + PaymentMandate),
  SD-JWT signing, double-ledger bookkeeping, verification flow, and all
  Python code patterns. Domain-agnostic — works for any commerce type
  (e-commerce, ticketing, bookings, subscriptions, etc.).
metadata:
  author: Karthikeyan TS
  version: 1.0.0
  requires:
    packages:
      - ap2
      - jwcrypto
      - python-jose
---

# AP2 — Agent Payment Protocol v2

AP2 is a double-mandate payment protocol designed for autonomous agent commerce.
Instead of a single authorization token, every transaction requires **two independent
mandates** that must both verify before any money moves or any order is confirmed.

---

## Core Concept — The Double-Mandate Architecture

```
User agent                   Merchant agent
    │                             │
    │── select item ─────────────▶│
    │                             │── sign CartMandate (merchant JWT) ──▶ returns to user
    │◀── CartMandate ─────────────│
    │
    │── user reviews CartMandate
    │── signs PaymentMandate (SD-JWT with user's private key)
    │
    │── POST both mandates to merchant ──────────────────────────────▶│
    │                                                                   │── verify CartMandate (merchant re-checks own JWT)
    │                                                                   │── verify PaymentMandate (SD-JWT against user public key)
    │                                                                   │── both valid → confirm order → debit user ledger
    │◀── order_id ──────────────────────────────────────────────────────│
```

**Why two mandates?**
- CartMandate proves the merchant agreed to these exact terms (price, quantity, item) and signed them.
- PaymentMandate proves the user explicitly authorized this exact transaction amount.
- Neither party can forge the other's mandate. Both must be present and valid.

---

## The Double Ledger

AP2 uses **two independent ledgers** — one per party. Each records only the
transactions it controls. A valid order requires an entry in both.

| Ledger | Owner | Records | Updated by |
|--------|-------|---------|-----------|
| Merchant ledger | Merchant server | CartMandate issued → order confirmed | Merchant when `verify_mandate` succeeds |
| User wallet | User/agent | Balance, debits, credits | Agent after order confirmed |

**Critical rule:** Never debit the user wallet before the merchant confirms the
order via `verify_mandate`. The deduction must happen **after** the merchant
returns `{"verified": true, "order_id": "..."}`.

```python
# CORRECT order:
result = await _CLIENT.verify_mandate(...)         # 1. merchant verifies both mandates
if result.get("verified"):
    await wallet_ops.deduct(user_id, total_cents)  # 2. only then debit user ledger

# WRONG order — never debit first:
# await wallet_ops.deduct(...)   ← NO
# result = await _CLIENT.verify_mandate(...)
```

---

## Dependencies

```toml
# pyproject.toml
dependencies = [
    "ap2",
    "jwcrypto>=1.5",
]
```

```python
from ap2.models.mandate import (
    CartContents,
    CartMandate,
    PaymentMandate,
    PaymentMandateContents,
)
from ap2.models.payment_request import (
    PaymentCurrencyAmount,
    PaymentItem,
    PaymentResponse,
)
from ap2.sdk.mandate import MandateClient, SdJwtMandate
from jwcrypto.jwk import JWK
from jwcrypto.jwt import JWT
```

---

## Step 1 — CartMandate (Merchant Side)

The merchant creates a CartMandate when a checkout is initiated. It is a JWT
signed with the merchant's private key. The agent never creates CartMandates —
it receives them from the merchant.

**What a CartMandate contains (`CartContents`):**
- `id` — unique cart ID
- `merchant_name` — human-readable merchant name
- `cart_expiry` — ISO-8601 UTC expiry timestamp
- Line items (product, variant, quantity, price)

**Verifying a CartMandate (agent side):**

```python
from jwcrypto.jwk import JWK
from jwcrypto.jwt import JWT
import json

def verify_cart_mandate_jwt(jwt_str: str, merchant_public_jwk: dict) -> bool:
    """Returns True if the merchant's JWT signature is valid and not tampered."""
    try:
        key = JWK.from_json(json.dumps(merchant_public_jwk))
        JWT(key=key, jwt=jwt_str)  # raises on invalid signature or expired
        return True
    except Exception:
        return False

# Usage:
cart = CartMandate(**checkout["cart_mandate"])
is_valid = verify_cart_mandate_jwt(
    jwt_str=cart.merchant_authorization,
    merchant_public_jwk=merchant_public_jwk_dict,
)
```

**Checking expiry:**

```python
from datetime import datetime, timezone

contents = cart.contents   # CartContents object
expiry = datetime.fromisoformat(contents.cart_expiry.replace("Z", "+00:00"))
if expiry <= datetime.now(timezone.utc):
    raise ValueError("CartMandate expired")
```

---

## Step 2 — PaymentMandate (User/Agent Side)

The agent creates a PaymentMandate by signing `PaymentMandateContents` with
the **user's private key** as an SD-JWT (Selective Disclosure JWT).

```python
import uuid
from datetime import datetime, timezone
from ap2.sdk.mandate import MandateClient

def sign_payment_mandate(
    session_id: str,
    total_cents: int,
    merchant_id: str,          # the merchant / service identifier
    user_private_key,          # JWK private key object
) -> tuple[PaymentMandate, str]:
    """Signs and returns (PaymentMandate, sd_jwt_string)."""

    pmc = PaymentMandateContents(
        payment_mandate_id=f"pm-{uuid.uuid4().hex[:12]}",
        payment_details_id=session_id,              # ties to the CartMandate session
        payment_details_total=PaymentItem(
            label="Total",
            amount=PaymentCurrencyAmount(
                currency="USD",
                value=total_cents / 100,            # convert cents → dollars
            ),
        ),
        payment_response=PaymentResponse(
            request_id=session_id,
            method_name="card",
            details={"card_type": "visa", "last4": "4242"},
        ),
        merchant_agent=merchant_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    sd_jwt_str = MandateClient().create(
        payloads=[pmc],
        issuer_key=user_private_key,
    )

    payment_mandate = PaymentMandate(
        payment_mandate_contents=pmc,
        user_authorization=sd_jwt_str,
    )

    return payment_mandate, sd_jwt_str
```

---

## Step 3 — Verifying a PaymentMandate (Merchant/Agent Side)

Before sending to the merchant, verify the SD-JWT locally:

```python
from ap2.sdk.mandate import SdJwtMandate

def verify_payment_sd_jwt(
    sd_jwt_str: str,
    user_public_key,           # JWK public key object
) -> PaymentMandateContents:
    """Raises on invalid SD-JWT. Returns decoded contents on success."""
    return SdJwtMandate.from_sd_jwt(
        compact_serialization=sd_jwt_str,
        issuer_public_key=user_public_key,
        payload_type=PaymentMandateContents,
    )
```

---

## Step 4 — Double Verification (Both Mandates Together)

This is the critical gate. Both mandates must pass before the order is confirmed.

```python
async def verify_both_mandates(
    cart_mandate: dict,
    payment_sd_jwt: str,
    user_id: str,
    merchant_public_jwk: dict,
) -> dict:
    """
    1. Re-verify CartMandate JWT (agent-side sanity check)
    2. Re-verify PaymentMandate SD-JWT (agent-side sanity check)
    3. POST both to merchant — merchant is the final authority
    Returns {"verified": True, "order_id": "..."} or {"verified": False, "error": "..."}
    """
    issues = []
    cart = CartMandate(**cart_mandate)

    # Agent-side checks
    if not verify_cart_mandate_jwt(cart.merchant_authorization or "", merchant_public_jwk):
        issues.append("CartMandate merchant signature invalid")

    user_pub_key = user_public_key_for(user_id)
    try:
        verify_payment_sd_jwt(payment_sd_jwt, user_pub_key)
    except Exception as exc:
        issues.append(f"PaymentMandate SD-JWT: {exc}")

    if issues:
        return {"verified": False, "error": "; ".join(issues)}

    # Merchant is the final authority — POST both mandates
    result = await merchant_client.verify_mandate(
        session_id=cart_mandate["session_id"],
        cart_mandate=cart_mandate,
        payment_sd_jwt=payment_sd_jwt,
        user_public_jwk=user_public_jwk_dict,
    )
    return result
```

---

## Step 5 — Debit the User Ledger (After Merchant Confirms)

Only debit after the merchant confirms the order:

```python
async def complete_order(node_input: dict, order_id: str, user_id: str):
    total_cents = node_input["total_cents"]
    session_id  = node_input["session_id"]

    # Debit wallet — must come AFTER verify_mandate returns verified=True
    await wallet_ops.deduct(
        user_id=user_id,
        amount_cents=total_cents,
        reason="purchase",
        reference_id=session_id,   # cross-reference to CartMandate session
    )
```

**Wallet operations interface (implement these for your storage backend):**

```python
async def get_balance(user_id: str) -> int:
    """Returns balance in cents."""

async def deduct(user_id: str, amount_cents: int, reason: str, reference_id: str) -> None:
    """Deducts amount from balance. Raises InsufficientFunds if balance < amount."""

async def credit(user_id: str, amount_cents: int, reason: str, reference_id: str) -> None:
    """Credits amount to balance."""
```

**Always check balance BEFORE showing the payment modal:**

```python
balance = await get_balance(user_id)
if balance < total_cents:
    # Route to cancelled — don't show PIN modal for a transaction that will fail
    return Event(output=..., route="cancelled",
                 content=_content(f"Insufficient balance: ${balance/100:.2f} available"))
```

---

## Full Node Sequence (ADK Workflow Pattern)

```
create_checkout      ← calls merchant API, receives CartMandate JWT
    │
verify_cart          ← checks CartMandate expiry + signature
    │
authorize_payment    ← HITL: checks wallet balance, then shows PIN modal
    │
sign_ap2_mandates    ← creates PaymentMandate, signs SD-JWT with user key
    │
verify_mandates      ← double-verifies both mandates locally, then POSTs to merchant
    │
order_complete       ← merchant confirmed: debit user wallet, record order_id
```

---

## Key Types Reference

```python
# CartMandate — what the merchant sends back after checkout
class CartMandate:
    contents: CartContents
    merchant_authorization: str    # JWT string, signed by merchant

class CartContents:
    id: str                        # cart ID
    merchant_name: str
    cart_expiry: str               # ISO-8601 UTC
    # ... line items

# PaymentMandateContents — what the user/agent constructs and signs
class PaymentMandateContents:
    payment_mandate_id: str
    payment_details_id: str        # MUST match CartMandate session_id
    payment_details_total: PaymentItem
    payment_response: PaymentResponse
    merchant_agent: str            # merchant / service identifier
    timestamp: str                 # ISO-8601 UTC signing time

# PaymentItem
class PaymentItem:
    label: str
    amount: PaymentCurrencyAmount

class PaymentCurrencyAmount:
    currency: str    # "USD"
    value: float     # dollars (not cents)
```

---

## Gotchas

| # | Symptom | Cause | Fix |
|---|---------|-------|-----|
| 1 | `JWT` raises on valid-looking token | `JWK.from_json()` expects a JSON **string**, not a dict | `JWK.from_json(json.dumps(jwk_dict))` — always serialize first |
| 2 | SD-JWT verification fails | `payment_details_id` doesn't match the `session_id` from checkout | They must be identical — use `checkout["session_id"]` for both |
| 3 | Wallet deducted but order not confirmed | Debit happened before `verify_mandate` responded | Move `wallet.deduct()` inside the `if result.get("verified")` branch |
| 4 | `InsufficientFunds` raised mid-purchase | Balance not checked before PIN modal | Always `get_balance()` in `authorize_payment` before `RequestInput` |
| 5 | `value` field wrong | `PaymentCurrencyAmount.value` is in **dollars**, not cents | `value=total_cents / 100` |
| 6 | Mandate ID collision | Using a fixed string as `payment_mandate_id` | Always `f"pm-{uuid.uuid4().hex[:12]}"` |
