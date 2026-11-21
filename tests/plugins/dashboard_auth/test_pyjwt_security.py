"""Real crypto regressions for CVE-2026-102268 in the locked JWT dependency."""
from __future__ import annotations

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.exceptions import InvalidKeyError


@pytest.fixture(scope="module")
def rsa_public_pem():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


@pytest.mark.parametrize("mutation", ["tab-before-end", "carriage-return", "no-newlines"])
def test_hmac_rejects_loader_accepted_asymmetric_pem(rsa_public_pem, mutation):
    if mutation == "tab-before-end":
        key = rsa_public_pem.replace(b"-----END", b"\t-----END")
    elif mutation == "carriage-return":
        key = rsa_public_pem.replace(b"\n", b"\r")
    else:
        key = rsa_public_pem.replace(b"\n", b"")
    # This is asymmetric material despite its unusual formatting.
    assert isinstance(serialization.load_pem_public_key(key), rsa.RSAPublicKey)
    with pytest.raises(InvalidKeyError):
        jwt.encode({"sub": "forged-user"}, key, algorithm="HS256")


def test_legitimate_hmac_and_rsa_round_trips_remain_supported():
    secret = b"synthetic-test-secret-for-hmac-round-trip"
    token = jwt.encode({"sub": "legitimate-user"}, secret, algorithm="HS256")
    assert jwt.decode(token, secret, algorithms=["HS256"])["sub"] == "legitimate-user"
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode({"sub": "legitimate-user"}, private, algorithm="RS256")
    assert jwt.decode(token, private.public_key(), algorithms=["RS256"])["sub"] == "legitimate-user"
