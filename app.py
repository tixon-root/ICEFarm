import os
import re
import time
import random
from flask import Flask, request, jsonify
import telebot
from telebot import types
from pymongo import MongoClient
from dotenv import load_dotenv
from bson.objectid import ObjectId
import logging

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ---------- LOAD ENV ----------
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
WEBHOOK = os.getenv("WEBHOOK")
ADMIN = os.getenv("ADMIN_USERNAME")

# ---------- CONSTANTS (ВАЖНО!) ----------
FARM_CD = 10800          # 3 часа
CHANNEL_ID = "@BANCUS_RUCOY" 
FEE = 0.1                # Комиссия для /send
MIN_WITHDRAW = 30.0      
FEE_GOLD = 3.0           
FEE_BOT_TRANSFER = 1.0   
ADMIN_ID = 6395348885   

# ---------- INIT ----------
bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)

# ---------- DB ----------
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.server_info()  
    db = client["icecoin"]
    users = db["users"]
    battles = db["battles"]
    settings = db["settings"]
    
    yeti_db = client["rucoy"]
    bank_db = yeti_db["bank"] 
    
    users.create_index("username")
    logger.info("База данных подключена")
except Exception as e:
    logger.error(f"Ошибка БД: {e}")
    raise

# ---------- UTILS ----------

def get_user(uid, username, first_name=None):
    try:
        u = users.find_one({"_id": uid})
        display_name = first_name or username or f"User_{uid}"
        if not u:
            u = {
                "_id": uid,
                "username": username or f"user_{uid}",
                "first_name": display_name,
                "balance": 0.0,
                "level": 1,
                "inventory": [], # ТВОЙ СКЛАД/МЕШОК ДЛЯ NFT
                "wins": 0
            }
            users.insert_one(u)
        else:
            # Если имя в ТГ изменилось, обновляем в базе для Топа
            if first_name and u.get("first_name") != first_name:
                users.update_one({"_id": uid}, {"$set": {"first_name": first_name}})
                u["first_name"] = first_name
        return u
    except Exception as e:
        logger.error(f"Ошибка get_user: {e}")
        return None

def farm_amount(level):
    """Доход: до 15 лвла +0.4, после 15 лвла +0.1"""
    if level <= 15:
        return round(0.4 * level, 2)
    else:
        # База за 15 лвл (6.0) + по 0.1 за каждый уровень выше
        return round(6.0 + (level - 15) * 0.1, 2)

def upgrade_price(level):
    """Старая дешевая цена: базовая 1 + 0.8 за каждый уровень"""
    return round(1 + level * 0.8, 2)

def fmt(x): 
    return round(float(x), 2)

def is_subscribed(m):
    """Проверка подписки на канал"""
    try:
        status = bot.get_chat_member(CHANNEL_ID, m.from_user.id).status
        if status in ["member", "administrator", "creator"]:
            return True
    except Exception as e:
        # Если бот не админ или канала нет — пускаем игрока, чтобы не ломать игру
        logger.warning(f"Не удалось проверить подписку: {e}")
        return True 
        
    bot.send_message(
        m.chat.id, 
        f"❌ <b>Доступ ограничен!</b>\n\nЧтобы играть, подпишитесь на наш канал: {CHANNEL_ID}",
        parse_mode="HTML",
        message_thread_id=getattr(m, "message_thread_id", None)
    )
    return False

def create_main_keyboard():
    """Главное меню"""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("⛏ Фарм", "⏫ Улучшить")
    kb.add("🏆 Топ", "💸 Отправить")
    kb.add("👤 Профиль", "🎒 Инвентарь") # Добавляем сразу в kb
    return kb

# ---------- WEBHOOK ----------

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    """Обработка webhook от Telegram"""
    try:
        json_data = request.get_json(force=True)
        update = telebot.types.Update.de_json(json_data)
        bot.process_new_updates([update])
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Ошибка webhook: {e}")
        return jsonify({"status": "error"}), 500

@app.route("/")
def index():
    """Проверка работы сервера"""
    return jsonify({
        "status": "online",
        "bot": "ICECOIN",
        "version": "2.0"
    })

@app.route("/set_webhook")
def set_webhook():
    """Установка webhook"""
    try:
        bot.remove_webhook()
        time.sleep(1)
        result = bot.set_webhook(url=f"{WEBHOOK}/{TOKEN}")
        return jsonify({"webhook_set": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------- START ----------

@bot.message_handler(commands=["start"])
def start(m):
    """Команда /start"""
    try:
        # ПРОВЕРКА: Если это не личные сообщения (private), бот просто игнорирует команду
        if m.chat.type != "private":
            return 

        u = get_user(m.from_user.id, m.from_user.username)
        if not u:
            bot.send_message(m.chat.id, "❌ Ошибка получения данных")
            return

        # ПОЛУЧАЕМ АКТУАЛЬНУЮ ЦЕНУ
        price_doc = settings.find_one({"_id": "ice_price"})
        current_price = price_doc["value"] if price_doc else "не установлен"

        txt = f"""
❄️ <b>ICECOIN - Криптовалютная игра</b>

👤 @{u['username']}
🆔 <code>{u['_id']}</code>
💰 Баланс: <b>{fmt(u['balance'])} ICE</b>
⛏ Уровень фарма: <b>{u['level']}</b>
🏆 Побед в батлах: <b>{u['wins']}</b>

📊 <b>Курс: 1 ICE = {current_price} GOLD</b>

<i>Выберите действие из меню:</i>
"""
        # Кнопки (ReplyKeyboardMarkup) отправляются только здесь
        bot.send_message(
            m.chat.id, 
            txt, 
            reply_markup=create_main_keyboard(),
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Ошибка start: {e}")
        bot.send_message(m.chat.id, "❌ Произошла ошибка")

# ---------- PROFILE ----------

@bot.message_handler(func=lambda m: m.text in ["👤 Профиль", "/profile"])
def profile(m):
    """Показать профиль"""
    try:
        u = get_user(m.from_user.id, m.from_user.username)
        if not u:
            # Безопасная отправка сообщения (проверка на наличие темы)
            t_id = getattr(m, 'message_thread_id', None)
            bot.send_message(m.chat.id, "❌ Ошибка получения данных", message_thread_id=t_id)
            return

        now = int(time.time())
        # БЕЗОПАСНО: используем .get("farm", 0) вместо ["farm"]
        last_farm = u.get("farm", 0)
        next_farm = last_farm + FARM_CD - now
        
        if next_farm <= 0:
            farm_status = "✅ Доступно!"
        else:
            # Считаем часы и минуты для красоты
            mins = next_farm // 60
            farm_status = f"⏳ Через {mins} мин"
        
                # Внутри profile(m)
        txt = (f"👤 <b>Профиль @{u['username']}</b>\n\n"
               f"💰 Баланс: <b>{fmt(u['balance'])} ICE</b>\n"
               f"⛏ Уровень: <b>{u['level']}</b>\n"
               f"📈 Доход: <b>{farm_amount(u['level'])} ICE</b>\n" # Берет новую формулу
               f"⏫ Цена апа: <b>{upgrade_price(u['level'])} ICE</b>\n" # Берет дешевую формулу
               f"🏆 Побед: {u.get('wins', 0)}\n\n"
               f"⛏ Статус: {farm_status}")

        
        bot.send_message(
            m.chat.id, 
            txt, 
            parse_mode="HTML",
            message_thread_id=getattr(m, 'message_thread_id', None)
        )

    except Exception as e:
        logger.error(f"Ошибка profile: {e}")
        # Если ошибка всё же случилась, бот не просто молчит, а пишет лог
        bot.send_message(m.chat.id, "❌ Произошла ошибка в профиле", 
                         message_thread_id=getattr(m, 'message_thread_id', None))

# ---------- FARM ----------

@bot.message_handler(func=lambda m: m.text == "⛏ Фарм" or m.text == "/farm")
def farm(m):
    if not is_subscribed(m): return # ДОБАВИТЬ ЭТО
    try:
        u = get_user(m.from_user.id, m.from_user.username)
        if not u:
            bot.send_message(m.chat.id, "❌ Ошибка получения данных", message_thread_id=m.message_thread_id)
            return

        now = int(time.time())
        time_passed = now - u["farm"]

        if time_passed < FARM_CD:
            wait = FARM_CD - time_passed
            hours = wait // 3600
            minutes = (wait % 3600) // 60
            bot.send_message(
                m.chat.id, 
                f"⏳ Следующий фарм через: <b>{hours}ч {minutes}м</b>",
                parse_mode="HTML",
                message_thread_id=m.message_thread_id # Добавлено
            )
            return

        gain = farm_amount(u["level"])
        new_balance = fmt(u["balance"] + gain)

        users.update_one(
            {"_id": u["_id"]},
            {"$set": {"farm": now, "balance": new_balance}}
        )
        
        bot.send_message(
            m.chat.id, 
            f"❄️ Вы добыли <b>{gain} ICE</b>\n💰 Баланс: <b>{new_balance} ICE</b>",
            parse_mode="HTML",
            message_thread_id=m.message_thread_id # Добавлено
        )
        
    except Exception as e:
        logger.error(f"Ошибка farm: {e}")
        bot.send_message(m.chat.id, "❌ Произошла ошибка", message_thread_id=m.message_thread_id)
        

# ---------- UPGRADE ----------

@bot.message_handler(func=lambda m: m.text == "⏫ Улучшить" or m.text == "/upgrade")
def upgrade(m):
    """Улучшение уровня фарма"""
    try:
        u = get_user(m.from_user.id, m.from_user.username)
        if not u:
            bot.send_message(m.chat.id, "❌ Ошибка получения данных", message_thread_id=m.message_thread_id)
            return

        price = upgrade_price(u["level"])

        if u["balance"] < price:
            bot.send_message(
                m.chat.id, 
                f"❌ Недостаточно средств!\nНужно: <b>{price} ICE</b>\nУ вас: <b>{fmt(u['balance'])} ICE</b>",
                parse_mode="HTML",
                message_thread_id=m.message_thread_id # Добавлено
            )
            return

        new_level = u["level"] + 1
        new_balance = fmt(u["balance"] - price)
        new_farm_amount = farm_amount(new_level)

        users.update_one({"_id": u["_id"]}, {"$set": {"balance": new_balance, "level": new_level}})
        
        bot.send_message(
            m.chat.id,
            f"✅ <b>Уровень фарма повышен!</b>\n\n"
            f"⛏ Новый уровень: <b>{new_level}</b>\n"
            f"📈 Добыча за фарм: <b>{new_farm_amount} ICE</b>\n"
            f"💰 Остаток: <b>{new_balance} ICE</b>",
            parse_mode="HTML",
            message_thread_id=m.message_thread_id # Добавлено
        )
    except Exception as e:
        logger.error(f"Ошибка upgrade: {e}")
        bot.send_message(m.chat.id, "❌ Произошла ошибка", message_thread_id=m.message_thread_id)

# --- Кнопка инвентаря (поддерживает текст и команду) ---
@bot.message_handler(func=lambda m: m.text in ["🎒 Инвентарь", "/inv"])
def show_inventory(m):
    try:
        t_id = getattr(m, 'message_thread_id', None)
        u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
        inv = u.get("inventory", [])
        
        if not inv:
            bot.send_message(m.chat.id, "📭 Твой инвентарь пуст.", message_thread_id=t_id)
            return

        kb = types.InlineKeyboardMarkup(row_width=1)
        for i, item in enumerate(inv):
            # i - это индекс предмета в массиве, его мы передаем в callback_data
            kb.add(types.InlineKeyboardButton(f"🖼 {item['name']}", callback_data=f"view_nft_{i}"))
        
        bot.send_message(m.chat.id, "🎒 <b>Твой инвентарь:</b>", reply_markup=kb, parse_mode="HTML", message_thread_id=t_id)
    except Exception as e:
        logger.error(f"Ошибка инвентаря: {e}")

# --- Обработчик нажатия на кнопку предмета ---
@bot.callback_query_handler(func=lambda c: c.data.startswith("view_nft_"))
def view_nft_callback(c):
    try:
        t_id = getattr(c.message, 'message_thread_id', None)
        # Получаем данные игрока
        u = users.find_one({"_id": c.from_user.id})
        # Вытаскиваем индекс из callback_data (view_nft_0 -> 0)
        index = int(c.data.split("_")[2])
        inv = u.get("inventory", [])
        
        if index < len(inv):
            nft = inv[index]
            if nft.get("type") == "photo":
                bot.send_photo(c.message.chat.id, nft["file_id"], 
                               caption=f"🖼 NFT: <b>{nft['name']}</b>", 
                               parse_mode="HTML", message_thread_id=t_id)
            else:
                bot.send_animation(c.message.chat.id, nft["file_id"], 
                                   caption=f"🖼 NFT: <b>{nft['name']}</b>", 
                                   parse_mode="HTML", message_thread_id=t_id)
        
        bot.answer_callback_query(c.id)
    except Exception as e:
        logger.error(f"Ошибка показа NFT: {e}")
        bot.answer_callback_query(c.id, "❌ Ошибка")

#-------------SEND----------
@bot.message_handler(func=lambda m: m.text == "💸 Отправить" or (m.text and m.text.startswith("/send")))
def send(m):
    """Отправка монет (поддерживает Reply, прямую команду и кнопку)"""
    if not is_subscribed(m): return

    # Проверка на нажатие кнопки
    if m.text == "💸 Отправить":
        bot.reply_to(m, "💡 Чтобы отправить ICE, используйте команду:\n<code>/send ID СУММА</code>\nИли ответьте на сообщение игрока: <code>/send СУММА</code>", parse_mode="HTML")
        return

    try:
        parts = m.text.split()
        to_id = None
        amount = 0.0

        # Определяем ID получателя и сумму
        if m.reply_to_message:
            if len(parts) < 2:
                bot.reply_to(m, "❌ Укажите сумму.\nПример: <code>/send 10</code>", parse_mode="HTML")
                return
            to_id = m.reply_to_message.from_user.id
            amount = float(parts[1].replace(',', '.'))
        else:
            if len(parts) < 3:
                bot.send_message(m.chat.id, "❌ Формат: <code>/send ID СУММА</code>", parse_mode="HTML")
                return
            to_id = int(parts[1])
            amount = float(parts[2].replace(',', '.'))

        if amount <= FEE:
            bot.reply_to(m, f"❌ Сумма должна быть больше комиссии ({FEE} ICE)")
            return

        u = get_user(m.from_user.id, m.from_user.username)
        
        # ПРОВЕРКА БАЛАНСА (с округлением)
        if round(u["balance"], 8) < round(amount, 8):
            bot.reply_to(m, f"❌ Недостаточно средств!\n\n(⚠️ Переводы по ID работает только в личке боте)\nВаш баланс: <b>{fmt(u['balance'])} ICE</b>", parse_mode="HTML")
            return

        recipient = users.find_one({"_id": to_id})
        if not recipient:
            bot.reply_to(m, "❌ Получатель не найден в базе бота.")
            return

        if m.from_user.id == to_id:
            bot.reply_to(m, "❌ Нельзя отправить самому себе.")
            return

        # РАСЧЕТ: получатель получит (сумма - комиссия)
        amount_to_receive = round(amount - FEE, 8)

        # Проведение транзакции в базе
        users.update_one({"_id": u["_id"]}, {"$inc": {"balance": -amount}})
        users.update_one({"_id": to_id}, {"$inc": {"balance": amount_to_receive}})

        # ПОДТВЕРЖДЕНИЕ В ТУ ЖЕ ТЕМУ
        bot.send_message(
            m.chat.id,
            f"✅ <b>Перевод выполнен!</b>\n\n"
            f"👤 От: @{u['username']}\n"
            f"👤 Кому: @{recipient.get('username', to_id)}\n"
            f"💰 Списано: <b>{fmt(amount)} ICE</b>\n"
            f"📥 Получено: <b>{fmt(amount_to_receive)} ICE</b>\n"
            f"💳 Комиссия: <b>{FEE} ICE</b>",
            parse_mode="HTML",
            message_thread_id=m.message_thread_id
        )

    except (ValueError, IndexError):
        bot.reply_to(m, "❌ Ошибка! Проверьте сумму или ID пользователя.")
    except Exception as e:
        logger.error(f"Ошибка в функции send: {e}")
        bot.reply_to(m, "❌ Произошла ошибка при выполнении перевода.")
        
# ---------- TOP ----------

@bot.message_handler(func=lambda m: m.text in ["🏆 Топ", "/top"])
def top_menu(m):
    """Меню выбора типа топа с кнопками в ряд и одной снизу"""
    if not is_subscribed(m): return
    
    kb = types.InlineKeyboardMarkup(row_width=2)
    # Кнопки в ряд
    b1 = types.InlineKeyboardButton("💰 По балансу", callback_data="top_balance")
    b2 = types.InlineKeyboardButton("🎖 По уровню", callback_data="top_level")
    # Кнопка на отдельной строке снизу
    b3 = types.InlineKeyboardButton("⚔️ По победам", callback_data="top_wins")
    
    kb.add(b1, b2) # Первый ряд
    kb.add(b3)     # Второй ряд
    
    bot.send_message(
        m.chat.id, 
        "<b>Выберите таблицу лидеров:</b>", 
        parse_mode="HTML", 
        reply_markup=kb,
        message_thread_id=getattr(m, 'message_thread_id', None)
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("top_"))
def top_callback(c):
    """Обработка выбора топа с отображением Имени"""
    try:
        data = c.data
        if data == "top_balance":
            sort_field = "balance"
            title = "🏆 <b>ТОП-10 БОГАТЕЕВ (ICE)</b>"
            unit = "ICE"
        elif data == "top_level":
            sort_field = "level"
            title = "🎖 <b>ТОП-10 МАСТЕРОВ ФАРМА</b>"
            unit = "LVL"
        else:
            sort_field = "wins"
            title = "⚔️ <b>ТОП-10 ГЛАДИАТОРОВ</b>"
            unit = "побед"

        # Достаем лучших из базы
        top_users = users.find().sort(sort_field, -1).limit(10)
        
        text = f"{title}\n\n"
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        
        for i, user in enumerate(top_users, 1):
            # ТУТ ИСПРАВЛЕНИЕ: сначала ищем ИМЯ (first_name), если нет - юзернейм
            name = user.get("first_name") or user.get("username") or f"Игрок {user['_id']}"
            
            # Очистка имени от лишних символов, чтобы не ломать HTML
            name = name.replace("<", "").replace(">", "").replace("@", "")
            
            val = user.get(sort_field, 0)
            prefix = medals.get(i, f"{i}.")
            
            # Форматируем значение (для баланса - дробное, для лвла и побед - целое)
            val_fmt = fmt(val) if sort_field == "balance" else int(val)
            
            text += f"{prefix} <b>{name}</b> — {val_fmt} {unit}\n"
        
        bot.edit_message_text(
            text, 
            c.message.chat.id, 
            c.message.message_id, 
            parse_mode="HTML"
        )
        bot.answer_callback_query(c.id)
        
    except Exception as e:
        logger.error(f"Ошибка топа: {e}")
        bot.answer_callback_query(c.id, "❌ Ошибка загрузки данных")

@bot.callback_query_handler(func=lambda c: c.data.startswith("view_nft_"))
def view_nft_callback(c):
    try:
        t_id = getattr(c.message, 'message_thread_id', None)
        u = users.find_one({"_id": c.from_user.id})
        index = int(c.data.split("_")[2])
        inv = u.get("inventory", [])
        
        if index < len(inv):
            nft = inv[index]
            if nft.get("type") == "photo":
                bot.send_photo(c.message.chat.id, nft["file_id"], 
                               caption=f"🖼 NFT: <b>{nft['name']}</b>", 
                               parse_mode="HTML", message_thread_id=t_id)
            else:
                bot.send_animation(c.message.chat.id, nft["file_id"], 
                                   caption=f"🖼 NFT: <b>{nft['name']}</b>", 
                                   parse_mode="HTML", message_thread_id=t_id)
        bot.answer_callback_query(c.id)
    except Exception as e:
        logger.error(f"Error viewing NFT: {e}")
        bot.answer_callback_query(c.id, "❌ Ошибка")

# ---------- BATTLE ----------

# --- блок11---
@bot.message_handler(commands=["batle"])
def battle_call(m):
    if not m.reply_to_message:
        bot.send_message(m.chat.id, "❌ Ответьте на сообщение игрока!", message_thread_id=m.message_thread_id)
        return
    
    challenger = m.from_user
    opponent = m.reply_to_message.from_user

    if challenger.id == opponent.id:
        bot.send_message(m.chat.id, "❌ Нельзя вызвать самого себя!", message_thread_id=m.message_thread_id)
        return

    # Создаем запись в базе
    battle_id = battles.insert_one({
        "challenger_id": challenger.id,
        "challenger_name": challenger.first_name,
        "opponent_id": opponent.id,
        "opponent_name": opponent.first_name,
        "status": "waiting",
        "chat_id": m.chat.id,
        "thread_id": m.message_thread_id
    }).inserted_id

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Принять", callback_data=f"b_acc_{battle_id}"),
        types.InlineKeyboardButton("❌ Отказаться", callback_data=f"b_den_{battle_id}")
    )

    text = f"🔔 <b>{opponent.first_name}</b>, минуточку внимания!\n" \
           f"⚔️ <b>{challenger.first_name}</b> вызывает вас на батл бросания кубов!"
    
    bot.send_message(m.chat.id, text, reply_markup=kb, parse_mode="HTML", message_thread_id=m.message_thread_id)

# --- Обработка кнопок ---
@bot.callback_query_handler(func=lambda c: c.data.startswith("b_"))
def battle_callback(c):
    data = c.data.split("_")
    action = data[1] # acc, den, bet
    bid = ObjectId(data[2])
    battle = battles.find_one({"_id": bid})

    if not battle:
        bot.answer_callback_query(c.id, "Батл не найден")
        return

    # --- ОТКАЗ ---
    if action == "den":
        if c.from_user.id != battle["opponent_id"]:
            bot.answer_callback_query(c.id, "Это не ваш вызов!")
            return
        bot.edit_message_text("❌ Батл был отклонен.", battle["chat_id"], c.message.message_id)
        battles.delete_one({"_id": bid})

    # --- ПРИНЯТИЕ (Выбор ставки) ---
    elif action == "acc":
        if c.from_user.id != battle["opponent_id"]:
            bot.answer_callback_query(c.id, "Это не ваш вызов!")
            return
        
        kb = types.InlineKeyboardMarkup(row_width=3)
        btns = [types.InlineKeyboardButton(f"{x} ❄️", callback_data=f"b_bet_{bid}_{x}") for x in [1, 5, 10, 25, 50, 100]]
        kb.add(*btns)
        
        bot.edit_message_text("💰 Выберите ставку для батла:", battle["chat_id"], c.message.message_id, reply_markup=kb)

    # --- СТАВКА ВЫБРАНА (Начало игры) ---
    elif action == "bet":
        bet = float(data[3])
        if c.from_user.id != battle["opponent_id"]:
            bot.answer_callback_query(c.id, "Ставку должен выбрать тот, кого вызвали!")
            return

        # Проверка баланса у обоих
        p1 = users.find_one({"_id": battle["challenger_id"]})
        p2 = users.find_one({"_id": battle["opponent_id"]})

        if p1["balance"] < bet or p2["balance"] < bet:
            bot.send_message(battle["chat_id"], "❌ У одного из игроков не хватает ICE!", message_thread_id=battle["thread_id"])
            battles.delete_one({"_id": bid})
            return

        # Начинаем процесс бросков
        bot.delete_message(battle["chat_id"], c.message.message_id)
        run_battle(battle, bet)

def run_battle(battle, bet):
    chat_id = battle["chat_id"]
    t_id = battle["thread_id"]

    # Бросок первого (вызвавшего)
    msg1 = bot.send_message(chat_id, f"🎲 Первым бросает <b>{battle['challenger_name']}</b>...", parse_mode="HTML", message_thread_id=t_id)
    dice1 = bot.send_dice(chat_id, message_thread_id=t_id)
    val1 = dice1.dice.value
    time.sleep(4) # Ждем пока кубик докрутится

    # Бросок второго (принявшего)
    msg2 = bot.send_message(chat_id, f"🎲 Теперь куб бросает <b>{battle['opponent_name']}</b>...", parse_mode="HTML", message_thread_id=t_id)
    dice2 = bot.send_dice(chat_id, message_thread_id=t_id)
    val2 = dice2.dice.value
    time.sleep(4)

    # Определение победителя
    winner_id = None
    if val1 > val2:
        winner_id, winner_name = battle["challenger_id"], battle["challenger_name"]
        loser_id = battle["opponent_id"]
    elif val2 > val1:
        winner_id, winner_name = battle["opponent_id"], battle["opponent_name"]
        loser_id = battle["challenger_id"]
    else:
        bot.send_message(chat_id, "🤝 Ничья! ICE остаются при своих.", message_thread_id=t_id)
        battles.delete_one({"_id": battle["_id"]})
        return

    # Начисление/Списание
    users.update_one({"_id": winner_id}, {"$inc": {"balance": bet, "wins": 1}})
    users.update_one({"_id": loser_id}, {"$inc": {"balance": -bet}})

    bot.send_message(chat_id, f"🏆 Победил <b>{winner_name}</b>!\n💰 Счет пополнен на <b>+{bet} ICE</b>", 
                     parse_mode="HTML", message_thread_id=t_id)
    
    battles.delete_one({"_id": battle["_id"]})

# ---------- ОБЪЕДИНЕННАЯ АДМИН-СИСТЕМА ----------

@bot.message_handler(commands=["admin"])
def admin_panel(m):
    """Общая статистика (твоя прошлая функция)"""
    if m.from_user.id != ADMIN_ID: return
    try:
        total_users = users.count_documents({})
        pipeline = [{"$group": {"_id": None, "total": {"$sum": "$balance"}}}]
        result = list(users.aggregate(pipeline))
        total_sum = result[0]['total'] if result else 0
        
        txt = (f"👑 <b>АДМИН-ПАНЕЛЬ</b>\n\n"
               f"👥 Всего пользователей: <b>{total_users}</b>\n"
               f"💰 Всего в обороте: <b>{fmt(total_sum)} ICE</b>\n\n"
               f"<b>Команды:</b>\n"
               f"/stats ID — Управление игроком\n"
               f"/broadcast — Сделать рассылку\n"
               f"/give_nft - создать нфт\n"
               f"/setprice ЦЕНА — Изменение курса")
        bot.send_message(m.chat.id, txt, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка в админ-панели: {e}")

@bot.message_handler(commands=["stats"])
def admin_manage_user(m):
    """ТВОЙ БЛОК: Вызов карточки управления игроком через /stats ID"""
    if m.from_user.id != ADMIN_ID: return
    try:
        parts = m.text.split()
        if len(parts) < 2:
            bot.reply_to(m, "💡 Формат: <code>/stats ID</code>", parse_mode="HTML")
            return
        
        target_id = int(parts[1])
        u = users.find_one({"_id": target_id})
        
        if not u:
            bot.reply_to(m, "❌ Пользователь не найден в базе.")
            return

        # ТВОИ КНОПКИ
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("💰 Баланс", callback_data=f"adm_edit_bal_{target_id}"),
            types.InlineKeyboardButton("📈 Уровень", callback_data=f"adm_edit_lvl_{target_id}"),
            types.InlineKeyboardButton("❌ Закрыть", callback_data="adm_close")
        )
        
        txt = (f"🎛 <b>Панель управления игроком</b>\n\n"
               f"👤 Ник: <b>{u.get('first_name', 'Не указан')}</b>\n"
               f"🆔 ID: <code>{u['_id']}</code>\n\n"
               f"💰 Баланс: <code>{fmt(u['balance'])}</code> ICE\n"
               f"⛏ Уровень: <code>{u['level']}</code> LVL\n"
               f"🏆 Побед: {u.get('wins', 0)}")
        
        bot.send_message(m.chat.id, txt, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        bot.reply_to(m, f"❌ Ошибка: {e}")

# ---------- ЛОГИКА КНОПОК И ВВОДА ДАННЫХ ----------

@bot.callback_query_handler(func=lambda c: c.data.startswith("adm_"))
def admin_callback(c):
    if c.from_user.id != ADMIN_ID: return
    if c.data == "adm_close":
        bot.delete_message(c.message.chat.id, c.message.message_id)
        return

    data = c.data.split("_")
    action = data[2] 
    target_id = int(data[3])
    
    label = "баланс" if action == "bal" else "уровень"
    msg = bot.send_message(c.message.chat.id, f"⌨️ Введите новый <b>{label}</b> для <code>{target_id}</code>:", parse_mode="HTML")
    
    if action == "bal":
        bot.register_next_step_handler(msg, save_admin_balance, target_id)
    else:
        bot.register_next_step_handler(msg, save_admin_level, target_id)
    bot.answer_callback_query(c.id)

def save_admin_balance(m, target_id):
    try:
        new_val = float(m.text.replace(',', '.'))
        users.update_one({"_id": target_id}, {"$set": {"balance": round(new_val, 2)}})
        bot.send_message(m.chat.id, f"✅ Баланс игрока <code>{target_id}</code> изменен на <b>{new_val} ICE</b>", parse_mode="HTML")
    except:
        bot.send_message(m.chat.id, "❌ Ошибка! Введите число.")

def save_admin_level(m, target_id):
    try:
        new_val = int(m.text)
        users.update_one({"_id": target_id}, {"$set": {"level": new_val}})
        bot.send_message(m.chat.id, f"✅ Уровень игрока <code>{target_id}</code> изменен на <b>{new_val} LVL</b>", parse_mode="HTML")
    except:
        bot.send_message(m.chat.id, "❌ Ошибка! Введите целое число.")

# ---------- РАССЫЛКА ----------

@bot.message_handler(commands=["broadcast"])
def broadcast(m):
    if m.from_user.id != ADMIN_ID: return
    msg = bot.reply_to(m, "Введите текст или пришлите фото. /cancel для отмены")
    bot.register_next_step_handler(msg, start_broadcast)

def start_broadcast(m):
    if m.text == "/cancel":
        bot.send_message(m.chat.id, "Отменено.")
        return
    all_u = users.find()
    count = 0
    for u in all_u:
        try:
            if m.content_type == 'photo':
                bot.send_photo(u["_id"], m.photo[-1].file_id, caption=m.caption, parse_mode="HTML")
            else:
                bot.send_message(u["_id"], m.text, parse_mode="HTML")
            count += 1
            time.sleep(0.05)
        except: continue
    bot.send_message(m.chat.id, f"✅ Рассылка завершена: {count} чел.")
    

# ---------- ADMIN COMMANDS ----------

@bot.message_handler(commands=["give"])
def admin_give(m):
    """Начислить монеты (только для админа)"""
    # Замени 12345678 на свой реальный ID
    ADMIN_ID = 6395348885 
    
    if m.from_user.id != ADMIN_ID:
        bot.send_message(m.chat.id, "❌ У вас нет прав администратора!", message_thread_id=m.message_thread_id)
        return

    try:
        parts = m.text.split()
        if len(parts) != 3:
            bot.send_message(m.chat.id, "🔧 Формат: `/give ID СУММА`", parse_mode="Markdown", message_thread_id=m.message_thread_id)
            return

        to_id = int(parts[1])
        amount = float(parts[2])

        # Начисляем монеты в базе
        result = users.update_one({"_id": to_id}, {"$inc": {"balance": amount}})

        if result.matched_count > 0:
            bot.send_message(m.chat.id, f"✅ Успешно начислено **{amount} ICE** пользователю `{to_id}`", parse_mode="Markdown", message_thread_id=m.message_thread_id)
            # Уведомляем счастливчика
            try:
                bot.send_message(to_id, f"🎁 Админ начислил вам **{amount} ICE**!", parse_mode="Markdown")
            except:
                pass
        else:
            bot.send_message(m.chat.id, "❌ Пользователь не найден в базе!", message_thread_id=m.message_thread_id)

    except Exception as e:
        logger.error(f"Ошибка give: {e}")
        bot.send_message(m.chat.id, "❌ Ошибка при выполнении команды", message_thread_id=m.message_thread_id)


# Команда для установки курса (только для админа)
@bot.message_handler(commands=["setprice"])
def set_price(m):
    if m.from_user.id != 6395348885: return # Замени ADMIN_ID на свой ID
    
    try:
        # Пример: /setprice 8000
        new_price = m.text.split()[1]
        # Сохраняем цену в специальную коллекцию settings
        db.settings.update_one({"_id": "ice_price"}, {"$set": {"value": new_price}}, upsert=True)
        bot.reply_to(m, f"✅ Курс обновлен: 1 ICE = {new_price} GOLD")
    except:
        bot.reply_to(m, "❌ Ошибка. Используйте: <code>/setprice 8000</code>", parse_mode="HTML")

# Функция для получения текущей цены из базы
def get_current_price():
    price_doc = db.settings.find_one({"_id": "ice_price"})
    return price_doc["value"] if price_doc else "не установлен"

@bot.message_handler(content_types=['photo', 'animation'])
def admin_give_nft(m):
    # 1. Проверка админа
    if m.from_user.id != ADMIN_ID: 
        return
    
    # 2. Проверяем, есть ли текст команды в подписи (caption)
    if not m.caption or not m.caption.startswith('/give_nft'):
        return

    try:
        t_id = getattr(m, 'message_thread_id', None)
        parts = m.caption.split(maxsplit=2)
        
        if len(parts) < 3:
            bot.reply_to(m, "📝 <b>Формат:</b> Прикрепите фото/гиф и подпишите:\n<code>/give_nft [ID] [Название]</code>", parse_mode="HTML")
            return

        target_id = int(parts[1])
        nft_name = parts[2]
        
        # Определяем file_id в зависимости от типа
        if m.content_type == 'photo':
            file_id = m.photo[-1].file_id
        else: # animation
            file_id = m.animation.file_id
        
        nft_data = {
            "name": nft_name, 
            "file_id": file_id, 
            "type": m.content_type, 
            "date": int(time.time())
        }
        
        # Записываем в базу
        result = users.update_one({"_id": target_id}, {"$push": {"inventory": nft_data}})
        
        if result.matched_count > 0:
            bot.send_message(m.chat.id, f"✅ NFT «<b>{nft_name}</b>» успешно выдано игроку <code>{target_id}</code>", 
                             parse_mode="HTML", message_thread_id=t_id)
        else:
            bot.reply_to(m, "❌ Пользователь не найден в базе.")
                         
    except Exception as e:
        logger.error(f"Ошибка выдачи NFT: {e}")
        bot.reply_to(m, f"❌ Ошибка: {e}")
        
#------вывод блят-------------

@bot.message_handler(commands=["withdraw"])
def withdraw(m):
    if not is_subscribed(m): return

    # 1. Получаем курс
    price_doc = settings.find_one({"_id": "ice_price"})
    if not price_doc:
        bot.reply_to(m, "❌ Курс обмена еще не установлен админом.")
        return
    
    try:
        # Извлекаем число из курса (удаляем пробелы и точки)
        rate_str = "".join(re.findall(r"(\d+)", price_doc["value"]))
        rate = float(rate_str)
    except:
        rate = 8000.0

    parts = m.text.split()
    if len(parts) < 3:
        bot.reply_to(m, "💡 <b>Формат вывода:</b>\n\n"
                        "1️⃣ <b>В золото, комиссия: 3 ❄️ (админу):</b>\n\n<code>/withdraw gold [сумма ICE]</code>\n"
                        "\n2️⃣ <b>В Rucoy Bank комиссия: 1 ❄️ (авто):</b>\n\n<code>/withdraw bot [сумма ICE]</code>", parse_mode="HTML")
        return

    action = parts[1].lower()
    try:
        amount = float(parts[2].replace(',', '.'))
    except:
        bot.reply_to(m, "❌ Укажите корректную сумму.")
        return

    if amount < MIN_WITHDRAW:
        bot.reply_to(m, f"❌ Минимальная сумма вывода: {MIN_WITHDRAW} ICE")
        return

    u = get_user(m.from_user.id, m.from_user.username)
    if u["balance"] < amount:
        bot.reply_to(m, f"❌ Недостаточно средств. Ваш баланс: <b>{fmt(u['balance'])} ICE</b>", parse_mode="HTML")
        return

    # --- ВЫВОД В БОТА (АВТОМАТИЧЕСКИЙ) ---
    if action == "bot":
        fee = FEE_BOT_TRANSFER
        ice_to_send = amount - fee
        gold_to_receive = int(ice_to_send * rate)

        # Списываем ICE
        users.update_one({"_id": m.from_user.id}, {"$inc": {"balance": -amount}})

        try:
            # ЗАЧИСЛЯЕМ В ТАБЛИЦУ BANK (которую видит Yeti)
            # Мы используем bank_db, которую определили в начале файла
            bank_db.update_one(
                {"uid": str(m.from_user.id)}, 
                {"$inc": {"balance": gold_to_receive}, "$set": {"name": m.from_user.first_name}}, 
                upsert=True
            )
            
            bot.reply_to(m, f"✅ <b>Успешный перевод!</b>\n\n"
                            f"💰 Списано: <b>{amount} ICE</b>\n"
                            f"💳 Комиссия: <b>{fee} ICE</b>\n"
                            f"🏦 В Rucoy Bank: <b>{gold_to_receive:,} GOLD</b>", parse_mode="HTML")
            
            # Уведомление админу
            bot.send_message(ADMIN_ID, f"🔔 <b>Авто-вывод</b>\nЮзер: @{u.get('username')} (<code>{u['_id']}</code>)\nСумма: {amount} ICE -> {gold_to_receive} GOLD", parse_mode="HTML")
            
        except Exception as e:
            # Если не вышло записать в базу
            users.update_one({"_id": m.from_user.id}, {"$inc": {"balance": amount}})
            logger.error(f"Ошибка БД при выводе: {e}")
            bot.reply_to(m, "❌ Ошибка базы данных Rucoy Bank. Попробуйте позже.")

    # --- ВЫВОД GOLD (ЧЕРЕЗ АДМИНА) ---
    elif action == "gold":
        fee = FEE_GOLD
        ice_to_send = amount - fee
        gold_to_receive = int(ice_to_send * rate)

        users.update_one({"_id": m.from_user.id}, {"$inc": {"balance": -amount}})

        bot.send_message(ADMIN_ID, f"📤 <b>ЗАЯВКА НА ВЫВОД</b>\n\n"
                                   f"👤 От: @{u.get('username')} (<code>{u['_id']}</code>)\n"
                                   f"💰 ICE: <b>{amount}</b>\n"
                                   f"💵 GOLD: <b>{gold_to_receive:,}</b>\n"
                                   f"⚠️ Выплатить вручную!", parse_mode="HTML")

        bot.reply_to(m, f"✅ <b>Заявка принята!</b>\n\n"
                        f"Списано: <b>{amount} ICE</b>\n"
                        f"Ожидайте: <b>{gold_to_receive:,} GOLD</b>", parse_mode="HTML")
    else:
        bot.reply_to(m, "❌ Используйте: <code>gold</code> или <code>bot</code>", parse_mode="HTML")

#-----------------------------------------------------Handlers---------------------------


# ---------- ERROR HANDLER ----------

@bot.message_handler(func=lambda m: True)
def unknown_command(m):
    # Если сообщение пришло из группы — просто игнорируем его
    if m.chat.type != 'private':
        return
        
    # Если в личке — подсказываем
    bot.reply_to(m, "❓ Неизвестная команда. Используйте меню или /start")

# ---------- RUN ----------

if __name__ == "__main__":
    logger.info("Запуск ICECOIN...")
    
    # На Render переменная WEBHOOK должна содержать https://ваш-домен.onrender.com
    if WEBHOOK and "http" in WEBHOOK:
        try:
            bot.remove_webhook()
            time.sleep(1)
            bot.set_webhook(url=f"{WEBHOOK}/{TOKEN}")
            logger.info(f"Webhook установлен: {WEBHOOK}/{TOKEN}")
            # Порт для Render берется из переменной окружения
            port = int(os.environ.get("PORT", 10000))
            app.run(host="0.0.0.0", port=port)
        except Exception as e:
            logger.error(f"Ошибка при установке Webhook: {e}")
    else:
        logger.info("Запуск через Long Polling (локально)...")
        bot.remove_webhook()
        bot.infinity_polling()
                             
