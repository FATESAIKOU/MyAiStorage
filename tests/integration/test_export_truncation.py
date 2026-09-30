"""impl1：容器裡的整合重現——大 Session 也要取得完整（**不需要模型**）。

9.5 e2e 的失敗：`opencode export` 的 stdout 走 pipe 時，輸出超過約 64 KiB 就會以
rc=0 回傳被截斷的 JSON，整個 Session 收不進 Agora。這個檔案在**真的 resident 映像**
裡重現同一件事，並且不需要任何模型、Drive 憑證或網路：

1. 自己起一個容器（不碰 e2e 的 `resident_pool`，名稱也不共用）；
2. 用 `opencode import` 造一個約 300 KB 的 Session（匯入本身不需要模型）；
3. 對照組：證明舊的取法（`capture_output=True` → pipe）會被截斷；
4. 實作後的取法：連續取得 N 次，每次都要完整、且位元組與匯出完全相同；
5. 為什麼不選「HTTP API 逐則取訊息再組回」（內容等價、位元組不等價）。

執行：`pytest -m integration tests/integration/test_export_truncation.py`
（`integration` 標記預設不跑，要明確指定。映像或 docker 不可用時 FAIL，不是 skip。）
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import textwrap
import time

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE = "aistorage-resident:latest"
CONTAINER = "aistorage-it-export-large"
SID = "ses_impl1big"
BIG_BYTES = 300_000


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


@pytest.fixture(scope="module")
def big_session_container():
    """起一個自己的容器，裡面有一個約 300 KB 的 Session 與一個 opencode serve。"""
    probe = _docker("image", "inspect", IMAGE)
    if probe.returncode != 0:
        pytest.fail(
            f"需要 resident 映像 {IMAGE}，但 docker image inspect 失敗："
            f"{probe.stderr.strip()}（用 resident/build.sh 產生）"
        )
    import os
    import socket

    with socket.socket() as s:          # 挑一個沒人用的 port，避開 e2e 的 4096
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    # 工作目錄一定要在 $HOME 底下：colima 只把 home 掛進 VM，/private/var 掛不進去
    work = Path.home() / ".local" / "share" / "aistorage" / "work" / f"impl1-it-{os.getpid()}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    _docker("rm", "-f", CONTAINER)
    started = _docker(
        "run", "-d", "--name", CONTAINER, "--entrypoint", "sleep",
        "-v", f"{work}:/work",
        "-v", f"{REPO_ROOT / 'src'}:/probe-src:ro",
        "-v", f"{REPO_ROOT / 'schemas'}:/probe-schemas:ro",
        IMAGE, "infinity",
    )
    if started.returncode != 0:
        pytest.fail(f"起容器失敗：{started.stderr.strip()}")
    try:
        _import_big_session(port, work)
        yield {"port": port, "work": work}
    finally:
        _docker("rm", "-f", CONTAINER)
        shutil.rmtree(work, ignore_errors=True)


def _import_big_session(port: int, work: Path) -> None:
    """在容器裡造一個約 300 KB 的 Session 並匯入（**不需要模型**）。"""
    generator = textwrap.dedent(
        '''
        import json, random, sys
        # 形狀比照 1.18.32 的原生 export（docs/spike/evidence/impl1-export-truncation.md
        # 第 3 節），id 全部自己編（匯入端要求 message/part id 全域唯一）。
        doc = {
            "info": {"id": "ses_impl1big", "slug": "impl1-big", "projectID": "global",
                     "directory": "/work", "path": "work", "title": "impl1 big",
                     "version": "1.18.32", "cost": 0,
                     "tokens": {"input": 0, "output": 0, "reasoning": 0,
                                "cache": {"read": 0, "write": 0}},
                     "time": {"created": 1790400000000, "updated": 1790400009999}},
            "messages": [],
        }
        rng = random.Random(20260928)
        parent, i, target = "msg_impl1bigroot", 0, int(sys.argv[1])
        while True:
            mid = f"msg_impl1big{i:06d}"
            info = {"id": mid, "sessionID": doc["info"]["id"], "parentID": parent,
                    "role": "user" if i % 2 == 0 else "assistant",
                    "time": {"created": 1790400000000 + i}}
            if info["role"] == "assistant":
                info.update({"mode": "build", "agent": "build",
                             "path": {"cwd": "/work", "root": "/"}, "cost": 0,
                             "tokens": {"total": 10, "input": 5, "output": 5,
                                        "reasoning": 0, "cache": {"read": 0, "write": 0}},
                             "modelID": "space-bunny-free", "providerID": "opencode",
                             "finish": "stop"})
            else:
                info.update({"agent": "build",
                             "model": {"providerID": "opencode",
                                       "modelID": "space-bunny-free"},
                             "summary": {"diffs": []}})
            parts = [{"id": f"prt_impl1big{i:06d}", "sessionID": doc["info"]["id"],
                      "messageID": mid, "type": "text",
                      "text": ("內容" + str(i) + " ") * 120
                              + "".join(rng.choice("abcdefghij") for _ in range(20))}]
            doc["messages"].append({"info": info, "parts": parts})
            parent = mid
            i += 1
            if i % 5 == 0 and len(json.dumps(doc, ensure_ascii=False)) >= target:
                break
        blob = json.dumps(doc, ensure_ascii=False, indent=2)
        open("/work/impl1-big.json", "w", encoding="utf-8").write(blob)
        print(len(blob.encode()), len(doc["messages"]))
        '''
    ).strip()
    (work / "gen.py").write_text(generator, encoding="utf-8")

    made = _docker("exec", "-w", "/work", CONTAINER, "python3", "gen.py", str(BIG_BYTES))
    if made.returncode != 0:
        pytest.fail(f"造大 Session 失敗：{made.stderr.strip()}")
    imported = _docker(
        "exec", "-w", "/work", "-e", "OPENCODE_DISABLE_PROJECT_CONFIG=1",
        CONTAINER, "opencode", "import", "/work/impl1-big.json",
    )
    if imported.returncode != 0 or "Imported session" not in imported.stdout:
        pytest.fail(f"opencode import 失敗：{imported.stdout}{imported.stderr}")

    # 真值：stdout 導到一般檔案（實測 100% 完整，見 evidence 第 2 節）
    truth = _docker(
        "exec", "-w", "/work", "-e", "OPENCODE_DISABLE_PROJECT_CONFIG=1",
        CONTAINER, "sh", "-c", f"opencode export {SID} > /work/truth.json",
    )
    if truth.returncode != 0:
        pytest.fail(f"匯出真值失敗：{truth.stderr.strip()}")

    served = _docker(
        "exec", "-d", "-w", "/work", "-e", "OPENCODE_DISABLE_PROJECT_CONFIG=1",
        CONTAINER, "opencode", "serve", "--port", str(port), "--hostname", "127.0.0.1",
    )
    if served.returncode != 0:
        pytest.fail(f"起 opencode serve 失敗：{served.stderr.strip()}")
    ready = textwrap.dedent(
        f"""
        import sys, urllib.request
        try:
            urllib.request.urlopen(
                "http://127.0.0.1:{port}/session?directory=/work", timeout=5).read()
        except Exception:
            sys.exit(1)
        """
    ).strip()
    deadline = time.time() + 60
    while time.time() < deadline:
        if _docker("exec", CONTAINER, "python3", "-c", ready).returncode == 0:
            return
        time.sleep(1.0)
    pytest.fail("容器裡的 opencode serve 沒有在 60 秒內就緒")


def _in_container(big_session_container: dict, script: str) -> subprocess.CompletedProcess:
    """在容器裡跑一段 python，跑的是**工作目錄的實作**（不是映像裡的舊 wheel）。

    `AISTORAGE_SCHEMA_DIR` 是 `aistorage.schema` 認得的指向（schema.py 第 100 行），
    所以掛上 repo 的 `src` 與 `schemas` 就等於在容器裡跑最新實作，**不必重建映像**。
    """
    return _docker(
        "exec", "-w", "/work",
        "-e", "PYTHONPATH=/probe-src",
        "-e", "AISTORAGE_SCHEMA_DIR=/probe-schemas",
        "-e", f"PROBE_PORT={big_session_container['port']}",
        "-e", "OPENCODE_DISABLE_PROJECT_CONFIG=1",
        "-e", "HOME=/work",
        CONTAINER, "/opt/aistorage/venv/bin/python", "-c", script,
    )


def _run(big_session_container: dict, script: str) -> dict:
    out = _in_container(big_session_container, script)
    assert out.returncode == 0, f"容器內腳本失敗：\n{out.stdout}\n{out.stderr}"
    return json.loads(out.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_pipe_stdout_truncates_a_large_export(big_session_container: dict):
    """對照組：舊的取法（stdout 走 pipe）**會**拿到被截斷的 JSON，而且 rc=0。

    這條是這個 bug 的存在證明。哪一天 opencode 修好了它會失敗——那時應該改成
    「已修正」的記錄，不是刪掉。
    """
    data = _run(big_session_container, textwrap.dedent(
        """
        import json, subprocess
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/work",
               "OPENCODE_DISABLE_PROJECT_CONFIG": "1"}
        sizes, parsed = [], []
        for _ in range(3):
            p = subprocess.run(["opencode", "export", "ses_impl1big"],
                               capture_output=True, cwd="/work", env=env)
            sizes.append(len(p.stdout))
            try:
                json.loads(p.stdout.decode("utf-8"))
                parsed.append(True)
            except ValueError:
                parsed.append(False)
        print(json.dumps({"sizes": sizes, "parsed": parsed, "rc": p.returncode}))
        """
    ).strip())
    assert data["rc"] == 0, "opencode export 失敗就不會是截斷的問題了"
    assert not all(data["parsed"]), (
        f"pipe 取法居然三次都完整（{data}）；若 opencode 已修正，請更新本檔與 "
        "docs/spike/evidence/impl1-export-truncation.md 的結論"
    )


@pytest.mark.integration
def test_export_from_a_file_is_complete_and_byte_identical(big_session_container: dict):
    """實作後的取法：連續三次都完整，且位元組與匯出本身**完全相同**。

    位元組相同是硬條件：原始紀錄要原封不動、checkout／轉接器的開頭要位元組相同
    （ADR 0010）。所以比的是 sha256，不只是「能 parse」。
    """
    data = _run(big_session_container, textwrap.dedent(
        """
        import hashlib, json, os, sys
        from pathlib import Path
        from aistorage.syncer.opencode_api import OpencodeApi

        sid = "ses_impl1big"
        api = OpencodeApi(base_url="http://127.0.0.1:" + os.environ["PROBE_PORT"],
                          directory="/work")
        truth = Path("/work/truth.json").read_bytes()
        doc = json.loads(truth.decode("utf-8"))
        runs = []
        for i in range(3):
            dest = Path(f"/work/got-{i}.json")
            api.export(sid, dest)
            runs.append(hashlib.sha256(dest.read_bytes()).hexdigest())
        print(json.dumps({
            "truth_bytes": len(truth),
            "messages": len(doc["messages"]),
            "truth_sha": hashlib.sha256(truth).hexdigest(),
            "runs": runs,
            "leftovers": sorted(p.name for p in Path("/work").glob("*.partial")),
        }))
        """
    ).strip())

    assert data["messages"] > 100, "這個測試要的是「真的很大」的 Session"
    assert data["truth_bytes"] > BIG_BYTES, data["truth_bytes"]
    assert data["runs"] == [data["truth_sha"]] * 3, data
    # 驗證通過後不留暫存檔
    assert data["leftovers"] == [], data["leftovers"]


@pytest.mark.integration
def test_api_rebuild_matches_export_in_content_but_not_in_bytes(
    big_session_container: dict,
):
    """為什麼不選「HTTP API 逐則取訊息再組回」：內容等價，**位元組不等價**。

    `opencode export` 依 DB 欄位順序輸出，API 依自己的順序回，欄位順序不同。
    原始紀錄要求與 export 位元組相同，所以那條路不能用。這是取法的取捨依據，
    留下來當作「換取法」的守門。
    """
    data = _run(big_session_container, textwrap.dedent(
        """
        import json, os, urllib.request
        from pathlib import Path

        def get(path):
            req = urllib.request.Request(
                f"http://127.0.0.1:{os.environ['PROBE_PORT']}{path}?directory=/work",
                headers={"accept": "application/json"})
            return json.loads(urllib.request.urlopen(req, timeout=60).read())

        export = json.loads(Path("/work/truth.json").read_text(encoding="utf-8"))
        rebuilt = {"info": get("/session/ses_impl1big"),
                   "messages": get("/session/ses_impl1big/message")}
        print(json.dumps({
            "same_content": rebuilt == export,
            "same_order": list(rebuilt["info"]) == list(export["info"]),
            "counts": [len(export["messages"]), len(rebuilt["messages"])],
        }))
        """
    ).strip())

    # 訊息數對得上（這是完整性驗證用的交叉檢查）
    assert data["counts"][0] == data["counts"][1], data["counts"]
    # 內容等價
    assert data["same_content"] is True, data
    # 但欄位順序不同 → 位元組不同 → 不能拿來組原始紀錄
    assert data["same_order"] is False, (
        "API 的欄位順序竟然和 export 一致了；若 opencode 改了，值得重新評估"
        "用 API 組回來（可省掉對 CLI 的依賴）"
    )


@pytest.mark.integration
def test_cross_check_rejects_an_export_the_api_does_not_know(
    big_session_container: dict,
):
    """交叉檢查真的會擋，而且擋下之後 `dest` 上不留任何東西。

    這是「形狀對、JSON 也對，但內容不是整個 Session」那一種的守門。用真的 CLI
    匯出、真的 API 取 id，只把 API 看到的訊息砍掉尾巴——模擬匯出多出了 API 還
    沒有的訊息。單元測試用假資料，這裡證明兩邊的訊息 id 真的對得上。
    """
    data = _run(big_session_container, textwrap.dedent(
        """
        import json, os, subprocess
        from pathlib import Path
        from aistorage.errors import IncompleteFetch, ReadError
        from aistorage.syncer.opencode_api import OpencodeApi

        sid = "ses_impl1big"
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/work",
               "OPENCODE_DISABLE_PROJECT_CONFIG": "1"}
        api = OpencodeApi(base_url="http://127.0.0.1:" + os.environ["PROBE_PORT"],
                          directory="/work")
        real_ids = api.message_ids(sid)
        doc = json.loads(Path("/work/truth.json").read_text(encoding="utf-8"))
        assert [m["info"]["id"] for m in doc["messages"]] == real_ids, (
            "匯出的訊息 id 順序應該與 API 一致")

        # API 只看得到前 N-3 則：匯出宣稱到了最後一則，就必須判定為取得不完整
        api.message_ids = lambda _sid: real_ids[:-3]

        import aistorage.syncer.opencode_api as mod
        real_run = subprocess.run                          # 先抓住原來的，否則會自己遞迴
        mod.subprocess.run = lambda argv, **kw: real_run(  # type: ignore[assignment]
            argv, stdout=kw["stdout"], stderr=subprocess.PIPE,
            cwd="/work", env=env, check=False)
        dest = Path("/work/should-not-exist.json")
        try:
            api.export(sid, dest)
        except IncompleteFetch as e:
            print(json.dumps({
                "rejected": True,
                "is_read_error": isinstance(e, ReadError),
                "message": str(e)[:90],
                "dest_exists": dest.exists(),
                "partials": sorted(p.name for p in Path("/work").glob("*.partial")),
            }))
        else:
            print(json.dumps({"rejected": False}))
        """
    ).strip())

    assert data.get("rejected") is True, data
    # 明確是「取得不完整」，不是「讀不到」
    assert data["is_read_error"] is False, data
    assert data["dest_exists"] is False, "不能留下可被上傳的半份"
    assert data["partials"] == [], data["partials"]
