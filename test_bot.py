import asyncio
import os
import datetime
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.dispatcher.event.bases import SkipHandler

# --- Настройки ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else None
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None # Глобальный пул соединений

TARIFFS = {
    "1m": ("1 мес · 3 устр.", 200),
    "3m": ("3 мес · 3 устр. 🔥 -25%", 450),
    "6m": ("6 мес · 3 устр. 🔥 -33%", 800),
    "1y": ("1 год · 5 устр. 🔥 -42%", 1400),
}

# --- БАЗА ДАННЫХ (PostgreSQL / asyncpg) ---
async def init_db():
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY, username TEXT,
                trial_used INTEGER DEFAULT 0, sub_until TEXT,
                referrer_id BIGINT, is_banned INTEGER DEFAULT 0,
                reg_date TEXT, consented INTEGER DEFAULT 0)
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                id SERIAL PRIMARY KEY, timestamp TEXT,
                action TEXT, user_id BIGINT, details TEXT)
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY, user_id BIGINT,
                amount INTEGER, tariff TEXT, date TEXT)
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY)
        ''')

async def is_admin(user_id):
    if user_id == ADMIN_ID: return True
    async with db_pool.acquire() as conn:
        res = await conn.fetchval('SELECT 1 FROM admins WHERE user_id = $1', user_id)
        return bool(res)

async def get_user(user_id):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow('SELECT * FROM users WHERE user_id = $1', user_id)

async def add_user(user_id, username, referrer_id=None):
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow('SELECT user_id FROM users WHERE user_id = $1', user_id)
        if not user:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await conn.execute(
                'INSERT INTO users (user_id, username, referrer_id, reg_date, consented) VALUES ($1, $2, $3, $4, 1)', 
                user_id, username, referrer_id, now
            )

async def update_trial(user_id):
    async with db_pool.acquire() as conn:
        await conn.execute('UPDATE users SET trial_used = 1 WHERE user_id = $1', user_id)
    await add_log("TRIAL", user_id, "Взял пробник")

async def add_days(user_id, days):
    user = await get_user(user_id)
    if not user: return
    now = datetime.datetime.now()
    
    if user['sub_until']:
        current_until = datetime.datetime.fromisoformat(user['sub_until'])
        new_until = (current_until if current_until > now else now) + datetime.timedelta(days=days)
    else:
        new_until = now + datetime.timedelta(days=days)
        
    async with db_pool.acquire() as conn:
        await conn.execute('UPDATE users SET sub_until = $1 WHERE user_id = $2', new_until.isoformat(), user_id)
    await add_log("SUB_ADD", user_id, f"Выдано {days} дней")

async def toggle_ban(user_id, ban_status):
    async with db_pool.acquire() as conn:
        await conn.execute('UPDATE users SET is_banned = $1 WHERE user_id = $2', ban_status, user_id)
    await add_log("BAN" if ban_status else "UNBAN", user_id, "Забанен" if ban_status else "Разбанен")

async def add_log(action, user_id, details):
    async with db_pool.acquire() as conn:
        now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
        await conn.execute('INSERT INTO logs (timestamp, action, user_id, details) VALUES ($1, $2, $3, $4)', now, action, user_id, details)

async def get_logs():
    async with db_pool.acquire() as conn:
        return await conn.fetch('SELECT * FROM logs ORDER BY id DESC LIMIT 10')

async def add_payment(user_id, amount, tariff):
    async with db_pool.acquire() as conn:
        now = datetime.datetime.now().isoformat()
        await conn.execute('INSERT INTO payments (user_id, amount, tariff, date) VALUES ($1, $2, $3, $4)', user_id, amount, tariff, now)

async def get_stats():
    async with db_pool.acquire() as conn:
        total_users = await conn.fetchval('SELECT COUNT(*) FROM users')
        total_subs = await conn.fetchval('SELECT COUNT(*) FROM users WHERE sub_until > $1', datetime.datetime.now().isoformat())
        total_money = await conn.fetchval('SELECT SUM(amount) FROM payments') or 0
        week_ago = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat()
        week_money = await conn.fetchval('SELECT SUM(amount) FROM payments WHERE date > $1', week_ago) or 0
        week_subs = await conn.fetchval('SELECT COUNT(*) FROM payments WHERE date > $1', week_ago)
        return total_users, total_subs, total_money, week_money, week_subs

# --- СОСТОЯНИЯ (FSM) ---
class AdminStates(StatesGroup):
    waiting_broadcast = State()
    waiting_user_id = State()
    waiting_admin_id = State()

# --- КЛАВИАТУРЫ ---
async def main_kb(user_id):
    keyboard = [
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🔌 Подключить подписку", callback_data="subscribe")],
        [InlineKeyboardButton(text="🎁 Пробный период — 3 дня", callback_data="trial")],
        [InlineKeyboardButton(text="📖 Инструкция", callback_data="howto")],
        [InlineKeyboardButton(text="🤝 Пригласить друга", callback_data="ref")],
        [InlineKeyboardButton(text="ℹ️ Документы", callback_data="docs"), InlineKeyboardButton(text="🆘 Поддержка", callback_data="support")],
    ]
    if await is_admin(user_id):
        keyboard.append([InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика и Деньги", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👥 Список юзеров", callback_data="admin_users:0")],
        [InlineKeyboardButton(text="👤 Найти юзера по ID", callback_data="admin_find_user")],
        [InlineKeyboardButton(text="📝 Логи", callback_data="admin_logs"), InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="🛡 Добавить админа", callback_data="admin_add")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back")]
    ])

def consent_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Согласен с условиями", callback_data="consent_agree")]
    ])

# --- ПРОВЕРКА НА БАН ---
@dp.callback_query()
async def check_ban(cb: CallbackQuery):
    user = await get_user(cb.from_user.id)
    if user and user['is_banned']:
        await cb.answer("⛔️ Вы заблокированы.", show_alert=True)
        return
    raise SkipHandler()

# --- ГЛАВНЫЙ ХЕНДЛЕР ---
@dp.message(Command("start"))
async def start(msg: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    user = await get_user(msg.from_user.id)
    
    if user and user['is_banned']:
        await msg.answer("⛔️ Вы заблокированы в этом боте.")
        return

    if not user:
        consent_text = (
            "👋 Добро пожаловать в WeskVPN!\n\n"
            "Для использования сервиса нам необходимо ваше согласие на обработку персональных данных.\n\n"
            "🔒 <b>Какие данные мы собираем:</b>\n"
            "• Telegram ID (для управления подпиской)\n"
            "• Никнейм (для связи с поддержкой)\n\n"
            "🚫 <b>Мы НЕ собираем:</b> ваши номера телефонов, ФИО или данные банковских карт.\n\n"
            "Нажимая кнопку ниже, вы подтверждаете согласие с нашей Политикой конфиденциальности."
        )
        await msg.answer(consent_text, parse_mode="HTML", reply_markup=consent_kb())
        return

    ref_id = int(command.args) if command.args and command.args.isdigit() else None
    
    if ref_id and ref_id != msg.from_user.id:
        ref_user = await get_user(ref_id)
        if ref_user and not ref_user['is_banned']:
            await add_days(ref_id, 3)
            try:
                await bot.send_message(ref_id, "🎉 По вашей ссылке зарегистрировался друг! Вам начислено <b>+3 дня</b>.", parse_mode="HTML")
            except: pass
    
    await msg.answer(f"Привет, {msg.from_user.first_name}! 👋\nДобро пожаловать в WeskVPN.", reply_markup=await main_kb(msg.from_user.id))

# --- ОБРАБОТКА СОГЛАСИЯ ---
@dp.callback_query(F.data == "consent_agree")
async def consent_agree(cb: CallbackQuery):
    await add_user(cb.from_user.id, cb.from_user.username)
    await cb.message.edit_text(
        f"Отлично! Добро пожаловать в WeskVPN, {cb.from_user.first_name}. 👋",
        reply_markup=await main_kb(cb.from_user.id)
    )
    await cb.answer()

# --- АДМИН ПАНЕЛЬ ---
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await is_admin(cb.from_user.id): 
        await cb.answer("Доступ запрещен", show_alert=True)
        return
    await cb.message.edit_text("👑 <b>Админ-панель</b>\n\nВыбери действие:", parse_mode="HTML", reply_markup=admin_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    tu, ts, tm, wm, ws = await get_stats()
    text = (f"📊 <b>Статистика</b>\n\n"
            f"👥 Всего юзеров: <b>{tu}</b>\n"
            f"💎 Активных подписок: <b>{ts}</b>\n\n"
            f"💰 <b>Финансы:</b>\n"
            f"За всё время: <b>{tm} ₽</b>\n"
            f"За 7 дней: <b>{wm} ₽</b> ({ws} покупок)\n\n"
            f"<i>(Кнопка симуляции покупки ниже нужна для теста)</i>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧪 Симулировать покупку (тест)", callback_data="admin_test_pay")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
    ])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data == "admin_test_pay")
async def admin_test_pay(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    await add_payment(cb.from_user.id, 500, "3m")
    await add_days(cb.from_user.id, 90)
    await cb.answer("✅ Тестовая покупка на 500₽ записана!", show_alert=True)
    await admin_stats(cb)

@dp.callback_query(F.data.startswith("admin_users:"))
async def admin_users(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    page = int(cb.data.split(":")[1])
    limit = 5
    offset = page * limit
    
    async with db_pool.acquire() as conn:
        users = await conn.fetch('SELECT * FROM users LIMIT $1 OFFSET $2', limit, offset)
    
    text = f"👥 <b>Список юзеров (Стр. {page+1})</b>\n\n"
    kb_buttons = []
    
    for u in users:
        status = "⛔" if u['is_banned'] else "✅"
        text += f"{status} <code>{u['user_id']}</code> | @{u['username'] or '—'}\n"
        kb_buttons.append([InlineKeyboardButton(text=f"Управлять {u['user_id']}", callback_data=f"adm_view:{u['user_id']}")])
        
    nav_buttons = []
    if page > 0: nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_users:{page-1}"))
    if len(users) == limit: nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"admin_users:{page+1}"))
    
    if nav_buttons: kb_buttons.append(nav_buttons)
    kb_buttons.append([InlineKeyboardButton(text="⬅️ В админку", callback_data="admin_panel")])
    
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))
    await cb.answer()

@dp.callback_query(F.data == "admin_add")
async def admin_add(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: 
        await cb.answer("Только главный админ может добавлять других!", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_admin_id)
    await cb.message.edit_text("🛡 Отправь ID пользователя, которого хочешь сделать админом.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_panel")]]))
    await cb.answer()

@dp.message(AdminStates.waiting_admin_id)
async def process_admin_id(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    if not msg.text.isdigit():
        await msg.answer("❌ Нужны только цифры.")
        return
    new_admin = int(msg.text)
    async with db_pool.acquire() as conn:
        await conn.execute('INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING', new_admin)
    await state.clear()
    await msg.answer(f"✅ Пользователь <code>{new_admin}</code> теперь админ!", parse_mode="HTML", reply_markup=admin_kb())

# --- УПРАВЛЕНИЕ ЮЗЕРОМ ---
@dp.callback_query(F.data == "admin_find_user")
async def admin_find_user(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id): return
    await state.set_state(AdminStates.waiting_user_id)
    await cb.message.edit_text("👤 Отправь мне <b>ID пользователя</b> (цифры).", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_panel")]]))
    await cb.answer()

@dp.message(AdminStates.waiting_user_id)
async def process_user_id(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id): return
    if not msg.text.isdigit():
        await msg.answer("❌ Нужны только цифры.")
        return
    await state.clear()
    await show_user_card(msg, int(msg.text))

@dp.callback_query(F.data.startswith("adm_view:"))
async def adm_view(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    await show_user_card(cb, int(cb.data.split(":")[1]))

async def show_user_card(message_obj, target_id):
    user = await get_user(target_id)
    if not user:
        await message_obj.answer("❌ Пользователь не найден.")
        return
    ban = "⛔️ Забанен" if user['is_banned'] else "✅ Активен"
    sub = f"До {datetime.datetime.fromisoformat(user['sub_until']).strftime('%d.%m.%Y')}" if user['sub_until'] else "Нет"
    
    text = (f"👤 <b>Юзер</b>\nID: <code>{user['user_id']}</code>\nНик: @{user['username'] or '—'}\n"
            f"Статус: {ban}\nПодписка: {sub}\nПробник: {'Да' if user['trial_used'] else 'Нет'}")
            
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 +30 дней", callback_data=f"adm_give:{target_id}")],
        [InlineKeyboardButton(text="⛔️ Забанить" if not user['is_banned'] else "✅ Разбанить", callback_data=f"adm_ban:{target_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
    ])
    if isinstance(message_obj, CallbackQuery):
        await message_obj.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await message_obj.answer()
    else:
        await message_obj.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("adm_give:"))
async def adm_give(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    target_id = int(cb.data.split(":")[1])
    await add_days(target_id, 30)
    await cb.answer("✅ Выдано 30 дней!", show_alert=True)

@dp.callback_query(F.data.startswith("adm_ban:"))
async def adm_ban(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    target_id = int(cb.data.split(":")[1])
    user = await get_user(target_id)
    await toggle_ban(target_id, 0 if user['is_banned'] else 1)
    await cb.answer("✅ Статус изменен!", show_alert=True)
    await show_user_card(cb, target_id)

# --- ОСТАЛЬНОЕ ---
@dp.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery):
    user = await get_user(cb.from_user.id)
    sub = f"✅ До {datetime.datetime.fromisoformat(user['sub_until']).strftime('%d.%m.%Y')}" if user['sub_until'] else "❌ Не активна"
    await cb.message.edit_text(f"👤 <b>Профиль</b>\n\n🆔 ID: <code>{cb.from_user.id}</code>\n💎 Подписка: {sub}\n📱 Устройств: до 3", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))
    await cb.answer()

@dp.callback_query(F.data == "trial")
async def trial(cb: CallbackQuery):
    user = await get_user(cb.from_user.id)
    if user['trial_used'] == 1:
        await cb.answer("❌ Вы уже использовали пробник!", show_alert=True)
        return
    await update_trial(cb.from_user.id)
    await add_days(cb.from_user.id, 3)
    await cb.message.edit_text("🎁 <b>Пробный период на 3 дня активирован!</b>\n\n🔗 Ссылка: <code>https://vpn.example.com/trial</code>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))
    await cb.answer()

@dp.callback_query(F.data == "howto")
async def howto(cb: CallbackQuery):
    text = (
        "📖 <b>Как подключиться к WeskVPN?</b>\n\n"
        "1️⃣ Скачайте приложение <b>Happ</b> для вашего устройства (ссылки ниже).\n"
        "2️⃣ Скопируйте ссылку-ключ, которую выдал вам бот.\n"
        "3️⃣ Откройте Happ, нажмите «+» (Добавить) и вставьте скопированную ссылку.\n"
        "4️⃣ Нажмите «Подключиться» и пользуйтесь!\n\n"
        "🔗 <b>Скачать Happ:</b>"
    )
    keyboard = [
        [InlineKeyboardButton(text="🍏 iOS (iPhone/iPad)", url="https://apps.apple.com/us/app/happ-proxy-utility/id6504287215")],
        [InlineKeyboardButton(text="🤖 Android (APK)", url="https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk")],
        [InlineKeyboardButton(text="📺 Smart TV (Android TV)", url="https://play.google.com/store/apps/details?id=com.happproxy")],
        [InlineKeyboardButton(text="🍏 macOS (Mac)", url="https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.macOS.universal.dmg")],
        [InlineKeyboardButton(text="🐧 Linux (.deb)", url="https://github.com/Happ-proxy/happ-desktop/releases/latest/download/Happ.linux.x64.deb")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ]
    await cb.message.edit_text(text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await cb.answer()

@dp.callback_query(F.data == "ref")
async def ref(cb: CallbackQuery):
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={cb.from_user.id}"
    await cb.message.edit_text(f"🤝 Пригласи друга:\n<code>{ref_link}</code>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))
    await cb.answer()

@dp.callback_query(F.data == "subscribe")
async def subscribe(cb: CallbackQuery):
    rows = [[InlineKeyboardButton(text=f"{name} — {price} ₽", callback_data=f"pay:{key}")] for key, (name, price) in TARIFFS.items()]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])
    await cb.message.edit_text("🔌 <b>Выберите тариф:</b>\n\nОплата картой РФ (СБП) или криптовалютой.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@dp.callback_query(F.data.startswith("pay:"))
async def pay(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    name, price = TARIFFS[key]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Согласен с офертой", callback_data=f"agree:{key}")],[InlineKeyboardButton(text="⬅️ Назад", callback_data="subscribe")]])
    await cb.message.edit_text(f"Вы выбрали: <b>{name}</b> за {price} ₽\n\nДля продолжения вы должны согласиться с условиями оферты.", parse_mode="HTML", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data.startswith("agree:"))
async def agree(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    await cb.message.edit_text(f"💳 <b>Переход к оплате...</b>\n\n⚙️ <i>Прием платежей временно настраивается (ждем RollyPay).</i>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="back")]]))
    await cb.answer()

@dp.callback_query(F.data == "docs")
async def docs(cb: CallbackQuery):
    await cb.message.edit_text("ℹ️ <a href='https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-08-12-99'>Политика</a>\n📄 <a href='https://telegra.ph/PUBLICHNAYA-OFERTA-08-12-15'>Оферта</a>", parse_mode="HTML", disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))
    await cb.answer()

@dp.callback_query(F.data == "support")
async def support(cb: CallbackQuery):
    await cb.message.edit_text("🆘 Поддержка: @wesksupport", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))
    await cb.answer()

@dp.callback_query(F.data == "back")
async def back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Главное меню:", reply_markup=await main_kb(cb.from_user.id))
    await cb.answer()

# --- ЛОГИ И РАССЫЛКА ---
@dp.callback_query(F.data == "admin_logs")
async def admin_logs(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    logs = await get_logs()
    text = "📝 <b>Последние 10 событий:</b>\n\n"
    for log in logs:
        text += f"[{log['timestamp']}] {log['action']} (ID: {log['user_id']})\n{log['details']}\n\n"
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]]))
    await cb.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id): return
    await state.set_state(AdminStates.waiting_broadcast)
    await cb.message.edit_text("📢 Отправь мне текст для рассылки.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_panel")]]))
    await cb.answer()

@dp.message(AdminStates.waiting_broadcast)
async def process_broadcast(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id): return
    await state.clear()
    await msg.answer("⏳ Начинаю рассылку...")
    
    async with db_pool.acquire() as conn:
        users = await conn.fetch('SELECT user_id FROM users WHERE is_banned = 0')
        
    success, failed = 0, 0
    for user in users:
        try:
            await bot.send_message(user['user_id'], msg.text, parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.05)
        except: failed += 1
    await msg.answer(f"✅ Доставлено: {success}\n🚫 Ошибок: {failed}", parse_mode="HTML", reply_markup=admin_kb())

# --- ЗАПУСК ---
async def on_startup(bot: Bot):
    global db_pool
    print("Подключение к Supabase...")
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    await init_db()
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)
    print("Бот успешно подключен к внешней базе!")

async def main():
    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    app.router.add_get("/", lambda r: web.Response(text="Bot is alive!"))
    dp.startup.register(on_startup)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print("Бот запущен!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
