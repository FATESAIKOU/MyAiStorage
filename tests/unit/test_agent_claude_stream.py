"""B1a: the claude export reads the session in one pass and moves no byte.

The reference is the pre-B1a adapter, imported from a `git archive` of the
commit this slice started on. For a generated batch of session files (fixed
seed, plus the shapes the ticket names: summaries before/after/absent, the
first title at different positions, several timestamps/versions/cwds/models,
isMeta/isCompactSummary, unknown line types, CJK, a big tool_result, broken
lines, a half-written tail, blank lines, non-dict lines) every `Exported`
field and the raw bytes must equal what HEAD produces - including which error
comes out, and with which line number.

`AGORA_B1A_HEAD_FILE` points the reference at another copy of claude.py; the
mutation checks in the B1a report use it to make the reference deliberately
wrong and watch this test go red.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import contextlib
import importlib.util
import io
import json
import os
import random
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agora.agents import claude as C
from agora.agents.base import AgentError

REPO = Path(__file__).resolve().parents[2]

#: The adapter before B1a (`Merge pull request #108`). A fixed commit and not
#: `HEAD`: after PM commits this slice, HEAD is the code under test and the
#: property test would only compare the new implementation with itself.
HEAD_COMMIT = "fa513923f3a1bf2a9851658b1579af5ca1a8bb3d"

SID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CWD = "/golden/b1a"
STAMP = "2026-10-20T00:00:00.000Z"

_UUID_N = 0


def _uuid() -> str:
    global _UUID_N
    _UUID_N += 1
    return f"{_UUID_N:08d}-1111-4222-8333-444444444444"


def _obj(otype, *, timestamp=STAMP, version="2.1.286", cwd=CWD, **kw) -> dict:
    o = {"type": otype, "sessionId": SID, "uuid": _uuid(), "parentUuid": None,
         "isSidechain": False}
    if timestamp is not None:
        o["timestamp"] = timestamp
    if version is not None:
        o["version"] = version
    if cwd is not None:
        o["cwd"] = cwd
    o.update(kw)
    return o


def _line(o: dict) -> str:
    return json.dumps(o, ensure_ascii=False) + "\n"


def _user(text: str, **kw) -> str:
    return _line(_obj("user", message={"role": "user", "content": text}, **kw))


def _assistant(text: str | None = None, model: str | None = "model-a", **kw) -> str:
    content = ([{"type": "text", "text": text}] if text is not None else [])
    message: dict = {"role": "assistant", "content": content}
    if model is not None:
        message["model"] = model
    return _line(_obj("assistant", message=message, **kw))


def _summary(text, **kw) -> str:
    return _line(_obj("summary", summary=text, **kw))


def _half(line: str) -> str:
    """A line cut mid-JSON, the way a writer that is still going leaves it."""
    return line[:40]


@dataclass(frozen=True)
class Case:
    name: str
    content: bytes
    sidecar: tuple[tuple[str, bytes], ...] = field(default=())


def _text_case(name: str, *lines: str,
               sidecar: tuple[tuple[str, bytes], ...] = ()) -> Case:
    return Case(name, "".join(lines).encode("utf-8"), sidecar)


def _cases() -> list[Case]:
    cases: list[Case] = []

    def add(name: str, *lines: str, sidecar: tuple[tuple[str, bytes], ...] = ()) -> None:
        cases.append(_text_case(name, *lines, sidecar=sidecar))

    add("empty")
    add("blank-lines", "\n", "   \n", "\t\n")

    u1, a1 = _user("ZZUSER 第一句"), _assistant("ZZASSIST 回答")
    summary = _summary("ZZSUMMARY 摘要勝出")
    add("summary-first", summary, u1, a1)
    add("summary-middle", u1, summary, a1)
    add("summary-last", u1, a1, summary)
    add("summary-empty-first", _summary(""), u1, _summary("ZZSUMMARY 後來的摘要"), a1)
    add("summary-non-string", _summary(42), u1, a1)
    add("summary-whitespace", _summary("  "), u1, a1)

    add("title-after-noise",
        _user("ZZMETA 這是系統提示", isMeta=True),
        _user("<command-name>/model</command-name>"),
        _user("<local-command-stdout>done</local-command-stdout>"),
        u1, a1)
    add("compact-summary-is-a-title",
        _user("ZZCOMPACT 壓縮摘要", isCompactSummary=True), u1, a1)
    add("title-cap-60", _user("字" * 100), a1)
    add("title-no-user", _assistant("ZZASSIST 只有助手"), _summary("ZZSUMMARY"))

    add("timestamps-first-present",
        _user("ZZUSER 沒有時間戳", timestamp=None),
        _assistant("ZZASSIST 第一個時間戳", timestamp="2026-10-20T01:00:00.000Z"),
        _user("ZZUSER 第二個時間戳", timestamp="2026-10-20T02:00:00.000Z"),
        _assistant("ZZASSIST 第三個時間戳", timestamp="2026-10-20T03:00:00.000Z"))
    add("versions-last-present",
        _user("ZZUSER 沒有版本", version=None),
        _assistant("ZZASSIST 版本一", version="v1"),
        _user("ZZUSER 版本二", version="v2"),
        _assistant("ZZASSIST 沒有版本", version=None),
        _user("ZZUSER 版本三", version="v3"))
    add("cwds-first-present",
        _user("ZZUSER 沒有目錄", cwd=None),
        _assistant("ZZASSIST 目錄一", cwd="/one"),
        _user("ZZUSER 目錄二", cwd="/two"))
    add("models-last-non-empty",
        _user("ZZUSER 開場"),
        _assistant("ZZASSIST 模型一", model="model-one"),
        _assistant("ZZASSIST 空模型", model=""),
        _line(_obj("assistant", message={"role": "assistant", "content": []})),
        _line(_obj("assistant", message="not a dict")),
        _assistant("ZZASSIST 模型三", model="model-three"),
        _assistant("ZZASSIST 沒有模型", model=None))

    add("noise-excluded",
        _user("ZZMETA 系統提示", isMeta=True),
        _user("<command-name>/clear</command-name>"),
        _user("<local-command-caveat>提醒</local-command-caveat>"),
        _user("<local-command-stdout>輸出</local-command-stdout>"),
        u1,
        _line(_obj("user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ZZTOOLOUT"}]})),
        a1,
        _user("ZZCOMPACT 壓縮後仍算訊息", isCompactSummary=True))

    add("silent-and-unknown-types",
        _line(_obj("attachment", attachment={"type": "file"})),
        _line(_obj("queue-operation", operation="enqueue")),
        _line(_obj("progress", content="3/18")),
        _line(_obj("custom-title", title="ZZTITLE")),
        _line(_obj("zzwidget", payload={"x": 1})),
        _line({"sessionId": SID, "uuid": _uuid()}),
        _line({"type": 7, "sessionId": SID, "uuid": _uuid()}),
        u1)

    big = "\n".join(f"ZZBIG 第 {n} 行：讀取完成，exit code 0；" + "x" * 32
                    for n in range(1200))          # well past one line of a real tool result
    add("cjk-big-tool-result",
        u1,
        _line(_obj("assistant", message={"role": "assistant", "model": "model-a",
                                         "content": [
                                             {"type": "text", "text": "ZZ 我先看一下。"},
                                             {"type": "tool_use", "id": "toolu_big",
                                              "name": "Read",
                                              "input": {"file_path": "/golden/b1a/big.py"}}]})),
        _line(_obj("user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_big", "content": big}]})),
        _assistant("ZZASSIST 看完了。"))

    bad_mid = '{"type": "user", "message": '
    add("broken-line-mid", u1, a1, bad_mid + "\n", u1, a1)
    add("broken-line-last", u1, a1, bad_mid)
    add("half-line-last", u1, a1, _half(_assistant("ZZASSIST 半行")))
    add("blank-lines-do-not-number",
        u1, "\n   \n", bad_mid + "\n", "\n", a1)

    add("non-dict-first", "42\n", u1, a1)
    add("non-dict-mid", u1, "null\n", a1)
    add("non-dict-last", u1, a1, "[1, 2]\n")
    add("non-dict-before-broken", u1, "42\n", bad_mid + "\n", a1)
    add("non-dict-before-half", u1, "42\n", _half(a1))

    add("no-trailing-newline", _user("ZZUSER 最後沒有換行").rstrip("\n"))
    add("crlf", "".join([u1, a1]).replace("\n", "\r\n"))
    add("cr-only", "".join([u1, a1]).replace("\n", "\r"))
    # Review 中1: the line-splitting rule is "\n" only. These characters sit raw
    # (ensure_ascii=False) inside a JSON string and must neither split the line
    # (str.splitlines would) nor change the packed raw.
    add("u2028-in-field", _user("ZZUSER 前 後"), _assistant("ZZASSIST 前 後"))
    add("u0085-in-field", _user("ZZUSER 前\u0085後"), _assistant("ZZASSIST 前\u0085後"))
    add("u2029-in-field", _user("ZZUSER a b"), a1)
    # A BOM at the start: HEAD reads it into the first line, json.loads refuses
    # it, and the error (type and text) has to come out the same.
    cases.append(Case("bom-start", b"\xef\xbb\xbf" + "".join([u1, a1]).encode("utf-8")))
    add("trailing-spaces", u1.rstrip("\n") + "   \n", a1)

    add("sidecar",
        u1, a1,
        sidecar=(("subagents/agent-0123456789abcdef.jsonl",
                  _assistant("ZZSUB 子代理").encode("utf-8")),
                 ("blob.bin", bytes(range(256))),
                 ("nested/note.txt", "ZZ 附註\n".encode("utf-8"))))

    # The ticket's "random batch": fixed seed, the shapes mixed in at random.
    rng = random.Random(0xB1A)
    special = [
        lambda: _summary(f"ZZSUMMARY 隨機 {rng.randrange(100)}"),
        lambda: _summary(""),
        lambda: _summary(42),
        lambda: _user("ZZMETA 隨機", isMeta=True),
        lambda: _user("ZZCOMPACT 隨機", isCompactSummary=True),
        lambda: _user("<command-name>/random</command-name>"),
        lambda: _line(_obj("zzwidget", n=rng.randrange(100))),
        lambda: _line(_obj("attachment")),
        lambda: "42\n",
        lambda: bad_mid + "\n",
        lambda: _user(f"ZZUSER 隨機中文 {rng.randrange(100)}",
                      timestamp=f"2026-10-20T0{rng.randrange(10)}:00:00.000Z",
                      version=f"v{rng.randrange(10)}", cwd=f"/random/{rng.randrange(10)}"),
        lambda: _assistant(f"ZZASSIST 隨機 {rng.randrange(100)}",
                           model=f"model-{rng.randrange(10)}"),
        lambda: _assistant(None, model=None),
        lambda: "\n",
    ]
    for n in range(18):
        lines = [rng.choice(special)() for _ in range(rng.randint(2, 12))]
        if rng.random() < 0.2:
            lines.append(_half(rng.choice(special)()))
        cases.append(_text_case(f"random-{n}", *lines))

    return cases


CASES = _cases()


@pytest.fixture(scope="session")
def head_claude(tmp_path_factory):
    """The pre-B1a claude.py, imported under its own module name."""
    override = os.environ.get("AGORA_B1A_HEAD_FILE")
    if override:
        source = Path(override).read_bytes()
    else:
        try:
            proc = subprocess.run(["git", "archive", HEAD_COMMIT, "src/agora/agents/claude.py"],
                                  cwd=REPO, capture_output=True)
        except OSError as e:
            pytest.skip(f"git 不可用：{e}")
        if proc.returncode != 0:
            pytest.skip(f"git archive {HEAD_COMMIT} 不可用：{proc.stderr.decode('utf-8', 'replace')[-200:]}")
        with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
            source = tar.extractfile("src/agora/agents/claude.py").read()
    path = tmp_path_factory.mktemp("head-claude") / "claude_b1a_head.py"
    path.write_bytes(source)
    spec = importlib.util.spec_from_file_location("agora_b1a_head_claude", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _observe(module, path: Path, sidecar: Path, *, head: bool) -> dict:
    """One export, reduced to the things the two versions have to agree on."""
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        try:
            main = module._read_lines(path)
            if head:
                exported = module._exported(SID, main, sidecar)
            else:
                exported = module._exported(SID, main, sidecar, label=path.name)
        except Exception as exc:                    # noqa: BLE001 - the type is the assertion
            return {"stderr": stderr.getvalue(),
                    "error": (type(exc).__name__, str(exc))}
    return {
        "stderr": stderr.getvalue(),
        "main": main,
        "raw": exported.raw,
        "fields": {k: getattr(exported, k) for k in
                   ("session_id", "dir", "title", "created_at", "agent_version",
                    "message_count", "model", "turns")},
    }


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_the_new_export_is_head_bit_for_bit(case, head_claude, tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_bytes(case.content)
    sidecar = path.parent / "session"
    for rel, data in case.sidecar:
        dest = sidecar / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    assert _observe(C, path, sidecar, head=False) == _observe(head_claude, path, sidecar, head=True)


def test_a_non_dict_line_fails_where_head_fails(tmp_path):
    """`42` is valid JSON, so validation lets it through; HEAD's first `.get` on
    it is what raises. The streaming version must raise the same type and text."""
    path = tmp_path / "session.jsonl"
    path.write_text('{"type": "user", "message": "hi"}\n42\n', encoding="utf-8")
    with pytest.raises(AttributeError) as exc:
        C._exported(SID, C._read_lines(path), path.parent / "session", label=path.name)
    assert str(exc.value) == "'int' object has no attribute 'get'"


def test_a_broken_line_wins_over_a_non_dict_line(tmp_path):
    """HEAD validates every line before it touches a parsed object, so a syntax
    error below a non-dict line is the error that comes out. A naive streaming
    loop would hit the non-dict first."""
    path = tmp_path / "session.jsonl"
    path.write_text('{"type": "user", "message": "hi"}\n42\n{"type": "user", \n'
                    + _assistant("ZZASSIST 最後一行是好的"), encoding="utf-8")
    with pytest.raises(AgentError, match=r"第 3 行解析失敗"):
        C._exported(SID, C._read_lines(path), path.parent / "session", label=path.name)


def test_an_unhashable_type_fails_like_head(tmp_path):
    """A dict whose `type` is a list is valid JSON; HEAD's `_turns` hashes it and
    raises TypeError. The deferred post-parse error must keep that type/text."""
    path = tmp_path / "session.jsonl"
    path.write_text('{"type": []}\n', encoding="utf-8")
    with pytest.raises(TypeError, match="unhashable type: 'list'"):
        C._exported(SID, C._read_lines(path), path.parent / "session", label=path.name)


def test_a_post_parse_error_waits_for_a_later_syntax_error(tmp_path):
    """The same precedence for the other post-parse errors: an unhashable `type`
    would fail `_take_turn`, but HEAD's validation runs first, so the broken line
    below it is what comes out."""
    path = tmp_path / "session.jsonl"
    path.write_text('{"type": []}\n{"type": "user", \n'
                    + _user("ZZUSER 最後一行是好的"), encoding="utf-8")
    with pytest.raises(AgentError, match=r"第 2 行解析失敗"):
        C._exported(SID, C._read_lines(path), path.parent / "session", label=path.name)


def test_the_file_is_read_one_line_at_a_time(tmp_path, monkeypatch):
    """The whole-file read (and its split copy) is the 1× the slice removes."""
    path = tmp_path / "session.jsonl"
    path.write_text('{"type": "user", "message": "hi"}\n', encoding="utf-8")

    def refuse(*_args, **_kwargs):
        raise AssertionError("_read_lines must not read the whole file")

    monkeypatch.setattr(Path, "read_text", refuse)
    main = C._read_lines(path)
    assert main == ['{"type": "user", "message": "hi"}']


def test_each_line_is_parsed_once(tmp_path, monkeypatch):
    lines = [_user("ZZUSER 一"), _assistant("ZZASSIST 一"), _summary("ZZSUMMARY")]
    path = tmp_path / "session.jsonl"
    path.write_text("".join(lines), encoding="utf-8")
    calls = []
    real_loads = json.loads

    def counted(*args, **kwargs):
        calls.append(1)
        return real_loads(*args, **kwargs)

    monkeypatch.setattr(C.json, "loads", counted)
    C._exported(SID, C._read_lines(path), path.parent / "session", label=path.name)
    assert len(calls) == len(lines)


def test_a_non_utf8_file_keeps_the_existing_message(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_bytes(b'{"type": "user", "message": "hi"}\n\xff\xfe')
    with pytest.raises(AgentError) as exc:
        C._read_lines(path)
    assert str(exc.value).startswith(f"{path.name} 不是 UTF-8：")


def test_a_large_non_utf8_file_reports_the_whole_file_position(tmp_path):
    """The text wrapper's own error says 1696 (inside its 8 KiB buffer) for a bad
    byte at 100000; the existing message is HEAD's whole-file position."""
    path = tmp_path / "session.jsonl"
    path.write_bytes(b"a" * 100_000 + b"\xff" + b"b" * 1_000 + b"\n")
    with pytest.raises(AgentError) as exc:
        C._read_lines(path)
    assert "position 100000" in str(exc.value)
