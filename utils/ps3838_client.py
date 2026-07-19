"""HTTP client for PS3838 Lines / Bets / Customer API (Basic auth)."""
from __future__ import annotations

import base64
import os
import time
import uuid
from typing import Any, Optional
from urllib.parse import quote

import requests


DEFAULT_API_BASE = os.getenv("PS3838_API_BASE", "https://api.ps3838.com")
# Baseball sportId on PS3838 / Pinnacle family feeds.
DEFAULT_BASEBALL_SPORT_ID = int(os.getenv("PS3838_BASEBALL_SPORT_ID", "3"))


class Ps3838ApiError(Exception):
    pass


def _proxy_dict_from_env() -> Optional[dict]:
    """Optional HTTP(S) proxy for Cloudflare / geo. Prefer PS3838_PROXY_URL."""
    raw = (os.getenv("PS3838_PROXY_URL") or "").strip()
    if raw:
        return {"http": raw, "https": raw}

    # Fallback: IPRoyal residential (same creds as LowVig), if enabled.
    if os.getenv("PS3838_USE_IPROYAL", "true").lower() not in ("1", "true", "yes"):
        return None
    user = os.getenv("IPROYAL_PROXY_USERNAME") or os.getenv("LOWVIG_PROXY_USERNAME")
    password = os.getenv("IPROYAL_PROXY_PASSWORD") or os.getenv("LOWVIG_PROXY_PASSWORD")
    host = (
        os.getenv("PS3838_PROXY_HOST")
        or os.getenv("IPROYAL_PROXY_HOST")
        or os.getenv("LOWVIG_PROXY_HOST")
        or "geo.iproyal.com"
    )
    port = (
        os.getenv("PS3838_PROXY_PORT")
        or os.getenv("IPROYAL_PROXY_PORT")
        or os.getenv("LOWVIG_PROXY_PORT")
        or "12321"
    )
    if not user or not password:
        return None
    session = os.getenv("PS3838_PROXY_SESSION", "ps3838mlb")
    country = os.getenv("PS3838_PROXY_COUNTRY", "")
    # Plain username works on this account; country modifiers often 407.
    if country:
        user = f"{user}_country-{country}_session-{session}_lifetime-30m"
    u = quote(user, safe="")
    p = quote(password, safe="")
    url = f"http://{u}:{p}@{host}:{port}"
    return {"http": url, "https": url}


class Ps3838Client:
    def __init__(
        self,
        username: str,
        password: str,
        api_base: str | None = None,
        max_retries: int | None = None,
        retry_sleep: float | None = None,
    ):
        self.username = username
        self.password = password
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self.max_retries = (
            max_retries
            if max_retries is not None
            else int(os.getenv("PS3838_MAX_RETRIES", "3"))
        )
        self.retry_sleep = (
            retry_sleep
            if retry_sleep is not None
            else float(os.getenv("PS3838_RETRY_SLEEP_SEC", "0.8"))
        )
        self._session = requests.Session()
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self._session.headers.update(
            {
                "Authorization": f"Basic {token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": os.getenv(
                    "PS3838_USER_AGENT",
                    "betting-arbitrage-ps3838/1.0",
                ),
            }
        )
        proxies = _proxy_dict_from_env()
        if proxies:
            self._session.proxies.update(proxies)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
    ) -> Any:
        url = path if path.startswith("http") else f"{self.api_base}{path}"
        last_status = None
        last_body = ""
        for attempt in range(1, self.max_retries + 1):
            resp = self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                timeout=45,
            )
            last_status = resp.status_code
            last_body = (resp.text or "")[:500]
            ctype = (resp.headers.get("content-type") or "").lower()
            if resp.status_code in (200, 201):
                if "json" not in ctype and not (resp.text or "").lstrip().startswith(("{", "[")):
                    raise Ps3838ApiError(
                        f"{method} {url} non-JSON 200 (likely Cloudflare HTML): "
                        f"{last_body[:160]}"
                    )
                try:
                    return resp.json()
                except ValueError as exc:
                    raise Ps3838ApiError(f"Invalid JSON from {url}: {exc}") from exc
            if resp.status_code == 401:
                raise Ps3838ApiError(f"Unauthorized (401) for {url}: {last_body}")
            if resp.status_code == 403 and "cloudflare" in last_body.lower():
                raise Ps3838ApiError(
                    f"Cloudflare blocked {url} (403). Whitelist this server IP with "
                    f"your PS3838 agent, or set PS3838_PROXY_URL to a gambling-allowed proxy."
                )
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                time.sleep(self.retry_sleep * attempt)
                continue
            if attempt < self.max_retries and resp.status_code >= 500:
                time.sleep(self.retry_sleep)
                continue
            break
        raise Ps3838ApiError(
            f"{method} {url} failed after {self.max_retries} tries "
            f"(last HTTP {last_status}): {last_body}"
        )

    def get(self, path: str, **kwargs) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> Any:
        return self.request("POST", path, **kwargs)

    def get_balance(self) -> dict:
        return self.get("/v3/client/balance")

    def get_sports(self) -> dict:
        return self.get("/v3/sports")

    def get_leagues(self, sport_id: int) -> dict:
        return self.get("/v3/leagues", params={"sportId": int(sport_id)})

    def get_fixtures(
        self,
        sport_id: int,
        *,
        league_ids: list[int] | None = None,
        since: int | None = None,
        is_live: int | None = None,
    ) -> dict:
        params: dict[str, Any] = {"sportId": int(sport_id)}
        if league_ids:
            params["leagueIds"] = ",".join(str(x) for x in league_ids)
        if since is not None:
            params["since"] = int(since)
        if is_live is not None:
            params["isLive"] = int(is_live)
        return self.get("/v3/fixtures", params=params)

    def get_odds(
        self,
        sport_id: int,
        *,
        league_ids: list[int] | None = None,
        since: int | None = None,
        odds_format: str = "American",
        is_live: int | None = None,
    ) -> dict:
        params: dict[str, Any] = {
            "sportId": int(sport_id),
            "oddsFormat": odds_format,
        }
        if league_ids:
            params["leagueIds"] = ",".join(str(x) for x in league_ids)
        if since is not None:
            params["since"] = int(since)
        if is_live is not None:
            params["isLive"] = int(is_live)
        return self.get("/v3/odds", params=params)

    def get_line(
        self,
        *,
        sport_id: int,
        league_id: int,
        event_id: int,
        period_number: int,
        bet_type: str,
        team: str | None = None,
        side: str | None = None,
        handicap: float | None = None,
        odds_format: str = "American",
    ) -> dict:
        params: dict[str, Any] = {
            "sportId": int(sport_id),
            "leagueId": int(league_id),
            "eventId": int(event_id),
            "periodNumber": int(period_number),
            "betType": bet_type,
            "oddsFormat": odds_format,
        }
        if team is not None:
            params["team"] = team
        if side is not None:
            params["side"] = side
        if handicap is not None:
            params["handicap"] = handicap
        return self.get("/v2/line", params=params)

    def place_straight_bet(
        self,
        *,
        sport_id: int,
        league_id: int,
        event_id: int,
        period_number: int,
        line_id: int,
        bet_type: str,
        stake: float,
        win_risk_stake: str = "RISK",
        team: str | None = None,
        side: str | None = None,
        handicap: float | None = None,
        alt_line_id: int | None = None,
        odds_format: str = "American",
        accept_better_line: bool = True,
        unique_request_id: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "oddsFormat": odds_format,
            "uniqueRequestId": unique_request_id or str(uuid.uuid4()),
            "acceptBetterLine": bool(accept_better_line),
            "stake": float(stake),
            "winRiskStake": win_risk_stake,
            "lineId": int(line_id),
            "sportId": int(sport_id),
            "eventId": int(event_id),
            "periodNumber": int(period_number),
            "betType": bet_type,
            "leagueId": int(league_id),
        }
        if team is not None:
            body["team"] = team
        if side is not None:
            body["side"] = side
        if handicap is not None:
            body["handicap"] = handicap
        if alt_line_id is not None:
            body["altLineId"] = int(alt_line_id)
        return self.post("/v2/bets/place", json_body=body)

    def get_bets_by_unique_request_ids(self, ids: list[str]) -> dict:
        return self.get(
            "/v3/bets",
            params={"uniqueRequestIds": ",".join(ids)},
        )
