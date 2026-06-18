#!/usr/bin/env python3
"""
Sports411 placement test using nodriver (no Selenium / chromedriver).
Run on EC2: xvfb-run -a venv/bin/python3 test_nodriver_s411.py
"""
import argparse
import asyncio
import sys


ACCOUNT = "8715"
PASSWORD = "eqr0mjx-MXY*rcn1ana"
MLB_URL = "https://be.sports411.ag/en/sports/baseball/mlb/game-lines/"


async def place_bet(team_substr: str, stake: float):
    import nodriver as uc

    browser = await uc.start(
        browser_args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--window-size=1920,1080",
            "--window-position=0,0",
        ],
        headless=False,
    )
    tab = await browser.get("https://www.sports411.ag/")
    await tab.sleep(3)

    account_el = await tab.select("#account")
    await account_el.send_keys(ACCOUNT)
    password_el = await tab.select("#password")
    await password_el.send_keys(PASSWORD)
    login_btn = await tab.select("input[type='submit'].login")
    await login_btn.click()
    await tab.sleep(5)

    tab = await browser.get(MLB_URL)
    await tab.sleep(6)

    labels = await tab.select_all("label.bet-indicator")
    target = None
    for label in labels:
        attrs = label.attrs or {}
        text = (attrs.get("title") or label.text or "").strip()
        if team_substr.lower() in text.lower() and any(
            c.isdigit() or c in "+-" for c in text
        ):
            target = label
            print(f"Found line: {text}")
            break

    if not target:
        print(f"No moneyline found for team containing: {team_substr}")
        return 1

    await target.click()
    await tab.sleep(2)

    stake_input = await tab.select("input[id^='risk_']")
    await stake_input.clear_input()
    await stake_input.send_keys(f"{stake:.2f}")
    await tab.sleep(1)

    for radio in await tab.select_all("#betslip input[type='radio']"):
        label_id = (radio.attrs or {}).get("id")
        if not label_id:
            continue
        label = await tab.select(f"label[for='{label_id}']")
        label_text = ((label.text if label else "") or "").lower()
        if label and "accept all" in label_text:
            await radio.click()
            break

    place_btn = await tab.select(".place-bet-container button.btn-primary")
    if not place_btn:
        print("Place Bet button not found")
        return 1

    print("Clicking Place Bet...")
    await tab.evaluate(
        """
        window.__wagerResponses = [];
        if (!window.__wagerHookInstalled) {
            window.__wagerHookInstalled = true;
            const capture = (url, body) => {
                const u = String(url).toLowerCase();
                if (u.includes('sendbets') || u.includes('wager') || u.includes('bet')) {
                    window.__wagerResponses.push({url, body: String(body||'').slice(0,4000)});
                }
            };
            const origFetch = window.fetch;
            window.fetch = function(...args) {
                const reqUrl = args[0];
                return origFetch.apply(this, args).then(resp => {
                    resp.clone().text().then(t => capture(reqUrl, t)).catch(() => {});
                    return resp;
                });
            };
            const origSend = XMLHttpRequest.prototype.send;
            XMLHttpRequest.prototype.send = function(...args) {
                this.addEventListener('load', function() {
                    capture(this.responseURL || this.__arbUrl, this.responseText);
                });
                return origSend.apply(this, args);
            };
        }
        """
    )
    await place_btn.click()
    await tab.sleep(8)

    wager_log = await tab.evaluate("window.__wagerResponses || []")
    if wager_log:
        print(f"SendBets responses: {wager_log}")

    html = await tab.get_content()
    if "wager accepted" in html.lower() or "bet accepted" in html.lower():
        print("SUCCESS: acceptance text found on page")
        return 0

    pending = await browser.get("https://be.sports411.ag/en/account/pending")
    await pending.sleep(3)
    pending_html = (await pending.get_content() or "")
    print(f"Pending page length: {len(pending_html)}")
    if team_substr.lower() in pending_html.lower():
        print(f"SUCCESS: {team_substr} found on pending page")
        return 0

    print("FAILED: no confirmation found")
    if wager_log:
        print(f"Last wager body: {wager_log[-1].get('body', '')[:500]}")
    return 1


def main():
    parser = argparse.ArgumentParser(description="Sports411 nodriver placement test")
    parser.add_argument("--team", default="Baltimore Orioles")
    parser.add_argument("--stake", type=float, default=25.0)
    args = parser.parse_args()
    return asyncio.run(place_bet(args.team, args.stake))


if __name__ == "__main__":
    sys.exit(main())
