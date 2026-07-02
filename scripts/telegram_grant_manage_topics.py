#!/usr/bin/env python3
"""Grant Telegram forum-topic admin rights to WalkCode bot accounts.

This script uses a Telegram MTProto user session via Telethon. Bot API tokens
can observe whether a bot has can_manage_topics, but they cannot grant a bot
rights the bot itself does not already have.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import stat
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_ENV_FILES = (
    Path.home() / ".walkcode" / "telegram-claude.env",
    Path.home() / ".walkcode" / "telegram-codex.env",
)
DEFAULT_SESSION = Path.home() / ".walkcode" / "telegram-user"


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def bot_api(token: str, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = urllib.parse.urlencode(payload or {}).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(f"Telegram Bot API {method} failed")
    return result


def chat_id_from_env(values: dict[str, str]) -> str:
    return (
        values.get("WALKCODE_TELEGRAM_TUI_CHAT_ID")
        or values.get("WALKCODE_E2E_TELEGRAM_CHAT_ID")
        or values.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",", 1)[0].strip()
    )


def bool_attr(value: Any, name: str) -> bool:
    return bool(getattr(value, name, False))


def clone_rights_with_manage_topics(current: Any):
    from telethon import types

    return types.ChatAdminRights(
        change_info=bool_attr(current, "change_info"),
        post_messages=bool_attr(current, "post_messages"),
        edit_messages=bool_attr(current, "edit_messages"),
        delete_messages=bool_attr(current, "delete_messages"),
        ban_users=bool_attr(current, "ban_users"),
        invite_users=bool_attr(current, "invite_users"),
        pin_messages=bool_attr(current, "pin_messages"),
        add_admins=bool_attr(current, "add_admins"),
        anonymous=bool_attr(current, "anonymous"),
        manage_call=bool_attr(current, "manage_call"),
        other=bool_attr(current, "other"),
        manage_topics=True,
        post_stories=bool_attr(current, "post_stories"),
        edit_stories=bool_attr(current, "edit_stories"),
        delete_stories=bool_attr(current, "delete_stories"),
        manage_direct_messages=bool_attr(current, "manage_direct_messages"),
        manage_ranks=bool_attr(current, "manage_ranks"),
    )


def fallback_rights_with_manage_topics():
    from telethon import types

    return types.ChatAdminRights(
        change_info=True,
        delete_messages=True,
        ban_users=True,
        invite_users=True,
        pin_messages=True,
        manage_call=False,
        other=True,
        manage_topics=True,
    )


def load_targets(env_files: list[Path]) -> list[dict[str, str]]:
    targets = []
    for env_file in env_files:
        values = read_env_file(env_file)
        token = values.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = chat_id_from_env(values)
        agent = values.get("WALKCODE_AGENT", env_file.stem)
        if not token or not chat_id:
            raise SystemExit(f"{env_file}: missing TELEGRAM_BOT_TOKEN or target chat id")
        me = bot_api(token, "getMe")["result"]
        member = bot_api(token, "getChatMember", {"chat_id": chat_id, "user_id": me["id"]})["result"]
        targets.append(
            {
                "agent": agent,
                "env_file": str(env_file),
                "chat_id": str(chat_id),
                "bot_id": str(me["id"]),
                "bot_username": str(me.get("username", "")),
                "status": str(member.get("status", "")),
                "can_manage_topics": str(bool(member.get("can_manage_topics"))).lower(),
            }
        )
    return targets


async def grant(args) -> list[dict[str, Any]]:
    from telethon import TelegramClient, functions

    targets = load_targets(args.env_file)
    if args.dry_run:
        return [{"target": target, "would_grant_manage_topics": target["can_manage_topics"] != "true"} for target in targets]

    api_id = args.api_id or os.environ.get("TELEGRAM_API_ID") or os.environ.get("TG_API_ID")
    api_hash = args.api_hash or os.environ.get("TELEGRAM_API_HASH") or os.environ.get("TG_API_HASH")
    if not api_id or not api_hash:
        raise SystemExit("Set TELEGRAM_API_ID and TELEGRAM_API_HASH, or pass --api-id/--api-hash.")

    session = Path(args.session).expanduser()
    session.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session), int(api_id), api_hash)

    async def password_callback() -> str:
        return getpass.getpass("Telegram 2FA password: ")

    await client.start(phone=args.phone, password=password_callback)
    try:
        results = []
        for target in targets:
            channel = await client.get_entity(int(target["chat_id"]))
            bot = await client.get_entity(target["bot_username"] or int(target["bot_id"]))
            participant = await client(functions.channels.GetParticipantRequest(channel, bot))
            current = getattr(participant.participant, "admin_rights", None)
            rights = clone_rights_with_manage_topics(current) if current is not None else fallback_rights_with_manage_topics()
            await client(functions.channels.EditAdminRequest(channel, bot, rights, rank=""))
            refreshed = load_targets([Path(target["env_file"])])[0]
            results.append(
                {
                    "agent": target["agent"],
                    "bot_username": target["bot_username"],
                    "chat_id": target["chat_id"],
                    "before_can_manage_topics": target["can_manage_topics"] == "true",
                    "after_can_manage_topics": refreshed["can_manage_topics"] == "true",
                }
            )
        return results
    finally:
        await client.disconnect()
        session_file = session.with_suffix(".session")
        if session_file.exists():
            session_file.chmod(stat.S_IRUSR | stat.S_IWUSR)


def parse_args():
    parser = argparse.ArgumentParser(description="Grant can_manage_topics to WalkCode Telegram bots.")
    parser.add_argument("--env-file", action="append", type=Path, default=None, help="WalkCode Telegram env file")
    parser.add_argument("--session", default=str(DEFAULT_SESSION), help="Telethon session path without .session suffix")
    parser.add_argument("--api-id", default="", help="Telegram API id")
    parser.add_argument("--api-hash", default="", help="Telegram API hash")
    parser.add_argument("--phone", default="", help="Telegram account phone number for first login")
    parser.add_argument("--dry-run", action="store_true", help="Only inspect targets")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.env_file:
        args.env_file = list(DEFAULT_ENV_FILES)
    results = asyncio.run(grant(args))
    print(json.dumps(results, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
