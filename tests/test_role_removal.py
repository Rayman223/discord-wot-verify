import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from wot_verify.bot import LoginView, VerifyBot
from wot_verify.config import Config
from wot_verify.role_removal import RoleRemovalView


@pytest.fixture
def env(tmp_path):
    bot = VerifyBot(
        Config(
            "token",
            "app",
            "x" * 32,
            1,
            2,
            "https://example.org",
            database=str(tmp_path / "withdrawal.sqlite3"),
            visitor_role_id=4,
            clan_role_id=3,
            locale="fr",
        )
    )
    roles = {
        i: SimpleNamespace(
            id=i, managed=False, is_default=lambda: False, permissions=discord.Permissions.none()
        )
        for i in (2, 3, 4, 5)
    }
    member = SimpleNamespace(
        id=7,
        nick="Player",
        roles=[roles[2], roles[5]],
        guild=SimpleNamespace(id=1, get_role=roles.get),
        edit=AsyncMock(),
    )

    async def add(role, **kwargs):
        if role.id not in {r.id for r in member.roles}:
            member.roles.append(role)

    async def remove(role, **kwargs):
        member.roles = [r for r in member.roles if r.id != role.id]

    member.add_roles = AsyncMock(side_effect=add)
    member.remove_roles = AsyncMock(side_effect=remove)
    bot.member = AsyncMock(return_value=member)
    bot.store.link(7, "eu", 100, "Player")
    yield bot, member, roles
    bot.store.close()


def interaction(*, user_id=7, guild_id=1):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild_id=guild_id,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def ids(member):
    return {role.id for role in member.roles}


async def contact_menu(env):
    bot, member, roles = env
    member.roles.append(roles[3])
    member.nick = "Player [TAG]"
    bot.store.reserve_contact(7, "eu", 42)
    bot.store.remember_tag(7, " [TAG]")
    click = interaction()
    await LoginView(bot).remove_role(click)
    assert click.followup.send.await_args.kwargs["ephemeral"] is True
    member.remove_roles.assert_not_awaited()
    view = click.followup.send.await_args.kwargs["view"]
    assert isinstance(view, RoleRemovalView)
    return view


async def player_menu(env):
    bot, member, _ = env
    before = list(bot.store.db.iterdump())
    click = interaction()
    await LoginView(bot).children[2].callback(click)
    member.remove_roles.assert_not_awaited()
    member.add_roles.assert_not_awaited()
    member.edit.assert_not_awaited()
    assert list(bot.store.db.iterdump()) == before
    assert click.followup.send.await_args.args == (bot.t("remove_player_confirmation"),)
    assert click.followup.send.await_args.kwargs["ephemeral"] is True
    view = click.followup.send.await_args.kwargs["view"]
    assert isinstance(view, RoleRemovalView)
    assert [button.label for button in view.children] == [
        bot.t("remove_player_confirm_button"),
        bot.t("remove_role_cancel_button"),
    ]
    return view


@pytest.mark.parametrize("locale", ["fr", "en"])
async def test_setup_updates_persistent_panel_with_withdrawal_button(env, locale):
    bot, _, _ = env
    bot.config = replace(bot.config, locale=locale)
    message = SimpleNamespace(id=30, edit=AsyncMock())
    channel = SimpleNamespace(
        id=20,
        guild=SimpleNamespace(id=1),
        send=AsyncMock(return_value=message),
        fetch_message=AsyncMock(return_value=message),
    )
    bot.get_channel = lambda _: channel
    click = interaction()
    click.channel = channel
    setup = bot.tree.get_command("setup")
    await setup.callback(click)
    await setup.callback(click)
    channel.send.assert_awaited_once()
    message.edit.assert_awaited_once()
    for call in (channel.send.await_args, message.edit.await_args):
        view = call.kwargs["view"]
        assert view.is_persistent()
        assert view.children[2].label == bot.t("remove_role_button")
        assert view.children[2].custom_id == "wot:remove-role:v1"


@pytest.mark.parametrize("linked", [True, False])
@pytest.mark.parametrize("locale", ["fr", "en"])
async def test_player_withdrawal_restores_visitor_and_preserves_other_roles(env, linked, locale):
    bot, member, _ = env
    bot.config = replace(bot.config, locale=locale)
    if not linked:
        bot.store.unlink(7)
    view = await player_menu(env)
    click = interaction()
    await view.children[0].callback(click)
    assert ids(member) == {4, 5}
    assert bot.store.get(7) is None
    result = click.edit_original_response.await_args.kwargs
    assert result["view"] is None
    assert bot.t("unlinked") in result["content"]
    assert bot.t("remove_role_visitor") in result["content"]
    assert view.is_finished()
    member.edit.assert_not_awaited()


@pytest.mark.parametrize("locale", ["fr", "en"])
async def test_player_withdrawal_cancel_keeps_roles_and_link_and_closes_confirmation(env, locale):
    bot, member, _ = env
    bot.config = replace(bot.config, locale=locale)
    before = list(bot.store.db.iterdump())
    view = await player_menu(env)
    click = interaction()
    await view.children[1].callback(click)
    assert ids(member) == {2, 5}
    assert list(bot.store.db.iterdump()) == before
    member.remove_roles.assert_not_awaited()
    member.add_roles.assert_not_awaited()
    click.edit_original_response.assert_awaited_once_with(
        content=bot.t("remove_role_cancelled"),
        view=None,
    )
    assert view.is_finished()
    replay = interaction()
    await view.children[0].callback(replay)
    member.remove_roles.assert_not_awaited()
    assert bot.store.get(7) is not None
    replay.followup.send.assert_awaited_once_with(bot.t("remove_role_expired"), ephemeral=True)


@pytest.mark.parametrize("action", [0, 1])
@pytest.mark.parametrize("wrong_owner", [False, True])
async def test_player_confirmation_buttons_reject_other_members_and_servers(
    env, action, wrong_owner
):
    bot, member, _ = env
    view = await player_menu(env)
    click = interaction(user_id=8) if wrong_owner else interaction(guild_id=99)
    await view.children[action].callback(click)
    key = "remove_role_owner_only" if wrong_owner else "wrong_server"
    click.response.send_message.assert_awaited_once_with(bot.t(key), ephemeral=True)
    assert bot.store.get(7) is not None
    member.remove_roles.assert_not_awaited()
    assert not view.is_finished()


async def test_contacts_choose_between_two_private_actions(env):
    bot, _, _ = env
    view = await contact_menu(env)
    assert [button.label for button in view.children] == [
        "supprimer tous les roles",
        "supprimer contact de clan",
    ]
    assert bot.store.get(7) is not None
    assert bot.store.contact(7) is not None


@pytest.mark.parametrize("contact_only", [True, False])
async def test_contact_choices_change_only_requested_roles(env, contact_only):
    bot, member, _ = env
    view = await contact_menu(env)
    click = interaction()
    await view.children[1 if contact_only else 0].callback(click)
    assert ids(member) == ({2, 5} if contact_only else {4, 5})
    assert (bot.store.get(7) is not None) is contact_only
    assert bot.store.contact(7) is None
    member.edit.assert_awaited_once_with(nick="Player", reason="Clan contact tag removed")
    assert bot.store.contact_tags(7) == []
    assert click.edit_original_response.await_args.kwargs["view"] is None
    assert view.is_finished()


async def test_manual_contact_role_without_reservation_can_be_removed(env):
    bot, member, roles = env
    member.roles.append(roles[3])
    member.nick = "Player [TAG]"
    click = interaction()
    await LoginView(bot).remove_role(click)
    view = click.followup.send.await_args.kwargs["view"]
    await view.remove_contact(interaction())
    assert ids(member) == {2, 5}
    assert bot.store.get(7) is not None
    member.edit.assert_awaited_once_with(nick="Player", reason="Clan contact tag removed")


@pytest.mark.parametrize("choice", [False, True])
async def test_other_server_never_modifies_roles(env, choice):
    bot, member, _ = env
    click = interaction(guild_id=99)
    if choice:
        await RoleRemovalView(bot, 7).remove_all(click)
    else:
        await LoginView(bot).remove_role(click)
    bot.member.assert_not_awaited()
    member.remove_roles.assert_not_awaited()
    click.response.send_message.assert_awaited_once_with(bot.t("wrong_server"), ephemeral=True)


@pytest.mark.parametrize("contact_only", [True, False])
async def test_private_choice_cannot_target_another_member(env, contact_only):
    bot, member, _ = env
    view = RoleRemovalView(bot, 7)
    click = interaction(user_id=8)
    await view.remove(click, contact_only=contact_only)
    bot.member.assert_not_awaited()
    member.remove_roles.assert_not_awaited()
    click.response.send_message.assert_awaited_once_with(
        bot.t("remove_role_owner_only"), ephemeral=True
    )


@pytest.mark.parametrize("contact_choice", [True, False])
async def test_expired_choice_does_not_modify_roles(env, contact_choice):
    bot, member, _ = env
    view = RoleRemovalView(bot, 7, contact_choice=contact_choice)
    view.expires_at = 0
    click = interaction()
    await view.remove_all(click)
    member.remove_roles.assert_not_awaited()
    click.followup.send.assert_awaited_once_with(bot.t("remove_role_expired"), ephemeral=True)


async def test_double_click_is_serialized_and_completed_menu_cannot_remove_new_link(env):
    bot, member, _ = env
    view = await contact_menu(env)
    first, second = interaction(), interaction()
    await asyncio.gather(view.remove_all(first), view.remove_contact(second))
    assert member.remove_roles.await_count == 2
    assert ids(member) == {4, 5}
    second.followup.send.assert_awaited_once_with(bot.t("remove_role_expired"), ephemeral=True)
    bot.store.link(7, "eu", 101, "NewPlayer")
    await view.remove_all(interaction())
    assert bot.store.get(7)["account_id"] == 101
    assert member.remove_roles.await_count == 2


@pytest.mark.parametrize("contact_only", [False, True])
async def test_discord_refusal_preserves_link_and_allows_choice_retry(env, contact_only):
    bot, member, _ = env
    view = await contact_menu(env)
    remove = member.remove_roles.side_effect
    member.remove_roles.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason=""), "")
    click = interaction()
    await view.remove(click, contact_only=contact_only)
    assert bot.store.get(7) is not None
    assert bot.store.contact(7) is not None
    assert not view.is_finished()
    click.edit_original_response.assert_not_awaited()
    click.followup.send.assert_awaited_once_with(bot.t("discord_error"), ephemeral=True)
    member.remove_roles.side_effect = remove
    await view.remove(interaction(), contact_only=contact_only)
    assert view.is_finished()


@pytest.mark.parametrize("contact_only", [False, True])
async def test_blocked_tag_reports_partial_result(env, contact_only):
    bot, member, _ = env
    view = await contact_menu(env)
    member.edit.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason=""), "")
    click = interaction()
    await view.remove(click, contact_only=contact_only)
    assert bot.store.contact(7) is None
    assert bot.store.contact_tags(7) == [" [TAG]"]
    assert ids(member) == ({2, 5} if contact_only else {4, 5})
    assert (bot.store.get(7) is not None) is contact_only
    key = "clan_tag_cleanup_error" if contact_only else "unlinked_tag_pending"
    assert bot.t(key) in click.edit_original_response.await_args.kwargs["content"]


async def test_visitor_assignment_failure_retains_link_and_can_be_retried(env):
    bot, member, _ = env
    add = member.add_roles.side_effect
    view = await player_menu(env)
    member.add_roles.side_effect = TimeoutError()
    click = interaction()
    await view.children[0].callback(click)
    assert bot.store.get(7) is not None
    assert not view.is_finished()
    click.followup.send.assert_awaited_once_with(bot.t("discord_error"), ephemeral=True)
    member.add_roles.side_effect = add
    await view.children[0].callback(interaction())
    assert ids(member) == {4, 5}
    assert bot.store.get(7) is None


async def test_unconfigured_visitor_blocks_full_withdrawal_but_not_contact_only(env):
    bot, member, _ = env
    bot.config = replace(bot.config, visitor_role_id=0)
    click = interaction()
    await LoginView(bot).remove_role(click)
    member.remove_roles.assert_not_awaited()
    assert bot.store.get(7) is not None
    assert bot.t("remove_role_visitor_required") in click.followup.send.await_args.args[0]
    view = await contact_menu(env)
    await view.remove_contact(interaction())
    assert ids(member) == {2, 5}
    assert bot.store.get(7) is not None


async def test_member_without_roles_or_link_has_nothing_to_remove(env):
    bot, member, roles = env
    bot.store.unlink(7)
    member.roles = [roles[4], roles[5]]
    click = interaction()
    await LoginView(bot).remove_role(click)
    member.remove_roles.assert_not_awaited()
    member.add_roles.assert_not_awaited()
    assert bot.t("remove_role_nothing") == click.followup.send.await_args.args[0]
