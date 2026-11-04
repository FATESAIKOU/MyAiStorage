"""`pmo._io.read_json` 讀不起來＝`SchemaError`，以及 `pmo` 設定檔的 exit 2 路徑（#381）。"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import pytest

from pmo import _io
from pmo.errors import SchemaError

from _pmo_env import pmo_env, run_cli  # noqa: F401  (fixture)


def test_a_missing_file_is_a_schema_error(tmp_path):
    """不存在也是「讀不起來」：呼叫端不必再分辨 OSError 的種類。"""
    with pytest.raises(SchemaError) as caught:
        _io.read_json(tmp_path / "nowhere.json")
    assert "cannot be read" in str(caught.value)
    assert "No such file" in str(caught.value)


def test_a_directory_in_place_of_the_file_is_a_schema_error(tmp_path):
    path = tmp_path / "as-a-directory.json"
    path.mkdir()
    with pytest.raises(SchemaError) as caught:
        _io.read_json(path)
    assert "cannot be read" in str(caught.value)
    assert "Is a directory" in str(caught.value)


def test_a_missing_config_file_exits_2_without_a_traceback(pmo_env):
    """設計 §3.2：設定讀不起來＝`[pmo] 設定讀不起來：…`＋exit 2，與壞 JSON 同路徑。"""
    missing = pmo_env.tmp_path / "no-such-teams.json"
    pmo_env.monkeypatch.setenv("PMO_SKILL_CONFIG", str(missing))
    rc, out, err = run_cli(["inbox", "list"], pmo_env)
    assert rc == 2
    assert out == ""
    assert "[pmo] 設定讀不起來：" in err
    assert "cannot be read" in err
    assert "Traceback" not in err


def test_a_directory_as_the_config_file_exits_2(pmo_env):
    as_dir = pmo_env.tmp_path / "teams-as-a-directory"
    as_dir.mkdir()
    pmo_env.monkeypatch.setenv("PMO_SKILL_CONFIG", str(as_dir))
    rc, out, err = run_cli(["inbox", "list"], pmo_env)
    assert rc == 2
    assert out == ""
    assert "[pmo] 設定讀不起來：" in err
    assert "cannot be read" in err
    assert "Traceback" not in err
