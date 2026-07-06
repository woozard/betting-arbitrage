#!/usr/bin/env python3
"""LowVig login via IPRoyal proxy + headed Chrome (run under xvfb-run)."""
import json
import tempfile
import time

from selenium import webdriver
from selenium.webdriver.common.by import By

from utils.config import LOWVIG_ACCOUNT, LOWVIG_PASSWORD, lowvig_proxy_settings


def main():
    proxy = lowvig_proxy_settings()
    if not proxy:
        raise SystemExit("IPROYAL_PROXY_USERNAME/PASSWORD not set")

    ext_dir = tempfile.mkdtemp(prefix="lv_iproyal_")
    host = proxy["host"]
    port = proxy["port"]
    user = proxy["username"]
    password = proxy["password"]
    bg = (
        "chrome.proxy.settings.set({value:{mode:'fixed_servers',"
        f"rules:{{singleProxy:{{scheme:'http',host:'{host}',port:{port}}}}}}},"
        "scope:'regular'},function(){});"
        "chrome.webRequest.onAuthRequired.addListener("
        "function(d){return{authCredentials:{"
        f"username:'{user}',password:'{password}'"
        "}};},{urls:['<all_urls>']},['blocking']);"
    )
    with open(f"{ext_dir}/manifest.json", "w", encoding="utf-8") as f:
        json.dump(
            {
                "version": "1.0.0",
                "manifest_version": 2,
                "name": "IPRoyal",
                "permissions": [
                    "proxy",
                    "tabs",
                    "storage",
                    "webRequest",
                    "webRequestBlocking",
                ],
                "background": {"scripts": ["background.js"]},
            },
            f,
        )
    with open(f"{ext_dir}/background.js", "w", encoding="utf-8") as f:
        f.write(bg)

    opts = webdriver.ChromeOptions()
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(f"--load-extension={ext_dir}")
    opts.add_argument("--disable-extensions-except=" + ext_dir)
    opts.add_argument("--disable-blink-features=AutomationControlled")

    driver = webdriver.Chrome(options=opts)
    try:
        driver.get("https://www.lowvig.ag/")
        for i in range(30):
            time.sleep(5)
            cf = "just a moment" in (driver.page_source or "").lower()
            print(f"home t+{(i + 1) * 5}s cf={cf} title={(driver.title or '')[:50]}")
            if not cf:
                break

        btn = driver.find_elements(By.CSS_SELECTOR, "#lvbtn")
        if btn:
            driver.execute_script("arguments[0].click();", btn[0])
            print("clicked login")

        for i in range(36):
            time.sleep(5)
            cf = "just a moment" in (driver.page_source or "").lower()
            user = [
                e
                for e in driver.find_elements(
                    By.CSS_SELECTOR, "#CustomerID, #customerID, #username"
                )
                if e.is_displayed()
            ]
            pwd = [
                e
                for e in driver.find_elements(By.CSS_SELECTOR, "input[type=password]")
                if e.is_displayed()
            ]
            print(
                f"auth t+{(i + 1) * 5}s cf={cf} user={len(user)} pwd={len(pwd)} "
                f"url={driver.current_url[:95]}"
            )
            if user and pwd:
                user[0].send_keys(LOWVIG_ACCOUNT)
                pwd[0].send_keys(LOWVIG_PASSWORD)
                for sel in ("#btnLogin", "button[type=submit]", "input[type=submit]"):
                    btns = driver.find_elements(By.CSS_SELECTOR, sel)
                    if btns and btns[0].is_displayed():
                        btns[0].click()
                        print("submitted", sel)
                        break
                time.sleep(12)
                print("post-login", driver.current_url[:95], (driver.title or "")[:50])
                driver.get("https://sports.lowvig.ag/sportsbook")
                time.sleep(10)
                print(
                    "sports",
                    driver.current_url[:95],
                    (driver.title or "")[:50],
                    "cf=",
                    "just a moment" in driver.page_source.lower(),
                )
                return
        print("NO LOGIN FORM")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
