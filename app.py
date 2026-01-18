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
FEE = 1.0                # Комиссия для /send
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

def get_user(uid, username):
    """Получить юзера и убедиться, что все поля на месте"""
    try:
        # 1. Ищем пользователя в базе
        u = users.find_one({"_id": uid})
        curr_name = username if username else f"user_{uid}"
        
        if not u:
            # 2. Если пользователя нет — СОЗДАЕМ его
            u = {
                "_id": uid,
                "username": curr_name,
                "first_name": curr_name,
                "balance": 0.0,
                "level": 1,
                "farm": 0,
                "wins": 0
            }
            users.insert_one(u)
            logger.info(f"Зарегистрирован новый игрок: {curr_name}")
        else:
            # 3. Если пользователь есть — ПРОВЕРЯЕМ наличие новых полей (защита от краша)
            updates = {}
            if "first_name" not in u: updates["first_name"] = curr_name
            if "farm" not in u: updates["farm"] = 0
            if "wins" not in u: updates["wins"] = 0
            
            # Обновляем ник, если игрок его сменил в Telegram
            if username and u.get("username") != username:
                updates["username"] = username

            if updates:
                users.update_one({"_id": uid}, {"$set": updates})
                u.update(updates) # Обновляем данные в текущей переменной u
                
        return u
    except Exception as e:
        logger.error(f"Ошибка в get_user: {e}")
        return None

def farm_amount(level):
    """Сбалансированный расчет награды"""
    if level <= 20:
        return round(0.4 * level, 2)
    else:
        # После 20 уровня рост замедляется, чтобы не было гиперинфляции
        return round(8.0 + (level - 20) * 0.2, 2)

def upgrade_price(level):
    """Прогрессивная цена: чем выше уровень, тем сложнее апать"""
    # Формула делает апгрейд на высоких уровнях очень дорогим
    return round(5 + (level ** 1.9), 2)

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
    kb.add("🏆 Топ")
    kb.add("💸 Отправить", "👤 Профиль")
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
        
        txt = (f"👤 <b>Профиль @{u['username']}</b>\n\n"
               f"💰 Баланс: <b>{fmt(u['balance'])} ICE</b>\n"
               f"⛏ Уровень: <b>{u['level']}</b>\n"
               f"📈 Доход: <b>{farm_amount(u['level'])} ICE</b>\n"
               f"⏫ Цена апа: <b>{upgrade_price(u['level'])} ICE</b>\n"
               f"🏆 Побед: <b>{u.get('wins', 0)}</b>\n\n"
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

@bot.message_handler(func=lambda m: m.text == "🏆 Топ" or m.text == "/top")
def top(m):
    # Проверка подписки
    if not is_subscribed(m): return
    
    try:
        # Получаем 10 самых богатых
        top_users = users.find().sort("balance", -1).limit(10)
        
        text = "🏆 <b>ТОП-10 ИГРОКОВ</b>\n\n"
        
        # Медали для красоты, как в старой версии
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        
        for i, user in enumerate(top_users, 1):
            # Используем имя (first_name). Это просто текст, он НЕ ТЕГАЕТ.
            # Если имени нет, используем username, но без символа @
            name = user.get("first_name") or user.get("username") or "Игрок"
            
            # Убираем символ @, если он случайно остался в имени, чтобы точно не было тега
            name = name.replace("@", "") 
            
            prefix = medals.get(i, f"{i}.")
            
            # Стиль: Медаль/Цифра Имя — Баланс ICE
            text += f"{prefix} {name} — {fmt(user['balance'])} ICE\n"
        
        bot.send_message(
            m.chat.id, 
            text, 
            parse_mode="HTML", 
            message_thread_id=m.message_thread_id
        )
        
    except Exception as e:
        logger.error(f"Ошибка в ТОП: {e}")
        bot.reply_to(m, "❌ Ошибка загрузки топа.")

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

# ---------- ADMIN ----------

@bot.message_handler(commands=["admin"])
def admin_panel(m):
    """Админ-панель"""
    if m.from_user.username != ADMIN:
        return
    
    try:
        total_users = users.count_documents({})
        # Агрегация для подсчета общей суммы монет
        pipeline = [{"$group": {"_id": None, "total": {"$sum": "$balance"}}}]
        result = list(users.aggregate(pipeline))
        total_sum = result[0]['total'] if result else 0
        
        txt = f"""
👑 <b>АДМИН-ПАНЕЛЬ</b>

👥 Всего пользователей: <b>{total_users}</b>
💰 Всего монет в обороте: <b>{fmt(total_sum)} ICE</b>

<b>Команды:</b>
/stats ID — Статистика игрока
/broadcast ТЕКСТ — Рассылка всем
/setprice ЦЕНА - изменения цен
"""
        bot.send_message(m.chat.id, txt, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка в админ-панели: {e}")

@bot.message_handler(commands=["stats"])
def stats(m):
    """Статистика конкретного пользователя (только для админа)"""
    if m.from_user.username != ADMIN:
        return
    try:
        parts = m.text.split()
        if len(parts) < 2:
            bot.send_message(m.chat.id, "Использование: /stats ID")
            return
        
        target_id = int(parts[1])
        user = users.find_one({"_id": target_id})
        
        if not user:
            bot.send_message(m.chat.id, "❌ Пользователь не найден")
            return
            
        txt = f"""
📊 <b>Статистика @{user.get('username', 'N/A')}</b>
🆔 ID: <code>{user['_id']}</code>
💰 Баланс: {fmt(user['balance'])} ICE
⛏ Уровень: {user['level']}
🏆 Побед: {user.get('wins', 0)}
"""
        bot.send_message(m.chat.id, txt, parse_mode="HTML")
    except Exception as e:
        bot.send_message(m.chat.id, f"❌ Ошибка: {e}")

@bot.message_handler(commands=["broadcast"])
def broadcast(m):
    # Проверка, что пишет админ (замени 12345678 на свой ID)
    if m.from_user.id != 6395348885:
        return

    # Просим админа прислать пост
    msg = bot.reply_to(m, "Введите текст объявления или пришлите фото с описанием. Для отмены напишите /cancel")
    bot.register_next_step_handler(msg, start_broadcast)

def start_broadcast(m):
    if m.text == "/cancel":
        bot.send_message(m.chat.id, "Рассылка отменена.")
        return

    all_users = users.find()
    count = 0
    
    for user in all_users:
        try:
            # Если админ прислал ФОТО
            if m.content_type == 'photo':
                bot.send_photo(
                    user["_id"], 
                    m.photo[-1].file_id, 
                    caption=m.caption, 
                    parse_mode="HTML"
                )
            # Если админ прислал только ТЕКСТ
            elif m.content_type == 'text':
                bot.send_message(
                    user["_id"], 
                    m.text, 
                    parse_mode="HTML", 
                    disable_web_page_preview=False
                )
            count += 1
        except Exception:
            continue # Пропускаем, если бот заблокирован пользователем

    bot.send_message(m.chat.id, f"✅ Рассылка завершена! Получили: {count} чел.")
    
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
                             
