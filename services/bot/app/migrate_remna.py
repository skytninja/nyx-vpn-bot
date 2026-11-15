"""Reconcile verified migration bindings. Dry run unless --apply is supplied.

Input is a private plan built from the source-subId migration ledger, never usernames.
Run only with the bot paused, after backing up PostgreSQL and Redis.
"""

import argparse
import asyncio
import json
from pathlib import Path

from app import order_storage as orders
from app.config import load_env
from app.services.remnawave import RemnawaveClient, parse_date
from app.services.subscriptions import save_user
from app.storage import _cache_clear_subscription, set_trial_used
from app.vpn_instructions import vpn_instructions


async def reconcile(plan, apply=False):
    client = RemnawaveClient()
    checked = []
    try:
        # Complete validation before the first write.
        seen_tg, seen_id = set(), set()
        for row in plan["bindings"]:
            if row["tg_id"] in seen_tg or row["remna_id"] in seen_id:
                raise RuntimeError("Duplicate binding in migration plan")
            seen_tg.add(row["tg_id"])
            seen_id.add(row["remna_id"])
            existing = orders.binding(row["tg_id"])
            sub = orders.query(
                "SELECT sub_id FROM subscriptions WHERE tg_id=%s", (row["tg_id"],)
            )
            if existing:
                if existing["remna_id"] != row["remna_id"]:
                    raise RuntimeError("Existing binding conflicts with plan")
            elif not sub or sub["sub_id"] != row["source_sub_id"]:
                raise RuntimeError("Source subscription changed since plan creation")
            live = await client.get_user(row["remna_id"])
            if live["shortUuid"] != row["short_uuid"] or live.get("telegramId") not in (
                None,
                row["tg_id"],
            ):
                raise RuntimeError("Panel ownership or shortUuid mismatch")
            checked.append((row, live))
        pending = []
        for row in plan["pending"]:
            old = orders.query(
                "SELECT sub_id FROM pending_subscriptions WHERE username=%s",
                (row["username"],),
            )
            if not old or old["sub_id"] != row["source_sub_id"]:
                raise RuntimeError("Pending source changed")
            live = await client.get_user(row["remna_id"])
            pending.append((row, live))
        if apply:
            for row, live in checked:
                _cache_clear_subscription(row["tg_id"])
                save_user(row["tg_id"], client, live)
                set_trial_used(row["tg_id"])
                orders.query(
                    "UPDATE remna_accounts SET source_sub_id=%s WHERE tg_id=%s",
                    (row["source_sub_id"], row["tg_id"]),
                )
            for row, live in pending:
                link = client.subscription_link(live)
                orders.query(
                    """UPDATE pending_subscriptions SET end_at=%s, subscription_link=%s,
                    instructions=%s, updated_at=NOW() WHERE username=%s AND sub_id=%s""",
                    (
                        parse_date(live["expireAt"]),
                        link,
                        vpn_instructions(link),
                        row["username"],
                        row["source_sub_id"],
                    ),
                )
            # Historical stored instructions are visible in cabinet/admin; rewrite even unresolved rows.
            for row in orders.query(
                "SELECT tg_id, subscription_link FROM subscriptions", one=False
            ):
                orders.query(
                    "UPDATE subscriptions SET instructions=%s WHERE tg_id=%s",
                    (
                        vpn_instructions(row["subscription_link"])
                        if row["subscription_link"]
                        else None,
                        row["tg_id"],
                    ),
                )
                _cache_clear_subscription(row["tg_id"])
        print(
            json.dumps(
                {
                    "apply": apply,
                    "verified_bindings": len(checked),
                    "pending_preserved": len(pending),
                    "unresolved": len(plan["unresolved"]),
                }
            )
        )
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    load_env()
    asyncio.run(reconcile(json.loads(args.plan.read_text()), args.apply))
