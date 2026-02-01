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

# --- НАСТРОЙКИ ---
TOKEN = "8236277660:AAE193jYrtDjbUyaKJcDlCnwyrqoZg5qnRE"
DB_PATH = "bot_system.db"
CONFIG_FILE = "managers_config.json"
PARTNER_URL = "https://clck.ru/3RaGGm" # Твоя партнерская ссылка

# Текст обучения
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

# --- КЛАВИАТУРЫ ---
def get_mgr_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🟢 Начать смену"), KeyboardButton(text="🔴 Завершить смену")],
        [KeyboardButton(text="🏃 Активная заявка"), KeyboardButton(text="🔗 Моя ссылка")]
    ], resize_keyboard=True)

def get_app_inline_kb(app_id, client_username=None, client_id=None):
    buttons = []
    if client_username:
        buttons.append([InlineKeyboardButton(text="💬 Написать кандидату", url=f"https://t.me/{client_username}")])
    elif client_id:
        buttons.append([InlineKeyboardButton(text="💬 Открыть профиль", url=f"tg://user?id={client_id}")])
    
    buttons.append([
        InlineKeyboardButton(text="✅ ЛИД", callback_data=f"status_lead_{app_id}"),
        InlineKeyboardButton(text="❌ НЕ ЛИД", callback_data=f"status_notlead_{app_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
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

# --- ФОНОВАЯ ЗАДАЧА АВТО-РАСПРЕДЕЛЕНИЯ (РАЗ В МИНУТУ) ---
async def auto_assign_scheduler():
    try:
        with open(CONFIG_FILE, "r") as f: config = json.load(f)
        if not config.get("auto_distribute", False): return

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT id, client_name, phone, city, client_username, client_id FROM applications WHERE manager_id IS NULL AND status = 'Новая'")
            new_apps = await cursor.fetchall()
            if not new_apps: return

            for app in new_apps:
                app_id, c_name, c_phone, c_city, c_username, c_id = app
                m_id, m_user = await get_best_manager()
                if m_id:
                    await db.execute("UPDATE applications SET manager_id = ?, status = 'В работе' WHERE id = ?", (m_id, app_id))
                    await db.commit()
                    try:
                        kb = get_app_inline_kb(app_id, c_username, c_id)
                        await bot.send_message(m_id, f"🔄 **АВТО-ОЧЕРЕДЬ**\n👤 {c_name}\n📞 `{c_phone}`", parse_mode="Markdown", reply_markup=kb)
                    except: pass
    except: pass

# --- ИНИЦИАЛИЗАЦИЯ ---
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
    polling_task = asyncio.create_task(dp.start_polling(bot))
    yield 
    scheduler.shutdown()
    polling_task.cancel()
    hostname = socket.gethostname()
    ip_address = socket.gethostbyname(hostname)
    print(f"--- LOG: Бот запущен на IP: {ip_address} ---")
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

@app.post("/add_manager")
async def add_mgr(tg_id: int = Form(...), name: str = Form(...), username: str = Form(default="")):
    async with aiosqlite.connect(DB_PATH) as db:
        clean_user = username.replace("@", "")
        await db.execute("INSERT OR REPLACE INTO managers (tg_id, name, username, status) VALUES (?, ?, ?, 'Вне смены')", (tg_id, name, clean_user))
        await db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/assign_manually/{app_id}")
async def assign_manually(app_id: int, manager_id: int = Form(...)):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE applications SET manager_id = ?, status = 'В работе' WHERE id = ?", (manager_id, app_id))
        row = await (await db.execute("SELECT client_name, client_id, phone, client_username FROM applications WHERE id = ?", (app_id,))).fetchone()
        await db.commit()
        if row:
            try: await bot.send_message(manager_id, f"🎯 **Назначена заявка:**\n👤 {row[0]}\n📞 `{row[2]}`", reply_markup=get_app_inline_kb(app_id, row[3], row[1]))
            except: pass
    return RedirectResponse(url="/", status_code=303)

@app.post("/delete_manager/{tg_id}")
async def del_mgr(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM managers WHERE tg_id = ?", (tg_id,))
        await db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/delete_app/{app_id}")
async def del_app(app_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM applications WHERE id = ?", (app_id,))
        await db.commit()
    return RedirectResponse(url="/", status_code=303)

# --- ЛОГИКА МЕНЕДЖЕРА ---
@dp.message(F.text == "🏃 Активная заявка")
async def show_active(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        row = await (await db.execute("SELECT id, client_name, phone, client_username, client_id FROM applications WHERE manager_id = ? AND status = 'В работе' ORDER BY id DESC LIMIT 1", (message.from_user.id,))).fetchone()
        if row: await message.answer(f"🏃 **Активная:**\n👤 {row[1]}\n📞 `{row[2]}`", reply_markup=get_app_inline_kb(row[0], row[3], row[4]))
        else: await message.answer("Нет активных заявок.")

@dp.message(F.text.in_(["🟢 Начать смену", "🔴 Завершить смену"]))
async def toggle_shift(message: types.Message):
    is_starting = "Начать" in message.text
    status = "На смене" if is_starting else "Вне смены"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE managers SET status = ? WHERE tg_id = ?", (status, message.from_user.id))
        if not is_starting:
            await db.execute("UPDATE applications SET manager_id = NULL, status = 'Новая' WHERE manager_id = ? AND status = 'В работе'", (message.from_user.id,))
        await db.commit()
    await message.answer(f"Статус: {status}", reply_markup=get_mgr_kb())

@dp.callback_query(F.data.startswith("status_"))
async def handle_status(cb: types.CallbackQuery):
    status_type = "lead" if "lead" in cb.data else "notlead"
    status_text = "✅ ЛИД" if status_type == "lead" else "❌ НЕ ЛИД"
    app_id = cb.data.split("_")[-1]

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT client_id FROM applications WHERE id = ?", (app_id,))
        row = await cursor.fetchone()
        client_id = row[0] if row else None
        await db.execute("UPDATE applications SET status = ? WHERE id = ?", (status_text, app_id))
        await db.commit()

    if status_type == "lead" and client_id:
        try:
            msg = f"🎉 **Заявка одобрена!**\n\n🔗 Ссылка: {PARTNER_URL}\n{LEARNING_MATERIALS}"
            await bot.send_message(client_id, msg, parse_mode="Markdown")
        except: pass

    await cb.message.edit_text(cb.message.text + f"\n\n🏁 Результат: {status_text}")

@dp.message(F.text == "🔗 Моя ссылка")
async def send_link(message: types.Message):
    await message.answer(f"Партнерская ссылка:\n`{PARTNER_URL}?start={message.from_user.id}`", parse_mode="Markdown")

# --- АНКЕТА ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        mgr = await (await db.execute("SELECT status FROM managers WHERE tg_id = ?", (message.from_user.id,))).fetchone()
    if mgr:
        await message.answer(f"Кабинет менеджера. Смена: {mgr[0]}", reply_markup=get_mgr_kb())
        return
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Да, мне есть 18 лет ✅"), KeyboardButton(text="Нет ❌")]], resize_keyboard=True)
    await message.answer("Вам есть 18 лет?", reply_markup=kb)
    await state.set_state(CourierForm.age_check)

@dp.message(CourierForm.age_check)
async def proc_age(message: types.Message, state: FSMContext):
    if "Да" in message.text:
        await message.answer("Ваш город?", reply_markup=ReplyKeyboardRemove())
        await state.set_state(CourierForm.city)
    else: await message.answer("Доступ запрещен.")

@dp.message(CourierForm.city)
async def proc_city(message: types.Message, state: FSMContext):
    await state.update_data(city=message.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="РФ 🇷🇺"), KeyboardButton(text="СНГ 🌍")]], resize_keyboard=True)
    await message.answer("Гражданство:", reply_markup=kb)
    await state.set_state(CourierForm.citizenship)

@dp.message(CourierForm.citizenship)
async def proc_cit(message: types.Message, state: FSMContext):
    await state.update_data(citizenship=message.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Пешком"), KeyboardButton(text="Вело"), KeyboardButton(text="Авто")]], resize_keyboard=True)
    await message.answer("Транспорт:", reply_markup=kb)
    await state.set_state(CourierForm.transport)

@dp.message(CourierForm.transport)
async def proc_trans(message: types.Message, state: FSMContext):
    await state.update_data(transport=message.text)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="📱 Отправить контакт", request_contact=True)]], resize_keyboard=True)
    await message.answer("Поделитесь контактом:", reply_markup=kb)
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
            await bot.send_message(m_id, f"📥 **АВТО-ЗАЯВКА**\n👤 {message.from_user.full_name}\n📞 `{phone}`", parse_mode="Markdown", reply_markup=kb)
            await message.answer(f"✅ Готово! Менеджер @{m_user} свяжется с вами.", reply_markup=ReplyKeyboardRemove())
        except: pass
    else:
        await message.answer("✅ Заявка принята! Ожидайте связи.", reply_markup=ReplyKeyboardRemove())
    await state.clear()

if __name__ == "__main__":
    # Пытаемся взять порт из настроек хостинга, если нет — ставим 8080
    port = int(os.environ.get("PORT", 8080)) 
    uvicorn.run(app, host="0.0.0.0", port=port)




