import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.github.auth import GitHubAuth


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture
def settings(keypair):
    private_pem, _ = keypair
    return Settings(
        github_app_id="123456",
        github_webhook_secret="s",
        github_private_key=private_pem,
        gcp_project_id="test-project",
    )


def test_app_jwt_is_verifiable_and_within_github_limits(settings, keypair):
    _, public_pem = keypair
    auth = GitHubAuth(settings, httpx.AsyncClient())

    token = auth.app_jwt()
    claims = jwt.decode(token, public_pem, algorithms=["RS256"], options={"verify_aud": False})

    assert claims["iss"] == "123456"
    assert claims["iat"] < time.time(), "iat must be backdated for clock skew"
    assert claims["exp"] - claims["iat"] <= 600, "GitHub rejects JWTs longer than 10 minutes"


def test_private_key_accepts_escaped_newlines(keypair):
    private_pem, public_pem = keypair
    settings = Settings(
        github_app_id="1",
        github_webhook_secret="s",
        github_private_key=private_pem.replace("\n", "\n"),
        gcp_project_id="p",
    )
    token = GitHubAuth(settings, httpx.AsyncClient()).app_jwt()
    assert jwt.decode(token, public_pem, algorithms=["RS256"])["iss"] == "1"


def test_private_key_accepts_base64(keypair):
    import base64

    private_pem, public_pem = keypair
    settings = Settings(
        github_app_id="1",
        github_webhook_secret="s",
        github_private_key=base64.b64encode(private_pem.encode()).decode(),
        gcp_project_id="p",
    )
    token = GitHubAuth(settings, httpx.AsyncClient()).app_jwt()
    assert jwt.decode(token, public_pem, algorithms=["RS256"])["iss"] == "1"
