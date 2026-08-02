from __future__ import annotations

from tce_shared.project_context import canonical_project_context, sanitize_project_remote


def test_project_identity_is_stable_and_remote_credentials_are_removed() -> None:
    first = canonical_project_context(
        {
            "project": "open-timeline-engine",
            "project_root": "/work/open-timeline-engine",
            "project_remote": "https://user:super-secret@github.com/acme/open-timeline-engine.git",
        }
    )
    second = canonical_project_context(
        {"project_remote": "https://github.com/acme/open-timeline-engine"}
    )

    assert first["project_id"] == second["project_id"]
    assert first["project_remote"] == "https://github.com/acme/open-timeline-engine"
    assert "super-secret" not in str(first)


def test_project_context_inherits_session_binding_when_request_is_empty() -> None:
    bound = canonical_project_context(
        {"project": "ote", "project_root": "/repos/ote", "project_branch": "main"}
    )

    assert canonical_project_context({}, bound) == bound


def test_project_context_is_empty_without_identity() -> None:
    assert canonical_project_context({"domain": "coding"}) == {}
    assert sanitize_project_remote("") == ""


def test_explicit_project_rebinds_session_identity() -> None:
    prior = canonical_project_context({"project_root": "/repos/one"})
    current = canonical_project_context({"project_root": "/repos/two"}, prior)

    assert current["project"] == "two"
    assert current["project_id"] != prior["project_id"]
