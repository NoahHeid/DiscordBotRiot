import logging
import re
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from pyke import Pyke

from config import RIOT_API_KEY
from db.database import (
    add_rank_snapshot,
    get_account,
    get_all_accounts,
    get_preferred_name,
    get_rank_changes,
    get_show_rank,
    get_latest_rank,
    get_queue_tenure_start,
    set_preferred_name,
    toggle_show_rank,
    upsert_account,
)
from services.riot_api import (
    fetch_rank_with_context,
    fetch_ranked_match_stats_since,
    normalize_combined_rank,
    rank_score,
)

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


def _split_combined_rank(rank_text: str) -> tuple[str, str]:
    parts = [part.strip() for part in rank_text.split("/", 1)]
    if len(parts) == 1:
        value = parts[0]
        return value, value
    return parts[0], parts[1]


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
        queue_details: dict[str, tuple[int, int, str | None]],
        puuid: str | None = None,
        routing: str | None = None,
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

            messages = await self.build_rank_change_messages_with_stats(
                person_mention=person_mention,
                role_mention=role_mention,
                old_rank=old_rank,
                new_rank=new_rank,
                queue_details=queue_details,
                puuid=puuid,
                routing=routing,
            )

            for msg in messages:
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

    def build_rank_change_messages(
        self,
        person_mention: str,
        role_mention: str,
        old_rank: str,
        new_rank: str,
        queue_details: dict[str, tuple[int, int, str | None]],
    ) -> list[str]:
        old_rank = normalize_combined_rank(old_rank)
        new_rank = normalize_combined_rank(new_rank)

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

        messages: list[str] = []
        for queue_label, new_queue_rank, queue_key in changes:
            old_queue_rank = old_solo if queue_key == "solo" else old_flex
            is_uprank = rank_score(new_queue_rank) > rank_score(old_queue_rank)
            days_spent, games_spent, _match_id = queue_details.get(queue_key, (0, 0, None))
            days_text = "1 Tag" if days_spent == 1 else f"{days_spent} Tage"

            if is_uprank:
                msg = (
                    f"{role_mention} Wow! {person_mention} hat hart gecarried in **{queue_label}** "
                    f"und erreicht jetzt Rang **{new_queue_rank}**. "
                    f"Das neue Ranking ist jetzt **{new_solo}** und **{new_flex}**. "
                    f"Er/Sie verbrachte in **{old_queue_rank}** {days_text} beziehungsweise **{games_spent} Games**."
                )
            else:
                msg = (
                    f"{role_mention} Schade! {person_mention} wurde von seinen Teammates runtergerannt "
                    f"in **{queue_label}** und leidet jetzt in Rang **{new_queue_rank}**. "
                    f"Das neue Ranking ist jetzt **{new_solo}** und **{new_flex}**. "
                    f"Er/Sie verbrachte in **{old_queue_rank}** {days_text} beziehungsweise **{games_spent} Games**."
                )

            messages.append(msg)

        return messages

    async def build_rank_change_messages_with_stats(
        self,
        person_mention: str,
        role_mention: str,
        old_rank: str,
        new_rank: str,
        queue_details: dict[str, tuple[int, int, str | None]],
        puuid: str | None = None,
        routing: str | None = None,
    ) -> list[str]:
        """
        Build rank change messages with enhanced stats from the rank change match.
        
        Args:
            person_mention: Discord mention string
            role_mention: Discord role mention string
            old_rank: Old rank string (e.g., "G2 / D1")
            new_rank: New rank string (e.g., "P1 / D1")
            queue_details: Dict with queue_key -> (days_spent, games_spent, match_id)
            puuid: Player PUUID for fetching match data
            routing: Routing string for continent
        
        Returns:
            List of message strings for Discord
        """
        from services.match_service import fetch_match_details
        from services.match_analyzer import extract_player_stats
        from pyke import Continent
        
        old_rank = normalize_combined_rank(old_rank)
        new_rank = normalize_combined_rank(new_rank)

        # Split combined rank string
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

        # Map routing to continent
        routing_to_continent = {
            "americas": Continent.AMERICAS,
            "europe": Continent.EUROPE,
            "asia": Continent.ASIA,
            "sea": Continent.SEA,
        }
        continent = routing_to_continent.get(routing.lower() if routing else "europe", Continent.EUROPE)

        messages: list[str] = []
        for queue_label, new_queue_rank, queue_key in changes:
            old_queue_rank = old_solo if queue_key == "solo" else old_flex
            is_uprank = rank_score(new_queue_rank) > rank_score(old_queue_rank)
            days_spent, games_spent, match_id = queue_details.get(queue_key, (0, 0, None))
            days_text = "1 Tag" if days_spent == 1 else f"{days_spent} Tage"
            
            # Fetch match details if available
            champion_name = None
            position = None
            kills = 0
            deaths = 0
            assists = 0
            cs = 0
            
            if match_id and puuid:
                try:
                    match_data = await fetch_match_details(match_id, continent)
                    if match_data:
                        stats = extract_player_stats(match_data, puuid)
                        if stats:
                            champion_name = stats.get("championName", "Unknown")
                            position = stats.get("individualPosition", "Unknown")
                            kills = stats.get("kills", 0)
                            deaths = stats.get("deaths", 0)
                            assists = stats.get("assists", 0)
                            cs = stats.get("totalMinionsKilled", 0)
                except Exception as e:
                    logger.debug(f"Failed to fetch match stats for {match_id}: {e}")

            # Build message with stats if available
            
            match_id_for_link = match_id.split("_", 1)[1] if match_id and "_" in match_id else match_id
            match_link = f"https://www.leagueofgraphs.com/de/match/euw/{match_id_for_link}" if match_id_for_link else None
            ka_text = f"{kills}/{deaths}/{assists}" if champion_name else "(Keine Statistiken verfügbar)"
            champion_text = f"{champion_name} in der {position} Lane" if champion_name else "überraschend gut"

            if is_uprank:
                msg = (
                    f"{role_mention} Wow! {person_mention} ist in **{queue_label}** in **{new_queue_rank}** "
                    f"aufgestiegen! Er war viel zu krass mit {champion_text} und konnte mit {ka_text} und {cs} CS "
                    f"komplett carrien. Er verbrachte {days_text} und **{games_spent} Games** in **{old_queue_rank}**."
                )
            else:
                msg = (
                    f"{role_mention} Schade! {person_mention} ist in **{queue_label}** in **{new_queue_rank}** "
                    f"abgestiegen. Seine Skills mit {champion_text} haben mit {ka_text} und {cs} CS nicht gereicht "
                    f"um die Klasse zu halten. Er verbrachte {days_text} und **{games_spent} Games** in **{old_queue_rank}**."
                )
            
            if match_link:
                msg += f"\n{match_link}"

            messages.append(msg)

        return messages

    async def run_update_nicknames_once(self) -> None:
        logger.info("Updating nicknames based on Riot ranks...")
        accounts = get_all_accounts()
        for discord_id, riot_name, riot_tag, guild_id, _channel_id, preferred_name, show_rank, db_puuid in accounts:
            try:
                rank, api_puuid, routing = await fetch_rank_with_context(riot_name, riot_tag)
                if rank is None:
                    continue

                rank = normalize_combined_rank(rank)
                
                # Use PUUID from API (most recent), fall back to DB
                puuid = api_puuid or db_puuid
                
                # Save PUUID if we got it from API and it wasn't in DB
                if api_puuid and not db_puuid:
                    from db.database import save_puuid
                    save_puuid(discord_id, api_puuid)

                last_rank = get_latest_rank(discord_id)
                normalized_last_rank = normalize_combined_rank(last_rank) if last_rank else None

                # Only snapshot when rank actually changed
                if normalized_last_rank is None or normalized_last_rank != rank:
                    solo_match_id: str | None = None
                    flex_match_id: str | None = None
                    queue_details: dict[str, tuple[int, int, str | None]] = {}

                    if normalized_last_rank is not None and puuid is not None and routing is not None:
                        old_solo, old_flex = _split_combined_rank(normalized_last_rank)
                        new_solo, new_flex = _split_combined_rank(rank)
                        now_utc = datetime.now(timezone.utc)

                        for queue_key, old_queue_rank, new_queue_rank in (
                            ("solo", old_solo, new_solo),
                            ("flex", old_flex, new_flex),
                        ):
                            if old_queue_rank == new_queue_rank:
                                continue

                            tenure_start = get_queue_tenure_start(discord_id, queue_key)
                            if tenure_start is None:
                                tenure_start = now_utc

                            days_spent = max(0, int((now_utc - tenure_start).total_seconds() // 86400))
                            games_spent, latest_match_id = await fetch_ranked_match_stats_since(
                                puuid=puuid,
                                routing=routing,
                                queue_key=queue_key,
                                since_utc=tenure_start,
                            )
                            queue_details[queue_key] = (days_spent, games_spent, latest_match_id)

                            if queue_key == "solo":
                                solo_match_id = latest_match_id
                            elif queue_key == "flex":
                                flex_match_id = latest_match_id

                    add_rank_snapshot(
                        discord_id=discord_id,
                        rank=rank,
                        solo_change_match_id=solo_match_id,
                        flex_change_match_id=flex_match_id,
                    )

                if normalized_last_rank is not None and normalized_last_rank != rank:
                    await self._notify_rank_change(
                        discord_id=discord_id,
                        guild_id=guild_id,
                        old_rank=normalized_last_rank,
                        new_rank=rank,
                        queue_details=queue_details,
                        puuid=puuid,
                        routing=routing,
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
        await self.run_update_nicknames_once()

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

        # Fetch PUUID for the account
        puuid = None
        try:
            from pyke import Continent, Region
            from services.riot_api import _TAG_TO_REGION, _CONTINENT_ROUTING
            
            continent, region = _TAG_TO_REGION.get(riot_tag.upper(), (Continent.EUROPE, Region.EUW))
            
            async with Pyke(RIOT_API_KEY, timeout=30) as api:
                account = await api.account.by_riot_id(continent, riot_name, riot_tag)
                puuid = account.get("puuid")
        except Exception as e:
            logger.warning(f"Failed to fetch PUUID for {riot_name}#{riot_tag}: {e}")
            # Continue without PUUID; it can be fetched later

        discord_id = str(ctx.author.id)
        guild_id = str(ctx.guild.id) if ctx.guild is not None else None
        channel_id = str(ctx.channel.id)
        upsert_account(
            discord_id=discord_id,
            riot_name=riot_name,
            riot_tag=riot_tag,
            guild_id=guild_id,
            channel_id=channel_id,
            puuid=puuid,
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
            riot_name, riot_tag, _puuid = row
            await ctx.send(f"Dein verknüpfter Riot-Account: **{riot_name}#{riot_tag}**")

    @commands.command(name="rankHistory")
    async def rank_history(self, ctx: commands.Context) -> None:
        discord_id = str(ctx.author.id)
        history_rows = get_rank_changes(discord_id, limit=10)

        if not history_rows:
            await ctx.send("Noch keine Rank-Änderungen vorhanden.")
            return

        lines: list[str] = []
        for old_rank, new_rank, checked_at, solo_match_id, flex_match_id in history_rows:
            base_line = f"{checked_at} UTC → **{old_rank}** → **{new_rank}**"

            details: list[str] = []
            if solo_match_id:
                details.append(
                    f"Solo Match: `{solo_match_id}` (https://www.op.gg/matches/{solo_match_id})"
                )
            if flex_match_id:
                details.append(
                    f"Flex Match: `{flex_match_id}` (https://www.op.gg/matches/{flex_match_id})"
                )

            if details:
                lines.append(base_line + "\n  " + " | ".join(details))
            else:
                lines.append(base_line)

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
