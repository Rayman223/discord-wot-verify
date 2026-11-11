"""Public WoT profiles; all lookups are read-only and failures stay private."""

import calendar
import logging
import re
from datetime import UTC, datetime
from urllib.parse import quote

import aiohttp
import discord
from discord import app_commands

from .nicknames import without_birthday_candles
from .wargaming import CONTACT_RANKS, valid_nickname

log = logging.getLogger(__name__)
LOOKUP_ERRORS = (discord.HTTPException, aiohttp.ClientError, OSError, ValueError)


def text(value):
    # Link labels need both brackets escaped; Discord's general helper leaves ']' untouched.
    value = re.sub(r"([\\`*_{}\[\]()<>|~])", r"\\\1", str(value))
    return discord.utils.escape_mentions(value)


def calendar_age(created, today):
    """Whole calendar years/months, clamping anniversaries to the month's last day."""
    months = (today.year - created.year) * 12 + today.month - created.month

    def anniversary(offset):
        year, month = divmod(created.year * 12 + created.month - 1 + offset, 12)
        month += 1
        return created.replace(
            year=year, month=month, day=min(created.day, calendar.monthrange(year, month)[1])
        )

    if anniversary(months) > today:
        months -= 1
    years, remaining = divmod(months, 12)
    return years, remaining, (today - anniversary(months)).days


def account_link(region, locale, account_id, nickname):
    path = quote(f"{account_id}-{nickname}", safe="")
    # WoT nicknames use only letters, digits and underscores. Escaping their underscores
    # leaves visible backslashes in Discord's link labels; keep escaping untrusted names.
    label = nickname if valid_nickname(nickname) else text(nickname)
    return f"[{label}](https://worldoftanks.{region}/{locale}/community/accounts/{path}/)"


async def profile_target(bot, discord_id):
    member = await bot.member(discord_id)
    if not any(role.id == bot.config.role_id for role in member.roles):
        raise ValueError("profile_player_required")
    row = bot.store.get(discord_id)
    if row is None:
        raise ValueError("profile_not_linked")
    if row["region"] != bot.config.region:
        raise ValueError("profile_wrong_region")
    return dict(row)


async def contact_field(bot, clan_id, region, locale):
    reservation = bot.store.clan_contact(region, clan_id)
    if reservation is None:
        return bot.t("profile_none")
    discord_id = reservation["discord_id"]
    row = bot.store.get(discord_id)
    if row is None or row["region"] != region:
        return bot.t("profile_contact_unusable")
    row = dict(row)
    warnings = []
    try:
        nickname = await bot.wg.nickname(row["account_id"])
    except ValueError:
        # Successful syncs store the Discord suffix too, not just the WoT account name.
        nickname = re.sub(
            r" \[[A-Za-z0-9_-]{2,5}\]$", "", without_birthday_candles(row["nickname"])
        )
        if not valid_nickname(nickname):
            return bot.t("profile_contact_unusable")
        warnings.append(bot.t("profile_contact_saved_name"))
    identity = account_link(region, locale, row["account_id"], nickname)
    try:
        member = await bot.member(discord_id)
    except discord.NotFound:
        warnings.append(bot.t("profile_contact_review"))
    except LOOKUP_ERRORS:
        warnings.append(bot.t("profile_contact_unknown"))
    else:
        roles = {role.id for role in member.roles}
        if not {bot.config.role_id, bot.config.clan_role_id} <= roles:
            warnings.append(bot.t("profile_contact_review"))
    try:
        membership = await bot.wg.clan_membership(row["account_id"])
    except ValueError:
        warnings.append(bot.t("profile_contact_unknown"))
    else:
        if membership is None or membership[0] != clan_id or membership[1] not in CONTACT_RANKS:
            warnings.append(bot.t("profile_contact_review"))
    return "\n".join([identity, *dict.fromkeys(warnings)])


async def profile_report(bot, row):
    region, account_id, locale = row["region"], row["account_id"], bot.config.locale
    player = await bot.wg.player_profile(account_id)
    report = discord.Embed(title=text(player.nickname), colour=discord.Colour.gold())

    def field(key, value, *, inline=True):
        report.add_field(name=bot.t(key), value=value, inline=inline)

    def date(value):
        return f"<t:{value}:F>" if value is not None else bot.t("profile_unavailable")

    field("profile_account_id", str(account_id))
    field("profile_created_at", date(player.created_at))
    age = bot.t("profile_unavailable")
    if player.created_at is not None:
        years, months, days = calendar_age(
            datetime.fromtimestamp(player.created_at, UTC).date(), datetime.now(UTC).date()
        )
        parts = []
        for unit, value in (("year", years), ("month", months), ("day", days)):
            if value:
                parts.append(
                    bot.t(f"profile_age_{unit}_{'one' if value == 1 else 'many'}").format(
                        count=value
                    )
                )
        age = ", ".join(parts) if parts else bot.t("profile_age_day_many").format(count=0)
    field("profile_age", age)
    field("profile_last_battle", date(player.last_battle_time))
    field("profile_logout", date(player.logout_at))

    try:
        membership = await bot.wg.profile_membership(account_id, locale)
    except ValueError:
        field("profile_clan", bot.t("profile_clan_unavailable"), inline=False)
        return report
    if membership is None:
        field("profile_clan", bot.t("status_no_clan"), inline=False)
        return report
    report.title = f"{text(player.nickname)} [{text(membership.tag)}]"
    field(
        "profile_rank", text(membership.rank) if membership.rank else bot.t("profile_unavailable")
    )
    try:
        clan = await bot.wg.clan_profile(membership.clan_id)
    except ValueError:
        field("profile_clan", bot.t("profile_clan_unavailable"), inline=False)
    else:
        realm = {"eu": "eu", "com": "na", "asia": "asia"}[region]
        field(
            "profile_clan",
            f"[{text(clan.name)}](https://{realm}.wargaming.net/clans/wot/{membership.clan_id})",
            inline=False,
        )
        field(
            "profile_members_count",
            str(clan.members_count)
            if clan.members_count is not None
            else bot.t("profile_unavailable"),
        )
        field("profile_clan_created", date(clan.created_at))
        leader = bot.t("profile_unavailable")
        if clan.leader_id is not None and clan.leader_name is not None:
            leader = account_link(region, locale, clan.leader_id, clan.leader_name)
        field("profile_commander", leader)
        if clan.emblem:
            report.set_thumbnail(url=clan.emblem)
    field(
        "profile_contact",
        await contact_field(bot, membership.clan_id, region, locale),
        inline=False,
    )
    return report


def register_profile_command(bot):
    @bot.tree.command(description=bot.t("command_profile"))
    @app_commands.guild_only()
    @app_commands.describe(member=bot.t("profile_member"))
    @app_commands.checks.cooldown(1, 30, key=lambda interaction: interaction.user.id)
    async def profile(interaction: discord.Interaction, member: discord.Member | None = None):
        if interaction.guild_id != bot.config.guild_id:
            raise ValueError("wrong_server")
        await interaction.response.defer(ephemeral=True, thinking=True)
        if interaction.channel is None:
            raise ValueError("discord_error")
        target = member or interaction.user
        row = await profile_target(bot, target.id)
        report = await profile_report(bot, row)
        # Do not publish after an unlink, identity change or player-role removal during API calls.
        async with bot.lock:
            current = await profile_target(bot, target.id)
            if (current["region"], current["account_id"]) != (row["region"], row["account_id"]):
                raise ValueError("profile_changed")
            try:
                await interaction.channel.send(
                    embed=report, allowed_mentions=discord.AllowedMentions.none()
                )
            except discord.Forbidden as exc:
                # Status/code identify Discord failures without logging response bodies or tokens.
                log.warning("Profile publication failed: status=%s code=%s", exc.status, exc.code)
                raise ValueError("profile_publish_forbidden") from None
            except discord.HTTPException as exc:
                log.warning("Profile publication failed: status=%s code=%s", exc.status, exc.code)
                raise
        try:
            await interaction.delete_original_response()
        except (discord.HTTPException, aiohttp.ClientError, OSError):
            # The public message was sent: never report a failure that invites a duplicate.
            log.warning("Could not remove private profile acknowledgement")
