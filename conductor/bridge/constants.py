"""Shared constants, imports, and formatting utilities."""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import toml

# Conditional imports for Telegram
try:
    from aiogram import Bot, Dispatcher, types
    from aiogram.filters import Command, CommandStart
    HAS_AIOGRAM = True
except ImportError:
    HAS_AIOGRAM = False

# Conditional imports for Slack
try:
    from slack_bolt.async_app import AsyncApp
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
    from slack_bolt.authorization import AuthorizeResult
    from slack_sdk.web.async_client import AsyncWebClient
    HAS_SLACK = True
except ImportError:
    HAS_SLACK = False

# Conditional imports for Discord
try:
    import discord
    from discord import app_commands
    HAS_DISCORD = True
except ImportError:
    HAS_DISCORD = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AGENT_DECK_DIR = Path.home() / ".agent-deck"
CONFIG_PATH = AGENT_DECK_DIR / "config.toml"
CONDUCTOR_DIR = AGENT_DECK_DIR / "conductor"
LOG_PATH = CONDUCTOR_DIR / "bridge.log"

# Telegram message length limit
TG_MAX_LENGTH = 4096

# Slack message length limit
SLACK_MAX_LENGTH = 40000

# Discord message length limit
DISCORD_MAX_LENGTH = 2000

# Marker for uploading local images through the Discord bridge.
IMAGE_MARKER_RE = re.compile(r"\[IMAGE:(?P<path>[^\]]+)\]")

# How long to wait for conductor to respond (seconds)
RESPONSE_TIMEOUT = 300

# Tool name -> emoji icon for Slack progress messages
TOOL_LABELS = {
    "Bash": "\U0001f4bb", "Read": "\U0001f4d6", "Write": "\u270d\ufe0f",
    "Edit": "\u270d\ufe0f", "Grep": "\U0001f50d", "Glob": "\U0001f50d",
    "Agent": "\U0001f916", "WebSearch": "\U0001f310", "WebFetch": "\U0001f310",
    "TaskCreate": "\U0001f4cb", "TaskUpdate": "\U0001f4cb",
    "Skill": "\u2699\ufe0f", "SendMessage": "\U0001f4e8",
}

# Strip [from:...] [channel:...] or [dm] prefixes from bridge-enriched messages
_FROM_PREFIX_RE = re.compile(r"^\[from:[^\]]+\]\s*(?:\[(?:channel:[^\]]+|dm)\]\s*)?")

# Tool result messages to suppress in mirror (noise)
_SKIP_RESULTS = frozenset({
    "The file has been updated successfully.",
    "The file has been updated. All occurrences were successfully replaced.",
    "File created successfully",
})

# Code block language -> file extension
LANG_EXTENSIONS = {
    "python": "py", "typescript": "ts", "javascript": "js",
    "bash": "sh", "shell": "sh", "json": "json", "sql": "sql",
    "yaml": "yaml", "toml": "toml", "go": "go", "rust": "rs",
}

# ---------------------------------------------------------------------------
# Markdown -> Slack mrkdwn conversion
# ---------------------------------------------------------------------------


def markdown_to_slack(text: str) -> str:
    """Convert GitHub-flavored markdown to Slack mrkdwn format."""
    code_blocks = []
    def _save_code_block(m):
        code_blocks.append(m.group(0))
        return f"__CODE_BLOCK_{len(code_blocks) - 1}__"
    text = re.sub(r"```[\s\S]*?```", _save_code_block, text)

    inline_codes = []
    def _save_inline_code(m):
        inline_codes.append(m.group(0))
        return f"__INLINE_CODE_{len(inline_codes) - 1}__"
    text = re.sub(r"`[^`\n]+`", _save_inline_code, text)

    def _convert_table(m):
        return "```\n" + m.group(0).strip() + "\n```"
    text = re.sub(r"(?:^\|.+\|$\n?){2,}", _convert_table, text, flags=re.MULTILINE)

    text = re.sub(r"^-{3,}$", "\u2500" * 20, text, flags=re.MULTILINE)
    text = re.sub(r"^#{1,2}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"^#{3,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)
    text = re.sub(r"^(\s*)[-*]\s+", "\\1\u2022 ", text, flags=re.MULTILINE)

    # Escape bare angle brackets so Slack doesn't interpret them as links/mentions.
    # Preserve valid Slack links (<url|label>, <@U123>, <#C123>) by saving them first.
    slack_links = []
    def _save_slack_link(m):
        slack_links.append(m.group(0))
        return f"__SLACK_LINK_{len(slack_links) - 1}__"
    text = re.sub(r"<(?:[a-z]+://[^>]+|[@#!][^>]+)>", _save_slack_link, text)
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    for i, link in enumerate(slack_links):
        text = text.replace(f"__SLACK_LINK_{i}__", link)

    for i, code in enumerate(inline_codes):
        text = text.replace(f"__INLINE_CODE_{i}__", code)
    for i, block in enumerate(code_blocks):
        text = text.replace(f"__CODE_BLOCK_{i}__", block)

    return text
