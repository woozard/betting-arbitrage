"""Production health checks and safe auto-remediation for book scanners + arb engine."""

from __future__ import annotations

import asyncio
import glob
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import func

from database.config import __get_db1_session__
from database.models.ArbitrageOdds import ArbitrageOdds
from utils.chrome_temp import cleanup_stale_temp_dirs
from utils.config import (
    ACTIVE_ARB_BOOKMAKERS,
    OPS_ARB_SCAN_STALE_SECONDS,
    OPS_ODDS_STALE_SECONDS,
    OPS_REMEDIATE_COOLDOWN_SECONDS,
    TELEGRAM,
)

BASE_PATH = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_PATH / "logs"

# Books with active scheduler jobs (lowvig disabled in jobs.yml).
BOOK_SPECS = {
    "sports411": {
        "label": "S411",
        "job_name": "sports411_betting",
        "script": "sports411_betting.py",
        "log": "sports411_betting.log",
        "uses_chrome": True,
    },
    "betamapola": {
        "label": "Betamapola",
        "job_name": "betamapola_betting",
        "script": "betamapola_betting.py",
        "log": "betamapola_betting.log",
        "uses_chrome": True,
    },
    "paradisewager": {
        "label": "Paradise",
        "job_name": "paradisewager_betting",
        "script": "paradisewager_betting.py",
        "log": "paradisewager_betting.log",
        "uses_chrome": True,
    },
    "betwar": {
        "label": "BetWar",
        "job_name": "betwar_betting",
        "script": "betwar_betting.py",
        "log": "betwar_betting.log",
        "uses_chrome": True,
    },
    "3et": {
        "label": "3et",
        "job_name": "threeet_betting",
        "script": "threeet_betting.py",
        "log": "threeet_betting.log",
        "uses_chrome": True,
    },
    "4casters": {
        "label": "4casters",
        "job_name": "fourcasters_betting",
        "script": "fourcasters_betting.py",
        "log": "fourcasters_betting.log",
        "uses_chrome": False,
    },
}

SCAN_LINE = re.compile(
    r"Odds: (\d+) - Matches: (\d+) - Arbs: (\d+)",
    re.I,
)
IMPORT_ERROR = re.compile(r"ImportError|ModuleNotFoundError|cannot import name", re.I)
CHROME_INIT = re.compile(r"Starting Chrome", re.I)
BETTING_READY = re.compile(
    r"unified session|Waiting for Arbitrage|API success|Published \d+",
    re.I,
)


@dataclass
class HealthIssue:
    component: str
    severity: str  # warning | critical
    code: str
    message: str
    auto_fixable: bool = False
    remediate: Optional[Callable[[], str]] = None
    details: dict = field(default_factory=dict)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _process_running(pattern: str) -> bool:
    try:
        r = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return False


def _tail_lines(log_name: str, n: int = 120) -> list[str]:
    path = LOG_DIR / log_name
    if not path.exists():
        return []
    try:
        with open(path, "r", errors="replace") as f:
            return f.readlines()[-n:]
    except Exception:
        return []


def _log_mtime(log_name: str) -> Optional[float]:
    path = LOG_DIR / log_name
    if not path.exists():
        return None
    try:
        return path.stat().st_mtime
    except Exception:
        return None


def _lock_path(job_name: str) -> str:
    return f"/tmp/{job_name}.lock"


def _remediation_cooldown_ok(key: str) -> bool:
    path = f"/tmp/ops_remediate_{key}.ts"
    if not os.path.exists(path):
        return True
    try:
        return time.time() - os.path.getmtime(path) >= OPS_REMEDIATE_COOLDOWN_SECONDS
    except Exception:
        return True


def _mark_remediation(key: str) -> None:
    path = f"/tmp/ops_remediate_{key}.ts"
    try:
        with open(path, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _clear_stale_lock(job_name: str) -> str:
    lock = _lock_path(job_name)
    if os.path.exists(lock):
        os.remove(lock)
        return f"removed stale lock {lock}"
    return f"no lock at {lock}"


def _cleanup_chrome_temps(aggressive: bool = False) -> str:
    max_age = 60 if aggressive else 300
    cleanup_stale_temp_dirs(active_dirs=(), max_age_seconds=max_age, logger=None)
    return f"pruned chrome temp dirs older than {max_age}s"


def _kill_orphan_chrome_profiles() -> str:
    """Kill chromedriver/chrome tied to stale tmp profiles with no owning book process."""
    killed = 0
    for pat in ("chrome_user_data_*", "brightdata_proxy_*"):
        for d in glob.glob(str(BASE_PATH / "tmp" / pat)):
            if not os.path.isdir(d):
                continue
            age = time.time() - os.path.getmtime(d)
            if age < 120:
                continue
            try:
                r = subprocess.run(
                    ["pgrep", "-f", d],
                    capture_output=True,
                    timeout=3,
                )
                if r.returncode != 0:
                    continue
                subprocess.run(
                    ["pkill", "-f", d],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                killed += 1
            except Exception:
                pass
    return f"killed orphan chrome for {killed} stale profile(s)"


def latest_odds_age_by_book() -> dict[str, int]:
    db = __get_db1_session__()
    try:
        rows = (
            db.query(
                ArbitrageOdds.bookmaker,
                func.max(ArbitrageOdds.created_at).label("latest"),
            )
            .group_by(ArbitrageOdds.bookmaker)
            .all()
        )
        now = datetime.utcnow()
        ages = {}
        for bookmaker, latest in rows:
            if latest is None:
                continue
            ages[bookmaker] = max(0, int((now - latest).total_seconds()))
        return ages
    finally:
        db.close()


def check_book_health(bookmaker: str, odds_ages: dict[str, int]) -> list[HealthIssue]:
    if bookmaker not in ACTIVE_ARB_BOOKMAKERS:
        return []
    spec = BOOK_SPECS.get(bookmaker)
    if not spec:
        return []

    issues: list[HealthIssue] = []
    label = spec["label"]
    job_name = spec["job_name"]
    script = spec["script"]
    log_name = spec["log"]
    running = _process_running(script)
    age = odds_ages.get(bookmaker)
    lines = _tail_lines(log_name)

    if age is None:
        issues.append(
            HealthIssue(
                component=bookmaker,
                severity="critical",
                code="no_odds",
                message=f"{label}: no odds rows in DB",
                auto_fixable=spec.get("uses_chrome", False),
            )
        )
    elif age > OPS_ODDS_STALE_SECONDS:
        issues.append(
            HealthIssue(
                component=bookmaker,
                severity="critical",
                code="stale_odds",
                message=f"{label}: odds stale {age}s (limit {OPS_ODDS_STALE_SECONDS}s)",
                auto_fixable=True,
                details={"age_seconds": age},
            )
        )

    if not running and (age is None or age > OPS_ODDS_STALE_SECONDS):
        issues.append(
            HealthIssue(
                component=bookmaker,
                severity="critical",
                code="process_down",
                message=f"{label}: betting process not running",
                auto_fixable=True,
            )
        )

    lock = _lock_path(job_name)
    if os.path.exists(lock) and not running:
        lock_age = int(time.time() - os.path.getmtime(lock))
        if lock_age > 60:
            issues.append(
                HealthIssue(
                    component=bookmaker,
                    severity="warning",
                    code="stale_lock",
                    message=f"{label}: stale flock lock ({lock_age}s) with no process",
                    auto_fixable=True,
                    details={"lock_age": lock_age},
                )
            )

    if lines and spec.get("uses_chrome"):
        recent = lines[-40:]
        starts = sum(1 for ln in recent if f"START {job_name}" in ln)
        ready = any(BETTING_READY.search(ln) for ln in recent)
        chrome_starts = sum(1 for ln in recent if CHROME_INIT.search(ln))
        if starts >= 2 and chrome_starts >= 2 and not ready and not running:
            issues.append(
                HealthIssue(
                    component=bookmaker,
                    severity="critical",
                    code="chrome_init_loop",
                    message=f"{label}: Chrome init loop ({starts} restarts, never reached betting loop)",
                    auto_fixable=True,
                )
            )

    if lines and IMPORT_ERROR.search("".join(lines[-30:])):
        issues.append(
            HealthIssue(
                component=bookmaker,
                severity="critical",
                code="import_error",
                message=f"{label}: import error in log — needs code fix",
                auto_fixable=False,
            )
        )

    return issues


def check_arb_scanner_health() -> list[HealthIssue]:
    issues: list[HealthIssue] = []
    running = _process_running("arbitrage.py")
    lines = _tail_lines("arbitrage.log")
    log_age = _log_mtime("arbitrage.log")
    log_stale = log_age is not None and (time.time() - log_age) > OPS_ARB_SCAN_STALE_SECONDS

    if lines and IMPORT_ERROR.search("".join(lines[-40:])):
        issues.append(
            HealthIssue(
                component="arbitrage",
                severity="critical",
                code="import_error",
                message="Arb scanner: ImportError in log — needs code fix",
                auto_fixable=False,
            )
        )
        return issues

    if not running:
        issues.append(
            HealthIssue(
                component="arbitrage",
                severity="critical",
                code="process_down",
                message="Arb scanner process not running",
                auto_fixable=True,
            )
        )
    elif log_stale and not any(SCAN_LINE.search(ln) for ln in lines[-80:]):
        issues.append(
            HealthIssue(
                component="arbitrage",
                severity="critical",
                code="scan_stale",
                message=f"Arb scanner: no recent scan output ({int(time.time() - log_age)}s since log write)",
                auto_fixable=True,
                details={"log_age_seconds": int(time.time() - log_age)},
            )
        )

    lock = _lock_path("arbitrage")
    if os.path.exists(lock) and not running:
        issues.append(
            HealthIssue(
                component="arbitrage",
                severity="warning",
                code="stale_lock",
                message="Arb scanner: stale flock lock with no process",
                auto_fixable=True,
            )
        )

    return issues


def remediate_issue(issue: HealthIssue) -> Optional[str]:
    if not issue.auto_fixable:
        return None
    key = f"{issue.component}_{issue.code}"
    if not _remediation_cooldown_ok(key):
        return None

    actions: list[str] = []
    spec = BOOK_SPECS.get(issue.component)
    job_name = spec["job_name"] if spec else issue.component

    if issue.code in ("stale_lock", "process_down", "stale_odds", "chrome_init_loop", "scan_stale"):
        actions.append(_clear_stale_lock(job_name if spec else "arbitrage"))

    if issue.code in ("chrome_init_loop", "stale_odds", "process_down") and spec and spec.get("uses_chrome"):
        actions.append(_cleanup_chrome_temps(aggressive=issue.code == "chrome_init_loop"))
        actions.append(_kill_orphan_chrome_profiles())

    if issue.code == "scan_stale" and issue.component == "arbitrage":
        actions.append(_clear_stale_lock("arbitrage"))

    if not actions:
        return None

    _mark_remediation(key)
    return "; ".join(actions)


async def send_ops_alert(message: str) -> None:
    token = TELEGRAM.get("bot_token")
    chat_id = TELEGRAM.get("ops") or TELEGRAM.get("monitoring")
    if not token or not chat_id:
        print(f"[ops] {message}")
        return
    try:
        from telegram import Bot

        bot = Bot(token=token)
        if len(message) > 3900:
            message = message[:3900] + "…"
        await bot.send_message(chat_id=chat_id, text=message)
    except Exception as exc:
        print(f"[ops] telegram failed: {exc}\n{message}")


def run_health_cycle(logger=None) -> dict:
    """One full health check + remediation pass. Returns summary dict."""
    odds_ages = latest_odds_age_by_book()
    issues: list[HealthIssue] = []

    for book in sorted(ACTIVE_ARB_BOOKMAKERS):
        if book in BOOK_SPECS:
            issues.extend(check_book_health(book, odds_ages))

    issues.extend(check_arb_scanner_health())

    remediated: list[str] = []
    alerts: list[str] = []

    for issue in issues:
        msg = f"[{issue.severity}] {issue.message}"
        if logger:
            (logger.error if issue.severity == "critical" else logger.warning)(msg)
        else:
            print(msg)

        if issue.auto_fixable:
            action = remediate_issue(issue)
            if action:
                line = f"REMEDIATED {issue.component}/{issue.code}: {action}"
                remediated.append(line)
                if logger:
                    logger.info(line)
                else:
                    print(line)
        elif issue.severity == "critical":
            alerts.append(msg)

    if issues and (remediated or alerts):
        summary_lines = [
            f"Ops health @ {_utc_now().strftime('%Y-%m-%d %H:%M:%S UTC')}",
            f"Issues: {len(issues)} | Auto-fixed: {len(remediated)} | Needs code: {len(alerts)}",
        ]
        for issue in issues:
            summary_lines.append(f"• {issue.message}")
        if remediated:
            summary_lines.append("")
            summary_lines.append("Auto-remediation:")
            summary_lines.extend(f"  - {r}" for r in remediated)
        if alerts:
            summary_lines.append("")
            summary_lines.append("Manual fix required:")
            summary_lines.extend(f"  - {a}" for a in alerts)
        asyncio.run(send_ops_alert("\n".join(summary_lines)))

    return {
        "issues": [{"component": i.component, "code": i.code, "message": i.message} for i in issues],
        "remediated": remediated,
        "odds_ages": odds_ages,
    }
