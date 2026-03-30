"""Heartbeat loop for periodic conductor checks."""

import asyncio
import os
import time
from pathlib import Path

from .config import log, discover_conductors, conductor_session_title, select_heartbeat_conductors, get_unique_profiles
from .cli import run_cli, send_to_conductor, get_session_status, get_sessions_list, get_status_summary_all, get_sessions_list_all, ensure_conductor_running
from .formatting import split_message, md_to_tg_html, send_discord_output
from .constants import TG_MAX_LENGTH, SLACK_MAX_LENGTH, DISCORD_MAX_LENGTH, RESPONSE_TIMEOUT

def _os_heartbeat_daemon_installed() -> bool:
    """Check if an OS-level heartbeat daemon (launchd or systemd) is installed."""
    import platform
    home = os.path.expanduser("~")
    if platform.system() == "Darwin":
        # Check for any launchd plist matching the heartbeat pattern
        agents_dir = os.path.join(home, "Library", "LaunchAgents")
        if os.path.isdir(agents_dir):
            for f in os.listdir(agents_dir):
                if f.startswith("com.agentdeck.conductor-heartbeat.") and f.endswith(".plist"):
                    return True
    else:
        # Check for any systemd timer matching the heartbeat pattern
        timers_dir = os.path.join(home, ".config", "systemd", "user")
        if os.path.isdir(timers_dir):
            for f in os.listdir(timers_dir):
                if f.startswith("agent-deck-conductor-heartbeat-") and f.endswith(".timer"):
                    return True
    return False


async def heartbeat_loop(
    config: dict, telegram_bot=None, slack_app=None, slack_channel_id=None,
    discord_bot=None, discord_channel_id=None,
):
    """Periodic heartbeat: check status for each conductor and trigger checks."""
    global_interval = config["heartbeat_interval"]
    if global_interval <= 0:
        log.info("Heartbeat disabled (interval=0)")
        return

    if _os_heartbeat_daemon_installed():
        log.info("OS heartbeat daemon detected, bridge heartbeat loop disabled (avoiding double-trigger)")
        return

    interval_seconds = global_interval * 60
    tg_user_id = config["telegram"]["user_id"] if config["telegram"]["configured"] else None

    log.info("Heartbeat loop started (global interval: %d minutes)", global_interval)

    while True:
        await asyncio.sleep(interval_seconds)

        all_conductors = discover_conductors()
        conductors = select_heartbeat_conductors(all_conductors)
        for conductor in conductors:
            try:
                name = conductor.get("name", "")
                profile = conductor.get("profile") or "default"
                if not name:
                    continue

                session_title = conductor_session_title(name)

                # Scope heartbeat monitoring to this conductor's own group.
                sessions = get_sessions_list(profile)
                scoped_sessions = []
                for s in sessions:
                    s_title = s.get("title", "untitled")
                    s_group = s.get("group", "") or ""
                    if s_title.startswith("conductor-"):
                        continue
                    if s_group != name and not s_group.startswith(f"{name}/"):
                        continue
                    scoped_sessions.append(s)

                waiting = sum(1 for s in scoped_sessions if s.get("status", "") == "waiting")
                running = sum(1 for s in scoped_sessions if s.get("status", "") == "running")
                idle = sum(1 for s in scoped_sessions if s.get("status", "") == "idle")
                error = sum(1 for s in scoped_sessions if s.get("status", "") == "error")
                stopped = sum(1 for s in scoped_sessions if s.get("status", "") == "stopped")

                log.info(
                    "Heartbeat [%s/%s]: %d waiting, %d running, %d idle, %d error, %d stopped",
                    name, profile, waiting, running, idle, error, stopped,
                )

                # Only trigger conductor if there are waiting or error sessions
                if waiting == 0 and error == 0:
                    continue

                # Build heartbeat message with waiting session details
                waiting_details = []
                error_details = []
                for s in scoped_sessions:
                    s_title = s.get("title", "untitled")
                    s_status = s.get("status", "")
                    s_path = s.get("path", "")
                    if s_status == "waiting":
                        waiting_details.append(
                            f"{s_title} (project: {s_path})"
                        )
                    elif s_status == "error":
                        error_details.append(
                            f"{s_title} (project: {s_path})"
                        )

                parts = [
                    f"[HEARTBEAT] [{name}] Status: {waiting} waiting, "
                    f"{running} running, {idle} idle, {error} error, {stopped} stopped."
                ]
                if waiting_details:
                    parts.append(
                        f"Waiting sessions: {', '.join(waiting_details)}."
                    )
                if error_details:
                    parts.append(
                        f"Error sessions: {', '.join(error_details)}."
                    )
                parts.append(
                    "Check if any need auto-response or user attention."
                )

                heartbeat_msg = " ".join(parts)

                # Ensure conductor is running
                if not ensure_conductor_running(name, profile):
                    log.error(
                        "Heartbeat [%s]: conductor not running, skipping",
                        name,
                    )
                    continue

                # Send heartbeat to conductor
                ok, response = send_to_conductor(
                    session_title,
                    heartbeat_msg,
                    profile=profile,
                    wait_for_reply=True,
                    response_timeout=RESPONSE_TIMEOUT,
                )
                if not ok:
                    log.error(
                        "Heartbeat [%s]: failed to send to conductor",
                        name,
                    )
                    continue

                # Response is returned directly by session send --wait.
                log.info(
                    "Heartbeat [%s] response: %s",
                    name, response[:200],
                )

                # If conductor flagged items needing attention, notify via Telegram and Slack
                if "NEED:" in response:
                    prefix = (
                        f"[{name}] " if len(all_conductors) > 1 else ""
                    )
                    alert_msg = f"{prefix}Conductor alert:\n{response}"

                    # Notify via Telegram
                    if telegram_bot and tg_user_id:
                        try:
                            alert_html = md_to_tg_html(alert_msg)
                            for chunk in split_message(alert_html):
                                await telegram_bot.send_message(
                                    tg_user_id, chunk, parse_mode="HTML",
                                )
                        except Exception as e:
                            log.error(
                                "Failed to send Telegram notification: %s", e
                            )

                    # Notify via Slack
                    if slack_app and slack_channel_id:
                        try:
                            await slack_app.client.chat_postMessage(
                                channel=slack_channel_id, text=alert_msg,
                            )
                        except Exception as e:
                            log.error(
                                "Failed to send Slack notification: %s", e
                            )

                    # Notify via Discord
                    if discord_bot and discord_channel_id:
                        try:
                            channel = discord_bot.get_channel(
                                discord_channel_id,
                            )
                            if channel:
                                await send_discord_output(channel, alert_msg)
                        except Exception as e:
                            log.error(
                                "Failed to send Discord notification: %s",
                                e,
                            )

            except Exception as e:
                log.error("Heartbeat [%s] error: %s", conductor.get("name", "?"), e)

