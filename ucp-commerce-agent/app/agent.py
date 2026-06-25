# ruff: noqa
"""CineAgent — ADK 2.0 Workflow graph for cinema ticket booking.

Graph topology:
  START
    └─▶ conversation_agent  ← LLM agent (multi-turn, tool-calling)
          ├─[cancelled]─▶ booking_cancelled_terminal
          └─[selected]──▶ create_checkout    (automated: CartMandate via merchant HTTP)
                            └─▶ verify_booking    (automated: validate CartMandate)
                                  ├─[invalid]─▶ booking_invalid_terminal
                                  └─[valid]──▶ authorize_payment  ← HITL: PIN via UI
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
from google import genai
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.workflow import Workflow, node
from google.genai import types as genai_types
from jwcrypto.jwk import JWK
from jwcrypto.jwt import JWT

from app.config import MODEL_NAME
from app.keys import user_private_key_for, user_public_key_for
from app.merchant_client import MerchantClient

# ── Singletons ─────────────────────────────────────────────────────────────────

_SEP = "=" * 60
_CLIENT = MerchantClient()

# Lazily initialised so the API key is already set in env before first use
_genai_client: genai.Client | None = None


def _get_genai_client() -> genai.Client:
    global _genai_client
    if _genai_client is None:
        _genai_client = genai.Client()
    return _genai_client


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
# Conversation-agent tools (FunctionDeclarations for Gemini)
# ══════════════════════════════════════════════════════════════════════════════

_CINEMA_TOOLS = genai_types.Tool(function_declarations=[
    genai_types.FunctionDeclaration(
        name="search_movies",
        description=(
            "Fetch the list of available movies and theaters from the cinema. "
            "Call this first when the user asks what's playing or wants to browse."
        ),
        parameters=genai_types.Schema(
            type=genai_types.Type.OBJECT,
            properties={
                "query": genai_types.Schema(
                    type=genai_types.Type.STRING,
                    description="Optional search term; leave empty to get all movies.",
                ),
            },
        ),
    ),
    genai_types.FunctionDeclaration(
        name="get_showtimes",
        description=(
            "Get available showtimes for a specific movie at a specific theater. "
            "Use when the user has picked a movie and wants to know what times are available."
        ),
        parameters=genai_types.Schema(
            type=genai_types.Type.OBJECT,
            properties={
                "theater_id": genai_types.Schema(
                    type=genai_types.Type.STRING,
                    description="Theater ID, e.g. pvr-001 or inox-001",
                ),
                "movie_id": genai_types.Schema(
                    type=genai_types.Type.STRING,
                    description="Movie ID returned by search_movies",
                ),
            },
            required=["theater_id", "movie_id"],
        ),
    ),
    genai_types.FunctionDeclaration(
        name="confirm_booking",
        description=(
            "Call this ONLY once the user has explicitly agreed to all booking details: "
            "theater, movie, showtime slot, seat type, and number of seats. "
            "Do NOT call this unless the user has confirmed. "
            "CRITICAL: movie_id MUST be the exact 'id' field from search_movies results "
            "(e.g. 'mov-001', 'mov-002', 'mov-003'). Never invent or guess an ID."
        ),
        parameters=genai_types.Schema(
            type=genai_types.Type.OBJECT,
            properties={
                "theater_id":  genai_types.Schema(type=genai_types.Type.STRING, description="e.g. pvr-001 or inox-001"),
                "movie_id":    genai_types.Schema(type=genai_types.Type.STRING, description="Exact 'id' from search_movies — e.g. 'mov-001'. Never make up an ID."),
                "movie_title": genai_types.Schema(type=genai_types.Type.STRING, description="Movie title for display"),
                "slot":        genai_types.Schema(type=genai_types.Type.STRING, description="Slot letter: A=10:00 AM, B=2:30 PM, C=7:00 PM, D=10:15 PM"),
                "seat_code":   genai_types.Schema(type=genai_types.Type.STRING, description="S=Standard, P=Premium Recliner, I=IMAX"),
                "qty":         genai_types.Schema(type=genai_types.Type.INTEGER, description="Number of tickets (1–6)"),
            },
            required=["theater_id", "movie_id", "movie_title", "slot", "seat_code", "qty"],
        ),
    ),
    genai_types.FunctionDeclaration(
        name="cancel_booking",
        description="Call this if the user explicitly wants to stop or cancel the booking process.",
        parameters=genai_types.Schema(type=genai_types.Type.OBJECT, properties={}),
    ),
])

_SYSTEM_PROMPT = """\
You are a friendly cinema booking assistant for a multiplex ticketing app.
Help the user discover movies and book tickets through natural conversation.

Available theaters:
  • pvr-001  — PVR Cinemas, Phoenix Mall
  • inox-001 — INOX Multiplex, Orion Mall

Showtime slots:  A = 10:00 AM  |  B = 2:30 PM  |  C = 7:00 PM  |  D = 10:15 PM
Seat categories: S = Standard ($12)  |  P = Premium Recliner ($18)  |  I = IMAX ($22)

Workflow:
1. The catalog has already been fetched — it is in your conversation history as a search_movies result.
   Use those exact movie IDs. Do NOT call search_movies again unless the user asks to refresh.
2. Show the movies and ask what the user wants to watch.
3. If the user asks about times, call get_showtimes for that movie + theater.
4. Guide the user to pick: theater → movie → showtime → seat type → number of seats.
5. Summarise the selection and ask the user to confirm.
6. Once confirmed, call confirm_booking — use the EXACT movie_id from the search_movies result
   already in your history (e.g. 'mov-001', 'mov-002'). Never invent or guess an ID.
7. If the user wants to quit at any point, call cancel_booking.

CRITICAL RULE: movie_id in confirm_booking must always be the exact 'id' field
from the search_movies tool result (like 'mov-001'). Inventing an ID causes a hard error.

Keep responses concise and conversational. Show prices when relevant.
"""


# ── History serialisation (state must be JSON-safe) ───────────────────────────

def _pack_history(contents: list[genai_types.Content]) -> list[dict]:
    """Convert Content objects → plain dicts for state storage."""
    out = []
    for c in contents:
        parts = []
        for p in c.parts:
            if p.text:
                parts.append({"text": p.text})
            elif p.function_call:
                parts.append({"function_call": {
                    "name": p.function_call.name,
                    "args": dict(p.function_call.args or {}),
                }})
            elif p.function_response:
                parts.append({"function_response": {
                    "name": p.function_response.name,
                    "response": dict(p.function_response.response or {}),
                }})
        if parts:
            out.append({"role": c.role, "parts": parts})
    return out


def _unpack_history(history: list[dict]) -> list[genai_types.Content]:
    """Convert plain dicts → Content objects for the Gemini API."""
    contents = []
    for item in history:
        parts = []
        for p in item.get("parts", []):
            if "text" in p:
                parts.append(genai_types.Part(text=p["text"]))
            elif "function_call" in p:
                fc = p["function_call"]
                parts.append(genai_types.Part(
                    function_call=genai_types.FunctionCall(
                        name=fc["name"], args=fc.get("args", {})
                    )
                ))
            elif "function_response" in p:
                fr = p["function_response"]
                parts.append(genai_types.Part(
                    function_response=genai_types.FunctionResponse(
                        name=fr["name"], response=fr.get("response", {})
                    )
                ))
        if parts:
            contents.append(genai_types.Content(role=item["role"], parts=parts))
    return contents


# ══════════════════════════════════════════════════════════════════════════════
# NODE 1 — conversation_agent   [LLM + HITL multi-turn]
#
# Runs Gemini with tool-calling in a loop:
#   • text response  → show to user, issue RequestInput, pause
#   • tool call      → execute, feed result back, loop
#   • confirm_booking → route "selected" to create_checkout
#   • cancel_booking  → route "cancelled"
# ══════════════════════════════════════════════════════════════════════════════

@node(rerun_on_resume=True)
async def conversation_agent(ctx: Context, node_input: Any):
    client = _get_genai_client()

    # ── First invocation: fetch catalog + greet the user ─────────────────────
    if "chat_history" not in ctx.state:
        profile = await _CLIENT.fetch_ucp_profile()
        ctx.state["merchant_public_jwk"] = profile.get("merchant_public_jwk", {})

        # Pre-fetch the full catalog and inject it as a real search_movies tool
        # result in history.  This guarantees the LLM always has genuine movie IDs
        # (mov-001, mov-002, …) in context before the user types anything, so it
        # can never invent an ID when calling confirm_booking.
        movies_rsp = await _CLIENT.mcp_call(
            "search_movies", {"query": "", "limit": 20, "theater_id": "pvr-001"}
        )
        movies_data = movies_rsp.get("result", {})
        movies = movies_data.get("movies", [])

        # Build a human-readable list for the welcome message
        movie_lines = "\n".join(
            f"  {i+1}. {m['title']} (ID: {m['id']}) — {m['genre']}, {m['duration_min']} min"
            for i, m in enumerate(movies)
        )
        welcome = (
            "🎬 Welcome to CineAgent! Here's what's playing right now:\n\n"
            f"{movie_lines}\n\n"
            "Which movie would you like to watch? I can also show showtimes and prices."
        )

        # Bootstrap history: simulate the search_movies call that already happened.
        # Gemini conversation rule: history must start with a user turn, and a
        # model function_call must immediately follow a user turn or function_response.
        # Pattern: user(seed) → model(function_call) → user(function_response) → model(text)
        ctx.state["chat_history"] = _pack_history([
            genai_types.Content(role="user", parts=[
                genai_types.Part(text="Hello, I'd like to book a movie ticket.")
            ]),
            genai_types.Content(role="model", parts=[
                genai_types.Part(function_call=genai_types.FunctionCall(
                    name="search_movies", args={"query": "", "theater_id": "pvr-001"}
                ))
            ]),
            genai_types.Content(role="user", parts=[
                genai_types.Part(function_response=genai_types.FunctionResponse(
                    name="search_movies", response=movies_data
                ))
            ]),
            genai_types.Content(role="model", parts=[genai_types.Part(text=welcome)]),
        ])

        yield Event(content=_content(welcome))
        yield RequestInput(interrupt_id="chat_turn", message=welcome)
        return

    # ── Re-invoked after validate_selection rejected the LLM's selection ────────
    # validate_selection routes "invalid" back here with _validation_error in
    # node_input.  We inject the error into the LLM's history so it understands
    # what went wrong, run it once to produce a corrective reply, then re-pause.
    if isinstance(node_input, dict) and "_validation_error" in node_input:
        err = node_input["_validation_error"]
        history: list[dict] = list(ctx.state.get("chat_history", []))
        history.append({
            "role": "user",
            "parts": [{"text": (
                f"[System: booking validation failed — {err}. "
                "The movie_id or other details you passed to confirm_booking were wrong. "
                "Check the exact IDs from the search_movies result already in your history. "
                "Apologise briefly to the user and ask them to clarify."
            )}],
        })
        response = await client.aio.models.generate_content(
            model=MODEL_NAME,
            contents=_unpack_history(history),
            config=genai_types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                tools=[_CINEMA_TOOLS],
            ),
        )
        raw_parts = response.candidates[0].content.parts
        text = "\n".join(p.text for p in raw_parts if getattr(p, "text", None)).strip()
        if not text:
            text = (
                "Sorry, I had trouble with those booking details. "
                "Could you confirm which movie you'd like? I'll look it up properly."
            )
        history.append({"role": "model", "parts": [{"text": text}]})
        ctx.state["chat_history"] = history
        yield Event(content=_content(text))
        yield RequestInput(interrupt_id="chat_turn", message=text)
        return

    # ── Resumed: get the user's latest message ────────────────────────────────
    user_msg = str(ctx.resume_inputs.get("chat_turn", "")).strip()
    if not user_msg:
        yield RequestInput(interrupt_id="chat_turn", message="What would you like to do?")
        return

    history: list[dict] = list(ctx.state.get("chat_history", []))
    history.append({"role": "user", "parts": [{"text": user_msg}]})

    # ── Inner loop: LLM → tool call → result → LLM … until text or terminal ──
    while True:
        response = await client.aio.models.generate_content(
            model=MODEL_NAME,
            contents=_unpack_history(history),
            config=genai_types.GenerateContentConfig(
                system_instruction=_SYSTEM_PROMPT,
                tools=[_CINEMA_TOOLS],
            ),
        )

        raw_parts = response.candidates[0].content.parts
        fc_parts  = [p for p in raw_parts if getattr(p, "function_call", None)
                     and p.function_call.name]

        # ── Branch: function call ──────────────────────────────────────────────
        if fc_parts:
            fc   = fc_parts[0].function_call
            args = dict(fc.args or {})
            history.append({"role": "model", "parts": [{"function_call": {"name": fc.name, "args": args}}]})

            # Terminal: user confirmed booking
            if fc.name == "confirm_booking":
                selection = {
                    "theater_id":          args.get("theater_id", "pvr-001"),
                    "movie_id":            args.get("movie_id", ""),
                    "movie_title":         args.get("movie_title", ""),
                    "slot":                args.get("slot", "A").upper(),
                    "seat":                args.get("seat_code", "S").upper(),
                    "qty":                 max(1, min(6, int(args.get("qty", 1)))),
                    "merchant_public_jwk": ctx.state.get("merchant_public_jwk", {}),
                }
                msg = (
                    f"✓ Got it! Booking **{selection['movie_title']}**\n"
                    f"  Slot {selection['slot']} · {selection['seat']} × {selection['qty']} seat(s)\n"
                    f"  Proceeding to checkout…"
                )
                yield Event(output=selection, route="selected", content=_content(msg))
                return

            # Terminal: user cancelled
            if fc.name == "cancel_booking":
                yield Event(
                    output={},
                    route="cancelled",
                    content=_content("👋 Booking cancelled. Come back whenever you'd like to watch something!"),
                )
                return

            # Data tool: search_movies
            if fc.name == "search_movies":
                result = await _CLIENT.mcp_call("search_movies", {
                    "query":     args.get("query", ""),
                    "limit":     20,
                    "theater_id": "pvr-001",
                })
                tool_data = result.get("result", {})

            # Data tool: get_showtimes
            elif fc.name == "get_showtimes":
                result = await _CLIENT.mcp_call("get_showtimes", {
                    "movie_id":  args.get("movie_id", ""),
                    "theater_id": args.get("theater_id", "pvr-001"),
                })
                tool_data = result.get("result", {})

            else:
                tool_data = {"error": f"Unknown tool: {fc.name}"}

            history.append({
                "role": "user",
                "parts": [{"function_response": {"name": fc.name, "response": tool_data}}],
            })
            # Loop back → LLM processes tool result

        # ── Branch: text response → show to user, wait for next turn ──────────
        else:
            text = "\n".join(
                p.text for p in raw_parts if getattr(p, "text", None)
            ).strip() or "…"

            history.append({"role": "model", "parts": [{"text": text}]})
            ctx.state["chat_history"] = history

            yield Event(content=_content(text))
            yield RequestInput(interrupt_id="chat_turn", message=text)
            return


# ══════════════════════════════════════════════════════════════════════════════
# NODE 2 — validate_selection
#
# Hard gate between LLM output and the merchant API.
#
# Problem: the LLM can call confirm_booking with an invented movie_id (e.g.
# "dune-messiah-id" instead of "mov-002"). This node catches that before any
# real money or merchant state is touched.
#
# Calls get_showtimes(theater_id, movie_id) against the live merchant.
# If the movie doesn't exist at that theater, the MCP returns empty shows.
# Any field mismatch → route "invalid" back to conversation_agent with the
# exact error so the LLM can correct itself.
# All fields verified → route "valid" forward to create_checkout.
# ══════════════════════════════════════════════════════════════════════════════

async def validate_selection(node_input: dict[str, Any]) -> Any:
    theater_id = str(node_input.get("theater_id", "")).strip()
    movie_id   = str(node_input.get("movie_id",   "")).strip()
    slot       = str(node_input.get("slot",       "")).upper().strip()
    seat_code  = str(node_input.get("seat",       "")).upper().strip()
    qty        = int(node_input.get("qty", 0))

    issues: list[str] = []

    try:
        rsp = await _CLIENT.mcp_call("get_showtimes", {
            "theater_id": theater_id,
            "movie_id":   movie_id,
        })
        data  = rsp.get("result", {})
        shows = data.get("shows", [])     # list of {slot, time_label, …}
        seats = data.get("seats", {})     # {code: {label, price_cents, …}}
    except Exception as exc:
        issues.append(f"merchant unreachable: {exc}")
        shows = []
        seats = {}

    # 1. Movie must actually be showing at this theater
    if not issues and not shows:
        issues.append(
            f"movie_id '{movie_id}' is not screening at theater '{theater_id}'. "
            "Use the exact 'id' field (e.g. 'mov-001', 'mov-002') from the "
            "search_movies result already in your history."
        )

    # 2. Slot must exist in the live schedule
    valid_slots = {s["slot"] for s in shows}
    if shows and slot not in valid_slots:
        issues.append(
            f"slot '{slot}' is not available. "
            f"Valid slots: {', '.join(sorted(valid_slots))}"
        )

    # 3. Seat code must be a real category
    if seats and seat_code not in seats:
        issues.append(
            f"seat_code '{seat_code}' is not valid. "
            f"Valid codes: {', '.join(seats.keys())}"
        )

    # 4. Quantity must be sane
    if not (1 <= qty <= 6):
        issues.append(f"qty {qty} is out of range — must be 1 to 6")

    if issues:
        err = "; ".join(issues)
        return Event(
            output={**node_input, "_validation_error": err},
            route="invalid",
            content=_content(f"[validate_selection: INVALID — {err}]"),
        )

    return Event(
        output=node_input,
        route="valid",
        content=_content(
            f"[validate_selection: OK — {movie_id} @ {theater_id}, "
            f"slot {slot}, {seat_code}×{qty}]"
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — create_checkout
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
    seat_label    = checkout["seat"]["label"]
    movie_title   = checkout["movie"]["title"]
    show_time     = checkout["show"]["time_label"]
    merchant_auth = checkout["cart_mandate"].get("merchant_authorization", "")

    msg = (
        f"\n{_SEP}\n"
        f"  Checkout & CartMandate (AP2)\n"
        f"{_SEP}\n"
        f"  Session:      {session_id}\n"
        f"  Theater:      {checkout['theater_name']}\n"
        f"  Movie:        {movie_title}\n"
        f"  Showtime:     {show_time}\n"
        f"  Seats:        {seat_label} × {sel['qty']}\n"
        f"  Total:        ${total_cents / 100:.2f} USD\n"
        f"  Expires:      {expires_at}\n"
        f"  Merchant sig: {merchant_auth[:52]}…\n"
        f"{_SEP}\n"
    )
    payload = {
        "cart_mandate":        checkout["cart_mandate"],
        "checkout":            checkout,
        "total_cents":         total_cents,
        "session_id":          session_id,
        "merchant_public_jwk": sel.get("merchant_public_jwk", {}),
    }
    return Event(output=payload, state={"cart_mandate": checkout["cart_mandate"]}, content=_content(msg))


# ══════════════════════════════════════════════════════════════════════════════
# NODE 3 — verify_booking
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
        msg = (
            f"\n{_SEP}\n"
            f"  CartMandate Verified ✓\n"
            f"{_SEP}\n"
            f"  Merchant: {contents.merchant_name}\n"
            f"  Cart ID:  {contents.id}\n"
            f"  Total:    ${node_input['total_cents'] / 100:.2f} USD\n"
            f"  Sig: VALID  |  Expiry: VALID\n"
            f"{_SEP}\n"
        )
        return Event(output=node_input, route="valid", content=_content(msg))

    msg = (
        f"\n{_SEP}\n"
        f"  CartMandate INVALID\n"
        f"{_SEP}\n"
        f"  Issues: {', '.join(issues)}\n"
        f"{_SEP}\n"
    )
    return Event(output=node_input, route="invalid", content=_content(msg))


# ══════════════════════════════════════════════════════════════════════════════
# NODE 4 — authorize_payment   [HUMAN-IN-THE-LOOP]
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

        movie = checkout.get("movie", {})
        show  = checkout.get("show", {})
        seat  = checkout.get("seat", {})
        qty   = checkout.get("qty", "?")
        prompt = (
            f"\n{_SEP}\n"
            f"  PAYMENT AUTHORIZATION REQUIRED\n"
            f"{_SEP}\n"
            f"  Movie:      {movie.get('title', '?')}\n"
            f"  Showtime:   {show.get('time_label', show.get('time', '?'))}\n"
            f"  Seats:      {seat.get('label', '?')} × {qty}\n"
            f"  Theater:    {checkout.get('theater_name', '?')}\n"
            f"  ──────────────────────────────────────\n"
            f"  TOTAL:      ${total_cents / 100:.2f} USD\n"
            f"{_SEP}\n"
            f"\n  Enter your PIN to confirm payment.\n"
        )
        yield _content(prompt)
        yield RequestInput(interrupt_id="payment_auth", message=prompt)
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
# NODE 5 — sign_ap2_mandates
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

    msg = (
        f"\n{_SEP}\n"
        f"  AP2 PaymentMandate Signed (SD-JWT)\n"
        f"{_SEP}\n"
        f"  Mandate ID: {pmc.payment_mandate_id}\n"
        f"  Session:    {session_id}\n"
        f"  Amount:     ${total_cents / 100:.2f} USD\n"
        f"  SD-JWT:     {sd_jwt_str[:60]}…\n"
        f"{_SEP}\n"
    )
    return Event(
        output={
            **node_input,
            "payment_mandate": payment_mandate.model_dump(),
            "payment_sd_jwt":  sd_jwt_str,
            "user_id":         user_id,
        },
        state={"payment_mandate": payment_mandate.model_dump()},
        content=_content(msg),
    )


# ══════════════════════════════════════════════════════════════════════════════
# NODE 6 — verify_mandates
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
                    msg = (
                        f"\n{_SEP}\n"
                        f"  Double-Mandate Verification PASSED ✓\n"
                        f"{_SEP}\n"
                        f"  CartMandate   (merchant JWT):  VALID\n"
                        f"  PaymentMandate (AP2 SD-JWT):   VALID\n"
                        f"  Merchant confirmed booking ID: {booking_id}\n"
                        f"{_SEP}\n"
                    )
                    yield Event(
                        output={**node_input, "verified": True, "booking_id": booking_id, "user_id": user_id},
                        route="verified",
                        content=_content(msg),
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
    note        = "  Note:     Human override applied\n" if node_input.get("override") else ""

    user_id = node_input.get("user_id")
    if user_id and total_cents:
        try:
            from app import wallet as wallet_ops
            await wallet_ops.deduct(user_id, total_cents, reason="booking", reference_id=session_id)
        except Exception as exc:
            note += f"  Wallet deduction failed: {exc}\n"

    msg = (
        f"\n{_SEP}\n"
        f"  🎉 BOOKING CONFIRMED!\n"
        f"{_SEP}\n"
        f"  Movie:      {checkout.get('movie', {}).get('title', '?')}\n"
        f"  Theater:    {checkout.get('theater_name', '?')}\n"
        f"  Showtime:   {checkout.get('show', {}).get('time_label', '?')}\n"
        f"  Seats:      {checkout.get('seat', {}).get('label', '?')} × {checkout.get('qty', '?')}\n"
        f"  Charged:    ${total_cents / 100:.2f} USD\n"
        f"  Booking ID: {booking_id}\n"
        f"{note}"
        f"\n  Enjoy your movie! 🍿\n"
        f"{_SEP}\n"
    )
    return Event(
        output={"status": "booked", "session_id": session_id, "booking_id": booking_id},
        content=_content(msg),
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
        "Conversational cinema booking agent. Uses an LLM with tools for discovery, "
        "then a deterministic AP2 mandate-based payment flow with HITL PIN gate."
    ),
    edges=[
        ("START", conversation_agent),

        (conversation_agent, {
            "selected":  validate_selection,      # LLM output goes here first …
            "cancelled": booking_cancelled_terminal,
        }),

        (validate_selection, {
            "valid":   create_checkout,           # … only reaches checkout if all fields verified
            "invalid": conversation_agent,        # LLM must correct itself and try again
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
