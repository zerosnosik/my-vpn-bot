import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton


BOT_TOKEN = "9140886899:AAF85RPP3LGXnyZXqNawmDLRxAybPc3YTAk"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

TARIFFS = {
    "1m": ("1 месяц", 200),
    "3m": ("3 месяца", 500),
    "6m": ("6 месяцев", 900),
    "1y": ("1 год", 1500),
}

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🔌 Подключить подписку", callback_data="subscribe")],
        [InlineKeyboardButton(text="🎟 Промокод", callback_data="promo")],
        [InlineKeyboardButton(text="🎁 Пробный период — 5 дней", callback_data="trial")],
    ])

@dp.message(Command("start"))
async def start(msg: Message):
    await msg.answer(
        f"Привет, {msg.from_user.first_name}! 👋\n\n"
        f"Это тестовая версия VPN-бота.\nВыбери действие:",
        reply_markup=main_kb()
    )

@dp.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery):
    nick = f"@{cb.from_user.username}" if cb.from_user.username else "—"
    await cb.message.edit_text(
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{cb.from_user.id}</code>\n"
        f"📛 Ник: {nick}\n\n"
        f"❌ Подписка не оплачена",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", callback_data="subscribe")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
        ])
    )
    await cb.answer()

@dp.callback_query(F.data == "subscribe")
async def subscribe(cb: CallbackQuery):
    rows = [[InlineKeyboardButton(
        text=f"{name} — {price} ₽",
        callback_data=f"pay:{key}"
    )] for key, (name, price) in TARIFFS.items()]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])
    await cb.message.edit_text(
        "Выберите тариф:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("pay:"))
async def pay(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    name, price = TARIFFS[key]
    await cb.message.edit_text(
        f"✅ Тест: выбран тариф «{name}» на {price} ₽.\n\n"
        f"Это тестовая версия — реальная оплата будет позже.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
        ])
    )
    await cb.answer()

@dp.callback_query(F.data == "promo")
async def promo(cb: CallbackQuery):
    await cb.message.edit_text(
        "🎟 Тест: промокод WELCOME3 дал бы 3 дня бесплатно.\n"
        "(В боевой версии здесь будет форма ввода кода)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
        ])
    )
    await cb.answer()

@dp.callback_query(F.data == "trial")
async def trial(cb: CallbackQuery):
    await cb.message.edit_text(
        "🎁 Тест: пробный период на 5 дней активирован!\n\n"
        "🔗 Ссылка (демо): <code>https://vpn.example.com/demo-123</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
        ])
    )
    await cb.answer()

@dp.callback_query(F.data == "back")
async def back(cb: CallbackQuery):
    await cb.message.edit_text("Главное меню:", reply_markup=main_kb())
    await cb.answer()

async def main():
    print("Бот запущен! Откройте Telegram и напишите боту /start")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())