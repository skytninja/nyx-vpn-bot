import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import order_storage as orders
from app.config import get_trial_days, sales_enabled
from app.keyboards.menu import main_menu_keyboard, renew_keyboard
from app.services.remnawave import RemnawaveClient, is_unlimited, parse_date
from app.services.subscriptions import apply_order, assert_new_user_allowed, sync_user
from app.storage import ensure_user, is_trial_used, set_referrer
from app.vpn_instructions import APPSTORE_SECTION, vpn_instructions

router = Router()
logger = logging.getLogger(__name__)


@router.message(CommandStart())
async def start_handler(message: Message, state: FSMContext):
    ensure_user(message.from_user.id, message.from_user.username)
    await state.clear()
    payload = (
        message.text.split(maxsplit=1)[1]
        if len(message.text.split(maxsplit=1)) > 1
        else ""
    )
    if payload.startswith("ref_"):
        try:
            set_referrer(message.from_user.id, int(payload[4:]))
        except ValueError:
            pass
    if payload == "extend":
        from app.handlers.payment import show_sbp_instructions

        await show_sbp_instructions(message, state)
        return
    await message.answer(
        f"🐾 NYX VPN — ваш VPN-сервис в Telegram.\n\n"
        f"Пробный доступ — {get_trial_days()} дня. "
        "Оформить или продлить подписку можно на выбранное количество месяцев.\n"
        "Ссылка подключения — в личном кабинете.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("help"))
@router.message(F.text.in_({"Поддержка", "🧑‍💻 Поддержка"}))
async def help_handler(message: Message):
    await message.answer(
        "🧑‍💻 Поддержка: @nyxsupportvpn\nВремя ответа ~2 часа",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("info"))
@router.message(F.text.in_({"ℹ️ Информация", "Информация"}))
async def info_handler(message: Message):
    await message.answer(
        f"ℹ️ NYX VPN\n\nПробный период — {get_trial_days()} дня.\n"
        "Срок подписки выбирается в месяцах. Оплата — СБП с проверкой чека.\n"
        "Серверы доступны по единой ссылке подписки.\n"
        "Подключение: Happ, v2rayTun или Hiddify.\n\nПомощь: @nyxsupportvpn",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("appstore"))
async def appstore_handler(message: Message):
    await message.answer(APPSTORE_SECTION, reply_markup=main_menu_keyboard())


@router.message(F.text.in_({"🧪 Получить пробник", "Получить пробник"}))
async def trial_button_handler(message: Message, state: FSMContext):
    await state.clear()
    await _request_trial(message.from_user, message)


@router.callback_query(F.data == "trial:request")
async def trial_callback(callback: CallbackQuery):
    await callback.answer()
    await _request_trial(callback.from_user, callback.message)


async def _request_trial(user, message):
    if not sales_enabled():
        await message.answer(
            "Оформление подписки временно недоступно. Поддержка: @nyxsupportvpn"
        )
        return
    ensure_user(user.id, user.username)
    client = RemnawaveClient()
    try:
        with orders.user_lock(user.id):
            entry = orders.open_order(user.id)
            current = await sync_user(user.id, client)
            recovering = (
                entry and entry["kind"] == "trial" and entry["status"] == "applying"
            )
            if (
                not recovering
                and current
                and current["status"] == "ACTIVE"
                and parse_date(current["expireAt"]) > datetime.now(timezone.utc)
            ):
                until = (
                    "бессрочно"
                    if is_unlimited(current)
                    else "до " + parse_date(current["expireAt"]).strftime("%d.%m.%Y")
                )
                await message.answer(
                    f"✅ Ваша подписка активна {until}.\n\n"
                    + vpn_instructions(client.subscription_link(current)),
                    reply_markup=main_menu_keyboard(),
                    disable_web_page_preview=True,
                )
                return
            if not recovering and (is_trial_used(user.id) or current):
                await message.answer(
                    "Пробный период уже использован. Выберите срок для продления.",
                    reply_markup=renew_keyboard(),
                )
                return
            entry = orders.open_order(user.id)
            if entry and entry["kind"] != "trial":
                await message.answer(
                    "Сначала завершите текущий заказ или обратитесь в поддержку."
                )
                return
            if not entry:
                assert_new_user_allowed(user.id)
                old_trial = orders.query(
                    "SELECT id FROM vpn_orders WHERE tg_id=%s AND kind='trial'",
                    (user.id,),
                )
                if old_trial:
                    await message.answer(
                        "Пробный период уже использован.", reply_markup=renew_keyboard()
                    )
                    return
                entry = orders.create_order(user.id, 0, get_trial_days(), 0, "trial")
        entry, current, _ = await apply_order(entry["id"], allow_trial=True)
        await message.answer(
            vpn_instructions(client.subscription_link(current)),
            reply_markup=main_menu_keyboard(),
            disable_web_page_preview=True,
        )
    except RuntimeError as exc:
        await message.answer(str(exc), reply_markup=main_menu_keyboard())
    except Exception:
        logger.exception("Trial failed for %s", user.id)
        await message.answer(
            "Сервис временно недоступен. Повторите чуть позже.",
            reply_markup=main_menu_keyboard(),
        )
    finally:
        await client.close()
