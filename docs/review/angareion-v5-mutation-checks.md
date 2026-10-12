# Angareion v5 — 4.2 mutation checks

Per tasks.md 4.2: for each implemented task item, one thing was broken in a
`git archive` copy of the repo (a fresh extraction of HEAD `f88e3ed`), and the
item's unit tests were run against the copy. Every mutation turned the
relevant tests red; the unmutated baseline copy runs the same files green
(234 passed). 1.1 has no code (config/README data files), so it has no
mutation row — its format is guarded by 1.2's tests.

Harness notes: each copy ran with its own `src/` first on `sys.path` (the
copied venv's editable `.pth` was removed and replaced with a cwd-based one —
the first attempt silently imported the worktree's package, which is exactly
the false-green this check exists to avoid). `__pycache__` cleared per copy.

| item | mutated file | mutation | item's test files | result (copy) |
|---|---|---|---|---|
| 1.2 | `src/angareion/channel_config.py` | version check in `_load_json` removed | test_angareion_channel_config.py | **1 failed**, 10 passed |
| 1.3 | `src/angareion/channel_config.py` | periodic pull disabled (`ensure_fresh` returns early) | test_angareion_channel_pull.py | **3 failed**, 8 passed |
| 2.1 | `src/angareion/messages.py` | `parse_identity` scans roles left-to-right | test_angareion_address.py + test_angareion_messages.py | **1 failed**, 49 passed |
| 2.2 | `src/angareion/cli.py` | `parse_address` accepts any string | test_angareion_address.py + test_angareion_cli.py | **27 failed**, 66 passed |
| 2.3 | `src/angareion/state.py` | `recipient_cursor_key` drops the channel part | test_angareion_state.py | **1 failed**, 9 passed |
| 3.1 | `src/angareion/backend.py` | `recipient_matches` always True (broadcast filter gone) | test_angareion_backend.py + test_angareion_library.py | **3 failed**, 28 passed |
| 3.2 | `src/angareion/cli.py` | client-side entity verification skipped | test_angareion_cli.py | **1 failed**, 71 passed |
| 3.3 | `src/angareion/cli.py` | `channel delete` no longer closes the entity | test_angareion_cli.py | **6 failed**, 66 passed |
| 3.4 | `src/angareion/registry.py` | action comments with missing fields accepted | test_angareion_registry.py + test_angareion_rebuild.py | **1 failed**, 51 passed |
| 3.5 | `src/angareion/registry_rebuild.py` | later register overwrites (no first-come-first-served) | test_angareion_rebuild.py | **2 failed**, 26 passed |
| 3.6 | `src/angareion/client.py` | "to me" read no longer passes the recipient filter | test_angareion_library.py + test_angareion_cli.py | **2 failed**, 85 passed |
| 3.7 | `skills/angareion/SKILL.md` | the `channel delete` line removed | test_angareion_skill.py | **1 failed**, 7 passed |

Baseline (unmutated copy, same 11 angareion test files): `234 passed in 1.25s`.

What each mutation was caught by (one example per item): 1.2 →
`test_wrong_version_and_non_object_files_are_config_errors`; 1.3 →
`test_ensure_fresh_fetches_when_never_fetched`; 2.1 →
`test_parse_identity_takes_the_name_apart_from_the_right` ("foo-pm-test"); 2.2
→ the address/CLI address-rejection tests; 2.3 → `test_cursor_keys`; 3.1 →
`test_read_to_me_includes_broadcasts_and_excludes_others` (library) and the
backend's recipient-filter tests; 3.2 →
`test_channel_create_verify_failure_exits_2_and_registers_nothing`; 3.3 →
`test_channel_delete_closes_and_unregisters`; 3.4 →
`test_parse_comment_flags_action_attempts`; 3.5 →
`test_first_come_first_served_and_the_loser_is_reported`; 3.6 →
`test_read_to_me_spans_channels_with_one_cursor_per_channel` (library) and the
CLI's to-me test; 3.7 → `test_skill_doc_exists_and_covers_the_five_interfaces`.
