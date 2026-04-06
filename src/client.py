"""HTTP client for the OpenWISP REST API.

Handles authentication (static token or username/password), token caching,
monitoring snapshots, custom device commands, and Wi-Fi session listing.
All network calls use the Python standard-library urllib (no extra dependency).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from ssl import _create_unverified_context, create_default_context
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import APConfig, AppConfig


class OpenWispClient:
    """Thin wrapper around the OpenWISP REST API.

    Token resolution order:
      1. ``config.static_token`` (set via OPENWISP_TOKEN env var)
      2. On-disk token cache (avoids triggering the 429 rate-limit on restart)
      3. Fresh login call to the authentication endpoint
    """

    # Path to the on-disk token cache file (overridable via the env var).
    TOKEN_CACHE_PATH = Path(os.environ.get("OPENWISP_TOKEN_CACHE", ".openwisp_token_cache"))

    def __init__(self, config: AppConfig):
        self.config = config
        # Priority: static token from env > on-disk cache > API call
        self._token: Optional[str] = config.static_token or self._load_cached_token()
        self._ssl_context = create_default_context() if config.verify_ssl else _create_unverified_context()

    def _load_cached_token(self) -> Optional[str]:
        """Return the token stored on disk, or None if the cache file does not exist."""
        try:
            return self.TOKEN_CACHE_PATH.read_text().strip() or None
        except FileNotFoundError:
            return None

    def _save_cached_token(self, token: str) -> None:
        """Persist the token to disk so subsequent runs avoid re-authenticating."""
        self.TOKEN_CACHE_PATH.write_text(token)

    def _invalidate_cached_token(self) -> None:
        """Delete the on-disk cache and clear the in-memory token (e.g. after a 401)."""
        self._token = None
        self.TOKEN_CACHE_PATH.unlink(missing_ok=True)

    @property
    def token(self) -> str:
        """Return a valid Bearer token, obtaining one if necessary."""
        if not self._token:
            self._token = self.get_token()
            self._save_cached_token(self._token)
        return self._token

    def get_token(self) -> str:
        """Authenticate against the OpenWISP auth endpoint and return a Bearer token."""
        payload = urlencode(
            {
                "username": self.config.username,
                "password": self.config.password,
            }
        ).encode()
        response = self._request(
            "POST",
            self.config.auth_url,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            include_auth=False,
        )
        if isinstance(response, str):
            return response
        for key in ("token", "key", "access_token"):
            value = response.get(key)
            if value:
                return value
        raise RuntimeError(f"Token not found in authentication response: {response}")

    def get_monitoring_snapshot(self, ap: APConfig) -> dict[str, Any]:
        """Fetch the current monitoring snapshot for the given AP."""
        url = (
            f"{self.config.base_url}/api/v1/monitoring/device/{ap.device_id}/"
            f"?key={ap.device_key}&status=true"
        )
        return self._request("GET", url)

    def execute_custom_command(self, device_id: str, command: str) -> dict[str, Any]:
        """Send a custom shell command to a device through the OpenWISP controller API."""
        url = f"{self.config.base_url}/api/v1/controller/device/{device_id}/command/"
        payload = {
            "type": "custom",
            "input": {
                "command": command,
            },
        }
        return self._request("POST", url, json_data=payload)

    def list_wifi_sessions(self, **filters: str) -> list[dict[str, Any]]:
        """Return all Wi-Fi sessions matching the given filters, following pagination."""
        query = urlencode({key: value for key, value in filters.items() if value})
        url = f"{self.config.base_url}/api/v1/monitoring/wifi-session/"
        if query:
            url = f"{url}?{query}"
        results: list[dict[str, Any]] = []
        next_url: Optional[str] = url
        while next_url:
            page = self._request("GET", next_url)
            results.extend(page.get("results", []))
            next_url = page.get("next")
        return results

    def _request(
        self,
        method: str,
        url: str,
        data: bytes | None = None,
        json_data: dict[str, Any] | None = None,
        headers: Optional[dict[str, str]] = None,
        include_auth: bool = True,
    ) -> Any:
        """Perform an HTTP request and return the parsed JSON body (or raw string).

        Automatically attaches a Bearer token unless *include_auth* is False.
        On 401, the token cache is invalidated so the next call triggers a re-login.
        Raises RuntimeError for any non-2xx HTTP response.
        """
        request_headers = {"Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        if include_auth:
            request_headers["Authorization"] = f"Bearer {self.token}"
        body = data
        if json_data is not None:
            body = json.dumps(json_data).encode()
            request_headers["Content-Type"] = "application/json"
        request = Request(url, data=body, method=method, headers=request_headers)
        try:
            with urlopen(request, context=self._ssl_context) as response:
                raw = response.read().decode()
        except HTTPError as exc:
            error_body = exc.read().decode()
            if exc.code == 401 and include_auth:
                # Token expired or invalid: clear the cache to force a re-login on the next call.
                self._invalidate_cached_token()
            raise RuntimeError(f"HTTP {exc.code} for {url}: {error_body}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
