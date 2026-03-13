import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN")
RIOT_API_KEY: str = os.getenv("RIOT_API_KEY")
DB_PATH: str = "data/riot_accounts.db"
