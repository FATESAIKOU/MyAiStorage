"""plugin 的冒煙測試（tasks 5.4）。

plugin 是 TypeScript，邏輯刻意很少，所以測試重點是**契約**：
- 工具名稱、說明、參數 schema、execute 都在；
- Session id 由 `context.sessionID` 帶入，**模型的參數裡沒有「自己」這個 id**
  （`aistorage_read`／`aistorage_reference` 的 `session_id` 是**目標**）；
- 主 Session 限定的工具有 parentID 就拒絕（plugin 這是第一層，Python 是第二層）；
- 參數經過單引號跳脫，不會變成 shell 結構；
- 工具名稱對應到真的 `python -m aistorage.skill` 子命令。

用 node 的 `--experimental-strip-types` 直接跑 plugin 模組（不建 node_modules），
並在 PATH 前放一個假的 `python`，把真的 argv 記下來。沒有 node 就整組略過。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import textwrap

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "resident/opencode/plugin/aistorage.ts"

#: 工具名稱 → `python -m aistorage.skill` 的子命令
TOOL_TO_COMMAND = {
    "aistorage_whoami": "whoami",
    "aistorage_split": "split",
    "aistorage_handoff_end": "handoff-end",
    "aistorage_claim": "claim",
    "aistorage_find": "find",
    "aistorage_read": "read",
    "aistorage_reference": "reference",
    "aistorage_list_handoffs": "list-handoffs",
    "aistorage_stop": "stop",
}

#: 只有主 Session 能用的工具（plugin 這一層是第一道防線）
MAIN_ONLY = ("aistorage_claim", "aistorage_stop")

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="沒有 node 就沒辦法驗 plugin"
)

# 假的 python：把 argv 寫進檔案，輸出假的成功 JSON
# 假的 python：把 argv 逐行 append 進 log，輸出假的成功 JSON
FAKE_PYTHON = """#!/bin/sh
: >> "$AISTORAGE_ARGV_LOG"
printf '%s\\n' "--CALL--" >> "$AISTORAGE_ARGV_LOG"
printf '%s\\n' "$@" >> "$AISTORAGE_ARGV_LOG"
echo '{"session_id":"opencode:ses_main","parent_id":null,"is_main":true}'
"""

HARNESS = """
import {{ pathToFileURL }} from "node:url"
const mod = await import(pathToFileURL({plugin!r}).href)
const plugin = await mod.AistoragePlugin()
const out = {{ tools: {{}}, errors: {{}} }}

for (const [name, tool] of Object.entries(plugin.tool)) {{
  out.tools[name] = {{
    description: tool.description,
    has_schema: !!tool.args,
    properties: Object.keys(tool.args?.properties ?? {{}}),
    has_execute: typeof tool.execute === "function",
  }}
}}

// 情境 1：主 Session（沒有 parentID）
process.env.AISTORAGE_ARGV_LOG = '{log_main}'
const ctxMain = {{ sessionID: "ses_main" }}
for (const [name, tool] of Object.entries(plugin.tool)) {{
  try {{
    await tool.execute(
      {{ query: "接續", summary: "做完了", session_id: "ses_target",
         handoff_ids: ["handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV"],
         parts: [{{ title: "甲", summary: "甲的工作" }}] }},
      ctxMain)
  }} catch (e) {{
    out.errors[name] = String(e.message)
  }}
}}

// 情境 2：子 Session（有 parentID）→ 主 Session 限定的工具必須拒絕
process.env.AISTORAGE_ARGV_LOG = '{log_child}'
const ctxChild = {{ sessionID: "ses_child" }}
for (const name of Object.keys(plugin.tool)) {{
  const tool = plugin.tool[name]
  try {{
    await tool.execute({{ query: "q", summary: "s", handoff_ids: [] }}, ctxChild)
    out.errors[name] = null          // 沒拒絕＝有問題，測試再驗一次
  }} catch (e) {{
    out.errors[name] = String(e.message)
  }}
}}

// 情境 3：context 沒有 sessionID → 必須拒絕（不能猜）
const noCtx = {{}}
try {{
  await plugin.tool.aistorage_whoami.execute({{}}, noCtx)
  out.errors.no_ctx = null
}} catch (e) {{
  out.errors.no_ctx = String(e.message)
}}

console.log(JSON.stringify(out))
"""


def _fake_python(tmp_path: Path) -> tuple[Path, Path]:
    """在 tmp 放一個假的 `python`，argv 寫進 log。回傳 (bin 目錄, log 檔)。"""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "argv.log"
    script = bin_dir / "python"
    script.write_text(FAKE_PYTHON, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir, log


def _harness(tmp_path: Path, child_session: bool = False) -> dict:
    """跑 node 載入 plugin，呼叫所有工具，回傳工具資訊、錯誤與 argv log。"""
    bin_dir, log = _fake_python(tmp_path)
    script = tmp_path / "harness.mjs"
    log_main = tmp_path / "argv-main.log"
    log_child = tmp_path / "argv-child.log"
    if child_session:
        sessions = '[{ id: "ses_main" }, { id: "ses_child", parentID: "ses_main" }]'
    else:
        sessions = '[{ id: "ses_main" }]'
    script.write_text(
        textwrap.dedent(f"""
        const _ORIG_FETCH = globalThis.fetch
        globalThis.fetch = async (url) => {{
          if (String(url).endsWith("/session"))
            return {{ ok: true, json: async () => ({sessions}) }}
          return _ORIG_FETCH(url)
        }}
        """) + HARNESS.format(plugin=str(PLUGIN), log_main=str(log_main),
                              log_child=str(log_child)),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    env["AISTORAGE_ARGV_LOG"] = str(log_main)
    env["AISTORAGE_TMP_DIR"] = str(tmp_path / "tmp")
    proc = subprocess.run(
        ["node", "--experimental-strip-types", str(script)],
        capture_output=True, timeout=120, cwd=str(tmp_path), env=env,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-3000:]
    line = [l for l in proc.stdout.decode("utf-8", "replace").splitlines()
            if l.startswith("{")]
    assert line, proc.stdout.decode("utf-8", "replace")[-3000:]
    data = json.loads(line[-1])
    data["_log_main"] = str(log_main)
    data["_log_child"] = str(log_child)
    return data


def _argv_calls(log: Path) -> list[list[str]]:
    """把 log 拆成「每次呼叫一組 argv」。"""
    if not log.is_file():
        return []
    calls: list[list[str]] = []
    current: list[str] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line == "--CALL--":
            if current:
                calls.append(current)
            current = []
        else:
            current.append(line)
    if current:
        calls.append(current)
    return calls


def _argv(tmp_path: Path) -> list[str]:
    """所有呼叫的 argv 串接（看旗標值時夠用）。"""
    out: list[str] = []
    for call in _argv_calls(tmp_path):
        out.extend(call)
    return out


def test_plugin_exposes_every_documented_tool(tmp_path: Path):
    from aistorage.skill.__main__ import build_parser

    skill_commands = set(
        next(a for a in build_parser()._actions if a.dest == "command").choices
    )
    out = _harness(tmp_path)
    assert set(out["tools"]) == set(TOOL_TO_COMMAND)
    for name, info in out["tools"].items():
        assert info["description"], f"{name} 缺少說明"
        assert info["has_schema"], f"{name} 缺少參數 schema"
        assert info["has_execute"], f"{name} 缺少 execute"
        assert TOOL_TO_COMMAND[name] in skill_commands, f"{name} 沒有對應的 Python 子命令"
        # 情境 1（主 Session）不該有任何錯誤
        assert out["errors"][name] is None, f"{name} 在主 Session 失敗：{out['errors'][name]}"


def test_plugin_takes_the_session_id_from_context(tmp_path: Path):
    """`--session` 一定是 context 的 id，不是模型參數裡的 session_id。"""
    out = _harness(tmp_path)
    calls = _argv_calls(Path(out["_log_main"]))
    assert calls, "應該有呼叫到 CLI"
    session_flags = 0
    for argv in calls:
        assert argv[:2] == ["-m", "aistorage.skill"]
        assert "--session" in argv
        assert argv[argv.index("--session") + 1] == "ses_main", "自己的 id 必須來自 context"
        session_flags += 1
        # 模型的 session_id 只能當「目標」，出現在 --target／--to 位置
        for i, flag in enumerate(argv):
            if flag in ("--session", "--target", "--to"):
                assert argv[i + 1] != "ses_target", f"{flag} 竟然用了模型給的 id"
    assert session_flags == len(TOOL_TO_COMMAND)


def test_plugin_argument_quoting(tmp_path: Path):
    """AI 寫的文字被單引號包住，不會變成 shell 結構。"""
    out = _harness(tmp_path)
    calls = _argv_calls(Path(out["_log_main"]))
    queries = [c[c.index("--query") + 1] for c in calls if "--query" in c]
    assert queries, "find 的查詢字串沒有出現在 argv"
    for query in queries:
        assert query.startswith("'") and query.endswith("'"), "參數應該被單引號包住"
    assert "'接續'" in queries


def test_plugin_refuses_main_session_only_tools_in_a_child_session(tmp_path: Path):
    """有 parentID 時，claim／stop 必須被 plugin 擋下（第一層）。"""
    out = _harness(tmp_path, child_session=True)
    for name in MAIN_ONLY:
        msg = out["errors"][name]
        assert msg, f"{name} 在子 Session 沒有被拒絕"
        assert "主 Session" in msg, f"{name} 的拒絕訊息要講清楚原因：{msg}"
    # 其他工具在子 Session 應該照常可用
    for name in set(TOOL_TO_COMMAND) - set(MAIN_ONLY):
        assert out["errors"][name] is None, f"{name} 不該被主 Session 限定擋下"
    # 被擋下來的工具**不應該**真的去呼叫 CLI
    child_calls = [
        " ".join(c) for c in _argv_calls(Path(out["_log_child"]))
    ]
    for command in ("claim", "stop"):
        assert not any(command in c for c in child_calls), \
            f"{command} 在子 Session 仍然呼叫了 CLI"
    # 但子 Session 的 --session 仍然是自己的 id
    for argv in _argv_calls(Path(out["_log_child"])):
        assert argv[argv.index("--session") + 1] == "ses_child"


def test_plugin_refuses_without_a_context_session_id(tmp_path: Path):
    out = _harness(tmp_path)
    assert "sessionID" in (out["errors"]["no_ctx"] or "")
    assert out["errors"]["no_ctx"] is not None


def test_plugin_never_reads_secrets(tmp_path: Path):
    """plugin 的程式碼裡不該有秘密的來源（它只 spawn 本機 python）。"""
    lines = [
        line.split("//", 1)[0]
        for line in PLUGIN.read_text(encoding="utf-8").splitlines()
    ]
    code = "\n".join(lines)
    for word in ("apiKey", "api_key", "token", "PAT", "private_key", "secret",
                 "/secrets", "readFile"):
        assert word not in code, f"plugin 的程式碼裡不該出現 {word}"


def test_skill_doc_exists_and_names_every_tool():
    """skill 說明要真的存在，而且每個工具都寫在裡面。"""
    doc = Path(__file__).resolve().parents[2] / "resident/opencode/skills/aistorage/SKILL.md"
    assert doc.is_file(), "找不到 skill 說明"
    text = doc.read_text(encoding="utf-8")
    for name in TOOL_TO_COMMAND:
        assert name in text, f"skill 說明裡沒有 {name}"
    # 兩條一定要寫進去的規則
    assert "快照時間" in text          # 引用讀到的內容時要帶快照時間
    assert "可能不是最新" in text      # 新鮮度警告要明說
    assert "不要為了讀到更新的內容而要求對方同步" in text   # ADR 0007
    assert "被拒收" in text and "停下" in text             # 拒收就停
    # frontmatter
    assert text.startswith("---\n")


def test_python_side_also_blocks_main_session_only_tools():
    """plugin 與 Python 兩層都要有同一份限制（不能只有一層）。"""
    from aistorage.skill import tools

    src = Path(tools.__file__).read_text(encoding="utf-8")
    assert "def _require_main_session" in src
    # claim 與 stop 都呼叫它
    claim_body = src[src.index("def claim("):src.index("def _handoff_payloads(")]
    stop_body = src[src.index("def stop("):]
    assert "_require_main_session" in claim_body
    assert "_require_main_session" in stop_body
