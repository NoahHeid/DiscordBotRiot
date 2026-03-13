import os
import sqlite3

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

    con.execute("""
        CREATE TABLE IF NOT EXISTS rank_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id  TEXT NOT NULL,
            rank        TEXT NOT NULL,
            checked_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
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


def get_all_accounts() -> list[tuple[str, str, str, str | None, str | None, str | None]]:
    con = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT discord_id, riot_name, riot_tag, guild_id, channel_id, preferred_name FROM riot_accounts"
    ).fetchall()
    con.close()
    return rows


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


def add_rank_snapshot(discord_id: str, rank: str) -> None:
    con = sqlite3.connect(DB_PATH)
    con.execute(
        "INSERT INTO rank_history (discord_id, rank) VALUES (?, ?)",
        (discord_id, rank),
    )
    con.commit()
    con.close()


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
