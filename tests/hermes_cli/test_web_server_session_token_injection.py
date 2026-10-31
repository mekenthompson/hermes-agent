"""Desktop token adoption preserves the already-loaded dashboard app."""
from unittest.mock import patch


class TestSessionTokenInjection:
    """The desktop shell mints HERMES_DASHBOARD_SESSION_TOKEN and signs its
    /api + /api/ws calls with it. The backend must adopt that token, else every
    desktop request 401s ("gateway is offline"). A main-merge once silently
    dropped this read — this guards the contract, not a literal value.
    """

    def test_honors_injected_token(self, monkeypatch):
        import hermes_cli.web_server as ws
        import hermes_cli.web_server_session_token as st

        original_app = ws.app
        original_token = ws._SESSION_TOKEN
        monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "desktop-seeded-token-0123456789abcdef")
        assert st.resolve_session_token() == "desktop-seeded-token-0123456789abcdef"
        # No module reload: the loaded app and its adopted token are untouched.
        assert ws.app is original_app
        assert ws._SESSION_TOKEN == original_token


    def test_session_token_resolution_preserves_loaded_app_auth(self, monkeypatch):
        import hermes_cli.web_server as ws
        import hermes_cli.web_server_session_token as st
        from starlette.testclient import TestClient

        original_app = ws.app
        original_header_name = ws._SESSION_HEADER_NAME
        original_token = ws._SESSION_TOKEN
        monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "desktop-seeded-token-0123456789abcdef")
        assert st.resolve_session_token() == "desktop-seeded-token-0123456789abcdef"
        monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
        with patch.object(st.secrets, "token_urlsafe", return_value="generated-token"):
            assert st.resolve_session_token() == "generated-token"

        client = TestClient(original_app)
        client.headers[original_header_name] = original_token
        assert client.get("/api/__session_token_probe").status_code == 404
        assert ws.app is original_app
        assert ws._SESSION_HEADER_NAME == original_header_name
        assert ws._SESSION_TOKEN == original_token
