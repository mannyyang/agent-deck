"""Regression suite for fork-local conductor-bridge customizations.

This suite is BOTH this port's acceptance gate AND future-merge protection: if a
future upstream merge drops or overwrites one of our customizations in the
embedded bridge (``internal/session/conductor_bridge.py``), the matching test
fails loudly.

One test (or group) per customization:

  BEHAVIORAL (mocks/stubs):
    1. _resolve_secret           — keychain ref resolves; plain string passes through
    2. run_cli robustness        — start_new_session=True + errors="replace" decode
    3. /ad-* slash commands      — registered + dispatch to the right CLI subcommand
    4. multi-conductor filtering — mirror target selection (configured vs unconfigured)
    5. no-double-logging         — one inbound message -> exactly one routing log record
  PRESENCE/SMOKE (heavy to mock):
    6. slack liveness watchdog   — function exists, scheduled, _SLACK_STALE_TIMEOUT == 1800
    7. voice STT                 — BRIDGE_STT_ENABLED gate + transcription path present

Import mechanics: the canonical bridge is embedded via go:embed. We load it from
internal/session/conductor_bridge.py under the module name ``bridge`` and stub the
heavy third-party deps (aiogram / slack_bolt / slack_sdk / discord / toml) so the
module imports in CI without those installed. conftest.py already loads the same
file as ``bridge`` for the rest of the suite; this file's stubs are installed at
import time and are tolerant of conftest having loaded first.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import logging
import subprocess
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import shim: stub heavy deps, then load the canonical embedded bridge.
# ---------------------------------------------------------------------------

_CANONICAL = (
    Path(__file__).resolve().parents[2] / "internal" / "session" / "conductor_bridge.py"
)


def _install_dep_stubs() -> None:
    """Register minimal stand-ins so `HAS_AIOGRAM`/`HAS_SLACK` are True at import.

    Non-clobbering: a dependency that is genuinely installed (e.g. aiogram in CI)
    is left untouched; we only fabricate modules that are absent. We never modify
    the shared ``bridge`` module that conftest loads — our bridge instance loads
    under a private name (see ``_load_bridge``).
    """
    def _stub(name: str) -> types.ModuleType | None:
        """Create+register a stub module only if `name` isn't already importable."""
        if name in sys.modules:
            return None  # real (or already-stubbed) — don't touch
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    if (toml := _stub("toml")) is not None:
        toml.load = lambda *a, **k: {}

    if (aiogram := _stub("aiogram")) is not None:
        aiogram.Bot = type("Bot", (), {})
        aiogram.Dispatcher = type("Dispatcher", (), {})
        aiogram.types = sys.modules.setdefault("aiogram.types", types.ModuleType("aiogram.types"))
        aiogram.types.Message = type("Message", (), {})
    if (filters := _stub("aiogram.filters")) is not None:
        filters.Command = lambda *a, **k: None
        filters.CommandStart = lambda *a, **k: None
    _stub("aiogram.client")
    _stub("aiogram.client.session")
    if (sess := _stub("aiogram.client.session.aiohttp")) is not None:
        sess.AiohttpSession = type("AiohttpSession", (), {})

    _stub("slack_bolt")
    if (async_app := _stub("slack_bolt.async_app")) is not None:
        async_app.AsyncApp = _FakeAsyncApp
    _stub("slack_bolt.adapter")
    _stub("slack_bolt.adapter.socket_mode")
    if (sm := _stub("slack_bolt.adapter.socket_mode.async_handler")) is not None:
        sm.AsyncSocketModeHandler = type("AsyncSocketModeHandler", (), {})
    if (authz := _stub("slack_bolt.authorization")) is not None:
        authz.AuthorizeResult = lambda **k: dict(**k)
    _stub("slack_sdk")
    _stub("slack_sdk.web")
    if (web := _stub("slack_sdk.web.async_client")) is not None:
        web.AsyncWebClient = type("AsyncWebClient", (), {})


class _FakeAsyncApp:
    """Captures @app.command / @app.event handlers and exposes a fake client."""

    def __init__(self, *args, **kwargs):
        self.commands: dict[str, callable] = {}
        self.events: dict[str, callable] = {}
        self.client = _FakeSlackClient()

    def command(self, name):
        def deco(fn):
            self.commands[name] = fn
            return fn
        return deco

    def event(self, name):
        def deco(fn):
            self.events[name] = fn
            return fn
        return deco


class _FakeSlackClient:
    async def users_info(self, user=None, **k):
        return {"user": {"profile": {"display_name": "tester", "real_name": "tester"}}}

    async def conversations_info(self, channel=None, **k):
        return {"channel": {"name": "ops", "is_im": False}}

    async def chat_postMessage(self, **k):
        return {"ok": True}


_PRIVATE_NAME = "conductor_bridge_customtest"


def _load_bridge():
    """Load the canonical bridge under a private module name with deps stubbed.

    We deliberately do NOT register it as ``bridge`` so we never disturb the
    instance conftest loads for the rest of the suite. Loading fresh (with the
    slack_bolt stub installed first) guarantees HAS_SLACK is True for our tests.
    """
    _install_dep_stubs()
    if _PRIVATE_NAME in sys.modules:
        return sys.modules[_PRIVATE_NAME]
    spec = importlib.util.spec_from_file_location(_PRIVATE_NAME, _CANONICAL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PRIVATE_NAME] = module
    spec.loader.exec_module(module)
    return module


bridge = _load_bridge()


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_CHANNEL = "C0TEST"
_CONDUCTORS = [
    {"name": "chief", "profile": "default"},
    {"name": "scout", "profile": "work"},
]


def _slack_config(conductors=None, listen_mode="mentions"):
    return {
        "slack": {
            "bot_token": "xoxb-test",
            "app_token": "xapp-test",
            "channel_id": _CHANNEL,
            "listen_mode": listen_mode,
            "allowed_user_ids": [],  # empty -> all users authorized
            "conductors": conductors or [],
            "configured": True,
        },
    }


def _make_slack_app(monkeypatch, config):
    """Stub the module-level routing deps and build the Slack app via create_slack_app."""
    monkeypatch.setattr(bridge, "get_conductor_names", lambda: [c["name"] for c in _CONDUCTORS])
    monkeypatch.setattr(bridge, "discover_conductors", lambda: list(_CONDUCTORS))
    monkeypatch.setattr(bridge, "get_default_conductor", lambda: _CONDUCTORS[0])
    monkeypatch.setattr(bridge, "get_unique_profiles", lambda: ["default", "work"])
    monkeypatch.setattr(bridge, "get_session_status", lambda *a, **k: "idle")

    async def _running(*a, **k):
        return True

    monkeypatch.setattr(bridge, "ensure_conductor_running", _running)
    return bridge.create_slack_app(config)


# ===========================================================================
# 1. _resolve_secret (behavioral)
# ===========================================================================

def test_resolve_secret_keychain(monkeypatch):
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="s3cr3t\n", stderr="")

    monkeypatch.setattr(bridge.subprocess, "run", fake_run)
    assert bridge._resolve_secret("keychain:my-bot-token") == "s3cr3t"
    # the right keychain service name was queried
    assert "my-bot-token" in calls["cmd"]
    assert "/usr/bin/security" in calls["cmd"]


def test_resolve_secret_plain_passthrough():
    assert bridge._resolve_secret("xoxb-literal-token") == "xoxb-literal-token"
    assert bridge._resolve_secret("") == ""


def test_resolve_secret_env_ref(monkeypatch):
    monkeypatch.setenv("BRIDGE_TEST_TOKEN", "from-env")
    assert bridge._resolve_secret("$BRIDGE_TEST_TOKEN") == "from-env"
    assert bridge._resolve_secret("${BRIDGE_TEST_TOKEN}") == "from-env"


# ===========================================================================
# 2. run_cli subprocess robustness (behavioral)
# ===========================================================================

def test_run_cli_passes_hardening_kwargs(monkeypatch):
    """run_cli must spawn with start_new_session=True and errors='replace'."""
    captured = {}

    class _FakeProc:
        pid = 4242
        returncode = 0

        def communicate(self, timeout=None):
            return ("ok", "")

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(bridge.subprocess, "Popen", fake_popen)
    bridge.run_cli("status", timeout=5)
    assert captured.get("start_new_session") is True, "process-group isolation dropped"
    assert captured.get("errors") == "replace", 'errors="replace" decode robustness dropped'
    assert captured.get("text") is True


def test_run_cli_decodes_invalid_utf8_without_raising():
    """The (text=True, errors='replace') combo the code uses must not raise on bad bytes.

    Proves the kwargs actually achieve robust decode: without errors='replace',
    text-mode decode of these bytes raises UnicodeDecodeError.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", r"import sys; sys.stdout.buffer.write(b'before\xff\xfeafter')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    out, _ = proc.communicate(timeout=10)
    assert "before" in out and "after" in out
    assert "�" in out  # replacement char proves errors="replace" took effect


# ===========================================================================
# 3. /ad-* slash commands (behavioral)
# ===========================================================================

@pytest.mark.parametrize(
    "command_name,expected_subcmd",
    [
        ("/ad-compact", "restart"),
        ("/ad-clear", "restart"),
        ("/ad-check", "output"),
        ("/ad-send", "send"),
    ],
)
def test_slash_commands_registered_and_dispatch(monkeypatch, tmp_path, command_name, expected_subcmd):
    # /ad-clear writes state.json under CONDUCTOR_DIR/<name>/ — redirect to tmp.
    monkeypatch.setattr(bridge, "CONDUCTOR_DIR", tmp_path)
    (tmp_path / "chief").mkdir(parents=True, exist_ok=True)

    cli_calls = []

    def fake_run_cli(*args, **kwargs):
        cli_calls.append(args)
        return subprocess.CompletedProcess(list(args), 0, stdout="some output", stderr="")

    monkeypatch.setattr(bridge, "run_cli", fake_run_cli)

    result = _make_slack_app(monkeypatch, _slack_config())
    assert result is not None, "create_slack_app returned None (Slack stub not active?)"
    app, _ = result

    assert command_name in app.commands, f"{command_name} not registered"

    class _AsyncRec:
        def __init__(self):
            self.calls = []

        async def __call__(self, *a, **k):
            self.calls.append((a, k))

    ack, respond = _AsyncRec(), _AsyncRec()
    # /ad-send needs "<session> <message>"; /ad-check needs "<session>"; others optional.
    text = "chief hello there" if command_name == "/ad-send" else "chief"
    cmd_payload = {"user_id": "U1", "text": text}

    asyncio.run(app.commands[command_name](ack, respond, cmd_payload))

    assert ack.calls, f"{command_name} never called ack()"
    assert cli_calls, f"{command_name} never invoked the agent-deck CLI"
    subcmds = [a[1] for a in cli_calls if len(a) >= 2]
    assert expected_subcmd in subcmds, (
        f"{command_name} should invoke `session {expected_subcmd}`; saw {cli_calls}"
    )


# ===========================================================================
# 4. multi-conductor filtering / mirror target selection (behavioral)
# ===========================================================================

def test_mirror_target_selects_configured_conductor():
    target = bridge.select_mirror_conductor(["scout"], _CONDUCTORS)
    assert target is not None and target["name"] == "scout"


def test_mirror_target_defaults_to_first_when_unconfigured():
    target = bridge.select_mirror_conductor([], _CONDUCTORS)
    assert target is not None and target["name"] == "chief"


def test_mirror_target_filters_out_unknown_conductor():
    # A configured name that matches no discovered conductor selects nothing.
    assert bridge.select_mirror_conductor(["ghost"], _CONDUCTORS) is None


def test_load_config_surfaces_slack_conductors(monkeypatch, tmp_path):
    """load_config must carry the per-platform `conductors` list through."""
    cfg_doc = {
        "conductor": {
            "enabled": True,
            "slack": {
                "bot_token": "xoxb-x",
                "app_token": "xapp-x",
                "channel_id": _CHANNEL,
                "conductors": ["scout"],
            },
        }
    }
    # Point CONFIG_PATH at a real (existing) file so load_config's exists() check
    # passes; toml.load is stubbed so the file's actual contents are irrelevant.
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("")
    monkeypatch.setattr(bridge, "CONFIG_PATH", cfg_file)
    monkeypatch.setattr(bridge.toml, "load", lambda *a, **k: cfg_doc)
    cfg = bridge.load_config()
    assert cfg["slack"]["conductors"] == ["scout"]


# ===========================================================================
# 5. no-double-logging: one inbound message -> exactly one routing log (behavioral)
# ===========================================================================

def test_single_inbound_message_logs_once(monkeypatch, caplog):
    send_calls = []

    def fake_send_to_conductor(*a, **k):
        send_calls.append((a, k))
        return (True, "done", False)

    monkeypatch.setattr(bridge, "send_to_conductor", fake_send_to_conductor)

    result = _make_slack_app(monkeypatch, _slack_config(listen_mode="all"))
    app, _ = result
    handler = app.events["app_mention"]  # always-active path

    event = {"user": "U1", "channel": _CHANNEL, "text": "do the thing", "ts": "1.1"}

    async def _say(*a, **k):
        return None

    with caplog.at_level(logging.INFO, logger="conductor-bridge"):
        asyncio.run(handler(event, _say))

    routing_logs = [r for r in caplog.records if "Slack message ->" in r.getMessage()]
    assert len(routing_logs) == 1, (
        f"expected exactly one routing log for one message, got {len(routing_logs)}: "
        f"{[r.getMessage() for r in routing_logs]}"
    )


def test_logger_has_no_duplicate_handlers():
    """The conductor-bridge logger must not emit through duplicate handlers."""
    lg = logging.getLogger("conductor-bridge")
    # The logger itself carries no handlers (it propagates to root, configured once).
    assert lg.handlers == [], "conductor-bridge logger should rely on root handlers only"


def test_mentions_mode_message_event_does_not_double_process(monkeypatch, caplog):
    """In the default 'mentions' mode, the message event must NOT route (only app_mention does),
    so an @-mention can't be processed twice."""
    monkeypatch.setattr(bridge, "send_to_conductor", lambda *a, **k: (True, "x", False))
    result = _make_slack_app(monkeypatch, _slack_config(listen_mode="mentions"))
    app, _ = result
    msg_handler = app.events["message"]

    event = {"user": "U1", "channel": _CHANNEL, "text": "hi", "ts": "1.1"}

    async def _say(*a, **k):
        return None

    with caplog.at_level(logging.INFO, logger="conductor-bridge"):
        asyncio.run(msg_handler(event, _say))

    routing_logs = [r for r in caplog.records if "Slack message ->" in r.getMessage()]
    assert routing_logs == [], "message event routed in 'mentions' mode (double-processing risk)"


# ===========================================================================
# 6. Slack liveness watchdog (presence/smoke)
# ===========================================================================

def test_watchdog_present_and_stale_timeout():
    src = _CANONICAL.read_text(encoding="utf-8")
    assert "slack_liveness_watchdog" in src, "Slack liveness watchdog removed"
    assert "_SLACK_STALE_TIMEOUT = 1800" in src, "watchdog stale timeout changed/removed"
    # The watchdog is scheduled as a task alongside the handler.
    assert "asyncio.create_task(slack_liveness_watchdog())" in src, "watchdog not scheduled"


# ===========================================================================
# 7. Voice STT (presence/smoke)
# ===========================================================================

def test_stt_gate_and_transcription_path_present():
    # The env gate is read at import time into a module attribute.
    assert hasattr(bridge, "BRIDGE_STT_ENABLED"), "BRIDGE_STT_ENABLED gate removed"
    assert isinstance(bridge.BRIDGE_STT_ENABLED, bool)
    # Transcription path defined and reads the opt-in env var.
    assert callable(getattr(bridge, "transcribe_voice_file", None)), "transcribe_voice_file removed"
    assert callable(getattr(bridge, "_transcribe_telegram_voice", None)), "telegram voice path removed"
    src = _CANONICAL.read_text(encoding="utf-8")
    assert 'os.environ.get("BRIDGE_STT_ENABLED"' in src, "STT env gate not read"
    assert "parakeet-mlx" in src, "parakeet-mlx transcription backend missing"
    # transcribe_voice_file body actually consults the gate / backend.
    assert "parakeet" in inspect.getsource(bridge.transcribe_voice_file)
