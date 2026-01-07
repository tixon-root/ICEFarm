import os
import time
from flask import Flask, request
import telebot
from telebot import types
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN = os.getenv("ADMIN_USERNAME")
WEBHOOK = os.getenv("WEBHOOK_URL")

bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)

db = MongoClient(MONGO_URI)["icecoin"]
users = db.users
promos = db.promos
withdraws = db.withdraws

FARM_CD = 10800
DAILY_CD = 86400

# ---------------- UTILS ----------------

def get_user(uid, username):
    if not username:
        username = f"id{uid}"
    u = users.find_one({"_id": uid})
    if not u:
        users.insert_one({
            "_id": uid,
            "username": username,
            "balance": 0.0,
            "level": 1,
            "last_farm": 0,
            "last_daily": 0,
            "promo_used": [],
            "battles_win": 0,
            "battles_lose": 0,
            "banned": False
        })
        return get_user(uid, username)
    if u.get("username") != username:
        users.update_one({"_id": uid}, {"$set": {"username": username}})
    return u

def farm_income(level):
    return round(0.4 * level, 2)

def upgrade_price(level):
    return level * 2

# ---------------- MENU ----------------

def main_menu():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("⛏ FARM", callback_data="farm"),
        types.InlineKeyboardButton("🎁 DAILY", callback_data="daily"),
        types.InlineKeyboardButton("⏫ UPGRADE", callback_data="upgrade"),
        types.InlineKeyboardButton("⚔ BATTLES", callback_data="battles"),
        types.InlineKeyboardButton("🏆 TOP", callback_data="top"),
        types.InlineKeyboardButton("📤 SEND", callback_data="send"),
        types.InlineKeyboardButton("📥 WITHDRAW", callback_data="withdraw")
    )
    return kb

# ---------------- START ----------------

@bot.message_handler(commands=["start"])
def start(m):
    u = get_user(m.from_user.id, m.from_user.username)
    text = f"""
🧊 ICECOIN FARM

👤 ID: {u['_id']}
💰 Balance: {u['balance']} ICE
⚙ Level: {u['level']}
⛏ Income: {farm_income(u['level'])} ICE / 3h
"""
    bot.send_message(m.chat.id, text, reply_markup=main_menu())

# ---------------- CALLBACKS ----------------

@bot.callback_query_handler(func=lambda c: True)
def callbacks(c):
    uid = c.from_user.id
    u = get_user(uid, c.from_user.username)

    if c.data == "farm":
        do_farm(uid, c.message)
    elif c.data == "daily":
        do_daily(uid, c.message)
    elif c.data == "upgrade":
        do_upgrade(uid, c.message)
    elif c.data == "top":
        send_top(c.message)
    elif c.data == "withdraw":
        do_withdraw(c.message)
    elif c.data == "send":
        bot.send_message(c.message.chat.id, "📤 Use: /send USERID AMOUNT")
    elif c.data == "battles":
        bot.send_message(c.message.chat.id, "⚔ Use /battles AMOUNT (reply to user)")

# ---------------- FARM ----------------

def do_farm(uid, msg):
    u = users.find_one({"_id": uid})
    now = int(time.time())
    if now - u["last_farm"] < FARM_CD:
        left = FARM_CD - (now - u["last_farm"])
        bot.send_message(msg.chat.id, f"⏳ Wait {left//60} min")
        return
    reward = farm_income(u["level"])
    users.update_one({"_id": uid}, {"$set": {"last_farm": now}, "$inc": {"balance": reward}})
    bot.send_message(msg.chat.id, f"⛏ +{reward} ICE")

# ---------------- DAILY ----------------

def do_daily(uid, msg):
    u = users.find_one({"_id": uid})
    now = int(time.time())
    if now - u["last_daily"] < DAILY_CD:
        bot.send_message(msg.chat.id, "⏳ Daily already claimed")
        return
    reward = round(1 + 0.2 * u["level"], 2)
    users.update_one({"_id": uid}, {"$set": {"last_daily": now}, "$inc": {"balance": reward}})
    bot.send_message(msg.chat.id, f"🎁 You got {reward} ICE")

# ---------------- UPGRADE ----------------

def do_upgrade(uid, msg):
    u = users.find_one({"_id": uid})
    price = upgrade_price(u["level"])
    if u["balance"] < price:
        bot.send_message(msg.chat.id, f"❌ Need {price} ICE")
        return
    users.update_one({"_id": uid}, {"$inc": {"balance": -price, "level": 1}})
    bot.send_message(msg.chat.id, "⏫ Level upgraded!")

# ---------------- SEND ----------------

@bot.message_handler(commands=["send"])
def send_ice(m):
    try:
        _, uid, amount = m.text.split()
        uid = int(uid)
        amount = float(amount)
    except:
        bot.reply_to(m, "Usage: /send USERID AMOUNT")
        return

    if amount < 1:
        bot.reply_to(m, "Minimum 1 ICE")
        return

    sender = get_user(m.from_user.id, m.from_user.username)
    if sender["balance"] < amount:
        bot.reply_to(m, "Not enough ICE")
        return

    fee = round(amount * 0.05, 2)
    receive = amount - fee

    users.update_one({"_id": sender["_id"]}, {"$inc": {"balance": -amount}})
    users.update_one({"_id": uid}, {"$inc": {"balance": receive}})

    bot.reply_to(m, f"📤 Sent {receive} ICE (fee {fee})")

# ---------------- WITHDRAW ----------------

def do_withdraw(msg):
    u = get_user(msg.from_user.id, msg.from_user.username)
    if u["balance"] < 25:
        bot.send_message(msg.chat.id, "❌ Minimum withdraw 25 ICE")
        return
    withdraws.insert_one({"user_id": u["_id"], "amount": u["balance"], "time": int(time.time())})
    bot.send_message(msg.chat.id, "📨 Write to @herozvz to withdraw")

# ---------------- TOP ----------------

def send_top(msg):
    top = users.find().sort("balance", -1).limit(10)
    text = "🏆 TOP ICECOIN\n\n"
    i = 1
    for u in top:
        name = u.get("username") or f"id{u['_id']}"
        text += f"{i}. @{name} — {round(u['balance'],2)} ICE\n"
        i += 1
    bot.send_message(msg.chat.id, text)

# ---------------- BATTLES ----------------

@bot.message_handler(commands=["battles"])
def battles(m):
    if not m.reply_to_message:
        bot.reply_to(m, "Reply to user")
        return
    try:
        bet = float(m.text.split()[1])
    except:
        bot.reply_to(m, "Usage: /battles 5")
        return

    u1 = get_user(m.from_user.id, m.from_user.username)
    u2 = get_user(m.reply_to_message.from_user.id, m.reply_to_message.from_user.username)

    if u1["balance"] < bet or u2["balance"] < bet:
        bot.reply_to(m, "Not enough ICE")
        return

    users.update_one({"_id": u1["_id"]}, {"$inc": {"balance": -bet}})
    users.update_one({"_id": u2["_id"]}, {"$inc": {"balance": -bet}})

    d1 = bot.send_dice(m.chat.id).dice.value
    d2 = bot.send_dice(m.chat.id).dice.value

    win = round(bet * 2 * 0.9, 2)

    if d1 > d2:
        users.update_one({"_id": u1["_id"]}, {"$inc": {"balance": win, "battles_win": 1}})
        users.update_one({"_id": u2["_id"]}, {"$inc": {"battles_lose": 1}})
        bot.send_message(m.chat.id, f"🏆 @{u1['username']} wins {win} ICE")
    else:
        users.update_one({"_id": u2["_id"]}, {"$inc": {"balance": win, "battles_win": 1}})
        users.update_one({"_id": u1["_id"]}, {"$inc": {"battles_lose": 1}})
        bot.send_message(m.chat.id, f"🏆 @{u2['username']} wins {win} ICE")

# ---------------- PROMO ----------------

@bot.message_handler(commands=["promo"])
def promo(m):
    u = get_user(m.from_user.id, m.from_user.username)
    try:
        code = m.text.split()[1]
    except:
        return
    p = promos.find_one({"code": code})
    if not p or p["uses_left"] <= 0:
        bot.reply_to(m, "Invalid promo")
        return
    if code in u["promo_used"]:
        bot.reply_to(m, "Already used")
        return

    users.update_one({"_id": u["_id"]}, {"$inc": {"balance": p["amount"]}, "$push": {"promo_used": code}})
    promos.update_one({"code": code}, {"$inc": {"uses_left": -1}})
    bot.reply_to(m, f"🎁 +{p['amount']} ICE")

# ---------------- ADMIN ----------------

@bot.message_handler(commands=["admin"])
def admin(m):
    if m.from_user.username != ADMIN: return
    bot.send_message(m.chat.id,
"""
🛠 ADMIN
/addpromo CODE AMOUNT USES
/give ID AMOUNT
/take ID AMOUNT
/reset ID
/stats
""")

@bot.message_handler(commands=["addpromo"])
def addpromo(m):
    if m.from_user.username != ADMIN: return
    _, code, amount, uses = m.text.split()
    promos.insert_one({"code": code, "amount": float(amount), "uses_left": int(uses)})
    bot.reply_to(m, "Promo added")

@bot.message_handler(commands=["give"])
def give(m):
    if m.from_user.username != ADMIN: return
    _, uid, amount = m.text.split()
    users.update_one({"_id": int(uid)}, {"$inc": {"balance": float(amount)}})
    bot.reply_to(m, "Done")

@bot.message_handler(commands=["take"])
def take(m):
    if m.from_user.username != ADMIN: return
    _, uid, amount = m.text.split()
    users.update_one({"_id": int(uid)}, {"$inc": {"balance": -float(amount)}})
    bot.reply_to(m, "Done")

@bot.message_handler(commands=["stats"])
def stats(m):
    if m.from_user.username != ADMIN: return
    total_users = users.count_documents({})
    total_ice = sum(u["balance"] for u in users.find())
    bot.reply_to(m, f"👥 {total_users} users\n💰 {round(total_ice,2)} ICE in system")

# ---------------- WEBHOOK ----------------

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    bot.process_new_updates([telebot.types.Update.de_json(request.stream.read().decode("utf-8"))])
    return "OK"

@app.route("/")
def index():
    return "ICECOIN BOT RUNNING"

bot.remove_webhook()
bot.set_webhook(url=f"{WEBHOOK}/{TOKEN}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
