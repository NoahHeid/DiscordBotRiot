"""
Match fetching and analysis service. Handles retrieving match data from Riot API
and counting ranked games within specific time ranges.
"""
import logging
from datetime import datetime, timezone

from pyke import Continent, Pyke, exceptions

from config import RIOT_API_KEY


logger = logging.getLogger(__name__)

_QUEUE_ID_BY_KEY = {
    "solo": 420,
    "flex": 440,
}


async def fetch_latest_match_id(
    puuid: str,
    continent: Continent,
    queue_key: str = "solo",
) -> str | None:
    """
    Fetch the single most recent ranked match ID for a specific queue type.
    
    Args:
        puuid: Player's UUID
        continent: Continent routing
        queue_key: "solo" (420) or "flex" (440)
    
    Returns:
        Match ID string or None if no matches found or API error
    """
    try:
        async with Pyke(RIOT_API_KEY, timeout=30) as api:
            match_ids = await api.match.match_ids_by_puuid(
                continent=continent,
                puuid=puuid,
                queue=_QUEUE_ID_BY_KEY.get(queue_key),
                count=1,
            )
            return match_ids[0] if match_ids else None
    except exceptions.DataNotFound:
        logger.debug(f"No matches found for PUUID {puuid}")
        return None
    except Exception as e:
        logger.exception(f"Failed to fetch latest match for PUUID {puuid}: {e}")
        return None


async def fetch_match_details(match_id: str, continent: Continent) -> dict | None:
    """
    Fetch full match details from Riot API.
    
    Args:
        match_id: Match ID from Riot API
        continent: Continent routing
    
    Returns:
        Match dict with full participant data or None if API error
    """
    try:
        async with Pyke(RIOT_API_KEY, timeout=30) as api:
            match = await api.match.by_match_id(continent=continent, match_id=match_id)
            return match
    except exceptions.DataNotFound:
        logger.warning(f"Match {match_id} not found")
        return None
    except Exception as e:
        logger.exception(f"Failed to fetch match {match_id}: {e}")
        return None


async def count_games_since(
    puuid: str,
    continent: Continent,
    queue_key: str,
    since_utc: datetime,
) -> int:
    """
    Count ranked games played in a specific queue since a given timestamp.
    
    Args:
        puuid: Player's UUID
        continent: Continent routing  
        queue_key: "solo" (420) or "flex" (440)
        since_utc: Only count games played after this datetime (UTC)
    
    Returns:
        Number of ranked games in the queue since the timestamp
    """
    queue_id = _QUEUE_ID_BY_KEY.get(queue_key)
    if queue_id is None:
        logger.error(f"Unknown queue key: {queue_key}")
        return 0
    
    try:
        async with Pyke(RIOT_API_KEY, timeout=30) as api:
            since_timestamp_ms = int(since_utc.timestamp() * 1000)
            
            match_ids = await api.match.match_ids_by_puuid(
                continent=continent,
                puuid=puuid,
                queue=queue_id,
                count=100,  # Fetch up to 100 match IDs to filter by date
            )
            
            if not match_ids:
                return 0
            
            # Fetch details of first few matches to check timestamps
            count = 0
            for i, match_id in enumerate(match_ids):
                try:
                    match_data = await api.match.by_match_id(continent=continent, match_id=match_id)
                    if match_data is None:
                        continue
                    
                    game_creation = match_data.get("info", {}).get("gameCreation", 0)
                    if game_creation >= since_timestamp_ms:
                        count += 1
                    else:
                        # Matches are in reverse chronological order, so stop here
                        break
                except Exception as e:
                    logger.debug(f"Skipping match {match_id}: {e}")
                    continue
            
            return count
    except exceptions.DataNotFound:
        logger.debug(f"No matches found for PUUID {puuid}")
        return 0
    except Exception as e:
        logger.exception(f"Failed to count games for PUUID {puuid} since {since_utc}: {e}")
        return 0
