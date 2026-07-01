#!/usr/bin/env python3
"""Fetch 3et v2 page via Zenrows and print API/script hints."""
import os
import re
import sys

import requests
from dotenv import load_dotenv

load_dotenv()


def main():
    key = os.getenv("ZENROWS_API_KEY")
    if not key:
        print("ZENROWS_API_KEY missing")
        sys.exit(1)

    r = requests.get(
        "https://api.zenrows.com/v1/",
        params={
            "apikey": key,
            "url": "https://www.3et.com/v2/",
            "js_render": "true",
            "wait": "12000",
            "premium_proxy": "true",
        },
        timeout=180,
    )
    html = r.text
    out = "/tmp/3et_zen.html"
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("status", r.status_code, "len", len(html), "saved", out)

    srcs = re.findall(r'<script[^>]+src="([^"]+)"', html)
    print("scripts:", srcs[:20])

    for pat in (
        r"https?://[^\"'\s>]+",
        r"/api/[a-zA-Z0-9_/\-]+",
        r"player-api",
        r"customerLogin",
        r"schedules",
        r"main\.[a-f0-9]+\.js",
    ):
        found = sorted(set(re.findall(pat, html, re.I)))
        if found:
            print(f"--- {pat} ---")
            for item in found[:15]:
                print(" ", item)

    title = re.search(r"<title>([^<]+)", html)
    print("title:", title.group(1).strip() if title else "?")


if __name__ == "__main__":
    main()
