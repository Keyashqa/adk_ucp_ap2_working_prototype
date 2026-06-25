"""Pydantic request/response models for the merchant server."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: int | str = 1
    method: str
    params: dict[str, Any] = {}


class JsonRpcResponse(BaseModel):
    jsonrpc: str = "2.0"
    id: int | str = 1
    result: Any = None
    error: dict | None = None


class MandateVerifyRequest(BaseModel):
    session_id: str
    cart_mandate: dict[str, Any]
    payment_sd_jwt: str
    user_public_jwk: dict[str, Any]


class MandateVerifyResponse(BaseModel):
    verified: bool
    booking_id: str | None = None
    error: str | None = None
