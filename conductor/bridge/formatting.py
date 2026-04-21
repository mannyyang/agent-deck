"""Message formatting, splitting, and routing utilities."""

from __future__ import annotations

import re
from pathlib import Path

from .config import log
from .constants import TG_MAX_LENGTH, DISCORD_MAX_LENGTH, IMAGE_MARKER_RE

# Conditional import for discord.File
try:
    import discord
except ImportError:
    discord = None

def parse_conductor_prefix(text: str, conductor_names: list[str]) -> tuple[str | None, str]:
    """Parse conductor name prefix from user message.

    Supports formats:
      <name>: <message>

    Returns (name_or_None, cleaned_message).
    """
    for name in conductor_names:
        prefix = f"{name}:"
        if text.startswith(prefix):
            return name, text[len(prefix):].strip()

    return None, text


# ---------------------------------------------------------------------------
# Message splitting
# ---------------------------------------------------------------------------


def split_message(text: str, max_len: int = TG_MAX_LENGTH) -> list[str]:
    """Split a long message into chunks that fit the platform limit."""
    if len(text) <= max_len:
        return [text]

    chunks = []
    while text:
        if len(text) <= max_len:
            chunks.append(text)
            break
        # Try to split at a newline
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1:
            # No newline found, split at max_len
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


def md_to_tg_html(text: str) -> str:
    """Convert markdown bold/italic/code to Telegram HTML and escape unsafe chars.

    Processes code spans first to protect their content from bold/italic conversion.
    """
    import html as _html

    # 1. Extract code spans before escaping (protect their content)
    code_spans: list[str] = []

    def _save_code(m: re.Match) -> str:
        code_spans.append(m.group(1))
        return f"\x00CODE{len(code_spans) - 1}\x00"

    text = re.sub(r'`(.+?)`', _save_code, text)

    # 2. Escape HTML special chars
    text = _html.escape(text, quote=False)

    # 3. Convert bold/italic (code spans are already replaced with placeholders)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', text)

    # 4. Restore code spans (escaped content wrapped in <code>)
    for i, code in enumerate(code_spans):
        text = text.replace(f"\x00CODE{i}\x00", f"<code>{_html.escape(code, quote=False)}</code>")

    return text


def parse_discord_message_parts(text: str) -> list[tuple[str, str]]:
    """Split Discord output into plain-text and image-upload segments."""
    parts = []
    last_idx = 0

    for match in IMAGE_MARKER_RE.finditer(text):
        if match.start() > last_idx:
            parts.append(("text", text[last_idx:match.start()]))

        image_path = match.group("path").strip()
        if image_path:
            parts.append(("image", image_path))
        last_idx = match.end()

    if last_idx < len(text):
        parts.append(("text", text[last_idx:]))

    if not parts:
        parts.append(("text", text))

    return parts


async def send_discord_output(channel, text: str, name_tag: str = ""):
    """Send Discord output, uploading [IMAGE:/path] markers as attachments."""
    prefix = name_tag if name_tag else ""
    attachment_content = name_tag.strip() if name_tag else None

    for part_type, payload in parse_discord_message_parts(text):
        if part_type == "text":
            if not payload.strip():
                continue
            for chunk in split_message(payload, max_len=DISCORD_MAX_LENGTH):
                prefixed = f"{prefix}{chunk}" if prefix else chunk
                await channel.send(prefixed)
            continue

        image_path = Path(payload).expanduser()
        if not image_path.is_absolute():
            warning = f"[Image path must be absolute: {payload}]"
            prefixed = f"{prefix}{warning}" if prefix else warning
            await channel.send(prefixed)
            continue
        if not image_path.is_file():
            warning = f"[Image not found: {image_path}]"
            prefixed = f"{prefix}{warning}" if prefix else warning
            await channel.send(prefixed)
            continue

        try:
            await channel.send(
                content=attachment_content,
                file=discord.File(str(image_path)),
            )
        except Exception as e:
            log.error("Failed to upload Discord image %s: %s", image_path, e)
            warning = f"[Failed to upload image: {image_path}]"
            prefixed = f"{prefix}{warning}" if prefix else warning
            await channel.send(prefixed)

