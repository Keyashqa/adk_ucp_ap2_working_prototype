"""Workflow integration tests — exercises each UCP/AP2 graph branch."""
from __future__ import annotations

import pytest
from google.adk.runners import InMemoryRunner
from google.adk.workflow.utils._workflow_hitl_utils import (
    create_request_input_response,
    get_request_input_interrupt_ids,
)
from google.genai import types

pytestmark = pytest.mark.asyncio(loop_scope="function")


# ── Helpers ────────────────────────────────────────────────────────────────

def _text(events) -> str:
    parts = []
    for ev in events:
        if ev.content and ev.content.parts:
            for p in ev.content.parts:
                if hasattr(p, "text") and p.text:
                    parts.append(p.text)
    return "".join(parts)


def _interrupt_id(events) -> str | None:
    for ev in events:
        ids = get_request_input_interrupt_ids(ev)
        if ids:
            return ids[0]
        if ev.long_running_tool_ids:
            return list(ev.long_running_tool_ids)[0]
    return None


async def _make_runner():
    from app.agent import app
    runner = InMemoryRunner(app=app)
    session = await runner.session_service.create_session(app_name="app", user_id="u")
    return runner, session.id


async def _turn1(runner, session_id):
    events = []
    async for ev in runner.run_async(
        user_id="u", session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part.from_text(text="buy headphones")]),
    ):
        events.append(ev)
    return events


async def _turn2(runner, session_id, interrupt_id, response_text):
    events = []
    resume_part = create_request_input_response(interrupt_id, {"result": response_text})
    async for ev in runner.run_async(
        user_id="u", session_id=session_id,
        new_message=types.Content(role="user", parts=[resume_part]),
    ):
        events.append(ev)
    return events


# ── Tests ──────────────────────────────────────────────────────────────────

async def test_nodes_1_to_3_and_hitl_gate():
    """Nodes 1-3 run automatically and workflow pauses at Node 4 HITL gate."""
    runner, sid = await _make_runner()
    events = await _turn1(runner, sid)
    text = _text(events)

    assert "NODE 1" in text and "UCP Discovery Complete" in text
    assert "NODE 2" in text and "MCP Capability Invocation" in text
    assert "NODE 3" in text and "CartMandate Verified" in text
    assert "NODE 4" in text and "PAYMENT AUTHORIZATION REQUIRED" in text
    assert _interrupt_id(events) == "payment_auth"


async def test_happy_path_approve():
    """Full happy path: user approves → Nodes 5 & 6 → PURCHASE COMPLETE."""
    runner, sid = await _make_runner()
    t1 = await _turn1(runner, sid)
    iid = _interrupt_id(t1)
    assert iid == "payment_auth"

    t2 = await _turn2(runner, sid, iid, "approve")
    text = _text(t2)

    assert "Payment authorized" in text
    assert "NODE 5" in text and "PaymentMandate Signed" in text
    assert "NODE 6" in text and "Double-Signature Verification PASSED" in text
    assert "PURCHASE COMPLETE" in text
    assert "249.97" in text


async def test_user_reject_path():
    """User declines at HITL gate → purchase cancelled, no payment made."""
    runner, sid = await _make_runner()
    t1 = await _turn1(runner, sid)
    iid = _interrupt_id(t1)

    t2 = await _turn2(runner, sid, iid, "reject")
    text = _text(t2)

    assert "PURCHASE CANCELLED" in text
    assert "PURCHASE COMPLETE" not in text


async def test_cart_mandate_crypto_round_trip():
    """sign → verify round-trip: CartMandate and PaymentMandate both verify correctly."""
    from app.mock_ucp import call_mcp, submit_and_verify_mandates
    from app.crypto import sign_payload, USER_PRIVATE_KEY, generate_sd_jwt
    import uuid
    from datetime import datetime, timezone

    cart = call_mcp("get_cart_manifest", {})["result"]

    mandate_id = f"pay-{uuid.uuid4().hex[:8]}"
    pay_signable = {
        "mandate_id": mandate_id,
        "cart_mandate_id": cart["mandate_id"],
        "user_id": "user-test",
    }
    payment = {
        **pay_signable,
        "authorization_token": generate_sd_jwt("user-test", cart["mandate_id"]),
        "user_signature": sign_payload(pay_signable, USER_PRIVATE_KEY),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    result = submit_and_verify_mandates(cart, payment)
    assert result["cart_signature_valid"] is True
    assert result["payment_signature_valid"] is True
    assert result["both_valid"] is True
    assert "successfully" in result["details"]


async def test_sd_jwt_structure():
    """SD-JWT has 3-part structure with correct mock header and required claims."""
    from app.crypto import generate_sd_jwt
    import base64, json

    token = generate_sd_jwt("user-x", "mandate-y")
    parts = token.split(".")
    assert len(parts) == 3

    header = json.loads(base64.urlsafe_b64decode(parts[0] + "=="))
    assert header["typ"] == "sd-jwt"
    assert header["alg"] == "mock-HS256"

    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=="))
    assert payload["sub"] == "user-x"
    assert payload["cart_mandate_id"] == "mandate-y"
    assert "_sd" in payload


async def test_mcp_jsonrpc_sequence():
    """All three JSON-RPC 2.0 methods return well-formed responses."""
    from app.mock_ucp import call_mcp

    search = call_mcp("search_products", {"query": "headphones", "limit": 3})
    assert "result" in search and "error" not in search
    assert len(search["result"]["products"]) >= 1

    checkout = call_mcp("create_checkout", {"product_ids": ["prod-001"]})
    assert "session_id" in checkout["result"]

    manifest = call_mcp("get_cart_manifest", {"session_id": checkout["result"]["session_id"]})
    cart = manifest["result"]
    assert "mandate_id" in cart
    assert "merchant_signature" in cart
    assert cart["total"] > 0
