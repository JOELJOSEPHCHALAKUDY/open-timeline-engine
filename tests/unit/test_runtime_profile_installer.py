from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "apply_runtime_profile.sh"


def _values(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )


def test_profile_application_preserves_existing_values_by_default(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TCE_CORS_ALLOW_ORIGINS=https://example.test\nCUSTOM=value\n", encoding="utf-8")

    subprocess.run(
        [str(SCRIPT), "--profile", "local-full", "--env-file", str(env_file)],
        check=True,
        cwd=ROOT,
    )

    values = _values(env_file)
    assert values["TCE_CORS_ALLOW_ORIGINS"] == "https://example.test"
    assert values["TCE_RUNTIME_PROFILE"] == "local-full"
    assert values["CUSTOM"] == "value"


def test_profile_application_overwrites_only_profile_owned_keys(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TCE_CORS_ALLOW_ORIGINS=*\nCUSTOM=value\n", encoding="utf-8")

    subprocess.run(
        [
            str(SCRIPT),
            "--profile",
            "team-secure",
            "--env-file",
            str(env_file),
            "--overwrite",
        ],
        check=True,
        cwd=ROOT,
    )

    values = _values(env_file)
    assert values["TCE_CORS_ALLOW_ORIGINS"] == ""
    assert values["TCE_IDENTITY_CLAIMS_MODE"] == "enforce"
    assert values["TCE_RUNTIME_PROFILE"] == "team-secure"
    assert values["CUSTOM"] == "value"


def test_profile_application_rejects_unknown_profile(tmp_path: Path) -> None:
    result = subprocess.run(
        [str(SCRIPT), "--profile", "../../bad", "--env-file", str(tmp_path / ".env")],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "Invalid runtime profile" in result.stderr
