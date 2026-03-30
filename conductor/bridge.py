#!/usr/bin/env python3
"""
Conductor Bridge: Telegram & Slack & Discord <-> Agent-Deck conductor sessions.

Thin wrapper that delegates to the bridge package.
Kept for backward compatibility with launchd/systemd configs that invoke bridge.py directly.
"""

import asyncio
from bridge.main import main

if __name__ == "__main__":
    asyncio.run(main())
