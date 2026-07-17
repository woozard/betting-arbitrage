from utils.ops_health import format_host_status_message


def test_format_host_status_message_with_stacks(monkeypatch):
    monkeypatch.setattr("utils.ops_health.OPS_CHROME_WARN_COUNT", 160)
    msg = format_host_status_message(
        {
            "cpu_pct": 18.2,
            "mem_used_pct": 14.0,
            "mem_used_gb": 4.3,
            "mem_total_gb": 31.0,
            "disk_used_pct": 42.0,
            "disk_free_gb": 55.0,
            "chrome": 95,
            "chromedriver": 4,
            "chrome_profiles": 4,
            "load_avg": (1.1, 1.0, 0.9),
        },
        stacks=[
            {
                "title": "WNBA",
                "ok": True,
                "config": {
                    "stake": "20",
                    "threshold": "1.00",
                    "fourcasters": "carlosmc",
                    "s411": "8715",
                    "amapola": "PC8396",
                },
                "arb_running": True,
                "scan": (120, 8, 0),
                "books": [
                    {"short": "S411", "ok": True, "odds_age": 12, "extracted": 10},
                    {"short": "Amapola", "ok": True, "odds_age": 20, "extracted": 8},
                    {"short": "4c", "ok": True, "odds_age": 9, "extracted": 12},
                ],
                "chrome_profiles": 2,
            },
            {
                "title": "MLB",
                "ok": False,
                "config": {
                    "stake": "10",
                    "threshold": "1.00",
                    "fourcasters": "carlos2",
                    "s411": "8714",
                    "amapola": "pc8261",
                },
                "arb_running": True,
                "scan": (400, 40, 0),
                "books": [
                    {"short": "S411", "ok": True, "odds_age": 15, "extracted": 44},
                    {"short": "Amapola", "ok": False, "odds_age": 200, "extracted": None},
                    {"short": "4c", "ok": True, "odds_age": 11, "extracted": 24},
                ],
                "chrome_profiles": 2,
            },
        ],
    )
    assert "CPU 18%" in msg
    assert "Mem 14% (4.3/31.0 GiB)" in msg
    assert "Chrome 95 · chromedriver 4 · profiles 4" in msg
    assert "Disk 42% used" in msg
    assert "WNBA [OK]" in msg
    assert "MLB [CHECK]" in msg
    assert "accts carlosmc / 8715 / PC8396" in msg
    assert "accts carlos2 / 8714 / pc8261" in msg
    assert "Amapola ✗" in msg
