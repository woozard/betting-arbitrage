#!/usr/bin/env python3
import re
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/3et_index.js"
text = open(path, encoding="utf-8", errors="ignore").read()

needles = [
    "API_LOGIN",
    "API_SECURITY",
    "API_LOGGED_IN",
    "fS=",
    "gS=",
    "const Ve=",
    "security:{",
    "user:{",
    "data:{",
    "bet:{",
    "placeBet:",
    "API_MARKET_DATA",
    "API_MOBILE_EVENTS",
    "MONEYLINE",
    "HANDICAP",
]

for needle in needles:
    idx = 0
    count = 0
    while True:
        i = text.find(needle, idx)
        if i < 0:
            break
        count += 1
        if count <= 2:
            snippet = text[max(0, i - 40) : i + 200].replace("\n", " ")
            print(f"\n=== {needle} @ {i} ===")
            print(snippet[:240])
        idx = i + len(needle)
    if count:
        print(f"[{needle}] total={count}")

# Extract Le= object-ish block
m = re.search(r"Le=\{", text)
if m:
    start = m.start()
    print("\n=== Le block start ===")
    print(text[start : start + 4000])
