from copy import deepcopy
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest
from app import order_storage as orders
from app import storage
from app.services import subscriptions as service

NOW = datetime.now(timezone.utc)


class Panel:
    def __init__(self, user=None):
        self.user = deepcopy(user)
        self.patch_calls = 0
        self.create_calls = 0
        self.crash_after_create = False
        self.crash_after_patch = False

    async def close(self):
        pass

    async def get_user(self, user_id):
        assert self.user["id"] == user_id
        return deepcopy(self.user)

    async def find_bot_user(self, tg_id):
        return deepcopy(self.user)

    async def create_user(self, tg_id, end_at):
        self.create_calls += 1
        self.user = account(end_at)
        if self.crash_after_create:
            self.crash_after_create = False
            raise TimeoutError("POST succeeded but response was lost")
        return deepcopy(self.user)

    async def set_expiry(self, user_id, end_at):
        self.patch_calls += 1
        self.user["expireAt"] = end_at.isoformat()
        self.user["status"] = "ACTIVE"
        if self.crash_after_patch:
            self.crash_after_patch = False
            raise TimeoutError("PATCH succeeded but response was lost")
        return deepcopy(self.user)

    def subscription_link(self, user):
        return "https://sub.example.test/" + user["shortUuid"]


def account(end_at=NOW + timedelta(days=10), status="ACTIVE"):
    return {
        "id": 41,
        "shortUuid": "test123",
        "expireAt": end_at.isoformat(),
        "status": status,
        "telegramId": 100,
        "trafficLimitBytes": 0,
        "hwidDeviceLimit": 0,
    }


def seed(panel, monkeypatch, kind="payment", months=3):
    storage.ensure_user(100, "tester")
    monkeypatch.setattr(service, "RemnawaveClient", lambda: panel)
    if panel.user:
        service.save_user(100, panel, panel.user)
    entry = orders.create_order(
        100,
        months,
        months * 30 if kind == "payment" else 3,
        months * 150 if kind == "payment" else 0,
        kind,
    )
    if kind == "payment":
        entry = orders.submit_receipt(100, 100, 234)
    return entry


@pytest.mark.asyncio
async def test_payment_preserves_remaining_days_and_is_idempotent(monkeypatch):
    panel = Panel(account())
    entry = seed(panel, monkeypatch)
    result, user, _ = await service.apply_order(entry["id"])
    expected = NOW + timedelta(days=100)
    assert result["target_end"] == expected
    assert user["trafficLimitBytes"] == 0
    assert user["hwidDeviceLimit"] == 0
    await service.apply_order(entry["id"])
    assert panel.patch_calls == 1
    assert storage.get_subscription_meta(100)["end_at"] == expected


@pytest.mark.asyncio
async def test_expired_subscription_renews_existing_account(monkeypatch):
    panel = Panel(account(NOW - timedelta(days=10), "EXPIRED"))
    entry = seed(panel, monkeypatch)
    before = datetime.now(timezone.utc)
    result, _, _ = await service.apply_order(entry["id"])
    assert (
        before + timedelta(days=90)
        <= result["target_end"]
        <= datetime.now(timezone.utc) + timedelta(days=90)
    )
    assert panel.create_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["patch", "create"])
async def test_timeout_retry_never_adds_second_period(monkeypatch, phase):
    panel = Panel(account() if phase == "patch" else None)
    entry = seed(panel, monkeypatch)
    setattr(panel, "crash_after_" + phase, True)
    with pytest.raises(TimeoutError):
        await service.apply_order(entry["id"])
    target = orders.order(entry["id"])["target_end"]
    await service.apply_order(entry["id"])
    assert service.parse_date(panel.user["expireAt"]) == target
    assert panel.create_calls == (1 if phase == "create" else 0)


@pytest.mark.asyncio
async def test_crash_after_patch_before_final_commit(monkeypatch):
    panel = Panel(account())
    entry = seed(panel, monkeypatch)
    finish = orders.finish
    monkeypatch.setattr(
        orders,
        "finish",
        lambda *a: (_ for _ in ()).throw(RuntimeError("DB unavailable")),
    )
    with pytest.raises(RuntimeError):
        await service.apply_order(entry["id"])
    target = orders.order(entry["id"])["target_end"]
    monkeypatch.setattr(orders, "finish", finish)
    await service.apply_order(entry["id"])
    assert service.parse_date(panel.user["expireAt"]) == target


@pytest.mark.asyncio
async def test_external_extension_is_not_shortened_on_retry(monkeypatch):
    panel = Panel(account())
    entry = seed(panel, monkeypatch)
    panel.crash_after_patch = True
    with pytest.raises(TimeoutError):
        await service.apply_order(entry["id"])
    external = NOW + timedelta(days=200)
    panel.user["expireAt"] = external.isoformat()
    result, _, _ = await service.apply_order(entry["id"])
    assert result["target_end"] == external


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["DISABLED", "LIMITED"])
async def test_disabled_account_is_not_reactivated(monkeypatch, status):
    panel = Panel(account(status=status))
    entry = seed(panel, monkeypatch)
    with pytest.raises(RuntimeError):
        await service.apply_order(entry["id"])
    assert panel.patch_calls == 0


@pytest.mark.asyncio
async def test_unlimited_subscription_remains_unlimited(monkeypatch):
    panel = Panel(account(datetime(9999, 12, 31, tzinfo=timezone.utc)))
    entry = seed(panel, monkeypatch)
    with pytest.raises(RuntimeError):
        await service.apply_order(entry["id"])
    assert panel.patch_calls == 0
    assert service.is_unlimited(panel.user)


@pytest.mark.asyncio
async def test_referral_reward_once_after_retry(monkeypatch):
    panel = Panel(account())
    entry = seed(panel, monkeypatch)
    storage.ensure_user(200, "referrer")
    storage.set_referrer(100, 200)
    finish = orders.finish
    monkeypatch.setattr(
        orders, "finish", lambda *a: (_ for _ in ()).throw(RuntimeError("crash"))
    )
    with pytest.raises(RuntimeError):
        await service.apply_order(entry["id"])
    monkeypatch.setattr(orders, "finish", finish)
    await service.apply_order(entry["id"])
    assert storage.get_referral_info(200)["referral_balance"] == 75


def test_expired_record_retained_for_account_binding():
    storage.ensure_user(100, "tester")
    storage.set_subscription(
        100,
        NOW - timedelta(days=10),
        NOW - timedelta(days=1),
        "https://example.test",
        "old",
        sub_id="legacy",
    )
    assert storage.get_subscription_meta(100)["sub_id"] == "legacy"
    assert storage.get_subscription(100)[1] < NOW
    with pytest.raises(RuntimeError):
        service.assert_new_user_allowed(100)


def test_username_alone_does_not_claim_migrated_pending():
    storage.ensure_user(100, "someone")
    orders.query(
        "INSERT INTO pending_subscriptions(username,email) VALUES('someone','old')"
    )
    with pytest.raises(RuntimeError):
        service.assert_new_user_allowed(100)
    assert orders.binding(100) is None


def test_concurrent_user_lock_and_unique_open_order():
    storage.ensure_user(100, "tester")
    with orders.user_lock(100):
        with pytest.raises(RuntimeError):
            with orders.user_lock(100):
                pass
    orders.create_order(100, 1, 30, 150)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        orders.create_order(100, 3, 90, 450)


@pytest.mark.asyncio
async def test_trial_created_once_and_cannot_be_approved_as_payment(monkeypatch):
    panel = Panel()
    entry = seed(panel, monkeypatch, kind="trial")
    with pytest.raises(RuntimeError):
        await service.apply_order(entry["id"])
    await service.apply_order(entry["id"], allow_trial=True)
    await service.apply_order(entry["id"], allow_trial=True)
    assert panel.create_calls == 1
    assert storage.is_trial_used(100)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        orders.create_order(100, 0, 3, 0, "trial")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["0", "121", "99999999999999999999"])
async def test_invalid_month_count(value, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.handlers import payment

    message = SimpleNamespace(text=value, answer=AsyncMock())
    await payment.select_months(message, AsyncMock())
    assert "от 1 до 120" in message.answer.call_args.args[0]


@pytest.mark.asyncio
async def test_arbitrary_month_count_shows_offer_without_creating_order(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.handlers import payment

    monkeypatch.setattr(payment, "get_payment_amount", lambda: 150)
    monkeypatch.setattr(payment, "get_payment_days", lambda: 30)
    message = SimpleNamespace(
        text="7", from_user=SimpleNamespace(id=100), answer=AsyncMock()
    )
    await payment.select_months(message, AsyncMock())
    text = message.answer.call_args.args[0]
    assert "7 мес. (210 дней)" in text and "1050 ₽" in text
    assert orders.query("SELECT COUNT(*) AS c FROM vpn_orders")["c"] == 0


def test_first_payment_recorded_even_without_referrer():
    storage.ensure_user(100, "tester")
    assert storage.record_first_payment_and_reward(100) is None
    row = orders.query("SELECT first_payment_done FROM users WHERE tg_id=100")
    assert row["first_payment_done"] is True


@pytest.mark.asyncio
async def test_api_contract_uses_absolute_expiry_without_changing_limits(monkeypatch):
    import json

    import httpx
    from app.services.remnawave import RemnawaveClient

    monkeypatch.setenv("REMNA_URL", "https://panel.example.test")
    monkeypatch.setenv("REMNA_TOKEN", "test-secret")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"response": account()})

    client = RemnawaveClient()
    await client.http.aclose()
    client.http = httpx.AsyncClient(
        base_url="https://panel.example.test/api/",
        transport=httpx.MockTransport(handler),
    )
    await client.set_expiry(41, NOW)
    assert calls[0].url.path == "/api/users"
    assert calls[0].method == "PATCH"
    assert json.loads(calls[0].content) == {
        "id": 41,
        "expireAt": NOW.isoformat(),
        "status": "ACTIVE",
    }
    await client.close()


@pytest.mark.asyncio
async def test_trial_recovers_from_crash_after_db_saved(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.handlers import start

    panel = Panel()
    entry = seed(panel, monkeypatch, kind="trial")
    finish = orders.finish
    monkeypatch.setattr(
        orders, "finish", lambda *a: (_ for _ in ()).throw(RuntimeError("crash"))
    )
    with pytest.raises(RuntimeError):
        await service.apply_order(entry["id"], allow_trial=True)
    monkeypatch.setattr(orders, "finish", finish)
    monkeypatch.setattr(start, "RemnawaveClient", lambda: panel)
    message = SimpleNamespace(answer=AsyncMock())
    await start._request_trial(SimpleNamespace(id=100, username="tester"), message)
    assert orders.order(entry["id"])["status"] == "approved"
    assert panel.create_calls == 1


@pytest.mark.asyncio
async def test_trial_available_while_sales_paused_and_not_granted_twice(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.config import sales_enabled
    from app.handlers import start

    monkeypatch.setenv("REMNA_SALES_ENABLED", "false")
    assert not sales_enabled()
    panel = Panel()
    monkeypatch.setattr(service, "RemnawaveClient", lambda: panel)
    monkeypatch.setattr(start, "RemnawaveClient", lambda: panel)
    monkeypatch.setattr(start, "get_trial_days", lambda: 3)
    user = SimpleNamespace(id=100, username="tester")
    message = SimpleNamespace(answer=AsyncMock())
    await start._request_trial(user, message)
    assert panel.create_calls == 1
    assert storage.is_trial_used(100)
    expiry = panel.user["expireAt"]
    await start._request_trial(user, message)
    assert panel.create_calls == 1
    assert panel.user["expireAt"] == expiry
    assert "подписка активна" in message.answer.call_args.args[0]
