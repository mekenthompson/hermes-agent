#!/usr/bin/env python3
"""Offline image gate for Nous JWT rejection versus provider failure.

Run with PYTHONPATH pointing at the *image's* /opt/hermes, never a checkout
mounted over it. Uses ephemeral RSA keys and a loopback JWKS server only.
No real profile configuration, credentials or Portal access is needed.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="hermes-oauth-image-") as home:
        os.environ["HERMES_HOME"] = home
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from jwt.algorithms import RSAAlgorithm

        from hermes_cli.dashboard_auth import InvalidCodeError, ProviderError
        from plugins.dashboard_auth.nous import NousDashboardAuthProvider

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
        public_key.update(kid="image-key", alg="RS256", use="sig")
        state = {"body": {"keys": [public_key]}, "status": 200}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(state["status"])
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(state["body"]).encode())

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        portal = f"http://127.0.0.1:{server.server_port}"
        now = int(time.time())
        claims = {"sub": "image-user", "iat": now, "exp": now + 300,
                  "iss": portal, "aud": "agent:image", "oauth_contract_version": 1}
        results = {}

        def provider():
            return NousDashboardAuthProvider(client_id="agent:image", portal_url=portal)

        def rejected(token, expected):
            try:
                provider()._verify_jwt(token)
            except expected:
                return True
            except Exception:
                return False
            return False

        try:
            valid = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "image-key"})
            try:
                results["valid_signature_and_claims"] = provider()._verify_jwt(valid)["sub"] == "image-user"
            except Exception:
                results["valid_signature_and_claims"] = False
            unknown = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "foreign-key"})
            missing = jwt.encode(claims, private_key, algorithm="RS256")
            results["unknown_key_rejected_as_credential"] = rejected(unknown, InvalidCodeError)
            results["missing_key_rejected_as_credential"] = rejected(missing, InvalidCodeError)
            results["opaque_bearer_rejected_as_credential"] = rejected("invalid-bearer", InvalidCodeError)
            wrong_audience = jwt.encode({**claims, "aud": "agent:other"}, private_key,
                                        algorithm="RS256", headers={"kid": "image-key"})
            results["wrong_audience_rejected"] = rejected(wrong_audience, InvalidCodeError)
            state["body"] = {"keys": "malformed"}
            results["malformed_jwks_remains_provider_fault"] = rejected(valid, ProviderError)
            state["status"] = 503
            results["jwks_outage_remains_provider_fault"] = rejected(valid, ProviderError)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
        print(json.dumps({"checks": results, "ok": all(results.values())}, sort_keys=True))
        return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
