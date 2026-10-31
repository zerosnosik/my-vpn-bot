@dp.callback_query(F.data == "subscribe")
async def subscribe(cb: CallbackQuery):
    text = (
        "🔌 <b>Выберите тариф:</b>\n\n"
        "📱 <i>Лимит устройств:</i>\n"
        "Каждая подписка позволяет подключить <b>до 3 устройств</b> одновременно (например: телефон, ноутбук и планшет).\n\n"
        "Одна подписка — ваша техника под защитой."
    )
    
    rows = [[InlineKeyboardButton(
        text=f"{name} — {price} ₽",
        callback_data=f"pay:{key}"
    )] for key, (name, price) in TARIFFS.items()]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])
    
    await cb.message.edit_text(
        text, 
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await cb.answer()
