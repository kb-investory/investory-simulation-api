import time
import unittest
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.core import auth


def _generate_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


class _FakeSigningKey:
    def __init__(self, key):
        self.key = key


class _FakeJwkClient:
    """Stands in for PyJWKClient without hitting a real JWKS endpoint."""

    def __init__(self, public_key_pem):
        self._signing_key = _FakeSigningKey(public_key_pem)

    def get_signing_key_from_jwt(self, token):
        return self._signing_key


class GetCurrentUserIdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.private_pem, cls.public_pem = _generate_keypair()
        cls.other_private_pem, _ = _generate_keypair()

    def _sign(self, private_pem=None, **claim_overrides):
        now = int(time.time())
        payload = {
            "sub": "482",
            "tokenType": "ACCESS",
            "iat": now,
            "exp": now + 3600,
        }
        payload.update(claim_overrides)
        return jwt.encode(payload, private_pem or self.private_pem, algorithm="RS256")

    @staticmethod
    def _credentials(token):
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    def _with_fake_client(self):
        return patch.object(auth, "_get_jwk_client", return_value=_FakeJwkClient(self.public_pem))

    def test_valid_access_token_returns_int_user_id(self):
        token = self._sign()
        with self._with_fake_client():
            user_id = auth.get_current_user_id(self._credentials(token))
        self.assertEqual(user_id, 482)
        self.assertIsInstance(user_id, int)

    def test_missing_authorization_header_raises_401(self):
        with self.assertRaises(HTTPException) as context:
            auth.get_current_user_id(None)
        self.assertEqual(context.exception.status_code, 401)

    def test_expired_token_raises_401(self):
        token = self._sign(exp=int(time.time()) - 10)
        with self._with_fake_client():
            with self.assertRaises(HTTPException) as context:
                auth.get_current_user_id(self._credentials(token))
        self.assertEqual(context.exception.status_code, 401)

    def test_forged_signature_raises_401(self):
        # Signed with a key the server never advertises via JWKS — must not verify.
        token = self._sign(private_pem=self.other_private_pem)
        with self._with_fake_client():
            with self.assertRaises(HTTPException) as context:
                auth.get_current_user_id(self._credentials(token))
        self.assertEqual(context.exception.status_code, 401)

    def test_refresh_token_is_rejected(self):
        token = self._sign(tokenType="REFRESH")
        with self._with_fake_client():
            with self.assertRaises(HTTPException) as context:
                auth.get_current_user_id(self._credentials(token))
        self.assertEqual(context.exception.status_code, 401)

    def test_token_without_sub_claim_raises_401(self):
        now = int(time.time())
        token = jwt.encode(
            {"tokenType": "ACCESS", "iat": now, "exp": now + 3600},
            self.private_pem,
            algorithm="RS256",
        )
        with self._with_fake_client():
            with self.assertRaises(HTTPException) as context:
                auth.get_current_user_id(self._credentials(token))
        self.assertEqual(context.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
