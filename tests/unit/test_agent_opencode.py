"""The opencode adapter (test-plan U-RV-01/03, U-CON-01/01b/02, plus the two
failure paths spike V1 found: the message-count check and id collisions).

The "opencode" here is tests/fakes/fake_opencode.py, so nothing touches the
user's real sessions.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from agora.agents import opencode as oc
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


def test_reading_keeps_text_and_one_line_per_tool_call(fake):
    body = oc.ADAPTER.reading(raw())
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


def test_reading_goes_through_the_shared_formatter(fake):
    """The reading version must come out of base.format_reading, so the merge
    path (U-RV-04) and this adapter cannot drift apart."""
    import agora.agents.base as base

    payload = json.loads(raw())
    expected = base.format_reading(
        [(m["info"]["role"], oc._lines_of(m.get("parts") or []))
         for m in payload["messages"]])
    assert oc.ADAPTER.reading(raw()) == expected
    assert base.TOOL_SUMMARY_MAX == 200


def test_tool_arguments_are_truncated_at_200(fake):
    payload = json.loads(raw())
    payload["messages"][1]["parts"][3]["state"]["input"] = {"blob": "x" * 1000}
    body = oc.reading_of(payload)
    line = next(l for l in body.splitlines() if l.startswith("[tool]"))
    assert line.endswith("…")
    assert len(line) <= 200 + len("[tool] read ") + 1


def test_reading_skips_turns_that_have_no_lines(fake):
    payload = json.loads(raw())
    payload["messages"] = [{"info": {"role": "user"}, "parts": [
        {"id": "prt_x", "type": "reasoning", "text": "ZZTHINK"}]}]
    assert "##" not in oc.reading_of(payload)


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


# --- start_injected (N4) --------------------------------------------------


def test_start_injected_imports_one_user_message_with_the_file(fake, workdir, tmp_path):
    reading = tmp_path / "reading.md"
    reading.write_text("## user\n把 CSV 轉成 Markdown 表格\n", encoding="utf-8")
    launch = oc.ADAPTER.start_injected(reading, workdir)

    assert launch.argv[1:] == ["--session", launch.agent_session_id]
    assert launch.before_count == 1
    landed = stored(fake, launch.agent_session_id)
    assert len(landed["messages"]) == 1
    message = landed["messages"][0]
    assert message["info"]["role"] == "user"
    text = message["parts"][0]["text"]
    assert text.startswith("以下是之前一個 Session 的閱讀版")
    assert "把 CSV 轉成 Markdown 表格" in text
    assert message["info"]["sessionID"] == launch.agent_session_id
    assert message["parts"][0]["messageID"] == message["info"]["id"]


def test_injected_payload_has_every_field_opencode_needs(fake, workdir):
    """opencode refuses a payload that is missing a field, and one it accepts but
    cannot export afterwards loses the whole continue-session result. The
    integration test found this: the first injected payload imported fine and
    then failed at `export` with 'Missing key at ["slug"]'.

    Compared against a real export's shape, which the fixture mirrors.
    """
    payload = oc._injected_payload("ses_" + "H" * 16, "ZZ\n", "probe", workdir)
    real = json.loads(raw())
    # `revert` is what a session the user undid something in carries; a fresh
    # injected one has nothing to revert.
    assert set(payload["info"]) == set(real["info"]) - {"revert"}
    assert isinstance(payload["info"]["permission"], list)
    assert set(payload["info"]["tokens"]["cache"]) == {"read", "write"}
    real_user = next(m for m in real["messages"] if m["info"]["role"] == "user")
    assert set(payload["messages"][0]["info"]) == set(real_user["info"])
    assert set(payload["messages"][0]["info"]["summary"]) == {"diffs"}
    assert set(payload["messages"][0]["info"]["model"]) == {"providerID", "modelID"}


def test_start_injected_reads_the_file_before_launching(fake, workdir, tmp_path):
    """The file is inlined at launch time, so the agent does not need a tool or
    a permission to read a path outside the project (spike V3)."""
    reading = tmp_path / "reading.md"
    reading.write_text("ZZREADING 一段閱讀版\n", encoding="utf-8")
    launch = oc.ADAPTER.start_injected(reading, workdir)
    assert "ZZREADING 一段閱讀版" in \
        stored(fake, launch.agent_session_id)["messages"][0]["parts"][0]["text"]


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
    assert "ZZAPPEND" in oc.ADAPTER.reading(exported.raw)


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


def test_our_injected_reading_version_is_one_line(fake, workdir, tmp_path):
    reading = tmp_path / "r.md"
    reading.write_text("## user\nZZLONG 一整份閱讀版\n" * 50, encoding="utf-8")
    launch = oc.ADAPTER.start_injected(reading, workdir)
    body = oc.ADAPTER.reading(
        (fake / "opencode-sessions" / f"{launch.agent_session_id}.json").read_bytes())
    assert body.count("[注入的閱讀版]") == 1
    assert "ZZLONG" not in body          # otherwise every handoff nests the last one
    assert launch.before_count == 1


def test_an_injected_part_is_marked_synthetic(fake, workdir, tmp_path):
    reading = tmp_path / "r.md"
    reading.write_text("ZZ\n", encoding="utf-8")
    launch = oc.ADAPTER.start_injected(reading, workdir)
    part = stored(fake, launch.agent_session_id)["messages"][0]["parts"][0]
    assert part["synthetic"] is True
    assert part["metadata"]["agora"] == oc.INJECTED_MARK


def test_an_attachment_opencode_inlined_produces_no_line(fake):
    """D6: tool results and inlined attachments are not the user's words."""
    body = oc.ADAPTER.reading(raw())
    assert "ZZATTACHMENT" not in body
    assert "ZZCOMPACT" not in body and "ZZSUBTASK" not in body
    assert "ZZTASKOUT" not in body
    assert "[tool] task" in body        # the call itself is one line, per D6


# --- OC3: the injected path is verified too -------------------------------


def test_start_injected_verifies_and_cleans_up(fake, workdir, tmp_path, monkeypatch):
    reading = tmp_path / "r.md"
    reading.write_text("ZZ 一段閱讀版\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_OPENCODE_DROP", "0")
    with pytest.raises(AgentError, match="數量不對"):
        oc.ADAPTER.start_injected(reading, workdir)
    assert list((fake / "opencode-sessions").glob("*.json")) == \
        [fake / "opencode-sessions" / "default.json"]


def test_a_failed_delete_is_reported_as_such(fake, workdir, tmp_path, monkeypatch):
    reading = tmp_path / "r.md"
    reading.write_text("ZZ\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_OPENCODE_DROP", "0")
    monkeypatch.setenv("FAKE_OPENCODE_FAIL", "delete")
    with pytest.raises(AgentError) as e:
        oc.ADAPTER.start_injected(reading, workdir)
    assert "刪不掉" in str(e.value) and "opencode session delete" in str(e.value)


# --- OC7/OC8: bounded calls, version from the export ----------------------


def test_a_hanging_opencode_becomes_an_error_not_a_hang(fake, workdir, monkeypatch):
    monkeypatch.setenv("AGORA_OPENCODE_TIMEOUT", "1")
    monkeypatch.setenv("FAKE_OPENCODE_SLEEP", "5")
    with pytest.raises(AgentError, match="逾時"):
        oc.ADAPTER.export("default")


def test_the_version_falls_back_to_the_cli_only_when_the_export_has_none(fake):
    payload = json.loads(raw())
    payload["info"].pop("version")
    store = fake / "opencode-sessions" / "noversion.json"
    store.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert oc.ADAPTER.export("noversion").agent_version == "9.9.9"
