import hashlib
import hmac

import pytest

from app.webhooks.security import compute_signature, verify_signature

SECRET = "a-test-secret"
BODY = b'{"action":"opened","number":1}'


def test_signature_matches_github_algorithm():
    expected = "sha256=" + hmac.new(SECRET.encode(), BODY, hashlib.sha256).hexdigest()
    assert compute_signature(SECRET, BODY) == expected


def test_valid_signature_accepted():
    assert verify_signature(SECRET, BODY, compute_signature(SECRET, BODY))


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "sha1=abcdef",
        "sha256=deadbeef",
        hashlib.sha256(BODY).hexdigest(),  # correct digest, missing prefix
    ],
)
def test_bad_signatures_rejected(header):
    assert not verify_signature(SECRET, BODY, header)


def test_body_mutation_breaks_signature():
    """Guards the 'never re-serialise the payload' rule."""
    signature = compute_signature(SECRET, BODY)
    reserialised = b'{"action": "opened", "number": 1}'
    assert not verify_signature(SECRET, reserialised, signature)


def test_wrong_secret_rejected():
    assert not verify_signature("other-secret", BODY, compute_signature(SECRET, BODY))
