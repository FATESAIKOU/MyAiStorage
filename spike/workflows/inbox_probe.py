#!/usr/bin/env python3
"""收件匣預檢（1.8 用）：以資料夾 id 列出 Drive 收件匣的檔案數。

為什麼不用 rclone：這一支必須在**安裝任何工具之前**跑完（空收件匣的 run 不該付安裝成本），
所以只用 Python 標準庫做 refresh token 交換與 files.list。

秘密處理：只從 `RCLONE_CONF`（環境變數）讀 conf 到記憶體；token 只進 HTTP header；
stdout 只有「檔案數、檔名清單、HTTP 狀態碼」，不印 token／client secret。exit code：
  0 = 查詢成功（stdout 第一行是數字，其後是檔名）
  3 = 查詢失敗（stderr 有原因，不含秘密）
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://www.googleapis.com/drive/v3"


def parse_conf(text: str):
    out = {}
    for line in text.splitlines():
        if line.startswith("token = "):
            out["token"] = json.loads(line[len("token = ") :])
        elif " = " in line and not line.startswith("["):
            k, v = line.split(" = ", 1)
            out[k] = v.strip()
    return out


def refresh(conf):
    tok = conf.get("token") or {}
    data = urllib.parse.urlencode(
        {
            "client_id": conf.get("client_id", ""),
            "client_secret": conf.get("client_secret", ""),
            "refresh_token": tok.get("refresh_token", ""),
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    return json.loads(urllib.request.urlopen(req, timeout=30).read())["access_token"]


def main():
    conf_text = os.environ.get("RCLONE_CONF", "")
    folder_id = os.environ.get("INBOX_ID", "")
    if not conf_text or not folder_id:
        print("missing RCLONE_CONF or INBOX_ID", file=sys.stderr)
        return 3

    conf = parse_conf(conf_text)
    try:
        access = refresh(conf)
    except urllib.error.HTTPError as e:
        print(f"token refresh HTTP {e.code}", file=sys.stderr)
        return 3

    q = urllib.parse.quote(f"'{folder_id}' in parents and trashed = false")
    names = []
    page = None
    while True:
        url = f"{API}/files?q={q}&fields=nextPageToken,files(name)&pageSize=100&supportsAllDrives=true"
        if page:
            url += f"&pageToken={urllib.parse.quote(page)}"
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + access})
        try:
            j = json.loads(urllib.request.urlopen(req, timeout=30).read())
        except urllib.error.HTTPError as e:
            print(f"files.list HTTP {e.code}", file=sys.stderr)
            return 3
        names += [f["name"] for f in j.get("files", [])]
        page = j.get("nextPageToken")
        if not page:
            break

    print(len(names))
    for n in names:
        print(n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
