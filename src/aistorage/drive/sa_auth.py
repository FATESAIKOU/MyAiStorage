"""讀取者身分：Service Account JWT bearer token（tasks 4.3／PM 決定 6）。

用既有的 `cryptography` 自簽 RS256（不新增依賴），作風與 RcloneConfToken 一致：
- 金鑰只以路徑讀取；private_key 不進 repr、例外訊息與 log。
- 認證方式固定為 JWT bearer（RS256），scope 為完整的 drive scope（1.5 H5）。
"""

from __future__ import annotations

import base64
import configparser
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from aistorage.errors import ReadError

_DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class ServiceAccountToken:
    """SA 金鑰的 token 提供者（給 HttpDriveClient 使用）。"""

    def __init__(self, key_path: Path | str) -> None:
        self._key_path = Path(key_path)
        try:
            payload = json.loads(self._key_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise ReadError(f"讀取 SA 金鑰檔失敗: {self._key_path}: {e}") from None
        try:
            self._client_email: str = payload["client_email"]
            private_pem: str = payload["private_key"]
        except (KeyError, TypeError) as e:
            raise ReadError(
                f"SA 金鑰檔缺少 client_email／private_key: {self._key_path}") from None
        if not isinstance(self._client_email, str) or not self._client_email:
            raise ReadError(f"SA 金鑰檔的 client_email 無效: {self._key_path}")
        token_uri = payload.get("token_uri", _DEFAULT_TOKEN_URI)
        self._token_uri = token_uri if isinstance(token_uri, str) and token_uri else _DEFAULT_TOKEN_URI
        try:
            self._private_key = serialization.load_pem_private_key(
                private_pem.encode("utf-8"), password=None)
        except Exception as e:
            raise ReadError(f"SA 私鑰解析失敗: {self._key_path}") from None
        self._cached_token: str | None = None
        self._expiry_epoch: float = 0.0

    def __repr__(self) -> str:
        return f"<ServiceAccountToken email={self._client_email}>"

    @classmethod
    def from_rclone_conf(cls, conf_path: Path | str,
                         remote: str = "gdrive") -> ServiceAccountToken:
        """從 rclone conf 的 `service_account_file` 取得 SA 金鑰路徑（只讀路徑）。"""
        parser = configparser.ConfigParser()
        try:
            with open(conf_path, encoding="utf-8") as f:
                parser.read_file(f)
        except OSError as e:
            raise ReadError(f"讀取 rclone conf 失敗: {conf_path}: {e}") from None
        if not parser.has_section(remote):
            raise ReadError(f"rclone conf 缺少 [{remote}]: {conf_path}")
        key_path = parser.get(remote, "service_account_file", fallback="")
        if not key_path:
            raise ReadError(f"rclone conf [{remote}] 沒有 service_account_file: {conf_path}")
        return cls(key_path)

    def build_assertion(self, *, now: float | None = None) -> str:
        """組出簽好的 JWT（供測試驗章；access_token 內部也用它）。"""
        moment = int(now if now is not None else time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        claims = {
            "iss": self._client_email,
            "scope": _DRIVE_SCOPE,
            "aud": self._token_uri,
            "exp": moment + 3600,
            "iat": moment,
        }
        signing_input = (
            _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
            + "."
            + _b64url(json.dumps(claims, separators=(",", ":")).encode("utf-8"))
        ).encode("ascii")
        signature = self._private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return signing_input.decode("ascii") + "." + _b64url(signature)

    def _request_token(self, assertion: str) -> tuple[str, int]:
        body = urllib.parse.urlencode({
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        }).encode("utf-8")
        req = urllib.request.Request(self._token_uri, data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            raise ReadError(f"SA token 交換失敗: {e}") from None
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise ReadError("SA token 交換回應缺少 access_token")
        expires_in = payload.get("expires_in", 3600)
        try:
            lifetime = int(expires_in)
        except (TypeError, ValueError):
            lifetime = 3600
        return token, lifetime

    def access_token(self) -> str:
        """取得 bearer token；過期前 60 秒自動刷新（結果不寫回檔案）。"""
        now = time.time()
        if self._cached_token is not None and now < self._expiry_epoch - 60:
            return self._cached_token
        token, lifetime = self._request_token(self.build_assertion(now=now))
        self._cached_token = token
        self._expiry_epoch = now + max(lifetime, 120)
        return token

    def invalidate(self) -> None:
        """丟棄快取的 token（401 時由呼叫端使用）。"""
        self._cached_token = None
        self._expiry_epoch = 0.0
