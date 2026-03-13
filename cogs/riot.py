import logging
import re

import discord
from discord.ext import commands, tasks

from db.database import (
    add_rank_snapshot,
    get_account,
    get_all_accounts,
    get_rank_changes,
    get_latest_rank,
    set_preferred_name,
    upsert_account,
)
from services.riot_api import fetch_rank

_MAX_NICK = 32
logger = logging.getLogger(__name__)


def _build_nickname(base_name: str, rank_text: str) -> str:
    suffix = f" [{rank_text}]"
    max_base = _MAX_NICK - len(suffix)

    if max_base >= 1:
        return base_name[:max_base] + suffix

    compact_suffix = f" [{rank_text.replace(' / ', '/').replace('N/A', 'NA')}]"
    compact_max_base = _MAX_NICK - len(compact_suffix)
    if compact_max_base >= 1:
        return base_name[:compact_max_base] + compact_suffix

    return base_name[:_MAX_NICK]


class Riot(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _notify_rank_change(
        self,
        discord_id: str,
        guild_id: str | None,
        channel_id: str | None,
        old_rank: str,
        new_rank: str,
    ) -> None:
        if guild_id is None or channel_id is None:
            return

        try:
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                return

            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                return

            member = guild.get_member(int(discord_id))
            mention = member.mention if member else f"<@{discord_id}>"

            await channel.send(
                f"{mention} Rank-Update: **{old_rank}** → **{new_rank}**"
            )
        except ValueError:
            logger.exception(
                "Invalid guild/channel/user id while sending rank update (discord_id=%s, guild_id=%s, channel_id=%s)",
                discord_id,
                guild_id,
                channel_id,
            )
            return
        except discord.Forbidden:
            logger.warning(
                "Missing permissions to post rank update (discord_id=%s, guild_id=%s, channel_id=%s)",
                discord_id,
                guild_id,
                channel_id,
            )
            return
        except discord.HTTPException:
            logger.exception(
                "Discord HTTP error while sending rank update (discord_id=%s, guild_id=%s, channel_id=%s)",
                discord_id,
                guild_id,
                channel_id,
            )
            return

    def cog_unload(self) -> None:
        self.update_nicknames.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.update_nicknames.is_running():
            self.update_nicknames.start()

    # ------------------------------------------------------------------ #
    #  Background task                                                     #
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=5)
    async def update_nicknames(self) -> None:
        logger.info("Updating nicknames based on Riot ranks...")
        accounts = get_all_accounts()
        for discord_id, riot_name, riot_tag, guild_id, channel_id, preferred_name in accounts:
            try:
                rank = await fetch_rank(riot_name, riot_tag)
                if rank is None:
                    continue

                last_rank = get_latest_rank(discord_id)
                add_rank_snapshot(discord_id, rank)

                if last_rank is not None and last_rank != rank:
                    await self._notify_rank_change(
                        discord_id=discord_id,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        old_rank=last_rank,
                        new_rank=rank,
                    )

                logger.info("%s#%s -> %s", riot_name, riot_tag, rank)
                for guild in self.bot.guilds:
                    member = guild.get_member(int(discord_id))
                    if member is None:
                        continue

                    base_name = preferred_name if preferred_name else member.name
                    new_nick = _build_nickname(base_name, rank)

                    if member.nick != new_nick:
                        try:
                            await member.edit(nick=new_nick)
                        except discord.Forbidden:
                            logger.warning(
                                "Cannot update nickname for %s in guild %s (missing permissions)",
                                member,
                                guild.name,
                            )
                        except discord.HTTPException:
                            logger.exception(
                                "Discord HTTP error while updating nickname for %s in guild %s",
                                member,
                                guild.name,
                            )
            except Exception:
                logger.exception(
                    "Unhandled error while processing account %s (%s#%s)",
                    discord_id,
                    riot_name,
                    riot_tag,
                )

    @update_nicknames.before_loop
    async def before_update(self) -> None:
        await self.bot.wait_until_ready()

    @update_nicknames.error
    async def update_nicknames_error(self, error: BaseException) -> None:
        logger.exception("Unhandled error in update_nicknames loop: %s", error)

    # ------------------------------------------------------------------ #
    #  Commands                                                            #
    # ------------------------------------------------------------------ #

    @commands.command(name="addRiot")
    async def add_riot(self, ctx: commands.Context, *, args: str = "") -> None:
        riot_name = None
        riot_tag = None

        name_match = re.search(r"--name\s+(.+?)(?=\s+--|\s*$)", args)
        tag_match  = re.search(r"--tag\s+(\S+)",                  args)

        if name_match:
            riot_name = name_match.group(1).strip()
        if tag_match:
            riot_tag = tag_match.group(1).strip()

        if not riot_name or not riot_tag:
            await ctx.send(
                "Falsche Verwendung. Benutze: `!addRiot --name <Name> --tag <Tag>`\n"
                "Beispiel: `!addRiot --name Ars Noah --tag EUW`"
            )
            return

        discord_id = str(ctx.author.id)
        guild_id = str(ctx.guild.id) if ctx.guild is not None else None
        channel_id = str(ctx.channel.id)
        upsert_account(
            discord_id=discord_id,
            riot_name=riot_name,
            riot_tag=riot_tag,
            guild_id=guild_id,
            channel_id=channel_id,
        )

        await ctx.send(
            f"Riot-Account **{riot_name}#{riot_tag}** wurde mit "
            f"{ctx.author.mention} verknüpft!"
        )

    @commands.command(name="myRiot")
    async def my_riot(self, ctx: commands.Context) -> None:
        discord_id = str(ctx.author.id)
        row = get_account(discord_id)

        if row is None:
            await ctx.send("Du hast noch keinen Riot-Account verknüpft. Benutze `!addRiot --name <Name> --tag <Tag>`.")
        else:
            riot_name, riot_tag = row
            await ctx.send(f"Dein verknüpfter Riot-Account: **{riot_name}#{riot_tag}**")

    @commands.command(name="rankHistory")
    async def rank_history(self, ctx: commands.Context) -> None:
        discord_id = str(ctx.author.id)
        history_rows = get_rank_changes(discord_id, limit=10)

        if not history_rows:
            await ctx.send("Noch keine Rank-Änderungen vorhanden.")
            return

        lines = [
            f"{checked_at} UTC → **{old_rank}** → **{new_rank}**"
            for old_rank, new_rank, checked_at in history_rows
        ]
        await ctx.send("Deine letzten Rank-Changes:\n" + "\n".join(lines))

    @commands.command(name="setName")
    async def set_name(self, ctx: commands.Context, *, args: str = "") -> None:
        name_match = re.search(r"--name\s+(.+?)(?=\s+--|\s*$)", args)
        if not name_match:
            await ctx.send(
                "Falsche Verwendung. Benutze: `!setName --name <NAME>`\n"
                "Beispiel: `!setName --name Ars Victoriae`"
            )
            return

        desired_name = name_match.group(1).strip()
        if not desired_name:
            await ctx.send("Bitte gib einen gültigen Namen an.")
            return

        discord_id = str(ctx.author.id)
        changed = set_preferred_name(discord_id, desired_name)
        if not changed:
            await ctx.send("Du musst zuerst deinen Riot-Account mit `!addRiot --name <Name> --tag <Tag>` verknüpfen.")
            return

        latest_rank = get_latest_rank(discord_id) or "N/A ⚪ / N/A ⚪"
        if ctx.guild is not None:
            member = ctx.guild.get_member(ctx.author.id)
            if member is not None:
                new_nick = _build_nickname(desired_name, latest_rank)
                if member.nick != new_nick:
                    try:
                        await member.edit(nick=new_nick)
                    except discord.Forbidden:
                        logger.warning(
                            "Cannot update nickname immediately for %s in guild %s (missing permissions)",
                            member,
                            ctx.guild.name,
                        )
                    except discord.HTTPException:
                        logger.exception(
                            "Discord HTTP error while immediately updating nickname for %s in guild %s",
                            member,
                            ctx.guild.name,
                        )

        await ctx.send(f"Wunschname gespeichert (case-sensitive): **{desired_name}**")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Riot(bot))
