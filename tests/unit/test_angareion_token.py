"""Unit tests for Angareion token reading and masking (D5, tasks 1.1-1.2)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import logging
import os
from pathlib import Path

import pytest

from angareion.config import (
    Config,
    TokenError,
    TokenMissingError,
    TokenPermissionError,
    load_token,
    mask_token,
)


def _create_token_file(path: Path, content: str = "ghp_TEST_TOKEN_0123456789abcdef", mode: int = 0o600) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def test_load_token_valid_0600(tmp_path):
    token_file = _create_token_file(tmp_path / "token", "ghp_SECRET_TOKEN_AABBCC\n", mode=0o600)
    assert load_token(token_file) == "ghp_SECRET_TOKEN_AABBCC"


def test_load_token_readonly_0400(tmp_path):
    """Read-only mount (MyLinuxPool's /run/mlp-secrets is 0400): owner-only still holds."""
    token_file = _create_token_file(tmp_path / "token", "ghp_READONLY_TOKEN\n", mode=0o400)
    assert load_token(token_file) == "ghp_READONLY_TOKEN"


def test_load_token_from_config(tmp_path):
    token_file = _create_token_file(tmp_path / "token", "ghp_CONFIG_TOKEN_123", mode=0o600)
    cfg = Config(
        channel_repo="owner-org/repo-chan",
        identity={"repo": "owner-org/team", "role": "PM"},
        token_file=token_file,
    )
    assert load_token(cfg) == "ghp_CONFIG_TOKEN_123"


def test_load_token_via_env_var(tmp_path, monkeypatch):
    token_file = _create_token_file(tmp_path / "env_token", "ghp_ENV_TOKEN_XYZ", mode=0o600)
    monkeypatch.setenv("ANGAREION_TOKEN_FILE", str(token_file))
    assert load_token() == "ghp_ENV_TOKEN_XYZ"


def test_load_token_missing_file_raises_error(tmp_path):
    missing_file = tmp_path / "nonexistent_token"
    with pytest.raises(TokenMissingError) as exc_info:
        load_token(missing_file)

    msg = str(exc_info.value)
    assert "Token file not found" in msg
    assert str(missing_file) in msg
    assert "0600" in msg


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions required for owner-only check")
@pytest.mark.parametrize("bad_mode", [0o644, 0o666, 0o777, 0o640, 0o444, 0o440])
def test_load_token_invalid_permissions_raises_error(tmp_path, bad_mode):
    token_file = _create_token_file(tmp_path / f"token_{oct(bad_mode)}", "ghp_PERM_TEST_TOKEN", mode=bad_mode)

    with pytest.raises(TokenPermissionError) as exc_info:
        load_token(token_file)

    msg = str(exc_info.value)
    assert "owner-only" in msg
    assert oct(bad_mode) in msg


def test_load_token_empty_file_raises_error(tmp_path):
    token_file = _create_token_file(tmp_path / "empty_token", "   \n\t  ", mode=0o600)
    with pytest.raises(TokenError) as exc_info:
        load_token(token_file)
    assert "Token file is empty" in str(exc_info.value)


def test_token_never_leaks_in_output_exceptions_or_logs(tmp_path, capsys, caplog):
    secret_value = "ghp_ULTRA_SECRET_TOKEN_DO_NOT_LEAK_999888777"
    token_file = _create_token_file(tmp_path / "leaky_token", secret_value, mode=0o644)

    logger = logging.getLogger("angareion.test")

    with caplog.at_level(logging.DEBUG):
        try:
            load_token(token_file)
        except TokenError as e:
            logger.error("Failed to load token: %s", str(e))
            masked = mask_token(f"Header: Authorization: Bearer {secret_value}", secret_value)
            assert secret_value not in masked
            assert "***" in masked

    captured = capsys.readouterr()
    assert secret_value not in captured.out
    assert secret_value not in captured.err
    assert secret_value not in caplog.text


def test_mask_token_helper():
    secret = "ghp_SECRET_VAL_123"
    text = f"curl -H 'Authorization: Bearer {secret}' https://api.github.com"
    assert mask_token(text, secret) == "curl -H 'Authorization: Bearer ***' https://api.github.com"
    assert mask_token("normal text without secret", secret) == "normal text without secret"
    assert mask_token("text", "") == "text"
