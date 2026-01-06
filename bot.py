import os
import random
import threading
from datetime import datetime, timedelta
from flask import Flask
from telebot import TeleBot, types
from pymongo import MongoClient

# --- НАСТРОЙКИ ---
TOKEN = os.getenv("BOT_TOKEN")  # Берем из настроек Render
MONGO_URI = os.getenv("MONGO_URI") # Берем из настроек Render
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

bot = TeleBot(TOKEN)
client = MongoClient(MONGO_URI)
db = client['icecoin_db'] 
users = db.ice_players  # Новое имя для юзеров ICECOIN
stats = db.ice_economy  # Новое имя для статистики ICECOIN
# Инициализация глобальной статистики
if not stats.find_one({"_id": "economy"}):
    stats.insert_one({
        "_id": "economy", 
        "total_mined": 0.0, 
        "total_burned": 0.0, 
        "total_withdrawn": 0.0
    })

# Стоимость улучшений
UPGRADE_COSTS = {1: 0, 2: 30, 3: 90, 4: 250, 5: 700, 6: 2000, 7: 6000}
COOLDOWN_HOURS = 3

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def get_user_data(user_id):
    user = users.find_one({"_id": user_id})
    if not user:
        user = {
            "_id": user_id,
            "balance": 0.0,
            "farm_level": 1,
            "last_farm": datetime.min,
            "total_farmed": 0.0
        }
        users.insert_one(user)
    return user

def update_economy(mined=0, burned=0, withdrawn=0):
    stats.update_one({"_id": "economy"}, {
        "$inc": {
            "total_mined": mined,
            "total_burned": burned,
            "total_withdrawn": withdrawn
        }
    })

def get_rate():
    s = stats.find_one({"_id": "economy"})
    if s['total_mined'] <= 0: return 0.1
    return ((s['total_burned'] + s['total_withdrawn']) / s['total_mined']) * 100

# --- КОМАНДЫ ---

@bot.message_handler(commands=['start', 'help'])
def help_cmd(message):
    text = (
        "❄️ **ICECOIN: Ледяная Ферма**\n\n"
        "⛏ /farm — Добыть ICE (раз в 3ч)\n"
        "⏫ /upgrade — Улучшить ферму\n"
        "⚔️ /battles <сумма> — Ставки (50/50)\n"
        "🎁 /gift <сумма> — Подарить (ответом на сообщ.)\n"
        "🏆 /top — Список богачей\n"
        "🌍 /pool — Экономика мира\n"
        "📤 /withdraw <сумма> — Вывод (только в ЛС)"
    )
    bot.reply_to(message, text, parse_mode='Markdown')

@bot.message_handler(commands=['farm'])
def farm(message):
    user = get_user_data(message.from_user.id)
    now = datetime.now()
    
    if user['last_farm'] + timedelta(hours=COOLDOWN_HOURS) > now:
        diff = (user['last_farm'] + timedelta(hours=COOLDOWN_HOURS)) - now
        mins = int(diff.total_seconds() / 60)
        return bot.reply_to(message, f"⌛️ Лёд еще не застыл! Жди {mins} мин.")

    # Формула: 0.3 * (2 ^ (level - 1))
    amount = 0.3 * (2 ** (user['farm_level'] - 1))
    
    users.update_one({"_id": user['_id']}, {
        "$inc": {"balance": amount, "total_farmed": amount},
        "$set": {"last_farm": now}
    })
    update_economy(mined=amount)
    
    bot.reply_to(message, f"⛏ Ты добыл **{amount:.2f} ICE**!\nПриходи через {COOLDOWN_HOURS} часа.")

@bot.message_handler(commands=['upgrade'])
def upgrade(message):
    user = get_user_data(message.from_user.id)
    lv = user['farm_level']
    
    if lv >= 7:
        return bot.reply_to(message, "❄️ У тебя максимальный уровень!")
    
    cost = UPGRADE_COSTS[lv + 1]
    if user['balance'] < cost:
        return bot.reply_to(message, f"❌ Нужно {cost} ICE для LVL {lv + 1}.")
    
    burn = cost * 0.20
    users.update_one({"_id": user['_id']}, {
        "$inc": {"balance": -cost, "farm_level": 1}
    })
    update_economy(burned=burn)
    bot.reply_to(message, f"⏫ Уровень повышен до **{lv + 1}**!\nСгорело в фонд: {burn:.2f} ICE.")

@bot.message_handler(commands=['battles'])
def battles(message):
    try:
        bet = float(message.text.split()[1])
        if bet <= 0: raise ValueError
    except:
        return bot.reply_to(message, "Использование: `/battles 10`", parse_mode='Markdown')
    
    user = get_user_data(message.from_user.id)
    if user['balance'] < bet:
        return bot.reply_to(message, "❌ Недостаточно ICE.")
    
    fee = bet * 0.10
    update_economy(burned=fee)
    
    if random.random() < 0.5:
        users.update_one({"_id": user['_id']}, {"$inc": {"balance": bet - fee}})
        bot.reply_to(message, f"⚔️ **Победа!** Чистый плюс: {bet-fee:.2f} ICE.\n(10% налог ушел в пул)")
    else:
        users.update_one({"_id": user['_id']}, {"$inc": {"balance": -bet}})
        bot.reply_to(message, f"💀 **Проигрыш!** Ты потерял {bet} ICE.")

@bot.message_handler(commands=['gift'])
def gift(message):
    if not message.reply_to_message:
        return bot.reply_to(message, "🎁 Ответь на сообщение того, кому хочешь подарить.")
    
    try:
        amount = float(message.text.split()[1])
        if amount <= 0: raise ValueError
    except:
        return bot.reply_to(message, "Использование: `/gift 10` (ответом)")
    
    sender = get_user_data(message.from_user.id)
    target_id = message.reply_to_message.from_user.id
    
    if sender['_id'] == target_id:
        return bot.reply_to(message, "❌ Нельзя дарить самому себе.")
    
    if sender['balance'] < amount:
        return bot.reply_to(message, "❌ Баланса не хватает.")
    
    fee = amount * 0.10
    net = amount - fee
    
    users.update_one({"_id": sender['_id']}, {"$inc": {"balance": -amount}})
    users.update_one({"_id": target_id}, {"$inc": {"balance": net}}, upsert=True)
    update_economy(burned=fee)
    
    bot.reply_to(message, f"🎁 Передано {net:.2f} ICE!\n(Комиссия 10% сожжена)")

@bot.message_handler(commands=['pool'])
def pool(message):
    s = stats.find_one({"_id": "economy"})
    rate = get_rate()
    text = (
        f"🌍 **ГЛОБАЛЬНЫЙ ПУЛ**\n\n"
        f"💎 Всего добыто: {s['total_mined']:.2f}\n"
        f"🔥 Всего сожжено: {s['total_burned']:.2f}\n"
        f"📤 Выведено: {s['total_withdrawn']:.2f}\n\n"
        f"💹 **Курс: 1 ICE = {rate:.2f} Gold**"
    )
    bot.reply_to(message, text, parse_mode='Markdown')

@bot.message_handler(commands=['top'])
def top(message):
    leaders = users.find().sort("balance", -1).limit(10)
    text = "🏆 **ТОП-10 ИГРОКОВ ICECOIN**\n\n"
    for i, u in enumerate(leaders, 1):
        text += f"{i}. {u['balance']:.2f} ICE\n"
    bot.send_message(message.chat.id, text, parse_mode='Markdown')

@bot.message_handler(commands=['withdraw'])
def withdraw(message):
    if message.chat.type != "private":
        return bot.reply_to(message, "❌ Вывод только в личных сообщениях с ботом!")
    
    try:
        amount = float(message.text.split()[1])
        if amount < 25: return bot.reply_to(message, "❌ Минимум 25 ICE.")
    except:
        return bot.reply_to(message, "Использование: `/withdraw 100`")
    
    user = get_user_data(message.from_user.id)
    if user['balance'] < amount:
        return bot.reply_to(message, "❌ Недостаточно средств.")
    
    rate = get_rate()
    gold = amount * rate
    
    users.update_one({"_id": user['_id']}, {"$inc": {"balance": -amount}})
    update_economy(withdrawn=amount)
    
    # Уведомление админу
    admin_text = (
        f"🚨 **ЗАЯВКА НА ВЫВОД**\n"
        f"Юзер: {message.from_user.first_name} (@{message.from_user.username})\n"
        f"ID: {user['_id']}\n"
        f"Сумма: {amount} ICE\n"
        f"Золото Rucoy: {gold:.2f}"
    )
    if ADMIN_ID != 0:
        bot.send_message(ADMIN_ID, admin_text)
    
    bot.reply_to(message, f"✅ Заявка на {amount} ICE принята! Ожидайте выплаты золота.")

# --- ВЕБ-СЕРВЕР ДЛЯ RENDER ---
app = Flask('')

@app.route('/')
def home():
    return "ICECOIN IS ALIVE"

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))

# --- ЗАПУСК ---
if __name__ == "__main__":
    # Запускаем Flask в отдельном потоке
    threading.Thread(target=run_flask).start()
    # Запускаем бота
    print("Бот запущен...")
    bot.infinity_polling()
                      
