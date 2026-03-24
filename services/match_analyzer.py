"""
Match data extraction and analysis. Parses participant stats from match data.
"""
import logging


logger = logging.getLogger(__name__)


def find_participant_by_puuid(match_data: dict, player_puuid: str) -> dict | None:
    """
    Find participant entry in match data by PUUID.
    
    Args:
        match_data: Full match dict from Riot API
        player_puuid: Target player's PUUID
    
    Returns:
        Participant dict or None if player not found
    """
    if match_data is None:
        return None
    
    info = match_data.get("info", {})
    participants = info.get("participants", [])
    
    for participant in participants:
        if participant.get("puuid") == player_puuid:
            return participant
    
    return None


def extract_player_stats(match_data: dict, player_puuid: str) -> dict | None:
    """
    Extract relevant statistics for a player from match data.
    
    Args:
        match_data: Full match dict from Riot API
        player_puuid: Target player's PUUID
    
    Returns:
        Dict with keys:
        - championName (str)
        - individualPosition (str)
        - kills (int)
        - deaths (int)
        - assists (int)
        - totalMinionsKilled (int)
        Or None if player not found in match
    """
    participant = find_participant_by_puuid(match_data, player_puuid)
    if participant is None:
        return None
    
    return {
        "championName": participant.get("championName", "Unknown"),
        "individualPosition": participant.get("individualPosition", "Unknown"),
        "kills": int(participant.get("kills", 0)),
        "deaths": int(participant.get("deaths", 0)),
        "assists": int(participant.get("assists", 0)),
        "totalMinionsKilled": int(participant.get("totalMinionsKilled", 0)),
    }
