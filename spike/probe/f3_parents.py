#!/usr/bin/env python3
"""1.4f3：多層前綴逐層檢查同名資料夾（M2／H6）。

前綴 `a/b/c` 的檢查：
  - 在 parent（例如 SPIKE_FOLDER_ID）之下，name=='a' 的項目必須恰好一個、id == 期望；
  - 在那一層之下，name=='b' 必須恰好一個、id == 期望；
  - 在那一層之下，name=='c' 必須恰好一個、id == 期望。
任何一層多出同名資料夾 → 移到隔離資料夾（不刪除）；任何移動失敗 → exit 2。
少於一個（找不到）也 exit 2（fail-closed）。

用法：
  f3_parents.py --cred <conf> --root <上層資料夾id> --prefix a/b/c \
      --expected-ids id_a,id_b,id_c --quarantine <id> [--apply]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drive_probe import API, call, load_token, reason_of  # noqa: E402


def find(token, parent, name):
    r = call("GET", f"{API}/files", token, params={
        "q": f"name='{name}' and '{parent}' in parents and trashed=false",
        "fields": "files(id,name)", "pageSize": 100,
    })
    if not r.ok:
        reason, msg = reason_of(r)
        print(f"find.http={r.status_code} reason={reason} message={msg}")
        sys.exit(2)
    return r.json().get("files", [])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cred", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--prefix", required=True)
    p.add_argument("--expected-ids", required=True, help="逗號分隔，逐層的期望資料夾 id")
    p.add_argument("--quarantine", required=True)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    token, _ = load_token(args.cred)
    parts = [x for x in args.prefix.split("/") if x]
    expected = [x for x in args.expected_ids.split(",") if x]
    if len(parts) != len(expected):
        print(f"prefix parts ({len(parts)}) != expected ids ({len(expected)})")
        sys.exit(2)

    failures = []
    parent = args.root
    for i, (name, want) in enumerate(zip(parts, expected)):
        hits = find(token, parent, name)
        marks = [(f["id"] == want) for f in hits]
        level = "/".join(parts[: i + 1])
        print(f"level={level} parent={parent} count={len(hits)} expected_id={want}")
        for f, is_want in zip(hits, marks):
            print(f"  [{'EXPECTED' if is_want else 'DUPLICATE'}] id={f['id']}")
        if sum(marks) != 1:
            print(f"level={level} FAIL: 期望恰好一個相符資料夾（找到 {sum(marks)} 個）")
            failures.append(level)
        for f, is_want in zip(hits, marks):
            if not is_want:
                if args.apply:
                    r = call("PATCH", f"{API}/files/{f['id']}", token,
                             params={"addParents": args.quarantine, "removeParents": parent,
                                     "fields": "id,name,parents"}, json={})
                    if r.ok:
                        print(f"  moved id={f['id']} http={r.status_code}")
                    else:
                        reason, _ = reason_of(r)
                        print(f"  move_failed id={f['id']} http={r.status_code} reason={reason}")
                        failures.append(level)
                else:
                    print(f"  would_move id={f['id']}")
        parent = want
    if failures:
        print("PARENTS-FAILED")
        return 2
    print("PARENTS-OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
