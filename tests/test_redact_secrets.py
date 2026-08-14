"""Unit tests for shell-tool output secret redaction (``_redact_secrets``)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.routers.sessions import _redact_secrets


def test_redacts_unquoted_assignment():
    assert _redact_secrets("API_KEY=abc123def456") == "API_KEY=[REDACTED]"


def test_redacts_quoted_double_value_preserving_quotes():
    out = _redact_secrets('export API_KEY="abc123def456"')
    assert out == 'export API_KEY="[REDACTED]"'


def test_redacts_quoted_single_value_preserving_quotes():
    out = _redact_secrets("PASSWORD='hunter2secret'")
    assert out == "PASSWORD='[REDACTED]'"


def test_redacts_json_style_key_value():
    out = _redact_secrets('{"api_key": "abc123def456"}')
    assert out == '{"api_key": "[REDACTED]"}'
    assert "abc123def456" not in out


def test_redacts_json_with_spacing_and_other_fields():
    out = _redact_secrets('{"password" : "hunter2secret", "x": 1}')
    assert out == '{"password" : "[REDACTED]", "x": 1}'


def test_redacts_yaml_style_value():
    assert _redact_secrets("api_key: abc123def456") == "api_key: [REDACTED]"


def test_redacts_colon_separated_with_spaces():
    assert _redact_secrets("auth_token: 8f6a5b4c3d2e1f") == "auth_token: [REDACTED]"


def test_redaction_is_case_insensitive():
    assert _redact_secrets("GITHUB_TOKEN=ghp_abc123def456") == "GITHUB_TOKEN=[REDACTED]"


def test_redacts_multiple_occurrences():
    out = _redact_secrets("API_KEY=abc123def456 SLACK_TOKEN=xoxb-123456789")
    assert out == "API_KEY=[REDACTED] SLACK_TOKEN=[REDACTED]"


def test_redacts_known_key_substrings():
    assert _redact_secrets("google_api_key=AIzaSy1234567890") == "google_api_key=[REDACTED]"
    assert _redact_secrets("aws_secret_access_key=AKIAIOSFODNN7EXAMPLE") == "aws_secret_access_key=[REDACTED]"
    assert _redact_secrets("openai_api_key=sk-abc123def456") == "openai_api_key=[REDACTED]"


def test_leaves_short_values_alone():
    assert _redact_secrets("password=admin") == "password=admin"


def test_leaves_plain_prose_alone():
    text = "INFO: Deployment successful, the token will rotate tomorrow"
    assert _redact_secrets(text) == text


def test_leaves_prose_after_keyword_alone():
    text = "the password is hunter2 but keep it secret"
    assert _redact_secrets(text) == text


def test_leaves_multiline_logs_without_secrets_alone():
    log = "2026-08-14 10:00:01 INFO  starting agent\n2026-08-14 10:00:02 INFO  done"
    assert _redact_secrets(log) == log


def test_redacts_secrets_inside_larger_stream():
    out = _redact_secrets(
        "build starting\nGITHUB_TOKEN=ghp_abc123def456\nAPI_KEY=\"zzz111222333\"\nbuild done"
    )
    assert "ghp_abc123def456" not in out
    assert "zzz111222333" not in out
    assert "build starting" in out
    assert "build done" in out
