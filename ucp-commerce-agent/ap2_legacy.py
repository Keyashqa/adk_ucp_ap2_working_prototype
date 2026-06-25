"""AP2 handler — mock payment mandate signer for demo use."""

import hashlib
import json
import time


class AP2Handler:
    def process_cart_mandate(self, checkout: dict) -> dict | None:
        """Extract the cart mandate from a UCP checkout response."""
        return checkout.get("ap2", {}).get("cart_mandate")

    def create_payment_mandate(self, cart_mandate: dict, payment_method: str) -> dict:
        """Create a mock signed payment mandate from a cart mandate.

        In production this triggers biometric/device auth via the AP2 Wallet SDK.
        Here it just computes a SHA-256 hash of the cart contents as the signature.
        """
        contents = cart_mandate.get("contents", {})
        payload = json.dumps(contents, sort_keys=True).encode()
        signature = hashlib.sha256(payload).hexdigest()
        return {
            "cart_mandate_id": contents.get("id"),
            "payment_method": payment_method,
            "amount": contents.get("total", {}).get("amount", {}),
            "merchant_name": contents.get("merchant_name"),
            "authorized_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "signature": signature,
        }
