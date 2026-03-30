"""Discord bot setup."""

import asyncio
import re
import time

try:
    import discord
    from discord import app_commands
    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False

from .config import log, discover_conductors, conductor_session_title, get_conductor_names, get_unique_profiles
from .cli import run_cli, send_to_conductor, get_session_status, get_session_output, get_status_summary, get_status_summary_all, get_sessions_list, get_sessions_list_all, ensure_conductor_running
from .formatting import parse_conductor_prefix, split_message, send_discord_output
from .constants import RESPONSE_TIMEOUT, DISCORD_MAX_LENGTH

def create_discord_bot(config: dict):
    """Create and configure the Discord bot.

    Returns (client, channel_id) or None if Discord is not configured or discord.py unavailable.
    """
    if not HAS_DISCORD:
        log.warning("discord.py not installed, skipping Discord bot")
        return None
    if not config["discord"]["configured"]:
        return None

    bot_token = config["discord"]["bot_token"]
    guild_id = config["discord"]["guild_id"]
    channel_id = config["discord"]["channel_id"]
    authorized_user = config["discord"]["user_id"]
    listen_mode = str(config["discord"].get("listen_mode", "all") or "all").strip().lower()
    ignore_replies_to_others = bool(
        config["discord"].get("ignore_replies_to_others", False)
    )

    if listen_mode not in {"all", "mentions"}:
        log.warning("Unknown Discord listen_mode %r, falling back to 'all'", listen_mode)
        listen_mode = "all"

    intents = discord.Intents.default()
    intents.message_content = True

    class ConductorBot(discord.Client):
        def __init__(self):
            super().__init__(intents=intents)
            self.tree = app_commands.CommandTree(self)
            self.target_channel_id = channel_id
            self.authorized_user_id = authorized_user

        async def setup_hook(self):
            g = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
            log.info("Discord slash commands synced to guild %d", guild_id)

        async def on_ready(self):
            log.info(
                "Discord bot ready: %s (id=%d)", self.user, self.user.id
            )

    bot = ConductorBot()

    def is_authorized(user_id: int) -> bool:
        return user_id == authorized_user

    def message_mentions_bot(message: discord.Message) -> bool:
        if not bot.user:
            return False
        return any(getattr(user, "id", 0) == bot.user.id for user in message.mentions)

    def strip_bot_mentions(text: str) -> str:
        if not bot.user:
            return text.strip()
        return re.sub(rf"<@!?{bot.user.id}>", "", text).strip()

    async def should_ignore_reply_to_other(message: discord.Message) -> bool:
        if not ignore_replies_to_others:
            return False

        reference = getattr(message, "reference", None)
        reference_id = getattr(reference, "message_id", None)
        if not reference_id:
            return False

        referenced = getattr(reference, "resolved", None)
        if not isinstance(referenced, discord.Message):
            try:
                referenced = await message.channel.fetch_message(reference_id)
            except Exception as e:
                log.warning(
                    "Failed to resolve Discord reply target %d: %s",
                    reference_id, e,
                )
                return False

        if not bot.user:
            return False

        if referenced.author.id != bot.user.id:
            log.info(
                "Ignoring Discord reply to non-bot message %d from user %d",
                referenced.id, message.author.id,
            )
            return True
        return False

    async def ensure_discord_channel(interaction: discord.Interaction) -> bool:
        """Restrict slash commands to the configured channel."""
        if interaction.channel_id != channel_id:
            await interaction.response.send_message(
                "This command is only available in the configured channel.",
                ephemeral=True,
            )
            return False
        return True

    def get_default_conductor() -> dict | None:
        conductors = discover_conductors()
        return conductors[0] if conductors else None

    # Register slash commands
    g = discord.Object(id=guild_id)

    @bot.tree.command(
        name="ad-status",
        description="Aggregated status across all profiles",
        guild=g,
    )
    async def dc_cmd_status(interaction: discord.Interaction):
        if not is_authorized(interaction.user.id):
            await interaction.response.send_message(
                "Unauthorized.", ephemeral=True,
            )
            return
        if not await ensure_discord_channel(interaction):
            return

        profiles = get_unique_profiles()
        agg = get_status_summary_all(profiles)
        totals = agg["totals"]

        lines = [
            f"**Total:** {totals['total']} sessions",
            f"  Running: {totals['running']}",
            f"  Waiting: {totals['waiting']}",
            f"  Idle: {totals['idle']}",
            f"  Error: {totals['error']}",
        ]

        if len(profiles) > 1:
            lines.append("")
            for profile in profiles:
                p = agg["per_profile"][profile]
                lines.append(
                    f"[{profile}] {p['total']}s "
                    f"({p['running']}R {p['waiting']}W {p['idle']}I {p['error']}E)"
                )

        await interaction.response.send_message("\n".join(lines))

    @bot.tree.command(
        name="ad-sessions",
        description="List all sessions (all profiles)",
        guild=g,
    )
    async def dc_cmd_sessions(interaction: discord.Interaction):
        if not is_authorized(interaction.user.id):
            await interaction.response.send_message(
                "Unauthorized.", ephemeral=True,
            )
            return
        if not await ensure_discord_channel(interaction):
            return

        profiles = get_unique_profiles()
        all_sessions = get_sessions_list_all(profiles)
        if not all_sessions:
            await interaction.response.send_message("No sessions found.")
            return

        STATUS_ICONS = {
            "running": "\U0001f7e2",
            "waiting": "\U0001f7e1",
            "idle": "\u26aa",
            "error": "\U0001f534",
            "stopped": "\u23f9",
        }

        lines = []
        for profile, s in all_sessions:
            icon = STATUS_ICONS.get(s.get("status", ""), "\u2753")
            title = s.get("title", "untitled")
            tool = s.get("tool", "")
            prefix = f"[{profile}] " if len(profiles) > 1 else ""
            lines.append(f"{icon} {prefix}{title} ({tool})")

        text = "\n".join(lines)
        for i, chunk in enumerate(split_message(text, max_len=DISCORD_MAX_LENGTH)):
            if i == 0:
                await interaction.response.send_message(chunk)
            else:
                await interaction.followup.send(chunk)

    @bot.tree.command(
        name="ad-restart",
        description="Restart a conductor",
        guild=g,
    )
    @app_commands.describe(name="Conductor name (optional, defaults to first)")
    async def dc_cmd_restart(
        interaction: discord.Interaction, name: str = "",
    ):
        if not is_authorized(interaction.user.id):
            await interaction.response.send_message(
                "Unauthorized.", ephemeral=True,
            )
            return
        if not await ensure_discord_channel(interaction):
            return

        conductor_names = get_conductor_names()
        target = None
        if name and name in conductor_names:
            for c in discover_conductors():
                if c["name"] == name:
                    target = c
                    break
        if target is None:
            target = get_default_conductor()

        if target is None:
            await interaction.response.send_message("No conductors found.")
            return

        session_title = conductor_session_title(target["name"])
        await interaction.response.send_message(
            f"Restarting conductor {target['name']}...",
        )

        result = run_cli(
            "session", "restart", session_title,
            profile=target["profile"], timeout=60,
        )
        if result.returncode == 0:
            await interaction.followup.send(
                f"Conductor {target['name']} restarted.",
            )
        else:
            await interaction.followup.send(
                f"Restart failed: {result.stderr.strip()}",
            )

    @bot.tree.command(
        name="ad-help",
        description="Show conductor bridge help",
        guild=g,
    )
    async def dc_cmd_help(interaction: discord.Interaction):
        if not is_authorized(interaction.user.id):
            await interaction.response.send_message(
                "Unauthorized.", ephemeral=True,
            )
            return
        if not await ensure_discord_channel(interaction):
            return

        conductors = discover_conductors()
        names = [c["name"] for c in conductors]
        await interaction.response.send_message(
            "**Conductor Commands:**\n"
            "`/ad-status`    - Aggregated status across all profiles\n"
            "`/ad-sessions`  - List all sessions (all profiles)\n"
            "`/ad-restart`   - Restart a conductor (specify name)\n"
            "`/ad-help`      - This message\n\n"
            f"**Conductors:** {', '.join(names) if names else 'none'}\n"
            f"**Route:** `<name>: <message>`\n"
            f"**Default:** messages go to first conductor"
        )

    @bot.event
    async def on_message(message):
        # Ignore bot's own messages
        if message.author == bot.user:
            return
        # Ignore messages from other bots
        if message.author.bot:
            return
        # Only listen in the configured channel
        if message.channel.id != bot.target_channel_id:
            return
        # Authorization check
        if not is_authorized(message.author.id):
            log.warning(
                "Unauthorized Discord message from user %d",
                message.author.id,
            )
            return
        if await should_ignore_reply_to_other(message):
            return
        text = message.content
        if listen_mode == "mentions":
            if not message_mentions_bot(message):
                return
            text = strip_bot_mentions(text)
        # Ignore empty messages
        if not text:
            return

        conductor_names = get_conductor_names()
        conductors = discover_conductors()

        target_name, cleaned_msg = parse_conductor_prefix(
            text, conductor_names,
        )

        target = None
        if target_name:
            for c in conductors:
                if c["name"] == target_name:
                    target = c
                    break
        if target is None:
            target = get_default_conductor()
        if target is None:
            await message.channel.send(
                "[No conductors configured. Run: agent-deck conductor setup <name>]",
            )
            return

        if not cleaned_msg:
            cleaned_msg = text

        session_title = conductor_session_title(target["name"])
        profile = target["profile"]

        if not ensure_conductor_running(target["name"], profile):
            await message.channel.send(
                f"[Could not start conductor {target['name']}. Check agent-deck.]",
            )
            return

        log.info(
            "Discord message -> [%s]: %s",
            target["name"], cleaned_msg[:100],
        )
        async with message.channel.typing():
            loop = asyncio.get_event_loop()
            ok, response = await loop.run_in_executor(
                None,
                lambda: send_to_conductor(
                    session_title,
                    cleaned_msg,
                    profile=profile,
                    wait_for_reply=True,
                    response_timeout=RESPONSE_TIMEOUT,
                ),
            )
        if not ok:
            await message.channel.send(
                f"[Failed to send message to conductor {target['name']}.]",
            )
            return

        log.info(
            "Conductor [%s] response: %s",
            target["name"], response[:100],
        )

        name_tag = (
            f"[{target['name']}] " if len(conductors) > 1 else ""
        )
        await send_discord_output(message.channel, response, name_tag=name_tag)

    log.info(
        "Discord bot initialized (guild=%d, channel=%d)",
        guild_id, channel_id,
    )
    return bot, channel_id


