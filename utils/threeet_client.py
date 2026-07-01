"""HTTP client for 3et.com mercury/v3 API via Zenrows (Cloudflare bypass on EC2)."""
import json
import os
import time
from typing import Any

import requests

from utils.config import ZENROWS_API_KEY

ZENROWS_URL = "https://api.zenrows.com/v1/"
DEFAULT_API_BASE = os.getenv("THREEET_API_BASE", "https://sports.3et.com")
WWW_ORIGIN = "https://www.3et.com"


class ThreeEtApiError(Exception):
    pass


class ThreeEtClient:
    def __init__(
        self,
        api_base: str = None,
        zenrows_api_key: str = None,
        max_retries: int = 8,
        retry_sleep: float = 3.0,
    ):
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.zenrows_api_key = zenrows_api_key or ZENROWS_API_KEY
        self.max_retries = max_retries
        self.retry_sleep = retry_sleep
        self._session_token: str | None = None

    @property
    def session_token(self) -> str | None:
        return self._session_token

    def clear_session(self):
        self._session_token = None

    def login(self, username: str, password: str) -> dict:
        data = self.request(
            "POST",
            "/accounts/v3/security/session",
            json_body={"username": username, "password": password},
            auth=False,
        )
        token = (data.get("session") or {}).get("sessionToken")
        if not token:
            raise ThreeEtApiError(f"Login missing sessionToken: {json.dumps(data)[:300]}")
        self._session_token = token
        return data

    def ensure_login(self, username: str, password: str) -> dict:
        if self._session_token:
            return {"session": {"sessionToken": self._session_token}}
        return self.login(username, password)

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict | None = None,
        auth: bool = True,
        js_render: bool = True,
    ) -> Any:
        if path.startswith("http"):
            url = path
        else:
            url = f"{self.api_base}{path}"

        if params:
            qs = "&".join(f"{k}={requests.utils.quote(str(v))}" for k, v in params.items())
            url = f"{url}?{qs}"

        last_status = None
        last_body = ""
        for attempt in range(1, self.max_retries + 1):
            zr_params = {
                "apikey": self.zenrows_api_key,
                "url": url,
                "premium_proxy": "true",
                "custom_headers": "true",
            }
            if js_render:
                zr_params["js_render"] = "true"
                zr_params["wait"] = "2000"

            headers = {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": WWW_ORIGIN,
                "Referer": f"{WWW_ORIGIN}/v2/",
            }
            if auth and self._session_token:
                headers["session-token"] = self._session_token
                headers["Cookie"] = f"prod-3et-ui-session-token={self._session_token}"

            resp = requests.request(
                method,
                ZENROWS_URL,
                params=zr_params,
                headers=headers,
                data=json.dumps(json_body) if json_body is not None else None,
                timeout=180,
            )
            last_status = resp.status_code
            last_body = resp.text[:400]
            if resp.status_code == 200:
                try:
                    return resp.json()
                except json.JSONDecodeError:
                    return {"raw": resp.text}
            if resp.status_code == 401:
                self.clear_session()
            time.sleep(self.retry_sleep)

        raise ThreeEtApiError(
            f"{method} {url} failed after {self.max_retries} tries "
            f"(last HTTP {last_status}): {last_body}"
        )

    def get(self, path: str, **kwargs) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> Any:
        return self.request("POST", path, **kwargs)
