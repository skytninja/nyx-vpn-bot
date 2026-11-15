from datetime import datetime, timedelta, timezone

from app import order_storage as orders
from app.services.remnawave import RemnawaveClient, is_unlimited, parse_date
from app.storage import (
    get_subscription_meta,
    is_trial_used,
    record_first_payment_and_reward,
    set_subscription,
    set_trial_used,
)
from app.vpn_instructions import vpn_instructions


def save_user(tg_id, client, user):
    orders.bind(tg_id, user)
    old = get_subscription_meta(tg_id)
    link = client.subscription_link(user)
    end = parse_date(user["expireAt"])
    set_subscription(
        tg_id,
        old["start_at"] if old else datetime.now(timezone.utc),
        end,
        link,
        vpn_instructions(link),
        "all",
        client_uuid=str(user["id"]),
        sub_id=user["shortUuid"],
    )
    return user


async def sync_user(tg_id, client):
    bound = orders.binding(tg_id)
    if not bound:
        return None
    user = await client.get_user(bound["remna_id"])
    return save_user(tg_id, client, user)


def assert_new_user_allowed(tg_id):
    # Preserve unmatched migrated records for manual review; never guess ownership by name.
    if get_subscription_meta(tg_id):
        raise RuntimeError("Подписка требует сверки. Напишите @nyxsupportvpn.")
    pending = orders.query(
        """SELECT 1 FROM pending_subscriptions p JOIN users u
        ON lower(ltrim(u.username,'@'))=p.username WHERE u.tg_id=%s""",
        (tg_id,),
    )
    if pending:
        raise RuntimeError("Подписка требует сверки. Напишите @nyxsupportvpn.")


async def apply_order(order_id, allow_trial=False):
    entry = orders.order(order_id)
    if not entry:
        raise RuntimeError("Заказ не найден")
    tg_id = entry["tg_id"]
    with orders.user_lock(tg_id):
        entry = orders.order(order_id)
        if entry["status"] == "approved":
            return entry, None, None
        if entry["kind"] == "trial" and not allow_trial:
            raise RuntimeError("Некорректный заказ")
        allowed = (
            ("draft", "applying")
            if entry["kind"] == "trial"
            else ("submitted", "applying")
        )
        if entry["status"] not in allowed:
            raise RuntimeError("Заказ не ожидает подтверждения")
        client = RemnawaveClient()
        try:
            user = await sync_user(tg_id, client)
            if not user:
                assert_new_user_allowed(tg_id)
                # Deterministic username recovers POST success after timeout/crash.
                user = await client.find_bot_user(tg_id)
            if user and user["status"] in ("DISABLED", "LIMITED"):
                raise RuntimeError("Аккаунт требует проверки администратором")
            if (
                entry["kind"] == "trial"
                and is_trial_used(tg_id)
                and entry["status"] != "applying"
            ):
                raise RuntimeError("Пробный период уже использован")
            target = entry["target_end"]
            if target is None:
                now = datetime.now(timezone.utc)
                if user and is_unlimited(user):
                    raise RuntimeError(
                        "У вас бессрочная подписка — продление не требуется"
                    )
                anchor = max(parse_date(user["expireAt"]), now) if user else now
                target = anchor + timedelta(days=entry["days"])
                entry = orders.prepare(order_id, user["id"] if user else None, target)
            if not user:
                user = await client.create_user(tg_id, target)
                orders.bind(tg_id, user)
            if entry["remna_id"] and user["id"] != entry["remna_id"]:
                raise RuntimeError("Account binding mismatch")
            # Absolute date makes retries safe, and never shortens externally extended access.
            target = max(target, parse_date(user["expireAt"]))
            entry = orders.prepare(order_id, user["id"], target)
            user = await client.set_expiry(user["id"], target)
            save_user(tg_id, client, user)
            set_trial_used(tg_id)  # Paid access must not later acquire a fresh trial.
            reward = (
                record_first_payment_and_reward(tg_id)
                if entry["kind"] == "payment"
                else None
            )
            orders.finish(order_id)
            return orders.order(order_id), user, reward
        finally:
            await client.close()


async def check_purchase(tg_id):
    client = RemnawaveClient()
    try:
        user = await sync_user(tg_id, client)
        if not user:
            assert_new_user_allowed(tg_id)
        elif is_unlimited(user):
            raise RuntimeError("У вас бессрочная подписка. Продление не требуется.")
        elif user["status"] in ("DISABLED", "LIMITED"):
            raise RuntimeError("Аккаунт требует сверки. Напишите @nyxsupportvpn.")
    finally:
        await client.close()
