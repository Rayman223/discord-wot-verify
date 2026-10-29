import discord


async def update_panel(bot, channel, view):
    """Called under the bot lock; only a confirmed deletion permits recreation."""
    saved = bot.store.panel(bot.config.guild_id)
    if saved:
        try:
            original = bot.get_channel(saved["channel_id"])
            if original is None:
                original = await bot.fetch_channel(saved["channel_id"])
            if original.guild.id != bot.config.guild_id:
                raise ValueError("panel_inaccessible")
            message = await original.fetch_message(saved["message_id"])
        except discord.NotFound:
            pass
        except discord.Forbidden:
            raise ValueError("panel_inaccessible") from None
        else:
            if original.id != channel.id:
                return bot.t("panel_elsewhere").format(channel_id=original.id)
            await message.edit(content=bot.t("panel"), view=view)
            return bot.t("panel_updated")
    message = await channel.send(bot.t("panel"), view=view)
    bot.store.save_panel(bot.config.guild_id, channel.id, message.id)
    return bot.t("setup_done")
