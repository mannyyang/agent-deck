"""Configuration loading and conductor discovery."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import toml

from .constants import AGENT_DECK_DIR, CONFIG_PATH, CONDUCTOR_DIR, LOG_PATH

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
log = logging.getLogger("conductor-bridge")

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def _resolve_secret(value: str) -> str:
    """Resolve a config value that may be an env-var reference or a macOS Keychain reference.

    Supports:
      - "$ENV_VAR" or "${ENV_VAR}" -> os.environ lookup
      - "keychain:service-name" -> macOS Keychain lookup via /usr/bin/security
      - Plain strings are returned as-is.
    """
    if not value:
        return value
    if value.startswith("$"):
        # Strip ${...} or $... syntax
        var_name = value.lstrip("$").strip("{}")
        resolved = os.environ.get(var_name, "")
        if not resolved:
            log.warning("Environment variable %s is not set", var_name)
        return resolved
    if value.startswith("keychain:"):
        service_name = value[len("keychain:"):]
        try:
            import subprocess
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", service_name, "-w"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            log.warning("Keychain lookup failed for service '%s': %s", service_name, result.stderr.strip())
        except Exception as e:
            log.warning("Keychain lookup error for service '%s': %s", service_name, e)
        return ""
    return value


def load_config() -> dict:
    """Load [conductor] section from config.toml.

    Returns a dict with nested 'telegram' and 'slack' sub-dicts,
    each with a 'configured' flag.
    """
    if not CONFIG_PATH.exists():
        log.error("Config not found: %s", CONFIG_PATH)
        sys.exit(1)

    config = toml.load(CONFIG_PATH)
    conductor_cfg = config.get("conductor", {})

    if not conductor_cfg.get("enabled", False):
        log.error("[conductor] section missing or not enabled in config.toml")
        sys.exit(1)

    # Telegram config
    tg = conductor_cfg.get("telegram", {})
    tg_token = _resolve_secret(tg.get("token", ""))
    tg_user_id = tg.get("user_id", 0)
    tg_configured = bool(tg_token and tg_user_id)

    # Slack config
    sl = conductor_cfg.get("slack", {})
    sl_bot_token = _resolve_secret(sl.get("bot_token", ""))
    sl_app_token = _resolve_secret(sl.get("app_token", ""))
    sl_channel_id = sl.get("channel_id", "")
    sl_listen_mode = sl.get("listen_mode", "mentions")  # "mentions" or "all"
    sl_allowed_users = sl.get("allowed_user_ids", [])  # List of authorized Slack user IDs
    sl_conductors = sl.get("conductors", [])  # Explicit list of conductor names for Slack (empty = all)
    sl_configured = bool(sl_bot_token and sl_app_token and sl_channel_id)

    # Discord config
    dc = conductor_cfg.get("discord", {})
    dc_bot_token = _resolve_secret(dc.get("bot_token", ""))
    dc_guild_id = dc.get("guild_id", 0)
    dc_channel_id = dc.get("channel_id", 0)
    dc_user_id = dc.get("user_id", 0)
    dc_listen_mode = dc.get("listen_mode", "all")  # "mentions" or "all"
    dc_ignore_replies_to_others = dc.get("ignore_replies_to_others", False)
    dc_configured = bool(dc_bot_token and dc_guild_id and dc_channel_id and dc_user_id)

    if not tg_configured and not sl_configured and not dc_configured:
        log.error(
            "No messaging platform configured in config.toml. "
            "Set [conductor.telegram], [conductor.slack], or [conductor.discord]."
        )
        sys.exit(1)

    return {
        "telegram": {
            "token": tg_token,
            "user_id": int(tg_user_id) if tg_user_id else 0,
            "configured": tg_configured,
        },
        "slack": {
            "bot_token": sl_bot_token,
            "app_token": sl_app_token,
            "channel_id": sl_channel_id,
            "listen_mode": sl_listen_mode,
            "allowed_user_ids": sl_allowed_users,
            "conductors": sl_conductors,
            "configured": sl_configured,
        },
        "discord": {
            "bot_token": dc_bot_token,
            "guild_id": int(dc_guild_id) if dc_guild_id else 0,
            "channel_id": int(dc_channel_id) if dc_channel_id else 0,
            "user_id": int(dc_user_id) if dc_user_id else 0,
            "listen_mode": dc_listen_mode,
            "ignore_replies_to_others": bool(dc_ignore_replies_to_others),
            "configured": dc_configured,
        },
        "heartbeat_interval": conductor_cfg.get("heartbeat_interval", 15),
    }


def discover_conductors() -> list[dict]:
    """Discover all conductors by scanning meta.json files."""
    conductors = []
    if not CONDUCTOR_DIR.exists():
        return conductors
    for entry in CONDUCTOR_DIR.iterdir():
        if entry.is_dir():
            meta_path = entry / "meta.json"
            if meta_path.exists():
                try:
                    with open(meta_path) as f:
                        meta = json.load(f)
                    if not isinstance(meta, dict):
                        continue
                    # Backward compatibility: normalize missing fields.
                    meta["name"] = meta.get("name") or entry.name
                    meta["profile"] = meta.get("profile") or "default"
                    conductors.append(meta)
                except (json.JSONDecodeError, IOError) as e:
                    log.warning("Failed to read %s: %s", meta_path, e)
    return conductors


def conductor_session_title(name: str) -> str:
    """Return the conductor session title for a given conductor name."""
    return f"conductor-{name}"


def get_conductor_names() -> list[str]:
    """Get list of all conductor names."""
    return [c["name"] for c in discover_conductors()]


def get_unique_profiles() -> list[str]:
    """Get unique profile names from all conductors."""
    profiles = set()
    for c in discover_conductors():
        profiles.add(c.get("profile") or "default")
    return sorted(profiles)


def select_heartbeat_conductors(conductors: list[dict]) -> list[dict]:
    """Select all heartbeat-enabled conductors in deterministic order."""
    enabled = [c for c in conductors if c.get("heartbeat_enabled", True)]
    return sorted(
        enabled,
        key=lambda c: (
            str(c.get("profile") or "default"),
            str(c.get("created_at", "")),
            str(c.get("name", "")),
        ),
    )
