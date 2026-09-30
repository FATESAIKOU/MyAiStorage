#!/usr/bin/env python3
"""1.4f2 對策 A：以 file id 清冊清掃 repo／讀取視圖資料夾。

列出目標資料夾的所有子項；file id 不在清冊上的檔（＝不是清冊當時的 committer 檔案集）
移到隔離資料夾（改 parents，不刪除）。輸出只有 file id、檔名、HTTP 狀態與 reason。

秘密處理：--cred 指到的 conf 只讀進記憶體，token 只放 HTTP header；不印出。

用法：
  spike/probe/scan_and_quarantine.py --cred <conf> --folder <repo資料夾id> \
      --inventory <清冊.json> --quarantine <隔離資料夾id> [--apply]
  （不加 --apply 時只列出將被移出的檔案，不動 Drive）
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drive_probe import API, call, load_token, reason_of  # noqa: E402


def list_children(token, folder_id):
    files = []
    page = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "fields": "nextPageToken,files(id,name,mimeType,md5Checksum,size,createdTime)",
            "pageSize": 100,
        }
        if page:
            params["pageToken"] = page
        r = call("GET", f"{API}/files", token, params=params)
        if not r.ok:
            reason, msg = reason_of(r)
            print(f"list.http={r.status_code} reason={reason} message={msg}")
            sys.exit(2)
        body = r.json()
        files += body.get("files", [])
        page = body.get("nextPageToken")
        if not page:
            break
    return files


def load_inventory(path):
    raw = json.load(open(path))
    if isinstance(raw, dict) and "files" in raw:
        return {f["id"] for f in raw["files"]}
    if isinstance(raw, dict):
        return set(raw.values())
    return set(raw)


def move(token, file_id, from_id, to_id):
    return call(
        "PATCH",
        f"{API}/files/{file_id}",
        token,
        params={"addParents": to_id, "removeParents": from_id, "fields": "id,name,parents"},
        json={},
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cred", required=True)
    p.add_argument("--folder", required=True, help="要清掃的資料夾 id（repo 前綴）")
    p.add_argument("--inventory", required=True, help="清冊 JSON（record_inventory.py 的輸出）")
    p.add_argument("--quarantine", required=True, help="隔離資料夾 id")
    p.add_argument("--apply", action="store_true", help="真的移出；不加只列出")
    args = p.parse_args()

    token, _ = load_token(args.cred)
    allowed = load_inventory(args.inventory)
    files = list_children(token, args.folder)
    unknown = [f for f in files if f["id"] not in allowed]

    print(f"op=scan folder={args.folder}")
    print(f"inventory_count={len(allowed)} children_count={len(files)} unknown_count={len(unknown)}")
    for f in files:
        mark = "OK" if f["id"] in allowed else "UNKNOWN"
        print(f"  [{mark}] id={f['id']} name={f['name']} md5={f.get('md5Checksum')}")

    for f in unknown:
        if args.apply:
            r = move(token, f["id"], args.folder, args.quarantine)
            if r.ok:
                print(f"moved id={f['id']} name={f['name']} http={r.status_code} to={args.quarantine}")
            else:
                reason, msg = reason_of(r)
                print(f"move_failed id={f['id']} name={f['name']} http={r.status_code} reason={reason} message={msg}")
        else:
            print(f"would_move id={f['id']} name={f['name']}")


if __name__ == "__main__":
    main()
