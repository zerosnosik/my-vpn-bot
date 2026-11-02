import asyncio
import os
import datetime
import asyncpg
import hmac
import hashlib
import json
from typing import Union
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import ErrorEvent

# --- Настройки ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
NOTIFY_BOT_TOKEN = os.getenv("NOTIFY_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")
ROLLYPAY_API_KEY = os.getenv("ROLLYPAY_API_KEY")
ROLLYPAY_TERMINAL_ID = os.getenv("ROLLYPAY_TERMINAL_ID")
ROLLYPAY_SIGNING_SECRET = os.getenv("ROLLYPAY_SIGNING_SECRET")
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else None
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
notify_bot = Bot(token=NOTIFY_BOT_TOKEN) if NOTIFY_BOT_TOKEN else bot
dp = Dispatcher()
db_pool = None 

# --- МАТРИЦА ТАРИФОВ (Первый тариф тестовый за 10₽) ---
TARIFFS = {
    "3_1m": ("1 мес · 3 устр. (ТЕСТ 10₽)", 10, 3), 
    "3_3m": ("3 мес · 3 устр. 🔥 -25%", 450, 3),
    "3_6m": ("6 мес · 3 устр. 🔥 -33%", 800, 3), 
    "3_1y": ("1 год · 3 устр. 🔥 -42%", 1400, 3),
    "6_1m": ("1 мес · 6 устр.", 350, 6), 
    "6_3m": ("3 мес · 6 устр. 🔥 -24%", 800, 6),
    "6_6m": ("6 мес · 6 устр. 🔥 -33%", 1400, 6), 
    "6_1y": ("1 год · 6 устр. 🔥 -40%", 2500, 6),
    "9_1m": ("1 мес · 9 устр.", 500, 9), 
    "9_3m": ("3 мес · 9 устр. 🔥 -20%", 1200, 9),
    "9_6m": ("6 мес · 9 устр. 🔥 -33%", 2000, 9), 
    "9_1y": ("1 год · 9 устр. 🔥 -42%", 3500, 9),
}

# --- ФУНКЦИИ УВЕДОМЛЕНИЙ ---
async def notify_payment_success(nick, user_id, tariff, price, devices):
    text = (
        f"👑 <b>ПОЗДРАВЛЯЮ, БОСС!</b> 🎉\n\n"
        f"👤 Клиент: <b>{nick}</b> (ID: <code>{user_id}</code>)\n"
        f"💎 Купил у вас подписку на <b>{tariff}</b>!\n"
        f"📱 Устройств: <b>{devices}</b>\n"
        f"💰 Сумма: <b>{price} ₽</b>\n\n"
        f"📈 <i>WeskVPN становится только сильнее. Так держать!</i>"
    )
    try: await notify_bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception: pass

async def notify_urgent(title: str, details: str):
    text = (
        f"🚨🚨🚨 <b>СРОЧНОЕ УВЕДОМЛЕНИЕ!</b> 🚨🚨🚨\n\n"
        f"⚠️ <b>{title.upper()}</b> ⚠️\n\n"
        f"📝 <b>ДЕТАЛИ:</b>\n<code>{details[:300]}</code>\n\n"
        f"🛠 <i>ТРЕБУЕТСЯ ТВОЕ ВНИМАНИЕ!</i>"
    )
    try: await notify_bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception: pass

# --- БАЗА ДАННЫХ ---
async def init_db():
    async with db_pool.acquire() as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, username TEXT, trial_used INTEGER DEFAULT 0, sub_until TEXT, referrer_id BIGINT, is_banned INTEGER DEFAULT 0, reg_date TEXT, consented INTEGER DEFAULT 0, devices_limit INTEGER DEFAULT 3)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS logs (id SERIAL PRIMARY KEY, timestamp TEXT, action TEXT, user_id BIGINT, details TEXT)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS payments (id SERIAL PRIMARY KEY, user_id BIGINT, amount INTEGER, tariff TEXT, date TEXT)''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS admins (user_id BIGINT PRIMARY KEY)''')
        try: await conn.execute('ALTER TABLE users ADD COLUMN devices_limit INTEGER DEFAULT 3')
        except asyncpg.exceptions.DuplicateColumnError: pass

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
            await conn.execute('INSERT INTO users (user_id, username, referrer_id, reg_date, consented, devices_limit) VALUES ($1, $2, $3, $4, 1, 3)', user_id, username, referrer_id, now)

async def update_trial(user_id):
    async with db_pool.acquire() as conn: await conn.execute('UPDATE users SET trial_used = 1 WHERE user_id = $1', user_id)
    await add_log("TRIAL", user_id, "Взял пробник")

async def add_days(user_id, days, devices=3):
    user = await get_user(user_id)
    if not user: return
    now = datetime.datetime.now()
    if user['sub_until']:
        current_until = datetime.datetime.fromisoformat(user['sub_until'])
        new_until = (current_until if current_until > now else now) + datetime.timedelta(days=days)
    else: new_until = now + datetime.timedelta(days=days)
    async with db_pool.acquire() as conn: await conn.execute('UPDATE users SET sub_until = $1, devices_limit = $2 WHERE user_id = $3', new_until.isoformat(), devices, user_id)
    await add_log("SUB_ADD", user_id, f"Выдано {days} дней, {devices} устройств")

async def toggle_ban(user_id, ban_status):
    async with db_pool.acquire() as conn: await conn.execute('UPDATE users SET is_banned = $1 WHERE user_id = $2', ban_status, user_id)
    await add_log("BAN" if ban_status else "UNBAN", user_id, "Забанен" if ban_status else "Разбанен")

async def add_log(action, user_id, details):
    async with db_pool.acquire() as conn:
        now = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")
        await conn.execute('INSERT INTO logs (timestamp, action, user_id, details) VALUES ($1, $2, $3, $4)', now, action, user_id, details)

async def get_logs():
    async with db_pool.acquire() as conn: return await conn.fetch('SELECT * FROM logs ORDER BY id DESC LIMIT 10')

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
        return total_users, total_subs, total_money, week_money, 0

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
    if await is_admin(user_id): keyboard.append([InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin_panel")])
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

def consent_kb(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Согласен с условиями", callback_data="consent_agree")]])
def devices_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 3 устройства (Базовый)", callback_data="dev_3")],
        [InlineKeyboardButton(text="📱 6 устройств (Семейный)", callback_data="dev_6")],
        [InlineKeyboardButton(text="📱 9 устройств (Максимум)", callback_data="dev_9")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ])

# --- ГЛОБАЛЬНЫЙ ЛОВЕЦ ОШИБОК ---
@dp.error()
async def error_handler(event: ErrorEvent):
    exception = event.exception
    await notify_urgent("ОШИБКА В БОТЕ", f"Тип: {type(exception).__name__}\nТекст: {str(exception)}")
    if event.update.callback_query:
        try: await event.update.callback_query.answer("Техническая ошибка. Мы чиним!", show_alert=True)
        except: pass

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
    if user and user['is_banned']: return await msg.answer("⛔️ Вы заблокированы в этом боте.")

    if not user:
        consent_text = "👋 Добро пожаловать в WeskVPN!\n\nДля работы нам нужны ваши Telegram ID и Никнейм.\n🚫 Мы НЕ собираем номера телефонов или данные карт.\n\nНажимая кнопку, вы соглашаетесь с Политикой конфиденциальности."
        return await msg.answer(consent_text, parse_mode="HTML", reply_markup=consent_kb())

    ref_id = int(command.args) if command.args and command.args.isdigit() else None
    if ref_id and ref_id != msg.from_user.id:
        ref_user = await get_user(ref_id)
        if ref_user and not ref_user['is_banned']:
            await add_days(ref_id, 3)
            try: await bot.send_message(ref_id, "🎉 По вашей ссылке зарегистрировался друг! Вам начислено <b>+3 дня</b>.", parse_mode="HTML")
            except: pass
    
    await msg.answer(f"Привет, {msg.from_user.first_name}! 👋\nДобро пожаловать в WeskVPN.", reply_markup=await main_kb(msg.from_user.id))

@dp.callback_query(F.data == "consent_agree")
async def consent_agree(cb: CallbackQuery):
    await add_user(cb.from_user.id, cb.from_user.username)
    await cb.message.edit_text(f"Отлично! Добро пожаловать в WeskVPN, {cb.from_user.first_name}. 👋", reply_markup=await main_kb(cb.from_user.id))
    await cb.answer()

# --- ВЫБОР УСТРОЙСТВ И ТАРИФЫ ---
@dp.callback_query(F.data == "subscribe")
async def subscribe(cb: CallbackQuery):
    await cb.message.edit_text("📱 <b>Сколько устройств вы хотите подключить?</b>", parse_mode="HTML", reply_markup=devices_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("dev_"))
async def show_tariffs(cb: CallbackQuery):
    dev_count = cb.data.split("_")[1]
    rows = [[InlineKeyboardButton(text=f"{name} — {price} ₽", callback_data=f"pay:{key}")] for key, (name, price, dev) in TARIFFS.items() if key.startswith(f"{dev_count}_")]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="subscribe")])
    await cb.message.edit_text(f"🔌 <b>Тарифы для {dev_count} устройств:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@dp.callback_query(F.data.startswith("pay:"))
async def pay(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    name, price, devices = TARIFFS[key]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Согласен с офертой", callback_data=f"agree:{key}")],[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"dev_{devices}")]])
    await cb.message.edit_text(f"Вы выбрали: <b>{name}</b>\nСумма: <b>{price} ₽</b>\n\nСогласны с офертой?", parse_mode="HTML", reply_markup=kb)
    await cb.answer()

# --- ОПЛАТА (ИНТЕГРАЦИЯ ROLLYPAY) ---
@dp.callback_query(F.data.startswith("agree:"))
async def agree(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    name, price, devices = TARIFFS[key]
    
    await cb.message.edit_text("⏳ <b>Создаем ссылку на оплату...</b>", parse_mode="HTML")
    
    nonce = str(int(datetime.datetime.now().timestamp() * 1000))
    
    headers = {
        "X-API-Key": ROLLYPAY_API_KEY,
        "X-Nonce": nonce,
        "Content-Type": "application/json"
    }
    
    payload = {
        "terminal_id": ROLLYPAY_TERMINAL_ID,
        "amount": f"{price}.00",
        "payment_currency": "RUB",
        "order_id": f"user_{cb.from_user.id}_tariff_{key}",
        "description": f"VPN {name}",
        "payment_method": "sbp", 
        "customer_id": str(cb.from_user.id),
        "redirect_url": "https://t.me/weskvpn_bot"
    }
    
    try:
        async with ClientSession() as session:
            async with session.post("https://api.rollypay.io/api/v1/payments", headers=headers, json=payload) as response:
                if response.status in [200, 201]:
                    data = await response.json()
                    pay_url = data.get("pay_url")
                    if pay_url:
                        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Перейти к оплате", url=pay_url)],[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back")]])
                        await cb.message.edit_text(f"Тариф: <b>{name}</b>\nСумма: <b>{price} ₽</b>\n\nНажмите кнопку ниже для оплаты.", parse_mode="HTML", reply_markup=kb)
                    else:
                        await cb.message.edit_text("❌ Ошибка: RollyPay не вернул ссылку.")
                else:
                    error_text = await response.text()
                    await notify_urgent("Ошибка RollyPay API", f"Код: {response.status}\n{error_text}")
                    await cb.message.edit_text(f"❌ Ошибка платежной системы. Попробуйте позже.")
    except Exception as e:
        await notify_urgent("Критическая ошибка RollyPay", str(e))
        await cb.message.edit_text("❌ Ошибка соединения с платежной системой.")
    
    await cb.answer()

# --- ВЕБХУК ROLLYPAY (ИСПРАВЛЕНА ПРОВЕРКА ПОДПИСИ) ---
async def rollypay_webhook(request: web.Request):
    try:
        # 1. Получаем сырое тело запроса и заголовки
        raw_body = await request.read()
        body_text = raw_body.decode('utf-8')
        
        signature = request.headers.get("X-Signature")
        timestamp = request.headers.get("X-Timestamp")
        
        # 2. Проверка подписи (timestamp + "." + body)
        if ROLLYPAY_SIGNING_SECRET and signature and timestamp:
            payload_to_sign = f"{timestamp}.{body_text}"
            expected_sig = hmac.new(ROLLYPAY_SIGNING_SECRET.encode(), payload_to_sign.encode(), hashlib.sha256).hexdigest()
            
            if not hmac.compare_digest(expected_sig, signature):
                await notify_urgent("Неверная подпись вебхука", f"Timestamp: {timestamp}")
                return web.Response(status=403, text="Invalid signature")
        
        # 3. Парсим JSON и обрабатываем событие
        data = json.loads(body_text)
        event_type = data.get("event_type")
        status = data.get("status")
        order_id = data.get("order_id", "")
        
        # Если оплата успешна
        if status in ["succeeded", "success"] or event_type in ["payment.succeeded", "payment.paid"]:
            parts = order_id.split("_")
            if len(parts) >= 4:
                user_id = int(parts[1])
                tariff_key = parts[3]
                
                if tariff_key in TARIFFS:
                    name, price, devices = TARIFFS[tariff_key]
                    days_map = {"1m": 30, "3m": 90, "6m": 180, "1y": 365}
                    days = days_map.get(tariff_key.split("_")[1], 30)
                    
                    await add_days(user_id, days, devices)
                    await add_payment(user_id, price, name)
                    
                    user = await get_user(user_id)
                    nick = f"@{user['username']}" if user and user['username'] else "без ника"
                    await notify_payment_success(nick, user_id, name, price, devices)
                    
                    try:
                        await bot.send_message(user_id, f"✅ <b>Оплата прошла успешно!</b>\nВаш тариф: {name}\n\n🔗 Ссылка для подключения:\n<code>https://vpn.example.com/sub_{user_id}</code>", parse_mode="HTML")
                    except: pass
        
        # Отвечаем RollyPay 200 OK, чтобы он перестал повторять запрос
        return web.Response(text="OK")
        
    except Exception as e:
        await notify_urgent("Ошибка вебхука", str(e))
        return web.Response(status=500, text="Error")

# --- АДМИН ПАНЕЛЬ ---
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await is_admin(cb.from_user.id): return await cb.answer("Доступ запрещен", show_alert=True)
    await cb.message.edit_text("👑 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    tu, ts, tm, wm, _ = await get_stats()
    text = (f"📊 <b>Статистика</b>\n\n👥 Юзеров: <b>{tu}</b>\n💎 Подписок: <b>{ts}</b>\n\n💰 <b>Финансы:</b>\nЗа всё время: <b>{tm} ₽</b>\nЗа 7 дней: <b>{wm} ₽</b>")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧪 Симулировать покупку", callback_data="admin_test_pay")],
        [InlineKeyboardButton(text="💣 Вызвать ошибку", callback_data="admin_test_error")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
    ])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await cb.answer()

@dp.callback_query(F.data == "admin_test_pay")
async def admin_test_pay(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    await add_payment(cb.from_user.id, 800, "3 мес · 6 устр.")
    await add_days(cb.from_user.id, 90, 6)
    await notify_payment_success("Админ (тест)", cb.from_user.id, "3 мес · 6 устр.", 800, 6)
    await cb.answer("✅ Тестовая покупка записана!", show_alert=True)
    await admin_stats(cb)

@dp.callback_query(F.data == "admin_test_error")
async def admin_test_error(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    await cb.answer("Вызываю ошибку...", show_alert=True)
    result = 1 / 0

@dp.callback_query(F.data.startswith("admin_users:"))
async def admin_users(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    page = int(cb.data.split(":")[1])
    async with db_pool.acquire() as conn: users = await conn.fetch('SELECT * FROM users LIMIT $1 OFFSET $2', 5, page * 5)
    text = f"👥 <b>Список юзеров (Стр. {page+1})</b>\n\n"
    kb_buttons = []
    for u in users:
        text += f"{'⛔' if u['is_banned'] else '✅'} <code>{u['user_id']}</code> | @{u['username'] or '—'}\n"
        kb_buttons.append([InlineKeyboardButton(text=f"Управлять {u['user_id']}", callback_data=f"adm_view:{u['user_id']}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_users:{page-1}"))
    if len(users) == 5: nav.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"admin_users:{page+1}"))
    if nav: kb_buttons.append(nav)
    kb_buttons.append([InlineKeyboardButton(text="⬅️ В админку", callback_data="admin_panel")])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))
    await cb.answer()

@dp.callback_query(F.data == "admin_add")
async def admin_add(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return await cb.answer("Только главный админ!", show_alert=True)
    await state.set_state(AdminStates.waiting_admin_id)
    await cb.message.edit_text("🛡 Отправь ID нового админа.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_panel")]]))
    await cb.answer()

@dp.message(AdminStates.waiting_admin_id)
async def process_admin_id(msg: Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID or not msg.text.isdigit(): return
    async with db_pool.acquire() as conn: await conn.execute('INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT DO NOTHING', int(msg.text))
    await state.clear()
    await msg.answer(f"✅ <code>{msg.text}</code> теперь админ!", parse_mode="HTML", reply_markup=admin_kb())

@dp.callback_query(F.data == "admin_find_user")
async def admin_find_user(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id): return
    await state.set_state(AdminStates.waiting_user_id)
    await cb.message.edit_text("👤 Отправь мне <b>ID пользователя</b>.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_panel")]]))
    await cb.answer()

@dp.message(AdminStates.waiting_user_id)
async def process_user_id(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id) or not msg.text.isdigit(): return
    await state.clear()
    await show_user_card(msg, int(msg.text))

@dp.callback_query(F.data.startswith("adm_view:"))
async def adm_view(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    await show_user_card(cb, int(cb.data.split(":")[1]))

async def show_user_card(message_obj, target_id):
    user = await get_user(target_id)
    if not user: return await message_obj.answer("❌ Не найден.")
    sub = f"До {datetime.datetime.fromisoformat(user['sub_until']).strftime('%d.%m.%Y')}" if user['sub_until'] else "Нет"
    text = (f"👤 <b>Юзер</b>\nID: <code>{user['user_id']}</code>\nНик: @{user['username'] or '—'}\nСтатус: {'⛔️' if user['is_banned'] else '✅'}\nПодписка: {sub}\nУстройств: {user['devices_limit']}\nПробник: {'Да' if user['trial_used'] else 'Нет'}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎁 +30 дней", callback_data=f"adm_give:{target_id}")],[InlineKeyboardButton(text="⛔️ Забанить" if not user['is_banned'] else "✅ Разбанить", callback_data=f"adm_ban:{target_id}")],[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]])
    if isinstance(message_obj, CallbackQuery): await message_obj.message.edit_text(text, parse_mode="HTML", reply_markup=kb); await message_obj.answer()
    else: await message_obj.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("adm_give:"))
async def adm_give(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    await add_days(int(cb.data.split(":")[1]), 30)
    await cb.answer("✅ Выдано 30 дней!", show_alert=True)

@dp.callback_query(F.data.startswith("adm_ban:"))
async def adm_ban(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    target_id = int(cb.data.split(":")[1])
    user = await get_user(target_id)
    await toggle_ban(target_id, 0 if user['is_banned'] else 1)
    await cb.answer("✅ Статус изменен!", show_alert=True)
    await show_user_card(cb, target_id)

# --- ПОЛЬЗОВАТЕЛЬСКИЕ КНОПКИ ---
@dp.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery):
    user = await get_user(cb.from_user.id)
    sub = f"✅ До {datetime.datetime.fromisoformat(user['sub_until']).strftime('%d.%m.%Y')}" if user['sub_until'] else "❌ Не активна"
    await cb.message.edit_text(f"👤 <b>Профиль</b>\n\n🆔 ID: <code>{cb.from_user.id}</code>\n💎 Подписка: {sub}\n📱 Устройств: {user['devices_limit']}", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))
    await cb.answer()

@dp.callback_query(F.data == "trial")
async def trial(cb: CallbackQuery):
    user = await get_user(cb.from_user.id)
    if user['trial_used'] == 1: return await cb.answer("❌ Вы уже использовали пробник!", show_alert=True)
    await update_trial(cb.from_user.id)
    await add_days(cb.from_user.id, 3, devices=3)
    await cb.message.edit_text("🎁 <b>Пробный период активирован!</b>\n\n🔗 Ссылка: <code>https://vpn.example.com/trial</code>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))
    await cb.answer()

@dp.callback_query(F.data == "howto")
async def howto(cb: CallbackQuery):
    text = "📖 <b>Как подключиться?</b>\n\n1️⃣ Скачайте <b>Happ</b>.\n2️⃣ Скопируйте ссылку-ключ.\n3️⃣ Вставьте в Happ и нажмите «Подключиться»."
    keyboard = [[InlineKeyboardButton(text="🍏 iOS", url="https://apps.apple.com/us/app/happ-proxy-utility/id6504287215")],[InlineKeyboardButton(text="🤖 Android", url="https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk")],[InlineKeyboardButton(text="💻 Windows", url="https://github.com/Happ-proxy/happ-desktop/releases")],[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]
    await cb.message.edit_text(text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await cb.answer()

@dp.callback_query(F.data == "ref")
async def ref(cb: CallbackQuery):
    bot_info = await bot.get_me()
    await cb.message.edit_text(f"🤝 Пригласи друга:\n<code>https://t.me/{bot_info.username}?start={cb.from_user.id}</code>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))
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

@dp.callback_query(F.data == "admin_logs")
async def admin_logs(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    logs = await get_logs()
    text = "📝 <b>Последние 10 событий:</b>\n\n" + "".join([f"[{log['timestamp']}] {log['action']} (ID: {log['user_id']})\n{log['details']}\n\n" for log in logs])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]]))
    await cb.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(cb: CallbackQuery, state: FSMContext):
    if not await is_admin(cb.from_user.id): return
    await state.set_state(AdminStates.waiting_broadcast)
    await cb.message.edit_text("📢 Отправь текст для рассылки.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin_panel")]]))
    await cb.answer()

@dp.message(AdminStates.waiting_broadcast)
async def process_broadcast(msg: Message, state: FSMContext):
    if not await is_admin(msg.from_user.id): return
    await state.clear()
    await msg.answer("⏳ Начинаю рассылку...")
    async with db_pool.acquire() as conn: users = await conn.fetch('SELECT user_id FROM users WHERE is_banned = 0')
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
    if WEBHOOK_URL: await bot.set_webhook(WEBHOOK_URL)
    print("Бот успешно подключен к внешней базе!")
    await notify_bot.send_message(ADMIN_ID, "✅ <b>Бот успешно перезапущен!</b>", parse_mode="HTML")

async def main():
    app = web.Application()
    webhook_requests_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    
    app.router.add_post("/rollypay_webhook", rollypay_webhook)
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
