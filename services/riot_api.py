import logging

from pyke import Continent, Region, Pyke, exceptions

from config import RIOT_API_KEY


logger = logging.getLogger(__name__)

# Maps riot_tag → (Continent, Region) — extend as needed
_TAG_TO_REGION: dict[str, tuple[Continent, Region]] = {
    "EUW":  (Continent.EUROPE, Region.EUW),
    "EUNE": (Continent.EUROPE, Region.EUNE),
    "NA":   (Continent.AMERICAS, Region.NA),
    "BR":   (Continent.AMERICAS, Region.BR),
    "LAN":  (Continent.AMERICAS, Region.LAN),
    "LAS":  (Continent.AMERICAS, Region.LAS),
    "KR":   (Continent.ASIA, Region.KR),
    "JP":   (Continent.ASIA, Region.JP),
    "OCE":  (Continent.SEA, Region.OCE),
    "TR":   (Continent.EUROPE, Region.TR),
    "RU":   (Continent.EUROPE, Region.RU),
}

_DEFAULT_REGION = (Continent.EUROPE, Region.EUW)

_TIER_DISPLAY = {
    "IRON": "Iron", 
    "BRONZE": "Bronze", 
    "SILVER": "Silver",
    "GOLD": "Gold", 
    "PLATINUM": "Plat", 
    "EMERALD": "Emerald",
    "DIAMOND": "Diamond", 
    "MASTER": "Master",
    "GRANDMASTER": "GM", 
    "CHALLENGER": "Challenger",
}

_TIER_EMOJI = {
    "IRON": "⚙️",
    "BRONZE": "🥉",
    "SILVER": "🥈",
    "GOLD": "🥇",
    "PLATINUM": "💠",
    "EMERALD": "💚",
    "DIAMOND": "💎",
    "MASTER": "👑",
    "GRANDMASTER": "🔥",
    "CHALLENGER": "🏆",
}

_UNRANKED = "N/A ⚪"

_TIER_SCORE: dict[str, int] = {
    display: (i + 1) * 10
    for i, display in enumerate(_TIER_DISPLAY.values())
}
_DIVISION_SCORE: dict[str, int] = {"IV": 1, "III": 2, "II": 3, "I": 4}


def rank_score(rank_str: str) -> int:
    """Convert a display rank string like 'Gold II 🥇' to a numeric score."""
    parts = rank_str.split()
    if not parts or parts[0] == "N/A":
        return 0
    tier_val = _TIER_SCORE.get(parts[0], 0)
    div_val = _DIVISION_SCORE.get(parts[1], 0) if len(parts) > 1 else 0
    return tier_val + div_val


def _format_entry_rank(entry: dict) -> str:
    tier_key = entry["tier"]
    tier = _TIER_DISPLAY.get(tier_key, tier_key.capitalize())
    division = entry.get("rank")
    emoji = _TIER_EMOJI.get(tier_key, "🏅")

    if division:
        return f"{tier} {division} {emoji}"

    return f"{tier} {emoji}"


async def fetch_rank(riot_name: str, riot_tag: str) -> str | None:
    continent, region = _TAG_TO_REGION.get(riot_tag.upper(), _DEFAULT_REGION)

    try:
        async with Pyke(RIOT_API_KEY, timeout=30) as api:
            account = await api.account.by_riot_id(continent, riot_name, riot_tag)
            puuid: str = account["puuid"]

            entries: list[dict] = await api.league.by_puuid(region, puuid)

        queue_to_rank: dict[str, str] = {}
        for entry in entries:
            queue_type = entry.get("queueType")
            if queue_type in {"RANKED_SOLO_5x5", "RANKED_FLEX_SR"}:
                queue_to_rank[queue_type] = _format_entry_rank(entry)

        solo_rank = queue_to_rank.get("RANKED_SOLO_5x5", _UNRANKED)
        flex_rank = queue_to_rank.get("RANKED_FLEX_SR", _UNRANKED)
        return f"{solo_rank} / {flex_rank}"

    except exceptions.DataNotFound:
        logger.warning("Riot account not found for %s#%s", riot_name, riot_tag)
        return None
    except Exception:
        logger.exception("Failed to fetch rank for %s#%s", riot_name, riot_tag)
        return None
