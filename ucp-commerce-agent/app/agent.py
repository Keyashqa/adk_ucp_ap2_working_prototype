# ruff: noqa
"""CineAgent — ADK 2.0 Workflow graph for cinema ticket booking.

Graph topology:
  START
    └─▶ discover_theaters            (automated: UCP BusinessSchema discovery)
          └─▶ search_movies           (automated: MCP catalog search)
                └─▶ select_showtime   ← HITL: user picks theater/movie/show/seat/qty
                      ├─[cancelled]─▶ booking_cancelled_terminal
                      └─[selected]──▶ create_checkout    (automated: CartMandate via AP2)
                                        └─▶ verify_booking    (automated: validate CartMandate)
                                              ├─[invalid]─▶ booking_invalid_terminal
                                              └─[valid]──▶ authorize_payment  ← HITL: confirm pay
                                                            ├─[cancelled]─▶ booking_cancelled_terminal
                                                            └─[confirmed]─▶ sign_ap2_mandates
                                                                              └─▶ verify_mandates
                                                                                    ├─[verified]──▶ booking_complete_terminal
                                                                                    └─[rejected]──▶ sig_rejected_terminal
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import google.auth
from ap2.models.mandate import (
    CartContents,
    CartMandate,
    PaymentMandate,
    PaymentMandateContents,
)
from ap2.models.payment_request import (
    PaymentCurrencyAmount,
    PaymentDetailsInit,
    PaymentItem,
    PaymentMethodData,
    PaymentRequest,
    PaymentResponse,
)
from ap2.sdk.mandate import MandateClient, SdJwtMandate
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import Workflow, node
from google.genai import types as genai_types
from jwcrypto.jwt import JWT

from app.cinema import MOVIES, SEAT_CATS, SHOWS, THEATER_META, build_ucp_profile, call_mcp
from app.keys import merchant_private_key, merchant_public_key, user_private_key, user_public_key

# ── GCP auth ──────────────────────────────────────────────────────────────

_, _project_id = google.auth.default()
os.environ["GOOGLE_CLOUD_PROJECT"] = _project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

# ── Helpers ────────────────────────────────────────────────────────────────

_SEP = "=" * 60


def _content(text: str) -> genai_types.Content:
    return genai_types.Content(role="model", parts=[genai_types.Part.from_text(text=text)])


def _ev(output: Any, text: str, route: str | None = None) -> Event:
    kw: dict[str, Any] = {"output": output, "content": _content(text)}
    if route:
        kw["route"] = route
    return Event(**kw)


# ── Cart JWT helpers (merchant signs CartContents) ─────────────────────────

def _sign_cart_jwt(contents_dict: dict[str, Any]) -> str:
    """Sign CartContents as a compact JWT using the theater's merchant key."""
    tok = JWT(
        header=json.dumps({"alg": "ES256", "typ": "JWT", "kid": "merchant"}),
        claims=json.dumps(contents_dict, default=str),
    )
    tok.make_signed_token(merchant_private_key())
    return tok.serialize()


def _verify_cart_jwt(jwt_str: str) -> bool:
    """Verify the merchant JWT signature over CartContents."""
    try:
        JWT(key=merchant_public_key(), jwt=jwt_str)
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — discover_theaters
# Automated: build real UCP BusinessSchema profiles for all registered theaters
# ══════════════════════════════════════════════════════════════════════════════

def discover_theaters(node_input: Any) -> Any:
    """Build UCP BusinessSchema profiles for every theater and return summaries."""
    theater_list = []
    for tid, meta in THEATER_META.items():
        profile = build_ucp_profile(tid)
        theater_list.append({
            "theater_id":      tid,
            "theater_name":    meta["name"],
            "location":        meta["location"],
            "mcp_endpoint":    meta["mcp_endpoint"],
            "ucp_version":     profile.version.root,
            "capabilities":    list((profile.capabilities or {}).keys()),
            "payment_handlers": list(profile.payment_handlers.keys()),
        })

    lines = "\n".join(
        f"  [{t['theater_id']}]  {t['theater_name']}  —  {t['location']}"
        for t in theater_list
    )
    summary = (
        f"\n{_SEP}\n"
        f"  NODE 1 · Theater Discovery (UCP)\n"
        f"{_SEP}\n"
        f"{lines}\n"
        f"  UCP version: {theater_list[0]['ucp_version']}\n"
        f"{_SEP}\n"
    )
    return Event(
        output=theater_list,
        state={"theaters": theater_list},
        content=_content(summary),
    )


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — search_movies
# Automated: MCP search_movies across every discovered theater
# ══════════════════════════════════════════════════════════════════════════════

def search_movies(node_input: list[dict[str, Any]]) -> Any:
    """Query each theater's MCP endpoint and return a deduplicated movie catalog."""
    all_movies: dict[str, dict[str, Any]] = {}
    for theater in node_input:
        rsp = call_mcp("search_movies", {"query": "", "limit": 10}, theater["theater_id"])
        for m in rsp.get("result", {}).get("movies", []):
            if m["movie_id"] not in all_movies:
                all_movies[m["movie_id"]] = {**m, "available_at": []}
            all_movies[m["movie_id"]]["available_at"].append(theater["theater_id"])

    movie_list  = list(all_movies.values())
    movie_lines = "\n".join(
        f"  {i + 1}. {m['title']} ({m['genre']}, {m['duration_min']}min, {m['language']})"
        f"  [{', '.join(m['available_at'])}]"
        for i, m in enumerate(movie_list)
    )
    slot_lines = "\n".join(
        f"  {s['slot']}. {s['time']}  ({s['seats_left']} seats left)" for s in SHOWS
    )
    seat_lines = "\n".join(
        f"  {k}. {v['label']}  —  ${v['price_cents'] / 100:.2f}" for k, v in SEAT_CATS.items()
    )

    summary = (
        f"\n{_SEP}\n"
        f"  NODE 2 · Movie Catalog (MCP)\n"
        f"{_SEP}\n"
        f"  MOVIES:\n{movie_lines}\n\n"
        f"  SHOWTIMES:\n{slot_lines}\n\n"
        f"  SEAT CATEGORIES:\n{seat_lines}\n"
        f"{_SEP}\n"
    )
    return Event(
        output={"movies": movie_list, "index": {str(i + 1): m for i, m in enumerate(movie_list)}},
        state={"movies": movie_list},
        content=_content(summary),
    )


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — select_showtime   [HUMAN-IN-THE-LOOP]
# Pause: user picks theater · movie · slot · seat · qty
# Resume: parse input → route "selected" or "cancelled"
# ══════════════════════════════════════════════════════════════════════════════

@node(rerun_on_resume=True)
async def select_showtime(ctx: Context, node_input: dict[str, Any]):
    """Prompt the user to choose their movie, showtime, seat type, and quantity."""
    movies = node_input.get("movies", [])

    if "showtime_selection" not in ctx.resume_inputs:
        movie_lines = "\n".join(
            f"    {i + 1}. {m['title']} ({m['genre']})  [{', '.join(m['available_at'])}]"
            for i, m in enumerate(movies)
        )
        prompt = (
            f"\n{_SEP}\n"
            f"  NODE 3 · SELECT YOUR BOOKING\n"
            f"{_SEP}\n"
            f"  THEATERS:\n"
            f"    pvr  = PVR Cinemas (Phoenix Mall)\n"
            f"    inox = INOX Multiplex (Orion Mall)\n\n"
            f"  MOVIES:\n{movie_lines}\n\n"
            f"  SHOWTIMES:  A = 10:00 AM   B = 2:30 PM   C = 7:00 PM   D = 10:15 PM\n"
            f"  SEATS:      S = Standard ($12)   P = Premium ($18)   I = IMAX ($22)\n\n"
            f"  Format: <theater> <movie#> <slot> <seat> <qty>\n"
            f"  Example: pvr 1 C P 2  →  PVR · Interstellar Redux · 7PM · Premium · 2 tickets\n"
            f"{_SEP}\n"
        )
        yield _content(prompt)
        yield RequestInput(interrupt_id="showtime_selection", message=prompt)
        return

    raw   = str(ctx.resume_inputs.get("showtime_selection", "")).strip()
    parts = raw.split()

    if len(parts) < 5:
        yield Event(
            output={"error": "incomplete_input", "raw": raw},
            route="cancelled",
            content=_content(
                f"  ✗ Input '{raw}' is incomplete.\n"
                f"  Need: <theater> <movie#> <slot> <seat> <qty>\n"
                f"  Booking cancelled."
            ),
        )
        return

    theater_key, movie_num, slot, seat, qty_str = (
        parts[0], parts[1], parts[2].upper(), parts[3].upper(), parts[4]
    )
    theater_id  = "pvr-001" if theater_key.lower().startswith("pvr") else "inox-001"
    movie_idx   = int(movie_num) - 1 if movie_num.isdigit() and 1 <= int(movie_num) <= len(movies) else 0
    movie       = movies[movie_idx]
    qty         = max(1, min(6, int(qty_str) if qty_str.isdigit() else 1))
    show        = next((s for s in SHOWS if s["slot"] == slot), SHOWS[2])
    seat_info   = SEAT_CATS.get(seat, SEAT_CATS["S"])

    selection = {
        "theater_id":   theater_id,
        "movie_id":     movie["movie_id"],
        "movie_title":  movie["title"],
        "slot":         slot,
        "seat":         seat,
        "qty":          qty,
    }

    yield Event(
        output=selection,
        state={"selection": selection},
        route="selected",
        content=_content(
            f"  ✓ Selected: {movie['title']} · {show['time']} · "
            f"{seat_info['label']} x{qty}  ({THEATER_META[theater_id]['name']})"
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — create_checkout
# Automated: MCP create_checkout → AP2 CartContents → merchant-signed CartMandate
# ══════════════════════════════════════════════════════════════════════════════

def create_checkout(node_input: dict[str, Any]) -> Any:
    """Call MCP create_checkout and produce a merchant-signed AP2 CartMandate."""
    sel = node_input

    checkout = call_mcp(
        "create_checkout",
        {
            "movie_id": sel["movie_id"],
            "slot":     sel["slot"],
            "seat":     sel["seat"],
            "qty":      sel["qty"],
        },
        sel["theater_id"],
    ).get("result", {})

    total_cents = checkout["total_cents"]
    session_id  = checkout["session_id"]
    expires_at  = checkout["expires_at"]
    seat_label  = checkout["seat"]["label"]
    movie_title = checkout["movie"]["title"]
    show_time   = checkout["show"]["time"]

    # AP2 W3C PaymentRequest
    display_label = f"{sel['qty']}x {seat_label} — {movie_title} ({show_time})"
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

    # AP2 CartContents
    contents = CartContents(
        id=session_id,
        user_cart_confirmation_required=True,
        payment_request=payment_request,
        cart_expiry=expires_at,
        merchant_name=checkout["theater_name"],
    )

    # Merchant signs CartContents → CartMandate
    merchant_auth = _sign_cart_jwt(contents.model_dump())
    cart_mandate  = CartMandate(contents=contents, merchant_authorization=merchant_auth)

    msg = (
        f"\n{_SEP}\n"
        f"  NODE 4 · Checkout & CartMandate (AP2)\n"
        f"{_SEP}\n"
        f"  Session:      {session_id}\n"
        f"  Theater:      {checkout['theater_name']}\n"
        f"  Movie:        {movie_title}\n"
        f"  Showtime:     {show_time}\n"
        f"  Seats:        {seat_label} x{sel['qty']}\n"
        f"  Total:        ${total_cents / 100:.2f} USD\n"
        f"  Expires:      {expires_at}\n"
        f"  Merchant sig: {merchant_auth[:52]}...\n"
        f"{_SEP}\n"
    )

    payload = {
        "cart_mandate": cart_mandate.model_dump(),
        "checkout":     checkout,
        "total_cents":  total_cents,
        "session_id":   session_id,
    }
    return Event(output=payload, state={"cart_mandate": cart_mandate.model_dump()}, content=_content(msg))


# ══════════════════════════════════════════════════════════════════════════════
# NODE 5 — verify_booking
# Automated: expiry check + merchant JWT verification on CartMandate
# Routes → "valid" | "invalid"
# ══════════════════════════════════════════════════════════════════════════════

def verify_booking(node_input: dict[str, Any]) -> Any:
    """Validate CartMandate: required fields, price-lock expiry, merchant signature."""
    cart      = CartMandate(**node_input["cart_mandate"])
    contents  = cart.contents
    issues: list[str] = []

    try:
        expiry = datetime.fromisoformat(contents.cart_expiry.replace("Z", "+00:00"))
        if expiry <= datetime.now(timezone.utc):
            issues.append("cart expired")
    except (ValueError, AttributeError):
        issues.append("invalid expiry format")

    if cart.merchant_authorization:
        if not _verify_cart_jwt(cart.merchant_authorization):
            issues.append("merchant signature invalid")
    else:
        issues.append("missing merchant_authorization")

    if not issues:
        msg = (
            f"\n{_SEP}\n"
            f"  NODE 5 · CartMandate Verified ✓\n"
            f"{_SEP}\n"
            f"  Merchant: {contents.merchant_name}\n"
            f"  Cart ID:  {contents.id}\n"
            f"  Total:    ${node_input['total_cents'] / 100:.2f} USD\n"
            f"  Sig:      VALID  |  Expiry: VALID\n"
            f"{_SEP}\n"
        )
        return Event(output=node_input, route="valid", content=_content(msg))

    msg = (
        f"\n{_SEP}\n"
        f"  NODE 5 · CartMandate INVALID\n"
        f"{_SEP}\n"
        f"  Issues: {', '.join(issues)}\n"
        f"{_SEP}\n"
    )
    return Event(output=node_input, route="invalid", content=_content(msg))


# ══════════════════════════════════════════════════════════════════════════════
# NODE 6 — authorize_payment   [HUMAN-IN-THE-LOOP]
# Shows full booking summary; user must type "confirm" or "cancel"
# Routes → "confirmed" | "cancelled"
# ══════════════════════════════════════════════════════════════════════════════

@node(rerun_on_resume=True)
async def authorize_payment(ctx: Context, node_input: dict[str, Any]):
    """Require explicit user confirmation before AP2 mandate signing."""
    checkout    = node_input.get("checkout", {})
    total_cents = node_input.get("total_cents", 0)

    if "payment_auth" not in ctx.resume_inputs:
        movie = checkout.get("movie", {})
        show  = checkout.get("show", {})
        seat  = checkout.get("seat", {})
        qty   = checkout.get("qty", "?")
        prompt = (
            f"\n{_SEP}\n"
            f"  NODE 6 · PAYMENT AUTHORIZATION REQUIRED\n"
            f"{_SEP}\n"
            f"  Movie:      {movie.get('title', '?')}\n"
            f"  Showtime:   {show.get('time', '?')}\n"
            f"  Seats:      {seat.get('label', '?')} x{qty}\n"
            f"  Theater:    {checkout.get('theater_name', '?')}\n"
            f"  ──────────────────────────────────────\n"
            f"  TOTAL:      ${total_cents / 100:.2f} USD\n"
            f"{_SEP}\n"
            f"\n  Type 'confirm' to pay or 'cancel' to abort.\n"
        )
        yield _content(prompt)
        yield RequestInput(interrupt_id="payment_auth", message=prompt)
        return

    response = str(ctx.resume_inputs.get("payment_auth", "")).lower().strip()
    if "confirm" in response:
        yield Event(
            output=node_input,
            route="confirmed",
            content=_content(
                f"  ✓ Payment confirmed (${total_cents / 100:.2f}). Signing AP2 mandates..."
            ),
        )
    else:
        yield Event(
            output=node_input,
            route="cancelled",
            content=_content(f"  ✗ Booking cancelled by user ('{response}')."),
        )


# ══════════════════════════════════════════════════════════════════════════════
# NODE 7 — sign_ap2_mandates
# Automated: PaymentMandateContents → SD-JWT via real AP2 MandateClient
# ══════════════════════════════════════════════════════════════════════════════

def sign_ap2_mandates(node_input: dict[str, Any]) -> Any:
    """Build and cryptographically sign an AP2 PaymentMandate as SD-JWT."""
    checkout    = node_input["checkout"]
    total_cents = node_input["total_cents"]
    session_id  = node_input["session_id"]

    pmc = PaymentMandateContents(
        payment_mandate_id=f"pm-{uuid.uuid4().hex[:12]}",
        payment_details_id=session_id,
        payment_details_total=PaymentItem(
            label="Total",
            amount=PaymentCurrencyAmount(currency="USD", value=total_cents / 100),
        ),
        payment_response=PaymentResponse(
            request_id=session_id,
            method_name="card",
            details={"card_type": "visa", "last4": "4242"},
        ),
        merchant_agent=checkout.get("theater_id", "theater"),
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    # Sign PaymentMandateContents as AP2 SD-JWT using user's private key
    sd_jwt_str = MandateClient().create(payloads=[pmc], issuer_key=user_private_key())

    payment_mandate = PaymentMandate(
        payment_mandate_contents=pmc,
        user_authorization=sd_jwt_str,
    )

    msg = (
        f"\n{_SEP}\n"
        f"  NODE 7 · AP2 PaymentMandate Signed (SD-JWT)\n"
        f"{_SEP}\n"
        f"  Mandate ID: {pmc.payment_mandate_id}\n"
        f"  Session:    {session_id}\n"
        f"  Amount:     ${total_cents / 100:.2f} USD\n"
        f"  SD-JWT:     {sd_jwt_str[:60]}...\n"
        f"{_SEP}\n"
    )

    return Event(
        output={
            **node_input,
            "payment_mandate": payment_mandate.model_dump(),
            "payment_sd_jwt":  sd_jwt_str,
        },
        state={"payment_mandate": payment_mandate.model_dump()},
        content=_content(msg),
    )


# ══════════════════════════════════════════════════════════════════════════════
# NODE 8 — verify_mandates   [HUMAN-IN-THE-LOOP if verification fails]
# Verifies CartMandate merchant JWT + PaymentMandate SD-JWT.
# Auto-routes "verified" if both pass; HITL on any failure.
# Routes → "verified" | "rejected"
# ══════════════════════════════════════════════════════════════════════════════

@node(rerun_on_resume=True)
async def verify_mandates(ctx: Context, node_input: dict[str, Any]):
    """Verify both mandates; escalate to human reviewer on any failure."""
    if "mandate_review" not in ctx.resume_inputs:
        cart       = CartMandate(**node_input["cart_mandate"])
        sd_jwt_str = node_input["payment_sd_jwt"]
        issues: list[str] = []

        # 1. Re-verify merchant CartMandate JWT
        if not _verify_cart_jwt(cart.merchant_authorization or ""):
            issues.append("CartMandate merchant signature invalid")

        # 2. Verify AP2 PaymentMandate SD-JWT (via SdJwtMandate.from_sd_jwt)
        try:
            SdJwtMandate.from_sd_jwt(
                compact_serialization=sd_jwt_str,
                issuer_public_key=user_public_key(),
                payload_type=PaymentMandateContents,
            )
        except Exception as exc:
            issues.append(f"PaymentMandate SD-JWT: {exc}")

        if not issues:
            msg = (
                f"\n{_SEP}\n"
                f"  NODE 8 · Double-Mandate Verification PASSED ✓\n"
                f"{_SEP}\n"
                f"  CartMandate   (merchant JWT):  VALID\n"
                f"  PaymentMandate (AP2 SD-JWT):   VALID\n"
                f"{_SEP}\n"
            )
            yield Event(
                output={**node_input, "verified": True},
                route="verified",
                content=_content(msg),
            )
            return

        # Pause for human review
        yield Event(state={"verification_issues": issues})
        issue_lines = "\n".join(f"    • {i}" for i in issues)
        prompt = (
            f"\n{_SEP}\n"
            f"  NODE 8 · MANDATE VERIFICATION FAILED — HUMAN REVIEW\n"
            f"{_SEP}\n"
            f"  Issues:\n{issue_lines}\n"
            f"{_SEP}\n"
            f"\n  Type 'override' to approve anyway, or 'reject' to abort.\n"
        )
        yield _content(prompt)
        yield RequestInput(interrupt_id="mandate_review", message=prompt)
        return

    # Resume pass: human decision
    response = str(ctx.resume_inputs.get("mandate_review", "")).lower().strip()
    issues   = ctx.state.get("verification_issues", [])

    if "override" in response:
        yield Event(
            output={**node_input, "verified": True, "override": True},
            route="verified",
            content=_content("  ✓ Human reviewer approved override."),
        )
    else:
        yield Event(
            output={**node_input, "verified": False},
            route="rejected",
            content=_content(f"  ✗ Rejected by reviewer. Issues: {issues}"),
        )


# ══════════════════════════════════════════════════════════════════════════════
# Terminal Nodes
# ══════════════════════════════════════════════════════════════════════════════

def booking_complete_terminal(node_input: dict[str, Any]) -> Any:
    checkout    = node_input.get("checkout", {})
    total_cents = node_input.get("total_cents", 0)
    note        = "  Note:     Human override applied\n" if node_input.get("override") else ""
    msg = (
        f"\n{_SEP}\n"
        f"  BOOKING CONFIRMED!\n"
        f"{_SEP}\n"
        f"  Movie:    {checkout.get('movie', {}).get('title', '?')}\n"
        f"  Theater:  {checkout.get('theater_name', '?')}\n"
        f"  Showtime: {checkout.get('show', {}).get('time', '?')}\n"
        f"  Seats:    {checkout.get('seat', {}).get('label', '?')} x{checkout.get('qty', '?')}\n"
        f"  Charged:  ${total_cents / 100:.2f} USD\n"
        f"{note}"
        f"\n  Enjoy your movie!\n"
        f"{_SEP}\n"
    )
    return Event(
        output={"status": "booked", "session_id": node_input.get("session_id")},
        content=_content(msg),
    )


def booking_invalid_terminal(node_input: dict[str, Any]) -> Any:
    msg = (
        f"\n{_SEP}\n"
        f"  BOOKING FAILED — Invalid CartMandate\n"
        f"{_SEP}\n"
        f"  The theater's CartMandate failed validation.\n"
        f"  Please try again or contact the theater.\n"
        f"{_SEP}\n"
    )
    return Event(output={"status": "invalid_mandate"}, content=_content(msg))


def booking_cancelled_terminal(node_input: dict[str, Any]) -> Any:
    msg = (
        f"\n{_SEP}\n"
        f"  BOOKING CANCELLED\n"
        f"{_SEP}\n"
        f"  No payment was made. Your session has been discarded.\n"
        f"{_SEP}\n"
    )
    return Event(output={"status": "cancelled"}, content=_content(msg))


def sig_rejected_terminal(node_input: dict[str, Any]) -> Any:
    msg = (
        f"\n{_SEP}\n"
        f"  BOOKING ABORTED — Mandate Verification Rejected\n"
        f"{_SEP}\n"
        f"  No payment was made.\n"
        f"{_SEP}\n"
    )
    return Event(output={"status": "sig_rejected"}, content=_content(msg))


# ══════════════════════════════════════════════════════════════════════════════
# Workflow Graph
# ══════════════════════════════════════════════════════════════════════════════

root_agent = Workflow(
    name="cineagent",
    description=(
        "Cinema ticket booking agent implementing UCP discovery and "
        "AP2 mandate-based payments with two HITL authorization gates."
    ),
    edges=[
        # Automated discovery
        ("START",           discover_theaters),
        (discover_theaters, search_movies),
        (search_movies,     select_showtime),

        # HITL 1: user picks movie/show/seat
        (select_showtime, {
            "selected":  create_checkout,
            "cancelled": booking_cancelled_terminal,
        }),

        # Automated: build + validate CartMandate
        (create_checkout, verify_booking),
        (verify_booking, {
            "valid":   authorize_payment,
            "invalid": booking_invalid_terminal,
        }),

        # HITL 2: user confirms payment
        (authorize_payment, {
            "confirmed": sign_ap2_mandates,
            "cancelled": booking_cancelled_terminal,
        }),

        # Automated: sign + verify AP2 mandates
        (sign_ap2_mandates, verify_mandates),
        (verify_mandates, {
            "verified": booking_complete_terminal,
            "rejected": sig_rejected_terminal,
        }),
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True),
)
