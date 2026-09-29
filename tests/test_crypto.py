import pytest

from app.crypto import SecretBox


def test_roundtrip():
    box = SecretBox(SecretBox.generate_key())
    token = box.encrypt("vercel-bypass-123")
    assert token != "vercel-bypass-123"
    assert box.decrypt(token) == "vercel-bypass-123"


def test_wrong_key_returns_none_not_garbage():
    a, b = SecretBox(SecretBox.generate_key()), SecretBox(SecretBox.generate_key())
    assert b.decrypt(a.encrypt("x")) is None


def test_no_key_refuses_to_encrypt_but_reads_as_none():
    box = SecretBox("")
    assert box.enabled is False
    with pytest.raises(RuntimeError):
        box.encrypt("x")
    assert box.decrypt("anything") is None
