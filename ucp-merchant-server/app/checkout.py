"""Checkout session creation and CartMandate signing logic."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from ap2.models.mandate import CartContents, CartMandate
from ap2.models.payment_request import (
    PaymentCurrencyAmount,
    PaymentDetailsInit,
    PaymentItem,
    PaymentMethodData,
    PaymentRequest,
)
from jwcrypto.jwt import JWT

from app.catalog import get_movie, get_seat_categories, get_showtime, get_theater
from app.db import get_conn
from app.keys import merchant_private_key


def create_checkout_session(
    theater_id: str,
    movie_id: str,
    slot: str,
    seat_code: str,
    qty: int,
) -> dict:
    """Create a checkout session and return it with a signed CartMandate."""
    theater = get_theater(theater_id)
    movie = get_movie(movie_id)
    show = get_showtime(theater_id, movie_id, slot)
    if not theater or not movie or not show:
        raise ValueError(f"Invalid booking parameters: {theater_id}/{movie_id}/{slot}")

    seat_cats = {sc["code"]: sc for sc in get_seat_categories(theater_id)}
    seat = seat_cats.get(seat_code.upper(), seat_cats["S"])
    qty = max(1, min(6, qty))

    total_cents = seat["price_cents"] * qty
    session_id = f"ckout-{uuid.uuid4().hex[:10]}"
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()

    cart_mandate = create_signed_cart_mandate(
        session_id=session_id,
        theater_name=theater["name"],
        movie_title=movie["title"],
        show_time=show["time_label"],
        seat_label=seat["label"],
        qty=qty,
        total_cents=total_cents,
        expires_at=expires_at,
    )
    cart_mandate_jwt = cart_mandate.merchant_authorization

    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO checkout_sessions
               (session_id, theater_id, movie_id, slot, seat_code, qty,
                total_cents, expires_at, status, cart_mandate_jwt)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (session_id, theater_id, movie_id, slot, seat_code, qty,
             total_cents, expires_at, "pending", cart_mandate_jwt),
        )
        conn.commit()
    finally:
        conn.close()

    return {
        "session_id":   session_id,
        "theater_id":   theater_id,
        "theater_name": theater["name"],
        "movie":        movie,
        "show":         show,
        "seat":         seat,
        "qty":          qty,
        "total_cents":  total_cents,
        "expires_at":   expires_at,
        "cart_mandate": cart_mandate.model_dump(),
    }


def create_signed_cart_mandate(
    session_id: str,
    theater_name: str,
    movie_title: str,
    show_time: str,
    seat_label: str,
    qty: int,
    total_cents: int,
    expires_at: str,
) -> CartMandate:
    """Build CartContents and sign it as a merchant ES256 JWT."""
    display_label = f"{qty}x {seat_label} — {movie_title} ({show_time})"
    payment_request = PaymentRequest(
        method_data=[PaymentMethodData(supported_methods="card")],
        details=PaymentDetailsInit(
            id=session_id,
            display_items=[
                PaymentItem(
                    label=display_label,
                    amount=PaymentCurrencyAmount(currency="USD", value=total_cents / 100),
                )
            ],
            total=PaymentItem(
                label="Total",
                amount=PaymentCurrencyAmount(currency="USD", value=total_cents / 100),
            ),
        ),
    )

    contents = CartContents(
        id=session_id,
        user_cart_confirmation_required=True,
        payment_request=payment_request,
        cart_expiry=expires_at,
        merchant_name=theater_name,
    )

    jwk = merchant_private_key()
    tok = JWT(
        header=json.dumps({"alg": "ES256", "typ": "JWT", "kid": "merchant"}),
        claims=json.dumps(contents.model_dump(), default=str),
    )
    tok.make_signed_token(jwk)

    return CartMandate(contents=contents, merchant_authorization=tok.serialize())


def confirm_booking(
    session_id: str,
    payment_mandate_jwt: str,
    payment_mandate_id: str,
    user_public_jwk: dict,
) -> str:
    """Mark a checkout session as confirmed and record the booking."""
    booking_id = f"bkg-{uuid.uuid4().hex[:12]}"
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT total_cents FROM checkout_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if not row:
            raise ValueError(f"Session not found: {session_id}")
        charged_cents = row["total_cents"]

        conn.execute(
            "UPDATE checkout_sessions SET status='confirmed' WHERE session_id=?",
            (session_id,),
        )
        conn.execute(
            """INSERT INTO bookings
               (id, session_id, payment_mandate_jwt, payment_mandate_id,
                user_public_jwk, charged_cents)
               VALUES (?,?,?,?,?,?)""",
            (booking_id, session_id, payment_mandate_jwt, payment_mandate_id,
             json.dumps(user_public_jwk), charged_cents),
        )
        conn.commit()
    finally:
        conn.close()
    return booking_id
