"""AiStorage Google Drive rclone 憑證解析與 Token 自動刷新模組。

依據規格：docs/impl/group3-modules.md 第 2.2 節
- RcloneConfToken: 解析 rclone.conf、快取 access_token、過期前 60 秒自動刷新
- 刷新結果不寫回檔案（runner 上的唯讀複本安全）
- 秘密資訊絕不進 repr 與 log
"""

from __future__ import annotations

import configparser
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from aistorage.errors import ReadError

GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"


class RcloneConfToken:
    """從 rclone.conf 讀取 Google OAuth 憑證並在記憶體內維護/刷新 access_token。"""

    def __init__(self, conf_path: Path | str, remote: str = "gdrive") -> None:
        self._conf_path = Path(conf_path)
        self._remote = remote
        self._client_id: str | None = None
        self._client_secret: str | None = None
        self._refresh_token: str | None = None
        self._cached_access_token: str | None = None
        self._expiry_timestamp: float = 0.0

        self._load_conf()

    def _load_conf(self) -> None:
        if not self._conf_path.is_file():
            raise ReadError(f"找不到 rclone 設定檔: {self._conf_path}")

        parser = configparser.ConfigParser()
        try:
            parser.read(self._conf_path, encoding="utf-8")
        except Exception as e:
            raise ReadError(f"解析 rclone 設定檔失敗: {e}") from e

        if not parser.has_section(self._remote):
            raise ReadError(f"rclone 設定檔中找不到 remote 區段: [{self._remote}]")

        section = parser[self._remote]
        self._client_id = section.get("client_id")
        self._client_secret = section.get("client_secret")

        token_raw = section.get("token")
        if not token_raw:
            raise ReadError(f"rclone remote [{self._remote}] 缺少 token 欄位")

        try:
            token_json = json.loads(token_raw)
        except Exception as e:
            raise ReadError(f"rclone token JSON 解析失敗: {e}") from e

        self._refresh_token = token_json.get("refresh_token")
        if not self._refresh_token:
            raise ReadError(f"rclone remote [{self._remote}] token 缺少 refresh_token")

        # 初始 access_token 與 expiry（若有）
        initial_access = token_json.get("access_token")
        expiry_str = token_json.get("expiry")
        if initial_access and expiry_str:
            try:
                # 解析 RFC 3339 日期時間
                exp_dt = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
                self._cached_access_token = initial_access
                self._expiry_timestamp = exp_dt.timestamp()
            except Exception:
                self._expiry_timestamp = 0.0

    def access_token(self) -> str:
        """取得有效的 access_token。若距離過期小於 60 秒則自動向 Google 刷新。"""
        now = time.time()
        if self._cached_access_token and (self._expiry_timestamp - now) > 60:
            return self._cached_access_token

        # 執行刷新
        return self._refresh()

    def _refresh(self) -> str:
        """呼叫 Google OAuth 端點刷新 access_token。"""
        if not self._refresh_token:
            raise ReadError("缺少 refresh_token，無法刷新")

        post_data = {
            "refresh_token": self._refresh_token,
            "grant_type": "refresh_token",
        }
        if self._client_id:
            post_data["client_id"] = self._client_id
        if self._client_secret:
            post_data["client_secret"] = self._client_secret

        encoded_data = urllib.parse.urlencode(post_data).encode("utf-8")
        req = urllib.request.Request(
            GOOGLE_OAUTH_TOKEN_URL,
            data=encoded_data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp_body = resp.read().decode("utf-8")
                resp_json = json.loads(resp_body)
        except urllib.error.HTTPError as e:
            raise ReadError(f"OAuth 刷新 token 失敗 (HTTP {e.code})") from e
        except Exception as e:
            raise ReadError(f"OAuth 刷新 token 網路失敗: {e}") from e

        new_access = resp_json.get("access_token")
        expires_in = resp_json.get("expires_in", 3600)

        if not new_access:
            raise ReadError("OAuth 刷新回應未包含 access_token")

        self._cached_access_token = new_access
        self._expiry_timestamp = time.time() + float(expires_in)
        return self._cached_access_token

    def __repr__(self) -> str:
        """絕不印出任何 token 或密鑰。"""
        return f"<RcloneConfToken remote={self._remote}>"
