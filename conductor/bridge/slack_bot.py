"""Slack bot setup."""

from __future__ import annotations

import asyncio
import re
import time

try:
    from slack_bolt.async_app import AsyncApp
    from slack_bolt.authorization import AuthorizeResult
    from slack_sdk.web.async_client import AsyncWebClient
    HAS_SLACK = True
except ImportError:
    HAS_SLACK = False

from .config import log, discover_conductors, conductor_session_title, get_conductor_names, get_unique_profiles
from .cli import run_cli, send_to_conductor, get_status_summary_all, get_sessions_list_all, ensure_conductor_running
from .formatting import parse_conductor_prefix
from .constants import markdown_to_slack, RESPONSE_TIMEOUT, CONDUCTOR_DIR

def create_slack_app(config: dict):
    """Create and configure the Slack app with Socket Mode.

    Returns (app, channel_id) or None if Slack is not configured or slack-bolt is not available.
    """
    if not HAS_SLACK:
        log.warning("slack-bolt not installed, skipping Slack app")
        return None
    if not config["slack"]["configured"]:
        return None

    bot_token = config["slack"]["bot_token"]
    channel_id = config["slack"]["channel_id"]

    # Cache auth.test() result to avoid calling it on every event.
    # The default SingleTeamAuthorization middleware calls auth.test()
    # per-event until it succeeds; if the Slack API is slow after a
    # Socket Mode reconnect, this causes cascading TimeoutErrors.
    _auth_cache: dict = {}
    _auth_lock = asyncio.Lock()

    async def _cached_authorize(**kwargs):
        async with _auth_lock:
            if "result" in _auth_cache:
                return _auth_cache["result"]
            client = AsyncWebClient(token=bot_token, timeout=30)
            for attempt in range(3):
                try:
                    resp = await client.auth_test()
                    _auth_cache["result"] = AuthorizeResult(
                        enterprise_id=resp.get("enterprise_id"),
                        team_id=resp.get("team_id"),
                        bot_user_id=resp.get("user_id"),
                        bot_id=resp.get("bot_id"),
                        bot_token=bot_token,
                    )
                    return _auth_cache["result"]
                except Exception as e:
                    log.warning("Slack auth.test attempt %d/3 failed: %s", attempt + 1, e)
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)
            raise RuntimeError("Slack auth.test failed after 3 attempts")

    app = AsyncApp(token=bot_token, authorize=_cached_authorize)
    listen_mode = config["slack"].get("listen_mode", "mentions")

    # Authorization setup
    allowed_users = config["slack"]["allowed_user_ids"]

    def is_slack_authorized(user_id: str) -> bool:
        """Check if Slack user is authorized to use the bot.

        If allowed_user_ids is empty, allow all users (backward compatible).
        Otherwise, only allow users in the list.
        """
        if not allowed_users:  # Empty list = no restrictions
            return True
        if user_id not in allowed_users:
            log.warning("Unauthorized Slack message from user %s", user_id)
            return False
        return True

    # Caches for Slack user/channel name resolution.
    # Entries: (value: str, expires_at: float | None).
    # Successful lookups never expire; failures expire after 5 minutes.
    _NEGATIVE_TTL = 300  # seconds
    _user_cache: dict[str, tuple[str, float | None]] = {}
    _channel_cache: dict[str, tuple[str, float | None]] = {}

    def _cache_get(cache: dict, key: str) -> str | None:
        entry = cache.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and time.monotonic() > expires_at:
            del cache[key]
            return None
        return value

    async def resolve_slack_username(user_id: str) -> str:
        """Resolve a Slack user ID to a display name, with caching."""
        cached = _cache_get(_user_cache, user_id)
        if cached is not None:
            return cached
        try:
            resp = await app.client.users_info(user=user_id)
            profile = resp["user"]["profile"]
            name = profile.get("display_name") or profile.get("real_name") or user_id
            _user_cache[user_id] = (name, None)
            return name
        except Exception as e:
            log.warning("Failed to resolve Slack user %s: %s", user_id, e)
            _user_cache[user_id] = (user_id, time.monotonic() + _NEGATIVE_TTL)
            return user_id

    async def resolve_slack_channel(event_channel: str) -> str:
        """Resolve a Slack channel ID to a context tag.

        Returns '[channel:#name (ID)]' for channels or '[dm]' for DMs.
        """
        cached = _cache_get(_channel_cache, event_channel)
        if cached is not None:
            return cached
        try:
            resp = await app.client.conversations_info(channel=event_channel)
            ch = resp["channel"]
            if ch.get("is_im"):
                tag = "[dm]"
            else:
                name = ch.get("name", event_channel)
                tag = f"[channel:#{name} ({event_channel})]"
            _channel_cache[event_channel] = (tag, None)
            return tag
        except Exception as e:
            log.warning("Failed to resolve Slack channel %s: %s", event_channel, e)
            tag = f"[channel:{event_channel}]"
            _channel_cache[event_channel] = (tag, time.monotonic() + _NEGATIVE_TTL)
            return tag

    # Conductors allowed for this Slack integration (empty = all)
    _slack_conductors = config["slack"].get("conductors", [])

    def get_allowed_conductors() -> list[dict]:
        """Get conductors allowed for this Slack integration."""
        conductors = discover_conductors()
        if not _slack_conductors:
            return conductors
        return [c for c in conductors if c["name"] in _slack_conductors]

    def get_default_conductor() -> dict | None:
        """Get the first allowed conductor (default target for messages)."""
        allowed = get_allowed_conductors()
        return allowed[0] if allowed else None

    async def _safe_say(say, **kwargs):
        """Wrapper around say() that catches network/API errors and converts markdown."""
        if "text" in kwargs:
            kwargs["text"] = markdown_to_slack(kwargs["text"])
        try:
            result = await say(**kwargs)
            return result
        except Exception as e:
            log.error("Slack say() failed: %s", e)
            return None

    async def _add_reaction(channel: str, timestamp: str, emoji: str):
        """Add an emoji reaction to a message."""
        try:
            await app.client.reactions_add(channel=channel, timestamp=timestamp, name=emoji)
        except Exception as e:
            log.debug("Failed to add reaction %s: %s", emoji, e)

    async def _remove_reaction(channel: str, timestamp: str, emoji: str):
        """Remove an emoji reaction from a message."""
        try:
            await app.client.reactions_remove(channel=channel, timestamp=timestamp, name=emoji)
        except Exception as e:
            log.debug("Failed to remove reaction %s: %s", emoji, e)

    async def _handle_slack_text(
        text: str, say, thread_ts: str = None,
        user_id: str = None, event_channel: str = None,
    ):
        """Shared handler for Slack messages and mentions."""
        msg_channel = event_channel or channel_id

        conductors = get_allowed_conductors()
        conductor_names = [c["name"] for c in conductors]

        target_name, cleaned_msg = parse_conductor_prefix(text, conductor_names)

        target = None
        if target_name:
            for c in conductors:
                if c["name"] == target_name:
                    target = c
                    break
        if target is None:
            target = get_default_conductor()
        if target is None:
            await _safe_say(
                say,
                text="[No conductors configured. Run: agent-deck conductor setup <name>]",
                thread_ts=thread_ts,
            )
            return

        if not cleaned_msg:
            cleaned_msg = text

        # Enrich message with sender and channel context for the conductor.
        prefix_parts = []
        if user_id and event_channel:
            username, channel_tag = await asyncio.gather(
                resolve_slack_username(user_id),
                resolve_slack_channel(event_channel),
            )
            prefix_parts.append(f"[from:{username} ({user_id})]")
            prefix_parts.append(channel_tag)
        elif user_id:
            username = await resolve_slack_username(user_id)
            prefix_parts.append(f"[from:{username} ({user_id})]")
        elif event_channel:
            channel_tag = await resolve_slack_channel(event_channel)
            prefix_parts.append(channel_tag)
        if prefix_parts:
            cleaned_msg = " ".join(prefix_parts) + " " + cleaned_msg

        session_title = conductor_session_title(target["name"])
        profile = target["profile"]

        if not ensure_conductor_running(target["name"], profile):
            await _safe_say(
                say,
                text=f"[Could not start conductor {target['name']}. Check agent-deck.]",
                thread_ts=thread_ts,
            )
            return

        log.info("Slack message -> [%s]: %s", target["name"], cleaned_msg[:100])

        # React to acknowledge receipt
        msg_ts = thread_ts
        if msg_ts:
            await _add_reaction(msg_channel, msg_ts, "eyes")

        # Send to conductor (no-wait). The JSONL mirror handles posting the response.
        loop = asyncio.get_running_loop()
        ok, _ = await loop.run_in_executor(
            None,
            lambda: send_to_conductor(
                session_title,
                cleaned_msg,
                profile=profile,
                wait_for_reply=False,
            ),
        )

        if not ok and msg_ts:
            await _remove_reaction(msg_channel, msg_ts, "eyes")
            await _add_reaction(msg_channel, msg_ts, "warning")

    @app.event("message")
    async def handle_slack_message(event, say):
        """Handle messages in the configured channel.

        Only active when listen_mode is "all". Ignored in "mentions" mode.
        """
        if listen_mode != "all":
            return
        # Ignore bot messages
        if event.get("bot_id") or event.get("subtype"):
            return
        # Only listen in configured channel
        if event.get("channel") != channel_id:
            return

        # Authorization check
        user_id = event.get("user", "")
        if not is_slack_authorized(user_id):
            return

        text = event.get("text", "").strip()
        if not text:
            return
        await _handle_slack_text(
            text, say,
            thread_ts=event.get("thread_ts") or event.get("ts"),
            user_id=user_id,
            event_channel=event.get("channel"),
        )

    @app.event("app_mention")
    async def handle_slack_mention(event, say):
        """Handle @bot mentions in any channel the bot is in. Always active."""

        # Authorization check
        user_id = event.get("user", "")
        if not is_slack_authorized(user_id):
            return

        text = event.get("text", "")
        # Strip the bot mention (e.g., "<@U01234> message" -> "message")
        text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()
        if not text:
            return
        thread_ts = event.get("thread_ts") or event.get("ts")
        await _handle_slack_text(
            text, say,
            thread_ts=thread_ts,
            user_id=user_id,
            event_channel=event.get("channel"),
        )

    def _resolve_conductor(name: str) -> dict | None:
        """Resolve a conductor by name, falling back to default."""
        if name:
            for c in discover_conductors():
                if c['name'] == name:
                    return c
        return get_default_conductor()

    @app.command("/ad-status")
    async def slack_cmd_status(ack, respond, command):
        """Handle /ad-status slash command."""
        await ack()

        # Authorization check
        user_id = command.get("user_id", "")
        if not is_slack_authorized(user_id):
            await respond("⛔ Unauthorized. Contact your administrator.")
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

        if len(profiles) > 1:
            lines.append("")
            for profile in profiles:
                p = agg["per_profile"][profile]
                lines.append(
                    f"[{profile}] {p['total']}s "
                    f"({p['running']}R {p['waiting']}W {p['idle']}I {p['error']}E)"
                )

        await respond("\n".join(lines))

    @app.command("/ad-sessions")
    async def slack_cmd_sessions(ack, respond, command):
        """Handle /ad-sessions slash command."""
        await ack()

        # Authorization check
        user_id = command.get("user_id", "")
        if not is_slack_authorized(user_id):
            await respond("⛔ Unauthorized. Contact your administrator.")
            return

        profiles = get_unique_profiles()
        all_sessions = get_sessions_list_all(profiles)
        if not all_sessions:
            await respond("No sessions found.")
            return

        lines = []
        for profile, s in all_sessions:
            title = s.get("title", "untitled")
            status = s.get("status", "unknown")
            tool = s.get("tool", "")
            prefix = f"[{profile}] " if len(profiles) > 1 else ""
            lines.append(f"  {prefix}{title} ({tool}) - {status}")

        await respond("\n".join(lines))

    @app.command("/ad-restart")
    async def slack_cmd_restart(ack, respond, command):
        """Handle /ad-restart slash command."""
        await ack()

        # Authorization check
        user_id = command.get("user_id", "")
        if not is_slack_authorized(user_id):
            await respond("⛔ Unauthorized. Contact your administrator.")
            return

        target_name = command.get("text", "").strip()
        conductor_names = get_conductor_names()

        target = None
        if target_name and target_name in conductor_names:
            for c in discover_conductors():
                if c["name"] == target_name:
                    target = c
                    break
        if target is None:
            target = get_default_conductor()

        if target is None:
            await respond("No conductors found.")
            return

        session_title = conductor_session_title(target["name"])
        await respond(f"Restarting conductor {target['name']}...")
        result = run_cli(
            "session", "restart", session_title,
            profile=target["profile"], timeout=60,
        )
        if result.returncode == 0:
            await respond(f"Conductor {target['name']} restarted.")
        else:
            await respond(f"Restart failed: {result.stderr.strip()}")

    @app.command("/ad-compact")
    async def slack_cmd_compact(ack, respond, command):
        """Handle /ad-compact slash command."""
        await ack()
        user_id = command.get("user_id", "")
        if not is_slack_authorized(user_id):
            await respond("⛔ Unauthorized. Contact your administrator.")
            return
        target = _resolve_conductor(command.get("text", "").strip())
        if target is None:
            await respond("No conductors found.")
            return
        session_title = conductor_session_title(target["name"])
        await respond(f'Compacting `{target["name"]}`...')
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda t=target: run_cli("session", "restart", session_title, profile=t["profile"], timeout=60),
        )
        if result.returncode == 0:
            await respond(f'`{target["name"]}` compacted. State recovers from state.json on startup.')
        else:
            await respond(f"Compact failed: {result.stderr.strip()}")

    @app.command("/ad-clear")
    async def slack_cmd_clear(ack, respond, command):
        """Handle /ad-clear slash command."""
        await ack()
        user_id = command.get("user_id", "")
        if not is_slack_authorized(user_id):
            await respond("⛔ Unauthorized. Contact your administrator.")
            return
        target = _resolve_conductor(command.get("text", "").strip())
        if target is None:
            await respond("No conductors found.")
            return
        state_path = CONDUCTOR_DIR / target["name"] / "state.json"
        try:
            state_path.write_text("{}")
        except Exception:
            pass
        session_title = conductor_session_title(target["name"])
        await respond(f'Clearing `{target["name"]}`...')
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda t=target: run_cli("session", "restart", session_title, profile=t["profile"], timeout=60),
        )
        if result.returncode == 0:
            await respond(f'`{target["name"]}` cleared and restarted fresh.')
        else:
            await respond(f"Clear failed: {result.stderr.strip()}")

    @app.command("/ad-check")
    async def slack_cmd_check(ack, respond, command):
        """Handle /ad-check slash command."""
        await ack()
        user_id = command.get("user_id", "")
        if not is_slack_authorized(user_id):
            await respond("⛔ Unauthorized. Contact your administrator.")
            return
        session_name = command.get("text", "").strip()
        if not session_name:
            await respond("Usage: `/ad-check <session-name>`")
            return
        profiles = get_unique_profiles()
        output = None
        for profile in profiles:
            result = run_cli("session", "output", session_name, "-q", profile=profile, timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                output = result.stdout.strip()
                break
        if output is None:
            await respond(f"Session `{session_name}` not found or has no output.")
            return
        max_len = 3000
        if len(output) > max_len:
            output = "...(truncated)\n" + output[-max_len:]
        await respond(f"*{session_name}* last output:\n```\n{output}\n```")

    @app.command("/ad-send")
    async def slack_cmd_send(ack, respond, command):
        """Handle /ad-send slash command."""
        await ack()
        user_id = command.get("user_id", "")
        if not is_slack_authorized(user_id):
            await respond("⛔ Unauthorized. Contact your administrator.")
            return
        text = command.get("text", "").strip()
        parts = text.split(None, 1)
        if len(parts) < 2:
            await respond("Usage: `/ad-send <session-name> <message>`")
            return
        session_name, message = parts[0], parts[1]
        profiles = get_unique_profiles()
        sent = False
        loop = asyncio.get_running_loop()
        for profile in profiles:
            result = await loop.run_in_executor(
                None,
                lambda p=profile: run_cli("session", "send", session_name, message, "--no-wait", profile=p, timeout=30),
            )
            if result.returncode == 0:
                sent = True
                break
        if sent:
            preview = message[:100] + ("..." if len(message) > 100 else "")
            await respond(f"Sent to `{session_name}`: {preview}")
        else:
            await respond(f"Failed to send to `{session_name}`. Session may not exist or is not running.")

    @app.command("/ad-help")
    async def slack_cmd_help(ack, respond, command):
        """Handle /ad-help slash command."""
        await ack()

        # Authorization check
        user_id = command.get("user_id", "")
        if not is_slack_authorized(user_id):
            await respond("⛔ Unauthorized. Contact your administrator.")
            return

        conductors = discover_conductors()
        names = [c["name"] for c in conductors]
        await respond(
            "*Conductor Commands:*\n"
            "`/ad-status`              - Aggregated status across all profiles\n"
            "`/ad-sessions`            - List all sessions (all profiles)\n"
            "`/ad-check <session>`     - Show last output from a session\n"
            "`/ad-send <session> <msg>` - Send message directly to a session\n"
            "`/ad-compact [conductor]` - Compact conductor (restart, state preserved)\n"
            "`/ad-clear [conductor]`   - Full reset (wipe state + restart)\n"
            "`/ad-restart [conductor]` - Restart a conductor\n"
            "`/ad-help`                - This message\n\n"
            f"*Conductors:* {', '.join(names) if names else 'none'}\n"
            f"*Route:* `<name>: <message>` to target a specific conductor\n"
            f"*Default:* messages go to first conductor"
        )

    log.info("Slack app initialized (Socket Mode, channel=%s)", channel_id)
    return app, channel_id
