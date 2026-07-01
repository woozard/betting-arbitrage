#!/usr/bin/env python3
"""Log into BrightData dashboard and list zones (one-time setup helper)."""
import asyncio
import os
import sys

from playwright.async_api import async_playwright

EMAIL = os.environ.get("BRIGHTDATA_EMAIL", "")
PASSWORD = os.environ.get("BRIGHTDATA_DASHBOARD_PASSWORD", "")

if not EMAIL or not PASSWORD:
    print("Set BRIGHTDATA_EMAIL and BRIGHTDATA_DASHBOARD_PASSWORD env vars")
    sys.exit(1)


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        page = await context.new_page()

        await page.goto("https://brightdata.com/cp/start", timeout=120_000)
        await page.wait_for_timeout(5000)
        print("start url:", page.url, "title:", await page.title())

        # BrightData login flow may vary — try common selectors
        for sel in (
            "input[type='email']",
            "input[name='email']",
            "#email",
            "input[placeholder*='mail' i]",
        ):
            if await page.locator(sel).count():
                await page.locator(sel).first.fill(EMAIL)
                print("filled email via", sel)
                break

        for sel in ("input[type='password']", "#password"):
            if await page.locator(sel).count():
                await page.locator(sel).first.fill(PASSWORD)
                print("filled password via", sel)
                break

        for sel in (
            "button[type='submit']",
            "button:has-text('Log in')",
            "button:has-text('Sign in')",
            "input[type='submit']",
        ):
            if await page.locator(sel).count():
                await page.locator(sel).first.click()
                print("clicked", sel)
                break

        await page.wait_for_timeout(15000)
        print("after login url:", page.url, "title:", await page.title())

        # Navigate to proxies/zones
        for path in (
            "https://brightdata.com/cp/zones",
            "https://brightdata.com/cp/proxy",
            "https://brightdata.com/cp/billing",
        ):
            try:
                await page.goto(path, timeout=60_000)
                await page.wait_for_timeout(5000)
                print("visited", path, "title:", await page.title())
            except Exception as e:
                print("skip", path, e)

        text = await page.content()
        out = "logs/debug/brightdata_dashboard.html"
        os.makedirs("logs/debug", exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print("saved", out, "len", len(text))

        # grep zone-like strings from visible text
        body = await page.inner_text("body")
        for line in body.splitlines():
            low = line.lower()
            if any(k in low for k in ("zone", "browser", "scraping", "arbitrage", "proxy")):
                if line.strip():
                    print(" ", line.strip()[:120])

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
