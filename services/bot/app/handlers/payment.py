"""Purchase preview for moderation; payment processing is not connected yet."""

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.config import get_payment_amount, get_payment_days
from app.keyboards.menu import main_menu_keyboard

router = Router()
PAYMENT_UNAVAILABLE = "Оплата временно недоступна. Платёжная система ещё не подключена."


class PaymentStates(StatesGroup):
    months = State()


def purchase_text(months: int) -> str:
    if not 1 <= months <= 120:
        raise ValueError("Количество месяцев должно быть от 1 до 120")
    days = months * get_payment_days()
    amount = months * get_payment_amount()
    return (
        "📦 Тариф «NYX VPN»\n\n"
        "Доступ к VPN по единой ссылке подписки.\n"
        "Серверы: Россия, Нидерланды и Германия.\n\n"
        f"Срок: {months} мес. ({days} дней)\n"
        f"Стоимость: {amount} ₽\n\n"
        "Нажмите «Оплатить», чтобы перейти к оплате."
    )


def purchase_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить", callback_data="checkout:pay")],
            [
                InlineKeyboardButton(
                    text="Изменить срок", callback_data="checkout:months"
                )
            ],
        ]
    )


async def show_purchase(message: Message, state: FSMContext):
    await state.set_state(PaymentStates.months)
    await message.answer(
        purchase_text(1)
        + "\n\nДля другого срока введите количество месяцев от 1 до 120. "
        "Для отмены — /cancel.",
        reply_markup=purchase_keyboard(),
    )


@router.message(Command("buy", "extend"))
@router.message(F.text.in_({"💳 Купить подписку", "💳 Продлить подписку"}))
async def buy_handler(message: Message, state: FSMContext):
    await show_purchase(message, state)


@router.message(Command("cancel"), PaymentStates.months)
async def cancel_months(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Выбор срока отменён.", reply_markup=main_menu_keyboard())


@router.message(PaymentStates.months, F.text.regexp(r"^\s*[0-9]+\s*$"))
async def select_months(message: Message, state: FSMContext):
    raw = message.text.strip()
    if len(raw) > 3 or not 1 <= int(raw) <= 120:
        await message.answer("Введите количество месяцев от 1 до 120.")
        return
    await state.clear()
    await message.answer(purchase_text(int(raw)), reply_markup=purchase_keyboard())


@router.callback_query(F.data == "checkout:months")
async def change_months(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(PaymentStates.months)
    await callback.message.answer(
        "Введите количество месяцев от 1 до 120.\n"
        f"1 месяц — {get_payment_days()} дней, {get_payment_amount()} ₽.\n"
        "Для отмены — /cancel."
    )


@router.callback_query(F.data == "checkout:pay")
async def pay_unavailable(callback: CallbackQuery):
    # No transactions, orders, callbacks to a provider, or access grants in this stage.
    await callback.answer(PAYMENT_UNAVAILABLE, show_alert=True)


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
            "ПОЛИТИКА",
        }
    ),
)
async def invalid_months(message: Message):
    await message.answer("Введите целое количество месяцев от 1 до 120 или /cancel.")


@router.message(StateFilter(None, PaymentStates.months), F.photo)
async def retired_receipt(message: Message):
    await message.answer(
        "Фотографии чеков больше не принимаются. "
        "Откройте «💳 Купить подписку», чтобы выбрать тариф.",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data.startswith("order:"))
@router.callback_query(F.data.startswith("payment:"))
async def retired_payment(callback: CallbackQuery):
    await callback.answer(
        "Этот способ оплаты больше не используется. Откройте «Купить подписку».",
        show_alert=True,
    )


@router.callback_query(F.data == "renew:select")
async def renew_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await show_purchase(callback.message, state)
