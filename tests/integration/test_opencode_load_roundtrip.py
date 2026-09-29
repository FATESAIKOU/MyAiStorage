"""impl2：容器裡的整合驗證——`agora-opencode load` 之後，開頭與原 session 位元組相同。

9.1／9.2 的失敗（log：`/tmp/e2e-logs/9.1-v3`、`9.2-v3`）：轉接器交給
`opencode import` 的內容有 3 則（8 則）訊息，import 之後再 export 只剩 **1 則**
（**0 則**）、3407 bytes（應該約 72 KB）。根因與證據寫在
`docs/spike/session-import.md` Q6：重編 id 的寫法 `f"msg_{tag}{i:020d}"[:30]` 在
tag 長過 10 碼時被截斷，序號的位數全被吃掉，**每一則訊息拿到同一個 id**；
`opencode import` 對重複 id 是 `onConflictDoNothing`——**靜默丟棄，rc=0，沒有任何
訊息**。9.2 的 0 則是同一個 bug 的另一副面孔：那次是 `load` 重跑，全部 id 撞上
第一次那批。

這支測試把整條路徑在**真的 resident 映像**裡跑一次，不需要模型、Drive 憑證或網路：

1. 自己起一個容器（不碰 e2e 的環境池，名稱也不共用）；
2. 用 `scripts/spike/session_import_fixture.py` 造一份形狀真實的匯出檔 →
   `opencode import` → **`opencode export` 取得「來源 session 的原始紀錄」**
   （Agora 存的永遠是 export 的位元組，所以這一步不能省）；
3. 用 repo 自己的 `write_package` 組出起點包（接續點＝倒數第二則，模擬 9.1 的
   截斷），`agora-opencode load` 匯入，**再 export**；
4. 斷言訊息數／part 數／內容／順序與原始紀錄完全一樣（只有 id 不同）；
5. 用 spike 的 stub provider（`scripts/spike/session_import_stub.py`，
   `@ai-sdk/openai-compatible` 已內建在 opencode 裡，不需外連）接著跑一句探針，
   比對**送給模型的 request body**：與原 session 接續的共同前綴位元組相同，
   1→n 的兩份更是 FULL 位元組相同（ADR 0010 的 KV cache 要求）；
6. 再 `load` 一次同一個起點包：訊息數不變（重跑是 no-op，不會複製、不會清空）。

執行：`pytest -m integration tests/integration/test_opencode_load_roundtrip.py`
（`integration` 標記預設不跑。映像或 docker 不可用時 FAIL，不是 skip。
容器與工作目錄用 pid 命名，別人不會踩到，也不會留垃圾。）
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import textwrap
import time

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
IMAGE = "aistorage-resident:latest"
CONTAINER = f"aistorage-it-load-{os.getpid()}"
PROBE = "PROBE-繼續做完這一段，然後回一句就好。"


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def _in_container(script: str) -> dict:
    """在容器裡跑一段 python（stdout 最後一行是 JSON）。

    跑的是**工作目錄的實作**（`PYTHONPATH=/probe-src` ＋ `AISTORAGE_SCHEMA_DIR=
    /probe-schemas`），所以不必重建映像就能測到最新的轉接器。
    """
    out = _docker(
        "exec", "-w", "/work",
        "-e", "PYTHONPATH=/probe-src",
        "-e", "AISTORAGE_SCHEMA_DIR=/probe-schemas",
        "-e", "OPENCODE_DISABLE_PROJECT_CONFIG=1",
        "-e", "HOME=/work",
        CONTAINER, "/opt/aistorage/venv/bin/python", "-c",
        textwrap.dedent(script).strip(),
    )
    assert out.returncode == 0, f"容器內腳本失敗：\n{out.stdout}\n{out.stderr}"
    return json.loads(out.stdout.strip().splitlines()[-1])


def _free_port() -> int:
    with socket.socket() as s:            # 避開 e2e 的 4096 與 stub 的 18080
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def load_roundtrip() -> dict:
    """起一個自己的容器，跑完整條 load → export → stub 抓包，回報結果。"""
    probe = _docker("image", "inspect", IMAGE)
    if probe.returncode != 0:
        pytest.fail(
            f"需要 resident 映像 {IMAGE}，但 docker image inspect 失敗："
            f"{probe.stderr.strip()}（用 resident/build.sh 產生）"
        )
    # 工作目錄一定要在 $HOME 底下：colima 只把 home 掛進 VM，/private/var 掛不進去
    work = (Path.home() / ".local" / "share" / "aistorage" / "work"
            / f"impl2-it-{os.getpid()}")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    port = _free_port()
    _docker("rm", "-f", CONTAINER)
    started = _docker(
        "run", "-d", "--name", CONTAINER, "--entrypoint", "sleep",
        "-v", f"{work}:/work",
        "-v", f"{REPO_ROOT / 'src'}:/probe-src:ro",
        "-v", f"{REPO_ROOT / 'schemas'}:/probe-schemas:ro",
        "-v", f"{REPO_ROOT / 'scripts' / 'spike'}:/probe-spike:ro",
        IMAGE, "infinity",
    )
    if started.returncode != 0:
        pytest.fail(f"起容器失敗：{started.stderr.strip()}")
    try:
        report = _stage_import_and_export()
        _start_stub(port)
        report["wire"] = _stage_stub_capture()
        report["merge"] = _stage_merge_interleaved()
        yield report
    finally:
        _docker("rm", "-f", CONTAINER)
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# 第一段：來源 session → 起點包 → load → export
# ---------------------------------------------------------------------------


def _stage_import_and_export() -> dict:
    return _in_container(
        f"""
        import hashlib, json, os, subprocess
        from pathlib import Path
        from aistorage.agora_cli.package import (
            ContextPackage, PackageSegment, write_package)
        from aistorage.agora_cli.startpoint import (
            ResolvedStartPoint, parse_startpoint)

        NATIVE = "ses_impl2src01"
        RESERVED_A = "ses_impl2newA0000000001"
        RESERVED_B = "ses_impl2newB0000000002"
        # 沿用 docker exec 給的環境（PYTHONPATH=/probe-src 在裡面），不要整個換掉
        env = dict(os.environ)
        env.update({{"OPENCODE_DISABLE_PROJECT_CONFIG": "1"}})

        def oc(*args):
            return subprocess.run(["opencode", *args], capture_output=True,
                                  cwd="/work", env=env, check=True)

        def export_to(sid, path):
            # export 的 stdout 走 pipe 會在約 64 KiB 處被截斷（impl1），
            # 所以匯出一定寫成檔案。
            subprocess.run(["sh", "-c", f"opencode export {{sid}} > {{path}}"],
                           capture_output=True, cwd="/work", env=env, check=True)
            return Path(path).read_bytes()

        # 1. 形狀真實的匯出檔 → import → **export**（原始紀錄＝export 的位元組）
        made = subprocess.run(
            ["python3", "/probe-spike/session_import_fixture.py",
             "/work/fixture-src.json", NATIVE],
            capture_output=True, text=True, check=True)
        fixture = json.loads(made.stdout.strip().splitlines()[-1])
        oc("import", "/work/fixture-src.json")
        source_bytes = export_to(NATIVE, "/work/source-raw.json")
        source = json.loads(source_bytes.decode("utf-8"))
        snap = hashlib.sha256(source_bytes).hexdigest()

        # 2. 起點包：接續點＝倒數第二則（9.1 是 8 則進、1 則出）
        ids = [m["info"]["id"] for m in source["messages"]]
        point = ids[-2]
        point_at = next(m for m in source["messages"] if m["info"]["id"] == point)

        def package(reserved, out):
            resolved = ResolvedStartPoint(
                startpoint=parse_startpoint(f"opencode:{{NATIVE}}@{{point}}"),
                session_id=f"opencode:{{NATIVE}}", source="opencode",
                snapshot_sha256=snap, snapshot_at=None, message_id=point)
            text_chars = sum(
                len(p.get("text") or "")
                for m in source["messages"][:ids.index(point) + 1]
                for p in m["parts"])
            write_package(ContextPackage(
                segments=(PackageSegment(
                    resolved=resolved, raw=source_bytes,
                    message_count=ids.index(point) + 1,
                    text_chars=text_chars),),
                task="接著把這一段做完", new_session_id=f"opencode:{{reserved}}",
                created_at="2026-09-30T00:00:00Z",
                created_by="profile:impl2-test"), Path(out))
            return ids.index(point) + 1

        kept = package(RESERVED_A, "/work/pkg-a")

        # 3. 真的跑一次 agora-opencode load（工作目錄的實作）
        def load(pkg):
            out = subprocess.run(
                ["python3", "-m", "aistorage.adapters.opencode", "load", pkg,
                 "-C", "/work", "--json"],
                capture_output=True, text=True, cwd="/work", env=env)
            assert out.returncode == 0, out.stdout + out.stderr
            return json.loads(out.stdout.strip().splitlines()[-1])

        first = load("/work/pkg-a")
        loaded_bytes = export_to(first["session_id"], "/work/loaded-a.json")

        # 4. 重跑同一個起點包（9.2 的形狀）：不能複製一份、也不能變 0 則
        again = load("/work/pkg-a")
        reloaded_bytes = export_to(first["session_id"], "/work/loaded-a-again.json")

        # 5. 1→n：同一份起點內容、第二個預留 id
        package(RESERVED_B, "/work/pkg-b")
        second = load("/work/pkg-b")
        second_bytes = export_to(second["session_id"], "/work/loaded-b.json")

        def shape(doc):
            # 去掉 id 與 parent 之後的「內容形狀」：順序也要在內
            return [(
                m["info"]["role"],
                m["info"].get("time"),
                m["info"].get("modelID"), m["info"].get("providerID"),
                m["info"].get("agent"),
                [(p["type"], p.get("text"), p.get("tool"), p.get("callID"),
                  json.dumps((p.get("state") or {{}}).get("input"), sort_keys=True),
                  (p.get("state") or {{}}).get("output"))
                 for p in m["parts"]],
            ) for m in doc["messages"]]

        loaded = json.loads(loaded_bytes.decode("utf-8"))
        reloaded = json.loads(reloaded_bytes.decode("utf-8"))
        loaded_b = json.loads(second_bytes.decode("utf-8"))
        expected = json.loads(source_bytes.decode("utf-8"))
        expected["messages"] = expected["messages"][:kept]

        print(json.dumps({{
            "fixture": fixture,
            "source": {{"session": NATIVE, "messages": len(source["messages"]),
                       "parts": sum(len(m["parts"]) for m in source["messages"]),
                       "bytes": len(source_bytes)}},
            "package": {{"kept": kept, "point": point,
                        "point_role": point_at["info"]["role"]}},
            "first": first,
            "second": second,
            "retry_session_id": again["session_id"],
            "loaded": {{"messages": len(loaded["messages"]),
                       "parts": sum(len(m["parts"]) for m in loaded["messages"]),
                       "bytes": len(loaded_bytes),
                       "ids_differ": [m["info"]["id"] for m in loaded["messages"]]
                                     != ids[:kept],
                       "ids_sorted": [m["info"]["id"] for m in loaded["messages"]]
                       == sorted(m["info"]["id"] for m in loaded["messages"]),
                       "ids_unique": len({{m["info"]["id"]
                                           for m in loaded["messages"]}})
                       == len(loaded["messages"]),
                       "same_shape_as_source": shape(loaded) == shape(expected),
                       "same_shape_as_reload": shape(loaded) == shape(reloaded)}},
            "reloaded": {{"messages": len(reloaded["messages"]),
                         "parts": sum(len(m["parts"]) for m in reloaded["messages"])}},
            "one_to_n": {{"messages": len(loaded_b["messages"]),
                         "parts": sum(len(m["parts"]) for m in loaded_b["messages"]),
                         "same_shape": shape(loaded_b) == shape(loaded)}},
        }}, ensure_ascii=False))
        """,
    )


# ---------------------------------------------------------------------------
# 第二段：stub 抓包——送給模型的開頭
# ---------------------------------------------------------------------------


def _start_stub(port: int) -> None:
    """在容器裡起 stub provider（`scripts/spike/session_import_stub.py`）。

    `@ai-sdk/openai-compatible` 是 opencode 內建的，所以**不需要外連**——這就是
    這支測試「不需要模型」的關鍵：opencode 真的把 request body 送出去，只是收件
    的不是模型而是一個本機 stub。
    """
    config = {
        "$schema": "https://opencode.ai/config.json",
        "model": "stub/stub-echo",
        "share": "disabled",
        "autoupdate": "notify",
        "snapshot": False,
        "instructions": [],
        "plugin": [],
        "lsp": False,
        "mcp": {},
        "provider": {
            "stub": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Stub (local)",
                "options": {"baseURL": f"http://127.0.0.1:{port}/v1"},
                "models": {"stub-echo": {"name": "Stub Echo"}},
            }
        },
    }
    work = (Path.home() / ".local" / "share" / "aistorage" / "work"
            / f"impl2-it-{os.getpid()}")
    (work / "oc-stub.json").write_text(json.dumps(config), encoding="utf-8")
    _docker("cp", str(work / "oc-stub.json"), f"{CONTAINER}:/work/oc-stub.json")
    _docker("cp", str(REPO_ROOT / "scripts" / "spike" / "session_import_stub.py"),
            f"{CONTAINER}:/work/stub.py")
    _docker("exec", CONTAINER, "mkdir", "-p", "/work/caps")
    listening = _docker(
        "exec", "-d", "-w", "/work", CONTAINER, "python3", "/work/stub.py",
        "--port", str(port), "--dir", "/work/caps", "--reply", "STUB-OK",
    )
    if listening.returncode != 0:
        pytest.fail(f"起 stub 失敗：{listening.stderr.strip()}")
    deadline = time.time() + 30
    while time.time() < deadline:
        if _docker("exec", CONTAINER, "python3", "-c",
                   "import urllib.request;"
                   "urllib.request.urlopen("
                   f"'http://127.0.0.1:{port}/v1/models', timeout=5).read()"
                   ).returncode == 0:
            return
        time.sleep(0.5)
    pytest.fail("容器裡的 stub 30 秒內沒起來")


def _stage_stub_capture() -> dict:
    return _in_container(
        f"""
        import json, subprocess
        from pathlib import Path

        PROBE = {PROBE!r}
        SRC = "ses_impl2src01"
        counts = []            # 每次接續各送出幾個請求（要比對就必須一樣多）
        env = {{"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/work",
               "OPENCODE_CONFIG": "/work/oc-stub.json",
               "OPENCODE_DISABLE_PROJECT_CONFIG": "1"}}

        def caps():
            return sorted(Path("/work/caps").glob("req-*.json"),
                          key=lambda p: int(p.stem.split("-")[1]))

        def continue_with(sid, mark):
            before = len(caps())
            out = subprocess.run(
                ["opencode", "run", "-m", "stub/stub-echo", "-s", sid, PROBE],
                capture_output=True, text=True, cwd="/work", env=env)
            assert out.returncode == 0, out.stdout + out.stderr
            # stub 依順序編號；把這次送模型的 request body 撈出來
            fresh = caps()[before:]
            assert fresh, "stub 沒有抓到任何請求"
            counts.append(len(fresh))
            Path(f"/work/cap-{{mark}}.json").write_bytes(fresh[-1].read_bytes())
            return json.loads(fresh[-1].read_text())

        original = continue_with(SRC, "source")
        loaded = continue_with("ses_impl2newA0000000001", "loaded-a")
        split = continue_with("ses_impl2newB0000000002", "loaded-b")

        def canon(value):
            return json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode("utf-8")

        # 來源 session（9 則）接續 vs 匯入 session（8 則）接續：共同前綴要覆蓋
        # 整段重播，差異恰好是匯入側新加入的探針。
        src_msgs, ld_msgs = original["messages"], loaded["messages"]
        common = 0
        for a, b in zip(src_msgs, ld_msgs):
            if canon(a) != canon(b):
                break
            common += 1
        raw_src = Path("/work/cap-source.json").read_bytes()
        raw_ld = Path("/work/cap-loaded-a.json").read_bytes()
        byte_common = next(
            (i for i, (x, y) in enumerate(zip(raw_src, raw_ld)) if x != y),
            min(len(raw_src), len(raw_ld)))

        ra = Path("/work/cap-loaded-a.json").read_bytes()
        rb = Path("/work/cap-loaded-b.json").read_bytes()
        print(json.dumps({{
            "source_wire_messages": len(src_msgs),
            "loaded_wire_messages": len(ld_msgs),
            "common": common,
            "prefix_identical": common > 1 and
                canon(src_msgs[:common]) == canon(ld_msgs[:common]),
            "last_is_probe": ld_msgs[-1] == {{"role": "user", "content": PROBE}},
            "tools_identical": canon(original["tools"]) == canon(loaded["tools"]),
            "system_identical": canon(src_msgs[0]) == canon(ld_msgs[0]),
            "system_bytes": len(canon(src_msgs[0])),
            "prefix_bytes": len(canon(ld_msgs[:common])),
            "raw_common_prefix_bytes": byte_common,
            "raw_bytes": [len(raw_src), len(raw_ld)],
            "one_to_n_full_identical": ra == rb,
            "one_to_n_bytes": [len(ra), len(rb)],
            "one_to_n_messages": [len(loaded["messages"]), len(split["messages"])],
            "requests_per_run": counts,
        }}, ensure_ascii=False))
        """,
    )


# ---------------------------------------------------------------------------
# 第三段：n→1，兩段的時間交錯
# ---------------------------------------------------------------------------


def _stage_merge_interleaved() -> dict:
    """兩段時間**交錯**的 n→1：匯入後順序要對，而且第一段的開頭位元組相同。

    兩個來源 session 用同一個時間基準（fixture 腳本的種子與時間都固定），所以
    它們的訊息時間是一格一格交錯的——沒有時間位移的話，opencode 匯出（以及送
    模型的上下文）會把兩段交錯著排出來，ADR 0010 的「最長的一段放最前面」就不
    成立（`docs/spike/evidence/impl2-import-id-collision.md` 第 6 節）。

    對照組是「只載入第一段」的 session：接續它與接續合併結果，兩邊送模型的開頭
    必須位元組相同（第一段原封不動）。
    """
    return _in_container(
        f"""
        import hashlib, json, os, subprocess
        from pathlib import Path
        from aistorage.agora_cli.package import (
            ContextPackage, PackageSegment, write_package)
        from aistorage.agora_cli.startpoint import ResolvedStartPoint, parse_startpoint

        PROBE = {PROBE!r}
        A, B = "ses_impl2mergeA01", "ses_impl2mergeB02"
        KEEP_A, KEEP_B = 6, 3          # A 比較長 → 排序後第一段是 A（ADR 0010）
        env = dict(os.environ)
        env.update({{"OPENCODE_DISABLE_PROJECT_CONFIG": "1"}})

        def oc(*args, check=True):
            return subprocess.run(["opencode", *args], capture_output=True,
                                  text=True, cwd="/work", env=env, check=check)

        def export_to(sid, path):
            subprocess.run(["sh", "-c", f"opencode export {{sid}} > {{path}}"],
                           capture_output=True, cwd="/work", env=env, check=True)
            return Path(path).read_bytes()

        def source_session(native):
            # 造一個來源 session，回傳**真實的匯出位元組**
            subprocess.run(["python3", "/probe-spike/session_import_fixture.py",
                            f"/work/{{native}}.json", native], check=True, env=env,
                           capture_output=True)
            oc("import", f"/work/{{native}}.json")
            return export_to(native, f"/work/{{native}}-raw.json")

        def segment(native, raw, keep):
            doc = json.loads(raw)
            ids = [m["info"]["id"] for m in doc["messages"]]
            point = ids[keep - 1]
            resolved = ResolvedStartPoint(
                startpoint=parse_startpoint(f"opencode:{{native}}@{{point}}"),
                session_id=f"opencode:{{native}}", source="opencode",
                snapshot_sha256=hashlib.sha256(raw).hexdigest(), snapshot_at=None,
                message_id=point)
            chars = sum(len(p.get("text") or "")
                        for m in doc["messages"][:keep] for p in m["parts"])
            return PackageSegment(resolved=resolved, raw=raw, message_count=keep,
                                  text_chars=chars)

        raw_a, raw_b = source_session(A), source_session(B)
        seg_a, seg_b = segment(A, raw_a, KEEP_A), segment(B, raw_b, KEEP_B)

        def package(segs, reserved, out):
            # `merge_later_segments` 就是 checkout 會做的事（n→1 宣告「後段時間
            # 已改寫」），用真的 to_dict 寫，不要在測試裡手刻那份宣告。
            write_package(ContextPackage(
                segments=tuple(segs), task="兩段合一",
                new_session_id=f"opencode:{{reserved}}",
                created_at="2026-09-30T00:00:00Z", created_by="profile:impl2-test",
                merge_later_segments=len(segs) > 1), Path(out))
            return json.loads((Path(out) / "package.json").read_text())

        merged_pkg = package([seg_a, seg_b], "ses_impl2merged0001", "/work/pkg-merge")
        package([seg_a], "ses_impl2onlya00001", "/work/pkg-onlya")

        def load(pkg):
            out = subprocess.run(
                ["python3", "-m", "aistorage.adapters.opencode", "load", pkg,
                 "-C", "/work", "--json"], capture_output=True, text=True,
                cwd="/work", env=env)
            assert out.returncode == 0, out.stdout + out.stderr
            return json.loads(out.stdout.strip().splitlines()[-1])

        merged = load("/work/pkg-merge")
        only_a = load("/work/pkg-onlya")
        merged_doc = json.loads(
            export_to(merged["session_id"], "/work/merged.json").decode())
        only_a_doc = json.loads(
            export_to(only_a["session_id"], "/work/only-a.json").decode())

        def texts(doc):
            return [next((p.get("text") for p in m["parts"]
                          if p.get("type") == "text"), "")
                    for m in doc["messages"]]

        src_a = json.loads(raw_a)
        src_a_texts = texts(src_a)[:KEEP_A]
        src_b_texts = texts(json.loads(raw_b))[:KEEP_B]
        merged_texts = texts(merged_doc)
        times = [m["info"]["time"]["created"] for m in merged_doc["messages"]]

        # ── 送模型的開頭：只載入第一段 vs 載入兩段 ──────────────────────────
        def caps():
            return sorted(Path("/work/caps").glob("req-*.json"),
                          key=lambda p: int(p.stem.split("-")[1]))

        def continue_with(sid, mark):
            before = len(caps())
            out = subprocess.run(
                ["opencode", "run", "-m", "stub/stub-echo", "-s", sid, PROBE],
                capture_output=True, text=True, cwd="/work",
                env={{**env, "OPENCODE_CONFIG": "/work/oc-stub.json"}})
            assert out.returncode == 0, out.stdout + out.stderr
            fresh = caps()[before:]
            assert fresh, "stub 沒有抓到任何請求"
            counts.append(len(fresh))
            Path(f"/work/cap-{{mark}}.json").write_bytes(fresh[-1].read_bytes())
            return json.loads(fresh[-1].read_text())

        counts = []
        wire_a = continue_with(only_a["session_id"], "onlya")
        wire_merged = continue_with(merged["session_id"], "merged")

        def canon(value):
            return json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode("utf-8")

        a_msgs, merged_msgs = wire_a["messages"], wire_merged["messages"]
        common = 0
        for x, y in zip(a_msgs, merged_msgs):
            if canon(x) != canon(y):
                break
            common += 1
        raw_a_body = Path("/work/cap-onlya.json").read_bytes()
        raw_merged_body = Path("/work/cap-merged.json").read_bytes()
        byte_common = next(
            (i for i, (x, y) in enumerate(zip(raw_a_body, raw_merged_body))
             if x != y), min(len(raw_a_body), len(raw_merged_body)))

        print(json.dumps({{
            "package": {{"segments": merged_pkg["totals"]["segments"],
                        "time_shift": merged_pkg.get("time_shift"),
                        "declared_kept": [KEEP_A, KEEP_B]}},
            "load": {{"merged": merged, "only_a": only_a}},
            "order": {{"first_segment_intact":
                       merged_texts[:KEEP_A] == src_a_texts,
                       "second_segment_intact":
                       merged_texts[KEEP_A:KEEP_A + KEEP_B] == src_b_texts,
                       "only_a_messages": len(only_a_doc["messages"])}},
            "times": {{"monotonic": times == sorted(times),
                      "shift_ms": merged.get("time_shift_ms"),
                      "first_segment_unchanged":
                          times[:KEEP_A] == [m["info"]["time"]["created"]
                                             for m in src_a["messages"][:KEEP_A]],
                      "count": len(times)}},
            "wire": {{"only_a_messages": len(a_msgs),
                     "merged_messages": len(merged_msgs),
                     "common": common,
                     "prefix_identical": common > 1 and
                        canon(a_msgs[:common]) == canon(merged_msgs[:common]),
                     "last_of_a_is_probe":
                        a_msgs[-1] == {{"role": "user", "content": PROBE}},
                     "prefix_bytes": len(canon(a_msgs[:common])),
                     "raw_common_prefix_bytes": byte_common,
                     "requests_per_run": counts}},
        }}, ensure_ascii=False))
        """,
    )


# ---------------------------------------------------------------------------
# 斷言
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_load_keeps_every_message_and_part_of_the_start_point(load_roundtrip: dict):
    """匯入之後再 export：訊息數、part 數、內容與順序都跟原始紀錄一樣。

    9.1 的形狀是 8 則進、1 則出（3407 bytes）；所以這裡連 part 級的內容與順序
    都比對——只比「訊息數」抓不到「同一則訊息裡的 part 被吃掉」那種情況。
    """
    source, package, loaded = (load_roundtrip["source"], load_roundtrip["package"],
                               load_roundtrip["loaded"])
    assert source["messages"] == 9, source
    assert package["kept"] == source["messages"] - 1 == 8, package
    assert load_roundtrip["first"]["messages"] == package["kept"], \
        load_roundtrip["first"]
    assert load_roundtrip["first"]["segments"] == 1
    assert loaded["messages"] == package["kept"], loaded
    assert loaded["parts"] > loaded["messages"], "這份樣本有 tool／step-finish 等 part"
    assert loaded["same_shape_as_source"] is True, (
        "匯入後的內容或順序與原始紀錄不同——轉接器不能改動任何欄位"
        f"（{loaded}）"
    )
    assert loaded["bytes"] > source["bytes"] // 2, loaded
    # id 全部換新（重編是規則本身），而且唯一、字典序遞增
    assert loaded["ids_differ"] is True
    assert loaded["ids_unique"] is True, loaded
    assert loaded["ids_sorted"] is True, (
        "id 字典序必須跟匯出順序一致：opencode 是 ORDER BY time_created, id"
    )


@pytest.mark.integration
def test_loading_the_same_package_twice_changes_nothing(load_roundtrip: dict):
    """重跑 `agora-opencode load`（9.2 的形狀）是 no-op：不複製、不清空。

    9.2 的 0 則就是重跑造成的：舊的 id 全部撞上第一次那批，被
    `onConflictDoNothing` 丟光。鹽由預留 session id 推導，所以重跑算出同一套 id。
    """
    assert load_roundtrip["retry_session_id"] == load_roundtrip["first"]["session_id"]
    loaded, reloaded = load_roundtrip["loaded"], load_roundtrip["reloaded"]
    assert reloaded["messages"] == loaded["messages"], (loaded, reloaded)
    assert reloaded["parts"] == loaded["parts"], (loaded, reloaded)
    assert loaded["same_shape_as_reload"] is True, (
        "重跑之後內容或順序變了：同一個預留 session id 必須算出同一套 id，"
        "匯入因此是 no-op"
    )


@pytest.mark.integration
def test_one_to_n_gives_two_complete_sessions(load_roundtrip: dict):
    """1→n：同一份起點內容、兩個預留 id，兩個 session 的內容都完整。"""
    one_to_n = load_roundtrip["one_to_n"]
    assert one_to_n["messages"] == load_roundtrip["loaded"]["messages"], one_to_n
    assert one_to_n["parts"] == load_roundtrip["loaded"]["parts"], one_to_n
    assert one_to_n["same_shape"] is True
    assert load_roundtrip["second"]["session_id"] != \
        load_roundtrip["first"]["session_id"]


@pytest.mark.integration
def test_the_prefix_sent_to_the_model_is_byte_identical(load_roundtrip: dict):
    """送給模型的開頭與原 session 位元組相同（ADR 0010 的 KV cache 要求）。

    用 spike 的 stub provider 抓**真的** request body（`docs/spike/session-import.md`
    Q2 的方法）：來源 session 繼續跑 vs 匯入出來的 session 繼續跑，同一句探針，
    共同前綴必須覆蓋整段重播——第一個不一樣的欄位就是匯入側新加入的探針本身。
    """
    wire = load_roundtrip["wire"]
    # 匯入側 = 系統提示 + 整段重播 + 探針；共同前綴要含系統提示與全部重播
    assert wire["common"] == wire["loaded_wire_messages"] - 1, wire
    assert wire["prefix_identical"] is True, wire
    assert wire["last_is_probe"] is True, wire
    assert wire["system_identical"] is True, wire
    assert wire["tools_identical"] is True, wire
    # 位元組層級：兩個 request body 的共同前綴至少涵蓋整段重播
    assert wire["raw_common_prefix_bytes"] >= wire["prefix_bytes"], wire
    assert wire["source_wire_messages"] > wire["loaded_wire_messages"], wire


@pytest.mark.integration
def test_two_loads_of_the_same_start_point_send_identical_bytes(load_roundtrip: dict):
    """1→n 的兩份，送給模型的 request body **FULL 位元組相同**。

    這是 ADR 0010 對 1→n 共用 prompt cache 的要求：只有重編 id 不同（id 不會
    送到模型），其他一個位元組都不能差。
    """
    wire = load_roundtrip["wire"]
    assert wire["one_to_n_full_identical"] is True, (
        f"1→n 的兩份 request body 不同：{wire['one_to_n_bytes']}；"
        "重編 id 之外還有東西不一樣，cache 就不會共用"
    )
    assert wire["one_to_n_bytes"][0] > 1000, wire
    # 三次接續各送出同樣數量的請求（比對才是在比同樣的東西）
    assert len(set(wire["requests_per_run"])) == 1, wire["requests_per_run"]


@pytest.mark.integration
def test_n_to_one_keeps_the_declared_segment_order(load_roundtrip: dict):
    """兩段時間交錯的 n→1：匯入後的順序仍然是起點包宣告的順序。

    opencode 匯出是 `ORDER BY time_created`，所以只把陣列排好不夠——後段必須整體
    往後排（ADR 0010 的「最長的一段放最前面」）。這裡的兩段時間是一格一格交錯的
    （沒有位移就會被交錯排出來），所以這個測試抓得到。
    """
    merge = load_roundtrip["merge"]
    package, order, times = merge["package"], merge["order"], merge["times"]

    assert package["segments"] == 2, package
    assert package["time_shift"] == {
        "rule": "later_segments_after_first", "first_segment_unchanged": True,
        "applier": "adapter"}, "起點包 metadata 沒有記「後段時間已改寫」"
    assert merge["load"]["merged"]["segments"] == 2
    assert merge["load"]["merged"]["messages"] == sum(package["declared_kept"]) == 9

    # 匯出來的順序 = 第一段六則（原封不動）＋第二段三則
    assert order["first_segment_intact"] is True, order
    assert order["second_segment_intact"] is True, order
    assert times["count"] == 9
    assert times["monotonic"] is True, (
        "時間必須單調遞增，否則 opencode 會依 time.created 交錯排列"
    )
    assert times["first_segment_unchanged"] is True, times
    assert times["shift_ms"][0] == 0 and times["shift_ms"][1] > 0, times


@pytest.mark.integration
def test_n_to_one_first_segment_prefix_is_byte_identical(load_roundtrip: dict):
    """合併之後，第一段送給模型的開頭與「只載入第一段」時**位元組相同**。

    這是 n→1 的位元組相同要求：第二段是**後來加上去的**，所以前綴不能動。
    對照組是只載入第一段的 session，兩邊用同一句探針接續。
    """
    wire = load_roundtrip["merge"]["wire"]
    assert wire["common"] == wire["only_a_messages"] - 1, wire
    assert wire["prefix_identical"] is True, wire
    assert wire["last_of_a_is_probe"] is True, wire
    assert wire["raw_common_prefix_bytes"] >= wire["prefix_bytes"], wire
    assert len(set(wire["requests_per_run"])) == 1, wire["requests_per_run"]
