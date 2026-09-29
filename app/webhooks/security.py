"""Webhook signature verification.

Two rules that matter:
  1. HMAC the *raw* body. Re-serialising the parsed JSON changes byte order and
     whitespace, and the digest will never match.
  2. Compare with hmac.compare_digest, not ==, so the comparison is constant
     time and does not leak the expected digest via timing.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Hub-Signature-256"
_PREFIX = "sha256="


def compute_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{_PREFIX}{digest}"


def verify_signature(secret: str, body: bytes, header_value: str | None) -> bool:
    if not header_value or not header_value.startswith(_PREFIX):
        return False
    return hmac.compare_digest(compute_signature(secret, body), header_value)
