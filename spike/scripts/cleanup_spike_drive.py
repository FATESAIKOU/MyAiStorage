#!/usr/bin/env python3
"""
Spike 1.9 清理腳本：Drive 測試資料清理工具
支援步驟 1（committer 清理 SPIKE_FOLDER_ID 下子項）、步驟 2（drive.file client 清理根目錄 aistorage-spike-* 及其所有遞迴內容）、步驟 3（清空垃圾桶）。
嚴格遵守硬規則：不印出任何 token 或機密字串。
"""
import argparse
import configparser
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://www.googleapis.com/drive/v3"
SPIKE_FOLDER_ID = "1Obn3Rj1Quyg1l_2YW0GhXE39FpETeyLj"


def load_cred(conf_path):
    text = open(os.path.expanduser(conf_path)).read()
    cp = configparser.ConfigParser()
    cp.read_string(text)
    sec = cp["gdrive"]
    tok = json.loads(sec.get("token", "{}"))
    return {
        "client_id": sec.get("client_id", ""),
        "client_secret": sec.get("client_secret", ""),
        "refresh_token": tok.get("refresh_token", ""),
    }


def get_token(cred):
    data = urllib.parse.urlencode({
        "client_id": cred["client_id"],
        "client_secret": cred["client_secret"],
        "refresh_token": cred["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    resp = urllib.request.urlopen(req, timeout=30)
    return json.loads(resp.read().decode())["access_token"]


def api_call(method, path, token, params=None, body=None):
    url = f"{API}/{path}" if not path.startswith("http") else path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Authorization": f"Bearer {token}"}
    )
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def reason_of(raw):
    try:
        j = json.loads(raw)
        err = j.get("error", {})
        return err.get("errors", [{}])[0].get("reason", "?"), err.get("message", "")[:120]
    except Exception:
        return "?", ""


def list_children(token, folder_id):
    items = []
    page = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed = false",
            "fields": "nextPageToken,files(id,name,mimeType,parents)",
            "pageSize": 100,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page:
            params["pageToken"] = page
        st, body = api_call("GET", "files", token, params=params)
        if st != 200:
            print(f"ERROR: list_children failed http={st} reason={reason_of(body)[0]}")
            sys.exit(1)
        j = json.loads(body)
        for f in j.get("files", []):
            items.append(f)
        page = j.get("nextPageToken")
        if not page:
            break
    return items


def list_all_files(token, q_filter):
    items = []
    page = None
    while True:
        params = {
            "q": q_filter,
            "fields": "nextPageToken,files(id,name,mimeType,parents)",
            "pageSize": 100,
            "spaces": "drive",
        }
        if page:
            params["pageToken"] = page
        st, body = api_call("GET", "files", token, params=params)
        if st != 200:
            print(f"ERROR: list_all_files failed http={st} reason={reason_of(body)[0]}")
            sys.exit(1)
        j = json.loads(body)
        for f in j.get("files", []):
            items.append(f)
        page = j.get("nextPageToken")
        if not page:
            break
    return items


def get_file_info(token, fid):
    params = {"fields": "id,name,mimeType,parents,trashed"}
    st, body = api_call("GET", f"files/{fid}", token, params=params)
    if st == 200:
        return json.loads(body)
    return None


def delete_file(token, fid):
    if fid == SPIKE_FOLDER_ID:
        raise ValueError(f"CRITICAL ERROR: Attempted to delete SPIKE_FOLDER_ID ({fid})! Aborting.")
    st, body = api_call("DELETE", f"files/{fid}", token)
    return st, body


def main():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="cmd", required=True)

    # Step 1: committer under SPIKE_FOLDER_ID
    p1 = subparsers.add_parser("step1")
    p1.add_argument("--cred", default="~/.config/aistorage-spike/rclone-committer.conf")
    p1.add_argument("--action", choices=["dry-run", "exec"], required=True)

    # Step 2: drive.file clients
    p2 = subparsers.add_parser("step2")
    p2.add_argument("--action", choices=["dry-run", "exec"], required=True)

    # Step 3: empty trash
    p3 = subparsers.add_parser("step3")
    p3.add_argument("--cred", default="~/.config/aistorage-spike/rclone-committer.conf")
    p3.add_argument("--action", choices=["dry-run", "exec"], required=True)

    args = parser.parse_args()

    if args.cmd == "step1":
        cred = load_cred(args.cred)
        tok = get_token(cred)
        items = list_children(tok, SPIKE_FOLDER_ID)
        print(f"=== Step 1 ({args.action}): SPIKE_FOLDER_ID children count = {len(items)} ===")
        for it in items:
            fid = it["id"]
            name = it.get("name", "")
            mime = it.get("mimeType", "")
            is_folder = (mime == "application/vnd.google-apps.folder")
            print(f"{fid}\t{name}\t[{'DIR' if is_folder else 'FILE'}]")

        if args.action == "exec":
            print("\n--- Executing Step 1 permanent deletions ---")
            deleted_count = 0
            for it in items:
                fid = it["id"]
                name = it.get("name", "")
                st, body = delete_file(tok, fid)
                if st in (200, 204):
                    print(f"{fid} http={st} deleted ({name})")
                    deleted_count += 1
                else:
                    r, m = reason_of(body)
                    print(f"{fid} http={st} reason={r} message={m} ({name})")
            print(f"\nStep 1 execution finished: {deleted_count}/{len(items)} items deleted.")
            rem = list_children(tok, SPIKE_FOLDER_ID)
            print(f"Post-verification: SPIKE_FOLDER_ID remaining children count = {len(rem)}")

    elif args.cmd == "step2":
        clients = [
            ("mac-opencode", "~/.config/aistorage-spike/rclone-mac-opencode.conf"),
            ("test-profile", "~/.config/aistorage-spike/rclone-test-profile.conf"),
            ("other-project", "~/.config/aistorage-spike/rclone-other-project.conf"),
        ]

        client_tokens = {}
        client_files = {}
        for cname, cconf in clients:
            if os.path.exists(os.path.expanduser(cconf)):
                tok = get_token(load_cred(cconf))
                client_tokens[cname] = tok
                client_files[cname] = list_all_files(tok, "trashed = false")

        # Discover all folders whose name starts with aistorage-spike-
        target_folder_ids = set()
        matched_items = {}  # fid -> (client_name, file_info)

        for cname, files in client_files.items():
            for f in files:
                fid = f["id"]
                fname = f.get("name", "")
                if fid == SPIKE_FOLDER_ID:
                    continue
                if fname.startswith("aistorage-spike-"):
                    target_folder_ids.add(fid)
                    matched_items[fid] = (cname, f)

        # Recursively find all items under target folders
        while True:
            added = 0
            for cname, files in client_files.items():
                for f in files:
                    fid = f["id"]
                    if fid == SPIKE_FOLDER_ID or fid in matched_items:
                        continue
                    parents = f.get("parents") or []
                    if any(p in target_folder_ids for p in parents):
                        matched_items[fid] = (cname, f)
                        if f.get("mimeType") == "application/vnd.google-apps.folder":
                            target_folder_ids.add(fid)
                        added += 1
            if added == 0:
                break

        # Also check for "Untitled" test file in root created by probe test
        for cname, files in client_files.items():
            for f in files:
                fid = f["id"]
                if f.get("name") == "Untitled" and fid not in matched_items:
                    matched_items[fid] = (cname, f)

        # Verify each via files.get
        verified_targets = []
        for fid, (cname, f) in matched_items.items():
            tok = client_tokens[cname]
            info = get_file_info(tok, fid)
            if not info:
                continue
            if info["id"] == SPIKE_FOLDER_ID:
                print(f"CRITICAL: Refusing to touch SPIKE_FOLDER_ID ({fid})")
                continue
            verified_targets.append((cname, info))

        # Order: files first, directories second
        verified_targets.sort(
            key=lambda x: (1 if x[1].get("mimeType") == "application/vnd.google-apps.folder" else 0)
        )

        print(f"=== Step 2 ({args.action}): Found {len(verified_targets)} items to delete ===")
        for cname, info in verified_targets:
            fid = info["id"]
            name = info.get("name", "")
            is_dir = (info.get("mimeType") == "application/vnd.google-apps.folder")
            parents = info.get("parents", [])
            print(f"[{cname}]\t{fid}\t{name}\t[{'DIR' if is_dir else 'FILE'}]\tparents={parents}")

        if args.action == "exec":
            print("\n--- Executing Step 2 permanent deletions ---")
            deleted_count = 0
            failed_items = []
            for cname, info in verified_targets:
                fid = info["id"]
                name = info.get("name", "")
                tok = client_tokens[cname]
                st, body = delete_file(tok, fid)
                if st in (200, 204):
                    print(f"[{cname}] {fid} http={st} deleted ({name})")
                    deleted_count += 1
                else:
                    # If this client cannot delete, try other clients
                    deleted_other = False
                    for ocname, otok in client_tokens.items():
                        if ocname == cname:
                            continue
                        ost, obody = delete_file(otok, fid)
                        if ost in (200, 204):
                            print(f"[{ocname} fallback] {fid} http={ost} deleted ({name})")
                            deleted_count += 1
                            deleted_other = True
                            break
                    if not deleted_other:
                        r, m = reason_of(body)
                        print(f"[{cname}] {fid} http={st} reason={r} message={m} ({name})")
                        failed_items.append((cname, fid, name, r, m))

            print(f"\nStep 2 execution finished: {deleted_count}/{len(verified_targets)} items deleted.")
            if failed_items:
                print("Failed items:")
                for cname, fid, name, r, m in failed_items:
                    print(f"  [{cname}] {fid} {name}: {r} - {m}")

            # Post verification across all clients
            print("\nPost-verification across clients:")
            for cname, tok in client_tokens.items():
                rem = [
                    f for f in list_all_files(tok, "trashed = false")
                    if f["id"] != SPIKE_FOLDER_ID and f.get("name", "").startswith("aistorage-spike-")
                ]
                print(f"  Client {cname}: remaining matching items = {len(rem)}")

    elif args.cmd == "step3":
        cred = load_cred(args.cred)
        tok = get_token(cred)
        trash_items = list_all_files(tok, "trashed = true")
        print(f"=== Step 3 ({args.action}): Current trash count = {len(trash_items)} ===")
        if args.action == "dry-run":
            print(f"Dry-run: Will call files.emptyTrash to delete all {len(trash_items)} trashed items.")
        elif args.action == "exec":
            print("Calling files.emptyTrash...")
            st, body = api_call("DELETE", "files/trash", tok)
            if st in (200, 204):
                print(f"files.emptyTrash returned http={st} success")
            else:
                r, m = reason_of(body)
                print(f"files.emptyTrash failed http={st} reason={r} message={m}")
            trash_after = list_all_files(tok, "trashed = true")
            print(f"Post-verification: trashed=true remaining count = {len(trash_after)}")


if __name__ == "__main__":
    main()
