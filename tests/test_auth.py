import base64

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from courtside.kalshi.auth import Signer


def test_rsa_signature_verifies_and_strips_query() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    h = Signer("kid", key).headers("get", "/trade-api/v2/portfolio/orders?limit=5", now_ms=1700)
    assert h["KALSHI-ACCESS-KEY"] == "kid"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "1700"
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
        b"1700GET/trade-api/v2/portfolio/orders",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_ed25519_signature_verifies() -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    h = Signer("kid", key).headers("GET", "/trade-api/ws/v2", now_ms=42)
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"42GET/trade-api/ws/v2"
    )
