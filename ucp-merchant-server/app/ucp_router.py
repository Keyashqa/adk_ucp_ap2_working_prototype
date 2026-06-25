"""UCP discovery endpoint — serves /.well-known/ucp."""
from __future__ import annotations

import json

from fastapi import APIRouter
from ucp_sdk.models.schemas import capability, payment_handler, service
from ucp_sdk.models.schemas import ucp as ucp_schema

from app.catalog import THEATERS
from app.config import MERCHANT_BASE_URL
from app.keys import merchant_public_jwk_dict

router = APIRouter()


def _build_ucp_profile(theater_id: str) -> ucp_schema.BusinessSchema:
    theater = next((t for t in THEATERS if t["id"] == theater_id), THEATERS[0])
    return ucp_schema.BusinessSchema(
        version=ucp_schema.Version("2025-01-01"),
        services={
            ucp_schema.ReverseDomainName("dev.ucp.shopping.checkout"): [
                service.BusinessSchema2(
                    root=service.BusinessSchema4(
                        version=service.Version("2025-01-01"),
                        transport="mcp",
                        endpoint=f"{MERCHANT_BASE_URL}/mcp",
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


@router.get("/.well-known/ucp")
def get_ucp_profile(theater_id: str = "pvr-001") -> dict:
    """Return the UCP BusinessSchema plus the merchant public key for agent verification."""
    profile = _build_ucp_profile(theater_id)
    return {
        "ucp": json.loads(profile.model_dump_json()),
        "merchant_public_jwk": merchant_public_jwk_dict(),
        "theaters": [
            {"id": t["id"], "name": t["name"], "location": t["location"]}
            for t in THEATERS
        ],
    }
