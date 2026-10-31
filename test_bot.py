import asyncio
import os
import sqlite3
import datetime
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# --- Настройки ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else None
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

TARIFFS = {
    "1m": ("1 месяц", 200),
    "3m": ("3 месяца", 500),
    "6m": ("6 месяцев", 900),
    "1y": ("1 год", 1500),
}

# --- БАЗА ДАННЫХ ---
def db_connect():
    conn = sqlite3.connect('weskvpn.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db_connect() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS users (
                            user_id INTEGER PRIMARY KEY, username TEXT,
                            trial_used INTEGER DEFAULT 0, sub_until TEXT,
                            referrer_id INTEGER, is_banned INTEGER DEFAULT 0,
                            reg_date TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS logs (
                            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
                            action TEXT, user_id INTEGER, details TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS payments (
                            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                            amount INTEGER, tariff TEXT, date TEXT)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS admins (
                            user_id INTEGER PRIMARY KEY)''')
        conn.commit()

def is_admin(user_id):
    if user_id == ADMIN_ID: return True
    with db_connect() as conn:
        res = conn.execute('SELECT 1 FROM admins WHERE user_id = ?', (user_id,)).fetchone()
        return bool(res)

def get_user(user_id):
    with db_connect() as conn:
        return conn.execute('SELECT * FROM users WHERE user_id = ?', (user_id,)).fetchone()

def add_user(user_id, username, referrer_id=None):
    with db_connect() as conn:
        if not get_user(user_id):
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute('INSERT INTO users (user_id, username, referrer_id, reg_date) VALUES (?, ?, ?, ?)', (user_id, username, referrer_id, now))
            conn.commit()

def update_trial(user_id):
    with db_connect() as conn:
        conn.execute('UPDATE users SET trial_used = 1 WHERE user_id = ?', (user_id,))
        conn.commit()
    add_log("TRIAL", user_id, "Взял пробник")

def add_days(user_id, days):
    user = get_user(user_id)
    if not user: return
    now = datetime.datetime.now()
    if user['sub_until']:
        current_until = datetime.datetime.fromisoformat(user['sub_until'])
        new_until = (current_until if current_until > now else now) + datetime.timedelta(days=days)
    else:
        new_until = now + datetime.timedelta(days=days)
        
    with db_connect() as conn:
        conn.execute('UPDATE users SET sub_until = ? WHERE user_id = ?', (new_until.isoformat(), user_id))
        conn.commit()
    add_log("SUB_ADD", user_id, f"Выдано {days} дней")

def toggle_ban(user_id, ban_status):
    with db_connect() as conn:
        conn.execute('UPDATE users SET is_banned = ? WHERE user_id = ?', (ban_status, user_id))
        conn.commit()
    add_log("BAN" if ban_status else "UNBAN", user_id, "Забанен" if ban_status else "Разбанен")

def add_log(action, user_id, details):
    with db_connect() as conn:
        now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
        conn.execute('INSERT INTO logs (timestamp, action, user_id, details) VALUES (?, ?, ?, ?)', (now, action, user_id, details))
        conn.commit()

def get_logs():
    with db_connect() as conn:
        return conn.execute('SELECT * FROM logs ORDER BY id DESC LIMIT 10').fetchall()

def add_payment(user_id, amount, tariff):
    with db_connect() as conn:
        now = datetime.datetime.now().isoformat()
        conn.execute('INSERT INTO payments (user_id, amount, tariff, date) VALUES (?, ?, ?, ?)', (user_id, amount, tariff, now))
        conn.commit()

def get_stats():
    with db_connect() as conn:
        total_users = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        total_subs = conn.execute('SELECT COUNT(*) FROM users WHERE sub_until > ?', (datetime.datetime.now().isoformat(),)).fetchone()[0]
        total_money = conn.execute('SELECT SUM(amount) FROM payments').fetchone()[0] or 0
        week_ago = (datetime.datetime.now() - datetime.timedelta(days=7)).isoformat()
        week_money = conn.execute('SELECT SUM(amount) FROM payments WHERE date > ?', (week_ago,)).fetchone()[0] or 0
        week_subs = conn.execute('SELECT COUNT(*) FROM payments WHERE date > ?', (week_ago,)).fetchone()[0]
        return total_users, total_subs, total_money, week_money, week_subs

# --- СОСТОЯНИЯ (FSM) ---
class AdminStates(StatesGroup):
    waiting_broadcast = State()
    waiting_user_id = State()
    waiting_admin_id = State()

# --- КЛАВИАТУРЫ ---
def main_kb(user_id):
    keyboard = [
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="🔌 Подключить подписку", callback_data="subscribe")],
        [InlineKeyboardButton(text="🎁 Пробный период — 3 дня", callback_data="trial")],
        [InlineKeyboardButton(text="📖 Инструкция", callback_data="howto")],
        [InlineKeyboardButton(text="🤝 Пригласить друга", callback_data="ref")],
        [InlineKeyboardButton(text="ℹ️ Документы", callback_data="docs"), InlineKeyboardButton(text="🆘 Поддержка", callback_data="support")],
    ]
    if is_admin(user_id):
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

# --- ПРОВЕРКА НА БАН ---
@dp.callback_query()
async def check_ban(cb: CallbackQuery):
    user = get_user(cb.from_user.id)
    if user and user['is_banned']:
        await cb.answer("⛔️ Вы заблокированы.", show_alert=True)
        return True
    return False

# --- ГЛАВНЫЙ ХЕНДЛЕР ---
@dp.message(Command("start"))
async def start(msg: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    user = get_user(msg.from_user.id)
    if user and user['is_banned']:
        await msg.answer("⛔️ Вы заблокированы в этом боте.")
        return

    ref_id = int(command.args) if command.args and command.args.isdigit() else None
    add_user(msg.from_user.id, msg.from_user.username, ref_id)
    
    if ref_id and ref_id != msg.from_user.id:
        ref_user = get_user(ref_id)
        if ref_user and not ref_user['is_banned']:
            add_days(ref_id, 3)
            try:
                await bot.send_message(ref_id, "🎉 По вашей ссылке зарегистрировался друг! Вам начислено <b>+3 дня</b>.", parse_mode="HTML")
            except: pass
    
    await msg.answer(f"Привет, {msg.from_user.first_name}! 👋\nДобро пожаловать в WeskVPN.", reply_markup=main_kb(msg.from_user.id))

# --- АДМИН ПАНЕЛЬ ---
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if not is_admin(cb.from_user.id): 
        await cb.answer("Доступ запрещен", show_alert=True)
        return
    await cb.message.edit_text("👑 <b>Админ-панель</b>\n\nВыбери действие:", parse_mode="HTML", reply_markup=admin_kb())
    await cb.answer() # <--- ТА САМАЯ МАГИЯ

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): 
        await cb.answer("Доступ запрещен", show_alert=True)
        return
    tu, ts, tm, wm, ws = get_stats()
    text = (f"📊 <b>Статистика</b>\n\n"
            f"👥 Всего юзеров: <b>{tu}</b>\n"
            f"💎 Активных подписок: <b>{ts}</b>\n\n"
            f"💰 <b>Финансы:</b>\n"
            f"За всё время: <b>{tm} ₽</b>\n"
            f"За 7 дней: <b>{wm} ₽</b> ({ws} покупок)\n\n"
            f"<i>(Кнопка симуляции покупки ниже нужна для теста, пока нет RollyPay)</i>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧪 Симулировать покупку (тест)", callback_data="admin_test_pay")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
    ])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer() # <--- МАГИЯ

@dp.callback_query(F.data == "admin_test_pay")
async def admin_test_pay(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    add_payment(cb.from_user.id, 500, "3m")
    add_days(cb.from_user.id, 90)
    await cb.answer("✅ Тестовая покупка на 500₽ записана!", show_alert=True)
    await admin_stats(cb)

@dp.callback_query(F.data.startswith("admin_users:"))
async def admin_users(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    page = int(cb.data.split(":")[1])
    limit = 5
    offset = page * limit
    
    with db_connect() as conn:
        users = conn.execute('SELECT * FROM users LIMIT ? OFFSET ?', (limit, offset)).fetchall()
    
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
    await cb.answer() # <--- МАГИЯ

@dp.callback_query(F.data == "admin_add")
async def admin_add(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: 
        await cb.answer("Только главный админ может добавлять других!", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_admin_id)
    await cb.message.edit_text("🛡 Отправь ID пользователя, которого хочешь сделать админом.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_panel")]]))
    await cb.answer() # <--- МАГИЯ

@dp.message(AdminStates.waiting_admin_id)
async def process_admin_id(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    if not msg.text.isdigit():
        await msg.answer("❌ Нужны только цифры.")
        return
    new_admin = int(msg.text)
    with db_connect() as conn:
        conn.execute('INSERT OR IGNORE INTO admins (user_id) VALUES (?)', (new_admin,))
        conn.commit()
    await state.clear()
    await msg.answer(f"✅ Пользователь <code>{new_admin}</code> теперь админ!", parse_mode="HTML", reply_markup=admin_kb())

# --- УПРАВЛЕНИЕ ЮЗЕРОМ ---
@dp.callback_query(F.data == "admin_find_user")
async def admin_find_user(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminStates.waiting_user_id)
    await cb.message.edit_text("👤 Отправь мне <b>ID пользователя</b> (цифры).", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_panel")]]))
    await cb.answer() # <--- МАГИЯ

@dp.message(AdminStates.waiting_user_id)
async def process_user_id(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    if not msg.text.isdigit():
        await msg.answer("❌ Нужны только цифры.")
        return
    await state.clear()
    await show_user_card(msg, int(msg.text))

@dp.callback_query(F.data.startswith("adm_view:"))
async def adm_view(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    await show_user_card(cb, int(cb.data.split(":")[1]))

async def show_user_card(message_obj, target_id):
    user = get_user(target_id)
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
        await message_obj.answer() # <--- МАГИЯ
    else:
        await message_obj.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("adm_give:"))
async def adm_give(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    target_id = int(cb.data.split(":")[1])
    add_days(target_id, 30)
    await cb.answer("✅ Выдано 30 дней!", show_alert=True)

@dp.callback_query(F.data.startswith("adm_ban:"))
async def adm_ban(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    target_id = int(cb.data.split(":")[1])
    user = get_user(target_id)
    toggle_ban(target_id, 0 if user['is_banned'] else 1)
    await cb.answer("✅ Статус изменен!", show_alert=True)
    await show_user_card(cb, target_id)

# --- ОСТАЛЬНОЕ ---
@dp.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery):
    if await check_ban(cb): return
    user = get_user(cb.from_user.id)
    sub = f"✅ До {datetime.datetime.fromisoformat(user['sub_until']).strftime('%d.%m.%Y')}" if user['sub_until'] else "❌ Не активна"
    await cb.message.edit_text(f"👤 <b>Профиль</b>\n\n🆔 ID: <code>{cb.from_user.id}</code>\n💎 Подписка: {sub}\n📱 Устройств: до 3", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))
    await cb.answer()

@dp.callback_query(F.data == "trial")
async def trial(cb: CallbackQuery):
    if await check_ban(cb): return
    user = get_user(cb.from_user.id)
    if user['trial_used'] == 1:
        await cb.answer("❌ Вы уже использовали пробник!", show_alert=True)
        return
    update_trial(cb.from_user.id)
    add_days(cb.from_user.id, 3)
    await cb.message.edit_text("🎁 <b>Пробный период на 3 дня активирован!</b>\n\n🔗 Ссылка: <code>https://vpn.example.com/trial</code>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))
    await cb.answer()

@dp.callback_query(F.data == "howto")
async def howto(cb: CallbackQuery):
    if await check_ban(cb): return
    await cb.message.edit_text("📖 Скачайте Happ, скопируйте ссылку и вставьте в приложение.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))
    await cb.answer()

@dp.callback_query(F.data == "ref")
async def ref(cb: CallbackQuery):
    if await check_ban(cb): return
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={cb.from_user.id}"
    await cb.message.edit_text(f"🤝 Пригласи друга:\n<code>{ref_link}</code>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))
    await cb.answer()

@dp.callback_query(F.data == "subscribe")
async def subscribe(cb: CallbackQuery):
    if await check_ban(cb): return
    rows = [[InlineKeyboardButton(text=f"{name} — {price} ₽", callback_data=f"pay:{key}")] for key, (name, price) in TARIFFS.items()]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])
    await cb.message.edit_text("🔌 <b>Выберите тариф:</b>\n\nВ каждую подписку входит <b>до 3 устройств</b>.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@dp.callback_query(F.data.startswith("pay:"))
async def pay(cb: CallbackQuery):
    if await check_ban(cb): return
    key = cb.data.split(":")[1]
    name, price = TARIFFS[key]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Согласен с офертой", callback_data=f"agree:{key}")],[InlineKeyboardButton(text="⬅️ Назад", callback_data="subscribe")]])
    await cb.message.edit_text(f"Вы выбрали: <b>{name}</b> за {price} ₽\n\nДля продолжения вы должны согласиться с условиями.", parse_mode="HTML", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data.startswith("agree:"))
async def agree(cb: CallbackQuery):
    if await check_ban(cb): return
    key = cb.data.split(":")[1]
    await cb.message.edit_text(f"💳 <b>Переход к оплате...</b>\n\n⚙️ <i>Прием платежей временно настраивается (ждем RollyPay).</i>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В меню", callback_data="back")]]))
    await cb.answer()

@dp.callback_query(F.data == "docs")
async def docs(cb: CallbackQuery):
    if await check_ban(cb): return
    await cb.message.edit_text("ℹ️ <a href='https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-08-12-99'>Политика</a>\n📄 <a href='https://telegra.ph/PUBLICHNAYA-OFERTA-08-12-15'>Оферта</a>", parse_mode="HTML", disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))
    await cb.answer()

@dp.callback_query(F.data == "support")
async def support(cb: CallbackQuery):
    if await check_ban(cb): return
    await cb.message.edit_text("🆘 Поддержка: @wesksupport", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))
    await cb.answer()

@dp.callback_query(F.data == "back")
async def back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if await check_ban(cb): return
    await cb.message.edit_text("Главное меню:", reply_markup=main_kb(cb.from_user.id))
    await cb.answer()

# --- ЛОГИ И РАССЫЛКА ---
@dp.callback_query(F.data == "admin_logs")
async def admin_logs(cb: CallbackQuery):
    if not is_admin(cb.from_user.id): return
    logs = get_logs()
    text = "📝 <b>Последние 10 событий:</b>\n\n"
    for log in logs:
        text += f"[{log['timestamp']}] {log['action']} (ID: {log['user_id']})\n{log['details']}\n\n"
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]]))
    await cb.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id): return
    await state.set_state(AdminStates.waiting_broadcast)
    await cb.message.edit_text("📢 Отправь мне текст для рассылки.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_panel")]]))
    await cb.answer()

@dp.message(AdminStates.waiting_broadcast)
async def process_broadcast(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id): return
    await state.clear()
    await msg.answer("⏳ Начинаю рассылку...")
    with db_connect() as conn:
        users = conn.execute('SELECT user_id FROM users WHERE is_banned = 0').fetchall()
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
    init_db()
    if WEBHOOK_URL:
        await bot.set_webhook(WEBHOOK_URL)

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
