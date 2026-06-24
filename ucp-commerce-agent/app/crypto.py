"""Mock cryptographic utilities for UCP/AP2 local prototype.

Real implementations would use proper asymmetric keys (Ed25519) and
standard SD-JWT libraries. These mocks reproduce the structural protocol
flow without requiring a PKI.
"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone

# Mock key material — constants so signatures are deterministic and verifiable
MERCHANT_PRIVATE_KEY = "mock-merchant-sk-abc123XYZ-secret"
MERCHANT_PUBLIC_KEY  = "mock-merchant-pk-xyz789ABC-public"
USER_PRIVATE_KEY     = "mock-user-sk-def456PQR-secret"
USER_PUBLIC_KEY      = "mock-user-pk-uvw012STU-public"

# Map public → private for local verification without real asymmetric crypto
_KEY_PAIRS: dict[str, str] = {
    MERCHANT_PUBLIC_KEY: MERCHANT_PRIVATE_KEY,
    USER_PUBLIC_KEY:     USER_PRIVATE_KEY,
}


def sign_payload(payload: dict, private_key: str) -> str:
    """Produce a mock signature: SHA-256(private_key || canonical_json), base64url-encoded."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256((private_key + canonical).encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def verify_signature(payload: dict, signature: str, public_key: str) -> bool:
    """Verify a mock signature using the pre-shared private key paired to public_key."""
    private_key = _KEY_PAIRS.get(public_key)
    if private_key is None:
        return False
    expected = sign_payload(payload, private_key)
    return expected == signature


def generate_sd_jwt(user_id: str, cart_mandate_id: str, scope: str = "payment_authorization") -> str:
    """Produce a mock Selective Disclosure JWT (SD-JWT) as a user authorization token.

    Format: <header>.<payload>.<signature>  (all base64url, no padding)
    """
    header_b64 = base64.urlsafe_b64encode(
        b'{"alg":"mock-HS256","typ":"sd-jwt"}'
    ).decode().rstrip("=")

    payload_obj = {
        "sub": user_id,
        "cart_mandate_id": cart_mandate_id,
        "scope": scope,
        "iat": datetime.now(timezone.utc).isoformat(),
        "_sd": ["sub", "cart_mandate_id"],  # selective-disclosure claim list
    }
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload_obj, separators=(",", ":")).encode()
    ).decode().rstrip("=")

    sig = sign_payload(payload_obj, USER_PRIVATE_KEY)
    return f"{header_b64}.{payload_b64}.{sig}"
