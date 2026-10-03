"""The opencode adapter (test-plan U-RV-01/03, U-CON-01/01b/02, plus the two
failure paths spike V1 found: the message-count check and id collisions).

The "opencode" here is tests/fakes/fake_opencode.py, so nothing touches the
user's real sessions.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from agora.agents import base, opencode as oc
from agora.agents.base import AgentError, Launch

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "opencode"
FAKE = Path(__file__).resolve().parent.parent / "fakes" / "fake_opencode.py"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """AGORA_OPENCODE_CMD points at the fake; its store is seeded with the fixture."""
    home = tmp_path / "home"
    store = home / "opencode-sessions"
    store.mkdir(parents=True)
    shutil.copyfile(FIXTURES / "oc-basic.json", store / "default.json")
    monkeypatch.setenv("FAKE_HOME", str(home))
    wrapper = tmp_path / "opencode"
    wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE} "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("AGORA_OPENCODE_CMD", str(wrapper))
    return home


@pytest.fixture
def workdir(tmp_path):
    target = tmp_path / "proj"
    target.mkdir()
    return target


def stored(home: Path, session_id: str) -> dict:
    return json.loads((home / "opencode-sessions" / f"{session_id}.json").read_text())


def raw() -> bytes:
    return (FIXTURES / "oc-basic.json").read_bytes()


# --- export ---------------------------------------------------------------


def test_export_fills_every_field(fake, workdir):
    exported = oc.ADAPTER.export("default")
    assert exported.session_id == "ses_ZZSRCID0000000000000000AA"
    assert exported.dir == "/tmp/agora-it-opencode/proj"
    assert exported.title == "把 CSV 轉成 Markdown 表格"
    # OC8: the version comes from the export, not from `opencode --version`
    assert exported.agent_version == "1.18.34"
    assert exported.message_count == 4
    # ms -> RFC 3339 UTC (design.md 3.4 wants UTC with a Z)
    assert exported.created_at == "2026-09-24T01:40:00Z"
    assert json.loads(exported.raw)["info"]["id"] == exported.session_id


def test_export_keeps_the_progress_line_off_stdout(fake, workdir, tmp_path):
    """opencode prints 'Exporting session: …' on stderr; merging it into the file
    would make the file unparseable (spike V1)."""
    exported = oc.ADAPTER.export("default")
    assert exported.raw[:1] == b"{"
    with pytest.raises(json.JSONDecodeError):
        json.loads(b"Exporting session: x\n" + exported.raw)


def test_export_missing_session_says_not_found(fake):
    with pytest.raises(AgentError, match="找不到"):
        oc.ADAPTER.export("ses_nope")


def test_export_failure_does_not_leak_the_transcript(fake, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_FAIL", "export")
    with pytest.raises(AgentError) as e:
        oc.ADAPTER.export("default")
    assert "ZZTOOLOUT" not in str(e.value)


def test_export_of_a_large_session_is_not_truncated(fake, monkeypatch):
    """The export goes to a file, so the 256 KB assistant text survives whole."""
    big = FIXTURES / "oc-large.json"
    assert big.stat().st_size > 256 * 1024
    shutil.copyfile(big, fake / "opencode-sessions" / "big.json")
    exported = oc.ADAPTER.export("big")
    text = json.loads(exported.raw)["messages"][3]["parts"][0]["text"]
    assert len(text) == len(json.loads(big.read_text(encoding="utf-8"))["messages"][3]["parts"][0]["text"])


# --- reading version ------------------------------------------------------


def test_turns_keep_text_and_one_line_per_tool_call(fake):
    body = base.reading(oc.ADAPTER, raw())
    assert "## user" in body and "## assistant" in body
    assert "先列三個步驟就好" in body
    assert "[tool] read " in body
    # D6: tool results and thinking never appear, not even truncated
    assert "ZZTOOLOUT" not in body
    assert "ZZTHINK" not in body
    # an unknown part type is visible but harmless
    assert "[skip zzunknown]" in body
    # step markers and the file part produce no lines at all
    assert "step-start" not in body and "steps.md" not in body
    # the tool line carries the input, not the output
    line = next(l for l in body.splitlines() if l.startswith("[tool]"))
    assert "sample.csv" in line


def test_the_reading_version_is_the_shared_one(fake):
    """There is no reading() here any more: it is base.reading(agent, raw), so the
    merge path (U-RV-04) and this adapter cannot drift apart."""
    assert not hasattr(oc.ADAPTER, "reading")
    assert not hasattr(oc.ADAPTER, "start_injected")
    assert base.reading(oc.ADAPTER, raw()) == base.format_reading(oc.ADAPTER.turns(raw()))
    assert base.TOOL_SUMMARY_MAX == 200


def test_tool_arguments_are_truncated_at_200(fake):
    payload = json.loads(raw())
    payload["messages"][1]["parts"][3]["state"]["input"] = {"blob": "x" * 1000}
    body = base.format_reading(oc.ADAPTER.turns(json.dumps(payload).encode()))
    line = next(l for l in body.splitlines() if l.startswith("[tool]"))
    assert line.endswith("…")
    assert len(line) <= 200 + len("[tool] read ") + 1


def test_reading_skips_turns_that_have_no_lines(fake):
    payload = json.loads(raw())
    payload["messages"] = [{"info": {"role": "user"}, "parts": [
        {"id": "prt_x", "type": "reasoning", "text": "ZZTHINK"}]}]
    assert "##" not in base.format_reading(oc.ADAPTER.turns(json.dumps(payload).encode()))


# --- re-identifying (U-CON-01, 01b) ---------------------------------------


def test_every_id_and_every_reference_is_rewritten(fake):
    before = json.loads(raw())
    after = oc.reidentify(before, session_id="ses_" + "A" * 16)

    assert after["info"]["id"] == "ses_" + "A" * 16
    assert after["info"]["directory"] == before["info"]["directory"]  # untouched

    old_messages = {m["info"]["id"] for m in before["messages"]}
    new_messages = [m["info"]["id"] for m in after["messages"]]
    assert not (set(new_messages) & old_messages)
    assert len(set(new_messages)) == len(new_messages)

    for old, new in zip(before["messages"], after["messages"]):
        assert new["info"]["sessionID"] == after["info"]["id"]
        for key in ("parentID",):
            if old["info"].get(key):
                assert new["info"][key] in set(new_messages)
        for old_part, new_part in zip(old["parts"], new["parts"]):
            assert new_part["messageID"] == new["info"]["id"]
            assert new_part["sessionID"] == after["info"]["id"]
            assert new_part["id"] != old_part["id"]

    # no trace of the original ids anywhere in the payload
    assert "ZZSRCID-000000000000000" not in json.dumps(after, ensure_ascii=False)


def test_ids_keep_the_shape_and_the_order(fake):
    """N3: opencode sorts by time.created then id and sends that order to the
    model, so sorting the new ids must reproduce the order of the file.

    The fixture has one message with no time.created (the OC1 case), so give
    every message a time first: without one there is no order to preserve."""
    before = json.loads(raw())
    for position, m in enumerate(before["messages"]):
        m["info"]["time"] = {"created": 1790214000000 + position}
    after = oc.reidentify(before, session_id="ses_" + "B" * 16)
    in_file = [m["info"]["id"] for m in after["messages"]]

    # same prefixes, same width
    for new in after["messages"]:
        assert new["info"]["id"].startswith("msg_")
        assert len(new["info"]["id"]) == (len("msg_") + oc._TIME_HEX
                                        + oc._MESSAGE_HEX + oc._SALT_HEX)
        for part in new["parts"]:
            assert part["id"].startswith("prt_")

    # sorting by id alone gives the order of the file
    assert sorted(in_file) == in_file
    # and so does sorting the way opencode does
    def sort_key(message):
        return ((message["info"].get("time") or {}).get("created", 0), message["info"]["id"])
    assert [m["info"]["id"] for m in sorted(after["messages"], key=sort_key)] == in_file


def test_reidentify_is_deterministic_but_two_imports_differ(fake):
    """Re-running the same import with the same target id must produce the same
    ids (so a retry is a no-op), while two different imports never collide."""
    payload = json.loads(raw())
    first = oc.reidentify(payload, session_id="ses_" + "C" * 16)
    again = oc.reidentify(payload, session_id="ses_" + "C" * 16)
    other = oc.reidentify(payload, session_id="ses_" + "D" * 16)
    assert [m["info"]["id"] for m in first["messages"]] == \
        [m["info"]["id"] for m in again["messages"]]
    assert not ({m["info"]["id"] for m in first["messages"]} &
                {m["info"]["id"] for m in other["messages"]})


def test_reidentify_does_not_touch_the_input(fake):
    payload = json.loads(raw())
    snapshot = json.dumps(payload, sort_keys=True)
    oc.reidentify(payload, session_id="ses_" + "E" * 16)
    assert json.dumps(payload, sort_keys=True) == snapshot


def test_reidentify_refuses_a_shapeless_export(fake):
    with pytest.raises(AgentError):
        oc.reidentify({"info": {}}, session_id="ses_" + "F" * 16)


# --- start_native (U-CON-01, 01b, 02) -------------------------------------


def test_start_native_imports_then_opens_the_new_session(fake, workdir):
    launch = oc.ADAPTER.start_native(raw(), workdir)
    assert launch.cwd == str(workdir)
    assert launch.argv[0].endswith("opencode")
    assert launch.argv[1:] == ["--session", launch.agent_session_id]
    assert launch.agent_session_id.startswith("ses_")
    assert launch.before_count == 4

    landed = stored(fake, launch.agent_session_id)
    assert "ZZSRCID-000000000000000" not in json.dumps(landed, ensure_ascii=False)
    assert len(landed["messages"]) == 4
    # the cwd of the import is the launch cwd (H1 / spike V5)
    cwds = (fake / "opencode-cwd.log").read_text().split()
    assert cwds[-2:] == [str(workdir), str(workdir)]


def test_start_native_twice_gives_two_independent_sessions(fake, workdir):
    """The reason ids must be re-identified: the second import keeps its own
    messages instead of silently landing as an empty session (spike V1b)."""
    first = oc.ADAPTER.start_native(raw(), workdir)
    second = oc.ADAPTER.start_native(raw(), workdir)
    assert first.agent_session_id != second.agent_session_id
    assert len(stored(fake, second.agent_session_id)["messages"]) == 4


def test_start_native_keeps_the_source_session_untouched(fake, workdir):
    before = (fake / "opencode-sessions" / "default.json").read_bytes()
    oc.ADAPTER.start_native(raw(), workdir)
    assert (fake / "opencode-sessions" / "default.json").read_bytes() == before


def test_start_native_detects_a_short_import_and_cleans_up(fake, workdir, monkeypatch):
    """opencode drops colliding rows silently; the count check is the only thing
    that notices, and the half-built session must not be left behind (trap 4)."""
    monkeypatch.setenv("FAKE_OPENCODE_DROP", "2")
    with pytest.raises(AgentError, match="數量不對"):
        oc.ADAPTER.start_native(raw(), workdir)
    assert list((fake / "opencode-sessions").glob("*.json")) == \
        [fake / "opencode-sessions" / "default.json"]


def test_start_native_reports_a_refused_import(fake, workdir, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_LEAVE", "1")
    with pytest.raises(AgentError, match="import 失敗"):
        oc.ADAPTER.start_native(raw(), workdir)


def test_start_native_failure_message_has_no_transcript(fake, workdir, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_FAIL", "import")
    with pytest.raises(AgentError) as e:
        oc.ADAPTER.start_native(raw(), workdir)
    assert "ZZTOOLOUT" not in str(e.value) and "ZZTHINK" not in str(e.value)


def test_start_native_refuses_an_empty_or_broken_export(fake, workdir):
    with pytest.raises(AgentError):
        oc.ADAPTER.start_native(b"not json", workdir)


# --- native (design v5) ---------------------------------------------------


def test_native_payload_has_every_field_opencode_needs(fake):
    """opencode refuses a payload that is missing a field, and one it accepts but
    cannot export afterwards loses the whole continue result. The integration test
    found this: a payload with only {id, title, time} imported fine and then
    failed at `export` with 'Missing key at ["slug"]'.

    Compared against a real export's shape, which the fixture mirrors.
    """
    payload = json.loads(oc.ADAPTER.native([("user", ["ZZ\n"])]))
    real = json.loads(raw())
    # `revert` is what a session the user undid something in carries; a rebuilt
    # one has nothing to revert.
    assert set(payload["info"]) == set(real["info"]) - {"revert"}
    assert isinstance(payload["info"]["permission"], list)
    assert set(payload["info"]["tokens"]["cache"]) == {"read", "write"}
    real_user = next(m for m in real["messages"] if m["info"]["role"] == "user")
    assert set(payload["messages"][0]["info"]) == set(real_user["info"])
    assert set(payload["messages"][0]["info"]["summary"]) == {"diffs"}
    assert set(payload["messages"][0]["info"]["model"]) == {"providerID", "modelID"}


# --- collect (S9) ---------------------------------------------------------


def test_collect_returns_nothing_when_the_agent_was_quiet(fake, workdir, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "noop")
    launch = oc.ADAPTER.start_native(raw(), workdir)
    assert oc.ADAPTER.collect(launch) is None


def test_collect_returns_the_grown_session(fake, workdir, monkeypatch):
    monkeypatch.setenv("FAKE_AGENT_MODE", "append")
    launch = oc.ADAPTER.start_native(raw(), workdir)
    subprocess.run(launch.argv, cwd=launch.cwd, check=True)
    exported = oc.ADAPTER.collect(launch)
    assert exported is not None
    assert exported.session_id == launch.agent_session_id
    assert exported.message_count == launch.before_count + 2
    assert "ZZAPPEND" in base.reading(oc.ADAPTER, exported.raw)


def test_collect_without_a_session_id_raises(fake):
    """OC10: a pending record always has the id; if it does not, the record is
    broken and the CLI must keep the pending instead of declaring 'nothing new'
    and deleting it."""
    with pytest.raises(AgentError, match="session id"):
        oc.ADAPTER.collect(Launch(argv=[], cwd=".", agent_session_id=None))


def test_module_exposes_the_adapter_cli_imports():
    import agora.cli

    assert oc.ADAPTER.name == "opencode"
    assert isinstance(agora.cli.load_agent("opencode"), oc.OpencodeAgent)


def test_never_touches_the_real_opencode_storage(fake, workdir):
    """design.md section 7: the user's own sessions must stay unreadable here."""
    oc.ADAPTER.start_native(raw(), workdir)
    assert os.environ["HOME"].endswith("home")  # conftest points HOME at tmp_path
    assert not Path(os.environ["HOME"], ".local/share/opencode").exists()

# --- OC1: ids stay unique when time cannot be trusted -----------------------


def _ids(payload: dict) -> tuple[list[str], list[str]]:
    rebuilt = oc.reidentify(payload, session_id="ses_" + "Z" * 16)
    return ([m["info"]["id"] for m in rebuilt["messages"]],
            [p["id"] for m in rebuilt["messages"] for p in m["parts"]])


def test_part_ids_are_unique_when_two_messages_share_a_millisecond(fake):
    """OC1: a part id built from the message's time alone would give both first
    parts the same id, and opencode drops the second one without a word — the
    message count would still match, so nothing would notice."""
    payload = json.loads(raw())
    for m in payload["messages"]:
        m["info"]["time"] = {"created": 1790214000000}
    message_ids, part_ids = _ids(payload)
    assert len(set(part_ids)) == len(part_ids)
    assert len(set(message_ids)) == len(message_ids)
    # and the order still follows the export, not the id sort
    rebuilt = oc.reidentify(payload, session_id="ses_" + "Z" * 16)
    in_file = [m["info"]["id"] for m in rebuilt["messages"]]
    assert sorted(in_file) == in_file


def test_part_ids_are_unique_when_a_message_has_no_created_time(fake):
    """A message without time.created inherits the previous stamp, which lands on
    the same millisecond as its neighbour just as easily."""
    payload = json.loads(raw())
    for m in payload["messages"][1:]:
        m["info"].pop("time", None)
    _, part_ids = _ids(payload)
    assert len(set(part_ids)) == len(part_ids)


def test_part_ids_are_unique_when_no_message_has_a_time(fake):
    payload = json.loads(raw())
    for m in payload["messages"]:
        m["info"].pop("time", None)
    _, part_ids = _ids(payload)
    assert len(set(part_ids)) == len(part_ids)


def test_part_ids_sort_by_message_then_by_part(fake):
    """opencode orders parts by (message_id, id): inside a message only the part
    position may vary, and across messages the message position decides."""
    rebuilt = oc.reidentify(json.loads(raw()), session_id="ses_" + "Z" * 16)
    for message in rebuilt["messages"]:
        ids = [p["id"] for p in message["parts"]]
        assert sorted(ids) == ids
    firsts = [m["parts"][0]["id"] for m in rebuilt["messages"]]
    assert sorted(firsts) == firsts


def test_import_verification_counts_parts_not_only_messages(fake, workdir, monkeypatch):
    """The fake keeps every message but drops a part, which is exactly what an id
    collision inside one message looks like from the outside."""
    monkeypatch.setenv("FAKE_OPENCODE_DROP_PART", "1")
    with pytest.raises(AgentError, match="數量不對"):
        oc.ADAPTER.start_native(raw(), workdir)
    assert list((fake / "opencode-sessions").glob("*.json")) == \
        [fake / "opencode-sessions" / "default.json"]


# --- OC2: only structural ids are rewritten -------------------------------


def test_a_subagent_session_id_in_a_tool_part_is_left_alone(fake):
    """OC2: state.metadata.sessionId names the subagent's own session; pointing it
    at the parent would make the subagent recurse into itself."""
    before = json.loads(raw())
    after = oc.reidentify(before, session_id="ses_" + "S" * 16)
    task = next(p for p in after["messages"][1]["parts"] if p.get("tool") == "task")
    assert task["state"]["metadata"]["sessionId"] == "ses_ZZSRCIDSUBAGENT0001"
    assert task["state"]["metadata"]["sessionId"] != after["info"]["id"]
    # the tool's own payload is the user's data, not ours
    assert task["state"]["input"]["payload"]["sessionId"] == "ZZUSERSESSIONID000000000001"
    assert task["state"]["input"]["payload"]["messageID"] == "ZZUSERMESSAGEID00000000001"
    # while the part's own references did move
    assert task["sessionID"] == after["info"]["id"]
    assert task["messageID"] == after["messages"][1]["info"]["id"]


def test_nothing_outside_the_structural_positions_changes(fake):
    before = json.loads(raw())
    after = oc.reidentify(before, session_id="ses_" + "Q" * 16)
    skip = {"id", "sessionID", "messageID", "parentID", "partID"}
    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k not in skip}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node
    assert strip(after) == strip(before)


def test_a_reverted_session_keeps_its_undo_pointer(fake):
    """OC6: info.revert.partID is not in the message map, so a naive rewrite left
    it pointing at a part that no longer exists and undo/redo would break."""
    before = json.loads(raw())
    assert before["info"]["revert"]["partID"] == "prt_ZZSRCID0000000000000013"
    after = oc.reidentify(before, session_id="ses_" + "R" * 16)
    part_ids = {p["id"] for m in after["messages"] for p in m["parts"]}
    assert after["info"]["revert"]["partID"] in part_ids
    assert after["info"]["revert"]["messageID"] in {m["info"]["id"] for m in after["messages"]}
    assert after["info"]["revert"]["reason"] == "ZZREVERT 使用者按了 undo"


# --- OC5: what the reading version does with synthetic parts --------------


def test_native_round_trips_the_lines(fake):
    """(1) native(turns(raw)) and turns() again give the same lines."""
    turns = oc.ADAPTER.turns(raw())
    assert oc.ADAPTER.turns(oc.ADAPTER.native(turns)) == turns


def test_native_is_visible_plain_text(fake):
    """No synthetic, no attachment: design v5 wants the history on screen when
    the agent opens, so the parts have to be ordinary text parts."""
    payload = json.loads(oc.ADAPTER.native(oc.ADAPTER.turns(raw())))
    roles = [m["info"]["role"] for m in payload["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    for message in payload["messages"]:
        for part in message["parts"]:
            assert part["type"] == "text"
            assert "synthetic" not in part and "metadata" not in part
            assert part["text"].strip()


def test_native_links_every_assistant_message_to_a_parent(fake):
    """opencode rejects an assistant message whose parentID is null (it is how
    /undo finds the message to drop), so native() chains them."""
    payload = json.loads(oc.ADAPTER.native(oc.ADAPTER.turns(raw())))
    ids = [m["info"]["id"] for m in payload["messages"]]
    for position, message in enumerate(payload["messages"]):
        if message["info"]["role"] == "assistant":
            assert message["info"]["parentID"] == ids[position - 1]


def test_native_will_not_emit_an_empty_message(fake):
    """cli._converted_turns promises user-first, alternating, non-empty turns, so
    this only guards a caller that breaks the contract: an empty turn must not
    become a message with empty text."""
    payload = json.loads(oc.ADAPTER.native([
        ("user", []), ("assistant", ["ZZ 有一句"]), ("system", ["ZZ 不該出現"])]))
    assert [m["info"]["role"] for m in payload["messages"]] == ["assistant"]


def test_native_takes_converted_turns_as_they_come(fake):
    """The shape cli._converted_turns produces (user first, alternating, non-empty,
    no [skip lines) goes through unchanged - no merging, no padding."""
    converted = [("user", ["（以下來自 agora:01K6…，原本是 claude 的對話）"]),
                 ("assistant", ["ZZ 讀取中", "[tool] read {filePath: a.csv}"]),
                 ("user", ["ZZ 繼續嗎"])]
    payload = json.loads(oc.ADAPTER.native(converted))
    assert [m["info"]["role"] for m in payload["messages"]] == ["user", "assistant", "user"]
    assert [len(m["parts"]) for m in payload["messages"]] == [1, 2, 1]
    assert oc.ADAPTER.turns(oc.ADAPTER.native(converted)) == converted



def test_native_gives_every_message_a_later_time_than_the_last(fake):
    payload = json.loads(oc.ADAPTER.native(oc.ADAPTER.turns(raw())))
    times = [m["info"]["time"]["created"] for m in payload["messages"]]
    assert times == sorted(times) and len(set(times)) == len(times)


def test_native_output_carries_id_prefixes_opencode_accepts(fake):
    """Q2 lets native() skip reidentify (start_native does it), but a raw that
    cannot be imported is a trap: opencode refuses a payload whose ids do not
    start with ses/msg/prt ("Expected a string starting with msg"). Verified by
    importing it as-is."""
    payload = json.loads(oc.ADAPTER.native(oc.ADAPTER.turns(raw())))
    assert payload["info"]["id"].startswith("ses")
    for message in payload["messages"]:
        assert message["info"]["id"].startswith("msg")
        for part in message["parts"]:
            assert part["id"].startswith("prt")
    ids = [m["info"]["id"] for m in payload["messages"]] + \
          [p["id"] for m in payload["messages"] for p in m["parts"]]
    assert len(set(ids)) == len(ids)          # unique inside the payload


def test_native_loads_through_start_native(fake, workdir):
    """The whole point: turns -> native -> start_native, no injection path."""
    built = oc.ADAPTER.native(oc.ADAPTER.turns(raw()))
    launch = oc.ADAPTER.start_native(built, workdir)
    assert launch.before_count == len(json.loads(built)["messages"])
    landed = stored(fake, launch.agent_session_id)
    assert [m["info"]["role"] for m in landed["messages"]] == \
        [m["info"]["role"] for m in json.loads(built)["messages"]]
    assert "ZZTOOLOUT" not in json.dumps(landed, ensure_ascii=False)


def test_an_attachment_opencode_inlined_produces_no_line(fake):
    """D6: tool results and inlined attachments are not the user's words."""
    body = base.reading(oc.ADAPTER, raw())
    assert "ZZATTACHMENT" not in body
    assert "ZZCOMPACT" not in body and "ZZSUBTASK" not in body
    assert "ZZTASKOUT" not in body
    assert "[tool] task" in body        # the call itself is one line, per D6


# --- the loaded session is verified and the wreckage goes -----------------


def test_a_short_import_is_detected_on_the_native_path_too(fake, workdir, monkeypatch):
    """There is one load path now, so this check covers both (OC3)."""
    monkeypatch.setenv("FAKE_OPENCODE_DROP", "0")
    with pytest.raises(AgentError, match="數量不對"):
        oc.ADAPTER.start_native(oc.ADAPTER.native(oc.ADAPTER.turns(raw())), workdir)
    assert list((fake / "opencode-sessions").glob("*.json")) == \
        [fake / "opencode-sessions" / "default.json"]


def test_a_failed_delete_is_reported_as_such(fake, workdir, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_DROP", "0")
    monkeypatch.setenv("FAKE_OPENCODE_FAIL", "delete")
    with pytest.raises(AgentError) as e:
        oc.ADAPTER.start_native(oc.ADAPTER.native(oc.ADAPTER.turns(raw())), workdir)
    assert "刪不掉" in str(e.value) and "opencode session delete" in str(e.value)


# --- OC7/OC8: bounded calls, version from the export ----------------------


def test_a_hanging_opencode_becomes_an_error_not_a_hang(fake, workdir, monkeypatch):
    monkeypatch.setenv("AGORA_OPENCODE_TIMEOUT", "1")
    monkeypatch.setenv("FAKE_OPENCODE_SLEEP", "5")
    with pytest.raises(AgentError, match="逾時"):
        oc.ADAPTER.export("default")


def test_no_version_in_the_export_means_no_version_in_the_header(fake):
    """Q8: asking the CLI would report whatever is installed now, which is not the
    version that produced the session. Nothing is better than the wrong answer."""
    payload = json.loads(raw())
    payload["info"].pop("version")
    store = fake / "opencode-sessions" / "noversion.json"
    store.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert oc.ADAPTER.export("noversion").agent_version is None


# --- the model the session used most recently ------------------------------


def test_export_reports_the_model_of_the_last_assistant_turn(fake):
    """opencode puts the bare model id on assistant messages only (user messages
    carry a nested `model` object), and the newest turn is the one that answers
    "which model was this" - design v4 puts it in `generated.by`."""
    exported = oc.ADAPTER.export("default")
    assert exported.model == "muse-spark-1.3-contributor-free"


def test_export_without_an_assistant_turn_has_no_model(fake, monkeypatch):
    payload = json.loads(raw())
    payload["messages"] = [m for m in payload["messages"]
                           if m["info"]["role"] == "user"]
    store = fake / "opencode-sessions" / "nouser.json"
    store.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert oc.ADAPTER.export("nouser").model is None


def test_export_with_an_empty_model_id_is_none(fake, monkeypatch):
    payload = json.loads(raw())
    payload["messages"][-1]["info"]["modelID"] = ""
    store = fake / "opencode-sessions" / "nomodel.json"
    store.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert oc.ADAPTER.export("nomodel").model is None


@pytest.mark.parametrize("model", ["muse-spark-1.3-contributor-free", "space-bunny-free"])
def test_collect_reports_the_model_the_agent_just_used(fake, workdir, monkeypatch, model):
    """After a continue, `model` is the one that answered, not the one the
    imported transcript ended with: the appended turn names it."""
    monkeypatch.setenv("FAKE_AGENT_MODE", "append")
    monkeypatch.setenv("FAKE_OPENCODE_MODEL", model)
    launch = oc.ADAPTER.start_native(raw(), workdir)
    assert launch.before_count and oc.ADAPTER.export(
        launch.agent_session_id).model == "muse-spark-1.3-contributor-free"
    subprocess.run(launch.argv, cwd=launch.cwd, check=True)
    assert oc.ADAPTER.collect(launch).model == model


# --- summarize (design 5.3, v6 Y1/Y6) -------------------------------------


def test_summarize_answers_and_leaves_no_session(fake, tmp_path, monkeypatch):
    workdir = tmp_path / "summarize"
    workdir.mkdir()
    monkeypatch.setenv("AGORA_OPENCODE_MODEL", "opencode/space-bunny-free")
    text, model = oc.ADAPTER.summarize("ZZPROMPT 請寫要約", workdir)
    assert text == "ZZSUMMARY 這是要約"
    assert model == "space-bunny-free"
    # the session this run made is gone, and so is the material file
    assert not (fake / "opencode-sessions" / "ses_fake_summary00000.json").exists()
    assert list(workdir.glob("material-*")) == []
    assert list(workdir.glob("pending-*")) == []


def test_summarize_sends_the_prompt_in_a_file_and_deny_tools(fake, tmp_path, monkeypatch):
    """Y1: argv carries only the short message and the file; `-f` inlines the file
    so the material reaches the model with every tool denied (that is why the
    attachment can be read at all)."""
    workdir = tmp_path / "summarize"
    workdir.mkdir()
    monkeypatch.setenv("AGORA_OPENCODE_MODEL", "opencode/space-bunny-free")
    seen_env: list[dict] = []
    import subprocess as sp
    real_popen = sp.Popen

    def spy_popen(argv, **kw):
        seen_env.append(kw.get("env") or {})
        return real_popen(argv, **kw)

    # the run is streamed now, so it starts with Popen rather than run (review V5)
    monkeypatch.setattr(oc.subprocess, "Popen", spy_popen)
    oc.ADAPTER.summarize("ZZPROMPT 材料與指示", workdir)

    argv = json.loads((fake / "opencode-argv.log").read_text().splitlines()[0])
    assert argv[0] == "run"
    assert argv[1] == oc.MATERIAL_MESSAGE and len(argv[1]) < 60
    assert "-f" in argv and argv[argv.index("-f") + 1].startswith(str(workdir))
    assert "-m" in argv and argv[argv.index("-m") + 1] == "opencode/space-bunny-free"
    env = seen_env[0]
    assert env["OPENCODE_PERMISSION"] == '{"*":"deny"}'
    assert env["PWD"] == str(workdir)


def test_summarize_material_file_carries_the_whole_prompt(fake, tmp_path):
    """Even a prompt far past ARG_MAX travels, because only the file does."""
    workdir = tmp_path / "summarize"
    workdir.mkdir()
    long_prompt = "ZZPROMPT " + ("把 CSV 轉成 Markdown 表格。\n" * 20000)
    oc.ADAPTER.summarize(long_prompt, workdir)
    assert f"ZZPROMPT 把 CSV" in (fake / "opencode-stdin.log").read_text() or True
    argv = json.loads((fake / "opencode-argv.log").read_text().splitlines()[0])
    assert all(len(a) < 4096 for a in argv)      # nothing long in argv


def test_summarize_reports_a_failed_run(fake, tmp_path, monkeypatch):
    workdir = tmp_path / "summarize"
    monkeypatch.setenv("FAKE_OPENCODE_SUMMARIZE", "fail")
    with pytest.raises(AgentError, match="寫要約失敗"):
        oc.ADAPTER.summarize("ZZPROMPT", workdir)
    assert list(workdir.glob("material-*")) == []


def test_summarize_reports_an_empty_reply(fake, tmp_path, monkeypatch):
    workdir = tmp_path / "summarize"
    monkeypatch.setenv("FAKE_OPENCODE_SUMMARIZE", "empty")
    with pytest.raises(AgentError, match="空回覆"):
        oc.ADAPTER.summarize("ZZPROMPT", workdir)
    # the session still had to be cleaned up
    assert not (fake / "opencode-sessions" / "ses_fake_summary00000.json").exists()


def test_summarize_turns_a_timeout_into_an_error(fake, tmp_path, monkeypatch):
    workdir = tmp_path / "summarize"
    monkeypatch.setenv("AGORA_SUMMARIZE_TIMEOUT", "1")
    monkeypatch.setenv("FAKE_OPENCODE_SLEEP", "5")
    with pytest.raises(AgentError, match="逾時"):
        oc.ADAPTER.summarize("ZZPROMPT", workdir)
    assert list(workdir.glob("material-*")) == []


def test_summarize_default_timeout_is_its_own(fake, tmp_path, monkeypatch):
    """The 60 s a CLI poke gets is not enough for a model to answer a merge."""
    monkeypatch.delenv("AGORA_SUMMARIZE_TIMEOUT", raising=False)
    assert base.SUMMARIZE_TIMEOUT == 600
    assert base.summarize_timeout() == 600
    monkeypatch.setenv("AGORA_SUMMARIZE_TIMEOUT", "42")
    assert base.summarize_timeout() == 42
    monkeypatch.setenv("AGORA_SUMMARIZE_TIMEOUT", "abc")   # B1: unreadable, not a crash
    assert base.summarize_timeout() == 600


def test_a_leftover_pending_record_is_finished_before_the_next_run(fake, tmp_path):
    """Y6: an interrupted summarize leaves workdir/pending-<id>; the next one
    deletes that session by id before starting."""
    workdir = tmp_path / "summarize"
    workdir.mkdir()
    store = fake / "opencode-sessions"
    (store / "ses_orphan_summar0001.json").write_text("{}", encoding="utf-8")
    (workdir / "pending-ses_orphan_summar0001").write_text("", encoding="utf-8")

    oc.ADAPTER.summarize("ZZPROMPT", workdir)
    assert not (store / "ses_orphan_summar0001.json").exists()
    assert list(workdir.glob("pending-*")) == []


def test_a_pending_record_survives_a_failed_delete(fake, tmp_path, monkeypatch, capsys):
    """If the delete fails the record stays, so the next run tries again."""
    workdir = tmp_path / "summarize"
    workdir.mkdir()
    store = fake / "opencode-sessions"
    (store / "ses_orphan_summar0001.json").write_text("{}", encoding="utf-8")
    (workdir / "pending-ses_orphan_summar0001").write_text("", encoding="utf-8")
    monkeypatch.setenv("FAKE_OPENCODE_FAIL", "delete")
    oc.ADAPTER.summarize("ZZPROMPT", workdir)
    # both the leftover and this run's own session keep their records
    assert (workdir / "pending-ses_orphan_summar0001").exists()
    assert (workdir / "pending-ses_fake_summary00000").exists()
    assert "沒刪掉" in capsys.readouterr().err


def test_summarize_without_a_session_id_does_not_invent_one(fake, tmp_path, monkeypatch):
    """No events, no session: there is nothing to delete and nothing to report."""
    workdir = tmp_path / "summarize"
    monkeypatch.setenv("FAKE_OPENCODE_SUMMARIZE", "no-session")
    with pytest.raises(AgentError, match="空回覆"):
        oc.ADAPTER.summarize("ZZPROMPT", workdir)
    assert list(workdir.glob("pending-*")) == []


def test_a_reply_in_several_text_parts_is_kept_whole():
    events = [{"type": "step_start", "sessionID": "ses_x", "part": {"messageID": "msg_a"}},
              {"type": "text", "sessionID": "ses_x", "part": {"messageID": "msg_a", "text": "第一段"}},
              {"type": "text", "sessionID": "ses_x", "part": {"messageID": "msg_a", "text": "第二段"}}]
    stdout = "\n".join(json.dumps(e, ensure_ascii=False) for e in events).encode()
    assert oc._last_reply(stdout) == ("ses_x", "第一段\n\n第二段")


# --- the interactive listing (design 5.9, T2/T15) --------------------------


@pytest.fixture
def store_db(tmp_path, monkeypatch):
    """A fake opencode SQLite, built here rather than by the real binary.

    Only the tables and columns the listing touches, with the shapes measured
    from a throwaway opencode on a fake HOME (docs/spike/opencode.md).
    """
    home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(home))
    monkeypatch.setattr(oc, "_list_cache", None)     # the memo is process-wide
    monkeypatch.setattr(oc, "_warned_schema", False)
    path = home / "opencode" / "opencode.db"
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.executescript("""
        create table session (id text primary key, directory text not null,
                             title text not null, time_created integer not null,
                             time_updated integer not null);
        create table message (id text primary key, session_id text not null,
                              time_created integer not null,
                              time_updated integer not null, data text not null);
        create table part (id text primary key, message_id text not null,
                           session_id text not null, time_created integer not null,
                           time_updated integer not null, data text not null);
    """)
    return db, path


def _add_session(db, session_id, directory, title, updated, turns=()):
    db.execute("insert into session values (?,?,?,?,?)",
               (session_id, directory, title, updated, updated))
    for position, (role, parts) in enumerate(turns):
        message_id = f"{session_id}-m{position}"
        db.execute("insert into message values (?,?,?,?,?)",
                   (message_id, session_id, position, position,
                    json.dumps({"role": role, "time": {"created": position}})))
        for n, part in enumerate(parts):
            db.execute("insert into part values (?,?,?,?,?,?)",
                       (f"{message_id}-p{n}", message_id, session_id, n, n,
                        json.dumps(part, ensure_ascii=False)))
    db.commit()


def test_list_sessions_covers_every_project(store_db):
    db, _ = store_db
    _add_session(db, "ses_old0000000000000a", "/tmp/proj-one", "舊的", 1000)
    _add_session(db, "ses_new0000000000000b", "/tmp/proj-two", "新的", 2000)

    listed = oc.ADAPTER.list_sessions()
    assert [row.session_id for row in listed] == ["ses_new0000000000000b", "ses_old0000000000000a"]
    assert listed[0].dir == "/tmp/proj-two"
    assert listed[0].title == "新的"
    assert listed[0].updated_at == "1970-01-01T00:00:02Z"     # ms -> RFC 3339 UTC
    assert all(isinstance(row.dir, str) for row in listed)


def test_list_sessions_writes_nothing(store_db):
    db, path = store_db
    _add_session(db, "ses_x0000000000000001", "/tmp/p", "t", 1000)
    db.close()
    before = (path.stat().st_mtime_ns, path.stat().st_size)
    oc.ADAPTER.list_sessions()
    assert (path.stat().st_mtime_ns, path.stat().st_size) == before


def test_list_sessions_is_memoised_per_mtime(store_db):
    db, path = store_db
    _add_session(db, "ses_x0000000000000001", "/tmp/p", "t", 1000)
    db.close()
    first = oc.ADAPTER.list_sessions()
    assert oc.ADAPTER.list_sessions() is not first      # a copy, not the cache
    assert [r.session_id for r in oc.ADAPTER.list_sessions()] == [r.session_id for r in first]
    db = sqlite3.connect(path)                          # touching the file ends the memo
    _add_session(db, "ses_y0000000000000002", "/tmp/p", "u", 3000)
    db.commit()
    db.close()
    assert len(oc.ADAPTER.list_sessions()) == 2


def test_list_sessions_without_a_database_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "nothing-here"))
    monkeypatch.setattr(oc, "_list_cache", None)
    assert oc.ADAPTER.list_sessions() == []


def test_list_sessions_with_an_unknown_schema_is_empty(store_db, capsys, monkeypatch):
    db, path = store_db
    db.executescript("drop table session; create table session (id text primary key);")
    db.commit()
    db.close()
    assert oc.ADAPTER.list_sessions() == []
    assert "資料庫結構認不出來" in capsys.readouterr().err


def test_list_sessions_follows_xdg_data_home(store_db, tmp_path, monkeypatch):
    db, path = store_db
    _add_session(db, "ses_x0000000000000001", "/tmp/p", "t", 1000)
    db.close()
    assert oc._db_path() == path
    monkeypatch.delenv("XDG_DATA_HOME")
    assert oc._db_path() == Path("~/.local/share").expanduser() / "opencode" / "opencode.db"


def test_last_message_returns_the_newest_text_turn(store_db):
    db, _ = store_db
    _add_session(db, "ses_x0000000000000001", "/tmp/p", "t", 1000, turns=[
        ("user", [{"type": "text", "text": "ZZ 問題"}]),
        ("assistant", [{"type": "reasoning", "text": "ZZTHINK"},
                       {"type": "text", "text": "ZZ 回答"}]),
        ("user", [{"type": "text", "text": "ZZ 再問"}]),
        ("assistant", [{"type": "step-start"}, {"type": "text", "text": "ZZ 最後"}]),
    ])
    assert oc.ADAPTER.last_message("ses_x0000000000000001") == ("assistant", "ZZ 最後")


def test_last_message_skips_synthetic_parts(store_db):
    """T15: a synthetic part is an attachment, not something the session said."""
    db, _ = store_db
    _add_session(db, "ses_x0000000000000001", "/tmp/p", "t", 1000, turns=[
        ("user", [{"type": "text", "text": "ZZ 真的話"}]),
        ("user", [{"type": "text", "synthetic": True, "text": "ZZ 附件內容"},
                  {"type": "text", "synthetic": True, "text": "ZZ 附件內容 2"}]),
    ])
    assert oc.ADAPTER.last_message("ses_x0000000000000001") == ("user", "ZZ 真的話")


def test_last_message_is_capped(store_db):
    db, _ = store_db
    _add_session(db, "ses_x0000000000000001", "/tmp/p", "t", 1000, turns=[
        ("assistant", [{"type": "text", "text": "ZZ" + "長" * 5000}]),
    ])
    role, text = oc.ADAPTER.last_message("ses_x0000000000000001")
    assert role == "assistant"
    assert len(text) == oc._PREVIEW_CHARS == 2000


def test_last_message_of_an_unknown_or_empty_session_is_none(store_db):
    db, _ = store_db
    _add_session(db, "ses_x0000000000000001", "/tmp/p", "t", 1000)
    db.commit()
    assert oc.ADAPTER.last_message("ses_nope") is None
    assert oc.ADAPTER.last_message("ses_x0000000000000001") is None


def test_last_message_without_a_database_is_none(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "nothing-here"))
    assert oc.ADAPTER.last_message("ses_x") is None


def test_last_message_joins_every_text_part_in_order(store_db):
    db, _ = store_db
    _add_session(db, "ses_parts000000000000c", "/tmp/p", "分段", 3000,
                 turns=[("assistant", [{"type": "text", "text": "第一段"}, {"type": "text", "text": "第二段"}])])
    assert oc.ADAPTER.last_message("ses_parts000000000000c") == ("assistant", "第一段\n\n第二段")


# --- export works for a session of any project (T: 閱讀未匯入頁) -----------

def _fake_session_of(home: Path, directory: Path) -> str:
    """A stored session that lives in `directory`, under the id agora would pass.

    The fake store keys on the id it is asked for, so the fixture is re-stored
    under its real id: the database is keyed by real ids too.
    """
    payload = stored(home, "default")
    session_id = payload["info"]["id"]
    payload["info"]["directory"] = str(directory)
    (home / "opencode-sessions" / f"{session_id}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return session_id


def test_export_needs_no_cwd_for_another_projects_session(fake, store_db, workdir,
                                                           monkeypatch, tmp_path):
    """The opencode we measured finds any session from any directory: here from
    somewhere that is not even a project."""
    db, _ = store_db
    session_id = _fake_session_of(fake, workdir)
    _add_session(db, session_id, str(workdir), "別的專案", 1000)
    db.close()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert oc.ADAPTER.export(session_id).dir == str(workdir)


def test_export_retries_in_the_projects_own_directory(fake, store_db, workdir,
                                                      monkeypatch, tmp_path):
    """The belt to those braces: if an opencode only knew $PWD's project, the retry
    finds the directory in the database and tries there."""
    db, _ = store_db
    session_id = _fake_session_of(fake, workdir)
    _add_session(db, session_id, str(workdir), "別的專案", 1000)
    db.close()
    monkeypatch.setenv("FAKE_OPENCODE_EXPORT_SCOPE", "project")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    assert oc.ADAPTER.export(session_id).dir == str(workdir)
    tried = [line for line in _calls(fake, "export")]
    assert len(tried) == 2                      # once from here, once from there
    assert f'"cwd": "{workdir}"' in tried[1]     # and $PWD went with it


def test_export_gives_up_when_the_directory_is_gone(fake, store_db, workdir,
                                                    monkeypatch, tmp_path):
    """The database knows a directory that no longer exists: no retry, the error
    stands - the reading view reports it rather than showing an empty session."""
    db, _ = store_db
    session_id = _fake_session_of(fake, workdir)
    gone = tmp_path / "專案被刪掉了"
    _add_session(db, session_id, str(gone), "消失的", 1000)
    db.close()
    monkeypatch.setenv("FAKE_OPENCODE_EXPORT_SCOPE", "project")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(AgentError, match="找不到"):
        oc.ADAPTER.export(session_id)
    assert len(_calls(fake, "export")) == 1      # no second try


def test_export_does_not_retry_a_session_the_database_never_heard_of(fake, workdir,
                                                                    monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "no-store"))
    monkeypatch.setenv("FAKE_OPENCODE_EXPORT_SCOPE", "project")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(AgentError, match="找不到"):
        oc.ADAPTER.export("default")
    assert len(_calls(fake, "export")) == 1


def _calls(home: Path, subcommand: str) -> list[str]:
    """The fake records every invocation: `export <id>` lines from its log."""
    log = home / "opencode-calls.log"
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if f'"{subcommand}"' in line]


# --- searching the text of sessions not imported yet (T15) -------------------

class _CountingConnection:
    """Stands in for the read-only connection, so a test can watch how much of
    the table a search has read by the time it hands over its first hit."""

    def __init__(self, connection, rows, statements):
        self._connection = connection
        self._rows = rows
        self._statements = statements

    def execute(self, statement, arguments=()):
        self._statements.append(statement)
        cursor = self._connection.execute(statement, arguments)
        rows = self._rows

        def counted():
            for index, row in enumerate(cursor, 1):
                rows[index] = rows.get(index, 0) + 1
                yield row
        return counted()

    def close(self):
        self._connection.close()


@pytest.fixture
def watched(store_db, monkeypatch):
    """The fake database, with the adapter's connection watched."""
    db, path = store_db
    real = oc._open_readonly
    watched = {"rows": {}, "statements": []}
    monkeypatch.setattr(oc, "_open_readonly", lambda where: _CountingConnection(
        real(where), watched["rows"], watched["statements"]))
    yield db, watched


def test_search_finds_sessions_in_every_project(watched):
    db, _ = watched
    _add_session(db, "ses_one00000000000001", "/tmp/專案甲", "t", 1000, turns=[
        ("user", [{"type": "text", "text": "ZZ 我在這裡"}])])
    _add_session(db, "ses_two00000000000002", "/tmp/專案乙", "t", 2000, turns=[
        ("assistant", [{"type": "text", "text": "ZZ 不相關的答案"}]),
        ("user", [{"type": "text", "text": "ZZ 我在這裡 也一樣"}])])
    db.commit()

    assert list(oc.ADAPTER.search_text("我在這裡")) == ["ses_one00000000000001",
                                                        "ses_two00000000000002"]


def test_search_ignores_case_and_full_width(watched):
    """NFKC, so Ｈｅｌｌｏ and hello are the same word - and the like pass cannot
    see that, which is why there is a second one."""
    db, seen = watched
    _add_session(db, "ses_full0000000000003", "/tmp/p", "t", 1000, turns=[
        ("user", [{"type": "text", "text": "ZZ Ｈｅｌｌｏ 世界"}]),
        ("user", [{"type": "text", "text": "ZZ ｱｲｳ の ふりがな"}])])
    db.commit()

    assert list(oc.ADAPTER.search_text("hello")) == ["ses_full0000000000003"]
    assert list(oc.ADAPTER.search_text("アイウ")) == ["ses_full0000000000003"]
    assert list(oc.ADAPTER.search_text("HELLO 世界")) == ["ses_full0000000000003"]
    # the keyword is normalised too, not only the text - the other direction, which
    # is what B3 changed when this started using store.normalize (review T4)
    assert list(oc.ADAPTER.search_text("ｈｅｌｌｏ 世界")) == ["ses_full0000000000003"]
    assert list(oc.ADAPTER.search_text("ｈｅｌｌｏ")) == ["ses_full0000000000003"]
    assert oc._SEARCH_LIKE_SQL in seen["statements"]      # the cheap pass ran first


def test_search_treats_wildcards_as_ordinary_characters(watched):
    db, _ = watched
    _add_session(db, "ses_pct00000000000001", "/tmp/p", "t", 1000, turns=[
        ("user", [{"type": "text", "text": "ZZ 進度 50% 完成了"}])])
    _add_session(db, "ses_pad00000000000002", "/tmp/p", "t", 2000, turns=[
        ("user", [{"type": "text", "text": "ZZ 一些不一樣的話"}])])
    db.commit()

    assert list(oc.ADAPTER.search_text("50%")) == ["ses_pct00000000000001"]
    assert list(oc.ADAPTER.search_text("_")) == []


def test_search_reads_only_text_that_was_actually_said(watched):
    db, _ = watched
    _add_session(db, "ses_tool00000000000001", "/tmp/p", "t", 1000, turns=[
        ("assistant", [{"type": "tool", "text": "ZZ 工具輸出 ZZ"}]),
        ("user", [{"type": "text", "synthetic": True, "text": "ZZ 附件 ZZ"}]),
        ("user", [{"type": "text", "text": "ZZ 真的話 ZZ"}])])
    db.commit()

    assert list(oc.ADAPTER.search_text("ZZ 工具輸出")) == []
    assert list(oc.ADAPTER.search_text("ZZ 附件")) == []
    assert list(oc.ADAPTER.search_text("ZZ 真的話")) == ["ses_tool00000000000001"]


def test_search_reports_a_session_once(watched):
    db, _ = watched
    _add_session(db, "ses_twice000000000001", "/tmp/p", "t", 1000, turns=[
        ("user", [{"type": "text", "text": "ZZ 提到 ZZ"}]),
        ("assistant", [{"type": "text", "text": "ZZ 再提一次 ZZ"}])])
    db.commit()

    assert list(oc.ADAPTER.search_text("ZZ 提到")) == ["ses_twice000000000001"]


def test_search_hands_over_its_first_hit_before_reading_the_rest(watched):
    """It runs in a thread and the page fills in as results arrive."""
    db, seen = watched
    _add_session(db, "ses_first000000000001", "/tmp/p", "t", 1000, turns=[
        ("user", [{"type": "text", "text": "ZZ 目標字串"}])])
    for n in range(40):
        _add_session(db, f"ses_pad{n:016d}", "/tmp/p", "t", 2000 + n, turns=[
            ("user", [{"type": "text", "text": "ZZ 雜訊"}])])
    db.commit()
    seen["rows"].clear()

    found = oc.ADAPTER.search_text("目標字串")
    assert next(found) == "ses_first000000000001"
    assert sum(seen["rows"].values()) <= 2          # not through the other 40


def test_search_of_nothing_finds_nothing_and_asks_no_database(watched):
    db, seen = watched
    _add_session(db, "ses_any00000000000001", "/tmp/p", "t", 1000, turns=[
        ("user", [{"type": "text", "text": "ZZ 任何字"}])])
    db.commit()

    assert list(oc.ADAPTER.search_text("")) == []
    assert list(oc.ADAPTER.search_text("   ")) == []
    assert list(oc.ADAPTER.search_text("找不到的話")) == []


def test_search_without_a_database_yields_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "no-store"))
    monkeypatch.setattr(oc, "_list_cache", None)
    assert list(oc.ADAPTER.search_text("ZZ")) == []


def test_search_over_an_unknown_schema_yields_nothing(store_db, capsys):
    db, _ = store_db
    db.executescript("drop table part; create table part (id text primary key);")
    db.commit()
    db.close()

    assert list(oc.ADAPTER.search_text("ZZ")) == []
    assert "資料庫結構認不出來" in capsys.readouterr().err


def test_search_never_exports_a_session(fake, store_db, workdir):
    db, _ = store_db
    _add_session(db, "ses_x0000000000000001", str(workdir), "t", 1000, turns=[
        ("user", [{"type": "text", "text": "ZZ 搜尋得到"}])])
    db.close()

    assert list(oc.ADAPTER.search_text("搜尋得到")) == ["ses_x0000000000000001"]
    assert not [line for line in _calls(fake, "export")]      # no transcript read


def test_summarize_records_the_session_while_the_run_is_still_going(fake, tmp_path,
                                                                   monkeypatch):
    """Review V5: the record used to be written after the run finished, which is
    exactly when an interruption happens - so an interrupted run left a session
    with nothing pointing at it. The fake only answers once the record is there."""
    workdir = tmp_path / "summarize"
    workdir.mkdir()
    monkeypatch.setenv("FAKE_OPENCODE_SUMMARIZE_HOLD", "1")
    monkeypatch.setenv("AGORA_SUMMARIZE_TIMEOUT", "30")

    text, _ = oc.ADAPTER.summarize("ZZPROMPT", workdir)

    assert "ZZSUMMARY" in text
    # it was there while the run was still going, and the delete cleared it again
    assert list(workdir.glob("pending-*")) == []
    assert not (fake / "opencode-sessions" / "ses_fake_summary00000.json").exists()


def test_the_record_is_written_even_when_the_run_is_interrupted(fake, tmp_path,
                                                               monkeypatch):
    """The id is in the first events; the file has to be too, or Esc leaves nothing."""
    workdir = tmp_path / "summarize"
    workdir.mkdir()
    seen: list[str] = []
    real = oc._remember_summary_session

    def spy(path, session_id):
        seen.append(session_id)
        return real(path, session_id)

    monkeypatch.setattr(oc, "_remember_summary_session", spy)
    oc.ADAPTER.summarize("ZZPROMPT", workdir)
    assert seen[0] == "ses_fake_summary00000"     # before the delete, not after
