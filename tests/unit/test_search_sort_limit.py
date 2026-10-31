"""#302: `agora search --sort updated` orders by when the header last moved, and
`--limit` takes the top N after the sort and the same-source dedupe - in both forms.

`date` (the default) has to leave the output byte for byte untouched; that piece is
checked here and held by the wider `-k search` run.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import pytest

from agora import cli, store
from test_search_json import _header, _staged, remote, run  # noqa: F401,E402  (remote is a fixture; imported so pytest finds it)
import json as _json

# two ULIDs whose order the test picks: LOW < HIGH, no clock involved (#302 review 低4)
LOW = "01K" + "0" * 22 + "1"
HIGH = "01K" + "0" * 22 + "2"


def _ids(text: str) -> list[str]:
    return [line.split()[0] for line in text.splitlines()]


def _staged_as(ulid: str, *, body="## user\n把 CSV 轉成 Markdown 表格\n", **kw) -> str:
    """`_staged`, with the ULID fixed by the test instead of by the clock."""
    hdr = _header(**kw)
    hdr["id"] = f"agora:{ulid}"
    paths = store.Paths.from_env()
    folder = store.stage(paths, hdr, body, b'{"x": 1}')
    store.remember(paths, folder)
    return hdr["id"]


def test_the_default_and_sort_date_are_the_same_row_list(remote, capsys):
    """No flag and `--sort date` name the same rows in the same order: the default
    really is the old behaviour, a flag nobody passes changes nothing."""
    early = _staged(session_id="ses_a", created_at="2026-10-01T09:00:00Z",
                    updated_at="2026-10-19T09:00:00Z", title="早的對話")
    late = _staged(session_id="ses_b", created_at="2026-10-02T09:00:00Z",
                   updated_at="2026-10-10T09:00:00Z", title="晚的對話")
    code, plain, _ = run(capsys, "search", "session", "--no-sync")
    code2, dated, _ = run(capsys, "search", "session", "--sort", "date", "--no-sync")
    assert code == 0 and code2 == 0
    assert plain == dated != ""
    assert _ids(plain) == [late, early]          # the conversation date, newest first


def test_sort_updated_orders_by_when_the_header_moved(remote, capsys):
    """A session created earlier but touched later comes first under `--sort updated`,
    while the default keeps ordering by the conversation."""
    early_touched = _staged(session_id="ses_a", created_at="2026-10-01T09:00:00Z",
                            updated_at="2026-10-20T09:00:00Z", title="早對話，晚改標頭")
    late_touched = _staged(session_id="ses_b", created_at="2026-10-02T09:00:00Z",
                           updated_at="2026-10-05T09:00:00Z", title="晚對話，早改標頭")
    code, plain, _ = run(capsys, "search", "session", "--no-sync")
    code2, moved, _ = run(capsys, "search", "session", "--sort", "updated", "--no-sync")
    assert code == 0 and code2 == 0
    assert _ids(plain) == [late_touched, early_touched]
    assert _ids(moved) == [early_touched, late_touched]


def test_a_tie_on_updated_at_goes_to_the_bigger_ulid(remote, capsys):
    """#302: `updated` ties break by ULID, newest ULID first. The ULIDs are fixed here,
    so the order does not depend on the clock or the random bits."""
    low = _staged_as(LOW, session_id="ses_x", created_at="2026-10-01T09:00:00Z",
                     updated_at="2026-10-20T09:00:00Z", title="先")
    high = _staged_as(HIGH, session_id="ses_y", created_at="2026-10-01T09:00:00Z",
                      updated_at="2026-10-20T09:00:00Z", title="後")
    code, out, _ = run(capsys, "search", "session", "--sort", "updated", "--no-sync")
    assert code == 0
    assert _ids(out) == [high, low]


def test_the_default_order_breaks_a_same_day_tie_by_updated_at(remote, capsys):
    """#302 review 低3: the default's second key is updated_at. Same conversation date,
    different updated_at: the one touched later comes first. It is staged with the
    SMALLER ULID, so only the updated_at key can put it on top."""
    same_day = "2026-10-05T09:00:00Z"
    later = _staged_as(LOW, session_id="ses_a", created_at=same_day,
                       updated_at="2026-10-07T09:00:00Z", title="後動的")
    earlier = _staged_as(HIGH, session_id="ses_b", created_at=same_day,
                         updated_at="2026-10-06T09:00:00Z", title="先動的")
    code, out, _ = run(capsys, "search", "session", "--no-sync")
    assert code == 0
    assert _ids(out) == [later, earlier]


def test_sort_updated_picks_the_same_rows_in_both_forms(remote, capsys):
    """--json obeys the same order; each row keeps its own updated_at (RFC 3339)."""
    first = _staged(session_id="ses_a", created_at="2026-10-01T09:00:00Z",
                    updated_at="2026-10-20T09:00:00Z", title="最早動的")
    second = _staged(session_id="ses_b", created_at="2026-10-02T09:00:00Z",
                     updated_at="2026-10-21T09:00:00Z", title="隨後動的")
    code, text, _ = run(capsys, "search", "session", "--sort", "updated", "--no-sync")
    code2, out, _ = run(capsys, "search", "session", "--sort", "updated", "--json", "--no-sync")
    assert code == 0 and code2 == 0
    rows = _json.loads(out)
    assert [row["id"] for row in rows] == _ids(text) == [second, first]
    assert [row["updated_at"] for row in rows] == ["2026-10-21T09:00:00Z", "2026-10-20T09:00:00Z"]


def test_limit_takes_the_top_n_after_the_sort_and_the_dedupe(remote, capsys):
    """Five hits, one pair of them the same source session: the limit counts kept
    rows, the duplicate never takes a slot, and a later row still fills in."""
    third_date = _staged(session_id="ses_1", created_at="2026-10-05T09:00:00Z",
                         updated_at="2026-10-05T09:00:00Z", title="新的")
    dup_new = _staged(session_id="ses_dup", created_at="2026-10-04T09:00:00Z",
                      updated_at="2026-10-04T09:00:00Z", title="重複的新一份")
    dup_old = _staged(session_id="ses_dup", created_at="2026-10-03T09:00:00Z",
                      updated_at="2026-10-03T09:00:00Z", title="重複的舊一份")   # deduped away
    kept_second = _staged(session_id="ses_2", created_at="2026-10-02T09:00:00Z",
                          updated_at="2026-10-02T09:00:00Z", title="第二個要留下的")
    _staged(session_id="ses_3", created_at="2026-10-01T09:00:00Z",
            updated_at="2026-10-01T09:00:00Z", title="超出名額")        # the 5th, pushed out
    code, out, err = run(capsys, "search", "session", "--limit", "3", "--no-sync")
    assert code == 0
    assert _ids(out) == [third_date, dup_new, kept_second], "in the sorted order"
    code2, out_json, _ = run(capsys, "search", "session", "--limit", "3", "--json", "--no-sync")
    assert code2 == 0
    assert [row["id"] for row in _json.loads(out_json)] == _ids(out)
    # the same-source explanation stays on stderr, like it does without --limit
    assert f"{dup_new} 與 agora:{dup_old.split(':', 1)[1]} 來自同一個來源 Session" in err


def test_limit_one_with_only_a_dedupe_pair_leaves_one_row(remote, capsys):
    """The duplicate is not printed at all and still does not eat the one slot."""
    dup_new = _staged(session_id="ses_dup", created_at="2026-10-04T09:00:00Z",
                      updated_at="2026-10-04T09:00:00Z", title="新的")
    _staged(session_id="ses_dup", created_at="2026-10-03T09:00:00Z",
            updated_at="2026-10-03T09:00:00Z", title="舊的")
    code, out, _ = run(capsys, "search", "session", "--limit", "1", "--no-sync")
    assert code == 0 and _ids(out) == [dup_new]


def test_limit_zero_and_negatives_are_refused_before_anything(remote, capsys):
    """0 and negatives say so and exit 1 (EXIT_INPUT), the --sync-period style."""
    _staged()
    for bad, why in (("0", "1 以上"), ("-3", "1 以上")):
        code, out, err = run(capsys, "search", "session", "--limit", bad, "--no-sync")
        assert code == 1, bad
        assert out == "", bad
        assert "[agora]" in err and why in err, bad


def test_limit_is_refused_before_the_sync_too(remote, capsys, monkeypatch):
    """#302 review 中1: "before anything" includes the sync. Without --no-sync, a bad
    --limit must stop before store.sync is ever called."""
    _staged()
    synced = []

    def spy(paths, **kw):
        synced.append(kw)
        raise RuntimeError("sync must not run for a refused --limit")

    monkeypatch.setattr(store, "sync", spy)
    for bad in ("0", "-3"):
        code, out, err = run(capsys, "search", "session", "--limit", bad)
        assert synced == [], f"a refused --limit {bad} must not reach the sync"
        assert code == 1, bad
        assert out == "", bad
        assert "1 以上" in err, bad


def test_limit_that_is_not_a_number_bounces_at_the_parser(capsys):
    """A non-integer is already argparse's business: exit 2, like --headless-port."""
    with pytest.raises(SystemExit) as e:
        cli.main(["search", "session", "--limit", "abc", "--no-sync"])
    assert e.value.code == 2
    assert "invalid int value" in capsys.readouterr().err


def test_a_sort_value_the_parser_does_not_know_bounces(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["search", "session", "--sort", "size"])
    assert e.value.code == 2
    assert "(choose from" in capsys.readouterr().err


def test_sort_and_limit_are_refused_by_every_other_action_and_run_nothing(remote, capsys, monkeypatch):
    """#302 review 中2: `--sort` and `--limit` are search's. Any other action that gets one
    says so and exits 1 (EXIT_INPUT) before it touches anything, the same rule as --json (#301)."""
    agora_id = _staged()
    ran = []
    for action in ("show", "import", "edit"):
        monkeypatch.setitem(cli.ACTIONS, action, lambda args, paths, _a=action: ran.append(_a) or 0)
    base_by_action = {
        "show": ["show", "session", agora_id],
        "import": ["import", "session", "--agent", "opencode"],
        "edit": ["edit", "session", agora_id],
    }
    for action, base in base_by_action.items():
        for flags in (["--limit", "1"], ["--sort", "updated"], ["--sort", "date"]):
            code, out, err = run(capsys, *base, *flags)
            assert code == 1, (action, flags)
            assert out == "", (action, flags)
            assert f"{action} 不吃 --sort／--limit（只有 search 吃）" in err, (action, flags)
    assert ran == [], "a refused action must not run"


def test_edit_add_tag_moves_a_session_up_only_under_sort_updated(remote, capsys):
    """DRAFT-205 M2: `edit --add-tag` refreshes `updated_at`, so the edited session
    rises to the top of `--sort updated` while `date` keeps its old place."""
    early = _staged(session_id="ses_a", created_at="2026-10-01T09:00:00Z",
                    updated_at="2026-10-01T09:00:00Z", title="早的")
    late = _staged(session_id="ses_b", created_at="2026-10-02T09:00:00Z",
                   updated_at="2026-10-02T09:00:00Z", title="晚的")
    code, out, _ = run(capsys, "search", "session", "--sort", "updated", "--no-sync")
    assert code == 0 and _ids(out) == [late, early]
    code2, out, _ = run(capsys, "edit", "session", early, "--add-tag", "touched")
    assert code2 == 0
    code3, moved, _ = run(capsys, "search", "session", "--sort", "updated", "--no-sync")
    assert code3 == 0 and _ids(moved) == [early, late]
    code4, dated, _ = run(capsys, "search", "session", "--no-sync")
    assert code4 == 0 and _ids(dated) == [late, early], "the conversation order did not move"
