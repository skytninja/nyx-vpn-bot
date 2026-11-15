"""Exercise the actual aiogram routers without Telegram or payment network calls."""

import importlib
from datetime import datetime, timezone

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import Update
from app import order_storage as orders


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        return True

    async def stream_content(self, *args, **kwargs):
        yield b""


@pytest.fixture
def flow(monkeypatch):
    from app.handlers import payment, start

    importlib.reload(payment)
    importlib.reload(start)
    monkeypatch.setenv("REMNA_SALES_ENABLED", "false")
    monkeypatch.setattr(payment, "get_payment_amount", lambda: 190)
    monkeypatch.setattr(payment, "get_payment_days", lambda: 30)
    session = RecordingSession()
    bot = Bot("123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", session=session)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(start.router)
    dispatcher.include_router(payment.router)
    return dispatcher, bot, session


def event(text=None, callback=None, photo=False):
    user = {"id": 100, "is_bot": False, "first_name": "Test"}
    message = {
        "message_id": 1,
        "date": datetime.now(timezone.utc).isoformat(),
        "chat": {"id": 100, "type": "private"},
        "from": user,
    }
    if photo:
        message["photo"] = [
            {"file_id": "test", "file_unique_id": "test", "width": 10, "height": 10}
        ]
    else:
        message["text"] = text or "Тариф"
    if callback:
        return Update.model_validate(
            {
                "update_id": 2,
                "callback_query": {
                    "id": "test",
                    "from": user,
                    "chat_instance": "test",
                    "data": callback,
                    "message": message,
                },
            }
        )
    return Update.model_validate({"update_id": 1, "message": message})


async def feed(flow, **kwargs):
    dispatcher, bot, session = flow
    await dispatcher.feed_update(bot, event(**kwargs))
    return session.calls


def last_message(calls):
    return next(call for call in reversed(calls) if isinstance(call, SendMessage))


def assert_no_payment_writes():
    for table in ["vpn_orders", "remna_accounts", "subscriptions"]:
        assert orders.query("SELECT COUNT(*) AS c FROM " + table)["c"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry", ["💳 Купить подписку", "💳 Продлить подписку", "/buy", "/extend"]
)
async def test_offer_accessible_while_sales_paused(flow, entry):
    calls = await feed(flow, text=entry)
    message = last_message(calls)
    assert "Тариф «NYX VPN»" in message.text
    assert "1 мес. (30 дней)" in message.text and "190 ₽" in message.text
    assert any(
        button.text == "Оплатить" and button.callback_data == "checkout:pay"
        for row in message.reply_markup.inline_keyboard
        for button in row
    )
    assert "СБП" not in message.text and "чек" not in message.text.lower()
    assert_no_payment_writes()


@pytest.mark.asyncio
async def test_custom_period_and_repeated_payment_clicks_never_grant_access(flow):
    await feed(flow, text="💳 Купить подписку")
    calls = await feed(flow, text="7")
    offer = last_message(calls)
    assert "7 мес. (210 дней)" in offer.text and "1330 ₽" in offer.text
    for _ in range(2):
        calls = await feed(flow, callback="checkout:pay")
        result = calls[-1]
        assert isinstance(result, AnswerCallbackQuery) and result.show_alert
        assert "Оплата временно недоступна" in result.text
    assert_no_payment_writes()


@pytest.mark.asyncio
async def test_change_period_button(flow):
    await feed(flow, callback="checkout:months")
    calls = await feed(flow, text="12")
    assert "2280 ₽" in last_message(calls).text
    assert_no_payment_writes()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["0", "121", "-1", "1.5", "два"])
async def test_invalid_period_does_not_offer_payment(flow, value):
    await feed(flow, text="/buy")
    calls = await feed(flow, text=value)
    message = last_message(calls)
    assert "от 1 до 6" in message.text
    assert message.reply_markup is None
    assert_no_payment_writes()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "callback",
    ["payment:approve:100", "payment:reject:100", "order:approve:1", "order:reject:1"],
)
async def test_old_admin_buttons_cannot_extend_or_reject_orders(flow, callback):
    calls = await feed(flow, callback=callback)
    assert isinstance(calls[-1], AnswerCallbackQuery)
    assert "больше не используется" in calls[-1].text
    assert_no_payment_writes()


@pytest.mark.asyncio
async def test_old_receipt_is_not_forwarded(flow):
    calls = await feed(flow, photo=True)
    assert len(calls) == 1 and isinstance(calls[0], SendMessage)
    assert "больше не принимаются" in calls[0].text
    assert_no_payment_writes()


@pytest.mark.asyncio
async def test_policy_available_during_month_selection(flow):
    await feed(flow, text="/buy")
    calls = await feed(flow, text="ПОЛИТИКА")
    links = last_message(calls).reply_markup.inline_keyboard
    assert [row[0].url for row in links] == [
        "https://telegra.ph/Politika-konfidencialnosti-11-15-76",
        "https://telegra.ph/Polzovatelskoe-soglashenie-11-15-39",
    ]


@pytest.mark.asyncio
async def test_retired_buttons_preserve_existing_payment_history(flow):
    from app.storage import ensure_user

    ensure_user(100, "test")
    entry = orders.create_order(100, 1, 30, 190)
    original = orders.submit_receipt(100, 100, 10)
    for callback in [f"order:approve:{entry['id']}", f"order:reject:{entry['id']}"]:
        await feed(flow, callback=callback)
        assert orders.order(entry["id"]) == original


@pytest.mark.asyncio
async def test_six_month_limit_and_valid_maximum(flow):
    await feed(flow, text="/buy")
    calls = await feed(flow, text="6")
    offer = last_message(calls)
    assert "6 мес. (180 дней)" in offer.text and "1140 ₽" in offer.text
    calls = await feed(flow, text="7")
    assert "от 1 до 6" in last_message(calls).text
    assert_no_payment_writes()
