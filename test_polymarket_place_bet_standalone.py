#!/usr/bin/env python3
"""Standalone Polymarket bet test (no DB). Supports HTTPS_PROXY / POLYMARKET_HTTP_PROXY."""
import argparse
import json
import os
import sys

import httpx
import requests

# Patch CLOB httpx client before ClobClient import if proxy configured.
_proxy = os.getenv("POLYMARKET_HTTP_PROXY") or os.getenv("HTTPS_PROXY")
if _proxy:
    import py_clob_client_v2.http_helpers.helpers as clob_http

    clob_http._http_client = httpx.Client(http2=True, proxy=_proxy, timeout=30.0)

from py_clob_client_v2 import MarketOrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client_v2 import ClobClient
from py_clob_client_v2.order_builder.constants import BUY

HOST = os.getenv("POLYMARKET_CLOB_HOST", "https://clob.polymarket.com")
CHAIN_ID = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
GAMMA = os.getenv("POLYMARKET_GAMMA_API_URL", "https://gamma-api.polymarket.com")
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
FUNDER = os.getenv("POLYMARKET_FUNDER_ADDRESS")
SIGNATURE_TYPE = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
SIGNER = os.getenv(
    "POLYMARKET_RELAYER_API_KEY_ADDRESS",
    "0x35C8180822f948F2b7Cf9e78514F5bA8F1A21B51",
)


def resolve_funder() -> str:
    if FUNDER:
        return FUNDER
    resp = requests.get(f"{GAMMA}/public-profile", params={"address": SIGNER}, timeout=20)
    resp.raise_for_status()
    return resp.json().get("proxyWallet") or SIGNER


def find_white_sox_market(game_slug: str = None):
    params = {
        "tag_id": 100381,
        "active": "true",
        "closed": "false",
        "limit": 100,
        "sports_market_types": "moneyline",
    }
    markets = requests.get(f"{GAMMA}/markets", params=params, timeout=30).json()
    for m in markets:
        slug = m.get("slug") or ""
        if game_slug and slug != game_slug:
            continue
        outcomes = json.loads(m.get("outcomes") or "[]")
        prices = json.loads(m.get("outcomePrices") or "[]")
        tokens = json.loads(m.get("clobTokenIds") or "[]")
        if len(outcomes) != 2:
            continue
        for idx, name in enumerate(outcomes):
            if "white sox" in (name or "").lower():
                return {
                    "slug": slug,
                    "question": m.get("question"),
                    "team": name,
                    "token_id": str(tokens[idx]),
                    "price_prob": float(prices[idx]),
                    "game_start": m.get("gameStartTime"),
                }
    raise SystemExit("White Sox moneyline market not found")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stake", type=float, default=1.0)
    parser.add_argument("--game-slug", default="mlb-cws-det-2026-06-20")
    parser.add_argument("--list-only", action="store_true")
    args = parser.parse_args()

    if not PRIVATE_KEY and not args.list_only:
        print("POLYMARKET_PRIVATE_KEY required", file=sys.stderr)
        sys.exit(1)

    pick = find_white_sox_market(args.game_slug)
    print(json.dumps(pick, indent=2))
    if args.list_only:
        return

    funder = resolve_funder()
    print(f"Funder: {funder}")
    if _proxy:
        print(f"Proxy: {_proxy.split('@')[-1] if '@' in _proxy else _proxy}")

    client = ClobClient(
        host=HOST,
        chain_id=CHAIN_ID,
        key=PRIVATE_KEY,
        signature_type=SIGNATURE_TYPE,
        funder=funder,
    )
    client.set_api_creds(client.create_or_derive_api_key())

    live = float(client.get_price(pick["token_id"], "BUY")["price"])
    worst = min(round(live + 0.05, 2), 0.99)
    tick = str(client.get_tick_size(pick["token_id"]))
    neg_risk = bool(client.get_neg_risk(pick["token_id"]))
    print(f"Live buy price: {live} | worst: {worst}")

    resp = client.create_and_post_market_order(
        order_args=MarketOrderArgs(
            token_id=pick["token_id"],
            side=BUY,
            amount=float(args.stake),
            price=worst,
        ),
        options=PartialCreateOrderOptions(tick_size=tick, neg_risk=neg_risk),
        order_type=OrderType.FOK,
    )
    print(json.dumps(resp, indent=2, default=str))


if __name__ == "__main__":
    main()
