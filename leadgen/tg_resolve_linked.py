#!/usr/bin/env python3
"""Resolve linked discussion chats for broadcast channels.

For each channel username/id in --channels, fetch full channel info and print
the linked discussion group as `id<linked_chat_id>` (one per line, stdout) so
tg_pipeline.sh can scrape comments under channel posts.

Usage:
    python scripts/tg_resolve_linked.py \
        --session ~/.claude/skill-telegram/tim3.session \
        --channels "saasstuff,blackproduct"
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from telethon import TelegramClient
from telethon.tl.functions.channels import GetFullChannelRequest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tg_scrape_posters import resolve_entity  # noqa: E402


async def _resolve_linked(session: str, channels: list[str]) -> None:
    api_id = int(os.getenv("TG_API_ID", "0"))
    api_hash = os.getenv("TG_API_HASH", "")
    if not api_id or not api_hash:
        print("# TG_API_ID/TG_API_HASH не заданы", file=sys.stderr)
        return
    client = TelegramClient(session, api_id, api_hash)
    await client.start()
    try:
        for ch in channels:
            ch = ch.strip()
            if not ch:
                continue
            try:
                entity = await resolve_entity(client, ch, {})
                full = await client(GetFullChannelRequest(entity))
                linked = getattr(full.full_chat, "linked_chat_id", None)
                if linked:
                    print(f"id{linked}")
                else:
                    print(f"# {ch}: нет привязанного чата", file=sys.stderr)
            except Exception as exc:
                print(f"# {ch}: {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        await client.disconnect()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--channels", required=True, help="csv: usernames or id<digits>")
    args = ap.parse_args()
    channels = [c.strip() for c in args.channels.split(",") if c.strip()]
    asyncio.run(_resolve_linked(args.session, channels))


if __name__ == "__main__":
    main()
