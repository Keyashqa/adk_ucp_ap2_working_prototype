"""EC P-256 JWK key pair management for merchant and user signing."""
from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
from jwcrypto.jwk import JWK

_KEY_DIR = Path(__file__).parent / "_keys"


def _load_or_generate(name: str) -> JWK:
    priv_path = _KEY_DIR / f"{name}.json"
    if priv_path.exists():
        return JWK.from_json(priv_path.read_text(encoding="utf-8"))
    _KEY_DIR.mkdir(exist_ok=True)
    raw = ec.generate_private_key(ec.SECP256R1())
    jwk = JWK.from_pyca(raw)
    jwk_dict = json.loads(jwk.export())
    jwk_dict["kid"] = name
    jwk_dict["alg"] = "ES256"
    jwk = JWK.from_json(json.dumps(jwk_dict))
    priv_path.write_text(jwk.export(), encoding="utf-8")
    return jwk


def merchant_private_key() -> JWK:
    return _load_or_generate("merchant")


def merchant_public_key() -> JWK:
    return JWK.from_json(merchant_private_key().export_public())


def user_private_key() -> JWK:
    return _load_or_generate("user")


def user_public_key() -> JWK:
    return JWK.from_json(user_private_key().export_public())
