"""Kalshi API-key request signing (REST and the WebSocket handshake).

Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP (ms), KALSHI-ACCESS-SIGNATURE (base64).
Signed message: f"{timestamp_ms}{METHOD}{path}". The path starts at /trade-api/...
and has no query string. The timestamp must be within 30 s of Kalshi's clock.
Key types: RSA (RSA-PSS, SHA-256, MGF1-SHA-256, salt = digest length) or Ed25519.
Source: https://docs.kalshi.com/getting_started/api_keys
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

PrivateKey = RSAPrivateKey | Ed25519PrivateKey

_PSS = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH)


class Signer:
    """Builds fresh auth headers for each request. Never logs the key or signatures."""

    def __init__(self, key_id: str, private_key: PrivateKey) -> None:
        self.key_id = key_id
        self._key = private_key

    @classmethod
    def from_pem_file(cls, key_id: str, path: str | Path) -> Signer:
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
        if not isinstance(key, RSAPrivateKey | Ed25519PrivateKey):
            raise ValueError(f"unsupported key type {type(key).__name__}; need RSA or Ed25519")
        return cls(key_id, key)

    def sign(self, message: str) -> str:
        data = message.encode()
        if isinstance(self._key, RSAPrivateKey):
            sig = self._key.sign(data, _PSS, hashes.SHA256())
        else:
            sig = self._key.sign(data)
        return base64.b64encode(sig).decode()

    def headers(self, method: str, path: str, *, now_ms: int | None = None) -> dict[str, str]:
        """Auth headers for one request. `path` is the full URL path; any query is dropped."""
        ts = str(now_ms if now_ms is not None else int(time.time() * 1000))
        bare_path = path.split("?", 1)[0]
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self.sign(f"{ts}{method.upper()}{bare_path}"),
        }
