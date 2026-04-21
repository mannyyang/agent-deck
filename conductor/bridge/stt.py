"""Voice transcription (local parakeet-mlx via subprocess).

Opt-in via BRIDGE_STT_ENABLED=true. Isolated in a subprocess so inference
doesn't block the bridge event loop.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from .config import log

# Opt-in: voice STT is disabled unless BRIDGE_STT_ENABLED=true
BRIDGE_STT_ENABLED = os.environ.get("BRIDGE_STT_ENABLED", "").lower() in (
    "true", "1", "yes",
)

# Path to the STT worker script (sibling in the bridge package)
STT_WORKER = Path(__file__).parent / "stt_worker.py"
# Python interpreter: prefer the bridge venv, fall back to system python3
_venv_python = Path(__file__).parent.parent / ".venv" / "bin" / "python3"
VENV_PYTHON = str(_venv_python) if _venv_python.exists() else "python3"


async def transcribe_voice_file(audio_bytes: bytes, suffix: str = ".ogg") -> str | None:
    """Transcribe raw audio bytes by writing to a temp file and invoking stt_worker.py.

    Returns the transcribed text, or None on failure/timeout.
    """
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            suffix=suffix, prefix="voice_", delete=False
        ) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        proc = await asyncio.create_subprocess_exec(
            str(VENV_PYTHON), str(STT_WORKER), tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=60
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            log.error("STT worker timed out (60s)")
            return None

        if proc.returncode != 0:
            log.error("STT worker failed: %s", stderr.decode().strip())
            return None

        text = stdout.decode().strip()
        return text if text else None

    except Exception as e:
        log.error("Voice transcription error: %s", e)
        return None
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
