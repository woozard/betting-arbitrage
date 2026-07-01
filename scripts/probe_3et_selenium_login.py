#!/usr/bin/env python3
import json
import os
import tempfile
import time

from selenium import webdriver


def _proxy_ext():
    d = tempfile.mkdtemp()
    manifest = {
        "manifest_version": 2,
        "name": "P",
        "version": "1.0",
        "permissions": [
            "proxy", "tabs", "unlimitedStorage", "storage",
            "webRequest", "webRequestBlocking",
        ],
        "background": {"scripts": ["background.js"]},
    }
    with open(os.path.join(d, "manifest.json"), "w") as f:
        json.dump(manifest, f)
    bg = """
chrome.proxy.settings.set({value:{mode:"fixed_servers",rules:{singleProxy:{scheme:"http",host:"brd.superproxy.io",port:33335},bypassList:["localhost"]}},scope:"regular"},function(){});
chrome.webRequest.onAuthRequired.addListener(function(details){return{authCredentials:{username:"brd-customer-hl_70fad530-zone-arbitrage_bot",password:"truzviha7wip"}}},{urls:["<all_urls>"]},["blocking"]);
"""
    with open(os.path.join(d, "background.js"), "w") as f:
        f.write(bg)
    return d


def main():
    opts = webdriver.ChromeOptions()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(f"--load-extension={_proxy_ext()}")
    opts.add_argument(f"--user-data-dir={tempfile.mkdtemp()}")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(options=opts)
    try:
        driver.get("https://www.3et.com/v2/")
        for wait in (5, 10, 15, 20, 25, 30):
            time.sleep(5)
            title = driver.title
            src = driver.page_source or ""
            cf = "Cloudflare" in src
            print(f"after {wait}s title={title!r} len={len(src)} cf={cf}")
            if not cf and len(src) > 20000:
                break

        script = """
        return fetch("https://sports.3et.com/accounts/v3/security/session", {
          method:"POST",
          headers:{"Content-Type":"application/json","Accept":"application/json"},
          credentials:"include",
          body: JSON.stringify({username:arguments[0], password:arguments[1]})
        }).then(r=>r.json().then(d=>({status:r.status, data:d})));
        """
        user = os.getenv("THREEET_ACCOUNT", "carlosmc")
        pw = os.getenv("THREEET_PASSWORD", "!Carlos123")
        res = driver.execute_script(script, user, pw)
        print("login", json.dumps(res)[:800])
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
