#!/usr/bin/env python3
import json
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()
key = os.getenv("ZENROWS_API_KEY")
BASE = "https://sports.3et.com"
MARLIN_EVENT = 2549886


def zr(method, url, token=None, body=None):
    for i in range(8):
        params = {
            "apikey": key,
            "url": url,
            "premium_proxy": "true",
            "custom_headers": "true",
            "js_render": "true",
            "wait": "2500",
        }
        headers = {"Accept": "application/json", "session-token": token or ""}
        r = requests.request(
            method,
            "https://api.zenrows.com/v1/",
            params=params,
            headers=headers,
            data=json.dumps(body) if body is not None else None,
            timeout=180,
        )
        if r.status_code == 200:
            return r.json()
        time.sleep(3)
    raise RuntimeError(f"failed {url}")


def main():
    token = zr(
        "POST",
        f"{BASE}/accounts/v3/security/session",
        body={"username": "carlosmc", "password": "!Carlos123"},
    )["session"]["sessionToken"]

    data = zr("GET", f"{BASE}/data/v3/competitions/4309/events?summarised=true", token=token)
    marlin = None
    for e in data["content"][0]["events"]:
        if e.get("id") == MARLIN_EVENT or "marlin" in (e.get("name") or "").lower():
            marlin = e
            break
    print("found", marlin is not None, marlin.get("name") if marlin else None)
    if not marlin:
        return

    ids = []
    for mp in marlin.get("marketPeriods", []):
        for mt in mp.get("marketTypes", []):
            print("marketType", mt.get("marketType"))
            for m in mt.get("markets", []):
                for rn in m.get("runners", []) or []:
                    print(" runner", rn.get("id"), rn.get("name"), rn.get("handicap"))
                    if rn.get("id"):
                        ids.append(str(rn["id"]))

    if ids:
        prices = zr("GET", f"{BASE}/data/v3/runners/prices?runnerIds={','.join(ids)}", token=token)
        print("prices", json.dumps(prices, indent=2)[:3000])


if __name__ == "__main__":
    main()
