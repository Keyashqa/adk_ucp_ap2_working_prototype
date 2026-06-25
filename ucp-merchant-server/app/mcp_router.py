"""MCP JSON-RPC 2.0 endpoint for the cinema merchant."""
from __future__ import annotations

from fastapi import APIRouter

from app.catalog import get_movies, get_seat_categories, get_showtimes, get_theater
from app.checkout import create_checkout_session
from app.models import JsonRpcRequest, JsonRpcResponse

router = APIRouter()


def _search_movies(params: dict) -> dict:
    theater_id = params.get("theater_id", "pvr-001")
    query = params.get("query", "")
    limit = min(int(params.get("limit", 10)), 20)
    movies = get_movies(theater_id, query=query, limit=limit)
    # Add available_at field per movie
    for m in movies:
        m["available_at"] = [theater_id]
    return {"movies": movies, "total": len(movies)}


def _get_showtimes(params: dict) -> dict:
    theater_id = params.get("theater_id", "pvr-001")
    movie_id = params.get("movie_id", "")
    shows = get_showtimes(theater_id, movie_id)
    seats = get_seat_categories(theater_id)
    theater = get_theater(theater_id)
    return {
        "theater": theater,
        "shows": shows,
        "seats": {sc["code"]: sc for sc in seats},
    }


def _create_checkout(params: dict) -> dict:
    theater_id = params.get("theater_id", "pvr-001")
    movie_id = params.get("movie_id", "")
    slot = params.get("slot", "C")
    seat_code = params.get("seat", "S")
    qty = int(params.get("qty", 1))
    return create_checkout_session(theater_id, movie_id, slot, seat_code, qty)


_HANDLERS = {
    "search_movies":   _search_movies,
    "get_showtimes":   _get_showtimes,
    "create_checkout": _create_checkout,
}


@router.post("/mcp", response_model_exclude_none=True)
def mcp_dispatch(req: JsonRpcRequest) -> JsonRpcResponse:
    handler = _HANDLERS.get(req.method)
    if handler is None:
        return JsonRpcResponse(
            id=req.id,
            error={"code": -32601, "message": f"Method not found: {req.method}"},
        )
    try:
        result = handler(req.params)
        return JsonRpcResponse(id=req.id, result=result)
    except Exception as exc:  # noqa: BLE001
        return JsonRpcResponse(
            id=req.id,
            error={"code": -32000, "message": str(exc)},
        )
