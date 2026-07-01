import os
import re
import time
import traceback
import requests
from time import sleep
import random
import configparser
from datetime import datetime, timedelta
from decimal import Decimal
import pytz
import asyncio
import tracemalloc
from utils.config import TELEGRAM, LOG_DIR, telegram_health_chat_id


# engine = create_engine('mysql://root@localhost/lockz')
# metadata = MetaData()
config = configparser.ConfigParser()

authorization = None
config.read("constants.ini")

# Create a request session
session = requests.Session()
    
def get_session():
    return session

def random_sleep(min_time=2, max_time=5):
    sleep_time = random.uniform(min_time, max_time)
    sleep(sleep_time)


def get_debug_dir():
    """Directory for HTML/JSON debug artifacts (under logs/debug, not project root)."""
    debug_dir = os.path.join(LOG_DIR or "logs", "debug")
    os.makedirs(debug_dir, exist_ok=True)
    return debug_dir


def debug_filepath(name: str) -> str:
    """Build a timestamped path under logs/debug/. Pass name without extension."""
    return os.path.join(get_debug_dir(), f"{name}_{int(time.time())}.html")


def prune_debug_files(max_age_hours: int = 24):
    """Remove debug artifacts older than max_age_hours from logs/debug/."""
    debug_dir = get_debug_dir()
    cutoff = time.time() - max_age_hours * 3600
    try:
        for fname in os.listdir(debug_dir):
            path = os.path.join(debug_dir, fname)
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except Exception:
        pass


def parse_game_datetime(value):
    """Parse game_datetime from str or datetime; returns naive UTC datetime or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    dt = datetime.strptime(text[:19], fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(pytz.UTC).replace(tzinfo=None)
    return dt


def is_game_pregame(game_datetime) -> bool:
    """Return True when game_datetime is in the future (game has not started yet)."""
    dt = parse_game_datetime(game_datetime)
    if dt is None:
        return False
    return dt > datetime.utcnow()

def run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop → safe to use asyncio.run
        return asyncio.run(coro)
    else:
        # Already in an event loop → schedule task
        return asyncio.create_task(coro)

def set_authorization(token):
    config.set("Authorization", "token", "Bearer {}".format(token))


def get_authorization():
    return config.get("Authorization", "token")


def get_csv_headers():
    return [
        "Book Ticket ID",
        "Game #",
        "Team 1",
        "Team 2",
        "Bet Type",
        "Odds",
        "Risk",
        "Win",
        "Status",
        "Final Score",
        "Accepted",
        "Placed On",
        "Sport",
        "Period",
        "Date",
        "Time",
        "Timezone",
        "MASTER BET ID",
        "WORKER",
        "WORKER FREEROLL %",
        "HANDICAPPER",
        "HANDICAPPER RISK %",
        "PLAY TYPE",
        "AGENT ID",
        "SUB AGENT ID",
        "BOOK ID",
        "MASTER AGENT RISK %",
        "SUB AGENT RISK %",
        "SUB AGENT FREEROLL %",
        "MASTER AGENT FREEROLL %",
        "NOTES",
        "UPLOAD FILES (PHOTO/VIDEO)",
    ]


def get_last_5_weeks_dates(format):
    # Get the current date and time
    now = datetime.now()
    # Create a list to store the dates
    last_5_weeks_dates = []
    # Get the start of the week (Monday) for the current date
    start_of_week = now - timedelta(days=now.weekday())
    for i in range(7):
        # Get the start and end date of the current week
        start_date = start_of_week - timedelta(days=7 * i)
        end_date = start_date + timedelta(days=6)

        # Loop through the dates in the current week
        current_date = start_date
        while current_date <= end_date:
            last_5_weeks_dates.append(current_date.strftime(format))
            current_date += timedelta(days=1)

    return last_5_weeks_dates


def get_last_2_weeks_dates(format):
    # Get the current date and time
    now = datetime.now()
    # Create a list to store the dates
    last_2_weeks_dates = []
    # Get the start of the week (Monday) for the current date
    start_of_week = now - timedelta(days=now.weekday())
    for i in range(7):
        # Get the start and end date of the current week
        start_date = start_of_week - timedelta(days=7 * i)
        end_date = start_date + timedelta(days=6)

        # Loop through the dates in the current week
        current_date = start_date
        while current_date <= end_date:
            last_2_weeks_dates.append(current_date.strftime(format))
            current_date += timedelta(days=1)

    return last_2_weeks_dates


def get_constants():
    config = configparser.ConfigParser()
    config.read("constants.ini")
    return config


def get_credentials():
    # credentials = {
    #     'customerID': 'doc1105',
    #     'password': '**********',
    #     'login.x': '42',
    #     'login.y': '5'
    # }
    # customer_id = 'tv111'
    # password = '**********'
    # login_x = '42'
    # login_y = '5'
    customer_id = "doc1105"
    password = "**********"
    login_x = "42"
    login_y = "5"
    return customer_id, password, login_x, login_y


def get_headers():
    headers = {
        "Host": "lockz.co",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/111.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Content-Type": "application/x-www-form-urlencoded",
        "Content-Length": "56",
        "Origin": "https://lockz.co",
        "Connection": "keep-alive",
        "Referer": "https://lockz.co/",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }
    return headers

def determine_wager_on_spread(spread, wager_on=None):
    """
    Process the spread to remove prefixes like u, o, under, or over
    and determine the value of wager_on.

    Args:
        spread (str): The original spread value.
        wager_on (str, optional): The initial wager_on value. Defaults to None.

    Returns:
        tuple: A tuple containing the cleaned spread and wager_on value.
    """
    spread = str(spread).strip().lower()  # Normalize the spread string

    # Check and process prefix
    if spread.startswith(("u", "under")):
        wager_on = "UNDER"
        spread = spread.removeprefix("under").removeprefix("u").strip()
    elif spread.startswith(("o", "over")):
        wager_on = "OVER"
        spread = spread.removeprefix("over").removeprefix("o").strip()

    return spread, wager_on

def format_website(url):
    # Remove http, https, and www
    website = url.replace('http://', '').replace('https://', '').replace('www.', '')
    return website.split('/')[0]  # Return only the domain


TELEGRAM_TZ = pytz.timezone("America/New_York")


def _as_eastern(ts=None, dt=None):
    """Normalize unix timestamp or datetime to US Eastern (handles naive UTC datetimes)."""
    if dt is not None:
        if dt.tzinfo is None:
            dt = pytz.UTC.localize(dt)
        else:
            dt = dt.astimezone(pytz.UTC)
    elif ts is None:
        dt = datetime.now(pytz.UTC)
    else:
        dt = datetime.fromtimestamp(float(ts), tz=pytz.UTC)
    return dt.astimezone(TELEGRAM_TZ)


def format_utc_timestamp(ts=None, *, dt=None, time_only: bool = False) -> str:
    """Format a timestamp for Telegram alerts in US Eastern Time (EST/EDT)."""
    eastern = _as_eastern(ts=ts, dt=dt)
    if time_only:
        return eastern.strftime("%H:%M:%S %Z")
    return eastern.strftime("%Y-%m-%d %H:%M:%S %Z")

async def send_telegram_alert(alert, chat_id = None) -> None:
    tracemalloc.start()
    try:
        from telegram import Bot
        token = TELEGRAM.get('bot_token')
        if not token:
            print("Telegram alerts disabled (no bot_token) - skipping")
            return
        chat_id = chat_id or TELEGRAM.get('chat_id')
        if not chat_id:
            print("No chat_id for telegram alert - skipping")
            return
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=alert)
    except Exception as e:
        print(f"Telegram alert failed (non-fatal) for chat_id={chat_id}: {e}")

async def send_monitoring_alert(website, account, ex, chat_id = None) -> None:
    tracemalloc.start()
    try:
        from telegram import Bot
        token = TELEGRAM.get('bot_token')
        if not token:
            print("Telegram monitoring disabled (no bot_token) - skipping alert for error")
            return
        chat_id = chat_id or telegram_health_chat_id()
        if not chat_id:
            print("No health/monitoring chat_id - skipping telegram error alert")
            return
        timestamp = format_utc_timestamp()

        exception_msg = f"{str(ex)}"
        tb_list = traceback.format_exception(type(ex), ex, ex.__traceback__, limit=3)
        trace = "".join(tb_list)
        
        if len(exception_msg) + len(trace) > 3600:
            exception_msg = exception_msg[:1800]
            trace = trace[:1800] + "... [truncated]"

        alert = (
            f"Website: {website}\n"
            f"Account: {account}\n" 
            f"Timestamp: {timestamp}\n"
            f"Exception: {exception_msg}\n"
            f"Traceback:\n{trace}"
        )
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=alert)
    except Exception as e:
        print(f"Telegram monitoring alert failed (non-fatal) for chat_id={chat_id}: {e}")


async def send_testing_alert(alert, chat_id = None) -> None:
    tracemalloc.start()
    try:
        from telegram import Bot
        token = TELEGRAM.get('bot_token')
        if not token:
            print("Telegram testing disabled (no bot_token) - skipping")
            return
        chat_id = chat_id or TELEGRAM.get('testing')
        if not chat_id:
            print("No testing chat_id - skipping")
            return
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=alert)
    except Exception as e:
        print(f"Telegram testing alert failed (non-fatal) for chat_id={chat_id}: {e}")


async def discover_telegram_chats() -> None:
    """Utility to discover chat IDs the bot can send to.

    Consistent with colleague feedback: create a new group chat, add the bot
    (kleyman_arb_bot) + the people who should receive arb alerts, send a test
    message in the group, then run this to get the numeric chat ID (required for
    groups; the bot must be a member).

    Run with:
        python -c '
        import asyncio
        from utils.helpers import discover_telegram_chats
        asyncio.run(discover_telegram_chats())
        '

    Paste the printed Chat ID into .env:
      TELEGRAM_CHAT_ARBITRAGE=-100...   (===== Arbitrage ===== alerts)
      TELEGRAM_CHAT_BETTING=-100...     (===== Moneyline Bet ===== alerts)
    Groups/channels use large negative IDs. Then restart the service.

    The bot created for this project is kleyman_arb_bot (token goes in TELEGRAM_BOT_TOKEN).

    See TELEGRAM_SETUP.md (in the repo root) for the full tailored instructions, including the exact steps
    after you received the BotFather token screenshot.
    """
    tracemalloc.start()
    try:
        from telegram import Bot
        token = TELEGRAM.get('bot_token')
        if not token:
            print("No TELEGRAM_BOT_TOKEN - cannot discover chats")
            return
        bot = Bot(token=token)
        updates = await bot.get_updates(limit=100, timeout=5)
        if not updates:
            print("No recent updates seen by the bot.")
            print("Steps:")
            print("  1. Create the Telegram group (or use existing).")
            print("  2. Add the bot by searching for its username: kleyman_arb_bot")
            print("  3. Add the recipient(s) to the group.")
            print("  4. Post a message in the group from one of the members.")
            print("  5. Re-run this discover function.")
            return
        seen = {}
        for update in updates:
            msg = update.message or update.channel_post or update.edited_channel_post
            if not msg or not msg.chat:
                continue
            chat = msg.chat
            cid = chat.id
            if cid in seen:
                continue
            seen[cid] = True
            name = chat.title or chat.username or (f"{chat.first_name or ''} {chat.last_name or ''}".strip()) or "unnamed"
            print(f"Chat ID: {cid}")
            print(f"  Type: {chat.type}")
            print(f"  Name/Title: {name}")
            print(f"  Suggested .env lines:")
            print(f"    TELEGRAM_CHAT_ARBITRAGE={cid}  # arb opportunity alerts")
            print(f"    TELEGRAM_CHAT_BETTING={cid}    # confirmed moneyline bet alerts")
            print()
        if not seen:
            print("No chats with messages found in updates.")
    except Exception as e:
        print(f"discover_telegram_chats failed (non-fatal): {e}")


# -----------------------------------
# Numbers
# -----------------------------------
def currency_to_float(currency):
    """
    Convert a currency to a float.
    Handles:
    - Dollar signs ($)
    - Commas as thousand separators
    - Parentheses for negative values (accounting format)
    - Negative signs
    - Whitespace
    
    Args:
        currency (str): The currency string to convert (e.g., "$-69,170.00")
    
    Returns:
        float: The numeric value
    
    Raises:
        ValueError: If the string cannot be converted to a float
    """
    # Remove all non-numeric characters except digits, minus sign, and decimal point
    currency = str(currency)
    cleaned = (currency.replace('(', '-')  # Replace opening parenthesis with minus
               .replace(')', '')  # Remove closing parenthesis
               .replace('$', '')  # Remove dollar signs
               .replace(',', '')  # Remove thousand separators
               .strip())  # Remove any whitespace
    
    try:
        return float(cleaned)
    except ValueError as e:
        raise ValueError(f"Could not convert string '{currency}' to float") from e

def to_decimal(value):
    if value is None or value == "":
        return None
    return Decimal(str(value))

def format_money(amount, symbol="$", decimals=2):
    """
    Format a numeric value as a currency string.
    
    Args:
        amount (float): The numeric value to format
        symbol (str): The currency symbol to prepend (default: "$")
        decimals (int): Number of decimal places (default: 2)
    
    Returns:
        str: Formatted currency string, e.g., "$1,234.56"
    """
    try:
        formatted = f"{amount:,.{decimals}f}"  # Adds commas and fixes decimals
        return f"{symbol}{formatted}"
    except Exception as e:
        raise ValueError(f"Could not format value '{amount}' as currency") from e

# -----------------------------------
# Date
# -----------------------------------
def parse_to_mysql_datetime(date_str, time_str=None, tz_name=None):
    """
    Parse date and time strings to MySQL DATETIME format.

    Supports flexible calling:
      - parse_to_mysql_datetime("12/16", "17:30")
      - parse_to_mysql_datetime("12/16/2026 17:30")
      - parse_to_mysql_datetime("2026-06-01 18:40:00")
      - parse_to_mysql_datetime("06/01/2026 18:40")

    If tz_name is provided (e.g. 'US/Eastern'), the parsed datetime is treated as
    local time in that timezone, then converted to UTC before returning the string.
    This ensures consistent game_datetime across bookmakers with different TZ displays.

    Args:
        date_str: Date part or combined "date time" string
        time_str: Optional time part
        tz_name: Optional pytz timezone name for the source time (e.g. 'US/Eastern', 'US/Pacific')

    Returns:
        String in MySQL DATETIME format (YYYY-MM-DD HH:MM:SS) in UTC if tz_name given, else naive as parsed.
    """
    try:
        if time_str is None and date_str:
            s = str(date_str).strip()
            if ' ' in s:
                # combined "MM/DD/YYYY HH:MM[:SS]" or "YYYY-MM-DD HH:MM[:SS]"
                date_part, time_part = s.split(' ', 1)
                date_str = date_part
                time_str = time_part
            else:
                time_str = "00:00"

        if tz_name:
            tz = pytz.timezone(tz_name)
            now = datetime.now(tz)
        else:
            now = datetime.now()
        current_year = now.year
        current_date = now

        # Parse date (support MM/DD, MM/DD/YYYY, YYYY-MM-DD)
        year = None
        month = day = None
        ds = str(date_str).strip()
        if '/' in ds:
            dparts = [p for p in ds.split('/') if p]
            if len(dparts) == 3:
                month, day, yr = int(dparts[0]), int(dparts[1]), int(dparts[2])
                if yr < 100:
                    yr += 2000
                year = yr
            elif len(dparts) == 2:
                month, day = int(dparts[0]), int(dparts[1])
        elif '-' in ds:
            dparts = [p for p in ds.split('-') if p]
            if len(dparts) == 3:
                year, month, day = int(dparts[0]), int(dparts[1]), int(dparts[2])

        if month is None or day is None:
            month, day = 1, 1

        if year is None:
            year = current_year
            game_date = datetime(year, month, day)
            # Handle year rollover for short dates
            if game_date.month == 1 and current_date.month == 12:
                game_date = game_date.replace(year=year + 1)
            elif game_date < current_date - timedelta(days=30):
                game_date = game_date.replace(year=year + 1)
        else:
            game_date = datetime(year, month, day)

        # Parse time (handle "7:10", "19:10", "7:10 PM", "19:10:00")
        time_str_lower = str(time_str).lower().strip()
        is_pm = 'pm' in time_str_lower
        is_am = 'am' in time_str_lower

        if is_pm or is_am:
            time_clean = time_str_lower.replace('am', '').replace('pm', '').strip()
            tparts = time_clean.split(':')
            hour = int(tparts[0])
            minute = int(tparts[1]) if len(tparts) > 1 else 0
            if is_pm and hour != 12:
                hour += 12
            elif is_am and hour == 12:
                hour = 0
        else:
            tparts = time_str_lower.split(':')
            hour = int(tparts[0])
            minute = int(tparts[1]) if len(tparts) > 1 else 0

        final_datetime = game_date.replace(hour=hour, minute=minute, second=0, microsecond=0)

        if tz_name:
            tz = pytz.timezone(tz_name)
            dt_local = tz.localize(final_datetime)
            final_datetime = dt_local.astimezone(pytz.UTC).replace(tzinfo=None)

        return final_datetime.strftime('%Y-%m-%d %H:%M:%S')

    except Exception as e:
        print(f"Error converting to MySQL datetime: {date_str} {time_str} - {e}")
        return None


def epoch_to_mysql_datetime(epoch_time, is_milliseconds=False):
    """
    Convert epoch time to MySQL DATETIME format.
    
    Args:
        epoch_time: Unix timestamp (int or float)
        is_milliseconds: True if epoch_time is in milliseconds, False for seconds
    
    Returns:
        String in MySQL DATETIME format (YYYY-MM-DD HH:MM:SS)
    """
    try:
        if is_milliseconds:
            # Convert milliseconds to seconds
            timestamp = epoch_time / 1000.0
        else:
            timestamp = float(epoch_time)
        
        # Convert to datetime object
        dt = datetime.fromtimestamp(timestamp)
        
        # Format as MySQL DATETIME
        return dt.strftime('%Y-%m-%d %H:%M:%S')
        
    except Exception as e:
        print(f"Error converting epoch {epoch_time} to MySQL datetime: {e}")
        return None

# -----------------------------------
# Odds
# -----------------------------------
def detect_odds_type(odds):
    odds = float(odds)

    if odds >= 1.01 and odds < 10:
        return "decimal"
    if odds <= -100 or odds >= 100:
        return "american"

    raise ValueError(f"Cannot detect odds type: {odds}")


def probability_to_american(probability, precision=0):
    """Convert a Polymarket-style implied probability (0-1) to American odds."""
    p = float(probability)
    if p <= 0 or p >= 1:
        raise ValueError(f"Probability must be between 0 and 1: {probability}")
    decimal_odds = 1.0 / p
    return decimal_to_american(decimal_odds, precision)


def decimal_to_american(decimal_odds, precision=0):
    """
    Convert Decimal odds to American odds
    precision: decimals to round American odds (0 = sportsbook standard)
    """
    d = float(decimal_odds)

    if d < 1:
        raise ValueError("Decimal odds must be >= 1.0")

    if d >= 2:
        american = (d - 1) * 100
    else:
        american = -100 / (d - 1)

    return round(american, precision)

def american_to_decimal(american_odds, precision=3):
    """
    Convert American odds to Decimal odds
    precision: decimals to round Decimal odds
    """
    a = float(american_odds)

    if a == 0:
        raise ValueError("American odds cannot be 0")

    if a > 0:
        decimal = (a / 100) + 1
    else:
        decimal = (100 / abs(a)) + 1

    return round(decimal, precision)

def american_to_probability(odds):
    odds = float(odds)
    if odds > 0:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)


def decimal_to_probability(odds):
    return 1 / float(odds)


def implied_probability(odds):
    odds_type = detect_odds_type(odds)
    return (
        american_to_probability(odds)
        if odds_type == "american"
        else decimal_to_probability(odds)
    )

def odds_equal(odds1, odds2, tolerance=1e-6):
    """
    Returns True if odds represent the same implied probability
    tolerance handles rounding differences between bookmakers
    """
    p1 = implied_probability(odds1)
    p2 = implied_probability(odds2)

    return abs(p1 - p2) <= tolerance


def american_odds_to_int(odds) -> int:
    """Normalize American odds to a signed integer (e.g. '+150' -> 150, '-110' -> -110)."""
    text = str(odds).strip().replace("−", "-")
    if not text:
        raise ValueError(f"Invalid American odds: {odds!r}")
    return int(float(text))


def arb_live_odds_acceptable(expected, live, tolerance: int = 0) -> bool:
    """True when live American odds match expected exactly, or within ±tolerance."""
    if live in (None, ""):
        return False
    try:
        exp = american_odds_to_int(expected)
        liv = american_odds_to_int(live)
    except (TypeError, ValueError):
        return False
    if tolerance <= 0:
        return exp == liv
    return abs(exp - liv) <= tolerance


def normalize_team(name: str) -> str:
    """Return canonical team slug for cross-book matchup keys."""
    from utils.team_registry import canonical_team

    return canonical_team(name)


def teams_same(a: str, b: str) -> bool:
    from utils.team_registry import canonical_team

    a_c = canonical_team(a)
    b_c = canonical_team(b)
    if a_c and b_c and a_c == b_c:
        return True
    a_n, b_n = (a or "").strip().lower(), (b or "").strip().lower()
    if not a_n or not b_n:
        return False
    return a_n == b_n or a_n in b_n or b_n in a_n


def align_cross_book_moneylines(o1: dict, o2: dict):
    """
    Return moneylines on o1's team_1/team_2 orientation:
    (o1_t1_ml, o1_t2_ml, o2_t1_ml, o2_t2_ml) or None if teams do not match.
    """
    if teams_same(o1["team_1"], o2["team_1"]) and teams_same(o1["team_2"], o2["team_2"]):
        return (
            o1["moneyline_team_1"],
            o1["moneyline_team_2"],
            o2["moneyline_team_1"],
            o2["moneyline_team_2"],
        )
    if teams_same(o1["team_1"], o2["team_2"]) and teams_same(o1["team_2"], o2["team_1"]):
        return (
            o1["moneyline_team_1"],
            o1["moneyline_team_2"],
            o2["moneyline_team_2"],
            o2["moneyline_team_1"],
        )
    return None


def normalize_spread_value(value) -> float | None:
    try:
        if value is None:
            return None
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def spread_values_match(a, b, tolerance: float = 0.01) -> bool:
    left = normalize_spread_value(a)
    right = normalize_spread_value(b)
    if left is None or right is None:
        return False
    return abs(left - right) <= tolerance


def align_cross_book_spreads(o1: dict, o2: dict):
    """
    Return spread juice on o1's team_1/team_2 orientation:
    (o1_t1_spread_odds, o1_t2_spread_odds, o2_t1_spread_odds, o2_t2_spread_odds, spread_value)
    or None if teams/lines do not match.
    """
    line_1 = normalize_spread_value(o1.get("spread_value"))
    line_2 = normalize_spread_value(o2.get("spread_value"))
    if line_1 is None or line_2 is None:
        return None

    if teams_same(o1["team_1"], o2["team_1"]) and teams_same(o1["team_2"], o2["team_2"]):
        if not spread_values_match(line_1, line_2):
            return None
        return (
            o1.get("spread_team_1"),
            o1.get("spread_team_2"),
            o2.get("spread_team_1"),
            o2.get("spread_team_2"),
            line_1,
        )

    if teams_same(o1["team_1"], o2["team_2"]) and teams_same(o1["team_2"], o2["team_1"]):
        if not spread_values_match(line_1, -line_2):
            return None
        return (
            o1.get("spread_team_1"),
            o1.get("spread_team_2"),
            o2.get("spread_team_2"),
            o2.get("spread_team_1"),
            line_1,
        )

    return None


def is_plausible_moneyline_pair(ml_1, ml_2) -> bool:
    """Reject obvious scrape glitches for 2-way American moneylines."""
    try:
        a = float(ml_1)
        b = float(ml_2)
    except (TypeError, ValueError):
        return False
    if a == 0.0 or b == 0.0:
        return False
    # One side must be the underdog (+) and the other the favorite (-).
    return (a > 0 and b < 0) or (a < 0 and b > 0)


def is_plausible_spread_pair(spread_value, spread_team_1, spread_team_2) -> bool:
    if normalize_spread_value(spread_value) is None:
        return False
    try:
        a = float(spread_team_1)
        b = float(spread_team_2)
    except (TypeError, ValueError):
        return False
    if a == 0.0 or b == 0.0:
        return False
    if not ((a > 0 and b < 0) or (a < 0 and b > 0)):
        return False
    return spread_odds_match_line_side(spread_value, spread_team_1, spread_team_2)


def spread_odds_match_line_side(spread_value, spread_team_1, spread_team_2) -> bool:
    """Favorite (negative line on team_1) should have negative juice; dog positive."""
    sv = normalize_spread_value(spread_value)
    if sv is None:
        return False
    try:
        t1 = float(spread_team_1)
        t2 = float(spread_team_2)
    except (TypeError, ValueError):
        return False
    if sv < 0:
        return t1 < 0 and t2 > 0
    if sv > 0:
        return t1 > 0 and t2 < 0
    return False


def fix_spread_odds_orientation(spread_value, spread_team_1, spread_team_2):
    """Swap spread odds when they contradict spread_value / favorite side."""
    if spread_odds_match_line_side(spread_value, spread_team_1, spread_team_2):
        return spread_team_1, spread_team_2
    if spread_odds_match_line_side(spread_value, spread_team_2, spread_team_1):
        return spread_team_2, spread_team_1
    return spread_team_1, spread_team_2


def spread_odds_rows_fresh_for_arb(
    created_at_1,
    created_at_2,
    *,
    max_age_seconds: int,
    max_gap_seconds: int,
    now=None,
) -> bool:
    """Both spread snapshots must be recent and close in time."""
    if created_at_1 is None or created_at_2 is None:
        return False
    if now is None:
        now = datetime.utcnow()
    age_1 = max(0.0, (now - created_at_1).total_seconds())
    age_2 = max(0.0, (now - created_at_2).total_seconds())
    if age_1 > max_age_seconds or age_2 > max_age_seconds:
        return False
    gap = abs((created_at_1 - created_at_2).total_seconds())
    return gap <= max_gap_seconds


def spread_market_label(spread_value, sport: str | None = None) -> str:
    line = normalize_spread_value(spread_value)
    sport_l = (sport or "").strip().lower()
    if sport_l in ("mlb", "baseball") and line is not None and abs(abs(line) - 1.5) <= 0.01:
        return f"run_line ({line:+.1f})"
    if line is None:
        return "spread"
    return f"spread ({line:+.1f})"


def extract_spread_line_odds_from_label(label) -> tuple[float | None, str | None]:
    """Parse handicap + American odds from a spread/run-line bet label."""
    import re

    if label is None:
        return None, None
    text = (label.get("title") if hasattr(label, "get") else None) or getattr(label, "text", "") or ""
    text = str(text).strip()
    match = re.search(r"([+-]?\d+(?:\.\d+)?)\s+([+-]?\d+)\s*$", text)
    if match:
        try:
            spread = float(match.group(1))
            odds = match.group(2)
            # S411 shows "Team 0 0" when run line is not posted yet.
            if spread == 0.0 and odds.lstrip("+-") == "0":
                return None, None
            return spread, odds
        except (TypeError, ValueError):
            return None, None
    return None, None


def sanitize_spread_odds(spread: dict) -> dict | None:
    """Normalize spread juice orientation and reject impossible favorite/dog pairs."""
    if not spread:
        return None
    sv = normalize_spread_value(spread.get("team_1_spread"))
    if sv is None:
        return None
    try:
        s1_raw = spread.get("team_1_odds")
        s2_raw = spread.get("team_2_odds")
        if s1_raw is None or s2_raw is None:
            return None
        s1 = float(str(s1_raw).replace("+", ""))
        s2 = float(str(s2_raw).replace("+", ""))
    except (TypeError, ValueError):
        return None

    s1, s2 = fix_spread_odds_orientation(sv, s1, s2)
    if not is_plausible_spread_pair(sv, s1, s2):
        return None

    team_2_spread = spread.get("team_2_spread")
    if normalize_spread_value(team_2_spread) is None:
        team_2_spread = -sv
    return {
        "team_1_spread": sv,
        "team_2_spread": normalize_spread_value(team_2_spread),
        "team_1_odds": s1,
        "team_2_odds": s2,
    }


def build_spread_odd_row(base: dict, spread: dict) -> dict | None:
    cleaned = sanitize_spread_odds(spread)
    if cleaned is None:
        return None
    return {
        **base,
        "bet_type": "spread",
        "moneyline_team_1": None,
        "moneyline_team_2": None,
        "moneyline_draw": None,
        "spread_team_1": to_decimal(cleaned["team_1_odds"]),
        "spread_team_2": to_decimal(cleaned["team_2_odds"]),
        "spread_value": to_decimal(cleaned["team_1_spread"]),
        "total_points": None,
        "over_odds": None,
        "under_odds": None,
    }
    """Reject obvious scrape glitches for 2-way American moneylines."""
    try:
        a = float(ml_1)
        b = float(ml_2)
    except (TypeError, ValueError):
        return False
    if a == 0.0 or b == 0.0:
        return False
    # One side must be the underdog (+) and the other the favorite (-).
    return (a > 0 and b < 0) or (a < 0 and b > 0)

def parse_odds(payload: dict) -> list[dict]:
    rows = []

    for match in payload.get("matches", []):
        base = {
            "sport": match["sport"],
            "league": match["league"],
            "game_id": match["game_id"],
            "game_datetime": datetime.strptime(
                match["game_datetime"], "%Y-%m-%d %H:%M:%S"
            ),
            "team_1": match["team_1"],
            "team_2": match["team_2"],
            "bookmaker": match["bookmaker"],
        }

        # -------------------
        # MONEYLINE
        # -------------------
        if "moneyline" in match:
            rows.append({
                **base,
                "bet_type": "moneyline",
                "moneyline_team_1": to_decimal(match["moneyline"].get("team_1")),
                "moneyline_team_2": to_decimal(match["moneyline"].get("team_2")),
                "moneyline_draw": None,
                "spread_team_1": None,
                "spread_team_2": None,
                "spread_value": None,
                "total_points": None,
                "over_odds": None,
                "under_odds": None,
            })

        # -------------------
        # SPREAD
        # -------------------
        if "spread" in match:
            spread_row = build_spread_odd_row(base, match["spread"])
            if spread_row:
                rows.append(spread_row)

        # -------------------
        # TOTAL
        # -------------------
        if "total" in match:
            total = match["total"]
            rows.append({
                **base,
                "bet_type": "total",
                "moneyline_team_1": None,
                "moneyline_team_2": None,
                "moneyline_draw": None,
                "spread_team_1": None,
                "spread_team_2": None,
                "spread_value": None,
                "total_points": to_decimal(total.get("over_total")),
                "over_odds": to_decimal(total.get("over_odds")),
                "under_odds": to_decimal(total.get("under_odds")),
            })

    return rows



