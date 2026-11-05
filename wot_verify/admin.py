"""Private administrator statistics and explicit import of existing players."""

import asyncio
import io
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

import aiohttp
import discord
from discord import app_commands

from .statistics import _snapshot, local_statistics
from .wargaming import CONTACT_RANKS

IO_ERRORS = (discord.HTTPException, aiohttp.ClientError, OSError, ValueError, TimeoutError)
OPERATION_TIMEOUT = 600
OPERATION_COOLDOWN = 60
LIVE_KEYS = (
    "members_checked",
    "linked_present",
    "linked_absent",
    "player_roles",
    "visitor_roles",
    "contact_roles",
    "players_unlinked",
    "linked_without_player",
    "player_visitor",
    "contact_without_player",
    "reservation_without_role",
    "role_without_reservation",
    "clan_changed",
    "rank_lost",
    "tag_mismatch",
    "nickname_mismatch",
    "wg_unknown",
    "importable",
    "desynchronized",
    "identity_conflict",
    "contact_conflict",
    "imported",
    "already_linked",
    "contacts_importable",
    "contacts_imported",
    "visitor_removed",
    "visitor_failed",
    "changed_during_scan",
    "import_failed",
)


def display_name(member):
    return member.nick or member.display_name


def role_ids(member):
    return {role.id for role in member.roles}


@dataclass
class Review:
    member_id: int
    name: str
    roles: set
    account_id: int | None = None
    nickname: str | None = None
    clan: tuple | None = None
    valid: bool = False
    contact_valid: bool = False
    linked: bool = False
    reasons: list = field(default_factory=list)


class Inspection:
    def __init__(self, bot):
        self.bot = bot
        self.metrics = dict.fromkeys(LIVE_KEYS, 0)
        self.reviews = []
        self.complete = False
        self.discord_complete = False
        self.members = {}
        self.total = 0
        self.processed = 0
        self.partial = False
        self._rate_lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(2)
        self._next_call = 0.0
        with _snapshot(bot.store.db):
            self.links = {
                row["discord_id"]: dict(row) for row in bot.store.db.execute("SELECT * FROM links")
            }
            self.contacts = {
                row["discord_id"]: dict(row)
                for row in bot.store.db.execute("SELECT * FROM clan_contacts")
            }

    async def api(self, method, *args):
        async with self._slots:
            async with self._rate_lock:
                now = time.monotonic()
                await asyncio.sleep(max(0, self._next_call - now))
                self._next_call = time.monotonic() + 0.5
            return await method(*args)

    async def review(self, member, link=None):
        config = self.bot.config
        roles = role_ids(member)
        result = Review(member.id, display_name(member), roles, linked=link is not None)
        suffix = re.fullmatch(r"(.+) \[([A-Za-z0-9_-]{2,5})\]", result.name)
        base, tag = (suffix[1], suffix[2]) if suffix else (result.name, None)
        try:
            if link is None:
                match = await self.api(self.bot.wg.find_exact, base)
                if match is None:
                    result.reasons.append("desynchronized")
                    return result
                result.account_id, result.nickname = match
            else:
                if link["region"] != config.region:
                    result.reasons.append("wg_unknown")
                    return result
                result.account_id = link["account_id"]
                result.nickname = await self.api(self.bot.wg.nickname, result.account_id)
            try:
                result.clan = await self.api(self.bot.wg.clan_membership, result.account_id)
            except IO_ERRORS:
                result.reasons.append("wg_unknown")
                return result
        except IO_ERRORS:
            result.reasons.append("wg_unknown")
            return result
        if tag is not None and (result.clan is None or tag != result.clan[2]):
            result.reasons.append("tag_mismatch")
        has_contact = getattr(config, "clan_role_id", 0) in roles
        expected = result.nickname
        if has_contact and result.clan:
            expected += f" [{result.clan[2]}]"
        if result.name != expected:
            result.reasons.append("nickname_mismatch")
        # Search is case insensitive, but no fuzzy or partial nickname is accepted.
        result.valid = (
            base.casefold() == result.nickname.casefold() and "tag_mismatch" not in result.reasons
        )
        if not result.valid:
            result.reasons.append("desynchronized")
        if has_contact or member.id in self.contacts:
            if result.clan is None:
                result.reasons.append("clan_changed")
            else:
                if result.clan[1] not in CONTACT_RANKS:
                    result.reasons.append("rank_lost")
                if tag != result.clan[2] and "tag_mismatch" not in result.reasons:
                    result.reasons.append("tag_mismatch")
                reservation = self.contacts.get(member.id)
                if reservation and (reservation["region"], reservation["clan_id"]) != (
                    config.region,
                    result.clan[0],
                ):
                    result.reasons.append("clan_changed")
                result.contact_valid = (
                    has_contact
                    and result.valid
                    and tag == result.clan[2]
                    and result.clan[1] in CONTACT_RANKS
                    and "clan_changed" not in result.reasons
                )
        return result

    async def review_absent_contact(self, discord_id):
        link = self.links[discord_id]
        result = Review(discord_id, "", set(), account_id=link["account_id"], linked=True)
        result.reasons.append("linked_absent")
        if link["region"] != self.bot.config.region:
            result.reasons.append("wg_unknown")
            return result
        try:
            result.clan = await self.api(self.bot.wg.clan_membership, link["account_id"])
        except IO_ERRORS:
            result.reasons.append("wg_unknown")
            return result
        reservation = self.contacts[discord_id]
        if result.clan is None or (reservation["region"], reservation["clan_id"]) != (
            self.bot.config.region,
            result.clan[0],
        ):
            result.reasons.append("clan_changed")
        if result.clan is not None and result.clan[1] not in CONTACT_RANKS:
            result.reasons.append("rank_lost")
        return result

    async def scan(self, guild):
        try:
            async for member in guild.fetch_members(limit=None):
                self.members[member.id] = member
        except IO_ERRORS:
            self.partial = True
        else:
            self.discord_complete = True
        self.metrics["members_checked"] = len(self.members)
        config = self.bot.config
        clan_role = getattr(config, "clan_role_id", 0)
        visitor_role = getattr(config, "visitor_role_id", 0)
        for member in self.members.values():
            roles = role_ids(member)
            player, contact = config.role_id in roles, clan_role in roles
            self.metrics["player_roles"] += player
            self.metrics["visitor_roles"] += visitor_role in roles
            self.metrics["contact_roles"] += contact
            self.metrics["players_unlinked"] += player and member.id not in self.links
            self.metrics["player_visitor"] += player and visitor_role in roles
            self.metrics["contact_without_player"] += contact and not player
            self.metrics["role_without_reservation"] += contact and member.id not in self.contacts
        for discord_id in self.links:
            member = self.members.get(discord_id)
            if member is not None:
                self.metrics["linked_present"] += 1
                self.metrics["linked_without_player"] += config.role_id not in role_ids(member)
            elif self.discord_complete:
                self.metrics["linked_absent"] += 1
        for discord_id in self.contacts:
            member = self.members.get(discord_id)
            if member is not None:
                self.metrics["reservation_without_role"] += clan_role not in role_ids(member)
            elif self.discord_complete:
                self.metrics["reservation_without_role"] += 1
        targets = [
            m
            for m in self.members.values()
            if not m.bot
            and (m.id in self.links or config.role_id in role_ids(m) or clan_role in role_ids(m))
        ]
        absent_contacts = (
            [i for i in self.contacts if i in self.links and i not in self.members]
            if self.discord_complete
            else []
        )
        self.total = len(targets) + len(absent_contacts)
        queue = asyncio.Queue()
        for member in targets:
            queue.put_nowait((member.id, member))
        for discord_id in absent_contacts:
            queue.put_nowait((discord_id, None))

        async def worker():
            while not queue.empty():
                discord_id, member = queue.get_nowait()
                review = (
                    await self.review(member, self.links.get(discord_id))
                    if member is not None
                    else await self.review_absent_contact(discord_id)
                )
                self.reviews.append(review)
                self.processed += 1
                for reason in set(review.reasons):
                    if reason != "linked_absent":
                        self.metrics[reason] += 1

        await asyncio.gather(worker(), worker())
        self.resolve_conflicts()
        self.complete = self.discord_complete

    def resolve_conflicts(self):
        config = self.bot.config
        eligible = [r for r in self.reviews if r.valid and config.role_id in r.roles]
        accounts = Counter(r.account_id for r in eligible)
        owners = {(r["region"], r["account_id"]): i for i, r in self.links.items()}
        clan_owners = {(r["region"], r["clan_id"]): i for i, r in self.contacts.items()}
        for review in eligible:
            owner = owners.get((config.region, review.account_id))
            if (owner is not None and owner != review.member_id) or (
                owner is None and accounts[review.account_id] > 1
            ):
                review.valid = False
                review.reasons.append("identity_conflict")
                self.metrics["identity_conflict"] += 1
                continue
            self.metrics["already_linked" if review.linked else "importable"] += 1
        eligible = [r for r in eligible if r.valid]
        clans = Counter(r.clan[0] for r in eligible if r.contact_valid)
        for review in eligible:
            if review.contact_valid:
                owner = clan_owners.get((config.region, review.clan[0]))
                if (owner is not None and owner != review.member_id) or (
                    owner is None and clans[review.clan[0]] > 1
                ):
                    review.contact_valid = False
                    review.reasons.append("contact_conflict")
                    self.metrics["contact_conflict"] += 1
                elif review.member_id not in self.contacts:
                    self.metrics["contacts_importable"] += 1

    async def apply(self):
        # A partial Discord enumeration cannot establish duplicate candidates safely.
        if not self.complete:
            return
        for review in self.reviews:
            if not review.valid or self.bot.config.role_id not in review.roles:
                continue
            try:
                fresh = await self.bot.member(review.member_id)
                checked = await self.review(fresh, self.links.get(review.member_id))
                if "wg_unknown" in checked.reasons:
                    self.metrics["wg_unknown"] += 1
                    self.metrics["import_failed"] += 1
                    review.reasons.extend(["wg_unknown", "import_failed"])
                    continue
                async with self.bot.lock:
                    fresh = await self.bot.member(review.member_id)
                    if (
                        role_ids(fresh) != review.roles
                        or display_name(fresh) != review.name
                        or not checked.valid
                        or checked.account_id != review.account_id
                        or (
                            review.contact_valid
                            and (not checked.contact_valid or checked.clan != review.clan)
                        )
                    ):
                        self.metrics["changed_during_scan"] += 1
                        review.reasons.append("changed_during_scan")
                        continue
                    current = self.bot.store.get(review.member_id)
                    snapshot = self.links.get(review.member_id)
                    if (dict(current) if current else None) != snapshot:
                        self.metrics["changed_during_scan"] += 1
                        review.reasons.append("changed_during_scan")
                        continue
                    had_contact = self.bot.store.contact(review.member_id) is not None
                    # A clan conflict never prevents an independently valid player link.
                    clan = checked.clan if review.contact_valid else None
                    if clan:
                        owner = self.bot.store.clan_contact(self.bot.config.region, clan[0])
                        existing = self.bot.store.contact(review.member_id)
                        if (owner and owner["discord_id"] != review.member_id) or (
                            existing
                            and (existing["region"], existing["clan_id"])
                            != (self.bot.config.region, clan[0])
                        ):
                            clan = None
                            review.reasons.append("contact_conflict")
                            self.metrics["contact_conflict"] += 1
                    self.bot.store.import_player(
                        review.member_id,
                        self.bot.config.region,
                        checked.account_id,
                        checked.nickname,
                        clan_id=clan[0] if clan else None,
                        tag=clan[2] if clan else None,
                    )
                    if current is None:
                        self.metrics["imported"] += 1
                        review.reasons.append("imported")
                    if clan and not had_contact:
                        self.metrics["contacts_imported"] += 1
                        review.reasons.append("contacts_imported")
                    if getattr(self.bot.config, "visitor_role_id", 0) in role_ids(fresh):
                        try:
                            await self.bot.sync_visitor_role(fresh, player=True)
                        except IO_ERRORS:
                            self.metrics["visitor_failed"] += 1
                            review.reasons.append("visitor_failed")
                        else:
                            self.metrics["visitor_removed"] += 1
                            review.reasons.append("visitor_removed")
            except IO_ERRORS:
                self.metrics["import_failed"] += 1
                review.reasons.append("import_failed")


def report_file(bot, local, inspection=None, *, applying=False):
    lines = [bot.t("admin_applied" if applying else "admin_preview"), bot.t("admin_local_note")]
    for key, value in local.items():
        if key in {"oldest", "newest"} and value is not None:
            value = datetime.fromtimestamp(value, UTC).isoformat()
        lines.append(
            f"{bot.t('metric_' + key)}: {value if value is not None else bot.t('status_never')}"
        )
    if inspection is not None:
        lines.extend(
            ["", bot.t("admin_coverage").format(done=inspection.processed, total=inspection.total)]
        )
        if not inspection.complete or inspection.partial:
            lines.append(bot.t("admin_partial"))
        for key, value in inspection.metrics.items():
            if key == "linked_absent" and not inspection.discord_complete:
                value = bot.t("status_unknown")
            if (key == "visitor_roles" and not getattr(bot.config, "visitor_role_id", 0)) or (
                key == "contact_roles" and not getattr(bot.config, "clan_role_id", 0)
            ):
                value = bot.t("status_disabled")
            lines.append(f"{bot.t('metric_' + key)}: {value}")
        for review in sorted(inspection.reviews, key=lambda r: r.member_id):
            reasons = [bot.t("metric_" + reason) for reason in dict.fromkeys(review.reasons)]
            if not reasons:
                reasons = [bot.t("admin_match")]
            lines.append(
                f"Discord {review.member_id} · WoT {review.account_id or '—'}: "
                + "; ".join(reasons)
            )
    return discord.File(io.BytesIO(("\n".join(lines) + "\n").encode()), filename="wot-report.txt")


def register_admin_commands(bot):
    running = False
    last_started = float("-inf")

    async def run(interaction, *, importing=False, confirm=False):
        nonlocal running, last_started
        await interaction.response.defer(ephemeral=True)
        if running or time.monotonic() - last_started < OPERATION_COOLDOWN:
            await interaction.followup.send(bot.t("admin_busy"), ephemeral=True)
            return
        if interaction.guild is None or interaction.guild_id != bot.config.guild_id:
            raise ValueError("wrong_server")
        running, last_started = True, time.monotonic()
        try:
            local = local_statistics(bot.store, int(time.time()))
            inspection = Inspection(bot)
            message = await interaction.followup.send(
                bot.t("admin_running"),
                file=report_file(bot, local),
                ephemeral=True,
                wait=True,
            )
            try:
                async with asyncio.timeout(OPERATION_TIMEOUT):
                    await inspection.scan(interaction.guild)
                    if importing and confirm:
                        await inspection.apply()
            except TimeoutError:
                inspection.partial = True
            await message.edit(
                content=bot.t("admin_applied" if importing and confirm else "admin_preview")
                + "\n"
                + bot.t("admin_read_only" if not (importing and confirm) else "admin_import_note"),
                attachments=[report_file(bot, local, inspection, applying=importing and confirm)],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        finally:
            running = False

    @bot.tree.command(description=bot.t("command_stats"))
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def stats(interaction: discord.Interaction):
        await run(interaction)

    @bot.tree.command(description=bot.t("command_admin_import_players"))
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def admin_import_players(interaction: discord.Interaction, confirm: bool = False):
        await run(interaction, importing=True, confirm=confirm)
