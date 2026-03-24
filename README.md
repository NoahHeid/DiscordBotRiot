# Ars Victoriae Discord Bot

Discord bot for linking Riot accounts to Discord users, showing Solo/Duo + Flex rank in nicknames, storing rank history, and notifying users about rank changes including match context.

## Features

- Link Riot account to Discord user: `!addRiot --name [NAME] --tag [TAG]`
  - Automatically creates the `@BotNotifier` role on the server (if it doesn't exist yet)
  - Automatically adds the user to `@BotNotifier`
  - Fetches and stores the player's `puuid` (if Riot API lookup succeeds)
- Set custom display name (case-sensitive): `!setName --name [NAME]`
- Toggle rank display next to own name: `!toggleShowRank`
- Show linked Riot account: `!myRiot`
- Show rank change history with timestamp: `!rankHistory`
- Every 5 minutes:
  - Fetches Riot rank data for all linked accounts
  - Backfills missing `puuid` values for already linked users
  - Updates nickname format to: `Name [SoloRank / FlexRank]`
    - If `!toggleShowRank` is disabled by a user, only the name is shown (without rank)
  - Saves a rank snapshot to SQLite **only when the rank has changed**
  - On rank change, stores the latest matching queue match id (Solo/Flex) in DB
  - Calculates queue-specific tenure data (`days` + `games since last queue change`)
  - Ensures a dedicated rank updates channel exists (`rank-updates-📈`, fallback: `rank-updates`)
  - Posts rank change notifications (uprank/downrank) in that rank updates channel with champion, role, K/D/A, CS and game link (see Rank Change Notifications below)

## Rank Display Format

Nickname format:

`PreferredName [NA / P2]`

- Left side = Solo/Duo rank (`RANKED_SOLO_5x5`)
- Right side = Flex rank (`RANKED_FLEX_SR`)
- Missing queue = `NA`

## Tech Stack

- Python 3.11+
- `discord.py`
- `pyke-lol`
- `python-dotenv`
- SQLite
- Docker
- GitHub Actions

## Project Structure

```
.
├── main.py
├── config.py
├── cogs/
│   └── riot.py
├── db/
│   └── database.py
├── services/
│   ├── riot_api.py
│   ├── match_service.py
│   └── match_analyzer.py
├── data/
│   └── riot_accounts.db
├── Dockerfile
└── .github/workflows/docker-build.yml
```

## Environment Variables

Create a `.env` file in project root:

```env
DISCORD_TOKEN=your_discord_bot_token
RIOT_API_KEY=your_riot_api_key
```

## Local Setup

1. Create venv

```powershell
python -m venv venv
```

2. Activate venv (PowerShell)

```powershell
.\venv\Scripts\Activate.ps1
```

3. Install dependencies

```powershell
pip install -r requirements.txt
```

4. Run bot

```powershell
python .\main.py
```

## Docker

Build image:

```powershell
docker build -t ars-victoriae-discord-bot .
```

Run container (with persistent DB volume and env file):

```powershell
docker run --name ars-victoriae-discord-bot --env-file .env -v riot_data:/app/data ars-victoriae-discord-bot
```

### Use image from GHCR

On every push to `master`, GitHub Actions publishes an image to:

`ghcr.io/<owner>/<repo>`

Published tags:

- `latest`
- `<full-commit-sha>`

Pull latest image:

```powershell
docker pull ghcr.io/<owner>/<repo>:latest
```

Run latest image:

```powershell
docker run --name ars-victoriae-discord-bot --env-file .env -v riot_data:/app/data ghcr.io/<owner>/<repo>:latest
```

## Bot Commands

### `!addRiot --name [NAME] --tag [TAG]`

Links Riot account to Discord user.

- Automatically creates the `@BotNotifier` role on the server if it doesn't exist.
- Automatically assigns the `@BotNotifier` role to the user.
- Fetches and stores `puuid` for the linked account (if available).

Example:

`!addRiot --name Aaron --tag EUW`

### `!setName --name [NAME]`

Sets preferred display name for nickname updates (case-sensitive).

Example:

`!setName --name Aaron`

### `!myRiot`

Shows linked Riot account.

### `!rankHistory`

Shows rank change history (only entries where the rank actually changed) with timestamps and stored Solo/Flex match references (if available).

### `!toggleShowRank`

Turns rank display next to your own name on/off.

### `!help`

Shows all available commands with short explanations and examples.

## Database Tables

### `riot_accounts`

- `discord_id` (PK)
- `riot_name`
- `riot_tag`
- `guild_id`
- `channel_id`
- `preferred_name`
- `show_rank` (0/1)
- `puuid`

### `rank_history`

- `id` (PK)
- `discord_id`
- `rank`
- `checked_at` (timestamp)
- `solo_change_match_id`
- `flex_change_match_id`
- `games_since_last_change` (currently reserved for future use)

## Rank Change Notifications

When a rank changes, the bot pings `@BotNotifier` in the dedicated rank updates channel (`rank-updates-📈`, fallback: `rank-updates`) with a message depending on the direction.

The message contains:

- Queue (`Solo/Duo` or `Flex`)
- New and previous queue rank
- Time and games spent in previous queue rank
- Champion, lane/position, K/D/A and CS from the fetched rank-change match (if match data is available)
- League of Graphs game link (`https://www.leagueofgraphs.com/de/match/euw/{MATCH_ID}`)

**Uprank:**

> `@BotNotifier Wow! @Person ist in Solo/Duo in G2 aufgestiegen! Er war viel zu krass mit Warwick in der TOP Lane und konnte mit 18/4/3 und 197 CS komplett carrien. Er verbrachte 2 Tage und 9 Games in G3.`
>
> `https://www.leagueofgraphs.com/de/match/euw/EUW1_7796905964`

**Downrank:**

> `@BotNotifier Schade! @Person ist in Flex in S1 abgestiegen. Seine Skills mit Nami in der UTILITY Lane haben mit 1/6/8 und 36 CS nicht gereicht um die Klasse zu halten. Er verbrachte 5 Tage und 14 Games in S2.`
>
> `https://www.leagueofgraphs.com/de/match/euw/EUW1_7796905964`

- The bot detects **per queue** (Solo/Duo and/or Flex) whether a change happened.
- Up/downrank is determined by comparing numeric rank scores.
- Game counting is queue-specific (Solo queue id 420, Flex queue id 440).
- The `@BotNotifier` role is created automatically with **mentionable** set to `true`.

## Permissions Needed in Discord

- Read messages / message content intent enabled
- Change Nickname (`Manage Nicknames`)
- Send Messages
- Manage Channels (needed to auto-create `rank-updates-📈` / `rank-updates`)
- Manage Roles (needed to create and assign `@BotNotifier`)

## GitHub Actions: Docker Build + GHCR Publish on `master`

Workflow file:

- `.github/workflows/docker-build.yml`

It runs on every push to `master` and performs a Docker build using the project `Dockerfile`.
It then pushes the built image to GitHub Container Registry (GHCR).

## Notes

- Keep `.env` private and never commit real tokens.
- Riot API key should be rotated if it was exposed publicly.
