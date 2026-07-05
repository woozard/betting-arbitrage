"""Cross-book moneyline arb structure validation (favorite vs underdog on each leg)."""

from __future__ import annotations

from utils.helpers import american_odds_to_int, is_plausible_moneyline_pair


def is_valid_moneyline_arb_legs(team_1_odds, team_2_odds) -> bool:
    """Arb legs must be opposite teams with one favorite (-) and one underdog (+)."""
    return is_plausible_moneyline_pair(team_1_odds, team_2_odds)


def validate_moneyline_arb_payload(arb: dict) -> str | None:
    """Return a skip reason when cached arb violates ML structure; else None."""
    if (arb.get("bet_type") or "moneyline").lower() != "moneyline":
        return None
    t1 = arb.get("team_1_odds")
    t2 = arb.get("team_2_odds")
    if is_valid_moneyline_arb_legs(t1, t2):
        return None
    return (
        "invalid moneyline arb — both legs same side "
        f"(team_1={t1}, team_2={t2}); need one favorite and one underdog"
    )


def validate_cross_leg_moneyline_signs(
    cache, arb: dict, bookmaker: str, leg: dict | None = None
) -> str | None:
    """
    When the other book already confirmed a leg, block if this leg would be same-side exposure.
    """
    if (arb.get("bet_type") or "moneyline").lower() != "moneyline":
        return None

    book_1 = (arb.get("team_1_bookmaker") or "").strip().lower()
    book_2 = (arb.get("team_2_bookmaker") or "").strip().lower()
    bm = (bookmaker or "").strip().lower()
    other_book = book_2 if bm == book_1 else book_1
    if not other_book or not cache.is_arb_leg_placed(arb, other_book):
        return None

    this_leg = leg
    if this_leg is None:
        from utils.arb_placement import arb_leg_for_book

        this_leg = arb_leg_for_book(arb, bookmaker)
    if not this_leg:
        return None

    other_odds = _confirmed_other_leg_odds(cache, arb, other_book)
    if other_odds is None:
        return None

    this_odds = this_leg.get("odds")
    if is_valid_moneyline_arb_legs(this_odds, other_odds):
        return None

    return (
        f"same-side exposure vs confirmed {other_book} leg "
        f"({this_odds} vs {other_odds}); arb requires favorite on one book, underdog on the other"
    )


def _confirmed_other_leg_odds(cache, arb: dict, other_book: str):
    pair_key = cache.arb_pair_key_from_arb(arb)
    summary = cache.redis.get(f"arb_real_bets_summary:{pair_key}") or {}
    other_bm = (other_book or "").strip().lower()
    side = (
        "leg1"
        if other_bm == (arb.get("team_1_bookmaker") or "").strip().lower()
        else "leg2"
    )
    leg_data = summary.get(side) or {}
    if leg_data.get("placed") and leg_data.get("odds") is not None:
        return leg_data.get("odds")

    from utils.arb_placement import arb_leg_for_book

    other_leg = arb_leg_for_book(arb, other_book)
    return other_leg.get("odds") if other_leg else None


def format_moneyline_sign(odds) -> str:
    try:
        val = american_odds_to_int(odds)
    except (TypeError, ValueError):
        return str(odds)
    return "underdog (+)" if val > 0 else "favorite (-)"
