#!/usr/bin/env python3
"""1.4f2 對策 A 的延伸：偵測 repo 前綴的「上一層同名資料夾」。

以 --parent 列出上層資料夾中 name == <prefix 名>（不含結尾 /）的所有項目；
--expected-id 是清冊記下的真資料夾 id。多出來的同名資料夾（file id != expected）
移到隔離資料夾（不刪除）。這一步必須在 clone 前做，因為 rclone 以名稱解析，
同名資料夾會讓 clone 走錯路（H6）。

用法：
  spike/probe/scan_parent_duplicates.py --cred <conf> --parent <SPIKE_FOLDER_ID> \
      --name <前綴名，例如 agora-1.4f2> --expected-id <真資料夾 id> \
      --quarantine <隔離資料夾id> [--apply]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drive_probe import API, call, load_token, reason_of  # noqa: E402


def find_by_name(token, parent_id, name):
    r = call(
        "GET",
        f"{API}/files",
        token,
        params={
            "q": f"name='{name}' and '{parent_id}' in parents and trashed=false",
            "fields": "files(id,name,mimeType,createdTime)",
            "pageSize": 100,
        },
    )
    if not r.ok:
        reason, msg = reason_of(r)
        print(f"find.http={r.status_code} reason={reason} message={msg}")
        sys.exit(2)
    return r.json().get("files", [])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cred", required=True)
    p.add_argument("--parent", required=True)
    p.add_argument("--name", required=True, help="前綴名（不含結尾 /）")
    p.add_argument("--expected-id", required=True)
    p.add_argument("--quarantine", required=True)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    token, _ = load_token(args.cred)
    hits = find_by_name(token, args.parent, args.name)
    print(f"op=scan-parent parent={args.parent} name={args.name} count={len(hits)}")
    for f in hits:
        mark = "EXPECTED" if f["id"] == args.expected_id else "DUPLICATE"
        print(f"  [{mark}] id={f['id']} created={f.get('createdTime')}")

    dups = [f for f in hits if f["id"] != args.expected_id]
    for f in dups:
        if args.apply:
            r = call(
                "PATCH",
                f"{API}/files/{f['id']}",
                token,
                params={"addParents": args.quarantine, "removeParents": args.parent, "fields": "id,name,parents"},
                json={},
            )
            if r.ok:
                print(f"moved id={f['id']} http={r.status_code} to={args.quarantine}")
            else:
                reason, msg = reason_of(r)
                print(f"move_failed id={f['id']} http={r.status_code} reason={reason} message={msg}")
        else:
            print(f"would_move id={f['id']}")
    if not dups:
        print("no_duplicates")


if __name__ == "__main__":
    main()
