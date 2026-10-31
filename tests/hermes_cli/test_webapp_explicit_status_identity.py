"""Public Webapp status uses the same explicit-token identity as gated routes."""

import secrets

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth.middleware import gated_auth_middleware
from hermes_cli.web_routers.status import _webapp_caller_signed_out


@pytest.mark.parametrize("credential,explicit,signed_out", [
    ("valid", True, False),
    ("foreign", True, True),
    ("missing", True, True),
    ("valid", False, True),
])
def test_public_status_explicit_token_identity(monkeypatch, credential, explicit, signed_out):
    token = secrets.token_urlsafe(32)
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", token)
    monkeypatch.setattr(web_server, "_SESSION_TOKEN_IS_EXPLICIT", explicit)
    app = FastAPI()
    app.state.auth_required = True
    app.state.ui_surface = "webapp"
    app.middleware("http")(gated_auth_middleware)

    @app.get("/api/status")
    async def status(request: Request):
        return {"signed_out": await _webapp_caller_signed_out(request)}

    headers = {}
    if credential != "missing":
        headers["X-Hermes-Session-Token"] = (
            token if credential == "valid" else secrets.token_urlsafe(32)
        )
    with TestClient(app) as client:
        response = client.get("/api/status", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"signed_out": signed_out}
