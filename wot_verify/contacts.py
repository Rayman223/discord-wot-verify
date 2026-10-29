import asyncio

import aiohttp
import discord
from discord import app_commands

from .wargaming import CONTACT_RANKS


async def contacts_report(bot, page):
    total, rows = bot.store.contacts_page(page)
    pages = max(1, (total + 4) // 5)
    if page < 1 or page > pages:
        raise ValueError("contacts_bad_page")
    report = discord.Embed(title=bot.t("contacts_title"), description=bot.t("contacts_read_only"))
    report.set_footer(text=bot.t("contacts_page").format(page=page, pages=pages, total=total))
    if not rows:
        report.description = bot.t("contacts_empty")
        return report

    async def review(row):
        messages = []
        current_clan = None
        try:
            member = await bot.member(row["discord_id"])
        except discord.NotFound:
            messages.append(bot.t("contacts_left"))
        except (discord.HTTPException, aiohttp.ClientError, OSError, ValueError):
            messages.append(bot.t("contacts_discord_unknown"))
        else:
            ids = {role.id for role in member.roles}
            if bot.config.role_id not in ids or getattr(bot.config, "clan_role_id", 0) not in ids:
                messages.append(bot.t("contacts_roles_missing"))
        if row["account_id"] is None:
            messages.append(bot.t("contacts_no_link"))
        elif row["region"] != bot.config.region:
            messages.append(bot.t("contacts_wg_unknown"))
        else:
            try:
                membership = await bot.wg.clan_membership(row["account_id"])
            except ValueError:
                messages.append(bot.t("contacts_wg_unknown"))
            else:
                if membership is not None:
                    current_clan = bot.t("contacts_current_clan").format(
                        tag=membership[2], clan_id=membership[0]
                    )
                if membership is None or membership[0] != row["clan_id"]:
                    messages.append(bot.t("contacts_clan_changed"))
                elif membership[1] not in CONTACT_RANKS:
                    messages.append(bot.t("contacts_rank_lost"))
        if not messages:
            messages.append(bot.t("contacts_ok"))
        if current_clan:
            messages.insert(0, current_clan)
        identity = bot.t("contacts_identity").format(
            clan_id=row["clan_id"],
            region=row["region"].upper(),
            account_id=row["account_id"] or "—",
        )
        return str(row["discord_id"]), identity + "\n" + "\n".join(messages)

    for name, value in await asyncio.gather(*(review(row) for row in rows)):
        report.add_field(name=f"Discord · {name}", value=value, inline=False)
    return report


def register_contacts_command(bot):
    @bot.tree.command(description=bot.t("command_contacts"))
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.cooldown(1, 30, key=lambda i: i.user.id)
    async def contacts(interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1):
        await interaction.response.defer(ephemeral=True)
        report = await contacts_report(bot, page)
        await interaction.followup.send(embed=report, ephemeral=True)
