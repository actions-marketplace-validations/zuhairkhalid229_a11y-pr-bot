"""Application-level encryption for per-repo secrets stored in Firestore.

Firestore encrypts at rest, but anyone with datastore.viewer sees plaintext in
the console. Fernet (AES-128-CBC + HMAC) with a key from Secret Manager keeps
the Vercel bypass token opaque there. Rotate by re-encrypting; MultiFernet is
the upgrade path if that ever matters.
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode("utf-8")) if key else None

    @property
    def enabled(self) -> bool:
        return self._fernet is not None

    def encrypt(self, plaintext: str) -> str:
        if self._fernet is None:
            raise RuntimeError("APP_ENCRYPTION_KEY is not set; refusing to store a secret in the clear")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str | None:
        if self._fernet is None:
            return None
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return None

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("ascii")
