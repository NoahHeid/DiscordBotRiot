import asyncio
import argparse
import logging
import discord
from discord.ext import commands

from config import DISCORD_TOKEN
from db.database import init_db


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def _build_bot() -> commands.Bot:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True

    bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

    @bot.event
    async def on_ready() -> None:
        logger.info("Logged on as %s", bot.user)

    @bot.event
    async def on_error(event_method: str, *args, **kwargs) -> None:
        logger.exception("Unhandled Discord event error in %s", event_method)

    @bot.event
    async def on_command_error(ctx: commands.Context, error: Exception) -> None:
        logger.exception(
            "Unhandled command error in '%s' by user %s",
            getattr(ctx.command, "qualified_name", "unknown"),
            ctx.author,
        )

    return bot


async def main() -> None:
    parser = argparse.ArgumentParser(description="Ars Victoriae Discord Bot")
    parser.add_argument(
        "--local",
        action="store_true",
        help="Starte lokalen CLI-Testmodus ohne Discord-Verbindung",
    )
    args = parser.parse_args()

    init_db()
    bot = _build_bot()

    try:
        await bot.load_extension("cogs.riot")

        if args.local:
            await run_local_cli(bot)
        else:
            if not DISCORD_TOKEN:
                raise RuntimeError("DISCORD_TOKEN fehlt. Bitte .env konfigurieren.")
            await bot.start(DISCORD_TOKEN)
    except Exception:
        logger.exception("Fatal error while starting bot")
        raise
    finally:
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    asyncio.run(main())