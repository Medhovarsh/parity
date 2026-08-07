"""Shared HTTP plumbing for network providers.

Centralised so that credential handling, timeout policy, and retryability
classification are written once and behave identically across providers. A
provider adapter that gets retryability wrong causes either flaky runs or a
stampede against a rate-limited endpoint.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from parity.errors import ConfigError, ProviderError

#: Status codes worth retrying. 408 and 409 are included because providers
#: behind load balancers use them for transient conditions; 4xx otherwise means
#: the request is wrong and retrying will not fix it.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})

DEFAULT_TIMEOUT_SECONDS = 120.0


class HttpProviderBase:
    """Base class holding a configured ``httpx.Client``.

    Credentials are read from an environment variable named in config and are
    never logged, never written to a store, and never included in an error
    message.
    """

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key_env: str | None = None,
        require_key: bool = True,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._name = name
        self._base_url = base_url.rstrip("/")
        self._api_key_env = api_key_env
        self._api_key = os.environ.get(api_key_env, "").strip() if api_key_env else ""
        if require_key and api_key_env and not self._api_key:
            raise ConfigError(
                f"provider '{name}' needs an API key but environment variable "
                f"{api_key_env} is unset or empty"
            )
        headers = {"content-type": "application/json", "accept": "application/json"}
        headers.update(extra_headers or {})
        headers.update(self._auth_headers())
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=min(30.0, timeout)),
            follow_redirects=False,
        )

    @property
    def name(self) -> str:
        return self._name

    def _auth_headers(self) -> dict[str, str]:
        """Subclasses override to place the credential in the right header."""
        if self._api_key:
            return {"authorization": f"Bearer {self._api_key}"}
        return {}

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(path, json=payload)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                f"request timed out: {exc}", provider=self._name, retryable=True
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"could not reach {self._base_url}: {exc}", provider=self._name, retryable=True
            ) from exc

        if response.status_code >= 400:
            raise ProviderError(
                self._error_detail(response),
                provider=self._name,
                retryable=response.status_code in RETRYABLE_STATUS,
                status_code=response.status_code,
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise ProviderError(
                "response body was not valid JSON", provider=self._name, retryable=True
            ) from exc
        if not isinstance(body, dict):
            raise ProviderError(
                f"expected a JSON object, got {type(body).__name__}",
                provider=self._name,
                retryable=False,
            )
        return body

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        """Extract a useful message without echoing an entire error page."""
        try:
            body = response.json()
        except ValueError:
            return response.text.strip()[:300] or response.reason_phrase
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                return str(error["message"])[:300]
            if isinstance(error, str):
                return error[:300]
            if isinstance(body.get("message"), str):
                return str(body["message"])[:300]
        return str(body)[:300]

    def close(self) -> None:
        self._client.close()
