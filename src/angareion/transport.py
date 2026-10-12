"""HTTP transport shared by the GitHub backend, the registry client and the rebuild script.

Standard library only: the registry rebuild runs on a bare runner with
`PYTHONPATH=src` and no third-party packages. Redirects are never followed, so
an Authorization header cannot travel to another host.
"""

from __future__ import annotations

import json
from typing import Any, Protocol
import urllib.error
import urllib.request

HTTP_TIMEOUT_SECONDS = 30.0


class HttpResponse:
    """HTTP response wrapper."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


class HttpTransport(Protocol):
    """Transport protocol allowing injecting fake HTTP responses for testing."""

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse to follow redirects: Authorization must not travel to another host (L3)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DefaultHttpTransport:
    """Standard library urllib HTTP transport with a fixed socket timeout (M2).

    A half-open connection (NAT, after sleep) must not hang `wait` past its
    `--timeout`; the request times out and the caller sees a BackendError.
    Redirects are not followed (L3): the 3xx response is surfaced and the
    backend turns it into an error.
    """

    def __init__(self, timeout: float = HTTP_TIMEOUT_SECONDS):
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirectHandler())

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        effective_timeout = self.timeout if timeout is None else timeout
        req = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with self._opener.open(req, timeout=effective_timeout) as resp:
                headers_dict = {str(k).lower(): str(v) for k, v in resp.headers.items()}
                return HttpResponse(resp.status, headers_dict, resp.read())
        except urllib.error.HTTPError as e:
            headers_dict = {str(k).lower(): str(v) for k, v in e.headers.items()}
            return HttpResponse(e.code, headers_dict, e.read())
        except TimeoutError as e:
            raise TimeoutError(f"HTTP request timed out after {effective_timeout:.0f}s") from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, TimeoutError):
                raise TimeoutError(f"HTTP request timed out after {effective_timeout:.0f}s") from e
            raise ConnectionError(f"Network error: {e.reason}") from e
