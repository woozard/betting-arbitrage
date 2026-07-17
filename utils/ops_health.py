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
    OPS_CHROME_WARN_COUNT,
    OPS_HOST_STATUS_ENABLED,
    OPS_HOST_STATUS_INTERVAL_SECONDS,
    OPS_ODDS_STALE_SECONDS,
    OPS_REMEDIATE_COOLDOWN_SECONDS,
    TELEGRAM,
    telegram_health_chat_id,
)

BASE_PATH = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_PATH / "logs"

# Books with active scheduler jobs (dual-stack: wnba + mlb).
# process_patterns match run_stack_job.sh cmdline so stacks stay distinct.
BOOK_SPECS = {
    "sports411_wnba": {
        "label": "S411",
        "short": "S411",
        "stack": "wnba",
        "league": "WNBA",
        "odds_book": "sports411",
        "job_name": "sports411_betting_wnba",
        "script": "sports411_betting.py",
        "process_pattern": "run_stack_job.sh wnba sports411_betting.py",
        "log": "sports411_betting_wnba.log",
        "uses_chrome": True,
    },
    "sports411_mlb": {
        "label": "S411",
        "short": "S411",
        "stack": "mlb",
        "league": "MLB",
        "odds_book": "sports411",
        "job_name": "sports411_betting_mlb",
        "script": "sports411_betting.py",
        "process_pattern": "run_stack_job.sh mlb sports411_betting.py",
        "log": "sports411_betting_mlb.log",
        "uses_chrome": True,
    },
    "betamapola_wnba": {
        "label": "Amapola",
        "short": "Amapola",
        "stack": "wnba",
        "league": "WNBA",
        "odds_book": "betamapola",
        "job_name": "betamapola_betting_wnba",
        "script": "betamapola_betting.py",
        "process_pattern": "run_stack_job.sh wnba betamapola_betting.py",
        "log": "betamapola_betting_wnba.log",
        "uses_chrome": True,
    },
    "betamapola_mlb": {
        "label": "Amapola",
        "short": "Amapola",
        "stack": "mlb",
        "league": "MLB",
        "odds_book": "betamapola",
        "job_name": "betamapola_betting_mlb",
        "script": "betamapola_betting.py",
        "process_pattern": "run_stack_job.sh mlb betamapola_betting.py",
        "log": "betamapola_betting_mlb.log",
        "uses_chrome": True,
    },
    "4casters_wnba": {
        "label": "4c",
        "short": "4c",
        "stack": "wnba",
        "league": "WNBA",
        "odds_book": "4casters",
        "job_name": "fourcasters_betting_wnba",
        "script": "fourcasters_betting.py",
        "process_pattern": "run_stack_job.sh wnba fourcasters_betting.py",
        "log": "fourcasters_betting_wnba.log",
        "uses_chrome": False,
    },
    "4casters_mlb": {
        "label": "4c",
        "short": "4c",
        "stack": "mlb",
        "league": "MLB",
        "odds_book": "4casters",
        "job_name": "fourcasters_betting_mlb",
        "script": "fourcasters_betting.py",
        "process_pattern": "run_stack_job.sh mlb fourcasters_betting.py",
        "log": "fourcasters_betting_mlb.log",
        "uses_chrome": False,
    },
}

STACKS = (
    {
        "name": "wnba",
        "title": "WNBA",
        "league": "WNBA",
        "env_file": ".env.wnba",
        "arb_pattern": "run_stack_job.sh wnba arbitrage.py",
        "arb_log": "arbitrage_wnba.log",
        "arb_job": "arbitrage_wnba",
        "books": ("sports411_wnba", "betamapola_wnba", "4casters_wnba"),
    },
    {
        "name": "mlb",
        "title": "MLB",
        "league": "MLB",
        "env_file": ".env.mlb",
        "arb_pattern": "run_stack_job.sh mlb arbitrage.py",
        "arb_log": "arbitrage_mlb.log",
        "arb_job": "arbitrage_mlb",
        "books": ("sports411_mlb", "betamapola_mlb", "4casters_mlb"),
    },
)

SCAN_LINE = re.compile(
    r"Odds: (\d+) - Matches: (\d+) - Arbs: (\d+)",
    re.I,
)
EXTRACTED_LINE = re.compile(
    r"Extracted (\d+)\s+\w+\s+matches|Parsed (\d+)\s+pregame",
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


def _odds_age_key(league: str, odds_book: str) -> str:
    return f"{(league or '').upper()}::{(odds_book or '').lower()}"


def latest_odds_age_by_book() -> dict[str, int]:
    """Legacy: newest odds age per bookmaker (any league)."""
    by_league = latest_odds_age_by_league_book()
    ages: dict[str, int] = {}
    for key, age in by_league.items():
        if "::" not in key:
            continue
        book = key.split("::", 1)[1]
        prev = ages.get(book)
        if prev is None or age < prev:
            ages[book] = age
    return ages


def latest_odds_age_by_league_book() -> dict[str, int]:
    """Newest moneyline/spread odds age keyed as LEAGUE::bookmaker."""
    db = __get_db1_session__()
    try:
        rows = (
            db.query(
                ArbitrageOdds.league,
                ArbitrageOdds.bookmaker,
                func.max(ArbitrageOdds.created_at).label("latest"),
            )
            .group_by(ArbitrageOdds.league, ArbitrageOdds.bookmaker)
            .all()
        )
        now = datetime.utcnow()
        ages = {}
        for league, bookmaker, latest in rows:
            if latest is None or not bookmaker:
                continue
            key = _odds_age_key(league or "", bookmaker)
            ages[key] = max(0, int((now - latest).total_seconds()))
        return ages
    finally:
        db.close()


def check_book_health(bookmaker: str, odds_ages: dict[str, int]) -> list[HealthIssue]:
    spec = BOOK_SPECS.get(bookmaker)
    if not spec:
        return []

    issues: list[HealthIssue] = []
    label = f"{spec['label']}-{spec['stack'].upper()}"
    job_name = spec["job_name"]
    script = spec.get("process_pattern") or spec["script"]
    log_name = spec["log"]
    running = _process_running(script)
    age_key = _odds_age_key(spec["league"], spec["odds_book"])
    age = odds_ages.get(age_key)
    # Fallback to bare bookmaker if league rows missing.
    if age is None:
        age = odds_ages.get(spec["odds_book"])
    lines = _tail_lines(log_name)

    if age is None:
        issues.append(
            HealthIssue(
                component=bookmaker,
                severity="critical",
                code="no_odds",
                message=f"{label}: no {spec['league']} odds rows in DB",
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
    """Check each stack's arb scanner (wnba + mlb)."""
    issues: list[HealthIssue] = []

    for stack in STACKS:
        component = f"arbitrage_{stack['name']}"
        label = f"Arb-{stack['title']}"
        pattern = stack["arb_pattern"]
        log_name = stack["arb_log"]
        job_name = stack["arb_job"]
        running = _process_running(pattern)
        lines = _tail_lines(log_name)
        log_age = _log_mtime(log_name)

        log_stale = log_age is not None and (time.time() - log_age) > OPS_ARB_SCAN_STALE_SECONDS

        if lines and IMPORT_ERROR.search("".join(lines[-40:])):
            issues.append(
                HealthIssue(
                    component=component,
                    severity="critical",
                    code="import_error",
                    message=f"{label}: ImportError in log — needs code fix",
                    auto_fixable=False,
                )
            )
            continue

        if not running:
            issues.append(
                HealthIssue(
                    component=component,
                    severity="critical",
                    code="process_down",
                    message=f"{label}: scanner process not running",
                    auto_fixable=True,
                )
            )
        elif log_stale and not any(
            SCAN_LINE.search(ln) or "Scan Opportunities" in ln for ln in lines[-80:]
        ):
            issues.append(
                HealthIssue(
                    component=component,
                    severity="critical",
                    code="scan_stale",
                    message=(
                        f"{label}: no recent scan output "
                        f"({int(time.time() - log_age)}s since log write)"
                    ),
                    auto_fixable=True,
                    details={"log_age_seconds": int(time.time() - log_age)},
                )
            )

        lock = _lock_path(job_name)
        if os.path.exists(lock) and not running:
            issues.append(
                HealthIssue(
                    component=component,
                    severity="warning",
                    code="stale_lock",
                    message=f"{label}: stale flock lock with no process",
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
    if spec:
        job_name = spec["job_name"]
    elif issue.component.startswith("arbitrage_"):
        job_name = issue.component
    else:
        job_name = issue.component

    if issue.code in ("stale_lock", "process_down", "stale_odds", "chrome_init_loop", "scan_stale"):
        actions.append(_clear_stale_lock(job_name))

    if issue.code in ("chrome_init_loop", "stale_odds", "process_down") and spec and spec.get("uses_chrome"):
        actions.append(_cleanup_chrome_temps(aggressive=issue.code == "chrome_init_loop"))
        actions.append(_kill_orphan_chrome_profiles())

    if not actions:
        return None

    _mark_remediation(key)
    return "; ".join(actions)


async def send_ops_alert(message: str) -> None:
    token = TELEGRAM.get("bot_token")
    chat_id = telegram_health_chat_id()
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


# ---------------------------------------------------------------------------
# Host / stack heartbeat (TELEGRAM_CHAT_HEALTH)
# ---------------------------------------------------------------------------


def _read_proc_cpu_times() -> Optional[tuple[int, int]]:
    """Return (idle+iowait, total) jiffies from /proc/stat, or None."""
    try:
        with open("/proc/stat", "r") as f:
            first = f.readline()
        if not first.startswith("cpu "):
            return None
        parts = [int(x) for x in first.split()[1:]]
        if len(parts) < 4:
            return None
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        return idle, sum(parts)
    except Exception:
        return None


def _cpu_percent(sample_seconds: float = 0.35) -> Optional[float]:
    """Sample CPU usage % over a short interval (100 = fully busy)."""
    a = _read_proc_cpu_times()
    if a is None:
        return None
    time.sleep(sample_seconds)
    b = _read_proc_cpu_times()
    if b is None:
        return None
    idle_delta = b[0] - a[0]
    total_delta = b[1] - a[1]
    if total_delta <= 0:
        return 0.0
    busy = max(0.0, 1.0 - (idle_delta / total_delta))
    return round(busy * 100.0, 1)


def _mem_stats() -> Optional[dict]:
    """Memory used % from /proc/meminfo (used = total - available)."""
    try:
        info: dict[str, int] = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].endswith(":"):
                    info[parts[0][:-1]] = int(parts[1])
        total_kb = info.get("MemTotal")
        avail_kb = info.get("MemAvailable")
        if not total_kb or avail_kb is None:
            return None
        used_kb = max(0, total_kb - avail_kb)
        return {
            "total_gb": round(total_kb / (1024 * 1024), 1),
            "used_gb": round(used_kb / (1024 * 1024), 1),
            "avail_gb": round(avail_kb / (1024 * 1024), 1),
            "used_pct": round((used_kb / total_kb) * 100.0, 1),
        }
    except Exception:
        return None


def _disk_stats() -> Optional[dict]:
    try:
        st = os.statvfs(str(BASE_PATH))
        total = st.f_frsize * st.f_blocks
        free = st.f_frsize * st.f_bavail
        used = max(0, total - free)
        if total <= 0:
            return None
        return {
            "used_pct": round((used / total) * 100.0, 1),
            "free_gb": round(free / (1024 ** 3), 1),
            "total_gb": round(total / (1024 ** 3), 1),
        }
    except Exception:
        return None


def _pgrep_count(pattern: str, *, full_cmdline: bool = False) -> int:
    try:
        cmd = ["pgrep", "-c"]
        if full_cmdline:
            cmd.append("-f")
        cmd.append(pattern)
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        out = (r.stdout or "").strip()
        if not out:
            return 0
        return int(out.splitlines()[-1])
    except Exception:
        return 0


def _chrome_profile_count(stack: Optional[str] = None) -> int:
    """Distinct live chrome_user_data_* profiles with at least one process."""
    try:
        needle = f"chrome_user_data_{stack}_" if stack else "chrome_user_data_"
        r = subprocess.run(
            ["pgrep", "-af", f"user-data-dir=.*{needle}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        dirs = set()
        for line in (r.stdout or "").splitlines():
            m = re.search(r"user-data-dir=(\S*chrome_user_data_\S+)", line)
            if m:
                dirs.add(m.group(1))
        return len(dirs)
    except Exception:
        return 0


def _read_env_kv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip("'").strip('"')
    except Exception:
        pass
    return out


def _stack_config(stack_name: str, env_file: str) -> dict:
    base = _read_env_kv(BASE_PATH / ".env")
    overlay = _read_env_kv(BASE_PATH / env_file)
    merged = {**base, **overlay}
    return {
        "stake": merged.get("BET_STAKE", "?"),
        "threshold": merged.get("MIN_ARB_PROFIT_PCT", "?"),
        "pairs": merged.get("ACTIVE_ARB_BOOK_PAIRS", ""),
        "s411": merged.get("SPORTS411_ACCOUNT", "?"),
        "amapola": merged.get("BETAMAPOLA_ACCOUNT", "?"),
        "fourcasters": merged.get("FOURCASTERS_ACCOUNT", "?"),
    }


def _fmt_age(seconds: Optional[int]) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _latest_extracted_count(log_name: str) -> Optional[int]:
    for ln in reversed(_tail_lines(log_name, 80)):
        m = EXTRACTED_LINE.search(ln)
        if not m:
            continue
        for g in m.groups():
            if g is not None:
                return int(g)
    return None


def _latest_scan_stats(log_name: str) -> Optional[tuple[int, int, int]]:
    for ln in reversed(_tail_lines(log_name, 120)):
        m = SCAN_LINE.search(ln)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


def collect_host_metrics() -> dict:
    """CPU %, memory %, disk, Chrome footprint for the health heartbeat."""
    mem = _mem_stats() or {}
    disk = _disk_stats() or {}
    # Process-name match (not -f) so "chrome" does not also count chromedriver.
    chrome = _pgrep_count("chrome")
    chromedriver = _pgrep_count("chromedriver")
    profiles = _chrome_profile_count()
    return {
        "cpu_pct": _cpu_percent(),
        "mem_used_pct": mem.get("used_pct"),
        "mem_used_gb": mem.get("used_gb"),
        "mem_total_gb": mem.get("total_gb"),
        "disk_used_pct": disk.get("used_pct"),
        "disk_free_gb": disk.get("free_gb"),
        "chrome": chrome,
        "chromedriver": chromedriver,
        "chrome_profiles": profiles,
        "chrome_profiles_wnba": _chrome_profile_count("wnba"),
        "chrome_profiles_mlb": _chrome_profile_count("mlb"),
        "load_avg": os.getloadavg() if hasattr(os, "getloadavg") else None,
    }


def collect_stack_status(odds_ages: dict[str, int]) -> list[dict]:
    """Per-stack process / odds / scan snapshot for the heartbeat."""
    rows = []
    for stack in STACKS:
        cfg = _stack_config(stack["name"], stack["env_file"])
        books = []
        for book_key in stack["books"]:
            spec = BOOK_SPECS[book_key]
            age_key = _odds_age_key(spec["league"], spec["odds_book"])
            age = odds_ages.get(age_key)
            running = _process_running(spec["process_pattern"])
            extracted = _latest_extracted_count(spec["log"])
            fresh = age is not None and age <= OPS_ODDS_STALE_SECONDS
            books.append(
                {
                    "key": book_key,
                    "short": spec["short"],
                    "running": running,
                    "odds_age": age,
                    "extracted": extracted,
                    "fresh": fresh,
                    "ok": running and fresh,
                }
            )
        arb_running = _process_running(stack["arb_pattern"])
        scan = _latest_scan_stats(stack["arb_log"])
        arb_log_age = _log_mtime(stack["arb_log"])
        arb_age_s = int(time.time() - arb_log_age) if arb_log_age else None
        rows.append(
            {
                "name": stack["name"],
                "title": stack["title"],
                "league": stack["league"],
                "config": cfg,
                "arb_running": arb_running,
                "arb_log_age": arb_age_s,
                "scan": scan,
                "books": books,
                "chrome_profiles": _chrome_profile_count(stack["name"]),
                "ok": arb_running and all(b["ok"] for b in books),
            }
        )
    return rows


def format_host_status_message(metrics: dict, stacks: Optional[list[dict]] = None) -> str:
    cpu = metrics.get("cpu_pct")
    mem_pct = metrics.get("mem_used_pct")
    mem_used = metrics.get("mem_used_gb")
    mem_total = metrics.get("mem_total_gb")
    disk_pct = metrics.get("disk_used_pct")
    disk_free = metrics.get("disk_free_gb")
    chrome = int(metrics.get("chrome") or 0)
    chromedriver = int(metrics.get("chromedriver") or 0)
    profiles = int(metrics.get("chrome_profiles") or 0)
    load = metrics.get("load_avg")

    cpu_s = f"{cpu:.0f}%" if cpu is not None else "n/a"
    if mem_pct is not None and mem_used is not None and mem_total is not None:
        mem_s = f"{mem_pct:.0f}% ({mem_used:.1f}/{mem_total:.1f} GiB)"
    else:
        mem_s = "n/a"
    disk_s = ""
    if disk_pct is not None and disk_free is not None:
        disk_s = f"\nDisk {disk_pct:.0f}% used · {disk_free:.1f} GiB free"
    load_s = ""
    if load and len(load) >= 3:
        load_s = f"\nLoad {load[0]:.2f} / {load[1]:.2f} / {load[2]:.2f}"

    warn = chrome >= OPS_CHROME_WARN_COUNT
    chrome_line = (
        f"{'⚠️ ' if warn else ''}Chrome {chrome} · chromedriver {chromedriver} · profiles {profiles}"
    )
    if warn:
        chrome_line += f"  (warn ≥{OPS_CHROME_WARN_COUNT} — possible leak)"

    lines = [
        f"Host status @ {_utc_now().strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"CPU {cpu_s} · Mem {mem_s}",
        chrome_line + disk_s + load_s,
    ]

    if stacks:
        lines.append("")
        for st in stacks:
            mark = "OK" if st.get("ok") else "CHECK"
            cfg = st.get("config") or {}
            lines.append(
                f"{st['title']} [{mark}] · stake ${cfg.get('stake', '?')} · thr {cfg.get('threshold', '?')}%"
            )
            lines.append(
                f"  accts {cfg.get('fourcasters', '?')} / {cfg.get('s411', '?')} / {cfg.get('amapola', '?')}"
            )
            arb_mark = "✓" if st.get("arb_running") else "✗"
            scan = st.get("scan")
            if scan:
                scan_s = f"odds {scan[0]} · matches {scan[1]} · arbs {scan[2]}"
            else:
                age = st.get("arb_log_age")
                scan_s = f"log {_fmt_age(age)}" if age is not None else "no scan yet"
            lines.append(f"  Arb {arb_mark} · {scan_s}")

            book_bits = []
            for b in st.get("books") or []:
                if not b.get("running"):
                    mark_b = "✗"
                elif b.get("fresh"):
                    mark_b = "✓"
                else:
                    mark_b = "~"  # process up, odds lagging
                extra = _fmt_age(b.get("odds_age"))
                if b.get("extracted") is not None:
                    extra += f"/{b['extracted']}g"
                book_bits.append(f"{b['short']} {mark_b} {extra}")
            lines.append("  " + " · ".join(book_bits))
            lines.append(f"  Chrome profiles: {st.get('chrome_profiles', 0)}")

    return "\n".join(lines)


def _host_status_stamp_path() -> str:
    return "/tmp/ops_host_status_last.ts"


def maybe_send_host_status(
    logger=None,
    *,
    force: bool = False,
    odds_ages: Optional[dict[str, int]] = None,
) -> Optional[str]:
    """Post CPU/mem/chrome + per-stack heartbeat to the health channel every N seconds."""
    if not OPS_HOST_STATUS_ENABLED:
        return None

    stamp = _host_status_stamp_path()
    if not force and os.path.exists(stamp):
        try:
            age = time.time() - os.path.getmtime(stamp)
            if age < OPS_HOST_STATUS_INTERVAL_SECONDS:
                return None
        except Exception:
            pass

    if odds_ages is None:
        odds_ages = latest_odds_age_by_league_book()
    metrics = collect_host_metrics()
    stacks = collect_stack_status(odds_ages)
    message = format_host_status_message(metrics, stacks)
    asyncio.run(send_ops_alert(message))

    try:
        with open(stamp, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass

    if logger:
        logger.info(
            f"Host status posted | CPU={metrics.get('cpu_pct')}% "
            f"Mem={metrics.get('mem_used_pct')}% Chrome={metrics.get('chrome')} "
            f"WNBA={'ok' if stacks and stacks[0].get('ok') else 'check'} "
            f"MLB={'ok' if stacks and len(stacks) > 1 and stacks[1].get('ok') else 'check'}"
        )
    return message


def run_health_cycle(logger=None) -> dict:
    """One full health check + remediation pass. Returns summary dict."""
    odds_ages = latest_odds_age_by_league_book()
    issues: list[HealthIssue] = []

    for book in sorted(BOOK_SPECS):
        issues.extend(check_book_health(book, odds_ages))

    # Also keep legacy single-name books if still configured elsewhere.
    for book in sorted(ACTIVE_ARB_BOOKMAKERS):
        if book in BOOK_SPECS:
            continue
        # skip — dual-stack specs cover active books

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

    host_msg = maybe_send_host_status(logger=logger, odds_ages=odds_ages)

    return {
        "issues": [{"component": i.component, "code": i.code, "message": i.message} for i in issues],
        "remediated": remediated,
        "odds_ages": odds_ages,
        "host_status_sent": bool(host_msg),
    }
