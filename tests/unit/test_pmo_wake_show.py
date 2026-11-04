"""`pmo wake show`（唯讀）與「不 import tick」（#358 T5-4；design §3.6）。

從 `tests/unit/test_pmo_wake.py` 原樣搬過來（只拆檔、不改斷言；nit-3）：

- `wake show` 只列不動句子不同的 poke job（且只列 `args.id` 是 PMO 的）；
- 最早到期含看門狗，與 tick 的 `[idle] [next-due=…]` 同一個答案；沒有 team
  狀態時只算 `tick.json`；
- `wake` 與 `pmo.due` 都不 import `tick`（`pmo.due` 由子行程 import 後檢查
  `sys.modules`；`due.py` 反向 import `tick` 就轉紅，第二輪審查 warning-2）；
- `show` 是唯讀：不 poke、不刪。

全部用假 sidecar 與暫存 state（`tests/conftest.py` 的 `isolated_home`）。
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401

import json

from pmo import wake
from pmo.config import DEFAULT_WAKE_TEXT
from pmo.fakes import FakeSidecar

from _pmo_env import (
    OTHER_TEXT,
    PMO_IDENTITY,
    TEAM_KEY,
    TEAM_PM,
    TICKET,
    WAKE_JOB,
    make_deps,
    pmo_env,
    put_job,
    run_tick,
    run_wake,
    seed_team_state,
    seed_tick_state,
    set_config,
)


# --------------------------------------------------------------------------- #
# A. `pmo wake show`（唯讀）
# --------------------------------------------------------------------------- #


def test_show_prints_the_own_job_the_interval_and_the_earliest_due(pmo_env):
    set_config(pmo_env)
    seed_tick_state(
        pmo_env,
        wake={"every": "10m", "set_at": "2026-10-19T10:00:00Z"},
        last_tick_at="2026-10-19T10:05:00Z",
        worker_poll={TEAM_KEY: {"next_due_at": "2026-10-19T10:20:00Z"}},
    )
    deps, sidecar = make_deps()
    put_job(
        sidecar,
        every=600.0,
        last_run="2026-10-19T10:05:00Z",
        last_error="agent_not_running",
        next_run_at="2026-10-19T10:25:00Z",
    )

    rc, out, err = run_wake(pmo_env, deps, "show")

    assert (rc, err) == (0, "")
    lines = out.splitlines()
    assert lines[0] == (
        "[wake] every=10m set_at=2026-10-19T10:00:00Z "
        "last_tick_at=2026-10-19T10:05:00Z next-due=2026-10-19T10:20:00Z"
    )
    assert lines[1] == (
        "[wake] job job-own every=600s next=2026-10-19T10:25:00Z "
        "last_run=2026-10-19T10:05:00Z last_error=agent_not_running"
    )
    assert len(lines) == 2


def test_show_json_carries_the_known_fields_only(pmo_env):
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": None, "set_at": None})
    deps, sidecar = make_deps()
    put_job(sidecar, every=600.0, next_run_at="2026-10-19T10:25:00Z")

    rc, out, err = run_wake(pmo_env, deps, "show", "--json")

    assert (rc, err) == (0, "")
    report = json.loads(out)
    assert report["wake"] == {"every": None, "set_at": None}
    assert report["next_due_at"] is None
    assert report["other_jobs"] == []
    (job,) = report["jobs"]
    assert job == {
        "id": WAKE_JOB,
        "action": "poke",
        "every": 600.0,
        "next_run_at": "2026-10-19T10:25:00Z",
        "last_run": None,
        "last_error": None,
    }


def test_show_lists_the_other_poke_jobs_without_deleting_them(pmo_env):
    """只列不動：`args.id` 是 PMO、句子不同的 poke job；兩個都不符的不列。

    句子是固定句的 job 一律算「自己的」（§3.6 用內容認，不看 `args.id`），
    所以瞄準別的 agent、句子卻是固定句的那一個也列在自己的 job 裡；兩個固定句
    的 job 都只出現在自己的區塊，`other` 區塊只有句子不同的 `job-other`。
    """
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": None})
    deps, sidecar = make_deps()
    put_job(sidecar, "job-other", text=OTHER_TEXT, agent_id=PMO_IDENTITY)
    put_job(sidecar, "job-team", text=OTHER_TEXT, agent_id=TEAM_PM)
    put_job(sidecar, "job-fixed", text=DEFAULT_WAKE_TEXT, agent_id=TEAM_PM)
    put_job(sidecar, "job-own-fixed", text=DEFAULT_WAKE_TEXT, agent_id=PMO_IDENTITY)

    rc, out, err = run_wake(pmo_env, deps, "show")

    assert (rc, err) == (0, "")
    lines = out.splitlines()
    header = f"[wake] 瞄準 {PMO_IDENTITY} 的其他 poke job（句子不同，只列不動）："
    assert lines[0] == "[wake] every=off set_at=- last_tick_at=- next-due=-"
    assert lines[1] == "[wake] job job-fixed every=600s next=- last_run=- last_error=-"
    assert lines[2] == ("[wake] job job-own-fixed every=600s "
                        "next=- last_run=- last_error=-")
    assert lines[3] == header
    # other 區塊在標頭之後，內容只有 job-other（固定句的 job 不在裡面）
    assert lines[4] == "[wake]   job-other every=600s next=- last_run=- last_error=-"
    assert len(lines) == 5
    assert "job-fixed" not in "\n".join(lines[4:])
    assert "job-own-fixed" not in "\n".join(lines[4:])
    assert "job-team" not in out  # 句子不同、又不是瞄準 PMO：兩邊都不列
    assert sidecar.deleted_jobs == []
    assert {"job-other", "job-team", "job-fixed", "job-own-fixed"} <= set(sidecar.jobs)


def test_show_counts_the_watchdog_in_the_earliest_due(pmo_env):
    """`next-due` 含看門狗：team 有派工、還沒 ack，最早到期就是 poke 時刻。

    NOW 是 10:15，`received_at` 是 10:10：看門狗下一次在 10:20（裁定第 2 節）。
    """
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": None})
    seed_team_state(
        pmo_env,
        ticket=TICKET,
        state="running",
        received_at="2026-10-19T10:10:00Z",
    )
    deps, _sidecar = make_deps()

    rc, out, err = run_wake(pmo_env, deps, "show")

    assert (rc, err) == (0, "")
    assert out.splitlines()[0] == (
        "[wake] every=off set_at=- last_tick_at=- next-due=2026-10-19T10:20:00Z"
    )


def test_show_and_tick_agree_on_the_next_due(pmo_env):
    """`wake show` 的 `next-due` 與 tick 的 `[idle] [next-due=…]` 是同一個答案。

    兩邊都呼叫 `pmo.due.earliest_due`；tick 這一輪不 poke（10:20 才到期），
    所以印的是 idle 而不是 watchdog 事件（裁定第 2 節）。
    """
    set_config(pmo_env)
    seed_tick_state(pmo_env, wake={"every": None})
    seed_team_state(
        pmo_env,
        ticket=TICKET,
        state="running",
        received_at="2026-10-19T10:10:00Z",
    )
    sidecar = FakeSidecar()
    sidecar.add_agent(TEAM_PM, state="running")
    deps, _sidecar = make_deps(sidecar)

    rc, out, err = run_tick(pmo_env, deps)
    assert (rc, out, err) == (0, "[idle] [next-due=2026-10-19T10:20:00Z]\n", "")

    rc, out, err = run_wake(pmo_env, deps, "show")
    assert (rc, err) == (0, "")
    assert out.splitlines()[0].endswith("next-due=2026-10-19T10:20:00Z")
    assert sidecar.pokes == []  # 還沒到期：show 也不 poke（唯讀）


def test_show_uses_only_tick_json_when_no_team_state_exists(pmo_env):
    """沒有 team 狀態時只算 `tick.json` 的 handoffs／worker_poll。"""
    set_config(pmo_env)
    seed_tick_state(
        pmo_env,
        wake={"every": None},
        handoffs={
            "01JGROUP": {
                "handed_at": "2026-10-19T10:00:00Z",
                "next_due_at": "2026-10-19T10:18:00Z",
            }
        },
        worker_poll={TEAM_KEY: {"next_due_at": "2026-10-19T10:20:00Z"}},
    )
    deps, _sidecar = make_deps()

    rc, out, err = run_wake(pmo_env, deps, "show")

    assert (rc, err) == (0, "")
    assert out.splitlines()[0].endswith("next-due=2026-10-19T10:18:00Z")


def test_wake_does_not_import_tick():
    """`wake`（原始碼）與 `pmo.due`（子行程載入）都不 import `tick`（裁定第 2 節）。

    `tick` import `wake`（job 健康那一步），反過來 import 會繞回來，而且會讓
    `wake show` 連 tick 一起載入。`TYPE_CHECKING` 底下的型別 import 不算：
    執行時不載入。逐個 import 檢查，連函式裡的延遲 import 也逃不掉。

    第二輪審查 warning-2：只在 `wake` 擋不夠，`due.py` 反向 import `tick` 一樣
    會把 tick 拉進來，所以在乾淨的子行程裡 import `pmo.due`、斷言 `sys.modules`
    沒有 `pmo.tick`。
    """
    import ast

    tree = ast.parse(_p.Path(wake.__file__).read_text(encoding="utf-8"))
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        name = getattr(test, "id", None) or getattr(getattr(test, "value", None), "id", None)
        if name != "TYPE_CHECKING":
            continue
        for child in ast.walk(node):
            line = getattr(child, "lineno", None)
            if line is not None:
                guarded.add(line)
    imported: list[str] = []
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) in guarded:
            continue
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    assert not any(name == "tick" or name.endswith(".tick") for name in imported)

    # `pmo.due` 也一樣：在乾淨的子行程裡 import 後，`sys.modules` 沒有
    # `pmo.tick`。`due.py` 反向 import `tick` 時這一條轉紅（第二輪審查
    # warning-2：只在 `wake` 擋不夠，`due` 也 import tick 就繞回來了）。
    import os
    import subprocess
    import sys

    source_root = _p.Path(wake.__file__).resolve().parent.parent
    inherited = os.environ.get("PYTHONPATH")
    child_env = {
        **os.environ,
        "PYTHONPATH": (
            os.pathsep.join([str(source_root), inherited]) if inherited else str(source_root)
        ),
    }
    code = (
        "import pmo, pmo.due, sys\n"
        "print(pmo.__file__)\n"
        "print(','.join(sorted(m for m in sys.modules if m.split('.')[0] == 'pmo')))\n"
    )
    child = subprocess.run(
        [sys.executable, "-c", code],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert child.returncode == 0, child.stderr
    loaded, modules = child.stdout.splitlines()
    # 子行程載入的是同一份原始碼（改壞驗證在副本裡跑時才咬得到）
    assert loaded == str(_p.Path(wake.__file__).resolve().parent / "__init__.py")
    assert "pmo.tick" not in modules.split(",")

