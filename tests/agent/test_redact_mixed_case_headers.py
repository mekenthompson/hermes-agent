"""Mixed-case Authorization headers are masked on every path (#108807)."""

import logging

import pytest

from agent.redact import RedactingFormatter, redact_sensitive_text


@pytest.fixture(autouse=True)
def _ensure_redaction_enabled(monkeypatch):
    monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)


def test_mixed_case_authorization_header_masked():
    # Regression for #108807: the gate checked exact all-lower/all-upper
    # substring runs, so a mixed-case header name (aUtHoRiZaTiOn) skipped
    # the IGNORECASE header regex and leaked Bearer/Basic credentials.
    secret = "SyntheticOpaqueCredential92837465"
    for name in ("aUtHoRiZaTiOn", "pRoXy-AuThOrIzAtIoN", "AUTHORIZATION", "authorization"):
        for scheme in ("Bearer", "Basic"):
            text = f"{name}: {scheme} {secret}"
            result = redact_sensitive_text(text)
            assert secret not in result, text
            assert f"{name}: {scheme}" in result, result


def test_mixed_case_header_via_logging_formatter():
    # Same failure through the real formatter path the reporter used.
    formatter = RedactingFormatter("%(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="aUtHoRiZaTiOn: Bearer SyntheticOpaqueCredential92837465",
        args=(),
        exc_info=None,
    )
    result = formatter.format(record)
    assert "SyntheticOpaqueCredential92837465" not in result
    assert "aUtHoRiZaTiOn: Bearer" in result, result
