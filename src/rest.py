"""Minimal REST client for ARM endpoints the SDKs do not expose cleanly, and Microsoft Graph.

Used for: subscription metadata, Microsoft.Authorization/permissions, management groups,
Azure Resource Graph and Microsoft Graph (users, invitations). Microsoft's Graph SDK for
Python is async-only and very large, so Graph is called directly over HTTPS.

Retries 429 and transient 5xx responses, honouring Retry-After. Never logs headers or tokens.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Iterator

import requests

from .errors import RestError
from .logging_utils import get_logger

ARM_ENDPOINT = "https://management.azure.com"
GRAPH_ENDPOINT = "https://graph.microsoft.com/v1.0"
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class RestClient:
    def __init__(
        self,
        credential: Any,
        base_url: str,
        token_scope: str,
        *,
        session: requests.Session | None = None,
        timeout: float = 60,
        max_retries: int = 4,
        sleep=time.sleep,
    ):
        self.credential = credential
        self.base_url = base_url.rstrip("/")
        self.token_scope = token_scope
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max_retries
        self._sleep = sleep

    def _url(self, path: str) -> str:
        return path if path.startswith("https://") else f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        url = self._url(path)
        attempt = 0
        while True:
            token = self.credential.get_token(self.token_scope).token
            request_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            if headers:
                request_headers.update(headers)
            response = self.session.request(
                method, url, params=params, json=json, headers=request_headers, timeout=self.timeout
            )
            if response.status_code in _RETRY_STATUSES and attempt < self.max_retries:
                attempt += 1
                delay = _retry_after(response, attempt)
                get_logger().debug(
                    "Retrying %s %s after HTTP %s (attempt %s, %.0fs)",
                    method, path.split("?")[0], response.status_code, attempt, delay,
                )
                self._sleep(delay)
                continue
            if response.status_code >= 400:
                raise _to_error(response)
            if response.status_code == 204 or not response.content:
                return None
            try:
                return response.json()
            except ValueError:
                return None

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def paged(self, path: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> Iterator[dict]:
        """Iterate over 'value' items following nextLink (ARM) or @odata.nextLink (Graph)."""
        data = self.get(path, params=params, headers=headers)
        while data:
            yield from data.get("value", [])
            next_link = data.get("nextLink") or data.get("@odata.nextLink")
            if not next_link:
                break
            data = self.get(next_link, headers=headers)


def _retry_after(response: requests.Response, attempt: int) -> float:
    for header in (
        "Retry-After",
        "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after",
        "x-ms-ratelimit-microsoft.costmanagement-entity-retry-after",
    ):
        value = response.headers.get(header)
        if value:
            try:
                return min(float(value), 60.0)
            except ValueError:
                pass
    return min(2 ** attempt, 30)


def _to_error(response: requests.Response) -> RestError:
    code = None
    message = response.reason or f"HTTP {response.status_code}"
    try:
        body = response.json()
        error = body.get("error", body) if isinstance(body, dict) else {}
        code = error.get("code") or None
        message = error.get("message") or message
    except ValueError:
        pass
    return RestError(
        response.status_code,
        code,
        message,
        request_id=response.headers.get("x-ms-request-id") or response.headers.get("request-id"),
    )


def as_json(obj: Any) -> Any:
    """Normalise an SDK model (old msrest-style or new mapping-style) into REST-shaped JSON."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, Mapping):
        return {str(k): as_json(val) for k, val in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [as_json(item) for item in obj]
    serialize = getattr(obj, "serialize", None)
    if callable(serialize):
        try:
            return as_json(serialize(keep_readonly=True))
        except TypeError:
            return as_json(serialize())
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "value"):  # enums
        return obj.value
    return str(obj)
