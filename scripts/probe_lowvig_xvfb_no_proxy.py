#!/usr/bin/env python3
import tempfile, time
from selenium import webdriver
from selenium.webdriver.common.by import By

options = webdriver.ChromeOptions()
options.add_argument("--no-sandbox")
options.add_argument("--disable-dev-shm-usage")
options.add_argument("--window-size=1920,1080")
options.add_argument("--disable-blink-features=AutomationControlled")
ud = tempfile.mkdtemp(prefix="lv_xvfb_np_")
options.add_argument(f"--user-data-dir={ud}")
driver = webdriver.Chrome(options=options)
try:
    driver.get("https://www.lowvig.ag/")
    time.sleep(8)
    print("home", driver.title)
    driver.find_element(By.CSS_SELECTOR, "#lvbtn").click()
    for i in range(30):
        time.sleep(5)
        src = driver.page_source.lower()
        cf = "just a moment" in src
        print(f"{i*5+5}s cf={cf} url={driver.current_url[:80]} title={(driver.title or '')[:40]}")
        if not cf:
            vis = [e for e in driver.find_elements(By.CSS_SELECTOR, "input") if e.is_displayed()]
            print(" inputs", [(v.get_attribute("id"), v.get_attribute("type")) for v in vis[:8]])
            if vis:
                break
finally:
    driver.quit()
