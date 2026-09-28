"""plugin 的冒煙測試。

plugin 是 TypeScript，邏輯刻意很少，所以測試重點是**契約**：
- 工具名稱、說明、參數 schema、execute 都在；
- 工具對應到真的 Python 子命令：`agora_*` → `python -m aistorage.agora_cli`，
  `aistorage_*` → `python -m aistorage.skill`；
- Session id 由 `context.sessionID` 帶入，**模型的參數裡沒有「自己」這個 id**
  （`agora_read`／`aistorage_reference` 的 `session_id` 是**目標**）；
- 主 Session 限定的工具有 parentID 就拒絕（plugin 這是第一層，Python 是第二層）；
- 參數經過單引號跳脫，不會變成 shell 結構；
- **沒有 claim 工具**：認領由 `agora_checkout` 一併登記（ADR 0010）。

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

#: 工具名稱 → (CLI 模組, 子命令)。`agora_*` 走 Agora 的單一入口，
#: `aistorage_*` 走住民工具的 CLI。
TOOL_TO_COMMAND = {
    "agora_find": ("aistorage.agora_cli", "find"),
    "agora_show": ("aistorage.agora_cli", "show"),
    "agora_read": ("aistorage.agora_cli", "read"),
    "agora_handoff": ("aistorage.agora_cli", "handoff"),
    "agora_checkout": ("aistorage.agora_cli", "checkout"),
    "aistorage_whoami": ("aistorage.skill", "whoami"),
    "aistorage_split": ("aistorage.skill", "split"),
    "aistorage_handoff_end": ("aistorage.skill", "handoff-end"),
    "aistorage_reference": ("aistorage.skill", "reference"),
    "aistorage_list_handoffs": ("aistorage.skill", "list-handoffs"),
    "aistorage_stop": ("aistorage.skill", "stop"),
}

#: 只有主 Session 能用的工具（plugin 這一層是第一道防線）
MAIN_ONLY = ("aistorage_stop",)

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
out.errors_main = {{}}
for (const [name, tool] of Object.entries(plugin.tool)) {{
    try {{
      await tool.execute(
        {{ query: "接續", summary: "做完了", session_id: "ses_target",
           startpoints: ["handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV"],
           tasks: [{{ title: "甲", summary: "甲的工作" }}],
           task: "接手甲的工作",
           kind: "link", name: "架構報告", link: "https://example.invalid/report",
           repo: "org/repo", path: "docs/report.md", case_id: "c1",
           parts: [{{ title: "甲", summary: "甲的工作" }}] }},
        ctxMain)
      out.errors_main[name] = null
    }} catch (e) {{
      out.errors_main[name] = String(e.message)
    }}
  }}

// 情境 2：子 Session（有 parentID）→ 主 Session 限定的工具必須拒絕
process.env.AISTORAGE_ARGV_LOG = '{log_child}'
const ctxChild = {{ sessionID: "ses_child" }}
out.errors = {{}}
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


def _harness(tmp_path: Path, child_session: bool = False,
             sessions_status: int = 200, omit_self: bool = False,
             recover_second_call: bool = False) -> dict:
    """跑 node 載入 plugin，呼叫所有工具，回傳工具資訊、錯誤與 argv log。"""
    bin_dir, log = _fake_python(tmp_path)
    script = tmp_path / "harness.mjs"
    log_main = tmp_path / "argv-main.log"
    log_child = tmp_path / "argv-child.log"
    if child_session:
        sessions = '[{ id: "ses_main" }, { id: "ses_child", parentID: "ses_main" }]'
    elif omit_self:
        sessions = '[{ id: "ses_other" }]'
    else:
        sessions = '[{ id: "ses_main" }]'
    script.write_text(
        textwrap.dedent(f"""
        const _ORIG_FETCH = globalThis.fetch
        let _n = 0
        globalThis.fetch = async (url) => {{
          if (String(url).endsWith("/session")) {{
            _n++
            if ({"true" if recover_second_call else "false"} && _n > 0) {{
              return {{ ok: true, json: async () => ({sessions}) }}
            }}
            if ({int(sessions_status)} !== 200) {{
              return {{ ok: false, status: {int(sessions_status)} }}
            }}
            return {{ ok: true, json: async () => ({sessions}) }}
          }}
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


def _subcommands(module: str) -> set:
    if module == "aistorage.skill":
        from aistorage.skill.__main__ import build_parser
    else:
        from aistorage.agora_cli.__main__ import build_parser
    return set(next(a for a in build_parser()._actions
                    if a.dest == "command").choices)


def test_plugin_exposes_every_documented_tool(tmp_path: Path):
    out = _harness(tmp_path)
    assert set(out["tools"]) == set(TOOL_TO_COMMAND)
    for name, info in out["tools"].items():
        assert info["description"], f"{name} 缺少說明"
        assert info["has_schema"], f"{name} 缺少參數 schema"
        assert info["has_execute"], f"{name} 缺少 execute"
        module, command = TOOL_TO_COMMAND[name]
        assert command in _subcommands(module), f"{name} 沒有對應的 Python 子命令"
        # 情境 1（主 Session）不該有任何錯誤
        assert out["errors_main"][name] is None, \
            f"{name} 在主 Session 失敗：{out['errors_main'][name]}"


def test_plugin_has_no_claim_tool(tmp_path: Path):
    """AI 不再自己認領：認領由 `agora_checkout` 一併登記（ADR 0010）。"""
    out = _harness(tmp_path)
    assert "aistorage_claim" not in out["tools"]
    assert "agora_claim" not in out["tools"]
    assert "agora_checkout" in out["tools"]


def _call_of(calls: list[list[str]], module: str, command: str) -> list[str]:
    """找出某個工具實際送給 CLI 的 argv。"""
    for call in calls:
        if call[:3] == ["-m", module, command]:
            return call
    raise AssertionError(f"沒有找到 `python -m {module} {command}` 的呼叫：{calls}")


def test_plugin_takes_the_session_id_from_context(tmp_path: Path):
    """自己的 id 必須來自 context，模型的 `session_id` 只能是「目標」。

    `aistorage_*` 走 `--session <context id>`；`agora_*` 沒有 `--session`
    （`agora handoff` 的位置參數、`agora read --from` 都由 context 帶入）。
    """
    out = _harness(tmp_path)
    calls = _argv_calls(Path(out["_log_main"]))
    assert calls, "應該有呼叫到 CLI"
    for name, (module, command) in TOOL_TO_COMMAND.items():
        call = _call_of(calls, module, command)
        if name.startswith("aistorage_"):
            assert "--session" in call, f"{name} 沒有帶 --session"
            assert call[call.index("--session") + 1] == "ses_main", \
                f"{name}：自己的 id 必須來自 context"
        else:
            assert "--session" not in call, \
                f"{name} 是 agora CLI，沒有 --session 這個參數"
        # 模型的 session_id 只能當「目標」，絕不能是「自己」
        if "--from" in call:
            assert call[call.index("--from") + 1] == "ses_main"
        for flag in ("--target", "--to"):
            if flag in call:
                assert call[call.index(flag) + 1] == "ses_target"


def test_agora_tools_use_the_single_agora_entry_point(tmp_path: Path):
    """`agora_*` 全部走 `python -m aistorage.agora_cli`（單一入口）。"""
    out = _harness(tmp_path)
    calls = _argv_calls(Path(out["_log_main"]))
    for name in [n for n in TOOL_TO_COMMAND if n.startswith("agora_")]:
        _module, command = TOOL_TO_COMMAND[name]
        call = _call_of(calls, "aistorage.agora_cli", command)
        assert call[:2] == ["-m", "aistorage.agora_cli"], call


def test_agora_checkout_passes_startpoints_and_task(tmp_path: Path):
    """`agora checkout` 的起點是位置參數，任務走 `--task`（不是引號字串）。"""
    out = _harness(tmp_path)
    calls = _argv_calls(Path(out["_log_main"]))
    call = _call_of(calls, "aistorage.agora_cli", "checkout")
    assert "handoff:01ARZ3NDEKTSV4RRFFQ69G5FAV" in call
    assert call[call.index("--task") + 1] == "接手甲的工作"
    # 輸出目錄一定要有，否則 CLI 會直接報錯（起點包要有地方放）
    assert "-o" in call and call[call.index("-o") + 1]


def test_agora_handoff_passes_the_current_session_as_the_target(tmp_path: Path):
    """`agora handoff` 的位置參數是「要交出的 Session」＝context 的 id。"""
    out = _harness(tmp_path)
    calls = _argv_calls(Path(out["_log_main"]))
    call = _call_of(calls, "aistorage.agora_cli", "handoff")
    assert call[3] == "ses_main", "要交出的 Session 必須是 context 的 id"
    assert "--task" in call and call[call.index("--task") + 1] == "甲"


def test_plugin_arguments_reach_python_verbatim(tmp_path: Path):
    """`spawn` 不經過 shell，所以**不能**加 shell 引號（review-g5-6 H2）。

    加了單引號，Python 收到的是 `'handoff:01…'`，格式檢查直接判成找不到。
    """
    out = _harness(tmp_path)
    calls = _argv_calls(Path(out["_log_main"]))
    # `agora find` 的關鍵字是**位置參數**（`agora find [關鍵字]`）
    queries = [c[3] for c in calls if c[:3] == ["-m", "aistorage.agora_cli", "find"]]
    assert queries, "find 的查詢字串沒有出現在 argv"
    assert "接續" in queries, f"查詢字串被改動了：{queries}"
    for call in calls:
        for value in call:
            assert not (value.startswith("'") and value.endswith("'") and len(value) > 1), \
                f"參數被 shell 引號包起來了：{value}"
    # 起點（handoff id）必須是乾淨的 handoff:<ULID>，位置參數
    startpoints = [c[3] for c in calls
                   if c[:3] == ["-m", "aistorage.agora_cli", "checkout"]
                   and c[3].startswith("handoff:")]
    assert startpoints, "checkout 的起點沒有出現在 argv"
    import re
    for hid in startpoints:
        assert re.fullmatch(r"handoff:[0-9A-HJKMNP-TV-Z]{26}", hid), f"id 被引號汙染：{hid}"


def test_plugin_does_not_lose_arguments_with_quotes_and_newlines(tmp_path: Path):
    """含引號、換行、`$`、反引號的 AI 文字要原樣傳過去（不經 shell 就不會被展開）。"""
    out = _harness(tmp_path)
    calls = _argv_calls(Path(out["_log_main"]))
    summaries = [c[c.index("--summary") + 1] for c in calls if "--summary" in c]
    assert summaries, "handoff-end 的 summary 沒有出現在 argv"
    for value in summaries:
        assert value == "做完了", f"summary 被改動了：{value!r}"


def test_plugin_refuses_when_the_session_list_query_fails(tmp_path: Path):
    """查不到 Session 清單 → 拒絕，不放行（review-g5-6 H1）。"""
    out = _harness(tmp_path, sessions_status=503)
    for name in MAIN_ONLY:
        msg = out["errors"][name] or ""
        assert "拒絕執行" in msg, f"{name} 在查詢失敗時沒有拒絕：{msg}"
    calls = [" ".join(c) for c in _argv_calls(Path(out["_log_main"]))]
    # 只有主 Session 限定的工具受這個檢查保護；它一個都不該被呼叫
    assert not any("stop" in c for c in calls), "stop 在查詢失敗時仍然呼叫了 CLI"


def test_plugin_refuses_when_itself_is_missing_from_the_list(tmp_path: Path):
    """清單裡找不到自己 → 拒絕（不能當成「沒有 parent 所以是主 Session」）。"""
    out = _harness(tmp_path, omit_self=True)
    for name in MAIN_ONLY:
        msg = out["errors"][name] or ""
        assert "找不到自己" in msg, f"{name} 應該因為找不到自己而拒絕：{msg}"


def test_plugin_recovers_after_a_failed_query(tmp_path: Path):
    """失敗的查詢**不能被快取**：下一次要重新問，API 恢復後就能用。"""
    # 第一次查詢失敗（拒絕），第二次清單正常（放行）
    out = _harness(tmp_path, sessions_status=503, recover_second_call=True)
    assert out["errors"].get("aistorage_stop"), "第一次應該被拒絕"
    calls = _argv_calls(Path(out["_log_main"]))
    assert calls, "第二次查詢恢復之後應該要能呼叫 CLI"
    stop_call = _call_of(calls, "aistorage.skill", "stop")
    assert stop_call[stop_call.index("--session") + 1] == "ses_main"


def test_plugin_refuses_main_session_only_tools_in_a_child_session(tmp_path: Path):
    """有 parentID 時，stop 必須被 plugin 擋下（第一層）。"""
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
    for command in ("stop",):
        assert not any(command in c for c in child_calls), \
            f"{command} 在子 Session 仍然呼叫了 CLI"
    # 但子 Session 的 --session 仍然是自己的 id（agora CLI 沒有 --session）
    for argv in _argv_calls(Path(out["_log_child"])):
        if argv[:2] == ["-m", "aistorage.skill"]:
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
    # 拒收就停：AI 不再自己 claim，所以這條規則落在 `agora_checkout` 上
    # （被拒 → 不產出起點包 → 回報使用者、不要重試同一批）
    assert "被拒就不產出" in text and "停下來" in text
    # frontmatter
    assert text.startswith("---\n")


def test_python_side_also_blocks_main_session_only_tools():
    """plugin 與 Python 兩層都要有同一份限制（不能只有一層）。"""
    from aistorage.skill import tools

    src = Path(tools.__file__).read_text(encoding="utf-8")
    assert "def _require_main_session" in src
    stop_body = src[src.index("def stop("):]
    assert "_require_main_session" in stop_body


def test_skill_cli_has_no_claim_subcommand():
    """AI 不再自己認領：`aistorage.skill` 沒有 claim 子命令（ADR 0010）。"""
    from aistorage.skill.__main__ import build_parser

    sub = next(a for a in build_parser()._actions if a.dest == "command")
    assert "claim" not in sub.choices
