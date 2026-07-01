#!/usr/bin/env python3
"""LowVig login via BrightData Scraping Browser (Playwright CDP)."""
import asyncio
import os
import re

from playwright.async_api import async_playwright

from utils.config import LOWVIG_ACCOUNT, LOWVIG_PASSWORD

CUSTOMER = os.getenv("BRIGHTDATA_CUSTOMER", "hl_70fad530")
ZONE = os.getenv("BRIGHTDATA_BROWSER_ZONE", "arbitrage_bot")
ZONE_PASS = os.getenv("BRIGHTDATA_ZONE_PASSWORD", "truzviha7wip")

SBR_CDP = os.getenv(
    "BRIGHTDATA_SBR_CDP",
    f"wss://brd-customer-{CUSTOMER}-zone-{ZONE}:{ZONE_PASS}@brd.superproxy.io:9222",
)


async def main():
    print("Connecting Scraping Browser...")
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(SBR_CDP)
        context = await browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
        )
        page = await context.new_page()

        await page.goto("https://www.lowvig.ag/", wait_until="domcontentloaded", timeout=120_000)
        await page.wait_for_timeout(8000)
        print("home title:", await page.title())

        await page.click("#lvbtn")
        print("clicked login, waiting for auth form...")
        await page.wait_for_timeout(5000)

        for i in range(36):
            await page.wait_for_timeout(5000)
            title = await page.title()
            url = page.url
            html = (await page.content()).lower()
            cf = "just a moment" in html
            print(f"  {(i+1)*5}s cf={cf} title={title[:50]} url={url[:90]}")
            if cf:
                continue

            user = page.locator("#CustomerID, #customerID, input[name='CustomerID']").first
            pwd = page.locator("#Password, #password, input[type='password']").first
            try:
                if await user.is_visible(timeout=1000) and await pwd.is_visible(timeout=1000):
                    await user.fill(LOWVIG_ACCOUNT)
                    await pwd.fill(LOWVIG_PASSWORD)
                    submit = page.locator(
                        "#btnLogin, button[type='submit'], input[type='submit']"
                    ).first
                    await submit.click()
                    print("submitted login")
                    await page.wait_for_timeout(15000)
                    print("post-login", page.url, await page.title())
                    await page.goto(
                        "https://sports.lowvig.ag/sportsbook",
                        wait_until="domcontentloaded",
                        timeout=120_000,
                    )
                    await page.wait_for_timeout(10000)
                    print("sports", page.url, await page.title())
                    result = await page.evaluate("""
                        () => fetch('/sports/Api/Offering.asmx/GetSportOffering', {
                            method: 'POST', credentials: 'include',
                            headers: {'Content-Type':'application/json'},
                            body: JSON.stringify({sportType:'Baseball',sportSubType:'MLB',wagerType:'Straight Bet',hoursAdjustment:0,periodNumber:null,gameNum:null,parentGameNum:null,teaserName:'',requestMode:null})
                        }).then(r=>r.json()).catch(e=>({error:String(e)}))
                    """)
                    lines = (((result or {}).get("d") or {}).get("Data") or {}).get("GameLines") or []
                    print("GameLines", len(lines))
                    if lines:
                        g = lines[0]
                        print("sample", g.get("Team1ID"), "vs", g.get("Team2ID"), g.get("MoneyLine1"))
                    await browser.close()
                    return
            except Exception as e:
                print("  form check:", e)

        path = "logs/debug/lowvig_sbr_fail.html"
        with open(path, "w", encoding="utf-8") as f:
            f.write(await page.content())
        print("FAILED - saved", path)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
