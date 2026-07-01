"""HTTP client for 4casters.io REST API (peer-to-peer exchange)."""
from __future__ import annotations

import json
import os
import time
from typing import Any

import requests

DEFAULT_API_BASE = os.getenv("FOURCASTERS_API_BASE", "https://api.4casters.io")


class FourCastersApiError(Exception):
    pass


class FourCastersClient:
    def __init__(
        self,
        api_base: str | None = None,
        max_retries: int = 4,
        retry_sleep: float = 2.0,
    ):
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.max_retries = max_retries
        self.retry_sleep = retry_sleep
        self._token: str | None = None

    @property
    def token(self) -> str | None:
        return self._token

    def clear_session(self):
        self._token = None

    def login(self, username: str, password: str) -> dict:
        data = self.request(
            "POST",
            "/user/login",
            json_body={"username": username, "password": password},
            auth=False,
        )
        user = (data.get("user") or {}) if isinstance(data, dict) else {}
        token = user.get("auth")
        if not token:
            raise FourCastersApiError(f"Login missing auth token: {json.dumps(data)[:300]}")
        self._token = token
        return data

    def ensure_login(self, username: str, password: str) -> dict:
        if self._token:
            return {"user": {"auth": self._token}}
        return self.login(username, password)

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: dict | None = None,
        auth: bool = True,
    ) -> Any:
        if path.startswith("http"):
            url = path
        else:
            url = f"{self.api_base}{path}"

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if auth and self._token:
            headers["Authorization"] = self._token

        last_status = None
        last_body = ""
        for attempt in range(1, self.max_retries + 1):
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=60,
            )
            last_status = resp.status_code
            last_body = resp.text[:500]
            if resp.status_code in (200, 201):
                try:
                    payload = resp.json()
                except json.JSONDecodeError:
                    return {"raw": resp.text}
                if isinstance(payload, dict) and "data" in payload:
                    return payload["data"]
                return payload
            if resp.status_code == 401:
                self.clear_session()
            time.sleep(self.retry_sleep)

        raise FourCastersApiError(
            f"{method} {url} failed after {self.max_retries} tries "
            f"(last HTTP {last_status}): {last_body}"
        )

    def get(self, path: str, **kwargs) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> Any:
        return self.request("POST", path, **kwargs)

    def get_leagues(self) -> list[str]:
        data = self.get("/exchange/getLeagues")
        if isinstance(data, dict):
            return list(data.get("availableLeagues") or [])
        return []

    def get_games(self, league: str) -> list[dict]:
        data = self.get("/exchange/v2/getGames", params={"league": league})
        if isinstance(data, dict):
            return list(data.get("games") or [])
        return []

    def get_orderbook(self, *, league: str | None = None, game_id: str | None = None) -> list[dict]:
        params = {}
        if game_id:
            params["gameID"] = game_id
        elif league:
            params["league"] = league
        else:
            raise FourCastersApiError("get_orderbook requires league or game_id")
        data = self.get("/exchange/v2/getOrderbook", params=params)
        if isinstance(data, dict):
            return list(data.get("games") or [])
        return []

    def place_orders(self, orders: list[dict]) -> list[dict]:
        data = self.post("/session/v3/place", json_body={"orders": orders})
        if isinstance(data, dict):
            return list(data.get("createdSessions") or [])
        return []
