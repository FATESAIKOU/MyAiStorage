"""AiStorage Google Drive rclone 憑證解析與 Token 自動刷新模組。

依據規格：
- docs/impl/group3-modules.md 第 2.2 節
- review-g3a.md H1（設定檔錯誤訊息絕不洩漏秘密）、M2（重試與 invalidate）
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

        # H1: 停用 interpolation，避免秘密中的 % 觸發 InterpolationSyntaxError 洩漏秘密
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read(self._conf_path, encoding="utf-8")
        except Exception:
            # H1: 絕不在錯誤訊息中帶入原生例外內容（因語法錯誤訊息會印出含秘密的該行原文）
            raise ReadError(
                f"rclone 設定檔無法解析（位置：{self._conf_path}，remote={self._remote}）"
            ) from None

        if not parser.has_section(self._remote):
            raise ReadError(f"rclone 設定檔中找不到 remote 區段: [{self._remote}]")

        section = parser[self._remote]
        try:
            self._client_id = section.get("client_id")
            self._client_secret = section.get("client_secret")
            token_raw = section.get("token")
        except Exception:
            raise ReadError(
                f"rclone 設定檔讀取欄位失敗（位置：{self._conf_path}，remote={self._remote}）"
            ) from None

        # L: committer 自帶 OAuth client，缺少 client_id 或 client_secret 直接報錯
        if not self._client_id or not self._client_secret:
            raise ReadError(f"rclone remote [{self._remote}] 缺少 client_id 或 client_secret") from None

        if not token_raw:
            raise ReadError(f"rclone remote [{self._remote}] 缺少 token 欄位") from None

        try:
            token_json = json.loads(token_raw)
        except Exception:
            # H1: 不接原生例外，避免 token 格式損毀時可能洩漏局部內容
            raise ReadError(f"rclone remote [{self._remote}] token JSON 解析失敗") from None

        if not isinstance(token_json, dict):
            raise ReadError(f"rclone remote [{self._remote}] token 必須為 JSON 物件") from None

        self._refresh_token = token_json.get("refresh_token")
        if not self._refresh_token:
            raise ReadError(f"rclone remote [{self._remote}] token 缺少 refresh_token") from None

        # 初始 access_token 與 expiry（若有）
        initial_access = token_json.get("access_token")
        expiry_str = token_json.get("expiry")
        if initial_access and expiry_str:
            try:
                exp_dt = datetime.fromisoformat(str(expiry_str).replace("Z", "+00:00"))
                self._cached_access_token = str(initial_access)
                self._expiry_timestamp = exp_dt.timestamp()
            except Exception:
                self._expiry_timestamp = 0.0

    def access_token(self) -> str:
        """取得有效的 access_token。若距離過期小於 60 秒則自動向 Google 刷新。"""
        now = time.time()
        if self._cached_access_token and (self._expiry_timestamp - now) > 60:
            return self._cached_access_token

        return self._refresh()

    def invalidate(self) -> None:
        """M2: 強制使快取的 access_token 失效（例如收到 401 時重試）。"""
        self._cached_access_token = None
        self._expiry_timestamp = 0.0

    def _refresh(self) -> str:
        """呼叫 Google OAuth 端點刷新 access_token（具備 5xx 退避重試）。"""
        if not self._refresh_token:
            raise ReadError("缺少 refresh_token，無法刷新") from None

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

        # M2: 對 5xx 退避重試（至多 3 次）；400 invalid_grant 則立刻中止
        max_attempts = 3
        last_code: int | None = None
        for attempt in range(max_attempts):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    resp_body = resp.read().decode("utf-8")
                    resp_json = json.loads(resp_body)
                    break
            except urllib.error.HTTPError as e:
                last_code = e.code
                if e.code == 400:
                    # 憑證已撤銷或無效，不重試
                    raise ReadError("OAuth 刷新 token 失敗 (HTTP 400 invalid_grant)") from None
                if 500 <= e.code < 600 and attempt < max_attempts - 1:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise ReadError(f"OAuth 刷新 token 失敗 (HTTP {e.code})") from None
            except Exception:
                if attempt < max_attempts - 1:
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise ReadError("OAuth 刷新 token 網路失敗") from None
        else:
            raise ReadError(f"OAuth 刷新 token 重試耗盡 (HTTP {last_code})") from None

        new_access = resp_json.get("access_token")
        expires_in = resp_json.get("expires_in", 3600)

        if not new_access:
            raise ReadError("OAuth 刷新回應未包含 access_token") from None

        self._cached_access_token = str(new_access)
        self._expiry_timestamp = time.time() + float(expires_in)
        return self._cached_access_token

    def __repr__(self) -> str:
        """絕不印出任何 token 或密鑰。"""
        return f"<RcloneConfToken remote={self._remote}>"
