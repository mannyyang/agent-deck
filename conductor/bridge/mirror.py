"""JSONL-based terminal mirror for continuous Slack sync."""

import asyncio
import json
import re
import time
from pathlib import Path

from .config import log, conductor_session_title
from .cli import get_session_data
from .formatting import split_message
from .constants import TOOL_LABELS, _FROM_PREFIX_RE, _SKIP_RESULTS, SLACK_MAX_LENGTH, markdown_to_slack

# XML tags injected by Claude Code internals — strip entirely from mirror output
_INTERNAL_XML_RE = re.compile(
    r"</?(?:system-reminder|task-notification|local-command-caveat|command-name"
    r"|command-message|command-args|local-command-stdout|fast_mode_info"
    r"|antml_\w+)(?:\s[^>]*)?>",
)

def _contains_internal_xml(text: str) -> bool:
    """Return True if text is predominantly internal XML (skip it)."""
    return bool(_INTERNAL_XML_RE.search(text))

def _strip_internal_xml(text: str) -> str:
    """Remove internal XML tags and their content blocks from text."""
    # Remove full blocks: <tag>...</tag>
    text = re.sub(
        r"<(?:system-reminder|task-notification|local-command-caveat|command-name"
        r"|command-message|command-args|local-command-stdout|fast_mode_info)"
        r"(?:\s[^>]*)?>.*?</(?:system-reminder|task-notification|local-command-caveat"
        r"|command-name|command-message|command-args|local-command-stdout|fast_mode_info)>",
        "", text, flags=re.DOTALL,
    )
    # Remove any remaining standalone tags
    text = _INTERNAL_XML_RE.sub("", text)
    return text.strip()

def resolve_jsonl_path(session_title: str, profile: str | None = None) -> str:
    """Resolve the Claude JSONL file path for a session."""
    data = get_session_data(session_title, profile=profile)
    claude_id = data.get("claude_session_id", "")
    if not claude_id:
        return ""
    # Search ~/.claude/projects/ for the JSONL file (called once at startup)
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return ""
    for d in claude_dir.iterdir():
        if d.is_dir():
            jsonl = d / f"{claude_id}.jsonl"
            if jsonl.exists():
                return str(jsonl)
    return ""


def format_jsonl_event(entry: dict) -> str | None:
    """Format a single JSONL entry for Slack. Returns None to skip."""
    entry_type = entry.get("type", "")

    # Skip progress events (hooks, internal)
    if entry_type == "progress":
        return None

    msg = entry.get("message", {})
    if not msg:
        return None

    role = msg.get("role", "")
    content = msg.get("content", "")

    if role == "user":
        if isinstance(content, str):
            if not content.strip():
                return None
            if _contains_internal_xml(content):
                return None
            text = _FROM_PREFIX_RE.sub("", content).strip()
            if not text:
                return None
            return f"> {text[:500]}"
        elif isinstance(content, list):
            parts = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text = item.get("text", "").strip()
                    if text and not _contains_internal_xml(text):
                        text = _FROM_PREFIX_RE.sub("", text).strip()
                        if text:
                            parts.append(f"> {text[:500]}")
                elif item.get("type") == "tool_result":
                    result_content = item.get("content", "")
                    if isinstance(result_content, str):
                        stripped = result_content.strip()
                        # Skip noise like "file updated successfully" or internal XML
                        if not stripped or any(stripped.startswith(s) for s in _SKIP_RESULTS):
                            continue
                        if _contains_internal_xml(stripped):
                            stripped = _strip_internal_xml(stripped)
                            if not stripped:
                                continue
                        preview = stripped[:2000]
                        if len(stripped) > 2000:
                            preview += "\n... (truncated)"
                        parts.append(f"```\n{preview}\n```")
            return "\n".join(parts) if parts else None

    elif role == "assistant":
        if isinstance(content, list):
            parts = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text = item.get("text", "").strip()
                    if text:
                        if _contains_internal_xml(text):
                            text = _strip_internal_xml(text)
                        if text:
                            parts.append(text)
                elif item.get("type") == "tool_use":
                    tool_name = item.get("name", "unknown")
                    tool_input = item.get("input", {})
                    icon = TOOL_LABELS.get(tool_name, "\U0001f527")
                    if not isinstance(tool_input, dict):
                        parts.append(f"{icon} `{tool_name}`")
                        continue

                    if tool_name == "Bash":
                        cmd = tool_input.get("command", "")
                        desc = tool_input.get("description", "")
                        if desc:
                            parts.append(f"{icon} *{desc}*")
                        if cmd:
                            parts.append(f"```\n$ {cmd[:500]}\n```")
                        elif not desc:
                            parts.append(f"{icon} `Bash`")
                    elif tool_name == "Edit":
                        fp = tool_input.get("file_path", "")
                        old = tool_input.get("old_string", "")
                        new = tool_input.get("new_string", "")
                        old_count = len(old.splitlines()) if old else 0
                        new_count = len(new.splitlines()) if new else 0
                        replace_all = tool_input.get("replace_all", False)
                        desc = f"Edit({fp})"
                        if replace_all:
                            desc += " [replace all]"
                        if old_count or new_count:
                            desc += f" (-{old_count}/+{new_count} lines)"
                        parts.append(f"{icon} `{desc}`")
                    elif tool_name == "Write":
                        fp = tool_input.get("file_path", "")
                        parts.append(f"{icon} `Write({fp})`")
                    elif tool_name == "Read":
                        fp = tool_input.get("file_path", "")
                        parts.append(f"\U0001f4d6 `Read({fp})`")
                    elif tool_name in ("Grep", "Glob"):
                        pattern = tool_input.get("pattern", "")
                        parts.append(f"\U0001f50d `{tool_name}({pattern})`")
                    else:
                        # Generic: show first meaningful arg
                        if "file_path" in tool_input:
                            parts.append(f"{icon} `{tool_name}({tool_input['file_path']})`")
                        elif "query" in tool_input:
                            parts.append(f"{icon} `{tool_name}({tool_input['query'][:80]})`")
                        elif "prompt" in tool_input:
                            parts.append(f"{icon} `{tool_name}({tool_input['prompt'][:80]})`")
                        else:
                            parts.append(f"{icon} `{tool_name}`")
            return "\n".join(parts) if parts else None
        elif isinstance(content, str) and content.strip():
            return content.strip()

    return None


async def mirror_loop(
    slack_app,
    channel_id: str,
    conductor_name: str,
    profile: str,
):
    """Continuously mirror conductor-chief output to Slack by tailing the JSONL file."""
    POLL_INTERVAL = 2  # seconds
    BATCH_INTERVAL = 2  # seconds to accumulate before posting

    session_title = conductor_session_title(conductor_name)

    # Resolve JSONL path
    jsonl_path = resolve_jsonl_path(session_title, profile=profile)
    if not jsonl_path:
        log.warning("Mirror: could not resolve JSONL path for %s, retrying in 30s", conductor_name)
        await asyncio.sleep(30)
        jsonl_path = resolve_jsonl_path(session_title, profile=profile)
        if not jsonl_path:
            log.error("Mirror: giving up on JSONL resolution for %s", conductor_name)
            return

    log.info("Mirror: tailing JSONL %s for conductor %s", jsonl_path, conductor_name)

    # Seek to end of file (skip history)
    file_path = Path(jsonl_path)
    file_pos = file_path.stat().st_size if file_path.exists() else 0
    last_inode = file_path.stat().st_ino if file_path.exists() else 0

    pending_messages: list[str] = []
    last_post_time = time.monotonic()

    async def flush_pending():
        nonlocal last_post_time
        if not pending_messages:
            return
        batch = markdown_to_slack("\n".join(pending_messages))
        pending_messages.clear()
        for chunk in split_message(batch, max_len=SLACK_MAX_LENGTH):
            try:
                await slack_app.client.chat_postMessage(channel=channel_id, text=chunk)
            except Exception as e:
                log.debug("Mirror: failed to post: %s", e)
        last_post_time = time.monotonic()

    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL)

            if not file_path.exists():
                continue

            # Detect file rotation (session restart/compact)
            current_stat = file_path.stat()
            if current_stat.st_ino != last_inode or current_stat.st_size < file_pos:
                log.info("Mirror: JSONL file rotated, reseeking")
                file_pos = current_stat.st_size
                last_inode = current_stat.st_ino
                continue

            if current_stat.st_size <= file_pos:
                if (time.monotonic() - last_post_time) >= BATCH_INTERVAL:
                    await flush_pending()
                continue

            # Read new bytes
            with open(file_path, "r", encoding="utf-8") as f:
                f.seek(file_pos)
                new_data = f.read()
                file_pos = f.tell()

            for line in new_data.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                formatted = format_jsonl_event(entry)
                if formatted:
                    pending_messages.append(formatted)

            if (time.monotonic() - last_post_time) >= BATCH_INTERVAL:
                await flush_pending()

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error("Mirror: unexpected error: %s", e)
            await asyncio.sleep(10)


