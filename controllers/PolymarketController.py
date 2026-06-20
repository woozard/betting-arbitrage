import json
from datetime import datetime, timezone, timedelta

import requests

from utils.config import (
    POLYMARKET,
    POLYMARKET_GAMMA_API_URL,
    POLYMARKET_MLB_TAG_ID,
    POLYMARKET_MAX_HOURS_AHEAD,
    POLYMARKET_CLOB_HOST,
    POLYMARKET_CHAIN_ID,
    POLYMARKET_PRIVATE_KEY,
    POLYMARKET_FUNDER_ADDRESS,
    POLYMARKET_SIGNATURE_TYPE,
    POLYMARKET_RELAYER_API_KEY_ADDRESS,
    TELEGRAM,
)
from utils.logger import Logger
from utils.storage import Storage
from utils.helpers import (
    parse_odds,
    send_monitoring_alert,
    is_game_pregame,
    parse_game_datetime,
    probability_to_american,
    teams_same,
)
from utils.timing import time_it
from cache.arbitrage_cache import ArbitrageCache


class PolymarketController:
    """Read-only Polymarket odds via the public Gamma API (no browser required)."""

    PAGE_SIZE = 100
    MAX_PAGES = 10
    MIN_PRICE = 0.02
    MAX_PRICE = 0.98

    def __init__(self, site=None, sport="baseball"):
        self.site = site or POLYMARKET
        self.bookmaker = self.site["bookmaker"]
        self.sport_input = (sport or "baseball").lower()
        self.sport_name = "MLB"
        self.league = "MLB"
        self.logger = Logger.get_logger(self.bookmaker)
        self.storage = Storage(self.logger)
        self.cache = ArbitrageCache()
        self.gamma_api_url = POLYMARKET_GAMMA_API_URL.rstrip("/")
        self.max_hours_ahead = POLYMARKET_MAX_HOURS_AHEAD

    def _is_actionable_game(self, game_datetime: str) -> bool:
        if not is_game_pregame(game_datetime):
            return False
        dt = parse_game_datetime(game_datetime)
        if dt is None:
            return False
        return dt <= datetime.utcnow() + timedelta(hours=self.max_hours_ahead)

    @staticmethod
    def _parse_json_field(value):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                return []
        return []

    @staticmethod
    def _normalize_team_name(name: str) -> str:
        return (name or "").strip().rstrip(".")

    def _parse_game_start_time(self, raw: str) -> str:
        text = (raw or "").strip()
        if not text:
            return None
        if text.endswith("+00"):
            text = text[:-3] + "+00:00"
        if " " in text and "T" not in text:
            text = text.replace(" ", "T", 1)
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _extract_game_id(self, market: dict) -> str:
        slug = (market.get("slug") or "").strip()
        if slug:
            return slug
        events = market.get("events") or []
        if events:
            event_slug = (events[0].get("slug") or "").strip()
            if event_slug:
                return event_slug
            game_id = events[0].get("gameId")
            if game_id is not None:
                return f"pm-{game_id}"
        market_id = market.get("id")
        if market_id is not None:
            return f"pm-market-{market_id}"
        return None

    def _probability_to_moneyline(self, probability) -> int:
        return int(probability_to_american(probability))

    def _parse_moneyline_market(self, market: dict):
        market_type = (market.get("sportsMarketType") or "").strip().lower()
        if market_type and market_type != "moneyline":
            return None

        outcomes = self._parse_json_field(market.get("outcomes"))
        prices = self._parse_json_field(market.get("outcomePrices"))
        if len(outcomes) != 2 or len(prices) != 2:
            return None

        try:
            p1 = float(prices[0])
            p2 = float(prices[1])
        except (TypeError, ValueError):
            return None

        if not (self.MIN_PRICE <= p1 <= self.MAX_PRICE and self.MIN_PRICE <= p2 <= self.MAX_PRICE):
            return None

        game_datetime = self._parse_game_start_time(market.get("gameStartTime"))
        if not game_datetime or not self._is_actionable_game(game_datetime):
            return None

        team_1 = self._normalize_team_name(outcomes[0])
        team_2 = self._normalize_team_name(outcomes[1])
        game_id = self._extract_game_id(market)
        if not team_1 or not team_2 or not game_id:
            return None

        return {
            "bookmaker": self.bookmaker,
            "sport": self.sport_name,
            "league": self.league,
            "game_id": game_id,
            "game_datetime": game_datetime,
            "team_1": team_1,
            "team_2": team_2,
            "moneyline": {
                "team_1": self._probability_to_moneyline(p1),
                "team_2": self._probability_to_moneyline(p2),
            },
        }

    def _fetch_moneyline_markets_page(self, offset: int):
        params = {
            "tag_id": POLYMARKET_MLB_TAG_ID,
            "active": "true",
            "closed": "false",
            "limit": self.PAGE_SIZE,
            "offset": offset,
            "sports_market_types": "moneyline",
        }
        url = f"{self.gamma_api_url}/markets"
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError(f"Unexpected Gamma API response type: {type(data).__name__}")
        return data

    def _fetch_all_moneyline_markets(self):
        markets = []
        for page in range(self.MAX_PAGES):
            offset = page * self.PAGE_SIZE
            batch = self._fetch_moneyline_markets_page(offset)
            if not batch:
                break
            markets.extend(batch)
            if len(batch) < self.PAGE_SIZE:
                break
        return markets

    @time_it
    def fetch_odds(self):
        self.logger = Logger.get_logger(f"{self.bookmaker}-fetch-odds")
        self.storage = Storage(self.logger)
        self.logger.info(
            f"========== Fetching Odds ({self.sport_name}) via Gamma API (START) =========="
        )

        try:
            raw_markets = self._fetch_all_moneyline_markets()
            self.logger.info(
                f"Gamma API returned {len(raw_markets)} active MLB moneyline markets"
            )

            games = []
            seen_game_ids = set()
            for market in raw_markets:
                row = self._parse_moneyline_market(market)
                if not row:
                    continue
                if row["game_id"] in seen_game_ids:
                    continue
                seen_game_ids.add(row["game_id"])
                games.append(row)

            games.sort(key=lambda g: g["game_datetime"])
            self.logger.info(f"Parsed {len(games)} pregame MLB moneyline games")

            odds_data = {
                "sport": self.sport_name,
                "league": self.league,
                "total_matches": len(games),
                "matches": games,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            parsed_odds = parse_odds(odds_data)

            saved = 0
            for odd_row in parsed_odds:
                if odd_row.get("bet_type") == "moneyline":
                    self.cache.add_odds(odd_row)
                try:
                    if self.storage.save_odds(odd_row):
                        saved += 1
                except Exception as db_err:
                    error_str = str(db_err).lower()
                    if "arbitrage_odds" in error_str or "doesn't exist" in error_str or "1146" in error_str:
                        self.logger.warning("[WARN] Table 'arbitrage_odds' issue - continuing")
                    else:
                        self.logger.warning(f"DB save failed: {db_err}")

            self.logger.info(f"Saved {saved} moneyline rows to arbitrage_odds")

        except Exception as e:
            self.logger.error(f"fetch_odds failed: {e}", exc_info=True)
            try:
                import asyncio
                asyncio.run(
                    send_monitoring_alert(
                        self.bookmaker, "fetch-odds", e, TELEGRAM.get("arbitrage_monitoring")
                    )
                )
            except Exception:
                pass
        finally:
            self.logger.info(
                f"========== Fetching Odds ({self.sport_name}) via Gamma API (END) =========="
            )

    def _resolve_funder_address(self) -> str:
        if POLYMARKET_FUNDER_ADDRESS:
            return POLYMARKET_FUNDER_ADDRESS
        signer = (POLYMARKET_RELAYER_API_KEY_ADDRESS or "").strip()
        if not signer:
            raise ValueError("POLYMARKET_FUNDER_ADDRESS or POLYMARKET_RELAYER_API_KEY_ADDRESS required")
        resp = requests.get(
            f"{self.gamma_api_url}/public-profile",
            params={"address": signer},
            timeout=20,
        )
        resp.raise_for_status()
        profile = resp.json() or {}
        proxy = (profile.get("proxyWallet") or "").strip()
        if proxy:
            self.logger.info(f"Resolved Polymarket proxy wallet funder: {proxy}")
            return proxy
        self.logger.info(f"Using signer address as funder: {signer}")
        return signer

    def _build_clob_client(self):
        if not POLYMARKET_PRIVATE_KEY:
            raise ValueError(
                "POLYMARKET_PRIVATE_KEY is not set. Export it from Polymarket "
                "Settings and add it to .env before placing bets."
            )
        from py_clob_client_v2 import ClobClient

        funder = self._resolve_funder_address()
        client = ClobClient(
            host=POLYMARKET_CLOB_HOST,
            chain_id=POLYMARKET_CHAIN_ID,
            key=POLYMARKET_PRIVATE_KEY,
            signature_type=POLYMARKET_SIGNATURE_TYPE,
            funder=funder,
        )
        client.set_api_creds(client.create_or_derive_api_key())
        return client, funder

    def find_moneyline_market_for_team(self, team_name: str, game_slug: str = None):
        team_name = (team_name or "").strip()
        if not team_name:
            raise ValueError("team_name is required")

        raw_markets = self._fetch_all_moneyline_markets()
        candidates = []
        for market in raw_markets:
            row = self._parse_moneyline_market(market)
            if not row:
                continue
            if game_slug and row["game_id"] != game_slug:
                continue

            outcomes = self._parse_json_field(market.get("outcomes"))
            prices = self._parse_json_field(market.get("outcomePrices"))
            tokens = self._parse_json_field(market.get("clobTokenIds"))
            if len(outcomes) != 2 or len(prices) != 2 or len(tokens) != 2:
                continue

            team_no = None
            for idx, outcome in enumerate(outcomes):
                if teams_same(outcome, team_name):
                    team_no = idx + 1
                    break
            if not team_no:
                continue

            candidates.append(
                {
                    **row,
                    "market_id": market.get("id"),
                    "condition_id": market.get("conditionId"),
                    "team_no": team_no,
                    "team_name": self._normalize_team_name(outcomes[team_no - 1]),
                    "token_id": str(tokens[team_no - 1]),
                    "price_prob": float(prices[team_no - 1]),
                    "american_odds": self._probability_to_moneyline(prices[team_no - 1]),
                }
            )

        if not candidates:
            raise ValueError(f"No actionable Polymarket moneyline market found for {team_name!r}")

        candidates.sort(key=lambda c: c["game_datetime"])
        return candidates[0]

    def place_moneyline_bet(
        self,
        team_name: str,
        stake_usd: float = 1.0,
        game_slug: str = None,
        slippage: float = 0.05,
    ) -> dict:
        from py_clob_client_v2 import MarketOrderArgs, OrderType, PartialCreateOrderOptions
        from py_clob_client_v2.order_builder.constants import BUY

        if stake_usd <= 0:
            raise ValueError("stake_usd must be positive")

        self.logger.info(
            f"========== Place Bet (START) | {team_name} | ${stake_usd:.2f} =========="
        )
        pick = self.find_moneyline_market_for_team(team_name, game_slug=game_slug)
        client, funder = self._build_clob_client()

        live_price = float(client.get_price(pick["token_id"], "BUY")["price"])
        worst_price = min(round(live_price + slippage, 2), 0.99)
        tick_size = str(client.get_tick_size(pick["token_id"]))
        neg_risk = bool(client.get_neg_risk(pick["token_id"]))

        self.logger.info(
            f"Market: {pick['team_1']} vs {pick['team_2']} | slug={pick['game_id']} | "
            f"team={pick['team_name']} | live={live_price:.3f} | worst={worst_price:.3f} | "
            f"funder={funder}"
        )

        response = client.create_and_post_market_order(
            order_args=MarketOrderArgs(
                token_id=pick["token_id"],
                side=BUY,
                amount=float(stake_usd),
                price=worst_price,
            ),
            options=PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk),
            order_type=OrderType.FOK,
        )

        self.logger.info(f"Order response: {response}")
        self.logger.info("========== Place Bet (END) ==========")
        return {
            "pick": pick,
            "live_price": live_price,
            "worst_price": worst_price,
            "funder": funder,
            "response": response,
        }


def main():
    controller = PolymarketController(POLYMARKET, sport="baseball")
    controller.fetch_odds()


if __name__ == "__main__":
    main()
