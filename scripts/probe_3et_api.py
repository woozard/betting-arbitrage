#!/usr/bin/env python3
"""Probe 3et mercury/v3 API: login, sports, MLB events, bet placement shape."""
import json
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()

BASE_V3 = os.getenv("THREEET_API_BASE", "https://sports.3et.com")
WWW = "https://www.3et.com"


def zenrows_request(method: str, url: str, *, json_body=None, session_token: str | None = None):
    key = os.getenv("ZENROWS_API_KEY")
    if not key:
        raise RuntimeError("ZENROWS_API_KEY required for 3et API access from EC2")

    params = {
        "apikey": key,
        "url": url,
        "premium_proxy": "true",
        "custom_headers": "true",
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": WWW,
        "Referer": f"{WWW}/v2/",
    }
    if session_token:
        headers["session-token"] = session_token
        headers["Cookie"] = f"prod-3et-ui-session-token={session_token}"

    resp = requests.request(
        method,
        "https://api.zenrows.com/v1/",
        params=params,
        headers=headers,
        data=json.dumps(json_body) if json_body is not None else None,
        timeout=180,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"{method} {url} -> zenrows {resp.status_code}: {resp.text[:400]}")
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text[:2000]}


def login(username: str, password: str) -> dict:
    data = zenrows_request(
        "POST",
        f"{BASE_V3}/accounts/v3/security/session",
        json_body={"username": username, "password": password},
    )
    return data


def main():
    user = os.getenv("THREEET_ACCOUNT") or (sys.argv[1] if len(sys.argv) > 1 else "")
    pw = os.getenv("THREEET_PASSWORD") or (sys.argv[2] if len(sys.argv) > 2 else "")
    if not user or not pw:
        print("Set THREEET_ACCOUNT / THREEET_PASSWORD or pass as args")
        sys.exit(1)

    sess = login(user, pw)
    token = (sess.get("session") or {}).get("sessionToken")
    print("login ok user", (sess.get("session") or {}).get("username"), "token", token[:12] if token else None)

    sports = zenrows_request("GET", f"{BASE_V3}/data/v3/sports", session_token=token)
    print("sports keys", list(sports.keys())[:10] if isinstance(sports, dict) else type(sports))
    if isinstance(sports, list):
        baseball = [s for s in sports if "baseball" in json.dumps(s).lower() or "mlb" in json.dumps(s).lower()]
        print("baseball sports", json.dumps(baseball[:3], indent=2)[:1500])
    elif isinstance(sports, dict):
        items = sports.get("sports") or sports.get("data") or list(sports.values())
        print(json.dumps(sports, indent=2)[:2000])

    events = zenrows_request("GET", f"{BASE_V3}/data/v3/events", session_token=token)
    print("events type", type(events))
    if isinstance(events, dict):
        print("events keys", list(events.keys())[:15])
        sample = events.get("events") or events.get("data") or events
        if isinstance(sample, list):
            print("event count", len(sample))
            mlb = [e for e in sample if "marlin" in json.dumps(e).lower() or "mlb" in json.dumps(e).lower() or "baseball" in json.dumps(e).lower()]
            print("mlb-ish", len(mlb))
            if mlb:
                print(json.dumps(mlb[0], indent=2)[:2500])
    elif isinstance(events, list):
        print("event count", len(events))
        mlb = [e for e in events if "marlin" in json.dumps(e).lower() or "baseball" in json.dumps(e).lower()]
        print("mlb-ish", len(mlb))
        if mlb:
            print(json.dumps(mlb[0], indent=2)[:2500])


if __name__ == "__main__":
    main()
