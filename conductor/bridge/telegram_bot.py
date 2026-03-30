"""Telegram bot setup."""

import re

try:
    from aiogram import Bot, Dispatcher, types
    from aiogram.filters import Command, CommandStart
    HAS_AIOGRAM = True
except ImportError:
    HAS_AIOGRAM = False

from .config import log, discover_conductors, conductor_session_title, get_conductor_names, get_unique_profiles
from .cli import run_cli, send_to_conductor, get_status_summary_all, get_sessions_list_all, ensure_conductor_running
from .formatting import parse_conductor_prefix, split_message, md_to_tg_html
from .constants import RESPONSE_TIMEOUT

def create_telegram_bot(config: dict):
    """Create and configure the Telegram bot.

    Returns (bot, dp) or None if Telegram is not configured or aiogram is not available.
    """
    if not HAS_AIOGRAM:
        log.warning("aiogram not installed, skipping Telegram bot")
        return None
    if not config["telegram"]["configured"]:
        return None

    bot = Bot(token=config["telegram"]["token"])
    dp = Dispatcher()
    authorized_user = config["telegram"]["user_id"]
    bot_info = {"username": ""}

    async def ensure_bot_info(bot_instance: Bot):
        """Lazy-init bot username on first message."""
        if not bot_info["username"]:
            me = await bot_instance.get_me()
            bot_info["username"] = me.username.lower()
            log.info("Bot username: @%s", bot_info["username"])

    def is_authorized(message: types.Message) -> bool:
        """Check if message is from the authorized user."""
        if message.from_user.id != authorized_user:
            log.warning(
                "Unauthorized message from user %d", message.from_user.id
            )
            return False
        return True

    def is_bot_addressed(message: types.Message) -> bool:
        """Check if message is directed at the bot (mention or reply in groups)."""
        if message.chat.type == "private":
            return True
        # Reply to the bot's own message
        if message.reply_to_message and message.reply_to_message.from_user:
            reply_username = message.reply_to_message.from_user.username
            if reply_username and reply_username.lower() == bot_info["username"]:
                return True
        # @mention in message entities
        if message.entities and message.text:
            for entity in message.entities:
                if entity.type == "mention":
                    mentioned = message.text[
                        entity.offset : entity.offset + entity.length
                    ].lower()
                    if mentioned == f"@{bot_info['username']}":
                        return True
        return False

    def strip_bot_mention(text: str) -> str:
        """Remove @botusername from message text."""
        if not bot_info["username"]:
            return text
        return re.sub(
            rf"@{re.escape(bot_info['username'])}\b",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

    def get_default_conductor() -> dict | None:
        """Get the first conductor (default target for messages)."""
        conductors = discover_conductors()
        return conductors[0] if conductors else None

    @dp.message(CommandStart())
    async def cmd_start(message: types.Message):
        if not is_authorized(message):
            return
        conductors = discover_conductors()
        names = [c["name"] for c in conductors]
        default = names[0] if names else "none"
        await message.answer(
            "Conductor bridge active.\n"
            f"Conductors: {', '.join(names) if names else 'none'}\n"
            "Commands: /status /sessions /help /restart\n"
            f"Route to conductor: <name>: <message>\n"
            f"Default conductor: {default}"
        )

    @dp.message(Command("status"))
    async def cmd_status(message: types.Message):
        if not is_authorized(message):
            return
        profiles = get_unique_profiles()
        agg = get_status_summary_all(profiles)
        totals = agg["totals"]

        lines = [
            f"Total: {totals['total']} sessions",
            f"  Running: {totals['running']}",
            f"  Waiting: {totals['waiting']}",
            f"  Idle: {totals['idle']}",
            f"  Error: {totals['error']}",
        ]

        # Per-profile breakdown (only if multiple profiles)
        if len(profiles) > 1:
            lines.append("")
            for profile in profiles:
                p = agg["per_profile"][profile]
                lines.append(
                    f"[{profile}] {p['total']}s "
                    f"({p['running']}R {p['waiting']}W {p['idle']}I {p['error']}E)"
                )

        await message.answer("\n".join(lines))

    @dp.message(Command("sessions"))
    async def cmd_sessions(message: types.Message):
        if not is_authorized(message):
            return
        profiles = get_unique_profiles()
        all_sessions = get_sessions_list_all(profiles)
        if not all_sessions:
            await message.answer("No sessions found.")
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

        await message.answer("\n".join(lines))

    @dp.message(Command("help"))
    async def cmd_help(message: types.Message):
        if not is_authorized(message):
            return
        conductors = discover_conductors()
        names = [c["name"] for c in conductors]
        await message.answer(
            "Conductor Commands:\n"
            "/status    - Aggregated status across all profiles\n"
            "/sessions  - List all sessions (all profiles)\n"
            "/restart   - Restart a conductor (specify name)\n"
            "/help      - This message\n\n"
            f"Conductors: {', '.join(names) if names else 'none'}\n"
            f"Route: <name>: <message>\n"
            f"Default: messages go to first conductor"
        )

    @dp.message(Command("restart"))
    async def cmd_restart(message: types.Message):
        if not is_authorized(message):
            return

        # Parse optional conductor name: /restart ryan
        text = message.text.strip()
        parts = text.split(None, 1)
        conductor_names = get_conductor_names()

        target = None
        if len(parts) > 1 and parts[1] in conductor_names:
            for c in discover_conductors():
                if c["name"] == parts[1]:
                    target = c
                    break
        if target is None:
            target = get_default_conductor()

        if target is None:
            await message.answer("No conductors found.")
            return

        session_title = conductor_session_title(target["name"])
        await message.answer(
            f"Restarting conductor {target['name']}..."
        )
        result = run_cli(
            "session", "restart", session_title,
            profile=target["profile"], timeout=60,
        )
        if result.returncode == 0:
            await message.answer(
                f"Conductor {target['name']} restarted."
            )
        else:
            await message.answer(
                f"Restart failed: {result.stderr.strip()}"
            )

    @dp.message()
    async def handle_message(message: types.Message):
        """Forward any text message to the conductor and return its response."""
        if not is_authorized(message):
            return
        if not message.text:
            return
        await ensure_bot_info(message.bot)
        if not is_bot_addressed(message):
            return

        # Strip @botname mention from group messages
        text = strip_bot_mention(message.text)
        if not text:
            return

        conductor_names = get_conductor_names()
        conductors = discover_conductors()

        # Determine target conductor from message prefix
        target_name, cleaned_msg = parse_conductor_prefix(
            text, conductor_names
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
            await message.answer("[No conductors configured. Run: agent-deck conductor setup <name>]")
            return

        if not cleaned_msg:
            cleaned_msg = text

        session_title = conductor_session_title(target["name"])
        profile = target["profile"]

        # Ensure conductor is running
        if not ensure_conductor_running(target["name"], profile):
            await message.answer(
                f"[Could not start conductor {target['name']}. Check agent-deck.]"
            )
            return

        # Send to conductor
        log.info(
            "User message -> [%s]: %s", target["name"], cleaned_msg[:100]
        )
        ok, response = send_to_conductor(
            session_title,
            cleaned_msg,
            profile=profile,
            wait_for_reply=True,
            response_timeout=RESPONSE_TIMEOUT,
        )
        if not ok:
            await message.answer(
                f"[Failed to send message to conductor {target['name']}.]"
            )
            return

        # Response is returned directly by session send --wait.
        name_tag = (
            f"[{target['name']}] " if len(conductors) > 1 else ""
        )
        await message.answer(f"{name_tag}...")  # typing indicator
        log.info("Conductor [%s] response: %s", target["name"], response[:100])

        # Convert to HTML first, then split to respect post-conversion length
        html_response = md_to_tg_html(
            f"{name_tag}{response}" if name_tag else response
        )
        for chunk in split_message(html_response):
            await message.answer(chunk, parse_mode="HTML")

    return bot, dp
