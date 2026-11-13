"""The aicomm skill file and docs: the soft rules are written down, no private info (#212)."""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
SKILL = REPO / "skills" / "aicomm" / "SKILL.md"
DOCS = REPO / "docs" / "aicomm.md"


def _skill() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_skill_has_frontmatter_with_name_and_description():
    text = _skill()
    head = text.split("---")[1]
    assert re.search(r"^name: aicomm$", head, re.M)
    assert re.search(r"^description: \S", head, re.M)


def test_skill_names_the_two_commands_and_nothing_else():
    text = _skill()
    assert "aicomm send" in text and "aicomm inbox" in text
    assert "aicomm poke" not in text and "aicomm status" not in text


def test_skill_states_the_three_soft_rules():
    text = _skill()
    assert "預設不中斷" in text and "--interrupt" in text
    assert "隊員基本上不要中斷" in text and "PM" in text and "PMO" in text
    rule3 = next(l for l in text.splitlines() if l.startswith("3. "))
    assert "「收到」類回覆" in rule3 and "--no-wake" in rule3 and "不叫醒" in rule3


def test_skill_points_hard_control_at_sidecar_open_not_at_aicomm_and_states_the_cost():
    text = _skill()
    assert "sidecar open" in text and "白名單" in text and "MyLinuxPool#346" in text
    assert "MyPMO#297" in text  # the cross-machine half is decided there, not in aicomm
    assert "pool 內任何 agent（含別台機器上的" in text and "都能叫醒 PMO" in text
    assert "M2 D6" in text


def test_c29_the_skill_says_the_cost_covers_the_whole_pool():
    # #368 PR-2: a recipient on another machine can be woken too, so "同台" is no longer true.
    text = _skill()
    assert "同台" not in text
    assert "pool 內任何 agent（含別台機器上的，經 `127.0.0.1:<port>` 的 sidecar）都能叫醒 PMO" in text


def test_c29_the_skill_says_the_cross_machine_access_control_is_elsewhere():
    text = _skill()
    assert "本機走 sidecar open 帶白名單（MyLinuxPool#346）" in text
    assert "跨機（經 `127.0.0.1:<port>` 的 sidecar）的存取控制併在 MyPMO#297 再議" in text


def test_skill_explains_delivery_exit_codes_and_the_idle_wait():
    text = _skill()
    assert "送達" in text and "寫進 Angareion" in text
    for code in ("exit 0", "exit 1", "exit 2", "exit 3"):
        assert code in text
    assert "inbox --wait" in text
    assert "不要在訊息裡放私人資訊" in text


def test_skill_does_not_tell_the_agent_to_inbox_at_the_end_of_every_round():
    assert "每輪結束前" not in _skill()


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_a_new_identitys_first_inbox_is_written_down(path):
    """#224: own messages from the start, old broadcasts only with --since."""
    text = path.read_text(encoding="utf-8")
    assert "第一次" in text
    assert "舊廣播" in text
    assert "angareion read --since" in text
    assert "wait" in text  # `--wait` may end on an old broadcast; that is not a lost message


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_no_secrets_or_private_info_patterns(path):
    text = path.read_text(encoding="utf-8")
    assert not re.search(r"gh[pousr]_[A-Za-z0-9]", text)
    assert not re.search(r"\bsk-[A-Za-z0-9]", text)
    assert "BEGIN" not in text  # no PEM blocks
    assert "@gmail.com" not in text
    # no machine addresses: the only IP allowed is the sidecar's documented loopback default
    ips = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text))
    assert ips <= {"127.0.0.1", "127.0.0.0"}  # the sidecar default and the loopback range spelling
    assert not re.search(r"/Users/|/home/[a-z]", text)


def test_docs_record_the_sidecar_fields_aicomm_depends_on():
    text = DOCS.read_text(encoding="utf-8")
    for needle in ("GET /agents/", "POST /agents/", "/poke", "state", "idle", "running", "404", "AICOMM_SIDECAR", "127.0.0.1:8471"):
        assert needle in text


# --- #219: one reader per identity ---

REMEDY = "angareion read --since <時刻>"
TIME_FORMAT = "RFC 3339，例如 2026-10-25T03:00:00Z；不移動游標"


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_one_reader_rule_and_remedy_are_written_down(path):
    text = path.read_text(encoding="utf-8")
    assert "同一個身分只留一個讀者" in text
    assert "背景" in text and "前景" in text
    assert "inbox --wait" in text
    assert "被叫醒但 inbox 是空的" in text
    assert REMEDY in text and TIME_FORMAT in text
    assert "--channel <ch> --since" not in text


def test_skill_says_mention_is_open_and_the_upgrade_ban_is_gone():
    text = _skill()
    assert "`--mention` 可以用了" in text
    assert "讀得懂 `mentions`" in text
    assert "先升級" not in text


def test_skill_states_the_one_reader_rule_itself():
    text = _skill()
    assert "不要跑背景讀信迴圈" in text
    assert "**不能**當成「背景只等、不讀」" in text
    assert "wait 之後會接著 read" in text
    assert "才用 `aicomm inbox --wait` 當**唯一**的讀者" in text


def test_docs_state_the_one_reader_rule_itself():
    text = DOCS.read_text(encoding="utf-8")
    assert "不跑背景讀信迴圈" in text
    assert "沒有人會 poke 的，才用 `aicomm inbox --wait` 當唯一的讀者" in text


def test_idle_wait_is_limited_to_agents_nobody_pokes():
    text = _skill()
    description = re.search(r"^description: (.*)$", text, re.M).group(1)
    assert "not managed by a sidecar" in description
    idle_line = next(line for line in text.splitlines() if "aicomm inbox --wait 600" in line)
    assert "只限沒有人會 poke 的" in idle_line
    assert "被 sidecar 管的，閒著就結束這一輪、等 poke" in idle_line


def test_the_hint_in_the_cli_matches_the_documented_remedy():
    cli = (REPO / "src" / "aicomm" / "cli.py").read_text(encoding="utf-8")
    assert REMEDY in cli and "同一個身分" in cli


# --- #258: AI finds the owner at the App address ---


def test_skill_gives_the_owner_address_and_keeps_the_pmo_rule():
    """The owner's address is written down; decisions still go through PMO (#258)."""
    text = _skill()
    assert "--to myaientry-secretary-p1" in text
    assert "經 PMO" in text


def test_skill_says_inline_at_is_plain_text_and_dispatch_is_secretary_pmo_only():
    """W1: inline '@' does not deliver; W2: only secretary-pmo dispatches (#271)."""
    text = _skill()
    assert "內文寫 `@ myaientry-secretary-p1` 只是普通文字" in text
    assert "廣播會進本人的 inbox，但不叫醒、也不標 @你" in text
    assert "App 不會把它當成 @你" in text
    assert "App 只看 `to`／`mentions`" in text
    assert "本人不在 sidecar 上，所以不會被叫醒" in text
    assert "只認 `secretary-pmo`" in text
    assert "自由文字不當派工" in text


# --- #254: aicomm log ---

def test_skill_and_docs_say_three_commands():
    for path in (SKILL, DOCS):
        text = path.read_text(encoding="utf-8")
        assert "三個指令" in text


def test_skill_and_docs_have_log_division_sentence():
    for path in (SKILL, DOCS):
        text = path.read_text(encoding="utf-8")
        assert "被叫醒先 `aicomm inbox`" in text or "被叫醒先" in text
        assert "aicomm log" in text
        assert "不動游標" in text


def test_cli_hint_contains_appended_plain_sentence():
    cli = (REPO / "src" / "aicomm" / "cli.py").read_text(encoding="utf-8")
    assert "要補查用 aicomm log（不動游標）。" in cli


def test_injection_literals_sync_with_cli():
    cli = (REPO / "src" / "aicomm" / "cli.py").read_text(encoding="utf-8")
    assert '"to"' in cli and '"mentioned"' in cli


# --- #278: send --ref ---


def test_skill_usage_line_lists_ref():
    usage = _skill().split("```bash")[1].split("```")[0]
    assert "--ref <name>=<locator>" in usage


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_ref_is_documented_as_a_reference_that_still_wakes(path):
    text = path.read_text(encoding="utf-8")
    assert "--ref <name>=<locator>" in text
    assert "仍然會叫醒" in text
    assert "angareion send --ref" in text and "不叫醒" in text


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_ref_without_an_equals_sign_is_written_down(path):
    text = path.read_text(encoding="utf-8")
    assert "沒有 `=`" in text and "exit 1" in text


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_a_ref_example_is_a_link(path):
    text = path.read_text(encoding="utf-8")
    assert "--ref pr=https://github.com/" in text


# --- #315: the two ways to send (不中斷／立刻看) written in one section ---


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_two_ways_to_send_are_written_down_in_one_section(path):
    text = path.read_text(encoding="utf-8")
    assert "送訊息的兩種送法" in text
    assert "`--interrupt`（立刻看）" in text
    assert "沒給 `--to`／`--mention` 的廣播誰都不戳" in text
    assert "太久沒回就再發一次 `--interrupt`" in text
    assert "`idle` 或 `running` 才戳" in text
    assert "不能同時給" in text
    assert "只用這一次" in text


# --- #368 PR-1: exit 0 only means delivered; the `wake:` line says who was woken ----------


def _documented_reason_codes(text: str) -> tuple[set[str], set[str]]:
    """The reason codes a doc lists, as cli spells them (`state-<state>` -> `state-`)."""
    def codes(label: str) -> set[str]:
        (line,) = [l.strip() for l in text.splitlines() if l.strip().startswith(f"- `{label}` 的原因碼：")]
        found = set(re.findall(r"`([^`]+)`", line)) - {label}
        return {code.split("<")[0] for code in found}

    return codes("not-woken"), codes("unknown")


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_c23_both_files_say_exit_0_only_means_delivered(path):
    text = path.read_text(encoding="utf-8")
    assert "exit 0 只代表已送達" in text
    assert "`wake:` 行" in text
    assert "exit 4" in text and "不要重送" in text


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_c23_the_documented_reason_codes_are_the_cli_constants(path):
    from aicomm import cli

    not_woken, unknown = _documented_reason_codes(path.read_text(encoding="utf-8"))
    assert not_woken == set(cli.NOT_WOKEN_CODES)
    assert unknown == set(cli.UNKNOWN_CODES)


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_c18_the_wake_line_grammar_is_written_down(path):
    text = path.read_text(encoding="utf-8")
    for form in ("wake: <agent> woken local",
                 "wake: <agent> woken local queued record=local/<id>",
                 "wake: <agent> woken peer:<port>[,<port>…]",
                 "wake: <agent> not-woken <原因碼>",
                 "wake: <agent> unknown <原因碼>",
                 "record=<records>"):
        assert form in text
    assert "每個收件人" in text and "一行" in text


def test_c19_the_skill_usage_line_lists_require_wake():
    usage = _skill().split("```bash")[1].split("```")[0]
    assert "--require-wake" in usage


# --- #457: the cross-machine fallback (sidecar-link) is written down -----------------------


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_the_transitional_no_peer_fallback_sentences_are_gone(path):
    text = path.read_text(encoding="utf-8")
    assert "還沒提供" not in text
    assert "要等跨機後援" not in text
    assert "peer:<port> 那一種還沒提供" not in text


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_the_old_cross_machine_command_is_gone(path):
    text = path.read_text(encoding="utf-8")
    assert "pool-peer" not in text
    assert "AICOMM_POOL_PEER" not in text


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_a_broken_port_list_is_never_folded_into_peer_unavailable(path):
    """#457 review low-1: a broken `ports` file is `peer-bad-answer`/`peer-port-out-of-range`.

    The "cannot look" list may only hold the cases that really mean `peer-unavailable`
    (no heartbeat, stale heartbeat, no list, nothing left after the local port).
    """
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "不可用（" in line:
            inside = line.split("不可用（", 1)[1].split("）", 1)[0]
            assert "壞" not in inside, inside
    assert "清單壞掉）" not in text  # the shape the first draft of SKILL.md had
    assert "`peer-bad-answer`" in text and "`peer-port-out-of-range`" in text


def test_docs_record_the_sidecar_link_files_and_the_two_routes():
    text = DOCS.read_text(encoding="utf-8")
    for needle in ("~/.mylinuxpool/sidecar-link/", "`heartbeat`", "`ports`",
                   "AICOMM_SIDECAR_LINK", "LINK_FRESH_SECONDS", "PEER_LOOKUP_BUDGET",
                   "127.0.0.1:P/agents/<id>", "127.0.0.1:P/agents/<id>/poke",
                   "peer:<port>", "`aicomm-peer`"):
        assert needle in text, needle


def test_a4_the_docs_say_a_peer_port_is_not_a_machine_address():
    # A.4: `peer:<port>` names a local sidecar-link port, never a machine.
    text = DOCS.read_text(encoding="utf-8")
    assert "不是機器位址" in text and "`peer:<port>`" in text


def test_a4_the_docs_say_the_wake_line_does_not_relay_an_answer_body():
    text = DOCS.read_text(encoding="utf-8")
    assert "sidecar 回應的 body 不會被轉印進去" in text


def test_the_docs_describe_the_order_local_first_then_the_link():
    for path in (SKILL, DOCS):
        text = path.read_text(encoding="utf-8")
        assert "先問本機" in text or "先本機" in text
        assert "sidecar-link" in text


# --- #406: the queued outcome and the sidecar record id are written down -------------------


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_406_c14_queued_and_the_record_id_are_written_down(path):
    text = path.read_text(encoding="utf-8")
    for needle in (
        "woken local queued",
        "record=",
        "回報一律帶 `--to`",
        "GET /pokes/",
        "`queued` 不要重送",
        "不是原因碼",
        "poke_not_found",
        "不代表沒叫醒",
        "每一行之後沒有其他文字",
    ):
        assert needle in text, needle


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_406_c14_the_two_record_queries_are_written_down(path):
    text = path.read_text(encoding="utf-8")
    assert "curl -s 127.0.0.1:8471/pokes/<id>" in text
    assert "curl -s 127.0.0.1:<port>/pokes/<id>" in text


@pytest.mark.parametrize("path", [SKILL, DOCS], ids=["skill", "docs"])
def test_406_c14_the_explanation_field_is_gone(path):
    text = path.read_text(encoding="utf-8")
    assert "<說明>" not in text


def test_413_docs_say_the_poke_body_carries_via_and_ref():
    text = (REPO / "docs" / "aicomm.md").read_text(encoding="utf-8")
    assert '"via": "aicomm"' in text and "aicomm-peer" in text
    assert "不貼進對方的輸入" in text and "[A-Za-z0-9_-]{1,64}" in text
    assert '"interrupt": <bool>}' not in text  # no body listed with only two keys
