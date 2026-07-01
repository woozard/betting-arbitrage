#!/usr/bin/env python3
"""Probe 3et.com login page and DOM/API structure (run on EC2 with Chrome)."""
import json
import re
import sys
import time

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

URLS = [
    "https://www.3et.com",
    "https://3et.com",
    "https://www.3et.com/login",
    "https://www.3et.com/v2/",
    "https://www.3et.com/v2/#/schedule",
    "https://www.3et.com/en/sports",
]


def _create_proxy_extension(host, port, user, password):
    import os
    import tempfile

    ext_dir = tempfile.mkdtemp(prefix="proxy_ext_")
    manifest = """{
      "version": "1.0.0",
      "manifest_version": 2,
      "name": "Proxy",
      "permissions": ["proxy","tabs","unlimitedStorage","storage","<all_urls>","webRequest","webRequestBlocking"],
      "background": {"scripts": ["background.js"]}
    }"""
    background = f"""
var config = {{
  mode: "fixed_servers",
  rules: {{ singleProxy: {{ scheme: "http", host: "{host}", port: {port} }}, bypassList: ["localhost"] }}
}};
chrome.proxy.settings.set({{value: config, scope: "regular"}}, function(){{}});
function callbackFn(details) {{
  return {{ authCredentials: {{ username: "{user}", password: "{password}" }} }};
}}
chrome.webRequest.onAuthRequired.addListener(callbackFn, {{urls: ["<all_urls>"]}}, ['blocking']);
"""
    with open(os.path.join(ext_dir, "manifest.json"), "w") as f:
        f.write(manifest)
    with open(os.path.join(ext_dir, "background.js"), "w") as f:
        f.write(background)
    return ext_dir


def main():
    use_proxy = "--no-proxy" not in sys.argv
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    if use_proxy:
        ext = _create_proxy_extension(
            "brd.superproxy.io", 33335,
            "brd-customer-hl_70fad530-zone-arbitrage_bot",
            "truzviha7wip",
        )
        options.add_argument(f"--load-extension={ext}")

    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 30)
    try:
        for url in URLS:
            print(f"\n===== {url} =====")
            try:
                driver.get(url)
                time.sleep(4)
                print("title:", driver.title)
                print("current:", driver.current_url)
                src = driver.page_source or ""
                print("len:", len(src))
                for pat in (
                    r"player-api",
                    r"ticosports",
                    r"schedule",
                    r"login",
                    r"account",
                    r"password",
                    r"Cloudflare",
                    r"angular",
                    r"react",
                    r"GetSportOffering",
                    r"api\.[a-z0-9.-]+",
                ):
                    if re.search(pat, src, re.I):
                        print("  match:", pat)
                inputs = driver.find_elements(By.CSS_SELECTOR, "input")
                print("inputs:", [(i.get_attribute("id"), i.get_attribute("name"), i.get_attribute("type")) for i in inputs[:15]])
                links = driver.find_elements(By.CSS_SELECTOR, "a[href]")
                hrefs = [a.get_attribute("href") for a in links[:20] if a.get_attribute("href")]
                print("sample links:", hrefs[:10])
            except Exception as e:
                print("ERR:", e)

        # try login if fields found
        driver.get("https://www.3et.com")
        time.sleep(5)
        with open("/tmp/3et_probe.html", "w") as f:
            f.write(driver.page_source)
        print("\nSaved /tmp/3et_probe.html")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
