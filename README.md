# Ars Victoriae Discord Bot

Discord bot for linking Riot accounts to Discord users, showing Solo/Duo + Flex rank in nicknames, storing rank history, and notifying users about rank changes.

## Features

- Link Riot account to Discord user: `!addRiot --name [NAME] --tag [TAG]`
- Set custom display name (case-sensitive): `!setName --name [NAME]`
- Show linked Riot account: `!myRiot`
- Show recent rank snapshots with timestamp: `!rankHistory`
- Every 5 minutes:
  - Fetches Riot rank data
  - Updates nickname format to: `Name [SoloRank / FlexRank]`
  - Stores rank snapshots in SQLite with timestamp
  - Sends channel message when rank changed

## Rank Display Format

Nickname format:

`PreferredName [N/A ⚪ / Plat III 💠]`

- Left side = Solo/Duo rank (`RANKED_SOLO_5x5`)
- Right side = Flex rank (`RANKED_FLEX_SR`)
- Missing queue = `N/A ⚪`

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
│   └── riot_api.py
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

Example:

`!addRiot --name Aaron --tag EUW`

### `!setName --name [NAME]`

Sets preferred display name for nickname updates (case-sensitive).

Example:

`!setName --name Aaron`

### `!myRiot`

Shows linked Riot account.

### `!rankHistory`

Shows latest rank snapshots with timestamp.

## Database Tables

### `riot_accounts`

- `discord_id` (PK)
- `riot_name`
- `riot_tag`
- `guild_id`
- `channel_id`
- `preferred_name`

### `rank_history`

- `id` (PK)
- `discord_id`
- `rank`
- `checked_at` (timestamp)

## Permissions Needed in Discord

- Read messages / message content intent enabled
- Change Nickname (`Manage Nicknames`)
- Send Messages

## GitHub Actions: Docker Build + GHCR Publish on `master`

Workflow file:

- `.github/workflows/docker-build.yml`

It runs on every push to `master` and performs a Docker build using the project `Dockerfile`.
It then pushes the built image to GitHub Container Registry (GHCR).

## Notes

- Keep `.env` private and never commit real tokens.
- Riot API key should be rotated if it was exposed publicly.
