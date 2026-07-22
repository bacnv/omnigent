"""Tests for rewriting claude-native permission_hook.json auth headers."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from omnigent.claude_native_bridge import (
    _PERMISSION_HOOK_FILE,
    read_permission_hook_config,
    refresh_permission_hook_headers,
)


@pytest.fixture
def bridge_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path)
    path = tmp_path / "bridge"
    path.mkdir()
    return path


def test_refresh_permission_hook_headers_overwrites_existing_file(bridge_dir: Path) -> None:
    """Periodic refresh replaces the bearer without dropping the server URL."""
    hook_path = bridge_dir / _PERMISSION_HOOK_FILE
    hook_path.write_text(
        json.dumps(
            {
                "ap_server_url": "http://127.0.0.1:8000",
                "ap_auth_headers": {"Authorization": "Bearer stale-token"},
                "updated_at": 1000.0,
            }
        ),
        encoding="utf-8",
    )

    refresh_permission_hook_headers(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8000",
        ap_auth_headers={"Authorization": "Bearer fresh-token"},
    )

    config = read_permission_hook_config(bridge_dir)
    assert config["ap_auth_headers"] == {"Authorization": "Bearer fresh-token"}
    assert config["ap_server_url"] == "http://127.0.0.1:8000"
    assert config["updated_at"] > 1000.0


def test_refresh_permission_hook_headers_creates_file_if_missing(bridge_dir: Path) -> None:
    refresh_permission_hook_headers(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8000",
        ap_auth_headers={"Authorization": "Bearer fresh-token"},
    )

    config = read_permission_hook_config(bridge_dir)
    assert config["ap_auth_headers"] == {"Authorization": "Bearer fresh-token"}
    assert config["ap_server_url"] == "http://127.0.0.1:8000"
    assert isinstance(config["updated_at"], float)


def test_refresh_permission_hook_headers_uses_protected_permissions(bridge_dir: Path) -> None:
    refresh_permission_hook_headers(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8000",
        ap_auth_headers={"Authorization": "Bearer fresh-token"},
    )

    mode = stat.S_IMODE((bridge_dir / _PERMISSION_HOOK_FILE).stat().st_mode)
    assert mode == 0o600
