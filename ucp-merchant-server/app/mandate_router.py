"""Mandate verification endpoint — verifies CartMandate + PaymentMandate SD-JWT."""
from __future__ import annotations

import json

from ap2.models.mandate import CartMandate, PaymentMandateContents
from ap2.sdk.mandate import SdJwtMandate
from fastapi import APIRouter
from jwcrypto.jwk import JWK
from jwcrypto.jwt import JWT

from app.catalog import get_stats
from app.catalog import get_all_bookings
from app.checkout import confirm_booking
from app.db import get_conn
from app.keys import merchant_public_key
from app.models import MandateVerifyRequest, MandateVerifyResponse

router = APIRouter()


def _verify_cart_mandate_jwt(jwt_str: str) -> bool:
    try:
        JWT(key=merchant_public_key(), jwt=jwt_str)
        return True
    except Exception:
        return False


def _verify_payment_sd_jwt(sd_jwt_str: str, user_public_jwk_dict: dict) -> tuple[bool, str]:
    try:
        user_pubkey = JWK.from_json(json.dumps(user_public_jwk_dict))
        SdJwtMandate.from_sd_jwt(
            compact_serialization=sd_jwt_str,
            issuer_public_key=user_pubkey,
            payload_type=PaymentMandateContents,
        )
        return True, ""
    except Exception as exc:
        return False, str(exc)


@router.post("/mandates/verify", response_model_exclude_none=True)
def verify_mandates(req: MandateVerifyRequest) -> MandateVerifyResponse:
    issues: list[str] = []

    # 1. Verify merchant CartMandate JWT
    cart = CartMandate(**req.cart_mandate)
    if not cart.merchant_authorization:
        issues.append("missing merchant_authorization")
    elif not _verify_cart_mandate_jwt(cart.merchant_authorization):
        issues.append("CartMandate merchant signature invalid")

    # 2. Verify that the session_id in the mandate matches our DB
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT session_id, total_cents FROM checkout_sessions WHERE session_id=? AND status='pending'",
            (req.session_id,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return MandateVerifyResponse(
            verified=False,
            error=f"Session not found or already confirmed: {req.session_id}",
        )

    # 3. Verify user PaymentMandate SD-JWT
    ok, err = _verify_payment_sd_jwt(req.payment_sd_jwt, req.user_public_jwk)
    if not ok:
        issues.append(f"PaymentMandate SD-JWT: {err}")

    if issues:
        return MandateVerifyResponse(verified=False, error="; ".join(issues))

    # 4. Extract payment_mandate_id from the SD-JWT header (best-effort)
    payment_mandate_id = req.session_id  # fallback
    try:
        parts = req.payment_sd_jwt.split(".")
        if parts:
            import base64
            padded = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
            payment_mandate_id = payload.get("payment_mandate_id", req.session_id)
    except Exception:
        pass

    # 5. Record the booking
    booking_id = confirm_booking(
        session_id=req.session_id,
        payment_mandate_jwt=req.payment_sd_jwt,
        payment_mandate_id=payment_mandate_id,
        user_public_jwk=req.user_public_jwk,
    )

    return MandateVerifyResponse(verified=True, booking_id=booking_id)


@router.get("/bookings")
def list_bookings() -> list[dict]:
    return get_all_bookings()


@router.get("/stats")
def get_revenue_stats() -> dict:
    return get_stats()
