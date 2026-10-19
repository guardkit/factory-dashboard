"""dashboard-up.sh checks the Linear door's volume under the name compose itself will mount
(Codex round 1, R2): the shell first, then ./.env, then the default."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _has_compose() -> bool:
    if shutil.which("docker") is None or shutil.which("python3") is None:
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode == 0


pytestmark = pytest.mark.skipif(not _has_compose(), reason="needs docker compose (config only, nothing is started)")


def _resolve(tmp_path: Path, dotenv: str, exported: str | None) -> str:
    (tmp_path / "deploy").mkdir()
    shutil.copy(REPO / "deploy" / "linear-volume.sh", tmp_path / "deploy" / "linear-volume.sh")
    for name in ("docker-compose.yml", "docker-compose.linear.yml"):
        shutil.copy(REPO / name, tmp_path / name)
    (tmp_path / ".env").write_text(dotenv, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "FACTORY_LINEAR_DOOR_VOLUME"}
    env["FACTORY_DASH_CONFIG_DIR"] = str(tmp_path)
    if exported is not None:
        env["FACTORY_LINEAR_DOOR_VOLUME"] = exported
    out = subprocess.run(["bash", str(tmp_path / "deploy" / "linear-volume.sh")], capture_output=True, text=True,
                         env=env, check=True, timeout=60)
    return out.stdout.strip()


def test_the_default_name(tmp_path: Path) -> None:
    assert _resolve(tmp_path, "", None) == "forge-estate_linear-door-state"


def test_a_name_in_dotenv_is_used(tmp_path: Path) -> None:
    assert _resolve(tmp_path, "FACTORY_LINEAR_DOOR_VOLUME=door-from-dotenv\n", None) == "door-from-dotenv"


def test_an_exported_name_wins_over_dotenv(tmp_path: Path) -> None:
    assert _resolve(tmp_path, "FACTORY_LINEAR_DOOR_VOLUME=door-from-dotenv\n", "door-exported") == "door-exported"
