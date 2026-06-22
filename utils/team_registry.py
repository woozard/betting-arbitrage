"""Canonical team identities for cross-book matchup matching.

Books use different labels for the same franchise (e.g. BetWar ``NY Yankees``
vs S411 ``New York Yankees``). All matching keys and ``teams_same`` checks should
go through :func:`canonical_team` so those rows land in the same matchup bucket.
"""

from __future__ import annotations

import re
from functools import lru_cache

# (canonical_slug, [aliases...]) — aliases are lowercase, punctuation-normalized.
_MLB_TEAMS: list[tuple[str, list[str]]] = [
    ("mlb_arizona_diamondbacks", ["arizona diamondbacks", "ari diamondbacks", "diamondbacks"]),
    ("mlb_athletics", ["athletics", "oakland athletics", "oak athletics"]),
    ("mlb_atlanta_braves", ["atlanta braves", "atl braves", "braves"]),
    ("mlb_baltimore_orioles", ["baltimore orioles", "bal orioles", "orioles"]),
    ("mlb_boston_red_sox", ["boston red sox", "bos red sox", "red sox"]),
    ("mlb_chicago_cubs", ["chicago cubs", "chi cubs", "cubs"]),
    ("mlb_chicago_white_sox", ["chicago white sox", "chi white sox", "white sox"]),
    ("mlb_cincinnati_reds", ["cincinnati reds", "cin reds", "reds"]),
    ("mlb_cleveland_guardians", ["cleveland guardians", "cle guardians", "guardians"]),
    ("mlb_colorado_rockies", ["colorado rockies", "col rockies", "rockies"]),
    ("mlb_detroit_tigers", ["detroit tigers", "det tigers", "tigers"]),
    ("mlb_houston_astros", ["houston astros", "hou astros", "astros"]),
    ("mlb_kansas_city_royals", ["kansas city royals", "kc royals", "royals"]),
    ("mlb_los_angeles_angels", ["los angeles angels", "la angels", "angels"]),
    ("mlb_los_angeles_dodgers", ["los angeles dodgers", "la dodgers", "dodgers"]),
    ("mlb_miami_marlins", ["miami marlins", "mia marlins", "marlins"]),
    ("mlb_milwaukee_brewers", ["milwaukee brewers", "mil brewers", "brewers"]),
    ("mlb_minnesota_twins", ["minnesota twins", "min twins", "twins"]),
    ("mlb_new_york_mets", ["new york mets", "ny mets", "mets"]),
    ("mlb_new_york_yankees", ["new york yankees", "ny yankees", "yankees"]),
    ("mlb_philadelphia_phillies", ["philadelphia phillies", "phi phillies", "phillies"]),
    ("mlb_pittsburgh_pirates", ["pittsburgh pirates", "pit pirates", "pirates"]),
    ("mlb_san_diego_padres", ["san diego padres", "sd padres", "padres"]),
    ("mlb_san_francisco_giants", ["san francisco giants", "sf giants", "giants"]),
    ("mlb_seattle_mariners", ["seattle mariners", "sea mariners", "mariners"]),
    ("mlb_st_louis_cardinals", [
        "st louis cardinals",
        "st. louis cardinals",
        "stl cardinals",
        "cardinals",
    ]),
    ("mlb_tampa_bay_rays", ["tampa bay rays", "tb rays", "rays"]),
    ("mlb_texas_rangers", ["texas rangers", "tex rangers", "rangers"]),
    ("mlb_toronto_blue_jays", ["toronto blue jays", "tor blue jays", "blue jays"]),
    ("mlb_washington_nationals", ["washington nationals", "was nationals", "nationals"]),
]

_CANONICAL_DISPLAY: dict[str, str] = {
    "mlb_arizona_diamondbacks": "Arizona Diamondbacks",
    "mlb_athletics": "Athletics",
    "mlb_atlanta_braves": "Atlanta Braves",
    "mlb_baltimore_orioles": "Baltimore Orioles",
    "mlb_boston_red_sox": "Boston Red Sox",
    "mlb_chicago_cubs": "Chicago Cubs",
    "mlb_chicago_white_sox": "Chicago White Sox",
    "mlb_cincinnati_reds": "Cincinnati Reds",
    "mlb_cleveland_guardians": "Cleveland Guardians",
    "mlb_colorado_rockies": "Colorado Rockies",
    "mlb_detroit_tigers": "Detroit Tigers",
    "mlb_houston_astros": "Houston Astros",
    "mlb_kansas_city_royals": "Kansas City Royals",
    "mlb_los_angeles_angels": "Los Angeles Angels",
    "mlb_los_angeles_dodgers": "Los Angeles Dodgers",
    "mlb_miami_marlins": "Miami Marlins",
    "mlb_milwaukee_brewers": "Milwaukee Brewers",
    "mlb_minnesota_twins": "Minnesota Twins",
    "mlb_new_york_mets": "New York Mets",
    "mlb_new_york_yankees": "New York Yankees",
    "mlb_philadelphia_phillies": "Philadelphia Phillies",
    "mlb_pittsburgh_pirates": "Pittsburgh Pirates",
    "mlb_san_diego_padres": "San Diego Padres",
    "mlb_san_francisco_giants": "San Francisco Giants",
    "mlb_seattle_mariners": "Seattle Mariners",
    "mlb_st_louis_cardinals": "St. Louis Cardinals",
    "mlb_tampa_bay_rays": "Tampa Bay Rays",
    "mlb_texas_rangers": "Texas Rangers",
    "mlb_toronto_blue_jays": "Toronto Blue Jays",
    "mlb_washington_nationals": "Washington Nationals",
}


def _alias_key(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"\bst\.\b", "st", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _name_variants(name: str) -> list[str]:
    raw = _alias_key(name)
    if not raw:
        return []
    variants = [raw]
    stripped = re.sub(r"^[a-z]{2,4}\s+", "", raw)
    if stripped and stripped not in variants:
        variants.append(stripped)
    return variants


def _build_lookup(teams: list[tuple[str, list[str]]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for canonical, aliases in teams:
        for alias in aliases:
            key = _alias_key(alias)
            if key in lookup and lookup[key] != canonical:
                raise ValueError(
                    f"Duplicate MLB alias {alias!r} "
                    f"({lookup[key]} vs {canonical})"
                )
            lookup[key] = canonical
    return lookup


@lru_cache(maxsize=1)
def mlb_alias_lookup() -> dict[str, str]:
    return _build_lookup(_MLB_TEAMS)


def canonical_team(
    name: str,
    *,
    sport: str = "baseball",
    league: str = "mlb",
) -> str:
    """Return a stable slug used for matchup keys and dedup."""
    if not name or not str(name).strip():
        return ""

    league_l = (league or "").strip().lower()
    sport_l = (sport or "").strip().lower()
    if sport_l in ("baseball", "mlb") or "mlb" in league_l:
        lookup = mlb_alias_lookup()
        for variant in _name_variants(name):
            hit = lookup.get(variant)
            if hit:
                return hit

    fallback = _name_variants(name)[-1] if _name_variants(name) else _alias_key(name)
    slug = re.sub(r"[^a-z0-9]+", "_", fallback).strip("_")
    return slug or "unknown"


def standard_team_name(
    name: str,
    *,
    sport: str = "baseball",
    league: str = "mlb",
) -> str:
    """Map a book-specific label to the preferred cross-book display name."""
    original = (name or "").strip()
    if not original:
        return original
    canon = canonical_team(original, sport=sport, league=league)
    return _CANONICAL_DISPLAY.get(canon, original)


def canonical_matchup_key(
    team_1: str,
    team_2: str,
    game_datetime=None,
    *,
    sport: str = "baseball",
    league: str = "mlb",
) -> tuple[tuple[str, str], str]:
    """Sorted canonical team pair + YYYY-MM-DD date for cross-book grouping."""
    dt = game_datetime or ""
    date_key = (dt[:10] if isinstance(dt, str) else str(dt)[:10]) if dt else ""
    pair = tuple(
        sorted(
            [
                canonical_team(team_1, sport=sport, league=league),
                canonical_team(team_2, sport=sport, league=league),
            ]
        )
    )
    return pair, date_key
