"""Agent-Deck CLI helpers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time

from .config import log, conductor_session_title, discover_conductors, get_conductor_names, get_unique_profiles
from .constants import AGENT_DECK_DIR, CONDUCTOR_DIR, RESPONSE_TIMEOUT

def run_cli(
    *args: str, profile: str | None = None, timeout: int = 120
) -> subprocess.CompletedProcess:
    """Run an agent-deck CLI command and return the result.

    If profile is provided, prepends -p <profile> to the command.
    """
    cmd = ["agent-deck"]
    if profile:
        cmd += ["-p", profile]
    cmd += list(args)
    log.debug("CLI: %s", " ".join(cmd))
    try:
        # Use Popen + communicate(timeout=) so we have the proc object available
        # when TimeoutExpired fires — subprocess.run() does NOT set exc.proc.
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,  # own process group → killpg kills grandchildren too
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            log.warning("CLI timeout: %s", " ".join(cmd))
            try:
                # Kill the entire process group so grandchildren (e.g. tmux send-keys)
                # don't survive as orphans and jam the pane's input queue.
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()  # fallback: kill direct child only
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return subprocess.CompletedProcess(cmd, 1, "", "timeout")
    except FileNotFoundError:
        log.error("agent-deck not found in PATH")
        return subprocess.CompletedProcess(cmd, 1, "", "not found")


def get_session_status(session: str, profile: str | None = None) -> str:
    """Get the status of a session (running/waiting/idle/error/stopped)."""
    result = run_cli(
        "session", "show", "--json", session, profile=profile, timeout=30
    )
    if result.returncode != 0:
        return "error"
    try:
        data = json.loads(result.stdout)
        return data.get("status", "error")
    except (json.JSONDecodeError, KeyError):
        return "error"


def get_session_output(session: str, profile: str | None = None) -> str:
    """Get the last response from a session."""
    result = run_cli(
        "session", "output", session, "-q", profile=profile, timeout=30
    )
    if result.returncode != 0:
        return f"[Error getting output: {result.stderr.strip()}]"
    return result.stdout.strip()


def send_to_conductor(
    session: str,
    message: str,
    profile: str | None = None,
    wait_for_reply: bool = False,
    response_timeout: int = RESPONSE_TIMEOUT,
) -> tuple[bool, str]:
    """Send a message to the conductor session.

    Returns (success, response_text). When wait_for_reply=False, response_text is "".
    """
    if wait_for_reply:
        # Single-call flow: send + wait + print raw response.
        # Avoids extra status/output polling round-trips.
        result = run_cli(
            "session", "send", session, message,
            "--wait", "--timeout", f"{response_timeout}s", "-q",
            profile=profile,
            timeout=max(response_timeout+30, 60),
        )
    else:
        result = run_cli(
            "session", "send", session, message, "--no-wait",
            profile=profile, timeout=30,
        )
    if result.returncode != 0:
        log.error(
            "Failed to send to conductor: %s", result.stderr.strip()
        )
        return False, ""
    return True, result.stdout.strip()


def get_session_data(session: str, profile: str | None = None) -> dict:
    """Get full session metadata as a dict from agent-deck."""
    result = run_cli("session", "show", "--json", session, profile=profile, timeout=10)
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, KeyError):
        return {}


def get_status_summary(profile: str | None = None) -> dict:
    """Get agent-deck status as a dict for a single profile."""
    result = run_cli("status", "--json", profile=profile, timeout=30)
    if result.returncode != 0:
        return {"waiting": 0, "running": 0, "idle": 0, "error": 0, "stopped": 0, "total": 0}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"waiting": 0, "running": 0, "idle": 0, "error": 0, "stopped": 0, "total": 0}


def get_status_summary_all(profiles: list[str]) -> dict:
    """Aggregate status across all profiles."""
    totals = {"waiting": 0, "running": 0, "idle": 0, "error": 0, "stopped": 0, "total": 0}
    per_profile = {}
    for profile in profiles:
        summary = get_status_summary(profile)
        per_profile[profile] = summary
        for key in totals:
            totals[key] += summary.get(key, 0)
    return {"totals": totals, "per_profile": per_profile}


def get_sessions_list(profile: str | None = None) -> list:
    """Get list of all sessions for a single profile."""
    result = run_cli("list", "--json", profile=profile, timeout=30)
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
        # list --json returns {"sessions": [...]}
        if isinstance(data, dict):
            return data.get("sessions", [])
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def get_sessions_list_all(profiles: list[str]) -> list[tuple[str, dict]]:
    """Get sessions from all profiles, each tagged with profile name."""
    all_sessions = []
    for profile in profiles:
        sessions = get_sessions_list(profile)
        for s in sessions:
            all_sessions.append((profile, s))
    return all_sessions


def ensure_conductor_running(name: str, profile: str) -> bool:
    """Ensure the conductor session exists and is running."""
    profile = profile or "default"
    session_title = conductor_session_title(name)
    status = get_session_status(session_title, profile=profile)

    if status == "error":
        log.info(
            "Conductor %s not running, attempting to start...", name,
        )
        # Try starting first (session might exist but be stopped)
        result = run_cli(
            "session", "start", session_title, profile=profile, timeout=60
        )
        if result.returncode != 0:
            # Session might not exist, try creating it
            log.info("Creating conductor session for %s...", name)
            session_path = str(CONDUCTOR_DIR / name)
            result = run_cli(
                "add", session_path,
                "-t", session_title,
                "-c", "claude",
                "-g", "conductor",
                profile=profile,
                timeout=60,
            )
            if result.returncode != 0:
                log.error(
                    "Failed to create conductor %s: %s",
                    name,
                    result.stderr.strip(),
                )
                return False
            # Start the newly created session
            run_cli(
                "session", "start", session_title,
                profile=profile, timeout=60,
            )

        # Wait a moment for the session to initialize
        time.sleep(5)
        return (
            get_session_status(session_title, profile=profile) != "error"
        )

    return True

