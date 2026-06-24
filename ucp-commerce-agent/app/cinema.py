"""Cinema simulation: theater UCP profiles, movie catalog, MCP JSON-RPC dispatcher."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ucp_sdk.models.schemas import capability, payment_handler, service
from ucp_sdk.models.schemas import ucp as ucp_schema


# ── Theater registry ───────────────────────────────────────────────────────

THEATER_META: dict[str, dict[str, Any]] = {
    "pvr-001": {
        "name": "PVR Cinemas",
        "location": "Phoenix Mall, 2nd Floor",
        "mcp_endpoint": "http://localhost:9001/mcp",
    },
    "inox-001": {
        "name": "INOX Multiplex",
        "location": "Orion Mall, 3rd Floor",
        "mcp_endpoint": "http://localhost:9002/mcp",
    },
}


def build_ucp_profile(theater_id: str) -> ucp_schema.BusinessSchema:
    """Build a real UCP BusinessSchema for the given theater."""
    meta = THEATER_META[theater_id]
    return ucp_schema.BusinessSchema(
        version=ucp_schema.Version("2025-01-01"),
        services={
            ucp_schema.ReverseDomainName("dev.ucp.shopping.checkout"): [
                service.BusinessSchema2(
                    root=service.BusinessSchema4(
                        version=service.Version("2025-01-01"),
                        transport="mcp",
                        endpoint=meta["mcp_endpoint"],  # type: ignore[arg-type]
                    )
                )
            ]
        },
        capabilities={
            ucp_schema.ReverseDomainName("dev.ucp.shopping"): [
                capability.BusinessSchema(
                    version=capability.Version("2025-01-01"),
                    config={"ticket_booking": True, "ap2_payments": True},
                )
            ]
        },
        payment_handlers={
            ucp_schema.ReverseDomainName("com.ap2.checkout"): [
                payment_handler.BusinessSchema(
                    version=payment_handler.Version("2025-01-01"),
                    id="com.ap2.checkout",
                )
            ]
        },
    )


# ── Movie catalog ──────────────────────────────────────────────────────────

MOVIES: list[dict[str, Any]] = [
    {
        "movie_id": "mov-001", "title": "Interstellar Redux",
        "genre": "Sci-Fi", "duration_min": 169,
        "rating": "U/A", "language": "English",
        "theaters": ["pvr-001", "inox-001"],
    },
    {
        "movie_id": "mov-002", "title": "Dune: Messiah",
        "genre": "Sci-Fi/Epic", "duration_min": 155,
        "rating": "U/A", "language": "English",
        "theaters": ["pvr-001", "inox-001"],
    },
    {
        "movie_id": "mov-003", "title": "Avengers: Kang Dynasty",
        "genre": "Action", "duration_min": 148,
        "rating": "U/A", "language": "English",
        "theaters": ["pvr-001"],
    },
    {
        "movie_id": "mov-004", "title": "Leo: The Beginning",
        "genre": "Action/Thriller", "duration_min": 162,
        "rating": "U/A", "language": "Tamil/Hindi",
        "theaters": ["inox-001"],
    },
]

SHOWS: list[dict[str, Any]] = [
    {"slot": "A", "time": "10:00 AM", "seats_left": 85},
    {"slot": "B", "time": "2:30 PM",  "seats_left": 60},
    {"slot": "C", "time": "7:00 PM",  "seats_left": 35},
    {"slot": "D", "time": "10:15 PM", "seats_left": 92},
]

SEAT_CATS: dict[str, dict[str, Any]] = {
    "S": {"label": "Standard",         "price_cents": 1200},
    "P": {"label": "Premium Recliner", "price_cents": 1800},
    "I": {"label": "IMAX",             "price_cents": 2200},
}


# ── MCP JSON-RPC dispatcher ────────────────────────────────────────────────

def call_mcp(
    method: str,
    params: dict[str, Any],
    theater_id: str = "pvr-001",
) -> dict[str, Any]:
    """Simulate a JSON-RPC 2.0 call to a theater's MCP endpoint."""
    handlers = {
        "search_movies":   _search_movies,
        "get_showtimes":   _get_showtimes,
        "create_checkout": _create_checkout,
    }
    if method not in handlers:
        return {"jsonrpc": "2.0", "error": {"code": -32601, "message": f"Unknown method: {method}"}}
    return {"jsonrpc": "2.0", "result": handlers[method](params, theater_id)}


def _search_movies(params: dict[str, Any], theater_id: str) -> dict[str, Any]:
    query = params.get("query", "").lower()
    movies = [m for m in MOVIES if theater_id in m["theaters"]]
    if query:
        movies = [m for m in movies
                  if query in m["title"].lower() or query in m["genre"].lower()]
    return {"movies": movies[: params.get("limit", 10)], "total": len(movies)}


def _get_showtimes(params: dict[str, Any], theater_id: str) -> dict[str, Any]:
    movie_id = params.get("movie_id")
    movie = next((m for m in MOVIES if m["movie_id"] == movie_id), None)
    if not movie or theater_id not in movie["theaters"]:
        return {"error": f"Movie {movie_id} not available at {theater_id}"}
    return {
        "movie": movie,
        "theater": THEATER_META[theater_id],
        "shows": SHOWS,
        "seats": SEAT_CATS,
    }


def _create_checkout(params: dict[str, Any], theater_id: str) -> dict[str, Any]:
    movie_id  = params.get("movie_id", "mov-001")
    slot      = params.get("slot", "C").upper()
    seat_code = params.get("seat", "S").upper()
    qty       = max(1, int(params.get("qty", 1)))

    movie   = next((m for m in MOVIES if m["movie_id"] == movie_id), MOVIES[0])
    show    = next((s for s in SHOWS if s["slot"] == slot), SHOWS[2])
    cat     = SEAT_CATS.get(seat_code, SEAT_CATS["S"])
    theater = THEATER_META[theater_id]

    total_cents = cat["price_cents"] * qty
    expires_at  = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()

    return {
        "session_id":   f"ckout-{uuid.uuid4().hex[:10]}",
        "theater_id":   theater_id,
        "theater_name": theater["name"],
        "movie":        movie,
        "show":         show,
        "seat":         cat,
        "qty":          qty,
        "total_cents":  total_cents,
        "expires_at":   expires_at,
    }
