import asyncio
import aiosqlite
import os
import json
import socket
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import uvicorn

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardRemove

from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Пробуем импортировать requests для определения IP
try:
    import requests
except ImportError:
    requests = None

# --- НАСТРОЙКИ ---
TOKEN = "8236277660:AAE193jYrtDjbUyaKJcDlCnwyrqoZg5qnRE"
DB_PATH = "bot_system.db"
CONFIG_FILE = "managers_config.json"
PARTNER_URL = "https://clck.ru/3RaGGm" 

LEARNING_MATERIALS = """
📖 **Ваши обучающие материалы:**
1. [Инструкция по работе](https://vk.com/video-228271511_456239156?t=6s)
2. [Стандарты сервиса](https://pro.yandex.ru/ru-ru/moskva/knowledge-base/courier/standarty-servisa/standarty)

🚀 Удачного старта!
"""

bot = Bot(token=TOKEN)
dp = Dispatcher()
templates = Jinja2Templates(directory="templates")
scheduler = AsyncIOScheduler()

class CourierForm(StatesGroup):
    age_check = State()
    city = State()
    citizenship = State()
    transport = State()
    phone = State()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def print_server_info(port):
    if requests:
        try:
            current_ip = requests.get('https://api.ipify.org', timeout=5).text
            print(f"\n🚀 СЕРВЕР ЗАПУЩЕН!", flush=True)
            print(f"🌍 ВНЕШНИЙ IP: {current_ip}", flush=True)
            print(f"🔗 АДМИНКА: http://{current_ip}:{port}\n", flush=True)
        except Exception as e:
            print(f"⚠️ Не удалось определить внешний IP: {e}", flush=True)

# --- КЛАВИАТУРЫ ---
def get_mgr_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🟢 Начать смену"), KeyboardButton(text="🔴 Завершить смену")],
        [KeyboardButton(text="🏃 Активная заявка"), KeyboardButton(text="🔗 Моя ссылка")]
    ], resize_keyboard=True)

def get_app_inline_kb(app_id, client_username=None, client_id=None):
    buttons = []
    if client_username:
        buttons.append([InlineKeyboardButton(text="💬 Написать", url=f"https://t.me/{client_username}")])
    elif client_id:
        buttons.append([InlineKeyboardButton(text="💬 Профиль", url=f"tg://user?id={client_id}")])
    buttons.append([
        InlineKeyboardButton(text="✅ ЛИД", callback_data=f"status_lead_{app_id}"),
        InlineKeyboardButton(text="❌ НЕ ЛИД", callback_data=f"status_notlead_{app_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def get_best_manager():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT tg_id, username FROM managers WHERE status = 'На смене'")
        active_managers = await cursor.fetchall()
        if not active_managers: return None, None
        manager_loads = []
        for m_id, m_user in active_managers:
            c = await db.execute("SELECT COUNT(*) FROM applications WHERE manager_id = ? AND status = 'В работе'", (m_id,))
            count = (await c.fetchone())[0]
            manager_loads.append((count, m_id, m_user))
        manager_loads.sort()
        return manager_loads[0][1], manager_loads[0][2]

async def auto_assign_scheduler():
    try:
        if not os.path.exists(CONFIG_FILE): return
        with open(CONFIG_FILE, "r") as f: config = json.load(f)
        if not config.get("auto_distribute", False): return
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT id, client_name, phone, client_username, client_id FROM applications WHERE manager_id IS NULL AND status = 'Новая'")
            new_apps = await cursor.fetchall()
            for app in new_apps:
                app_id, c_name, c_phone, c_username, c_id = app
                m_id, m_user = await get_best_manager()
                if m_id:
                    await db.execute("UPDATE applications SET manager_id = ?, status = 'В работе' WHERE id = ?", (m_id, app_id))
                    await db.commit()
                    try:
                        kb = get_app_inline_kb(app_id, c_username, c_id)
                        await bot.send_message(m_id, f"🔄 **АВТО-ОЧЕРЕДЬ**\n👤 {c_name}\n📞 `{c_phone}`", parse_mode="Markdown", reply_markup=kb)
                    except: pass
    except: pass

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS managers (tg_id INTEGER PRIMARY KEY, name TEXT, username TEXT, status TEXT DEFAULT 'Вне смены')")
        await db.execute("CREATE TABLE IF NOT EXISTS applications (id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER, client_name TEXT, client_username TEXT, city TEXT, phone TEXT, status TEXT DEFAULT 'Новая', manager_id INTEGER, created_at DATETIME)")
        await db.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f: json.dump({"auto_distribute": False}, f)
    scheduler.add_job(auto_assign_scheduler, "interval", minutes=1)
    scheduler.start()
    current_port = int(os.environ.get("PORT", 3000))
    print_server_info(current_port)
    polling_task = asyncio.create_task(dp.start_polling(bot))
    yield 
    scheduler.shutdown()
    polling_task.cancel()
    await bot.session.close()

app = FastAPI(lifespan=lifespan)
if not os.path.exists("static"): os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- WEB АДМИНКА ---
@app.get("/", response_class=HTMLResponse)
async def admin_panel(request: Request):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        managers = await (await db.execute("SELECT * FROM managers")).fetchall()
        apps = await (await db.execute("SELECT a.*, m.name as mgr_name FROM applications a LEFT JOIN managers m ON a.manager_id = m.tg_id ORDER BY a.id DESC LIMIT 100")).fetchall()
        stats = {
            "total": (await (await db.execute("SELECT COUNT(*) FROM applications")).fetchone())[0],
            "leads": (await (await db.execute("SELECT COUNT(*) FROM applications WHERE status = '✅ ЛИД'")).fetchone())[0],
            "refusals": (await (await db.execute("SELECT COUNT(*) FROM applications WHERE status = '❌ НЕ ЛИД'")).fetchone())[0]
        }
    with open(CONFIG_FILE, "r") as f: config = json.load(f)
    return templates.TemplateResponse("admin.html", {"request": request, "managers": managers, "apps": apps, "stats": stats, "auto_dist": config.get("auto_distribute", False)})

@app.post("/toggle_auto")
async def toggle_auto():
    with open(CONFIG_FILE, "r") as f: config = json.load(f)
    config["auto_distribute"] = not config.get("auto_distribute", False)
    with open(CONFIG_FILE, "w") as f: json.dump(config, f)
    return RedirectResponse(url="/", status_code=303)

# --- ЛОГИКА МЕНЕДЖЕРА ---
@dp.message(F.text == "/myip")
async def get_server_ip(message: types.Message):
    if requests:
        try:
            ip = requests.get('https://api.ipify.org', timeout=5).text
            await message.answer(f"🌐 **IP сервера:** `{ip}`\n🔗 **Админка:** http://{ip}:3000", parse_mode="Markdown")
        except: await message.answer("Ошибка связи с сервисом IP.")
    else: await message.answer("Библиотека `requests` не установлена.")

@dp.message(F.text.in_(["🟢 Начать смену", "🔴 Завершить смену"]))
async def toggle_shift(message: types.Message):
    status = "На смене" if "Начать" in message.text else "Вне смены"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE managers SET status = ? WHERE tg_id = ?", (status, message.from_user.id))
        await db.commit()
    await message.answer(f"Ваш статус: {status}", reply_markup=get_mgr_kb())

@dp.callback_query(F.data.startswith("status_"))
async def handle_status(cb: types.CallbackQuery):
    status_type = "lead" if "lead" in cb.data else "notlead"
    status_text = "✅ ЛИД" if status_type == "lead" else "❌ НЕ ЛИД"
    app_id = cb.data.split("_")[-1]
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT client_id FROM applications WHERE id = ?", (app_id,))).fetchone()
        client_id = row[0] if row else None
        await db.execute("UPDATE applications SET status = ? WHERE id = ?", (status_text, app_id))
        await db.commit()
    if status_type == "lead" and client_id:
        try:
            msg = f"🎉 **Заявка одобрена!**\n\n🔗 Ссылка: {PARTNER_URL}\n{LEARNING_MATERIALS}"
            await bot.send_message(client_id, msg, parse_mode="Markdown")
        except: pass
    await cb.message.edit_text(cb.message.text + f"\n\n🏁 Результат: {status_text}")

# --- АНКЕТА КЛИЕНТА ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        mgr = await (await db.execute("SELECT status FROM managers WHERE tg_id = ?", (message.from_user.id,))).fetchone()
    if mgr:
        await message.answer(f"Кабинет менеджера. Смена: {mgr[0]}", reply_markup=get_mgr_kb())
        return
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Да, мне есть 18 лет ✅"), KeyboardButton(text="Нет ❌")]], resize_keyboard=True)
    await message.answer("Привет! Вам есть 18 лет?", reply_markup=kb)
    await state.set_state(CourierForm.age_check)

@dp.message(CourierForm.age_check)
async def proc_age(message: types.Message, state: FSMContext):
    if "Да" in message.text:
        await message.answer("Ваш город?", reply_markup=ReplyKeyboardRemove())
        await state.set_state(CourierForm.city)
    else: await message.answer("Извините, работа только 18+.")

@dp.message(CourierForm.city)
async def proc_city(message: types.Message, state: FSMContext):
    await state.update_data(city=message.text)
    await message.answer("Ваше гражданство?")
    await state.set_state(CourierForm.citizenship)

@dp.message(CourierForm.citizenship)
async def proc_cit(message: types.Message, state: FSMContext):
    await state.update_data(citizenship=message.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Пешком"), KeyboardButton(text="Вело"), KeyboardButton(text="Авто")]], resize_keyboard=True)
    await message.answer("Транспорт для работы:", reply_markup=kb)
    await state.set_state(CourierForm.transport)

@dp.message(CourierForm.transport)
async def proc_trans(message: types.Message, state: FSMContext):
    await state.update_data(transport=message.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Отправить контакт", request_contact=True)]], resize_keyboard=True)
    await message.answer("Нажмите кнопку ниже, чтобы отправить номер телефона:", reply_markup=kb)
    await state.set_state(CourierForm.phone)

@dp.message(CourierForm.phone, F.contact)
async def finish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    phone = message.contact.phone_number
    info = f"{data['city']} | {data['citizenship']} | {data['transport']}"
    with open(CONFIG_FILE, "r") as f: config = json.load(f)
    m_id, m_user = await get_best_manager() if config.get("auto_distribute") else (None, None)
    async with aiosqlite.connect(DB_PATH) as db:
        st = "В работе" if m_id else "Новая"
        res = await db.execute("INSERT INTO applications (client_id, client_name, client_username, city, phone, created_at, manager_id, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (message.from_user.id, message.from_user.full_name, message.from_user.username, info, phone, datetime.now(), m_id, st))
        app_id = res.lastrowid
        await db.commit()
    if m_id:
        try:
            kb = get_app_inline_kb(app_id, message.from_user.username, message.from_user.id)
            await bot.send_message(m_id, f"📥 **НОВАЯ ЗАЯВКА**\n👤 {message.from_user.full_name}\n📞 `{phone}`\nℹ️ {info}", parse_mode="Markdown", reply_markup=kb)
        except: pass
    await message.answer("✅ Спасибо! Ваша заявка принята. Менеджер свяжется с вами в ближайшее время.", reply_markup=ReplyKeyboardRemove())
    await state.clear()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    uvicorn.run(app, host="0.0.0.0", port=port)
