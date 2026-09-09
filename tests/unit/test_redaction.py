"""Redaction corpus.

`project_hint` is host-supplied and untrusted: the hook redacts it client-side, but the server does not trust hook
redaction, so the shared redactor has to catch a credential that reaches it in a repo/branch/project field.
"""

from __future__ import annotations

import json

import pytest
from tce_api.redaction import redact_payload
from tce_shared.redaction import (
    DEFAULT_ALLOWLIST_KEYS,
    SECRET_PATTERNS,
    redact_project_hint,
    redact_text,
)

GHP = "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
GLPAT = "glpat-ABCDEFGHIJ1234567890"
# Assembled from parts on purpose: a literal Slack-shaped token in the tree trips secret scanners.
SLACK = "-".join(("xoxb", "123456789012", "1234567890123", "AbCdEfGhIjKlMnOpQrStUvWx"))
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"


def test_redacts_email_and_token():
    payload = {
        "message": "email me at a@b.com",
        "token": "api_key=ABCDEF123456",
        "project": "allowed",
    }
    redacted, applied = redact_payload(payload)
    assert "REDACTED" in redacted["message"]
    assert "REDACTED" in redacted["token"]
    assert redacted["project"] == "allowed"
    assert applied


# --------------------------------------------------------------------------- credential shapes

CASES: list[tuple[str, str, str, list[str]]] = [
    (
        "https_userinfo_with_password",
        "clone https://alice:s3cr3t@github.com/acme/widgets.git",
        "s3cr3t",
        ["url_credentials"],
    ),
    (
        "https_userinfo_without_password",
        "https://alice@github.com/acme/widgets.git",
        "alice",
        ["url_credentials"],
    ),
    (
        "bare_token_netloc",
        f"https://{GHP}@github.com/acme/widgets.git",
        GHP,
        ["url_credentials"],
    ),
    (
        "scp_style_remote",
        "git@github.com:acme/widgets.git",
        "git@github.com",
        ["email"],
    ),
    ("jwt", f"Authorization: Bearer {JWT}", JWT, ["token"]),
    ("github_pat_classic", f"remote set-url origin {GHP}", GHP, ["token"]),
    (
        "github_pat_fine_grained",
        "github_pat_11ABCDEFG0aBcDeFgHiJkL_1234567890abcdefghijKLMNOP",
        "github_pat_11ABCDEFG0",
        ["token"],
    ),
    ("gitlab_pat", f"header: {GLPAT}", GLPAT, ["token"]),
    (
        "openai_key",
        "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "sk-proj-ABCDEFGHIJ",
        ["token"],
    ),
    ("aws_access_key", "AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE", ["token"]),
    ("slack_bot_token", f"slack_token={SLACK}", "xoxb-123456789012", ["token"]),
]


@pytest.mark.parametrize(("name", "raw", "secret", "expected"), CASES, ids=[case[0] for case in CASES])
def test_secret_shapes_are_redacted(name: str, raw: str, secret: str, expected: list[str]) -> None:
    redacted, applied = redact_text(raw)
    assert secret not in redacted, name
    assert "REDACTED" in redacted, name
    for pattern_name in expected:
        assert pattern_name in applied, f"{name}: expected {pattern_name} in {applied}"


def test_url_credentials_run_before_the_email_pattern() -> None:
    """Order matters: the email pattern would otherwise merely mangle 'tok@github.com' and keep the shape."""

    redacted, applied = redact_text("https://user:tok@github.com/o/r")
    assert redacted == "https://<REDACTED:URL_CREDENTIALS>github.com/o/r"
    assert "url_credentials" in applied
    assert "email" not in applied
    assert list(SECRET_PATTERNS).index("url_credentials") < list(SECRET_PATTERNS).index("email")


def test_ordinary_project_text_is_left_alone() -> None:
    redacted, applied = redact_text("branch feature/checkout-v2 on github.com/acme/widgets")
    assert redacted == "branch feature/checkout-v2 on github.com/acme/widgets"
    assert applied == []


# --------------------------------------------------------------------------- project_hint

NESTED_HINT: dict[str, object] = {
    "repo": f"https://ci-bot:{GLPAT}@gitlab.com/acme/widgets.git",
    "project": "widgets",
    "branch": "feature/pay",
    "remotes": [
        "git@github.com:acme/widgets.git",
        f"https://x-access-token:{GHP}@github.com/acme/widgets.git",
    ],
    "meta": {"origin": {"url": "https://u:p@example.com/r"}, "depth": 2, "shallow": True},
}


def test_redact_project_hint_sanitizes_nested_dicts_and_lists() -> None:
    sanitized, applied = redact_project_hint(NESTED_HINT)
    blob = json.dumps(sanitized)
    assert GLPAT not in blob
    assert GHP not in blob
    assert "ci-bot" not in blob
    assert "x-access-token" not in blob
    assert applied == sorted(set(applied))
    assert "url_credentials" in applied
    # structure and non-string values survive
    assert isinstance(sanitized["remotes"], list)
    assert len(sanitized["remotes"]) == 2
    meta = sanitized["meta"]
    assert isinstance(meta, dict)
    assert meta["depth"] == 2
    assert meta["shallow"] is True
    assert isinstance(meta["origin"], dict)
    assert "u:p@" not in json.dumps(meta["origin"])
    # clean values are untouched
    assert sanitized["project"] == "widgets"
    assert sanitized["branch"] == "feature/pay"
    # the caller's hint is not mutated
    assert NESTED_HINT["repo"] == f"https://ci-bot:{GLPAT}@gitlab.com/acme/widgets.git"


def test_redact_project_hint_does_not_honour_the_allowlist() -> None:
    """'repo', 'project' and 'branch' are exactly the keys a credential URL hides in."""

    assert {"repo", "project", "branch"} <= DEFAULT_ALLOWLIST_KEYS
    hint = {
        "repo": f"https://ci-bot:{GLPAT}@gitlab.com/acme/widgets.git",
        "project": f"deploy with {GHP}",
        "branch": "user@example.com/hotfix",
    }
    allowlisted, _ = redact_payload(dict(hint))
    assert allowlisted["repo"] == hint["repo"]  # the generic payload redactor lets it through

    sanitized, applied = redact_project_hint(hint)
    assert GLPAT not in str(sanitized["repo"])
    assert GHP not in str(sanitized["project"])
    assert "user@example.com" not in str(sanitized["branch"])
    assert {"url_credentials", "token", "email"} <= set(applied)


def test_redact_project_hint_on_a_clean_hint_reports_nothing() -> None:
    hint = {"repo": "acme/widgets", "branch": "main", "dirty": False}
    sanitized, applied = redact_project_hint(hint)
    assert sanitized == hint
    assert applied == []
