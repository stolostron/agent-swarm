"""Unit tests for shell-tool output secret redaction (``_redact_secrets``).

Shell commands may write credentials to stdout/stderr — e.g. a script that
runs ``printenv``, ``env``, or echoes a config variable.  ``_redact_secrets``
scrubs known credential patterns before shell output is persisted to the DB
(``last_output`` / ``raw_output`` columns).  These tests verify:

* Positive cases: credential assignments are replaced with ``[REDACTED]``
  regardless of quoting style (unquoted, double-quoted, single-quoted),
  key casing, JSON/YAML structure, or value length (including short values
  like ``password=abc`` that would previously slip through a length floor).
* Email addresses: PII key names followed by an ``addr@domain`` value are
  also redacted via a dedicated ``_EMAIL_RE`` pattern.
* Negative cases: plain prose, logs, and text without ``key=value`` credential
  assignments are left untouched to avoid corrupting legitimate output.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.routers.sessions import _redact_secrets


def test_redacts_unquoted_assignment():
    assert _redact_secrets("API_KEY=test_value_abcdef") == "API_KEY=[REDACTED]"


def test_redacts_quoted_double_value_preserving_quotes():
    out = _redact_secrets('export API_KEY="test_value_abcdef"')
    assert out == 'export API_KEY="[REDACTED]"'


def test_redacts_quoted_single_value_preserving_quotes():
    out = _redact_secrets("PASSWORD='test_pass_abcdef'")
    assert out == "PASSWORD='[REDACTED]'"


def test_redacts_json_style_key_value():
    out = _redact_secrets('{"api_key": "test_value_abcdef"}')
    assert out == '{"api_key": "[REDACTED]"}'
    assert "test_value_abcdef" not in out


def test_redacts_json_with_spacing_and_other_fields():
    out = _redact_secrets('{"password" : "test_pass_abcdef", "x": 1}')
    assert out == '{"password" : "[REDACTED]", "x": 1}'


def test_redacts_yaml_style_value():
    assert _redact_secrets("api_key: test_value_abcdef") == "api_key: [REDACTED]"


def test_redacts_colon_separated_with_spaces():
    assert _redact_secrets("auth_token: test_hex_abcdef") == "auth_token: [REDACTED]"


def test_redaction_is_case_insensitive():
    assert _redact_secrets("GITHUB_TOKEN=test_ghp_abcdef12") == "GITHUB_TOKEN=[REDACTED]"


def test_redacts_multiple_occurrences():
    out = _redact_secrets("API_KEY=test_value_abcdef SLACK_TOKEN=test_slack_abcde1")
    assert out == "API_KEY=[REDACTED] SLACK_TOKEN=[REDACTED]"


def test_redacts_known_key_substrings():
    assert _redact_secrets("google_api_key=test_gcp_abcdef12") == "google_api_key=[REDACTED]"
    assert _redact_secrets("aws_secret_access_key=test_aws_abcdef123456") == "aws_secret_access_key=[REDACTED]"
    assert _redact_secrets("openai_api_key=test_oai_abcdef12") == "openai_api_key=[REDACTED]"


def test_redacts_short_credential_assignment():
    assert _redact_secrets("password=admin") == "password=[REDACTED]"
    assert _redact_secrets("PASSWORD=abc") == "PASSWORD=[REDACTED]"


def test_redacts_email_address_after_pii_key():
    assert _redact_secrets("JIRA_EMAIL=user@example.com") == "JIRA_EMAIL=[REDACTED]"
    assert _redact_secrets("email=admin@corp.internal") == "email=[REDACTED]"
    assert _redact_secrets('JIRA_EMAIL="user@example.com"') == 'JIRA_EMAIL="[REDACTED]"'
    assert _redact_secrets('user_email=me@example.com') == 'user_email=[REDACTED]'


def test_leaves_plain_prose_alone():
    text = "INFO: Deployment successful, the token will rotate tomorrow"
    assert _redact_secrets(text) == text


def test_leaves_prose_after_keyword_alone():
    text = "the password is test_val but keep it secret"
    assert _redact_secrets(text) == text


def test_leaves_multiline_logs_without_secrets_alone():
    log = "2026-08-14 10:00:01 INFO  starting agent\n2026-08-14 10:00:02 INFO  done"
    assert _redact_secrets(log) == log


def test_redacts_secrets_inside_larger_stream():
    out = _redact_secrets(
        "build starting\nGITHUB_TOKEN=test_ghp_abcdef12\nAPI_KEY=\"test_val_abcdef\"\nbuild done"
    )
    assert "test_ghp_abcdef12" not in out
    assert "test_val_abcdef" not in out
    assert "build starting" in out
    assert "build done" in out


def test_redacts_injected_literal_secret_values():
    secrets = ["ghp_1234567890abcdef", "secret_api_val_789"]
    out = _redact_secrets(
        "The bare token is ghp_1234567890abcdef in plain text",
        secret_values=secrets,
    )
    assert out == "The bare token is [REDACTED] in plain text"
    assert "ghp_1234567890abcdef" not in out


def test_redacts_injected_secrets_sorted_by_length():
    secrets = ["secret", "secret_longer_val"]
    out = _redact_secrets("Found secret_longer_val here", secret_values=secrets)
    assert out == "Found [REDACTED] here"

