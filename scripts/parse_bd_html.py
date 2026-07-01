import re
html = open("/home/ubuntu/betting-arbitrage/logs/debug/brightdata_dashboard.html").read()
for pat in ["arbitrage_bot", "browser", "scraping", "hl_70fad530", "Browser API", "Scraping Browser", "web_unlocker"]:
    print(pat, html.lower().count(pat.lower()))
for m in sorted(set(re.findall(r"[a-z][a-z0-9_]{2,30}", html.lower()))):
    if "arb" in m or "browser" in m or "scrap" in m or "zone" in m:
        print("token", m)
