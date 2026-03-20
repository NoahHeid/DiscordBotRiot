import logging
import re

import discord
from discord.ext import commands, tasks

from db.database import (
    add_rank_snapshot,
    get_account,
    get_all_accounts,
    get_preferred_name,
    get_rank_changes,
    get_show_rank,
    get_latest_rank,
    set_preferred_name,
    toggle_show_rank,
    upsert_account,
)
from services.riot_api import fetch_rank, normalize_combined_rank, rank_score

_MAX_NICK = 32
_NOTIFIER_ROLE = "BotNotifier"
_RANK_UPDATES_CHANNEL_PREFERRED = "rank-updates-📈"
_RANK_UPDATES_CHANNEL_FALLBACK = "rank-updates"
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


def _build_plain_nickname(base_name: str) -> str:
    return base_name[:_MAX_NICK]


class Riot(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    async def _get_or_create_notifier_role(self, guild: discord.Guild) -> discord.Role | None:
        role = discord.utils.get(guild.roles, name=_NOTIFIER_ROLE)
        if role is None:
            try:
                role = await guild.create_role(
                    name=_NOTIFIER_ROLE,
                    mentionable=True,
                    reason="Auto-created by Ars Victoriae Discord Bot",
                )
            except discord.Forbidden:
                logger.warning("Cannot create %s role in %s (missing permissions)", _NOTIFIER_ROLE, guild.name)
                return None
            except discord.HTTPException:
                logger.exception("Failed to create %s role in %s", _NOTIFIER_ROLE, guild.name)
                return None
        return role

    def _find_rank_updates_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        existing_channel = discord.utils.get(guild.text_channels, name=_RANK_UPDATES_CHANNEL_PREFERRED)
        if isinstance(existing_channel, discord.TextChannel):
            return existing_channel

        existing_channel = discord.utils.get(guild.text_channels, name=_RANK_UPDATES_CHANNEL_FALLBACK)
        if isinstance(existing_channel, discord.TextChannel):
            return existing_channel

        for text_channel in guild.text_channels:
            if text_channel.name.startswith("rank-updates"):
                return text_channel

        return None

    def _build_rank_updates_channel_overwrites(
        self,
        guild: discord.Guild,
        notifier_role: discord.Role | None,
    ) -> dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite]:
        overwrites: dict[discord.Role | discord.Member | discord.Object, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=False,
                send_messages=False,
                read_message_history=False,
            )
        }

        if notifier_role is not None:
            overwrites[notifier_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
                read_message_history=True,
                send_messages_in_threads=False,
                create_public_threads=False,
                create_private_threads=False,
            )

        for role in guild.roles:
            if role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    send_messages_in_threads=True,
                )

        bot_member = guild.get_member(self.bot.user.id) if self.bot.user is not None else None
        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                send_messages_in_threads=True,
            )

        return overwrites

    async def _ensure_rank_updates_channel_permissions(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        notifier_role: discord.Role | None,
    ) -> None:
        overwrites = self._build_rank_updates_channel_overwrites(guild, notifier_role)

        if channel.overwrites == overwrites:
            return

        try:
            await channel.edit(
                overwrites=overwrites,
                reason="Set private permissions for rank updates channel",
            )
        except discord.Forbidden:
            logger.warning("Cannot set permissions for rank updates channel in %s", guild.name)
        except discord.HTTPException:
            logger.exception("Failed to set permissions for rank updates channel in %s", guild.name)

    async def _get_or_create_rank_updates_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        notifier_role = await self._get_or_create_notifier_role(guild)

        existing_channel = self._find_rank_updates_channel(guild)
        if existing_channel is not None:
            await self._ensure_rank_updates_channel_permissions(guild, existing_channel, notifier_role)
            return existing_channel

        overwrites = self._build_rank_updates_channel_overwrites(guild, notifier_role)

        try:
            return await guild.create_text_channel(
                name=_RANK_UPDATES_CHANNEL_PREFERRED,
                topic="Automatische Solo/Flex Rank-Updates (Uprank/Downrank)",
                overwrites=overwrites,
                reason="Auto-created by Ars Victoriae Discord Bot",
            )
        except discord.Forbidden:
            logger.warning("Cannot create rank updates channel in %s (missing permissions)", guild.name)
            return None
        except discord.HTTPException:
            logger.exception("Failed to create preferred rank updates channel in %s", guild.name)

        try:
            return await guild.create_text_channel(
                name=_RANK_UPDATES_CHANNEL_FALLBACK,
                topic="Automatische Solo/Flex Rank-Updates (Uprank/Downrank)",
                overwrites=overwrites,
                reason="Auto-created by Ars Victoriae Discord Bot",
            )
        except discord.Forbidden:
            logger.warning("Cannot create fallback rank updates channel in %s (missing permissions)", guild.name)
            return None
        except discord.HTTPException:
            logger.exception("Failed to create fallback rank updates channel in %s", guild.name)
            return None

    async def _notify_rank_change(
        self,
        discord_id: str,
        guild_id: str | None,
        old_rank: str,
        new_rank: str,
    ) -> None:
        if guild_id is None:
            return

        old_rank = normalize_combined_rank(old_rank)
        new_rank = normalize_combined_rank(new_rank)

        try:
            guild = self.bot.get_guild(int(guild_id))
            if guild is None:
                return

            channel = await self._get_or_create_rank_updates_channel(guild)
            if channel is None:
                return

            member = guild.get_member(int(discord_id))
            person_mention = member.mention if member else f"<@{discord_id}>"

            notifier_role = await self._get_or_create_notifier_role(guild)
            role_mention = notifier_role.mention if notifier_role else f"@{_NOTIFIER_ROLE}"

            # Split combined rank string into solo and flex components
            old_parts = old_rank.split(" / ", 1)
            new_parts = new_rank.split(" / ", 1)
            old_solo = old_parts[0]
            old_flex = old_parts[1] if len(old_parts) > 1 else old_parts[0]
            new_solo = new_parts[0]
            new_flex = new_parts[1] if len(new_parts) > 1 else new_parts[0]

            changes: list[tuple[str, str, str]] = []
            if old_solo != new_solo:
                changes.append(("Solo/Duo", new_solo, "solo"))
            if old_flex != new_flex:
                changes.append(("Flex", new_flex, "flex"))

            for queue_label, new_queue_rank, queue_key in changes:
                old_queue_rank = old_solo if queue_key == "solo" else old_flex
                is_uprank = rank_score(new_queue_rank) > rank_score(old_queue_rank)

                if is_uprank:
                    msg = (
                        f"{role_mention} Wow! {person_mention} hat hart gecarried in **{queue_label}** "
                        f"und erreicht jetzt Rang **{new_queue_rank}**. "
                        f"Das neue Ranking ist jetzt **{new_solo}** und **{new_flex}**."
                    )
                else:
                    msg = (
                        f"{role_mention} Schade! {person_mention} wurde von seinen Teammates runtergerannt "
                        f"in **{queue_label}** und leidet jetzt in Rang **{new_queue_rank}**. "
                        f"Das neue Ranking ist jetzt **{new_solo}** und **{new_flex}**."
                    )

                await channel.send(msg)

        except ValueError:
            logger.exception(
                "Invalid guild/user id while sending rank update (discord_id=%s, guild_id=%s)",
                discord_id, guild_id,
            )
        except discord.Forbidden:
            logger.warning(
                "Missing permissions to post rank update (discord_id=%s, guild_id=%s)",
                discord_id, guild_id,
            )
        except discord.HTTPException:
            logger.exception(
                "Discord HTTP error while sending rank update (discord_id=%s, guild_id=%s)",
                discord_id, guild_id,
            )

    def cog_unload(self) -> None:
        self.update_nicknames.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        for guild in self.bot.guilds:
            notifier_role = await self._get_or_create_notifier_role(guild)
            existing_channel = self._find_rank_updates_channel(guild)
            if existing_channel is not None:
                await self._ensure_rank_updates_channel_permissions(guild, existing_channel, notifier_role)

        if not self.update_nicknames.is_running():
            self.update_nicknames.start()

    # ------------------------------------------------------------------ #
    #  Background task                                                     #
    # ------------------------------------------------------------------ #

    @tasks.loop(minutes=5)
    async def update_nicknames(self) -> None:
        logger.info("Updating nicknames based on Riot ranks...")
        accounts = get_all_accounts()
        for discord_id, riot_name, riot_tag, guild_id, _channel_id, preferred_name, show_rank in accounts:
            try:
                rank = await fetch_rank(riot_name, riot_tag)
                if rank is None:
                    continue

                rank = normalize_combined_rank(rank)

                last_rank = get_latest_rank(discord_id)
                normalized_last_rank = normalize_combined_rank(last_rank) if last_rank else None

                # Only snapshot when rank actually changed
                if normalized_last_rank is None or normalized_last_rank != rank:
                    add_rank_snapshot(discord_id, rank)

                if normalized_last_rank is not None and normalized_last_rank != rank:
                    await self._notify_rank_change(
                        discord_id=discord_id,
                        guild_id=guild_id,
                        old_rank=normalized_last_rank,
                        new_rank=rank,
                    )

                logger.info("%s#%s -> %s", riot_name, riot_tag, rank)
                for guild in self.bot.guilds:
                    member = guild.get_member(int(discord_id))
                    if member is None:
                        continue

                    base_name = preferred_name if preferred_name else member.name
                    new_nick = _build_nickname(base_name, rank) if bool(show_rank) else _build_plain_nickname(base_name)

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

        # Add user to @BotNotifier role
        if ctx.guild is not None:
            member = ctx.guild.get_member(ctx.author.id)
            if member is not None:
                notifier_role = await self._get_or_create_notifier_role(ctx.guild)
                if notifier_role is not None and notifier_role not in member.roles:
                    try:
                        await member.add_roles(notifier_role, reason="Linked Riot account via !addRiot")
                    except discord.Forbidden:
                        logger.warning("Cannot assign %s role to %s in %s", _NOTIFIER_ROLE, member, ctx.guild.name)
                    except discord.HTTPException:
                        logger.exception("Failed to assign %s role to %s", _NOTIFIER_ROLE, member)

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

        latest_rank = normalize_combined_rank(get_latest_rank(discord_id) or "NA / NA")
        show_rank = get_show_rank(discord_id)
        if ctx.guild is not None:
            member = ctx.guild.get_member(ctx.author.id)
            if member is not None:
                new_nick = _build_nickname(desired_name, latest_rank) if show_rank is not False else _build_plain_nickname(desired_name)
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

    @commands.command(name="toggleShowRank")
    async def toggle_show_rank_command(self, ctx: commands.Context) -> None:
        discord_id = str(ctx.author.id)
        new_show_rank = toggle_show_rank(discord_id)
        if new_show_rank is None:
            await ctx.send("Du musst zuerst deinen Riot-Account mit `!addRiot --name <Name> --tag <Tag>` verknüpfen.")
            return

        latest_rank = normalize_combined_rank(get_latest_rank(discord_id) or "NA / NA")
        if ctx.guild is not None:
            member = ctx.guild.get_member(ctx.author.id)
            if member is not None:
                preferred_name = get_preferred_name(discord_id) or member.name

                new_nick = _build_nickname(preferred_name, latest_rank) if new_show_rank else _build_plain_nickname(preferred_name)
                if member.nick != new_nick:
                    try:
                        await member.edit(nick=new_nick)
                    except discord.Forbidden:
                        logger.warning(
                            "Cannot update nickname after toggleShowRank for %s in guild %s (missing permissions)",
                            member,
                            ctx.guild.name,
                        )
                    except discord.HTTPException:
                        logger.exception(
                            "Discord HTTP error while updating nickname after toggleShowRank for %s in guild %s",
                            member,
                            ctx.guild.name,
                        )

        status_text = "aktiviert" if new_show_rank else "deaktiviert"
        await ctx.send(f"Rank-Anzeige neben deinem Namen wurde **{status_text}**.")

    @commands.command(name="help")
    async def help_command(self, ctx: commands.Context) -> None:
        lines = [
            "**Verfügbare Commands:**",
            "",
            "`!addRiot --name <Name> --tag <Tag>`",
            "Verknüpft deinen Riot-Account mit deinem Discord-User und gibt dir automatisch die Rolle `BotNotifier`.",
            "Beispiel: `!addRiot --name Ars Noah --tag EUW`",
            "",
            "`!myRiot`",
            "Zeigt deinen aktuell verknüpften Riot-Account an.",
            "",
            "`!rankHistory`",
            "Zeigt deine letzten Rank-Änderungen (max. 10) mit Zeitstempel.",
            "",
            "`!setName --name <Name>`",
            "Setzt deinen gewünschten Anzeigenamen (case-sensitive) für Nickname-Updates.",
            "Beispiel: `!setName --name Ars Victoriae`",
            "",
            "`!toggleShowRank`",
            "Schaltet die Anzeige deines Ranks neben deinem Namen ein oder aus.",
            "",
            "`!help`",
            "Zeigt diese Hilfe mit allen verfügbaren Commands an.",
        ]
        await ctx.send("\n".join(lines))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Riot(bot))
