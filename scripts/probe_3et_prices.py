#!/usr/bin/env python3
import json
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()
key = os.getenv("ZENROWS_API_KEY")
BASE = "https://sports.3et.com"


def zr(method, url, token=None, body=None, tries=8):
    last_status = None
    for _ in range(tries):
        params = {
            "apikey": key,
            "url": url,
            "premium_proxy": "true",
            "custom_headers": "true",
            "js_render": "true",
            "wait": "2500",
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://www.3et.com",
            "Referer": "https://www.3et.com/v2/",
        }
        if token:
            headers["session-token"] = token
        resp = requests.request(
            method,
            "https://api.zenrows.com/v1/",
            params=params,
            headers=headers,
            data=json.dumps(body) if body is not None else None,
            timeout=180,
        )
        last_status = resp.status_code
        if resp.status_code == 200:
            return resp.json()
        time.sleep(3)
    raise RuntimeError(f"failed {url} last {last_status}")


def main():
    sess = zr(
        "POST",
        f"{BASE}/accounts/v3/security/session",
        body={"username": "carlosmc", "password": "!Carlos123"},
    )
    token = sess["session"]["sessionToken"]
    data = zr(
        "GET",
        f"{BASE}/data/v3/competitions/4309/events?summarised=true",
        token=token,
    )
    events = data["content"][0]["events"]
    for e in events:
        if e.get("inRunning"):
            continue
        print("event", e["id"], e["name"])
        ids = []
        for mp in e.get("marketPeriods", []):
            for mt in mp.get("marketTypes", []):
                t = mt.get("marketType")
                if t not in ("ONE_X_TWO", "HANDICAP", "MONEY_LINE", "MATCH_ODDS"):
                    continue
                print(" type", t)
                for m in mt.get("markets", []):
                    for rn in m.get("runners", []) or []:
                        print(
                            "  runner",
                            rn.get("id"),
                            rn.get("name"),
                            rn.get("handicap"),
                            rn.get("decimalOdds"),
                            rn.get("odds"),
                        )
                        if rn.get("id"):
                            ids.append(str(rn["id"]))
        if ids:
            q = ",".join(ids[:30])
            prices = zr(
                "GET",
                f"{BASE}/data/v3/runners/prices?runnerIds={q}",
                token=token,
            )
            print("prices sample", json.dumps(prices)[:2500])
        break


if __name__ == "__main__":
    main()
