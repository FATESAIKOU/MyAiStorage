#!/usr/bin/env python3
"""1.3 用：永久刪除指定 Drive file id（files.delete；不在垃圾桶的會直接永久刪，
在垃圾桶的也會被永久刪除——Drive API 的 delete 是永久刪除，不是丟垃圾桶）。

用法：
  python3 erase_permanent_delete.py --cred <conf> <id> [<id> ...]
  python3 erase_permanent_delete.py --cred <conf> --list-live-prefix <drive:path>
  python3 erase_permanent_delete.py --cred <conf> --list-trash

秘密處理：token 只進記憶體與 HTTP header；輸出只有 id、HTTP 狀態與 error.reason。
"""
import argparse
import configparser
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://www.googleapis.com/drive/v3"


def load_cred(path):
    text = open(path).read()
    if path.endswith(".json"):
        j = json.load(open(path))
        return {
            "client_email": j.get("client_email", ""),
            "private_key": j.get("private_key", ""),
            "token_uri": j.get("token_uri", "https://oauth2.googleapis.com/token"),
            "_sa": True,
        }
    cp = configparser.ConfigParser()
    cp.read_string(text)
    sec = cp["gdrive"]
    tok = json.loads(sec.get("token", "{}"))
    return {
        "client_id": sec.get("client_id", ""),
        "client_secret": sec.get("client_secret", ""),
        "refresh_token": tok.get("refresh_token", ""),
        "_sa": False,
    }


def token_for(cred):
    if cred["_sa"]:
        import time

        import jwt  # 不一定有；SA 走 rclone 時不經這裡
        raise SystemExit("SA JSON 不在本腳本支援範圍（1.3 用 committer conf）")
    data = urllib.parse.urlencode(
        {
            "client_id": cred["client_id"],
            "client_secret": cred["client_secret"],
            "refresh_token": cred["refresh_token"],
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["access_token"]


def call(method, url, token, params=None, body=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Authorization": "Bearer " + token}
    )
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def reason_of(raw):
    try:
        j = json.loads(raw)
        err = j.get("error", {})
        return err.get("errors", [{}])[0].get("reason", "?"), err.get("message", "")[:120]
    except Exception:
        return "?", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cred", required=True)
    ap.add_argument("ids", nargs="*")
    ap.add_argument("--list-live-prefix")
    ap.add_argument("--list-trash", action="store_true")
    a = ap.parse_args()

    cred = load_cred(a.cred)
    token = token_for(cred)

    if a.list_trash:
        # 列出垃圾桶（含路徑），只輸出非秘密欄位
        page = None
        n = 0
        while True:
            params = {
                "q": "trashed = true",
                "fields": "nextPageToken,files(id,name)",
                "pageSize": 100,
            }
            if page:
                params["pageToken"] = page
            st, body = call("GET", f"{API}/files", token, params=params)
            if st != 200:
                print(f"list-trash http={st} reason={reason_of(body)[0]}")
                return 1
            j = json.loads(body)
            for f in j.get("files", []):
                n += 1
                print(f"{f.get('id','?')}  {f.get('name','?')}")
            page = j.get("nextPageToken")
            if not page:
                break
        print(f"# trash total={n}")
        return 0

    if a.list_live_prefix:
        # list-live-prefix 由呼叫端以 rclone 產生 id 清單；這裡只做永久刪除
        raise SystemExit("use rclone to enumerate; pass ids")

    rc = 0
    for fid in a.ids:
        st, body = call("DELETE", f"{API}/files/{fid}", token)
        if st in (204, 200):
            print(f"{fid} http={st} deleted")
        else:
            r, m = reason_of(body)
            print(f"{fid} http={st} reason={r} message={m}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
