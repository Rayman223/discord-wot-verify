"""Read-only, private reports for members and administrators."""

import discord

from .wargaming import CONTACT_RANKS


def text(value):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))


async def status_report(bot, discord_id):
    row = bot.store.get(discord_id)
    report = discord.Embed(title=bot.t("status_title"))
    if row is None:
        report.description = bot.t("status_no_link")
        return report

    # Snapshot local state before any network await; the report never writes it.
    contact = bot.store.contact(discord_id)
    pending_tags = bot.store.contact_tags(discord_id)
    member = await bot.member(discord_id)
    roles = {role.id for role in member.roles}
    clan_role_id = getattr(bot.config, "clan_role_id", 0)
    has_contact = clan_role_id != 0 and clan_role_id in roles
    nickname = None
    membership = None
    clan_known = False
    # The configured API must never query an identity from another region.
    if row["region"] == bot.config.region:
        try:
            nickname = await bot.wg.nickname(row["account_id"])
        except ValueError:
            pass
        try:
            membership = await bot.wg.clan_membership(row["account_id"])
            clan_known = True
        except ValueError:
            pass

    def field(key, value):
        report.add_field(name=bot.t(key), value=value, inline=False)

    field(
        "status_account", f"{text(row['nickname'])} · {row['account_id']} ({row['region'].upper()})"
    )
    field(
        "status_last_sync",
        f"<t:{row['synced_at']}:F>" if row["synced_at"] else bot.t("status_never"),
    )
    field(
        "status_player_role",
        bot.t("status_present" if bot.config.role_id in roles else "status_missing"),
    )
    field(
        "status_contact_role",
        bot.t(
            "status_present"
            if has_contact
            else "status_missing"
            if clan_role_id
            else "status_disabled"
        ),
    )
    if not clan_known:
        clan = bot.t("status_unknown")
    elif membership is None:
        clan = bot.t("status_no_clan")
    else:
        clan = f"[{text(membership[2])}] · {membership[0]}"
    field("status_clan", clan)
    if contact:
        field("status_reservation", f"{contact['clan_id']} ({contact['region'].upper()})")

    hints = []
    expected = nickname
    if has_contact and contact:
        if not clan_known:
            expected = None
        elif (
            membership is None
            or membership[1] not in CONTACT_RANKS
            or (contact["region"], contact["clan_id"]) != (row["region"], membership[0])
        ):
            hints.append(bot.t("clan_review"))
            expected = None
        elif nickname is not None:
            expected = f"{nickname} [{membership[2]}]"
            if len(expected) > 32:
                hints.append(bot.t("clan_name_long"))
                expected = None
    field(
        "status_current_nickname", text(member.nick) if member.nick else bot.t("status_no_nickname")
    )
    field(
        "status_expected_nickname",
        text(expected) if expected is not None else bot.t("status_unknown"),
    )
    if not clan_known or nickname is None:
        hints.append(bot.t("status_wg_unavailable"))
    if (contact is not None) != has_contact:
        hints.append(bot.t("status_contact_review"))
    if pending_tags and not has_contact:
        hints.append(bot.t("status_tag_pending"))
    if bot.config.role_id not in roles or (expected is not None and member.nick != expected):
        hints.append(bot.t("status_resync_hint"))
    me = member.guild.me
    if (
        expected is not None
        and member.nick != expected
        and me is not None
        and (
            member.id == member.guild.owner_id
            or member.top_role >= me.top_role
            or not me.guild_permissions.manage_nicknames
        )
    ):
        hints.append(bot.t("status_nickname_blocked"))
    report.description = "\n".join(hints) if hints else bot.t("status_ok")
    return report


def diagnose_report(bot, interaction):
    guild = interaction.guild
    me = guild.me if guild is not None else None
    if guild is None or me is None:
        raise ValueError("not_ready")
    report = discord.Embed(title=bot.t("diagnose_title"), description=bot.t("diagnose_read_only"))

    def check(label, ok, advice):
        report.add_field(
            name=bot.t(label),
            value=bot.t("diagnose_ok") if ok else bot.t(advice),
            inline=False,
        )

    permissions = me.guild_permissions
    check("diagnose_manage_roles", permissions.manage_roles, "diagnose_grant_roles")
    check("diagnose_manage_nicknames", permissions.manage_nicknames, "diagnose_grant_nicknames")
    for role_id, label in (
        (bot.config.role_id, "status_player_role"),
        (getattr(bot.config, "clan_role_id", 0), "status_contact_role"),
    ):
        if not role_id:
            report.add_field(name=bot.t(label), value=bot.t("status_disabled"), inline=False)
            continue
        role = guild.get_role(role_id)
        if role is None:
            result = bot.t("diagnose_role_missing")
        elif (
            role.is_default()
            or role.managed
            or role.permissions.administrator
            or (label == "status_contact_role" and role_id == bot.config.role_id)
        ):
            result = bot.t("diagnose_role_invalid")
        elif role >= me.top_role:
            result = bot.t("diagnose_role_hierarchy")
        else:
            result = bot.t("diagnose_ok")
        report.add_field(name=bot.t(label), value=f"{role_id} · {result}", inline=False)

    channel = interaction.channel
    if channel is None:
        report.add_field(
            name=bot.t("diagnose_channel"), value=bot.t("diagnose_channel_unknown"), inline=False
        )
    else:
        channel_permissions = channel.permissions_for(me)
        check("diagnose_view_channel", channel_permissions.view_channel, "diagnose_grant_view")
        can_send = (
            channel_permissions.send_messages_in_threads
            if isinstance(channel, discord.Thread)
            else channel_permissions.send_messages
        )
        check("diagnose_send_messages", can_send, "diagnose_grant_send")
    report.add_field(
        name=bot.t("diagnose_nickname_hierarchy"),
        value=bot.t("diagnose_nickname_hint"),
        inline=False,
    )
    return report
