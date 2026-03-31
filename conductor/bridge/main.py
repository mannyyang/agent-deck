"""Entry point: discovers conductors, starts platform handlers, runs heartbeat loop.

This module replaces the monolithic bridge.py as the authoritative entry point.
"""

import asyncio
import sys

from .config import log, load_config, discover_conductors
from .cli import ensure_conductor_running
from .constants import AGENT_DECK_DIR, CONFIG_PATH, HAS_AIOGRAM, HAS_SLACK, HAS_DISCORD
from .telegram_bot import create_telegram_bot
from .slack_bot import create_slack_app
from .discord_bot import create_discord_bot
from .heartbeat import heartbeat_loop
from .mirror import mirror_loop

# Conditional import for Slack Socket Mode handler
try:
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
except ImportError:
    AsyncSocketModeHandler = None


async def main():
    log.info("Loading config from %s", CONFIG_PATH)
    config = load_config()

    conductors = discover_conductors()
    conductor_names = [c["name"] for c in conductors]

    # Verify at least one integration is configured and available
    tg_ok = config["telegram"]["configured"] and HAS_AIOGRAM
    sl_ok = config["slack"]["configured"] and HAS_SLACK
    dc_ok = config["discord"]["configured"] and HAS_DISCORD

    if not tg_ok and not sl_ok and not dc_ok:
        if config["telegram"]["configured"] and not HAS_AIOGRAM:
            log.error("Telegram configured but aiogram not installed. pip install aiogram")
        if config["slack"]["configured"] and not HAS_SLACK:
            log.error("Slack configured but slack-bolt not installed. pip install slack-bolt slack-sdk")
        if config["discord"]["configured"] and not HAS_DISCORD:
            log.error("Discord configured but discord.py not installed. pip install discord.py")
        if not config["telegram"]["configured"] and not config["slack"]["configured"] and not config["discord"]["configured"]:
            log.error("No messaging platform configured. Exiting.")
        sys.exit(1)

    platforms = []
    if tg_ok:
        platforms.append("Telegram")
    if sl_ok:
        platforms.append("Slack")
    if dc_ok:
        platforms.append("Discord")

    log.info(
        "Starting conductor bridge (platforms=%s, heartbeat=%dm, conductors=%s)",
        "+".join(platforms),
        config["heartbeat_interval"],
        ", ".join(conductor_names) if conductor_names else "none",
    )

    # Create Telegram bot
    telegram_bot, telegram_dp = None, None
    if tg_ok:
        result = create_telegram_bot(config)
        if result:
            telegram_bot, telegram_dp = result
            log.info("Telegram bot initialized (user_id=%d)", config["telegram"]["user_id"])

    # Create Slack app
    slack_app, slack_handler, slack_channel_id = None, None, None
    if sl_ok:
        result = create_slack_app(config)
        if result:
            slack_app, slack_channel_id = result
            if AsyncSocketModeHandler:
                slack_handler = AsyncSocketModeHandler(slack_app, config["slack"]["app_token"])

    # Create Discord bot
    discord_bot, discord_channel_id = None, None
    if dc_ok:
        result = create_discord_bot(config)
        if result:
            discord_bot, discord_channel_id = result

    # Pre-start all conductors so they're warm when messages arrive
    for c in conductors:
        if ensure_conductor_running(c["name"], c["profile"]):
            log.info("Conductor %s is running", c["name"])
        else:
            log.warning("Failed to pre-start conductor %s", c["name"])

    # Start heartbeat (shared, notifies all platforms)
    heartbeat_task = asyncio.create_task(
        heartbeat_loop(
            config,
            telegram_bot=telegram_bot,
            slack_app=slack_app,
            slack_channel_id=slack_channel_id,
            discord_bot=discord_bot,
            discord_channel_id=discord_channel_id,
        )
    )

    # Slack liveness watchdog: track last activity and exit if stale.
    # launchd KeepAlive=true will restart the process automatically.
    _slack_last_activity = {"ts": asyncio.get_event_loop().time()}
    _SLACK_STALE_TIMEOUT = 1800  # 30 minutes with no socket activity -> restart

    if slack_handler:
        _original_handle = slack_handler.handle

        async def _tracked_handle(*args, **kwargs):
            _slack_last_activity["ts"] = asyncio.get_event_loop().time()
            return await _original_handle(*args, **kwargs)

        slack_handler.handle = _tracked_handle

        # Also track Socket Mode connection events (connect/disconnect/reconnect)
        # so ping/pong keepalives reset the watchdog even without user messages.
        _original_connect = getattr(slack_handler, 'connect_async', None)
        if _original_connect:
            async def _tracked_connect(*args, **kwargs):
                _slack_last_activity["ts"] = asyncio.get_event_loop().time()
                return await _original_connect(*args, **kwargs)
            slack_handler.connect_async = _tracked_connect

        # Track the underlying client's message handler for ping/pong frames
        if hasattr(slack_handler, 'client') and slack_handler.client:
            _sm_client = slack_handler.client
            _original_recv = getattr(_sm_client, 'receive_messages', None)
            if _original_recv:
                async def _tracked_recv(*args, **kwargs):
                    _slack_last_activity["ts"] = asyncio.get_event_loop().time()
                    return await _original_recv(*args, **kwargs)
                _sm_client.receive_messages = _tracked_recv

    async def slack_liveness_watchdog():
        """Exit the process if Slack socket goes stale."""
        while True:
            await asyncio.sleep(60)
            elapsed = asyncio.get_event_loop().time() - _slack_last_activity["ts"]
            if elapsed > _SLACK_STALE_TIMEOUT:
                log.error(
                    "Slack socket stale for %.0fs (threshold %ds), exiting for restart",
                    elapsed, _SLACK_STALE_TIMEOUT,
                )
                import os
                os._exit(1)

    # Run all platforms concurrently
    tasks = [heartbeat_task]
    if telegram_dp and telegram_bot:
        tasks.append(asyncio.create_task(telegram_dp.start_polling(telegram_bot)))
        log.info("Telegram bot polling started")
    if slack_handler:
        tasks.append(asyncio.create_task(slack_handler.start_async()))
        tasks.append(asyncio.create_task(slack_liveness_watchdog()))
        log.info("Slack Socket Mode handler started")
        # Start mirror for the first Slack-connected conductor (from config)
        slack_conductors = config["slack"].get("conductors", [])
        mirror_conductor = None
        if slack_conductors:
            mirror_conductor = next((c for c in conductors if c["name"] == slack_conductors[0]), None)
        else:
            mirror_conductor = conductors[0] if conductors else None
        if mirror_conductor:
            tasks.append(asyncio.create_task(
                mirror_loop(slack_app, slack_channel_id, mirror_conductor["name"], mirror_conductor["profile"])
            ))
            log.info("Mirror started for conductor %s", mirror_conductor["name"])
    if discord_bot:
        tasks.append(asyncio.create_task(discord_bot.start(config["discord"]["bot_token"])))
        log.info("Discord bot started")

    try:
        await asyncio.gather(*tasks)
    finally:
        heartbeat_task.cancel()
        if telegram_bot:
            await telegram_bot.session.close()
        if slack_handler:
            await slack_handler.close_async()
        if discord_bot:
            await discord_bot.close()
