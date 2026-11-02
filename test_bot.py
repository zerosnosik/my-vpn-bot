import asyncio
import os
import datetime
import asyncpg
import hmac
import hashlib
import json
import time
import uuid
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
XUI_URL = os.getenv("XUI_URL")
XUI_USERNAME = os.getenv("XUI_USERNAME")
XUI_PASSWORD = os.getenv("XUI_PASSWORD")
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else None
PORT = int(os.getenv("PORT", 10000))

bot = Bot(token=BOT_TOKEN)
notify_bot = Bot(token=NOTIFY_BOT_TOKEN) if NOTIFY_BOT_TOKEN else bot
dp = Dispatcher()
db_pool = None 

# --- МАТРИЦА ТАРИФОВ (Оставил тестовый за 10₽) ---
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
        f"💎 Купил подписку: <b>{tariff}</b>\n"
        f"📱 Устройств: <b>{devices}</b>\n"
        f"💰 Сумма: <b>{price} ₽</b>\n\n"
        f"📈 <i>WeskVPN растет!</i>"
    )
    try: await notify_bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except: pass

async def notify_urgent(title: str, details: str):
    text = f"🚨 <b>ОШИБКА: {title.upper()}</b> 🚨\n\n<code>{details[:300]}</code>"
    try: await notify_bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except: pass

# --- ИНТЕГРАЦИЯ С 3X-UI ---
async def create_vpn_client(user_id, devices):
    """Авторизуется в 3x-ui и создает клиента, возвращает ссылку"""
    if not XUI_URL or not XUI_USERNAME or not XUI_PASSWORD:
        return None

    base_url = XUI_URL.rstrip('/')
    
    # 1. Авторизация
    login_url = f"{base_url}/login"
    login_data = {"username": XUI_USERNAME, "password": XUI_PASSWORD}
    
    async with ClientSession() as session:
        try:
            async with session.post(login_url, json=login_data) as resp:
                if resp.status != 200:
                    await notify_urgent("3x-ui Login Error", f"Status: {resp.status}")
                    return None
                # Куки сохраняются в сессии автоматически
        except Exception as e:
            await notify_urgent("3x-ui Connection Error", str(e))
            return None

        # 2. Получаем список инбаундов, чтобы найти ID первого (VLESS TCP)
        try:
            async with session.get(f"{base_url}/panel/api/inbounds/list") as resp:
                data = await resp.json()
                if not data.get("success"):
                    return None
                
                inbounds = data.get("obj", [])
                if not inbounds:
                    await notify_urgent("3x-ui Error", "Нет созданных Inbounds!")
                    return None
                
                # Берем первый инбаунд (обычно это TCP Reality)
                target_inbound = inbounds[0]
                inbound_id = target_inbound["id"]
                protocol = target_inbound["protocol"]
                port = target_inbound["port"]
                remark = target_inbound["remark"] # Название (WeskVPN | Германия)
        except Exception as e:
            await notify_urgent("3x-ui Get Inbounds Error", str(e))
            return None

        # 3. Создаем клиента
        client_uuid = str(uuid.uuid4())
        email = f"user_{user_id}"
        expiry_time = int((time.time() + 30 * 24 * 60 * 60) * 1000) # +30 дней
        total_gb = 0 # 0 = безлимит
        limit_ip = devices # Количество устройств

        client_data = {
            "id": inbound_id,
            "settings": json.dumps({
                "clients": [{
                    "id": client_uuid,
                    "flow": "xtls-rprx-vision",
                    "email": email,
                    "limitIp": limit_ip,
                    "totalGB": total_gb,
                    "expiryTime": expiry_time,
                    "enable": True,
                    "tgId": str(user_id),
                    "subId": client_uuid[:16],
                    "reset": 0
                }]
            })
        }

        try:
            async with session.post(f"{base_url}/panel/api/inbounds/addClient", json=client_data) as resp:
                if resp.status == 200:
                    # Формируем ссылку для подключения
                    # Берем данные для Reality из настроек инбаунда
                    stream_settings = json.loads(target_inbound["streamSettings"])
                    reality_settings = stream_settings.get("realitySettings", {})
                    server_names = reality_settings.get("serverNames", [])
                    public_key = reality_settings.get("settings", {}).get("publicKey", "")
                    short_id = reality_settings.get("shortIds", [""])[0]
                    fingerprint = reality_settings.get("settings", {}).get("fingerprint", "chrome")
                    
                    # Получаем домен/IP сервера из XUI_URL
                    server_host = base_url.replace("https://", "").replace("http://", "").split(":")[0]

                    vless_link = (
                        f"vless://{client_uuid}@{server_host}:{port}"
                        f"?type=tcp&security=reality&pbk={public_key}&fp={fingerprint}"
                        f"&sni={server_names[0] if server_names else ''}&sid={short_id}&spx=%2F&flow=xtls-rprx-vision"
                        f"#{remark}-{email}"
                    )
                    return vless_link
                else:
                    error = await resp.text()
                    await notify_urgent("3x-ui Add Client Error", error)
                    return None
        except Exception as e:
            await notify_urgent("3x-ui Add Client Exception", str(e))
            return None

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
        return total_users, total_subs, total_money

# --- СОСТОЯНИЯ (FSM) ---
class AdminStates(StatesGroup):
    waiting_broadcast = State()
    waiting_user_id = State()

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
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="👥 Список юзеров", callback_data="admin_users:0")],
        [InlineKeyboardButton(text="👤 Найти юзера", callback_data="admin_find_user")],
        [InlineKeyboardButton(text="📝 Логи", callback_data="admin_logs"), InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="back")]
    ])

def consent_kb(): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Согласен", callback_data="consent_agree")]])
def devices_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 3 устройства", callback_data="dev_3")],
        [InlineKeyboardButton(text="📱 6 устройств", callback_data="dev_6")],
        [InlineKeyboardButton(text="📱 9 устройств", callback_data="dev_9")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ])

# --- ГЛОБАЛЬНЫЙ ЛОВЕЦ ОШИБОК ---
@dp.error()
async def error_handler(event: ErrorEvent):
    exception = event.exception
    await notify_urgent("ОШИБКА В БОТЕ", f"{type(exception).__name__}: {str(exception)}")
    if event.update.callback_query:
        try: await event.update.callback_query.answer("Техническая ошибка. Мы чиним!", show_alert=True)
        except: pass

# --- ГЛАВНЫЙ ХЕНДЛЕР ---
@dp.message(Command("start"))
async def start(msg: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    user = await get_user(msg.from_user.id)
    if user and user['is_banned']: return await msg.answer("⛔️ Вы заблокированы.")

    if not user:
        consent_text = "👋 Добро пожаловать в WeskVPN!\n\nДля работы нам нужны ваши Telegram ID и Никнейм.\n🚫 Мы НЕ собираем номера телефонов.\n\nНажимая кнопку, вы соглашаетесь с Политикой конфиденциальности."
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
    
    await cb.message.edit_text("⏳ Создаем ключ...")
    
    # Создаем реальный ключ на 3 дня и 3 устройства
    vless_link = await create_vpn_client(cb.from_user.id, 3)
    if not vless_link:
        return await cb.message.edit_text("❌ Ошибка сервера. Мы уже чиним!")
        
    await update_trial(cb.from_user.id)
    await add_days(cb.from_user.id, 3, devices=3)
    await cb.message.edit_text(f"🎁 <b>Пробный период активирован!</b>\n\n🔗 Ваша ссылка:\n<code>{vless_link}</code>\n\nСкопируйте её и вставьте в Happ.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))
    await cb.answer()

@dp.callback_query(F.data == "howto")
async def howto(cb: CallbackQuery):
    text = "📖 <b>Как подключиться?</b>\n\n1️⃣ Скачайте <b>Happ</b>.\n2️⃣ Скопируйте ссылку-ключ.\n3️⃣ Вставьте в Happ."
    keyboard = [[InlineKeyboardButton(text="🍏 iOS", url="https://apps.apple.com/us/app/happ-proxy-utility/id6504287215")],[InlineKeyboardButton(text="🤖 Android", url="https://github.com/Happ-proxy/happ-android/releases/latest/download/Happ.apk")],[InlineKeyboardButton(text="💻 Windows", url="https://github.com/Happ-proxy/happ-desktop/releases")],[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]
    await cb.message.edit_text(text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
    await cb.answer()

@dp.callback_query(F.data == "ref")
async def ref(cb: CallbackQuery):
    bot_info = await bot.get_me()
    await cb.message.edit_text(f"🤝 Пригласи друга:\n<code>https://t.me/{bot_info.username}?start={cb.from_user.id}</code>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]))
    await cb.answer()

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

# --- ОПЛАТА ---
@dp.callback_query(F.data.startswith("agree:"))
async def agree(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    name, price, devices = TARIFFS[key]
    await cb.message.edit_text("⏳ <b>Создаем ссылку на оплату...</b>", parse_mode="HTML")
    nonce = str(int(datetime.datetime.now().timestamp() * 1000))
    headers = {"X-API-Key": ROLLYPAY_API_KEY, "X-Nonce": nonce, "Content-Type": "application/json"}
    payload = {
        "terminal_id": ROLLYPAY_TERMINAL_ID, "amount": f"{price}.00", "payment_currency": "RUB",
        "order_id": f"user:{cb.from_user.id}:tariff:{key}", "description": f"VPN {name}",
        "payment_method": "sbp", "customer_id": str(cb.from_user.id), "redirect_url": "https://t.me/weskvpn_bot"
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
                    else: await cb.message.edit_text("❌ Ошибка: RollyPay не вернул ссылку.")
                else:
                    error_text = await response.text()
                    await notify_urgent("Ошибка RollyPay API", f"Код: {response.status}\n{error_text}")
                    await cb.message.edit_text(f"❌ Ошибка платежной системы.")
    except Exception as e:
        await notify_urgent("Критическая ошибка RollyPay", str(e))
        await cb.message.edit_text("❌ Ошибка соединения.")
    await cb.answer()

# --- ВЕБХУК ROLLYPAY ---
async def rollypay_webhook(request: web.Request):
    try:
        raw_body = await request.read()
        data = json.loads(raw_body.decode('utf-8'))
        event_type = data.get("event_type")
        status = data.get("status")
        order_id = data.get("order_id", "")
        
        if status in ["succeeded", "success", "paid"] or event_type in ["payment.succeeded", "payment.paid"]:
            parts = order_id.split(":")
            if len(parts) >= 4:
                user_id = int(parts[1])
                tariff_key = parts[3]
                
                if tariff_key in TARIFFS:
                    name, price, devices = TARIFFS[tariff_key]
                    days_map = {"1m": 30, "3m": 90, "6m": 180, "1y": 365}
                    days = days_map.get(tariff_key.split("_")[1], 30)
                    
                    # Создаем реального клиента в 3x-ui!
                    vless_link = await create_vpn_client(user_id, devices)
                    
                    if vless_link:
                        await add_days(user_id, days, devices)
                        await add_payment(user_id, price, name)
                        user = await get_user(user_id)
                        nick = f"@{user['username']}" if user and user['username'] else "без ника"
                        await notify_payment_success(nick, user_id, name, price, devices)
                        try:
                            await bot.send_message(user_id, f"✅ <b>Оплата прошла успешно!</b>\nВаш тариф: {name}\n\n🔗 Ссылка для подключения:\n<code>{vless_link}</code>", parse_mode="HTML")
                        except: pass
                    else:
                        await notify_urgent("Ошибка выдачи VPN", f"Юзер {user_id} оплатил, но ключ не создался.")
        
        return web.Response(text="OK")
    except Exception as e:
        await notify_urgent("Ошибка вебхука", str(e))
        return web.Response(status=500, text="Error")

# --- АДМИНКА ---
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await is_admin(cb.from_user.id): return await cb.answer("Доступ запрещен", show_alert=True)
    await cb.message.edit_text("👑 <b>Админ-панель</b>", parse_mode="HTML", reply_markup=admin_kb())
    await cb.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    tu, ts, tm = await get_stats()
    text = (f"📊 <b>Статистика</b>\n\n👥 Юзеров: <b>{tu}</b>\n💎 Подписок: <b>{ts}</b>\n💰 Выручка: <b>{tm} ₽</b>")
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]]))
    await cb.answer()

@dp.callback_query(F.data == "admin_logs")
async def admin_logs(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    logs = await get_logs()
    text = "📝 <b>Последние 10 событий:</b>\n\n" + "".join([f"[{log['timestamp']}] {log['action']} (ID: {log['user_id']})\n{log['details']}\n\n" for log in logs])
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]]))
    await cb.answer()

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
    text = (f"👤 <b>Юзер</b>\nID: <code>{user['user_id']}</code>\nНик: @{user['username'] or '—'}\nПодписка: {sub}\nУстройств: {user['devices_limit']}")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎁 +30 дней", callback_data=f"adm_give:{target_id}")],[InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]])
    if isinstance(message_obj, CallbackQuery): await message_obj.message.edit_text(text, parse_mode="HTML", reply_markup=kb); await message_obj.answer()
    else: await message_obj.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("adm_give:"))
async def adm_give(cb: CallbackQuery):
    if not await is_admin(cb.from_user.id): return
    await add_days(int(cb.data.split(":")[1]), 30)
    await cb.answer("✅ Выдано 30 дней!", show_alert=True)

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
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    await init_db()
    if WEBHOOK_URL: await bot.set_webhook(WEBHOOK_URL)
    await notify_bot.send_message(ADMIN_ID, "✅ <b>Бот перезапущен и готов к работе!</b>", parse_mode="HTML")

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
