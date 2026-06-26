# ruff: noqa
"""CineAgent — ADK 2.0 Workflow graph for cinema ticket booking.

Graph topology (fully deterministic — no LLM):
  START
    └─▶ show_movies         (A2UI movie cards + HITL: user clicks "Book Now")
          └─▶ show_showtimes   (A2UI showtime buttons + HITL: user picks slot)
                └─▶ show_seat_selection  (A2UI seat-type buttons + HITL)
                      └─▶ show_qty_selection  (A2UI ticket-count buttons + HITL)
                            ├─[confirmed]─▶ create_checkout   (merchant CartMandate)
                            │                └─▶ verify_booking
                            │                      ├─[invalid]─▶ booking_invalid_terminal
                            │                      └─[valid]──▶ authorize_payment  ← HITL: PIN
                            │                                    ├─[cancelled]─▶ booking_cancelled_terminal
                            │                                    └─[confirmed]─▶ sign_ap2_mandates
                            │                                                      └─▶ verify_mandates
                            │                                                            ├─[verified]──▶ booking_complete_terminal
                            │                                                            └─[rejected]──▶ sig_rejected_terminal
                            └─[cancelled]─▶ booking_cancelled_terminal
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

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
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import Workflow, node
from google.genai import types as genai_types
from jwcrypto.jwk import JWK
from jwcrypto.jwt import JWT

from app.keys import user_private_key_for, user_public_key_for
from app.merchant_client import MerchantClient

# ── Singletons ─────────────────────────────────────────────────────────────────

_SEP = "=" * 60
_CLIENT = MerchantClient()

# ── A2UI transport ─────────────────────────────────────────────────────────────

CATALOG_ID = "https://a2ui.org/specification/v0_9/catalogs/basic/catalog.json"

SLOT_LABELS: dict[str, str] = {
    "A": "10:00 AM",
    "B": "2:30 PM",
    "C": "7:00 PM",
    "D": "10:15 PM",
}


def _a2ui_content(messages: list[dict]) -> genai_types.Content:
    """Wrap A2UI messages as a tagged text Part for SSE transport.

    json.dumps without indent keeps everything on one line so it fits in a
    single SSE data: frame (the frontend buf.split('\\n') accumulator requires this).
    """
    payload = f"<a2ui-json>{json.dumps(messages)}</a2ui-json>"
    return genai_types.Content(role="model", parts=[genai_types.Part(text=payload)])


# ── A2UI builder functions ─────────────────────────────────────────────────────

def _build_welcome_a2ui(movies: list[dict]) -> list[dict]:
    surface_id = "cinema-welcome"
    movie_ids = [m["id"] for m in movies]
    components: list[dict] = [
        {"id": "root", "component": "Column", "children": ["welcome-heading", "welcome-sub", "movies-col"]},
        {"id": "welcome-heading", "component": "Text", "text": "Now Showing"},
        {"id": "welcome-sub", "component": "Text", "text": "Select a movie to get started"},
        {"id": "movies-col", "component": "Column", "children": [f"movie-card-{mid}" for mid in movie_ids]},
    ]
    for m in movies:
        mid = m["id"]
        genre    = m.get("genre", "")
        duration = m.get("duration_min", "")
        lang     = m.get("language", "English")
        rating   = m.get("certificate", "UA")
        components += [
            {"id": f"movie-card-{mid}", "component": "Card", "child": f"movie-inner-{mid}"},
            {"id": f"movie-inner-{mid}", "component": "Column",
             "children": [f"movie-title-{mid}", f"movie-tags-{mid}", f"movie-meta-{mid}", f"movie-btn-{mid}"]},
            {"id": f"movie-title-{mid}", "component": "Text", "text": m["title"]},
            {"id": f"movie-tags-{mid}", "component": "Row",
             "children": [f"movie-genre-{mid}", f"movie-rating-{mid}"]},
            {"id": f"movie-genre-{mid}", "component": "Text", "text": genre},
            {"id": f"movie-rating-{mid}", "component": "Text", "text": rating},
            {"id": f"movie-meta-{mid}", "component": "Text",
             "text": f"⏱ {duration} min  ·  {lang}"},
            {"id": f"movie-btn-{mid}", "component": "Button",
             "child": f"movie-btn-text-{mid}",
             "action": {"event": {"name": f"movie_selected|{mid}|{m['title']}"}}},
            {"id": f"movie-btn-text-{mid}", "component": "Text", "text": "BOOK NOW"},
        ]
    return [
        {"version": "v0.9", "createSurface": {"surfaceId": surface_id, "catalogId": CATALOG_ID}},
        {"version": "v0.9", "updateComponents": {"surfaceId": surface_id, "components": components}},
    ]


def _build_showtimes_a2ui(
    movie_id: str, movie_title: str, shows: list[dict], theater_name: str
) -> list[dict]:
    surface_id = f"showtimes-{movie_id}"
    slot_btn_ids = [f"slot-btn-{s['slot']}" for s in shows]
    components: list[dict] = [
        {"id": "root", "component": "Column",
         "children": ["heading", "subheading", "theater-row", "divider-0", "slots-row"]},
        {"id": "heading",     "component": "Text", "text": movie_title},
        {"id": "subheading",  "component": "Text", "text": "Select Showtime"},
        {"id": "theater-row", "component": "Row",
         "children": ["theater-icon", "theater-name"]},
        {"id": "theater-icon","component": "Text", "text": "📍"},
        {"id": "theater-name","component": "Text", "text": theater_name},
        {"id": "divider-0",   "component": "Divider"},
        {"id": "slots-row",   "component": "Row", "children": slot_btn_ids},
    ]
    for s in shows:
        slot = s["slot"]
        time_label = s.get("time_label", SLOT_LABELS.get(slot, slot))
        action = f"slot_selected|{movie_id}|{movie_title}|{slot}"
        components += [
            {"id": f"slot-btn-{slot}", "component": "Button",
             "child": f"slot-txt-{slot}",
             "action": {"event": {"name": action}}},
            {"id": f"slot-txt-{slot}", "component": "Text", "text": time_label},
        ]
    return [
        {"version": "v0.9", "createSurface": {"surfaceId": surface_id, "catalogId": CATALOG_ID}},
        {"version": "v0.9", "updateComponents": {"surfaceId": surface_id, "components": components}},
    ]


_SEAT_EMOJI: dict[str, str] = {"S": "🪑", "P": "🛋️", "I": "🎞️"}


def _build_seat_selection_a2ui(
    movie_id: str, movie_title: str, slot: str, slot_label: str,
    seats: dict, theater_name: str,
) -> list[dict]:
    surface_id = f"seats-{movie_id}-{slot}"
    seat_btn_ids = [f"seat-btn-{code}" for code in seats]
    components: list[dict] = [
        {"id": "root", "component": "Column",
         "children": ["heading", "showtime-row", "subheading", "divider-0", "seats-row"]},
        {"id": "heading",      "component": "Text", "text": movie_title},
        {"id": "showtime-row", "component": "Row",
         "children": ["show-icon", "showtime-val"]},
        {"id": "show-icon",   "component": "Text", "text": "🕐"},
        {"id": "showtime-val","component": "Text", "text": slot_label},
        {"id": "subheading",  "component": "Text", "text": "Choose Seat Category"},
        {"id": "divider-0",   "component": "Divider"},
        {"id": "seats-row",   "component": "Row", "children": seat_btn_ids},
    ]
    for code, info in seats.items():
        label = info.get("label", code)
        price = info.get("price_cents", 0) / 100
        emoji = _SEAT_EMOJI.get(code.upper(), "💺")
        action = f"seat_selected|{movie_id}|{movie_title}|{slot}|{code}"
        components += [
            {"id": f"seat-btn-{code}", "component": "Button",
             "child": f"seat-txt-{code}",
             "action": {"event": {"name": action}}},
            {"id": f"seat-txt-{code}", "component": "Text",
             "text": f"{emoji} {label}\n${price:.0f} / ticket"},
        ]
    return [
        {"version": "v0.9", "createSurface": {"surfaceId": surface_id, "catalogId": CATALOG_ID}},
        {"version": "v0.9", "updateComponents": {"surfaceId": surface_id, "components": components}},
    ]


def _build_qty_selection_a2ui(
    movie_id: str, theater_id: str, movie_title: str,
    slot: str, slot_label: str,
    seat_code: str, seat_label: str, price_per_seat_cents: int,
) -> list[dict]:
    surface_id = f"qty-{movie_id}-{slot}-{seat_code}"
    qty_wrapper_ids = [f"qty-wrapper-{q}" for q in range(1, 7)]
    emoji = _SEAT_EMOJI.get(seat_code.upper(), "💺")
    components: list[dict] = [
        {"id": "root", "component": "Column",
         "children": ["heading", "meta-row", "subheading", "divider-0", "qty-row"]},
        {"id": "heading",  "component": "Text", "text": movie_title},
        {"id": "meta-row", "component": "Row",
         "children": ["slot-chip", "seat-chip"]},
        {"id": "slot-chip","component": "Text", "text": f"🕐 {slot_label}"},
        {"id": "seat-chip","component": "Text", "text": f"{emoji} {seat_label}"},
        {"id": "subheading","component": "Text", "text": "How many tickets?"},
        {"id": "divider-0", "component": "Divider"},
        {"id": "qty-row",   "component": "Row", "children": qty_wrapper_ids},
    ]
    for q in range(1, 7):
        total  = price_per_seat_cents * q / 100
        action = f"booking_confirmed|{movie_id}|{theater_id}|{movie_title}|{slot}|{seat_code}|{q}"
        components += [
            {"id": f"qty-wrapper-{q}", "component": "Column",
             "children": [f"qty-btn-{q}", f"qty-price-{q}"]},
            {"id": f"qty-btn-{q}", "component": "Button",
             "child": f"qty-num-{q}",
             "action": {"event": {"name": action}}},
            {"id": f"qty-num-{q}",   "component": "Text", "text": str(q)},
            {"id": f"qty-price-{q}", "component": "Text", "text": f"${total:.0f}"},
        ]
    return [
        {"version": "v0.9", "createSurface": {"surfaceId": surface_id, "catalogId": CATALOG_ID}},
        {"version": "v0.9", "updateComponents": {"surfaceId": surface_id, "components": components}},
    ]


def _build_checkout_a2ui(checkout: dict, total_cents: int, session_id: str) -> list[dict]:
    surface_id = f"checkout-{session_id[:8]}"
    movie = checkout.get("movie", {})
    show  = checkout.get("show", {})
    seat  = checkout.get("seat", {})
    qty   = checkout.get("qty", 1)
    components = [
        {"id": "root", "component": "Column", "children": ["heading", "divider-0", "card"]},
        {"id": "heading", "component": "Text", "text": "🛒 Checkout Summary"},
        {"id": "divider-0", "component": "Divider"},
        {"id": "card", "component": "Card", "child": "card-inner"},
        {"id": "card-inner", "component": "Column",
         "children": ["movie-row", "theater-row", "show-row", "seat-row", "divider-1", "total-row"]},
        {"id": "movie-row",   "component": "Text", "text": f"🎬  {movie.get('title', '?')}"},
        {"id": "theater-row", "component": "Text", "text": f"📍  {checkout.get('theater_name', '?')}"},
        {"id": "show-row",    "component": "Text", "text": f"🕐  {show.get('time_label', show.get('time', '?'))}"},
        {"id": "seat-row",    "component": "Text", "text": f"💺  {seat.get('label', '?')} × {qty}"},
        {"id": "divider-1",   "component": "Divider"},
        {"id": "total-row",   "component": "Text", "text": f"💳  Total: ${total_cents / 100:.2f} USD"},
    ]
    return [
        {"version": "v0.9", "createSurface": {"surfaceId": surface_id, "catalogId": CATALOG_ID}},
        {"version": "v0.9", "updateComponents": {"surfaceId": surface_id, "components": components}},
    ]


def _build_verify_booking_a2ui(contents: Any, total_cents: int) -> list[dict]:
    surface_id = f"verify-{str(contents.id)[:8]}"
    components = [
        {"id": "root", "component": "Column", "children": ["heading", "divider-0", "card"]},
        {"id": "heading",  "component": "Text", "text": "✓ CartMandate Verified"},
        {"id": "divider-0", "component": "Divider"},
        {"id": "card",     "component": "Card", "child": "card-inner"},
        {"id": "card-inner", "component": "Column",
         "children": ["merchant-row", "cartid-row", "total-row", "sig-row", "expiry-row"]},
        {"id": "merchant-row", "component": "Text", "text": f"Merchant: {contents.merchant_name}"},
        {"id": "cartid-row",   "component": "Text", "text": f"Cart ID:  {contents.id}"},
        {"id": "total-row",    "component": "Text", "text": f"Total:    ${total_cents / 100:.2f} USD"},
        {"id": "sig-row",      "component": "Text", "text": "Signature: VALID"},
        {"id": "expiry-row",   "component": "Text", "text": "Expiry:    VALID"},
    ]
    return [
        {"version": "v0.9", "createSurface": {"surfaceId": surface_id, "catalogId": CATALOG_ID}},
        {"version": "v0.9", "updateComponents": {"surfaceId": surface_id, "components": components}},
    ]


def _build_authorize_payment_a2ui(checkout: dict, total_cents: int, session_id: str) -> list[dict]:
    surface_id = f"payment-{session_id[:8]}"
    movie = checkout.get("movie", {})
    show  = checkout.get("show", {})
    seat  = checkout.get("seat", {})
    qty   = checkout.get("qty", "?")
    components = [
        {"id": "root", "component": "Column",
         "children": ["heading", "divider-0", "card", "divider-1", "hint"]},
        {"id": "heading",  "component": "Text", "text": "💳 Payment Authorization Required"},
        {"id": "divider-0", "component": "Divider"},
        {"id": "card",     "component": "Card", "child": "card-inner"},
        {"id": "card-inner", "component": "Column",
         "children": ["movie-row", "show-row", "seat-row", "theater-row", "divider-2", "total-row"]},
        {"id": "movie-row",   "component": "Text", "text": f"🎬  {movie.get('title', '?')}"},
        {"id": "show-row",    "component": "Text", "text": f"🕐  {show.get('time_label', show.get('time', '?'))}"},
        {"id": "seat-row",    "component": "Text", "text": f"💺  {seat.get('label', '?')} × {qty}"},
        {"id": "theater-row", "component": "Text", "text": f"📍  {checkout.get('theater_name', '?')}"},
        {"id": "divider-2",   "component": "Divider"},
        {"id": "total-row",   "component": "Text", "text": f"💳  Total: ${total_cents / 100:.2f} USD"},
        {"id": "divider-1",  "component": "Divider"},
        {"id": "hint",       "component": "Text", "text": "Enter your PIN in the dialog below to confirm payment."},
    ]
    return [
        {"version": "v0.9", "createSurface": {"surfaceId": surface_id, "catalogId": CATALOG_ID}},
        {"version": "v0.9", "updateComponents": {"surfaceId": surface_id, "components": components}},
    ]


def _build_sign_mandates_a2ui(pmc: Any, sd_jwt_str: str, total_cents: int) -> list[dict]:
    surface_id = f"mandate-{pmc.payment_mandate_id[-8:]}"
    components = [
        {"id": "root", "component": "Column", "children": ["heading", "divider-0", "card"]},
        {"id": "heading",  "component": "Text", "text": "🖊️ AP2 PaymentMandate Signed"},
        {"id": "divider-0", "component": "Divider"},
        {"id": "card",     "component": "Card", "child": "card-inner"},
        {"id": "card-inner", "component": "Column",
         "children": ["mandateid-row", "session-row", "amount-row", "sdjwt-row"]},
        {"id": "mandateid-row", "component": "Text", "text": f"Mandate ID: {pmc.payment_mandate_id}"},
        {"id": "session-row",   "component": "Text", "text": f"Session:    {pmc.payment_details_id}"},
        {"id": "amount-row",    "component": "Text", "text": f"Amount:     ${total_cents / 100:.2f} USD"},
        {"id": "sdjwt-row",     "component": "Text", "text": f"SD-JWT:     {sd_jwt_str[:40]}…"},
    ]
    return [
        {"version": "v0.9", "createSurface": {"surfaceId": surface_id, "catalogId": CATALOG_ID}},
        {"version": "v0.9", "updateComponents": {"surfaceId": surface_id, "components": components}},
    ]


def _build_verify_mandates_a2ui(booking_id: str, session_id: str) -> list[dict]:
    ref = booking_id or session_id
    surface_id = f"verified-{ref[:8]}"
    components = [
        {"id": "root", "component": "Column", "children": ["heading", "divider-0", "card"]},
        {"id": "heading",  "component": "Text", "text": "✓ Double-Mandate Verification Passed"},
        {"id": "divider-0", "component": "Divider"},
        {"id": "card",     "component": "Card", "child": "card-inner"},
        {"id": "card-inner", "component": "Column",
         "children": ["cart-row", "payment-row", "divider-1", "booking-row"]},
        {"id": "cart-row",    "component": "Text", "text": "CartMandate (merchant JWT):  VALID"},
        {"id": "payment-row", "component": "Text", "text": "PaymentMandate (AP2 SD-JWT): VALID"},
        {"id": "divider-1",   "component": "Divider"},
        {"id": "booking-row", "component": "Text", "text": f"Booking ID: {booking_id}"},
    ]
    return [
        {"version": "v0.9", "createSurface": {"surfaceId": surface_id, "catalogId": CATALOG_ID}},
        {"version": "v0.9", "updateComponents": {"surfaceId": surface_id, "components": components}},
    ]


def _build_booking_complete_a2ui(checkout: dict, total_cents: int, booking_id: str) -> list[dict]:
    ref = booking_id or "x"
    surface_id = f"confirmed-{ref[:8]}"
    movie = checkout.get("movie", {})
    show  = checkout.get("show", {})
    seat  = checkout.get("seat", {})
    qty   = checkout.get("qty", "?")
    components = [
        {"id": "root", "component": "Column",
         "children": ["heading", "divider-0", "card", "divider-1", "enjoy"]},
        {"id": "heading",  "component": "Text", "text": "🎉 Booking Confirmed!"},
        {"id": "divider-0", "component": "Divider"},
        {"id": "card",     "component": "Card", "child": "card-inner"},
        {"id": "card-inner", "component": "Column",
         "children": ["movie-row", "theater-row", "show-row", "seat-row", "divider-2", "charged-row", "bookingid-row"]},
        {"id": "movie-row",    "component": "Text", "text": f"🎬  {movie.get('title', '?')}"},
        {"id": "theater-row",  "component": "Text", "text": f"📍  {checkout.get('theater_name', '?')}"},
        {"id": "show-row",     "component": "Text", "text": f"🕐  {show.get('time_label', '?')}"},
        {"id": "seat-row",     "component": "Text", "text": f"💺  {seat.get('label', '?')} × {qty}"},
        {"id": "divider-2",    "component": "Divider"},
        {"id": "charged-row",  "component": "Text", "text": f"💳  Charged: ${total_cents / 100:.2f} USD"},
        {"id": "bookingid-row", "component": "Text", "text": f"🎟️  Booking ID: {booking_id}"},
        {"id": "divider-1",   "component": "Divider"},
        {"id": "enjoy",       "component": "Text", "text": "Enjoy your movie! 🍿"},
    ]
    return [
        {"version": "v0.9", "createSurface": {"surfaceId": surface_id, "catalogId": CATALOG_ID}},
        {"version": "v0.9", "updateComponents": {"surfaceId": surface_id, "components": components}},
    ]


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _content(text: str) -> genai_types.Content:
    return genai_types.Content(role="model", parts=[genai_types.Part(text=text)])


def _ev(output: Any, text: str, route: str | None = None) -> Event:
    kw: dict[str, Any] = {"output": output, "content": _content(text)}
    if route:
        kw["route"] = route
    return Event(**kw)


def _verify_cart_jwt(jwt_str: str, merchant_public_jwk_dict: dict) -> bool:
    try:
        key = JWK.from_json(json.dumps(merchant_public_jwk_dict))
        JWT(key=key, jwt=jwt_str)
        return True
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — show_movies   [deterministic + HITL]
# Fetches the movie catalog and shows A2UI cards. Waits for the user to click
# a "Book Now" button, then routes "selected" with movie details.
# ══════════════════════════════════════════════════════════════════════════════

@node(rerun_on_resume=True)
async def show_movies(ctx: Context, node_input: Any):
    if "movie_selected" not in ctx.resume_inputs:
        profile = await _CLIENT.fetch_ucp_profile()
        ctx.state["merchant_public_jwk"] = profile.get("merchant_public_jwk", {})

        movies_rsp = await _CLIENT.mcp_call(
            "search_movies", {"query": "", "limit": 20, "theater_id": "pvr-001"}
        )
        movies = movies_rsp.get("result", {}).get("movies", [])
        ctx.state["movies"] = movies

        yield Event(content=_a2ui_content(_build_welcome_a2ui(movies)))
        yield RequestInput(interrupt_id="movie_selected", message="Select a movie to book")
        return

    # action format: "movie_selected|{movie_id}|{movie_title}"
    action = str(ctx.resume_inputs.get("movie_selected", "")).strip()
    parts  = action.split("|")
    if len(parts) >= 3 and parts[0] in ("movie_selected", "select_movie"):
        movie_id    = parts[1]
        movie_title = "|".join(parts[2:])
        ctx.state["movie_id"]    = movie_id
        ctx.state["movie_title"] = movie_title
        ctx.state["theater_id"]  = "pvr-001"
        yield Event(
            output={"movie_id": movie_id, "movie_title": movie_title, "theater_id": "pvr-001"},
            route="selected",
        )
    else:
        yield Event(output={}, route="cancelled")


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — show_showtimes   [deterministic + HITL]
# Fetches live showtimes for the chosen movie and shows them as buttons.
# ══════════════════════════════════════════════════════════════════════════════

@node(rerun_on_resume=True)
async def show_showtimes(ctx: Context, node_input: Any):
    if "slot_selected" not in ctx.resume_inputs:
        movie_id    = node_input.get("movie_id", ctx.state.get("movie_id", ""))
        movie_title = node_input.get("movie_title", ctx.state.get("movie_title", ""))
        theater_id  = node_input.get("theater_id", "pvr-001")

        rsp  = await _CLIENT.mcp_call("get_showtimes", {
            "theater_id": theater_id, "movie_id": movie_id
        })
        data         = rsp.get("result", {})
        shows        = data.get("shows", [])
        seats        = data.get("seats", {})
        theater      = data.get("theater", {})
        theater_name = theater.get("name", theater_id)

        ctx.state["seats"]        = seats
        ctx.state["theater_name"] = theater_name

        yield Event(content=_a2ui_content(
            _build_showtimes_a2ui(movie_id, movie_title, shows, theater_name)
        ))
        yield RequestInput(interrupt_id="slot_selected", message="Select a showtime")
        return

    # action format: "slot_selected|{movie_id}|{movie_title}|{slot}"
    action = str(ctx.resume_inputs.get("slot_selected", "")).strip()
    parts  = action.split("|")
    if len(parts) >= 4 and parts[0] == "slot_selected":
        movie_id    = parts[1]
        slot        = parts[-1].upper()
        movie_title = "|".join(parts[2:-1])
        ctx.state["slot"] = slot
        yield Event(
            output={**node_input, "movie_id": movie_id, "movie_title": movie_title, "slot": slot},
            route="selected",
        )
    else:
        yield Event(output=node_input, route="cancelled")


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — show_seat_selection   [deterministic + HITL]
# Shows seat type options (Standard / Premium / IMAX) with prices.
# ══════════════════════════════════════════════════════════════════════════════

@node(rerun_on_resume=True)
async def show_seat_selection(ctx: Context, node_input: Any):
    if "seat_selected" not in ctx.resume_inputs:
        movie_id    = node_input.get("movie_id", "")
        movie_title = node_input.get("movie_title", "")
        slot        = node_input.get("slot", "")
        theater_name = ctx.state.get("theater_name", "Theater")
        seats        = ctx.state.get("seats", {})
        slot_label   = SLOT_LABELS.get(slot, slot)

        yield Event(content=_a2ui_content(
            _build_seat_selection_a2ui(movie_id, movie_title, slot, slot_label, seats, theater_name)
        ))
        yield RequestInput(interrupt_id="seat_selected", message="Select seat type")
        return

    # action format: "seat_selected|{movie_id}|{movie_title}|{slot}|{seat_code}"
    action = str(ctx.resume_inputs.get("seat_selected", "")).strip()
    parts  = action.split("|")
    if len(parts) >= 5 and parts[0] == "seat_selected":
        movie_id    = parts[1]
        seat_code   = parts[-1].upper()
        slot        = parts[-2].upper()
        movie_title = "|".join(parts[2:-2])
        ctx.state["seat_code"] = seat_code
        yield Event(
            output={**node_input, "movie_id": movie_id, "movie_title": movie_title,
                    "slot": slot, "seat_code": seat_code},
            route="selected",
        )
    else:
        yield Event(output=node_input, route="cancelled")


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — show_qty_selection   [deterministic + HITL]
# Shows 1–6 ticket quantity buttons with per-option totals.
# Routes "confirmed" to create_checkout with the full selection payload.
# ══════════════════════════════════════════════════════════════════════════════

@node(rerun_on_resume=True)
async def show_qty_selection(ctx: Context, node_input: Any):
    if "booking_confirmed" not in ctx.resume_inputs:
        movie_id    = node_input.get("movie_id", "")
        movie_title = node_input.get("movie_title", "")
        slot        = node_input.get("slot", "")
        seat_code   = node_input.get("seat_code", "S")
        theater_id  = node_input.get("theater_id", "pvr-001")
        seats       = ctx.state.get("seats", {})
        slot_label  = SLOT_LABELS.get(slot, slot)
        seat_info   = seats.get(seat_code, {})
        seat_label  = seat_info.get("label", seat_code)
        price_cents = seat_info.get("price_cents", 1200)

        yield Event(content=_a2ui_content(
            _build_qty_selection_a2ui(
                movie_id, theater_id, movie_title,
                slot, slot_label, seat_code, seat_label, price_cents,
            )
        ))
        yield RequestInput(interrupt_id="booking_confirmed", message="Select number of tickets")
        return

    # action format: "booking_confirmed|{movie_id}|{theater_id}|{movie_title}|{slot}|{seat_code}|{qty}"
    action = str(ctx.resume_inputs.get("booking_confirmed", "")).strip()
    parts  = action.split("|")
    if len(parts) >= 7 and parts[0] == "booking_confirmed":
        movie_id    = parts[1]
        theater_id  = parts[2]
        qty         = int(parts[-1])
        seat_code   = parts[-2].upper()
        slot        = parts[-3].upper()
        movie_title = "|".join(parts[3:-3])
        yield Event(
            output={
                "movie_id":            movie_id,
                "theater_id":          theater_id,
                "movie_title":         movie_title,
                "slot":                slot,
                "seat":                seat_code,
                "qty":                 qty,
                "merchant_public_jwk": ctx.state.get("merchant_public_jwk", {}),
            },
            route="confirmed",
        )
    else:
        yield Event(output=node_input, route="cancelled")


# ══════════════════════════════════════════════════════════════════════════════
# NODE 5 — create_checkout
# Automated: real HTTP MCP create_checkout → merchant-signed CartMandate returned
# ══════════════════════════════════════════════════════════════════════════════

async def create_checkout(node_input: dict[str, Any]) -> Any:
    """Call merchant MCP create_checkout — merchant signs and returns CartMandate."""
    sel = node_input
    rsp = await _CLIENT.mcp_call(
        "create_checkout",
        {
            "movie_id":   sel["movie_id"],
            "slot":       sel["slot"],
            "seat":       sel["seat"],
            "qty":        sel["qty"],
            "theater_id": sel["theater_id"],
        },
    )
    checkout = rsp.get("result", {})

    total_cents   = checkout["total_cents"]
    session_id    = checkout["session_id"]
    expires_at    = checkout["expires_at"]

    payload = {
        "cart_mandate":        checkout["cart_mandate"],
        "checkout":            checkout,
        "total_cents":         total_cents,
        "session_id":          session_id,
        "merchant_public_jwk": sel.get("merchant_public_jwk", {}),
    }
    return Event(
        output=payload,
        state={"cart_mandate": checkout["cart_mandate"]},
        content=_a2ui_content(_build_checkout_a2ui(checkout, total_cents, session_id)),
    )


# ══════════════════════════════════════════════════════════════════════════════
# NODE 6 — verify_booking
# Automated: expiry check + merchant JWT verification
# Routes → "valid" | "invalid"
# ══════════════════════════════════════════════════════════════════════════════

async def verify_booking(node_input: dict[str, Any]) -> Any:
    cart_data = node_input["cart_mandate"]
    cart      = CartMandate(**cart_data)
    contents  = cart.contents
    issues: list[str] = []

    merchant_public_jwk = node_input.get("merchant_public_jwk", {})

    try:
        expiry = datetime.fromisoformat(contents.cart_expiry.replace("Z", "+00:00"))
        if expiry <= datetime.now(timezone.utc):
            issues.append("cart expired")
    except (ValueError, AttributeError):
        issues.append("invalid expiry format")

    if cart.merchant_authorization:
        if not _verify_cart_jwt(cart.merchant_authorization, merchant_public_jwk):
            issues.append("merchant signature invalid")
    else:
        issues.append("missing merchant_authorization")

    if not issues:
        return Event(
            output=node_input,
            route="valid",
            content=_a2ui_content(_build_verify_booking_a2ui(contents, node_input["total_cents"])),
        )

    msg = (
        f"\n{_SEP}\n"
        f"  CartMandate INVALID\n"
        f"{_SEP}\n"
        f"  Issues: {', '.join(issues)}\n"
        f"{_SEP}\n"
    )
    return Event(output=node_input, route="invalid", content=_content(msg))


# ══════════════════════════════════════════════════════════════════════════════
# NODE 7 — authorize_payment   [HUMAN-IN-THE-LOOP]
# Checks wallet balance first, then pauses for PIN (via React UI PinModal).
# Routes → "confirmed" | "cancelled"
# ══════════════════════════════════════════════════════════════════════════════

@node(rerun_on_resume=True)
async def authorize_payment(ctx: Context, node_input: dict[str, Any]):
    checkout    = node_input.get("checkout", {})
    total_cents = node_input.get("total_cents", 0)

    if "payment_auth" not in ctx.resume_inputs:
        user_id = ctx.state.get("user_id")
        if user_id:
            from app import wallet as wallet_ops
            balance = await wallet_ops.get_balance(user_id)
            if balance < total_cents:
                yield Event(
                    output=node_input,
                    route="cancelled",
                    content=_content(
                        f"  ✗ Insufficient wallet balance "
                        f"(${balance / 100:.2f} available, ${total_cents / 100:.2f} required).\n"
                        f"  Please top up your wallet and try again."
                    ),
                )
                return

        yield Event(content=_a2ui_content(
            _build_authorize_payment_a2ui(checkout, total_cents, node_input.get("session_id", ""))
        ))
        yield RequestInput(interrupt_id="payment_auth", message="Enter PIN to confirm payment")
        return

    response = str(ctx.resume_inputs.get("payment_auth", "")).lower().strip()
    if "confirm" in response:
        yield Event(
            output={**node_input, "user_id": ctx.state.get("user_id", "anonymous")},
            route="confirmed",
            content=_content(f"  ✓ Payment confirmed (${total_cents / 100:.2f}). Signing AP2 mandates…"),
        )
    else:
        yield Event(
            output=node_input,
            route="cancelled",
            content=_content(f"  ✗ Booking cancelled."),
        )


# ══════════════════════════════════════════════════════════════════════════════
# NODE 8 — sign_ap2_mandates
# Automated: PaymentMandateContents → SD-JWT using per-user key from agent DB
# ══════════════════════════════════════════════════════════════════════════════

async def sign_ap2_mandates(node_input: dict[str, Any]) -> Any:
    checkout    = node_input["checkout"]
    total_cents = node_input["total_cents"]
    session_id  = node_input["session_id"]
    user_id     = node_input.get("user_id", "anonymous")

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

    private_key = user_private_key_for(user_id)
    sd_jwt_str  = MandateClient().create(payloads=[pmc], issuer_key=private_key)

    payment_mandate = PaymentMandate(
        payment_mandate_contents=pmc,
        user_authorization=sd_jwt_str,
    )

    return Event(
        output={
            **node_input,
            "payment_mandate": payment_mandate.model_dump(),
            "payment_sd_jwt":  sd_jwt_str,
            "user_id":         user_id,
        },
        state={"payment_mandate": payment_mandate.model_dump()},
        content=_a2ui_content(_build_sign_mandates_a2ui(pmc, sd_jwt_str, total_cents)),
    )


# ══════════════════════════════════════════════════════════════════════════════
# NODE 9 — verify_mandates
# Verifies CartMandate JWT + PaymentMandate SD-JWT locally, then POST to merchant.
# Routes → "verified" | "rejected"
# ══════════════════════════════════════════════════════════════════════════════

@node(rerun_on_resume=True)
async def verify_mandates(ctx: Context, node_input: dict[str, Any]):
    if "mandate_review" not in ctx.resume_inputs:
        cart            = CartMandate(**node_input["cart_mandate"])
        sd_jwt_str      = node_input["payment_sd_jwt"]
        user_id         = node_input.get("user_id", ctx.state.get("user_id", "anonymous"))
        user_public_jwk = ctx.state.get("user_public_jwk", {})
        merchant_jwk    = ctx.state.get("merchant_public_jwk", {})
        issues: list[str] = []

        if not _verify_cart_jwt(cart.merchant_authorization or "", merchant_jwk):
            issues.append("CartMandate merchant signature invalid")

        try:
            pub_key = user_public_key_for(user_id)
            SdJwtMandate.from_sd_jwt(
                compact_serialization=sd_jwt_str,
                issuer_public_key=pub_key,
                payload_type=PaymentMandateContents,
            )
        except Exception as exc:
            issues.append(f"PaymentMandate SD-JWT: {exc}")

        if not issues:
            try:
                result = await _CLIENT.verify_mandate(
                    session_id=node_input["session_id"],
                    cart_mandate=node_input["cart_mandate"],
                    payment_sd_jwt=sd_jwt_str,
                    user_public_jwk=user_public_jwk,
                )
                if not result.get("verified"):
                    issues.append(f"Merchant rejected: {result.get('error', 'unknown')}")
                else:
                    booking_id = result.get("booking_id", "")
                    yield Event(
                        output={**node_input, "verified": True, "booking_id": booking_id, "user_id": user_id},
                        route="verified",
                        content=_a2ui_content(
                            _build_verify_mandates_a2ui(booking_id, node_input.get("session_id", ""))
                        ),
                    )
                    return
            except Exception as exc:
                issues.append(f"Merchant verification HTTP error: {exc}")

        # Pause for human review on failure
        yield Event(state={"verification_issues": issues})
        issue_lines = "\n".join(f"    • {i}" for i in issues)
        prompt = (
            f"\n{_SEP}\n"
            f"  MANDATE VERIFICATION FAILED — HUMAN REVIEW\n"
            f"{_SEP}\n"
            f"  Issues:\n{issue_lines}\n"
            f"{_SEP}\n"
            f"\n  Type 'override' to approve anyway, or 'reject' to abort.\n"
        )
        yield _content(prompt)
        yield RequestInput(interrupt_id="mandate_review", message=prompt)
        return

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
# Terminal nodes
# ══════════════════════════════════════════════════════════════════════════════

async def booking_complete_terminal(node_input: dict[str, Any]) -> Any:
    checkout    = node_input.get("checkout", {})
    total_cents = node_input.get("total_cents", 0)
    session_id  = node_input.get("session_id", "")
    booking_id  = node_input.get("booking_id", "")

    user_id = node_input.get("user_id")
    note = ""
    if user_id and total_cents:
        try:
            from app import wallet as wallet_ops
            await wallet_ops.deduct(user_id, total_cents, reason="booking", reference_id=session_id)
        except Exception as exc:
            note = f"  Wallet deduction failed: {exc}\n"

    return Event(
        output={"status": "booked", "session_id": session_id, "booking_id": booking_id},
        content=_a2ui_content(_build_booking_complete_a2ui(checkout, total_cents, booking_id)),
    )


def booking_invalid_terminal(node_input: dict[str, Any]) -> Any:
    return Event(
        output={"status": "invalid_mandate"},
        content=_content(
            f"\n{_SEP}\n  BOOKING FAILED — Invalid CartMandate\n{_SEP}\n"
            f"  The theater's CartMandate failed validation. Please try again.\n{_SEP}\n"
        ),
    )


def booking_cancelled_terminal(node_input: dict[str, Any]) -> Any:
    return Event(
        output={"status": "cancelled"},
        content=_content(
            f"\n{_SEP}\n  BOOKING CANCELLED\n{_SEP}\n"
            f"  No payment was made. Start a new chat to try again.\n{_SEP}\n"
        ),
    )


def sig_rejected_terminal(node_input: dict[str, Any]) -> Any:
    return Event(
        output={"status": "sig_rejected"},
        content=_content(
            f"\n{_SEP}\n  BOOKING ABORTED — Mandate Verification Rejected\n{_SEP}\n"
            f"  No payment was made.\n{_SEP}\n"
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# Workflow Graph
# ══════════════════════════════════════════════════════════════════════════════

root_agent = Workflow(
    name="cineagent",
    description=(
        "Fully deterministic cinema booking agent. "
        "Button-driven A2UI flow: movie → showtime → seat type → quantity, "
        "then AP2 double-mandate payment with HITL PIN gate."
    ),
    edges=[
        ("START", show_movies),

        (show_movies, {
            "selected":  show_showtimes,
            "cancelled": booking_cancelled_terminal,
        }),

        (show_showtimes, {
            "selected":  show_seat_selection,
            "cancelled": booking_cancelled_terminal,
        }),

        (show_seat_selection, {
            "selected":  show_qty_selection,
            "cancelled": booking_cancelled_terminal,
        }),

        (show_qty_selection, {
            "confirmed": create_checkout,
            "cancelled": booking_cancelled_terminal,
        }),

        (create_checkout, verify_booking),
        (verify_booking, {
            "valid":   authorize_payment,
            "invalid": booking_invalid_terminal,
        }),

        (authorize_payment, {
            "confirmed": sign_ap2_mandates,
            "cancelled": booking_cancelled_terminal,
        }),

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
