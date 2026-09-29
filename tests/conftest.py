"""Test environment.

FIRESTORE_EMULATOR_HOST must be set before any Firestore client is constructed,
otherwise the library goes looking for real ADC credentials and the whole suite
depends on a gcloud login.
"""

import base64
import os

os.environ.setdefault("FIRESTORE_EMULATOR_HOST", "localhost:8081")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TEST_PRIVATE_PEM = _key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
).decode()

os.environ.setdefault("GITHUB_APP_ID", "123456")
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("GITHUB_PRIVATE_KEY", base64.b64encode(TEST_PRIVATE_PEM.encode()).decode())
os.environ.setdefault("GCP_PROJECT_ID", "test-project")
os.environ.setdefault("ENABLE_CLOUD_TASKS", "false")
