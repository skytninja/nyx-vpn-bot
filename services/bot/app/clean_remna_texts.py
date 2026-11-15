"""Neutral subscription labels; default is a read-only preview."""

import argparse
import asyncio
import base64
import json
import re
from pathlib import Path

from app.config import load_env
from app.services.remnawave import RemnawaveClient

PATTERN = re.compile(
    r"white[\s_-]*lists?|бел\w*\s+спис\w*|обход|блокиров|цензур|\bdpi\b|тспу|глушил|аноним|\blte\b|censor|bypass|anonym",
    re.I,
)
ANNOUNCE = (
    "NYX VPN\nРоссия · Нидерланды · Германия\nПодписка и продление — в Telegram-боте"
)


async def clean(apply=False, backup=None):
    client = RemnawaveClient()
    try:
        hosts = await client.request("GET", "hosts")
        settings = await client.request("GET", "subscription-settings")
        changes = [
            {"uuid": host["uuid"], "remark": "🇳🇱Нидерланды · Дополнительный"}
            for host in hosts
            if PATTERN.search(host["remark"])
        ]
        if apply:
            if not backup:
                raise RuntimeError("--backup is required for --apply")
            # Exclusive creation prevents overwriting the original backup on a retry.
            with backup.open(
                "x",
                opener=lambda path, flags: __import__("os").open(path, flags, 0o600),
            ) as file:
                json.dump({"hosts": hosts, "subscription_settings": settings}, file)
            for change in changes:
                await client.request("PATCH", "hosts", change)
            headers = dict(settings.get("customResponseHeaders") or {})
            headers["announce"] = (
                "base64:" + base64.b64encode(ANNOUNCE.encode()).decode()
            )
            await client.request(
                "PATCH",
                "subscription-settings",
                {"uuid": settings["uuid"], "customResponseHeaders": headers},
            )
        print(
            json.dumps(
                {
                    "apply": apply,
                    "host_labels_to_replace": len(changes),
                    "announcement": ANNOUNCE,
                },
                ensure_ascii=False,
            )
        )
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args()
    load_env()
    asyncio.run(clean(args.apply, args.backup))
