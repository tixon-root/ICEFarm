import os
import time
from flask import Flask, request
import telebot
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
ADMIN = os.getenv("ADMIN_USERNAME")
WEBHOOK = os.getenv("WEBHOOK_URL")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

db = MongoClient(MONGO_URI)["icecoin"]
users = db.users
promos = db.promos
withdraws = db.withdraws

FARM_CD = 10800

# ---------------- UTILS ----------------

def get_user(uid, username):
    u = users.find_one({"_id": uid})
    if not u:
        users.insert_one({
            "_id": uid,
            "username": username,
            "balance": 0,
            "level": 1,
            "last_farm": 0,
            "promo_used": []
        })
        return get_user(uid, username)
    return u

def farm_income(level):
    return round(0.4 * level, 2)

def upgrade_price(level):
    return level * (level + 1)

# ---------------- START ----------------

@bot.message_handler(commands=["start"])
def start(m):
    u = get_user(m.from_user.id, m.from_user.username)
    bot.send_message(m.chat.id,
f"""
🧊 ICECOIN FARM

👤 ID: {u['_id']}
💰 {u['balance']} ICE
⚙ Level: {u['level']}
⛏ {farm_income(u['level'])} ICE / 3h
""")

# ---------------- FARM ----------------

@bot.message_handler(commands=["farm"])
def farm(m):
    u = get_user(m.from_user.id, m.from_user.username)
    now = int(time.time())

    if now - u["last_farm"] < FARM_CD:
        bot.reply_to(m, "⏳ Not ready")
        return

    reward = farm_income(u["level"])
    users.update_one({"_id": u["_id"]},
                     {"$set": {"last_farm": now}, "$inc": {"balance": reward}})
    bot.reply_to(m, f"⛏ +{reward} ICE")

# ---------------- UPGRADE ----------------

@bot.message_handler(commands=["upgrade"])
def upgrade(m):
    u = get_user(m.from_user.id, m.from_user.username)
    price = upgrade_price(u["level"])

    if u["balance"] < price:
        bot.reply_to(m, f"Need {price} ICE")
        return

    users.update_one({"_id": u["_id"]},
                     {"$inc": {"balance": -price, "level": 1}})
    bot.reply_to(m, "⏫ Level up")

# ---------------- PROMO ----------------

@bot.message_handler(commands=["promo"])
def promo(m):
    u = get_user(m.from_user.id, m.from_user.username)
    try:
        code = m.text.split()[1]
    except:
        return

    p = promos.find_one({"code": code})
    if not p:
        bot.reply_to(m, "Invalid promo")
        return

    if code in u["promo_used"]:
        bot.reply_to(m, "Already used")
        return

    users.update_one({"_id": u["_id"]},
                     {"$inc": {"balance": p["amount"]},
                      "$push": {"promo_used": code}})
    bot.reply_to(m, f"🎁 +{p['amount']} ICE")

# ---------------- ADMIN PANEL ----------------

@bot.message_handler(commands=["admin"])
def admin(m):
    if m.from_user.username != ADMIN:
        return
    bot.send_message(m.chat.id,
"""
🛠 ADMIN
/addpromo CODE AMOUNT
/give ID AMOUNT
/reset ID
""")

@bot.message_handler(commands=["addpromo"])
def addpromo(m):
    if m.from_user.username != ADMIN: return
    _, code, amount = m.text.split()
    promos.insert_one({"code": code, "amount": float(amount)})
    bot.reply_to(m, "Promo added")

@bot.message_handler(commands=["give"])
def give(m):
    if m.from_user.username != ADMIN: return
    _, uid, amount = m.text.split()
    users.update_one({"_id": int(uid)}, {"$inc": {"balance": float(amount)}})
    bot.reply_to(m, "Done")

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
