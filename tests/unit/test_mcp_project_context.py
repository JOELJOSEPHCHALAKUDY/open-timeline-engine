from __future__ import annotations

from tce_mcp import project_context


def test_explicit_context_overrides_discovered_project(monkeypatch) -> None:
    monkeypatch.setattr(
        project_context,
        "discover_project_context",
        lambda: project_context.canonical_project_context({"project_root": "/repos/old"}),
    )

    resolved = project_context.with_project_context(
        {"project_root": "/repos/current", "domain": "coding"}
    )

    assert resolved["project"] == "current"
    assert resolved["domain"] == "coding"


def test_empty_context_uses_discovered_project(monkeypatch) -> None:
    discovered = project_context.canonical_project_context({"project_root": "/repos/current"})
    monkeypatch.setattr(project_context, "discover_project_context", lambda: discovered)

    assert project_context.with_project_context({}) == discovered
