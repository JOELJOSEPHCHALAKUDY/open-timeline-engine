#!/usr/bin/env python3
"""Generate the TCE client hook JSON for Claude Code, Codex and Cursor.

``scripts/install.sh`` calls this instead of carrying a Python literal inside a bash
heredoc (a previous inline version shipped ``{{ }}`` and silently broke a hook). The
``UserPromptSubmit`` event always carries TWO command hooks, in this order:

1. the trusted human-input capture hook (``scripts/tce_capture_input.py``)
2. the unchanged takeover-policy ``systemMessage`` echo

so the two always travel together when a per-event list is replaced on merge.

Usage::

    generate_client_hooks.py --self-heal PATH --capture-script PATH --client claude
    generate_client_hooks.py ... --merge-into .claude/settings.json
    generate_client_hooks.py --ensure-codex-config .codex/config.toml
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

HOOK_TIMEOUT_SECONDS = 10
CLIENTS = ("claude", "codex", "cursor")

TAKEOVER_ECHO_COMMAND = (
    'echo \'{"systemMessage": "[TCE-HOOK] Conditional takeover policy: scan chat history for activation and stand-down state. '
    "Call mcp__tce-executor__tce_takeover_step only on activation turns and while takeover is active (use executor session_id, "
    "for example codex/claude, with activation_mode_default=takeover). On stand-down turns, call mcp__tce-executor__tce_reset_takeover_state. "
    "While inactive, respond naturally and do not call takeover_step or check_context in normal flow. If has_directive=true, execute the "
    "objective immediately. If safety_decision=confirm_required, ask for confirmation.\"}'"
)

COMPLETION_ECHO_COMMAND = (
    'echo \'{"systemMessage": "[TCE-COMPLETION] Before ending after any mutating change, call mcp__tce-executor__tce_complete_task with '
    "the title, touched files, decision, outcome/next step, git refs, and anchors. Skip only when tce.report_execution already captured this completion.\"}'"
)

# The host capture credential and spool must never be readable by the executor. The credential
# lives ONLY in ~/.config/open-timeline-engine/host_capture.token (0600) and is never written to
# the repo .env; these rules block the obvious Read/Bash routes to it.
#
# Defense in depth, not a boundary: these are Read-tool rules only, and an executor with a shell
# running as the same OS user can read a 0600 file that user owns regardless. Real separation needs
# a dedicated OS user for the executor, or identity mode `enforce` with a server-bound claim
# (see docs/plugin-setup.md).
PERMISSION_DENY_RULES = (
    "Read(~/.config/open-timeline-engine/**)",
    "Read(~/.cache/open-timeline-engine/**)",
)

CODEX_FEATURES_HEADER = "[features]"
CODEX_HOOKS_LINE = "hooks = true"


def self_heal_command(self_heal_path: str) -> str:
    # Plain concatenation: no f-strings, so the JSON braces stay single and literal.
    return (
        'if echo "$TOOL_INPUT" | grep -q "open-timeline-engine"; then echo \'{"systemMessage": "[TCE-SELF-HEAL] You modified a TCE file. Run: bash '
        + self_heal_path
        + " then continue the current workflow. Call mcp__tce-executor__tce_takeover_step only if takeover is active in this chat; otherwise continue naturally.\"}'; "
        "else echo '{\"continue\": true}'; fi"
    )


def capture_command(capture_script: str, client: str, python: str = "python3") -> str:
    return " ".join([python, shlex.quote(str(capture_script)), "--client", client])


def build_hooks(*, self_heal: str, capture_cmd: str, client: str) -> dict[str, Any]:
    if client not in CLIENTS:
        raise ValueError(f"unsupported client: {client}")
    hooks: dict[str, Any] = {
        "UserPromptSubmit": [
            {
                "matcher": "*",
                "hooks": [
                    {"type": "command", "command": capture_cmd, "timeout": HOOK_TIMEOUT_SECONDS},
                    {"type": "command", "command": TAKEOVER_ECHO_COMMAND, "timeout": HOOK_TIMEOUT_SECONDS},
                ],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Edit|Write",
                "hooks": [{"type": "command", "command": self_heal_command(self_heal), "timeout": HOOK_TIMEOUT_SECONDS}],
            }
        ],
        "Stop": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": COMPLETION_ECHO_COMMAND, "timeout": HOOK_TIMEOUT_SECONDS}],
            }
        ],
    }
    document: dict[str, Any] = {"hooks": hooks}
    if client == "claude":
        document["permissions"] = {"deny": list(PERMISSION_DENY_RULES)}
    return document


def merge_settings(existing: dict[str, Any], generated: dict[str, Any]) -> dict[str, Any]:
    """Replace whole per-event hook lists (so both UserPromptSubmit hooks travel together); union permissions.deny."""
    merged = dict(existing)
    hooks_existing = merged.get("hooks")
    hooks: dict[str, Any] = dict(hooks_existing) if isinstance(hooks_existing, dict) else {}
    hooks.update(generated.get("hooks") or {})
    merged["hooks"] = hooks

    generated_permissions = generated.get("permissions")
    if isinstance(generated_permissions, dict):
        permissions_existing = merged.get("permissions")
        permissions: dict[str, Any] = dict(permissions_existing) if isinstance(permissions_existing, dict) else {}
        deny_existing = permissions.get("deny")
        deny: list[Any] = list(deny_existing) if isinstance(deny_existing, list) else []
        for rule in generated_permissions.get("deny") or []:
            if rule not in deny:
                deny.append(rule)
        permissions["deny"] = deny
        merged["permissions"] = permissions
    return merged


def merge_into_file(path: Path, generated: dict[str, Any]) -> dict[str, Any]:
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            loaded = {}
        if isinstance(loaded, dict):
            existing = loaded
    merged = merge_settings(existing, generated)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return merged


def ensure_codex_features(path: Path) -> bool:
    """Make sure ``[features]`` in a Codex config.toml enables hooks. Returns True when the file changed."""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines()
    header_index = next((index for index, line in enumerate(lines) if line.strip() == CODEX_FEATURES_HEADER), None)
    if header_index is None:
        addition = [CODEX_FEATURES_HEADER, CODEX_HOOKS_LINE]
        new_lines = lines + ([""] if lines and lines[-1].strip() else []) + addition
    else:
        end = len(lines)
        for index in range(header_index + 1, len(lines)):
            if lines[index].strip().startswith("["):
                end = index
                break
        table = lines[header_index + 1 : end]
        if any(line.split("=", 1)[0].strip() == "hooks" for line in table if "=" in line):
            return False
        new_lines = lines[: header_index + 1] + [CODEX_HOOKS_LINE] + lines[header_index + 1 :]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return True


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate TCE client hook JSON")
    parser.add_argument("--self-heal", default="", help="absolute path to scripts/self-heal.sh")
    parser.add_argument("--capture-cmd", default="", help="verbatim capture hook command (overrides --capture-script)")
    parser.add_argument("--capture-script", default="", help="absolute path to scripts/tce_capture_input.py (quoted for the shell)")
    parser.add_argument("--python", default="python3", help="interpreter used to run the capture script")
    parser.add_argument("--client", choices=CLIENTS, default="claude")
    parser.add_argument("--merge-into", default="", help="merge the generated document into this settings JSON file")
    parser.add_argument("--ensure-codex-config", default="", help="ensure [features] hooks = true in this Codex config.toml")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.ensure_codex_config:
        changed = ensure_codex_features(Path(args.ensure_codex_config).expanduser())
        print(json.dumps({"path": args.ensure_codex_config, "changed": changed}))
        return 0
    capture_cmd = args.capture_cmd or capture_command(args.capture_script or str(Path(__file__).resolve().with_name("tce_capture_input.py")), args.client, args.python)
    document = build_hooks(self_heal=args.self_heal, capture_cmd=capture_cmd, client=args.client)
    if args.merge_into:
        merged = merge_into_file(Path(args.merge_into).expanduser(), document)
        print(json.dumps({"path": args.merge_into, "events": sorted(merged.get("hooks", {}).keys())}))
        return 0
    print(json.dumps(document, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
