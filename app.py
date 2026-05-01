import asyncio
import sqlite3
import os
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiosend import CryptoPay
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

# ========== КОНФИГ ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CRYPTOPAY_TOKEN = os.environ.get("CRYPTOPAY_TOKEN")
ADMIN_IDS = [5626697140]

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен!")
if not CRYPTOPAY_TOKEN:
    raise ValueError("CRYPTOPAY_TOKEN не установлен!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
cp = CryptoPay(token=CRYPTOPAY_TOKEN)

# ========== БАЗА ДАННЫХ ==========
DB_PATH = "/tmp/svat_bot.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            country TEXT,
            svat_balance INTEGER DEFAULT 0,
            total_spent_usd INTEGER DEFAULT 0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            invoice_id INTEGER PRIMARY KEY,
            user_id INTEGER,
            amount_usdt REAL,
            svat_amount INTEGER,
            status TEXT,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ========== FSM ДЛЯ ПОДДЕРЖКИ ==========
class SupportState(StatesGroup):
    waiting_message = State()

# ========== КЛАВИАТУРЫ ==========
def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎓 Сватнуть свою школу", callback_data="my_school")],
        [InlineKeyboardButton(text="🎯 Сватнуть жертву", callback_data="victim")],
        [InlineKeyboardButton(text="💰 Мой баланс (сваты)", callback_data="balance")],
        [InlineKeyboardButton(text="🛒 Купить сватов", callback_data="buy")],
        [InlineKeyboardButton(text="📞 Поддержка", callback_data="support")]
    ])

def country_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇷🇺 Россия", callback_data="country_rf")],
        [InlineKeyboardButton(text="🇧🇾 Беларусь", callback_data="country_by")],
        [InlineKeyboardButton(text="🇺🇦 Украина", callback_data="country_ua")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
    ])

def buy_svat_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 сват = 7 USDT (≈7$)", callback_data="pay_1_svat")],
        [InlineKeyboardButton(text="5 сватов = 30 USDT (≈30$)", callback_data="pay_5_svat")],
        [InlineKeyboardButton(text="10 сватов = 55 USDT (≈55$)", callback_data="pay_10_svat")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_menu")]
    ])

# ========== ФУНКЦИИ БАЛАНСА ==========
def add_svats(user_id, amount):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET svat_balance = svat_balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

def get_user_balance(user_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT svat_balance FROM users WHERE user_id = ?", (user_id,))
    res = cur.fetchone()
    conn.close()
    return res[0] if res else 0

def spend_svat(user_id, amount=1):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET svat_balance = svat_balance - ? WHERE user_id = ? AND svat_balance >= ?", 
                (amount, user_id, amount))
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0

# ========== ОПЛАТА ==========
async def create_crypto_invoice(user_id, svat_count, usdt_amount):
    try:
        invoice = await cp.create_invoice(
            amount=usdt_amount,
            asset="USDT",
            description=f"Покупка {svat_count} сватов",
        )
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO payments (invoice_id, user_id, amount_usdt, svat_amount, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (invoice.invoice_id, user_id, usdt_amount, svat_count, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return invoice.bot_invoice_url
    except Exception as e:
        print(f"Ошибка создания счета: {e}")
        return None

async def check_payment_status(invoice_id):
    try:
        invoices = await cp.get_invoices()
        for invoice in invoices:
            if invoice.invoice_id == invoice_id:
                return invoice.status
        return "not_found"
    except Exception as e:
        print(f"Ошибка проверки статуса: {e}")
        return "error"

async def check_payments_periodically():
    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT invoice_id, user_id, svat_amount FROM payments WHERE status = 'pending'")
            pending = cur.fetchall()
            for invoice_id, user_id, svat_count in pending:
                status = await check_payment_status(invoice_id)
                if status == "paid":
                    cur.execute("UPDATE payments SET status = 'paid' WHERE invoice_id = ?", (invoice_id,))
                    add_svats(user_id, svat_count)
                    await bot.send_message(user_id, f"✅ Оплата получена! Начислено {svat_count} сватов.\n💰 Твой баланс: {get_user_balance(user_id)} сватов")
                elif status == "expired":
                    cur.execute("UPDATE payments SET status = 'expired' WHERE invoice_id = ?", (invoice_id,))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Ошибка в check_payments_periodically: {e}")
        await asyncio.sleep(15)

# ========== ХЭНДЛЕРЫ ==========
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or "no_username"
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
    conn.commit()
    conn.close()
    await message.answer(
        "🔥 Добро пожаловать в Svat Bot!\n\n💰 1 сват = 7 USDT (≈7$)\n💸 Оплата через CryptoBot (@send)\n\nВыбери действие:",
        reply_markup=main_menu()
    )

@dp.callback_query(F.data == "my_school")
async def my_school_menu(callback: CallbackQuery):
    await callback.message.edit_text("🌍 Выбери свою страну:", reply_markup=country_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "victim")
async def victim_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        f"🎯 Введи username или ссылку на жертву\n\nПример: @username или t.me/username\n\n💎 Твой баланс: {get_user_balance(callback.from_user.id)} сватов\n⚠️ За 1 сват ты можешь сватнуть 1 жертву",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_menu")]])
    )
    await callback.answer()

@dp.message(F.text & ~F.text.startswith("/"))
async def victim_handler(message: types.Message):
    user_id = message.from_user.id
    target = message.text.strip()
    if get_user_balance(user_id) < 1:
        await message.answer("❌ Недостаточно сватов! Купи их в меню.", reply_markup=main_menu())
        return
    if spend_svat(user_id, 1):
        await message.answer(f"✅ Ты успешно сватнул(а) {target}!\n\nОсталось сватов: {get_user_balance(user_id)}", reply_markup=main_menu())
    else:
        await message.answer("❌ Ошибка! Попробуй позже.", reply_markup=main_menu())

@dp.callback_query(F.data.startswith("country_"))
async def choose_country(callback: CallbackQuery):
    country_code = callback.data.split("_")[1]
    country_names = {"rf": "Россия", "by": "Беларусь", "ua": "Украина"}
    country_name = country_names.get(country_code, "неизвестная страна")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE users SET country = ? WHERE user_id = ?", (country_name, callback.from_user.id))
    conn.commit()
    conn.close()
    if get_user_balance(callback.from_user.id) < 1:
        await callback.message.edit_text(f"❌ Недостаточно сватов для свата своей школы ({country_name})!\nКупи сваты в меню.", reply_markup=main_menu())
    else:
        spend_svat(callback.from_user.id, 1)
        await callback.message.edit_text(f"✅ Ты успешно сватнул свою школу ({country_name})!\n\nОсталось сватов: {get_user_balance(callback.from_user.id)}", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "balance")
async def show_balance(callback: CallbackQuery):
    bal = get_user_balance(callback.from_user.id)
    await callback.message.edit_text(f"💎 Твой баланс: {bal} сватов\n💰 1 сват = 7 USDT (≈7$)\n📦 Купить можно по кнопке ниже", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "buy")
async def buy_menu(callback: CallbackQuery):
    await callback.message.edit_text("Выбери количество сватов:", reply_markup=buy_svat_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "pay_1_svat")
async def pay_1_svat(callback: CallbackQuery):
    link = await create_crypto_invoice(callback.from_user.id, 1, 7)
    if link:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💸 Перейти к оплате (USDT)", url=link)],
            [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_menu")]
        ])
        await callback.message.edit_text("💎 1 сват = 7 USDT (≈7$)\n\nНажми на кнопку ниже для оплаты:\n\n✅ После оплаты сваты зачислятся автоматически", reply_markup=keyboard)
    else:
        await callback.message.edit_text("❌ Ошибка создания счета.", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "pay_5_svat")
async def pay_5_svat(callback: CallbackQuery):
    link = await create_crypto_invoice(callback.from_user.id, 5, 30)
    if link:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💸 Перейти к оплате (USDT)", url=link)],
            [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_menu")]
        ])
        await callback.message.edit_text("💎 5 сватов = 30 USDT (≈30$) (экономия 5$)\n\nНажми на кнопку ниже для оплаты:\n\n✅ После оплаты сваты зачислятся автоматически", reply_markup=keyboard)
    else:
        await callback.message.edit_text("❌ Ошибка создания счета.", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "pay_10_svat")
async def pay_10_svat(callback: CallbackQuery):
    link = await create_crypto_invoice(callback.from_user.id, 10, 55)
    if link:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💸 Перейти к оплате (USDT)", url=link)],
            [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_menu")]
        ])
        await callback.message.edit_text("💎 10 сватов = 55 USDT (≈55$) (экономия 15$)\n\nНажми на кнопку ниже для оплаты:\n\n✅ После оплаты сваты зачислятся автоматически", reply_markup=keyboard)
    else:
        await callback.message.edit_text("❌ Ошибка создания счета.", reply_markup=main_menu())
    await callback.answer()

@dp.callback_query(F.data == "support")
async def support_menu(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📞 Напиши свой вопрос. Оператор ответит в ближайшее время.\nДля отмены напиши /cancel", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="back_to_menu")]]))
    await state.set_state(SupportState.waiting_message)
    await callback.answer()

@dp.message(SupportState.waiting_message)
async def support_receive(message: types.Message, state: FSMContext):
    for admin_id in ADMIN_IDS:
        await bot.send_message(admin_id, f"🆘 Новое обращение от @{message.from_user.username} (ID:{message.from_user.id}):\n\n{message.text}")
    await message.answer("✅ Сообщение отправлено оператору. Ответ придёт сюда.")
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu())

@dp.callback_query(F.data == "back_to_menu")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu())
    await callback.answer()

@dp.message(Command("cancel"))
async def cancel_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=main_menu())

# ========== FASTAPI ДЛЯ WEBHOOK ==========
app = FastAPI()
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    update_data = await request.json()
    update = types.Update(**update_data)
    await dp.feed_update(bot, update)
    return JSONResponse({"status": "ok"})

@app.get("/health")
async def health_check():
    return JSONResponse({"status": "alive", "bot": "running"})

@app.get("/")
async def root():
    return JSONResponse({"message": "Svat Bot is running!"})

@app.on_event("startup")
async def on_startup():
    print("🤖 Svat Bot запущен!")
    if RENDER_EXTERNAL_URL:
        webhook_url = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
        await bot.set_webhook(webhook_url)
        print(f"✅ Webhook установлен на: {webhook_url}")
    asyncio.create_task(check_payments_periodically())

@app.on_event("shutdown")
async def on_shutdown():
    await bot.delete_webhook()
    await bot.session.close()

# ========== ЗАПУСК ==========
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
