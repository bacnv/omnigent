"""Tests for claude-native permission hook auth refresh scheduling."""

from __future__ import annotations

import base64
import json

from omnigent.runner import app


def _jwt_with_exp(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).rstrip(b"=").decode()
    return f"{header}.{payload}."


def test_permission_hook_refresh_delay_uses_jwt_exp() -> None:
    token = _jwt_with_exp(1_700_001_800)

    delay = app._permission_hook_refresh_delay_s(token, now=1_700_000_000)

    assert delay == 1_500.0


def test_permission_hook_refresh_delay_never_busy_loops_for_expired_jwt() -> None:
    token = _jwt_with_exp(1_700_000_001)

    assert app._permission_hook_refresh_delay_s(token, now=1_700_000_000) == 60.0


def test_permission_hook_refresh_delay_falls_back_for_non_jwt() -> None:
    assert app._permission_hook_refresh_delay_s("opaque-token", now=1_700_000_000) == 1200.0
