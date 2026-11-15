import asyncio
import logging
import os

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app import order_storage as orders
from app.config import (
    get_admin_id,
    get_payment_amount,
    get_payment_days,
    get_sbp_phone_number,
    sales_enabled,
)
from app.keyboards.menu import main_menu_keyboard
from app.services.subscriptions import apply_order, check_purchase
from app.storage import ensure_user
from app.utils.email import send_payment_notification

router = Router()
logger = logging.getLogger(__name__)


class PaymentStates(StatesGroup):
    months = State()


class OrderCallback(CallbackData, prefix="order"):
    action: str
    order_id: int


async def show_sbp_instructions(message: Message, state: FSMContext = None):
    if not sales_enabled():
        await message.answer(
            "Оформление подписки временно недоступно. Поддержка: @nyxsupportvpn"
        )
        return
    if state:
        await state.set_state(PaymentStates.months)
    await message.answer(
        f"💳 На сколько месяцев оформить VPN?\n\n"
        f"Введите целое число от 1 до 120.\n"
        f"1 месяц = {get_payment_days()} дней, стоимость {get_payment_amount()} ₽.\n"
        "Для отмены — /cancel.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("extend"))
@router.message(F.text == "💳 Продлить подписку")
async def extend_handler(message: Message, state: FSMContext):
    ensure_user(message.from_user.id, message.from_user.username)
    await show_sbp_instructions(message, state)


@router.message(Command("cancel"), PaymentStates.months)
async def cancel_months(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Выбор срока отменён.", reply_markup=main_menu_keyboard())


def payment_text(entry):
    return (
        f"💳 VPN на {entry['months']} мес. ({entry['days']} дней) — {entry['amount']} ₽\n\n"
        f"Переведите {entry['amount']} ₽ по СБП на номер:\n{get_sbp_phone_number()}\n\n"
        f"Пришлите фото чека сюда. Заказ №{entry['id']}.\n"
        "После проверки администратором подписка будет продлена."
    )


@router.message(PaymentStates.months, F.text.regexp(r"^\s*[0-9]+\s*$"))
async def select_months(message: Message, state: FSMContext):
    if not sales_enabled():
        await state.clear()
        await message.answer("Оформление подписки временно недоступно.")
        return
    raw = message.text.strip()
    if len(raw) > 3 or not 1 <= int(raw) <= 120:
        await message.answer("Введите количество месяцев от 1 до 120.")
        return
    tg_id = message.from_user.id
    try:
        with orders.user_lock(tg_id):
            await check_purchase(tg_id)
            entry = orders.open_order(tg_id)
            if entry and entry["status"] != "draft":
                await message.answer(
                    "Ваш чек уже на проверке. Дождитесь решения по текущему заказу."
                )
                await state.clear()
                return
            if entry:
                orders.query(
                    "UPDATE vpn_orders SET status='rejected' WHERE id=%s",
                    (entry["id"],),
                )
            months = int(raw)
            entry = orders.create_order(
                tg_id,
                months,
                months * get_payment_days(),
                months * get_payment_amount(),
            )
        await state.clear()
        await message.answer(payment_text(entry), reply_markup=main_menu_keyboard())
    except RuntimeError as exc:
        await message.answer(str(exc))
    except Exception:
        logger.warning("Cannot validate purchase for %s", tg_id)
        await message.answer("Не удалось проверить подписку. Попробуйте чуть позже.")


@router.message(
    PaymentStates.months,
    F.text,
    ~F.text.startswith("/"),
    ~F.text.in_(
        {
            "🧪 Получить пробник",
            "👤 Личный кабинет",
            "👥 Пригласить друга",
            "ℹ️ Информация",
            "📡 Серверы",
            "🧑‍💻 Поддержка",
        }
    ),
)
async def invalid_months(message: Message):
    await message.answer("Введите целое количество месяцев от 1 до 120 или /cancel.")


@router.message(StateFilter(None, PaymentStates.months), F.photo)
async def photo_handler(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    ensure_user(tg_id, message.from_user.username)
    with orders.user_lock(tg_id):
        entry = orders.open_order(tg_id)
        if not entry:
            await show_sbp_instructions(message, state)
            return
        if entry["kind"] != "payment":
            await message.answer("Сначала завершите получение пробного доступа.")
            return
        if entry["status"] == "draft":
            entry = orders.submit_receipt(tg_id, message.chat.id, message.message_id)
        # Resending a receipt forwards the same order, never creates a second credit.
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="✅ Подтвердить",
                        callback_data=OrderCallback(
                            action="approve", order_id=entry["id"]
                        ).pack(),
                    ),
                    InlineKeyboardButton(
                        text="❌ Отклонить",
                        callback_data=OrderCallback(
                            action="reject", order_id=entry["id"]
                        ).pack(),
                    ),
                ]
            ]
        )
        await message.bot.send_photo(
            get_admin_id(),
            message.photo[-1].file_id,
            caption=f"Заказ №{entry['id']} · ID {tg_id}\nСрок: {entry['months']} мес. / {entry['days']} дней\nСумма: {entry['amount']} ₽",
            reply_markup=keyboard,
        )
    asyncio.create_task(
        send_payment_notification(message.from_user.username or f"tg_{tg_id}", tg_id)
    )
    await message.answer("📸 Чек отправлен на проверку. Ожидайте подтверждения.")


@router.callback_query(OrderCallback.filter())
async def review_payment(callback: CallbackQuery, callback_data: OrderCallback):
    if callback.from_user.id != get_admin_id():
        await callback.answer("Только для администратора", show_alert=True)
        return
    entry = orders.order(callback_data.order_id)
    if not entry or entry["kind"] != "payment":
        await callback.answer("Заказ не найден", show_alert=True)
        return
    await callback.answer("Обрабатываю…")
    try:
        if callback_data.action == "reject":
            with orders.user_lock(entry["tg_id"]):
                result = orders.query(
                    "UPDATE vpn_orders SET status='rejected', updated_at=NOW() WHERE id=%s AND status='submitted' RETURNING id",
                    (entry["id"],),
                )
            if not result:
                await callback.message.answer(
                    "Заказ уже обработан или начисление начато. Отклонение недоступно."
                )
                return
            await callback.bot.send_message(
                entry["tg_id"],
                "❌ Оплата не подтверждена. Напишите @nyxsupportvpn, если нужна помощь.",
            )
            label = "ОТКЛОНЕНО"
        elif callback_data.action == "approve":
            if not sales_enabled():
                await callback.message.answer("Начисления приостановлены.")
                return
            entry, user, reward = await apply_order(entry["id"])
            label = "ОДОБРЕНО"
            if user:
                from app.vpn_instructions import vpn_instructions

                link = os.environ["REMNA_SUB_URL"].rstrip("/") + "/" + user["shortUuid"]
                await callback.bot.send_message(
                    entry["tg_id"],
                    f"✅ Оплата подтверждена. VPN на {entry['months']} мес.\n"
                    f"Срок до {entry['target_end'].strftime('%d.%m.%Y')}.\n\n"
                    + vpn_instructions(link),
                    reply_markup=main_menu_keyboard(),
                    disable_web_page_preview=True,
                )
                if reward:
                    try:
                        await callback.bot.send_message(
                            reward["referrer_tg_id"],
                            f"🎁 Ваш друг оплатил VPN. Начислено {reward['credited']} ₽.",
                        )
                    except Exception:
                        logger.warning("Referral notification failed")
        else:
            return
        await callback.message.edit_caption(
            caption=(callback.message.caption or "") + f"\n\n{label}", reply_markup=None
        )
    except Exception:
        logger.exception("Order processing failed: %s", entry["id"])
        await callback.message.answer(
            "Не удалось завершить обработку. Повторное подтверждение того же заказа безопасно; проверьте статус панели."
        )


@router.callback_query(F.data.startswith("payment:"))
async def legacy_receipt(callback: CallbackQuery):
    if callback.from_user.id != get_admin_id():
        await callback.answer("Только для администратора", show_alert=True)
        return
    await callback.answer(
        "Старый чек: требуется сверка ранее оплаченных услуг. Автоматическое начисление отключено.",
        show_alert=True,
    )


@router.callback_query(F.data == "renew:select")
async def renew_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    ensure_user(callback.from_user.id, callback.from_user.username)
    await show_sbp_instructions(callback.message, state)
