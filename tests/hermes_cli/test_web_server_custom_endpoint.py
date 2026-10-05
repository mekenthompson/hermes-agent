"""Submitted dashboard model endpoints survive credential assignment."""

class TestSubmittedCustomEndpointSurvivesAssignment:
    """#115661 follow-up: a bare-``custom`` main-slot pick carries the submitted endpoint as the
    current one (see ``_validated_main_model_selection``). Once the switch's credential step
    re-resolves that target, an env endpoint (``CUSTOM_BASE_URL`` / ``OPENROUTER_BASE_URL``) could
    replace what the user typed and had persisted."""

    def test_submitted_custom_endpoint_wins_over_an_env_endpoint(self, monkeypatch):
        from hermes_cli.web_server_config import _apply_main_model_assignment, _validated_main_model_selection

        monkeypatch.setenv("CUSTOM_BASE_URL", "http://127.0.0.1:9999/v1")
        monkeypatch.setattr(
            "hermes_cli.models_validate.validate_requested_model",
            lambda *a, **k: {"accepted": True, "persist": True, "recognized": True, "message": None})
        monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
        monkeypatch.setattr("hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None)

        cfg = {"model": {"provider": "openrouter", "default": "m"}}
        result = _validated_main_model_selection(
            cfg, "custom", "qwen3:8b", "https://api.anthropic.com", "submitted-key")

        assert result.base_url == "https://api.anthropic.com"
        # The wire protocol follows the endpoint that gets persisted, not the displaced env host.
        assert result.api_mode == "anthropic_messages"
        applied = _apply_main_model_assignment(cfg.get("model", {}), result, "submitted-key")
        assert applied["base_url"] == "https://api.anthropic.com"
        assert applied["api_mode"] == "anthropic_messages"
        assert applied["api_key"] == "submitted-key"
