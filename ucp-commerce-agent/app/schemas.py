"""Pydantic schemas for UCP profiles, CartMandate, and PaymentMandate."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class UcpCapability(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class UcpService(BaseModel):
    type: str  # "mcp" | "rest" | "a2a"
    endpoint: str
    version: str = "1.0"


class UcpProfile(BaseModel):
    merchant_id: str
    merchant_name: str
    capabilities: list[UcpCapability]
    services: list[UcpService]
    extensions: dict[str, Any] = Field(default_factory=dict)
    public_key: str
    mcp_endpoint: str | None = None


class CartItem(BaseModel):
    product_id: str
    name: str
    quantity: int
    unit_price: float
    currency: str = "USD"


class CartMandate(BaseModel):
    mandate_id: str
    merchant_id: str
    merchant_name: str
    items: list[CartItem]
    total: float
    currency: str = "USD"
    price_locked_until: str
    merchant_signature: str


class PaymentMandate(BaseModel):
    mandate_id: str
    cart_mandate_id: str
    user_id: str
    authorization_token: str  # SD-JWT
    user_signature: str
    timestamp: str


class DualMandatePayload(BaseModel):
    cart_mandate: CartMandate
    payment_mandate: PaymentMandate


class VerificationResult(BaseModel):
    cart_signature_valid: bool
    payment_signature_valid: bool
    both_valid: bool
    details: str = ""
