"""R1: the claude export streams the session in one pass and moves no byte.

The reference is the three-stage adapter this slice started on (`_read_lines` +
`_scan` + `_pack_raw_file`), read from tests/fixtures/claude_r1_reference_985afd7.py.txt (the file at the commit it was
cut from; no git needed, and a missing reference fails instead of skipping). For a generated batch of session files (fixed seed, plus the shapes
the ticket names: summaries before/after/absent, the first title at different
positions, several timestamps/versions/cwds/models, isMeta/isCompactSummary,
unknown line types, CJK, a big tool_result, broken lines, a half-written tail,
blank lines, non-dict lines, `/clear` switching files, sidecars) every
`Exported` field, the raw bytes, `main_end`, `stable_at(n)` and the digests
must equal what the three-stage version produces - including which error comes
out, and with which line number.

`AGORA_B1A_HEAD_FILE` points the reference at another copy of claude.py; the
mutation checks in the R1 report use it to make the reference deliberately
wrong and watch this test go red.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agora.agents import claude as C
from agora.agents.base import AgentError

REPO = Path(__file__).resolve().parents[2]

#: The adapter before R1 (`Merge pull request #147`, the design merge). A fixed
#: commit and not `HEAD`: after PM commits this slice, HEAD is the code under
#: test and the property test would only compare the new implementation with
#: itself. This one still has `_read_lines` + `_scan` + `_pack_raw_file`.
HEAD_COMMIT = "985afd7"

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

    # `/clear` keeps the same file going here; the switch to a new session file
    # is its own test below (the ticket's「/clear 換檔」).
    add("clear-keeps-writing",
        u1,
        _user("<command-name>/clear</command-name>"),
        _user("<local-command-stdout>Cleared conversation</local-command-stdout>"),
        a1, u1, a1)

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


REFERENCE_FIXTURE = REPO / "tests" / "fixtures" / "claude_r1_reference_985afd7.py.txt"


@pytest.fixture(scope="session")
def reference_claude(tmp_path_factory):
    """The three-stage claude.py, imported under its own module name.

    From `tests/fixtures/` (the file at `HEAD_COMMIT`), not from git history: a copy without `.git`
    runs these tests too. A reference that cannot be read is a **failure** - a skip would let the
    whole equivalence suite quietly stop proving anything.
    """
    override = os.environ.get("AGORA_B1A_HEAD_FILE")
    target = Path(override) if override else REFERENCE_FIXTURE
    try:
        source = target.read_bytes()
    except OSError as e:
        pytest.fail(f"參考版 claude.py 讀不到（{target}）：{e}", pytrace=False)
    path = tmp_path_factory.mktemp("reference-claude") / "claude_r1_reference.py"
    path.write_bytes(source)
    spec = importlib.util.spec_from_file_location("agora_r1_reference_claude", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module      # dataclasses resolves postponed annotations by name
    spec.loader.exec_module(module)
    return module


#: The envelope's opening bytes, as both paths write them.
HEAD_BYTES = b'{"format": "claude-jsonl/1", "main": ['


def _shape_of_raw(main: list[str], raw: bytes) -> dict:
    """`main_end`/`stable_at`/digests of the old path, read off the bytes it wrote.

    Computed from the raw and the lines it packed, not from the new adapter: the
    element boundaries are the serializer's own pieces (the same `json.dumps`
    `_pack_raw_file` called), and every digest is a slice of the actual bytes.
    """
    positions, pos = [0], len(HEAD_BYTES)
    for i, line in enumerate(main):
        if i:
            pos += 2
        pos += len(json.dumps(line, ensure_ascii=False).encode("utf-8"))
        positions.append(pos)
    return {
        "main_end": pos,
        "main_md5": hashlib.md5(raw[:pos]).hexdigest(),
        "stable_at": positions,
        "stable_md5": [hashlib.md5(raw[:p]).hexdigest() for p in positions],
    }


def _observe(module, path: Path, sidecar: Path, *, old: bool) -> dict:
    """One export, reduced to the things the two passes have to agree on."""
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        try:
            if old:
                main = module._read_lines(path)
                exported = module._exported(SID, main, sidecar, label=path.name)
                shape = _shape_of_raw(main, exported.raw)
            else:
                exported, packed = module._exported(SID, path, sidecar,
                                                    label=path.name, capture=True)
                shape = {"main_end": packed.main_end, "main_md5": packed.main_md5,
                         "stable_at": list(packed.stable_at),
                         "stable_md5": [packed.stable_md5(n)
                                        for n in range(len(packed.stable_at))]}
        except Exception as exc:                    # noqa: BLE001 - the type is the assertion
            return {"stderr": stderr.getvalue(),
                    "error": (type(exc).__name__, str(exc))}
    return {
        "stderr": stderr.getvalue(),
        "raw": exported.raw,
        "raw_md5": exported.raw_md5,
        "raw_size": exported.raw_size,
        "shape": shape,
        "fields": {k: getattr(exported, k) for k in
                   ("session_id", "dir", "title", "created_at", "agent_version",
                    "message_count", "model", "turns")},
    }


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_the_new_export_is_head_bit_for_bit(case, reference_claude, tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_bytes(case.content)
    sidecar = path.parent / "session"
    for rel, data in case.sidecar:
        dest = sidecar / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    assert _observe(C, path, sidecar, old=False) == _observe(reference_claude, path, sidecar, old=True)


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_stream_raw_and_pack_raw_produce_identical_bytes(case, tmp_path):
    """Parity check between _stream_raw and _pack_raw on the same session input.

    Covers sidecar (aux), CJK content, and incomplete / broken trailing lines.
    Cases where session parsing fails do not produce a raw export and are skipped.
    """
    path = tmp_path / "session.jsonl"
    path.write_bytes(case.content)
    sidecar = path.parent / "session"
    for rel, data in case.sidecar:
        dest = sidecar / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)

    out_path = tmp_path / "streamed.raw"
    try:
        C._stream_raw(path, sidecar, out_path, label=path.name)
    except (C.AgentError, AttributeError) as e:
        pytest.skip(f"invalid session file does not produce raw export: {e!r}")

    streamed = out_path.read_bytes()
    main = list(C._iter_lines(path))
    if main:
        try:
            json.loads(main[-1])
        except json.JSONDecodeError:
            main.pop()

    aux = {}
    if sidecar.is_dir():
        for p in sorted(sidecar.rglob("*")):
            if p.is_file():
                rel = p.relative_to(sidecar).as_posix()
                raw = p.read_bytes()
                try:
                    aux[rel] = raw.decode("utf-8")
                except UnicodeDecodeError:
                    aux[rel] = {"$base64": base64.b64encode(raw).decode("ascii")}

    packed = C._pack_raw(main, aux)
    assert streamed.startswith(HEAD_BYTES)
    assert streamed == packed

    unpacked_main, unpacked_aux = C._unpack_raw(streamed)
    assert unpacked_main == main
    assert unpacked_aux == aux


def test_stream_and_pack_raw_parity_explicit_composite(tmp_path):
    """Explicit parity check covering aux, CJK, and a truncated last line together."""
    u1 = _user("ZZUSER 第一句繁體中文")
    a1 = _assistant("ZZASSIST 這是包含日文「こんにちは」與韓文「안녕하세요」的回答")
    truncated_tail = _half(_assistant("ZZASSIST 截斷的最後一行"))

    path = tmp_path / "session.jsonl"
    path.write_text("".join([u1, a1, truncated_tail]), encoding="utf-8")

    sidecar = tmp_path / "session"
    sidecar.mkdir()
    (sidecar / "subagents").mkdir()
    sub_line = _assistant("ZZSUB 子代理中文字串")
    (sidecar / "subagents" / "sub.jsonl").write_text(sub_line, encoding="utf-8")
    (sidecar / "binary.bin").write_bytes(b"\x00\xff\xfe\x01\x02\x03")

    out_path = tmp_path / "out.raw"
    C._stream_raw(path, sidecar, out_path, label=path.name)
    streamed = out_path.read_bytes()

    main = [u1.rstrip("\n"), a1.rstrip("\n")]
    aux = {
        "binary.bin": {"$base64": base64.b64encode(b"\x00\xff\xfe\x01\x02\x03").decode("ascii")},
        "subagents/sub.jsonl": sub_line,
    }

    packed = C._pack_raw(main, aux)
    assert streamed.startswith(HEAD_BYTES)
    assert streamed == packed

    unpacked_main, unpacked_aux = C._unpack_raw(streamed)
    assert unpacked_main == main
    assert unpacked_aux == aux


def test_a_clear_switch_reads_only_the_named_file(reference_claude, tmp_path, monkeypatch):
    """`/clear` starts a new session file; the export of either id reads its own."""
    monkeypatch.setenv("AGORA_CLAUDE_HOME", str(tmp_path))
    pdir = tmp_path / ".claude" / "projects" / C.encode_project_dir(CWD)
    pdir.mkdir(parents=True)
    old_id, new_id = "clear-old-0000-4000-8000-000000000001", "clear-new-0000-4000-8000-000000000002"
    old_path, new_path = pdir / f"{old_id}.jsonl", pdir / f"{new_id}.jsonl"
    old_path.write_text("".join([_user("ZZUSER 清掉之前"), _assistant("ZZASSIST 之前"),
                                 _user("<command-name>/clear</command-name>")]), encoding="utf-8")
    new_path.write_text("".join([_user("ZZUSER 清掉之後"), _assistant("ZZASSIST 之後")]),
                        encoding="utf-8")
    for session_id, path in ((old_id, old_path), (new_id, new_path)):
        new = C.ClaudeAgent().export(session_id, CWD)
        old = reference_claude.ClaudeAgent().export(session_id, CWD)
        assert new.raw == old.raw
        assert new.raw_md5 == old.raw_md5
        assert {k: getattr(new, k) for k in ("title", "dir", "message_count", "turns")} == \
            {k: getattr(old, k) for k in ("title", "dir", "message_count", "turns")}
        _new_export, packed = C._exported(session_id, path, path.parent / session_id,
                                          capture=True)
        assert {"main_end": packed.main_end, "main_md5": packed.main_md5,
                "stable_at": list(packed.stable_at),
                "stable_md5": [packed.stable_md5(n) for n in range(len(packed.stable_at))]} \
            == _shape_of_raw(reference_claude._read_lines(path), old.raw)


def test_the_sidecar_order_is_stable(reference_claude, tmp_path):
    """Aux is serialized in `sorted(rglob)` order, so the same sidecar is the same
    bytes twice and an added file does not reorder the existing entries (B.2)."""
    path = tmp_path / "session.jsonl"
    path.write_text(_user("ZZUSER") + _assistant("ZZASSIST"), encoding="utf-8")
    sidecar = tmp_path / "session"
    sidecar.mkdir()
    for rel in ("b.txt", "a/one.txt", "a/two.txt"):
        dest = sidecar / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(f"ZZ {rel}\n", encoding="utf-8")
    first = C._exported(SID, path, sidecar)[0].raw
    second = C._exported(SID, path, sidecar)[0].raw
    assert first == second
    before = json.loads(first)["aux"]
    (sidecar / "a" / "zero.txt").write_text("ZZ zero\n", encoding="utf-8")
    after = json.loads(C._exported(SID, path, sidecar)[0].raw)["aux"]
    assert [k for k in after if k in before] == list(before)   # relative order kept
    assert list(after) == sorted(after)


def test_a_non_dict_line_fails_where_head_fails(tmp_path):
    """`42` is valid JSON, so validation lets it through; HEAD's first `.get` on
    it is what raises. The streaming version must raise the same type and text."""
    path = tmp_path / "session.jsonl"
    path.write_text('{"type": "user", "message": "hi"}\n42\n', encoding="utf-8")
    with pytest.raises(AttributeError) as exc:
        C._exported(SID, path, path.parent / "session", label=path.name)
    assert str(exc.value) == "'int' object has no attribute 'get'"


def test_a_broken_line_wins_over_a_non_dict_line(tmp_path):
    """HEAD validates every line before it touches a parsed object, so a syntax
    error below a non-dict line is the error that comes out. A naive streaming
    loop would hit the non-dict first."""
    path = tmp_path / "session.jsonl"
    path.write_text('{"type": "user", "message": "hi"}\n42\n{"type": "user", \n'
                    + _assistant("ZZASSIST 最後一行是好的"), encoding="utf-8")
    with pytest.raises(AgentError, match=r"第 3 行解析失敗"):
        C._exported(SID, path, path.parent / "session", label=path.name)


def test_an_unhashable_type_fails_like_head(tmp_path):
    """A dict whose `type` is a list is valid JSON; HEAD's `_turns` hashes it and
    raises TypeError. The deferred post-parse error must keep that type/text."""
    path = tmp_path / "session.jsonl"
    path.write_text('{"type": []}\n', encoding="utf-8")
    with pytest.raises(TypeError, match="unhashable type: 'list'"):
        C._exported(SID, path, path.parent / "session", label=path.name)


def test_a_post_parse_error_waits_for_a_later_syntax_error(tmp_path):
    """The same precedence for the other post-parse errors: an unhashable `type`
    would fail `_take_turn`, but HEAD's validation runs first, so the broken line
    below it is what comes out."""
    path = tmp_path / "session.jsonl"
    path.write_text('{"type": []}\n{"type": "user", \n'
                    + _user("ZZUSER 最後一行是好的"), encoding="utf-8")
    with pytest.raises(AgentError, match=r"第 2 行解析失敗"):
        C._exported(SID, path, path.parent / "session", label=path.name)


def test_the_file_is_read_one_line_at_a_time(tmp_path, monkeypatch):
    """The whole-file read (and its split copy) is the 1× the slice removes."""
    path = tmp_path / "session.jsonl"
    path.write_text('{"type": "user", "message": "hi"}\n', encoding="utf-8")

    def refuse(*_args, **_kwargs):
        raise AssertionError("_read_lines must not read the whole file")

    monkeypatch.setattr(Path, "read_text", refuse)
    main = C._read_lines(path)
    assert main == ['{"type": "user", "message": "hi"}']


def test_the_export_never_reads_the_whole_file(tmp_path, monkeypatch):
    """R1: the export streams the session; nothing loads it back (no read_text/
    read_bytes on the session, and the raw is never read either)."""
    path = tmp_path / "session.jsonl"
    path.write_text("".join([_user("ZZUSER 一"), _assistant("ZZASSIST 一")]),
                    encoding="utf-8")

    def refuse(*_args, **_kwargs):
        raise AssertionError("the export must not read the whole file")

    monkeypatch.setattr(Path, "read_text", refuse)
    monkeypatch.setattr(Path, "read_bytes", refuse)
    exported, packed = C._exported(SID, path, path.parent / "session", label=path.name)
    assert exported.raw_md5 == packed.md5
    assert exported.raw_file.stat().st_size == packed.size
    assert packed.main_count == 2


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
    C._exported(SID, path, path.parent / "session", label=path.name)
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


def test_a_syntax_error_far_before_non_utf8_bytes_is_reported_by_line_not_by_encoding(
        reference_claude, tmp_path):
    """The one intended difference in error order (#158): a file with a broken line early and
    non-UTF-8 bytes much later. HEAD read the whole file first and said "not UTF-8"; the streaming
    pass reaches line 2 first and says line 2 does not parse. Both refuse the file; only the
    message differs, and a file that is bad in both ways is bad in both - no behaviour a caller
    could rely on. (A short file is decoded in one go, so the encoding error still comes first
    there; the difference needs the bad bytes beyond the first read buffer.)"""
    good = _user("ZZUSER 好的一行")
    filler = _assistant("ZZASSIST " + "x" * 200)
    content = (good + "{ this is not json }\n" + filler * 600).encode("utf-8") + b"\xff\xfe broken\n" + \
        good.encode("utf-8")
    assert len(content) > 100_000
    path = tmp_path / f"{SID}.jsonl"
    path.write_bytes(content)
    sidecar = tmp_path / SID

    new = _observe(C, path, sidecar, old=False)
    old = _observe(reference_claude, path, sidecar, old=True)
    assert new["error"][0] == "AgentError" and old["error"][0] == "AgentError", "both refuse the file"
    assert "第 2 行解析失敗" in new["error"][1] and "UTF-8" not in new["error"][1]
    assert "UTF-8" in old["error"][1] and "第 2 行" not in old["error"][1]
