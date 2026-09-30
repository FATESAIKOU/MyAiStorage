#!/usr/bin/env python3
"""依 file id 操作 Drive API 的小型探測腳本（1.4 的負向與可見性測試用）。

為什麼不直接用 rclone：rclone 以名稱逐層解析路徑，drive.file client 看不到目標
資料夾時會在同名路徑下另建一個、回報成功（假綠燈），或根本沒碰到目標檔。這裡一律
以 file id 操作，輸出只有操作、目標 id、HTTP 狀態碼與 error.reason。

秘密處理：token 由 --cred 指到的檔案（rclone conf 或 service account JSON）讀進記憶體，
只放進 HTTP header；不印出、不寫檔、不放進 argv。輸出不得包含 token 或 client secret。

用法：
  spike/probe/drive_probe.py --cred <path> <op> [args...]

Ops:
  about
  get <id>
  get-meta <id>
  list-parents <id>
  list-children <id> [--max N]
  list-root [--max N]
  find-name --parent <id> --name <name>
  create-folder --name <name> [--parent <id>]
  create-file --name <name> [--parent <id>] [--content <text> | --content-file <path>]
  update-name <id> --name <new name>
  update-content <id> [--content <text> | --content-file <path>]
  delete <id>
  permissions-create <id> --role reader|writer --email <addr> [--type user]
  permissions-list <id>
  revisions-list <id>
  download <id>            # 輸出 sha256，不落檔
  battery                  # 正向對照：在自己的空間 create/update/get/delete 各成功一次
"""

import argparse
import configparser
import hashlib
import io
import json
import mimetypes
import os
import sys
import time
import uuid

import requests

API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3"

SHORT_SCOPES = {
    "drive": "https://www.googleapis.com/auth/drive",
    "drive.file": "https://www.googleapis.com/auth/drive.file",
    "drive.readonly": "https://www.googleapis.com/auth/drive.readonly",
    "drive.metadata.readonly": "https://www.googleapis.com/auth/drive.metadata.readonly",
}


def die(msg):
    print(f"ERROR {msg}", file=sys.stderr)
    sys.exit(2)


def load_token(cred_path):
    """回傳 (access_token, scope_note)。只讀檔到記憶體。"""
    if cred_path.endswith(".conf"):
        cp = configparser.ConfigParser()
        cp.read(cred_path)
        if "gdrive" not in cp:
            die("conf 沒有 [gdrive] section")
        sec = cp["gdrive"]
        token_info = json.loads(sec["token"])
        scopes = None
        if sec.get("scope"):
            scopes = [
                SHORT_SCOPES.get(s, s) if not s.startswith("http") else s
                for s in sec["scope"].split()
            ]
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request as GoogleAuthRequest

        creds = Credentials(
            token=token_info.get("access_token"),
            refresh_token=token_info.get("refresh_token"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=sec["client_id"],
            client_secret=sec["client_secret"],
            scopes=scopes,
        )
    else:
        raw = json.load(open(cred_path))
        if raw.get("type") == "service_account":
            from google.oauth2 import service_account
            from google.auth.transport.requests import Request as GoogleAuthRequest

            creds = service_account.Credentials.from_service_account_file(
                cred_path, scopes=["https://www.googleapis.com/auth/drive"]
            )
            scope_note = "service_account:drive(full)"
        elif "installed" in raw:
            die("這是 OAuth client JSON，沒有 refresh token；請用 rclone conf")
        else:
            die("不認得的憑證檔")

    # rclone conf 的 token 不一定帶 expiry，google-auth 會誤判「還沒過期」而不刷新；
    # 一律強制刷新一次，確保用到有效的 access token。
    try:
        creds.refresh(GoogleAuthRequest())
    except Exception as exc:  # noqa: BLE001
        print(f"op=auth\nhttp=0\nreason={type(exc).__name__}")
        sys.exit(3)
    if not cred_path.endswith(".conf"):
        return creds.token, scope_note
    scope = ",".join(creds.scopes) if creds.scopes else "unknown"
    return creds.token, scope


def out(lines):
    print("\n".join(lines))


def reason_of(resp):
    try:
        body = resp.json()
    except ValueError:
        return f"http-{resp.status_code}", ""
    err = body.get("error", {})
    if not isinstance(err, dict):
        return str(err), ""
    reasons = [e.get("reason", "?") for e in err.get("errors", []) or []]
    if resp.status_code == 401 and "invalid_grant" in json.dumps(body):
        reasons.append("invalid_grant")
    msg = str(err.get("message", ""))[:200].replace("\n", " ")
    return ",".join(reasons) if reasons else f"http-{resp.status_code}", msg


def call(method, url, token, **kw):
    headers = kw.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    return requests.request(method, url, headers=headers, timeout=60, **kw)


def multipart_body(metadata, content, content_type="application/octet-stream"):
    boundary = "probe" + uuid.uuid4().hex
    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(b"Content-Type: application/json; charset=UTF-8\r\n\r\n")
    buf.write(json.dumps(metadata).encode())
    buf.write(f"\r\n--{boundary}\r\n".encode())
    buf.write(f"Content-Type: {content_type}\r\n\r\n".encode())
    buf.write(content)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/related; boundary={boundary}"


def read_content(args):
    if getattr(args, "content_file", None):
        return open(args.content_file, "rb").read()
    if getattr(args, "content", None) is not None:
        return args.content.encode()
    return None


def main():
    p = argparse.ArgumentParser(add_help=True)
    p.add_argument("--cred", required=True)
    sub = p.add_subparsers(dest="op", required=True)

    def add(name):
        return sub.add_parser(name)

    sp = add("about")
    sp = add("tokeninfo")
    sp = add("get"); sp.add_argument("id")
    sp = add("get-meta"); sp.add_argument("id")
    sp = add("list-parents"); sp.add_argument("id")
    sp = add("list-children"); sp.add_argument("id"); sp.add_argument("--max", type=int, default=100)
    sp = add("list-root"); sp.add_argument("--max", type=int, default=100)
    sp = add("find-name"); sp.add_argument("--parent", required=True); sp.add_argument("--name", required=True)
    sp = add("create-folder"); sp.add_argument("--name", required=True); sp.add_argument("--parent")
    sp = add("create-file"); sp.add_argument("--name", required=True); sp.add_argument("--parent")
    sp.add_argument("--content"); sp.add_argument("--content-file")
    sp = add("update-name"); sp.add_argument("id"); sp.add_argument("--name", required=True)
    sp = add("update-content"); sp.add_argument("id"); sp.add_argument("--content"); sp.add_argument("--content-file")
    sp = add("delete"); sp.add_argument("id")
    sp = add("permissions-create"); sp.add_argument("id"); sp.add_argument("--role", default="reader")
    sp.add_argument("--email", required=True); sp.add_argument("--type", default="user")
    sp = add("permissions-list"); sp.add_argument("id")
    sp = add("revisions-list"); sp.add_argument("id")
    sp = add("download"); sp.add_argument("id"); sp.add_argument("--out", default=None)
    sp = add("battery")
    sp.add_argument("--parent", default=None, help="在自己的空間建對照物時的父資料夾 id")

    args = p.parse_args()
    token, scope_note = load_token(args.cred)
    lines = []

    if args.op == "about":
        r = call("GET", f"{API}/about", token, params={"fields": "storageQuota,user(permissionId)"})
        if r.ok:
            q = r.json().get("storageQuota", {})
            lines.append("op=about")
            lines.append(f"http={r.status_code}")
            lines.append(f"scope={scope_note}")
            lines.append(f"limit={q.get('limit')} usage={q.get('usage')}")
        else:
            reason, msg = reason_of(r)
            lines += ["op=about", f"http={r.status_code}", f"reason={reason}", f"message={msg}"]

    elif args.op == "tokeninfo":
        # L1：記下 token 實際授予的 scope（非秘密），不印 token 本身。
        r = call("GET", "https://oauth2.googleapis.com/tokeninfo", token)
        lines.append("op=tokeninfo")
        lines.append(f"http={r.status_code}")
        if r.ok:
            body = r.json()
            lines.append(f"scope={body.get('scope')}")
            lines.append(f"expires_in={body.get('expires_in')}")
        else:
            reason, msg = reason_of(r)
            lines.append(f"reason={reason}")
            lines.append(f"message={msg}")

    elif args.op in ("get", "get-meta"):
        fields = "id,name,mimeType,md5Checksum,size,createdTime,modifiedTime,trashed,parents,headRevisionId,owners(permissionId),capabilities(canEdit,canDelete,canAddChildren)"
        r = call("GET", f"{API}/files/{args.id}", token, params={"fields": fields})
        lines.append(f"op={args.op}")
        lines.append(f"target={args.id}")
        lines.append(f"http={r.status_code}")
        if r.ok:
            f = r.json()
            lines.append(f"name={f.get('name')}")
            lines.append(f"mime={f.get('mimeType')}")
            lines.append(f"md5={f.get('md5Checksum')}")
            lines.append(f"size={f.get('size')}")
            lines.append(f"created={f.get('createdTime')}")
            lines.append(f"modified={f.get('modifiedTime')}")
            lines.append(f"head_revision={f.get('headRevisionId')}")
            lines.append(f"trashed={f.get('trashed')}")
            lines.append(f"parents={f.get('parents')}")
            caps = f.get("capabilities", {})
            lines.append(f"canEdit={caps.get('canEdit')} canDelete={caps.get('canDelete')}")
        else:
            reason, msg = reason_of(r)
            lines.append(f"reason={reason}")
            lines.append(f"message={msg}")

    elif args.op in ("list-children", "list-root", "find-name"):
        if args.op == "list-children":
            q = f"'{args.id}' in parents and trashed=false"
            target = args.id
        elif args.op == "list-root":
            q = "'root' in parents and trashed=false"
            target = "root"
        else:
            q = f"name='{args.name}' and '{args.parent}' in parents and trashed=false"
            target = args.parent
        files = []
        page = None
        while True:
            params = {
                "q": q,
                "fields": "nextPageToken,files(id,name,mimeType,md5Checksum,size,modifiedTime,createdTime)",
                "pageSize": 100,
            }
            if page:
                params["pageToken"] = page
            r = call("GET", f"{API}/files", token, params=params)
            if not r.ok:
                reason, msg = reason_of(r)
                lines.append(f"op={args.op}")
                lines.append(f"target={target}")
                lines.append(f"http={r.status_code}")
                lines.append(f"reason={reason}")
                lines.append(f"message={msg}")
                out(lines)
                return
            body = r.json()
            files += body.get("files", [])
            page = body.get("nextPageToken")
            if not page or len(files) >= args.max:
                break
        lines.append(f"op={args.op}")
        lines.append(f"target={target}")
        lines.append(f"http=200")
        lines.append(f"count={len(files)}")
        for f in files:
            lines.append(
                "file="
                + json.dumps(
                    {
                        k: f.get(k)
                        for k in ("id", "name", "mimeType", "md5Checksum", "size")
                        if f.get(k) is not None
                    },
                    sort_keys=True,
                )
            )

    elif args.op == "create-folder":
        body = {"name": args.name, "mimeType": "application/vnd.google-apps.folder"}
        if args.parent:
            body["parents"] = [args.parent]
        r = call("POST", f"{API}/files", token, params={"fields": "id,name"}, json=body)
        lines.append("op=create-folder")
        lines.append(f"name={args.name}")
        lines.append(f"parent={args.parent or 'root'}")
        lines.append(f"http={r.status_code}")
        if r.ok:
            lines.append(f"file_id={r.json().get('id')}")
        else:
            reason, msg = reason_of(r)
            lines.append(f"reason={reason}")
            lines.append(f"message={msg}")

    elif args.op in ("create-file", "update-content"):
        content = read_content(args)
        if content is None:
            die("需要 --content 或 --content-file")
        if args.op == "create-file":
            meta = {"name": args.name}
            if args.parent:
                meta["parents"] = [args.parent]
            body, ctype = multipart_body(meta, content)
            r = call(
                "POST", f"{UPLOAD}/files", token,
                params={"uploadType": "multipart", "fields": "id,name,md5Checksum"},
                data=body, headers={"Content-Type": ctype},
            )
            lines.append("op=create-file")
            lines.append(f"name={args.name}")
            lines.append(f"parent={args.parent or 'root'}")
        else:
            r = call(
                "PATCH", f"{UPLOAD}/files/{args.id}", token,
                params={"uploadType": "media", "fields": "id,name,md5Checksum"},
                data=content, headers={"Content-Type": "application/octet-stream"},
            )
            lines.append("op=update-content")
            lines.append(f"target={args.id}")
        lines.append(f"http={r.status_code}")
        if r.ok:
            lines.append(f"file_id={r.json().get('id')}")
            lines.append(f"md5={r.json().get('md5Checksum')}")
        else:
            reason, msg = reason_of(r)
            lines.append(f"reason={reason}")
            lines.append(f"message={msg}")

    elif args.op == "update-name":
        r = call(
            "PATCH", f"{API}/files/{args.id}", token,
            params={"fields": "id,name,md5Checksum"}, json={"name": args.name},
        )
        lines.append("op=update-name")
        lines.append(f"target={args.id}")
        lines.append(f"new_name={args.name}")
        lines.append(f"http={r.status_code}")
        if r.ok:
            lines.append(f"name={r.json().get('name')}")
        else:
            reason, msg = reason_of(r)
            lines.append(f"reason={reason}")
            lines.append(f"message={msg}")

    elif args.op == "delete":
        r = call("DELETE", f"{API}/files/{args.id}", token)
        lines.append("op=delete")
        lines.append(f"target={args.id}")
        lines.append(f"http={r.status_code}")
        if not r.ok:
            reason, msg = reason_of(r)
            lines.append(f"reason={reason}")
            lines.append(f"message={msg}")

    elif args.op == "permissions-create":
        body = {"type": args.type, "role": args.role, "emailAddress": args.email}
        r = call("POST", f"{API}/files/{args.id}/permissions", token,
                 params={"fields": "id,role,type,emailAddress"}, json=body)
        lines.append("op=permissions-create")
        lines.append(f"target={args.id}")
        lines.append(f"role={args.role} email={args.email}")
        lines.append(f"http={r.status_code}")
        if r.ok:
            lines.append(f"permission_id={r.json().get('id')}")
        else:
            reason, msg = reason_of(r)
            lines.append(f"reason={reason}")
            lines.append(f"message={msg}")

    elif args.op == "permissions-list":
        r = call("GET", f"{API}/files/{args.id}/permissions", token,
                 params={"fields": "permissions(id,role,type,emailAddress)"})
        lines.append("op=permissions-list")
        lines.append(f"target={args.id}")
        lines.append(f"http={r.status_code}")
        if r.ok:
            for perm in r.json().get("permissions", []):
                lines.append("perm=" + json.dumps(perm, sort_keys=True))
        else:
            reason, msg = reason_of(r)
            lines.append(f"reason={reason}")
            lines.append(f"message={msg}")

    elif args.op == "revisions-list":
        r = call("GET", f"{API}/files/{args.id}/revisions", token,
                 params={"fields": "revisions(id,modifiedTime,size,keepForever,originalFilename)"})
        lines.append("op=revisions-list")
        lines.append(f"target={args.id}")
        lines.append(f"http={r.status_code}")
        if r.ok:
            revs = r.json().get("revisions", [])
            lines.append(f"count={len(revs)}")
            for rev in revs:
                lines.append("rev=" + json.dumps(rev, sort_keys=True))
        else:
            reason, msg = reason_of(r)
            lines.append(f"reason={reason}")
            lines.append(f"message={msg}")

    elif args.op == "download":
        r = call("GET", f"{API}/files/{args.id}", token, params={"alt": "media"})
        lines.append("op=download")
        lines.append(f"target={args.id}")
        lines.append(f"http={r.status_code}")
        if r.ok:
            data = r.content
            lines.append(f"sha256={hashlib.sha256(data).hexdigest()}")
            lines.append(f"bytes={len(data)}")
            if args.out:
                with open(args.out, "wb") as fh:
                    fh.write(data)
                lines.append(f"saved={args.out}")
        else:
            reason, msg = reason_of(r)
            lines.append(f"reason={reason}")
            lines.append(f"message={msg}")

    elif args.op == "battery":
        # N6 正向對照：自己的空間 create（資料夾＋檔案）→ update → get → delete 各一次。
        lines.append("op=battery")
        suffix = time.strftime("%H%M%S")
        meta = {"name": f"aistorage-spike-probe-battery-{suffix}",
                "mimeType": "application/vnd.google-apps.folder"}
        if args.parent:
            meta["parents"] = [args.parent]
        lines.append(f"parent={args.parent or 'root'}")
        r = call("POST", f"{API}/files", token,
                 params={"fields": "id,name"}, json=meta)
        lines.append(f"create_folder.http={r.status_code}")
        if not r.ok:
            reason, msg = reason_of(r)
            lines.append(f"create_folder.reason={reason}")
            lines.append(f"create_folder.message={msg}")
            out(lines)
            return
        folder_id = r.json()["id"]
        lines.append(f"create_folder.file_id={folder_id}")
        body, ctype = multipart_body({"name": "battery.txt", "parents": [folder_id]}, b"probe-one")
        r = call("POST", f"{UPLOAD}/files", token,
                 params={"uploadType": "multipart", "fields": "id,name,md5Checksum"},
                 data=body, headers={"Content-Type": ctype})
        lines.append(f"create_file.http={r.status_code}")
        if not r.ok:
            reason, msg = reason_of(r)
            lines.append(f"create_file.reason={reason}")
            lines.append(f"create_file.message={msg}")
            out(lines)
            return
        file_id = r.json()["id"]
        lines.append(f"create_file.file_id={file_id}")
        r = call("PATCH", f"{UPLOAD}/files/{file_id}", token,
                 params={"uploadType": "media", "fields": "id,name,md5Checksum"},
                 data=b"probe-two", headers={"Content-Type": "application/octet-stream"})
        lines.append(f"update_content.http={r.status_code}")
        if not r.ok:
            reason, msg = reason_of(r)
            lines.append(f"update_content.reason={reason}")
        else:
            lines.append(f"update_content.md5={r.json().get('md5Checksum')}")
        r = call("GET", f"{API}/files/{file_id}", token,
                 params={"fields": "id,name,md5Checksum"})
        lines.append(f"get.http={r.status_code}")
        if r.ok:
            lines.append(f"get.md5={r.json().get('md5Checksum')}")
        else:
            reason, msg = reason_of(r)
            lines.append(f"get.reason={reason}")
        r = call("DELETE", f"{API}/files/{file_id}", token)
        lines.append(f"delete.http={r.status_code}")
        if not r.ok:
            reason, msg = reason_of(r)
            lines.append(f"delete.reason={reason}")
        r = call("DELETE", f"{API}/files/{folder_id}", token)
        lines.append(f"delete_folder.http={r.status_code}")
        if not r.ok:
            reason, msg = reason_of(r)
            lines.append(f"delete_folder.reason={reason}")

    out(lines)


if __name__ == "__main__":
    main()
