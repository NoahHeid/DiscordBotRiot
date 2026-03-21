import logging
import asyncio
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

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

_CONTINENT_ROUTING: dict[Continent, str] = {
    Continent.AMERICAS: "americas",
    Continent.EUROPE: "europe",
    Continent.ASIA: "asia",
    Continent.SEA: "sea",
}

_QUEUE_ID_BY_KEY: dict[str, int] = {
    "solo": 420,
    "flex": 440,
}

_TIER_SHORT = {
    "IRON": "I",
    "BRONZE": "B",
    "SILVER": "S",
    "GOLD": "G",
    "PLATINUM": "P",
    "EMERALD": "E",
    "DIAMOND": "D",
    "MASTER": "M",
    "GRANDMASTER": "GM",
    "CHALLENGER": "C",
}

_LONG_TIER_TO_SHORT = {
    "IRON": "I",
    "BRONZE": "B",
    "SILVER": "S",
    "GOLD": "G",
    "PLAT": "P",
    "PLATINUM": "P",
    "EMERALD": "E",
    "DIAMOND": "D",
    "MASTER": "M",
    "GRANDMASTER": "GM",
    "CHALLENGER": "C",
}

_ROMAN_TO_NUM = {"IV": "4", "III": "3", "II": "2", "I": "1"}
_DIVISION_TO_NUM = {"4": "4", "3": "3", "2": "2", "1": "1", **_ROMAN_TO_NUM}

_UNRANKED = "NA"

_TIER_SCORE: dict[str, int] = {
    short: (i + 1) * 10
    for i, short in enumerate(_TIER_SHORT.values())
}
_DIVISION_SCORE: dict[str, int] = {"4": 1, "3": 2, "2": 3, "1": 4}


def normalize_queue_rank(rank_str: str) -> str:
    """Normalize queue rank to short format (e.g. 'Gold II 🥇' -> 'G2')."""
    cleaned = rank_str.strip().upper()
    if not cleaned:
        return _UNRANKED

    if cleaned in {"NA", "N/A", "N/A ⚪"} or cleaned.startswith("N/A"):
        return _UNRANKED

    parts = cleaned.split()
    if parts and parts[0] in _LONG_TIER_TO_SHORT:
        tier = _LONG_TIER_TO_SHORT[parts[0]]
        if len(parts) > 1:
            division = _DIVISION_TO_NUM.get(parts[1], "")
            return f"{tier}{division}" if division else tier
        return tier

    tier = ""
    division = ""
    if cleaned.startswith("GM"):
        tier = "GM"
        division = cleaned[2:]
    else:
        tier = cleaned[:1]
        division = cleaned[1:]

    if tier not in _TIER_SCORE:
        tier = _LONG_TIER_TO_SHORT.get(tier, tier)

    if tier not in _TIER_SCORE:
        return _UNRANKED

    division = _DIVISION_TO_NUM.get(division, "")
    return f"{tier}{division}" if division else tier


def normalize_combined_rank(rank_str: str) -> str:
    """Normalize combined rank string to 'X / Y' short format."""
    parts = [part.strip() for part in rank_str.split("/", 1)]
    if len(parts) == 1:
        return normalize_queue_rank(parts[0])

    solo_rank = normalize_queue_rank(parts[0])
    flex_rank = normalize_queue_rank(parts[1])
    return f"{solo_rank} / {flex_rank}"


def rank_score(rank_str: str) -> int:
    """Convert rank string like 'G2' or 'GM' to a numeric score."""
    normalized = normalize_queue_rank(rank_str)
    if normalized == _UNRANKED:
        return 0

    tier_key = ""
    division_key = ""

    if normalized.startswith("GM"):
        tier_key = "GM"
        division_key = normalized[2:]
    else:
        tier_key = normalized[0]
        division_key = normalized[1:]

    tier_val = _TIER_SCORE.get(tier_key, 0)
    div_val = _DIVISION_SCORE.get(division_key, 0) if division_key else 0
    return tier_val + div_val


def _format_entry_rank(entry: dict) -> str:
    tier_key = str(entry.get("tier", "")).upper()
    tier = _TIER_SHORT.get(tier_key)
    if tier is None:
        tier = tier_key[:1] if tier_key else "?"

    division = entry.get("rank")

    if division:
        division_str = str(division).upper()
        division_num = _ROMAN_TO_NUM.get(division_str, division_str)
        return f"{tier}{division_num}"

    return tier


async def fetch_rank(riot_name: str, riot_tag: str) -> str | None:
    rank, _puuid, _routing = await fetch_rank_with_context(riot_name, riot_tag)
    return rank


async def fetch_rank_with_context(riot_name: str, riot_tag: str) -> tuple[str | None, str | None, str | None]:
    continent, region = _TAG_TO_REGION.get(riot_tag.upper(), _DEFAULT_REGION)
    routing = _CONTINENT_ROUTING.get(continent, "europe")

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
        return f"{solo_rank} / {flex_rank}", puuid, routing

    except exceptions.DataNotFound:
        logger.warning("Riot account not found for %s#%s", riot_name, riot_tag)
        return None, None, None
    except Exception:
        logger.exception("Failed to fetch rank for %s#%s", riot_name, riot_tag)
        return None, None, None


def _queue_id_from_key(queue_key: str) -> int | None:
    return _QUEUE_ID_BY_KEY.get(queue_key)


def _fetch_match_ids_page(
    routing: str,
    puuid: str,
    queue_id: int,
    start_time_unix: int,
    start: int,
    count: int,
) -> list[str]:
    params = urllib.parse.urlencode(
        {
            "type": "ranked",
            "queue": queue_id,
            "startTime": max(0, start_time_unix),
            "start": start,
            "count": count,
        }
    )
    url = f"https://{routing}.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?{params}"
    request = urllib.request.Request(
        url,
        headers={"X-Riot-Token": RIOT_API_KEY},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read().decode("utf-8")

    import json

    parsed = json.loads(payload)
    if not isinstance(parsed, list):
        return []
    return [str(match_id) for match_id in parsed]


async def fetch_ranked_match_stats_since(
    puuid: str,
    routing: str,
    queue_key: str,
    since_utc: datetime,
) -> tuple[int, str | None]:
    queue_id = _queue_id_from_key(queue_key)
    if queue_id is None:
        return 0, None

    if since_utc.tzinfo is None:
        since_utc = since_utc.replace(tzinfo=timezone.utc)
    since_unix = int(since_utc.timestamp())

    all_match_ids: list[str] = []
    page_start = 0
    page_size = 100
    max_matches = 1000

    try:
        while len(all_match_ids) < max_matches:
            page = await asyncio.to_thread(
                _fetch_match_ids_page,
                routing,
                puuid,
                queue_id,
                since_unix,
                page_start,
                page_size,
            )
            if not page:
                break

            all_match_ids.extend(page)
            if len(page) < page_size:
                break

            page_start += page_size

    except urllib.error.HTTPError:
        logger.exception("Riot match endpoint HTTP error for puuid %s (queue=%s)", puuid, queue_key)
        return 0, None
    except urllib.error.URLError:
        logger.exception("Riot match endpoint URL error for puuid %s (queue=%s)", puuid, queue_key)
        return 0, None
    except Exception:
        logger.exception("Failed to fetch ranked match stats for puuid %s (queue=%s)", puuid, queue_key)
        return 0, None

    latest_match_id = all_match_ids[0] if all_match_ids else None
    return len(all_match_ids), latest_match_id
