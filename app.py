import os
import time
import random
from flask import Flask, request, jsonify
import telebot
from telebot import types
from pymongo import MongoClient
from dotenv import load_dotenv
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

# Валидация переменных окружения
if not all([TOKEN, MONGO_URI, WEBHOOK, ADMIN]):
    raise ValueError("Не все переменные окружения установлены!")

# ---------- INIT ----------
bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)

# ---------- DB ----------
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.server_info()  # Проверка подключения
    db = client["icecoin"]
    users = db.users
    battles = db.battles
    
    # Создание индексов для оптимизации
    users.create_index("username")
    users.create_index([("balance", -1)])
    battles.create_index("status")
    logger.info("База данных подключена успешно")
except Exception as e:
    logger.error(f"Ошибка подключения к БД: {e}")
    raise

FARM_CD = 10800  # 3 часа

# ---------- UTILS ----------

def get_user(uid, username):
    """Получить или создать пользователя"""
    try:
        username = username or f"user_{uid}"  # Защита от None
        users.update_one(
            {"_id": uid},
            {"$setOnInsert": {
                "username": username,
                "balance": 0.0,
                "level": 1,
                "farm": 0,
                "wins": 0
            }},
            upsert=True
        )
        return users.find_one({"_id": uid})
    except Exception as e:
        logger.error(f"Ошибка get_user: {e}")
        return None

def farm_amount(level):
    """Расчет награды за фарм"""
    return round(0.4 * level, 2)

def upgrade_price(level):
    """Расчет цены улучшения"""
    return round(1 + level * 0.8, 2)

def fmt(x):
    """Форматирование числа"""
    return round(float(x), 2)

def create_main_keyboard():
    """Создание главной клавиатуры"""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("⛏ Фарм", "⏫ Улучшить")
    kb.add("🏆 Топ", "⚔ Батл")
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
        u = get_user(m.from_user.id, m.from_user.username)
        if not u:
            bot.send_message(m.chat.id, "❌ Ошибка получения данных")
            return

        txt = f"""
❄️ <b>ICECOIN - Криптовалютная игра</b>

👤 @{u['username']}
🆔 <code>{u['_id']}</code>
💰 Баланс: <b>{fmt(u['balance'])} ICE</b>
⛏ Уровень фарма: <b>{u['level']}</b>
🏆 Побед в батлах: <b>{u['wins']}</b>

<i>Выберите действие из меню:</i>
"""
                bot.send_message(
            m.chat.id, 
            txt, 
            reply_markup=create_main_keyboard(),
            parse_mode="HTML",
            message_thread_id=m.message_thread_id # Добавлено
)
        
    except Exception as e:
        logger.error(f"Ошибка start: {e}")
        bot.send_message(m.chat.id, "❌ Произошла ошибка")

# ---------- PROFILE ----------

@bot.message_handler(func=lambda m: m.text == "👤 Профиль" or m.text == "/profile")
def profile(m):
    """Показать профиль"""
    try:
        u = get_user(m.from_user.id, m.from_user.username)
        if not u:
            bot.send_message(m.chat.id, "❌ Ошибка получения данных")
            return

        now = int(time.time())
        next_farm = u["farm"] + FARM_CD - now
        farm_status = f"✅ Доступно!" if next_farm <= 0 else f"⏳ Через {next_farm // 60} мин"
        
        txt = f"""
👤 <b>Профиль @{u['username']}</b>

💰 Баланс: <b>{fmt(u['balance'])} ICE</b>
⛏ Уровень фарма: <b>{u['level']}</b>
📈 Добыча за фарм: <b>{farm_amount(u['level'])} ICE</b>
⏫ Цена улучшения: <b>{upgrade_price(u['level'])} ICE</b>
🏆 Побед в батлах: <b>{u['wins']}</b>

⛏ Статус фарма: {farm_status}
"""
                bot.send_message(
            m.chat.id, 
            txt, 
            parse_mode="HTML",
            message_thread_id=m.message_thread_id # Добавлено
        )

    except Exception as e:
        logger.error(f"Ошибка profile: {e}")
        bot.send_message(m.chat.id, "❌ Произошла ошибка")

# ---------- FARM ----------

@bot.message_handler(func=lambda m: m.text == "⛏ Фарм" or m.text == "/farm")
def farm(m):
    """Добыча монет"""
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

# ---------- SEND ----------

@bot.message_handler(func=lambda m: m.text == "💸 Отправить")
def send_menu(m):
    """Меню отправки"""
    bot.send_message(
        m.chat.id,
        "💸 <b>Отправка ICE</b>\n\n"
        "Используйте команду:\n"
        "<code>/send ID СУММА</code>\n\n"
        "Пример: <code>/send 123456789 10</code>",
        parse_mode="HTML"
    )

@bot.message_handler(commands=["send"])
def send(m):
    """Отправка монет другому пользователю"""
    try:
        parts = m.text.split()
        if len(parts) != 3:
            bot.send_message(
                m.chat.id,
                "❌ Неверный формат!\nИспользуйте: <code>/send ID СУММА</code>",
                parse_mode="HTML"
            )
            return

        try:
            to_id = int(parts[1])
            amount = float(parts[2])
        except ValueError:
            bot.send_message(m.chat.id, "❌ ID и сумма должны быть числами!")
            return

        if amount <= 0:
            bot.send_message(m.chat.id, "❌ Сумма должна быть больше 0!")
            return

        if amount < 0.01:
            bot.send_message(m.chat.id, "❌ Минимальная сумма: 0.01 ICE")
            return

        u = get_user(m.from_user.id, m.from_user.username)
        if not u:
            bot.send_message(m.chat.id, "❌ Ошибка получения данных")
            return

        if u["_id"] == to_id:
            bot.send_message(m.chat.id, "❌ Нельзя отправить себе!")
            return

        if u["balance"] < amount:
            bot.send_message(
                m.chat.id,
                f"❌ Недостаточно средств!\nУ вас: <b>{fmt(u['balance'])} ICE</b>",
                parse_mode="HTML"
            )
            return

        # Проверка существования получателя
        recipient = users.find_one({"_id": to_id})
        if not recipient:
            bot.send_message(m.chat.id, "❌ Пользователь не найден!")
            return

        # Транзакция
        users.update_one({"_id": u["_id"]}, {"$inc": {"balance": -amount}})
        users.update_one({"_id": to_id}, {"$inc": {"balance": amount}})

        bot.send_message(
            m.chat.id,
            f"✅ Отправлено <b>{fmt(amount)} ICE</b> → @{recipient['username']}",
            parse_mode="HTML"
        )
        
        # Уведомление получателю
        try:
            bot.send_message(
                to_id,
                f"💰 Вам пришло <b>{fmt(amount)} ICE</b> от @{u['username']}",
                parse_mode="HTML"
            )
        except:
            pass  # Получатель мог заблокировать бота

    except Exception as e:
        logger.error(f"Ошибка send: {e}")
        bot.send_message(m.chat.id, "❌ Произошла ошибка при отправке")

# ---------- TOP ----------

@bot.message_handler(func=lambda m: m.text == "🏆 Топ" or m.text == "/top")
def top(m):
    """Топ игроков по балансу"""
    try:
        top_users = list(users.find().sort("balance", -1).limit(10))
        
        if not top_users:
            bot.send_message(m.chat.id, "📊 Топ пока пуст!", message_thread_id=m.message_thread_id)
            return

        txt = "🏆 <b>ТОП-10 ИГРОКОВ</b>\n\n"
        medals = ["🥇", "🥈", "🥉"]
        
        for i, u in enumerate(top_users, 1):
            medal = medals[i-1] if i <= 3 else f"{i}."
            txt += f"{medal} @{u['username']} — <b>{fmt(u['balance'])} ICE</b>\n"
        
        bot.send_message(m.chat.id, txt, parse_mode="HTML", message_thread_id=m.message_thread_id)
    except Exception as e:
        logger.error(f"Ошибка top: {e}")
        bot.send_message(m.chat.id, "❌ Произошла ошибка", message_thread_id=m.message_thread_id)
        

# ---------- BATTLE ----------

@bot.message_handler(func=lambda m: m.text == "⚔ Батл")
def battle_menu(m):
    """Меню батлов"""
    bot.send_message(
        m.chat.id,
        "⚔️ <b>БАТЛ</b>\n\n"
        "Чтобы начать батл, ответьте командой /battle на сообщение игрока\n\n"
        "Правила:\n"
        "• Оба игрока бросают кубик (1-6)\n"
        "• У кого больше — забирает ставку\n"
        "• При равенстве победитель определяется случайно",
        parse_mode="HTML"
    )

@bot.message_handler(commands=["battle"])
def battle(m):
    """Вызов на батл"""
    try:
        if not m.reply_to_message:
            bot.send_message(
                m.chat.id,
                "❌ Ответьте этой командой на сообщение игрока!"
            )
            return

        challenger = m.from_user
        opponent = m.reply_to_message.from_user

        if challenger.id == opponent.id:
            bot.send_message(m.chat.id, "❌ Нельзя вызвать самого себя!")
            return

        if opponent.is_bot:
            bot.send_message(m.chat.id, "❌ Нельзя вызвать бота!")
            return

        # Проверка существующих батлов
        existing = battles.find_one({
            "$or": [
                {"from": challenger.id, "status": {"$in": ["wait", "bet"]}},
                {"to": challenger.id, "status": {"$in": ["wait", "bet"]}}
            ]
        })
        
        if existing:
            bot.send_message(m.chat.id, "❌ У вас уже есть активный батл!")
            return

        # Создание батла
        battle_id = battles.insert_one({
            "chat": m.chat.id,
            "from": challenger.id,
            "from_username": challenger.username or f"user_{challenger.id}",
            "to": opponent.id,
            "to_username": opponent.username or f"user_{opponent.id}",
            "status": "wait",
            "created": int(time.time())
        }).inserted_id

        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ Принять", callback_data=f"accept_{battle_id}"),
            types.InlineKeyboardButton("❌ Отказать", callback_data=f"deny_{battle_id}")
        )

        bot.send_message(
            m.chat.id,
            f"⚔️ <b>ВЫЗОВ НА БАТЛ!</b>\n\n"
            f"@{challenger.username} вызывает @{opponent.username or opponent.id}",
            reply_markup=kb,
            parse_mode="HTML",
            message_thread_id=m.message_thread_id # Чтобы сообщение появилось в теме
        )

    except Exception as e:
        logger.error(f"Ошибка battle: {e}")
        bot.send_message(m.chat.id, "❌ Произошла ошибка")

@bot.callback_query_handler(func=lambda c: c.data.startswith("accept_"))
def accept_battle(c):
    """Принятие батла"""
    try:
        battle_id = c.data.split("_")[1]
        from bson.objectid import ObjectId
        
        b = battles.find_one({"_id": ObjectId(battle_id), "status": "wait"})
        
        if not b:
            bot.answer_callback_query(c.id, "❌ Батл уже не активен!")
            return

        if c.from_user.id != b["to"]:
            bot.answer_callback_query(c.id, "❌ Это не ваш батл!")
            return

        # Проверка балансов
        challenger = users.find_one({"_id": b["from"]})
        opponent = users.find_one({"_id": b["to"]})

        if not challenger or not opponent:
            bot.answer_callback_query(c.id, "❌ Ошибка данных игроков")
            battles.delete_one({"_id": b["_id"]})
            return

        battles.update_one({"_id": b["_id"]}, {"$set": {"status": "bet"}})

        kb = types.InlineKeyboardMarkup(row_width=3)
        bets = [1, 5, 10, 25, 50, 100]
        buttons = []
        
        for bet in bets:
            if challenger["balance"] >= bet and opponent["balance"] >= bet:
                buttons.append(
                    types.InlineKeyboardButton(
                        f"{bet} ICE",
                        callback_data=f"bet_{battle_id}_{bet}"
                    )
                )
        
        if not buttons:
            bot.edit_message_text(
                "❌ У одного из игроков недостаточно средств для минимальной ставки (1 ICE)",
                c.message.chat.id,
                c.message.message_id
            )
            battles.delete_one({"_id": b["_id"]})
            return

        kb.add(*buttons)

        bot.edit_message_text(
            f"✅ @{opponent['username']} принял вызов!\n\n"
            f"Выберите ставку:",
            c.message.chat.id,
            c.message.message_id,
            reply_markup=kb
        )
        bot.answer_callback_query(c.id, "✅ Вы приняли вызов!")

    except Exception as e:
        logger.error(f"Ошибка accept_battle: {e}")
        bot.answer_callback_query(c.id, "❌ Произошла ошибка")

@bot.callback_query_handler(func=lambda c: c.data.startswith("deny_"))
def deny_battle(c):
    """Отказ от батла"""
    try:
        battle_id = c.data.split("_")[1]
        from bson.objectid import ObjectId
        
        b = battles.find_one({"_id": ObjectId(battle_id)})
        
        if not b:
            bot.answer_callback_query(c.id, "❌ Батл уже не активен!")
            return

        if c.from_user.id != b["to"]:
            bot.answer_callback_query(c.id, "❌ Это не ваш батл!")
            return

        battles.delete_one({"_id": b["_id"]})
        
        bot.edit_message_text(
            f"❌ @{b['to_username']} отказался от батла",
            c.message.chat.id,
            c.message.message_id
        )
        bot.answer_callback_query(c.id, "Вы отказались от батла")

    except Exception as e:
        logger.error(f"Ошибка deny_battle: {e}")
        bot.answer_callback_query(c.id, "❌ Произошла ошибка")

@bot.callback_query_handler(func=lambda c: c.data.startswith("bet_"))
def place_bet(c):
    """Размещение ставки и проведение батла"""
    try:
        parts = c.data.split("_")
        battle_id = parts[1]
        bet = int(parts[2])
        
        from bson.objectid import ObjectId
        b = battles.find_one({"_id": ObjectId(battle_id), "status": "bet"})
        
        if not b:
            bot.answer_callback_query(c.id, "❌ Батл уже не активен!")
            return

        # Проверка что ставку делает участник
        if c.from_user.id not in [b["from"], b["to"]]:
            bot.answer_callback_query(c.id, "❌ Вы не участник этого батла!")
            return

        # Получение данных игроков
        challenger = users.find_one({"_id": b["from"]})
        opponent = users.find_one({"_id": b["to"]})

        if not challenger or not opponent:
            bot.answer_callback_query(c.id, "❌ Ошибка данных игроков")
            battles.delete_one({"_id": b["_id"]})
            return

        # Проверка балансов
        if challenger["balance"] < bet or opponent["balance"] < bet:
            bot.answer_callback_query(c.id, "❌ Недостаточно средств!")
            return

        # Проведение батла
        dice1 = random.randint(1, 6)
        dice2 = random.randint(1, 6)

        result_text = (
            f"⚔️ <b>БАТЛ!</b>\n\n"
            f"🎲 @{challenger['username']}: <b>{dice1}</b>\n"
            f"🎲 @{opponent['username']}: <b>{dice2}</b>\n\n"
        )

        # Определение победителя
        if dice1 > dice2:
            winner = challenger
            loser = opponent
        elif dice2 > dice1:
            winner = opponent
            loser = challenger
        else:
            # Ничья - случайный победитель
            winner, loser = random.choice([
                (challenger, opponent),
                (opponent, challenger)
            ])
            result_text += "🎲 Ничья! Победитель определен случайно\n\n"

        # Обновление балансов
        users.update_one({"_id": winner["_id"]}, {
            "$inc": {"balance": bet, "wins": 1}
        })
        users.update_one({"_id": loser["_id"]}, {
            "$inc": {"balance": -bet}
        })

        result_text += (
            f"🏆 Победитель: <b>@{winner['username']}</b>\n"
            f"💰 Выигрыш: <b>+{fmt(bet)} ICE</b>"
        )

        bot.edit_message_text(
            result_text,
            c.message.chat.id,
            c.message.message_id,
            parse_mode="HTML"
        )

        # Удаление батла
        battles.delete_one({"_id": b["_id"]})
        
        bot.answer_callback_query(c.id, "🎲 Батл завершен!")

    except Exception as e:
        logger.error(f"Ошибка place_bet: {e}")
        bot.answer_callback_query(c.id, "❌ Произошла ошибка")


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
    """Рассылка сообщения всем пользователям"""
    if m.from_user.username != ADMIN:
        return
    
    text = m.text.replace("/broadcast", "").strip()
    if not text:
        bot.send_message(m.chat.id, "Введите текст после команды /broadcast")
        return

    all_users = users.find({}, {"_id": 1})
    count = 0
    for user in all_users:
        try:
            bot.send_message(user["_id"], f"📢 <b>ОБЪЯВЛЕНИЕ:</b>\n\n{text}", parse_mode="HTML")
            count += 1
            time.sleep(0.05) # Защита от спам-фильтра Telegram
        except:
            continue
    
    bot.send_message(m.chat.id, f"✅ Рассылка завершена. Получили: {count} чел.")

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
                             
