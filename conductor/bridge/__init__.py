"""Conductor Bridge package.

Modules:
  constants    - Shared constants, regex patterns, markdown conversion
  config       - Configuration loading and conductor discovery
  cli          - Agent-Deck CLI helpers (run_cli, send_to_conductor, etc.)
  formatting   - Message formatting, splitting, and routing utilities
  telegram_bot - Telegram bot setup (aiogram)
  slack_bot    - Slack bot setup (slack-bolt, Socket Mode)
  discord_bot  - Discord bot setup (discord.py)
  heartbeat    - Periodic heartbeat loop for conductor status checks
  mirror       - JSONL-based terminal mirror for Slack sync
  main         - Entry point: discovers conductors, starts platforms, runs event loop
"""
