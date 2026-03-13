import asyncio
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


async def main() -> None:
    init_db()

    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True

    bot = commands.Bot(command_prefix="!", intents=intents)

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

    try:
        await bot.load_extension("cogs.riot")
        await bot.start(DISCORD_TOKEN)
    except Exception:
        logger.exception("Fatal error while starting bot")
        raise


if __name__ == "__main__":
    asyncio.run(main())