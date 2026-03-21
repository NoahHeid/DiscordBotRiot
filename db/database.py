import os
import sqlite3
from datetime import datetime, timezone

from config import DB_PATH


def _ensure_column(con: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row[1] for row in con.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS riot_accounts (
            discord_id  TEXT PRIMARY KEY,
            riot_name   TEXT NOT NULL,
            riot_tag    TEXT NOT NULL,
            guild_id    TEXT,
            channel_id  TEXT,
            preferred_name TEXT
        )
    """)

    _ensure_column(con, "riot_accounts", "guild_id", "guild_id TEXT")
    _ensure_column(con, "riot_accounts", "channel_id", "channel_id TEXT")
    _ensure_column(con, "riot_accounts", "preferred_name", "preferred_name TEXT")
    _ensure_column(con, "riot_accounts", "show_rank", "show_rank INTEGER NOT NULL DEFAULT 1")

    con.execute("""
        CREATE TABLE IF NOT EXISTS rank_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id  TEXT NOT NULL,
            rank        TEXT NOT NULL,
            checked_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    _ensure_column(con, "rank_history", "solo_change_match_id", "solo_change_match_id TEXT")
    _ensure_column(con, "rank_history", "flex_change_match_id", "flex_change_match_id TEXT")
    con.execute("""
        CREATE INDEX IF NOT EXISTS idx_rank_history_discord_time
        ON rank_history(discord_id, checked_at)
    """)

    con.commit()
    con.close()


def upsert_account(
    discord_id: str,
    riot_name: str,
    riot_tag: str,
    guild_id: str | None = None,
    channel_id: str | None = None,
) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO riot_accounts (discord_id, riot_name, riot_tag, guild_id, channel_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(discord_id) DO UPDATE SET
            riot_name = excluded.riot_name,
            riot_tag  = excluded.riot_tag,
            guild_id  = COALESCE(excluded.guild_id, riot_accounts.guild_id),
            channel_id = COALESCE(excluded.channel_id, riot_accounts.channel_id)
    """, (discord_id, riot_name, riot_tag, guild_id, channel_id))
    con.commit()
    con.close()


def get_account(discord_id: str) -> tuple[str, str] | None:
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT riot_name, riot_tag FROM riot_accounts WHERE discord_id = ?",
        (discord_id,)
    ).fetchone()
    con.close()
    return row


def get_all_accounts() -> list[tuple[str, str, str, str | None, str | None, str | None, int]]:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT discord_id, riot_name, riot_tag, guild_id, channel_id, preferred_name, COALESCE(show_rank, 1) FROM riot_accounts"
    ).fetchall()
    con.close()
    return rows


def get_show_rank(discord_id: str) -> bool | None:
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT COALESCE(show_rank, 1) FROM riot_accounts WHERE discord_id = ?",
        (discord_id,),
    ).fetchone()
    con.close()

    if row is None:
        return None

    return bool(row[0])


def toggle_show_rank(discord_id: str) -> bool | None:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        """
        UPDATE riot_accounts
        SET show_rank = CASE COALESCE(show_rank, 1) WHEN 1 THEN 0 ELSE 1 END
        WHERE discord_id = ?
        """,
        (discord_id,),
    )

    if cur.rowcount <= 0:
        con.close()
        return None

    row = con.execute(
        "SELECT COALESCE(show_rank, 1) FROM riot_accounts WHERE discord_id = ?",
        (discord_id,),
    ).fetchone()
    con.commit()
    con.close()

    if row is None:
        return None

    return bool(row[0])


def set_preferred_name(discord_id: str, preferred_name: str) -> bool:
    con = sqlite3.connect(DB_PATH)
    cur = con.execute(
        "UPDATE riot_accounts SET preferred_name = ? WHERE discord_id = ?",
        (preferred_name, discord_id),
    )
    con.commit()
    changed = cur.rowcount > 0
    con.close()
    return changed


def get_preferred_name(discord_id: str) -> str | None:
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT preferred_name FROM riot_accounts WHERE discord_id = ?",
        (discord_id,),
    ).fetchone()
    con.close()

    if row is None:
        return None

    return row[0]


def get_latest_rank(discord_id: str) -> str | None:
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        """
        SELECT rank
        FROM rank_history
        WHERE discord_id = ?
        ORDER BY checked_at DESC, id DESC
        LIMIT 1
        """,
        (discord_id,),
    ).fetchone()
    con.close()

    if row is None:
        return None

    return row[0]


def add_rank_snapshot(
    discord_id: str,
    rank: str,
    solo_change_match_id: str | None = None,
    flex_change_match_id: str | None = None,
) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        INSERT INTO rank_history (discord_id, rank, solo_change_match_id, flex_change_match_id)
        VALUES (?, ?, ?, ?)
        """,
        (discord_id, rank, solo_change_match_id, flex_change_match_id),
    )
    con.commit()
    con.close()


def _split_rank(rank: str) -> tuple[str, str]:
    parts = [part.strip() for part in rank.split("/", 1)]
    if len(parts) == 1:
        value = parts[0]
        return value, value
    return parts[0], parts[1]


def _parse_checked_at(checked_at: str) -> datetime:
    return datetime.strptime(checked_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def get_queue_tenure_start(discord_id: str, queue_key: str) -> datetime | None:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        """
        SELECT rank, checked_at
        FROM rank_history
        WHERE discord_id = ?
        ORDER BY checked_at ASC, id ASC
        """,
        (discord_id,),
    ).fetchall()
    con.close()

    if not rows:
        return None

    index = 0 if queue_key == "solo" else 1

    first_rank = _split_rank(str(rows[0][0]))
    previous_queue_rank = first_rank[index]
    first_checked_at = _parse_checked_at(str(rows[0][1]))
    last_queue_change_at: datetime | None = None

    for rank, checked_at in rows[1:]:
        queue_rank = _split_rank(str(rank))[index]
        if queue_rank != previous_queue_rank:
            last_queue_change_at = _parse_checked_at(str(checked_at))
        previous_queue_rank = queue_rank

    return last_queue_change_at or first_checked_at


def get_rank_history(discord_id: str, limit: int = 10) -> list[tuple[str, str]]:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        """
        SELECT rank, checked_at
        FROM rank_history
        WHERE discord_id = ?
        ORDER BY checked_at DESC, id DESC
        LIMIT ?
        """,
        (discord_id, limit),
    ).fetchall()
    con.close()
    return rows


def get_rank_changes(discord_id: str, limit: int = 10) -> list[tuple[str, str, str, str | None, str | None]]:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        """
        SELECT rank, checked_at, solo_change_match_id, flex_change_match_id
        FROM rank_history
        WHERE discord_id = ?
        ORDER BY checked_at ASC, id ASC
        """,
        (discord_id,),
    ).fetchall()
    con.close()

    changes: list[tuple[str, str, str, str | None, str | None]] = []
    previous_rank: str | None = None

    for rank, checked_at, solo_match_id, flex_match_id in rows:
        if previous_rank is None:
            previous_rank = rank
            continue

        if rank != previous_rank:
            changes.append((previous_rank, rank, checked_at, solo_match_id, flex_match_id))

        previous_rank = rank

    return list(reversed(changes[-limit:]))
