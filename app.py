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

# ---------- CONSTANTS ----------
FARM_CD = 10800
CHANNEL_ID = "@BANCUS_RUCOY"
FEE = 0.1
MIN_WITHDRAW = 30.0
FEE_GOLD = 3.0
FEE_BOT_TRANSFER = 1.0
ADMIN_ID = 6395348885

# ================================================================
# НОВОЕ: КОНСТАНТЫ ДЛЯ ЛИГИ, СЖИГАНИЯ, КРАФТА
# ================================================================

# --- Лиги (RP) ---
RP_WIN  = 15
RP_LOSS = -8

LEAGUES = [
    (0,    "🥉 Бронза",  "bronze"),
    (50,   "🥈 Серебро", "silver"),
    (150,  "🥇 Золото",  "gold"),
    (300,  "💎 Алмаз",   "diamond"),
    (600,  "👑 Мастер",  "master"),
    (1000, "🌟 Легенда", "legend"),
]

def get_league(rp: int):
    league_name, league_key = "🥉 Бронза", "bronze"
    for threshold, name, key in LEAGUES:
        if rp >= threshold:
            league_name, league_key = name, key
    return league_name, league_key

# --- Ранги сжигания ---
BURN_RANKS = {
    0:    ("🧊 Лёд",     ""),
    100:  ("🔥 Горящий", "🔥"),
    500:  ("💀 Пепел",   "💀"),
    1000: ("☄️ Метеор",  "☄️"),
    5000: ("🌋 Вулкан",  "🌋"),
}

def get_burn_rank(total_burned: float):
    rank_name, rank_emoji = "🧊 Лёд", ""
    for threshold in sorted(BURN_RANKS):
        if total_burned >= threshold:
            rank_name, rank_emoji = BURN_RANKS[threshold]
    return rank_name, rank_emoji

# --- Крафт рецепты ---
CRAFT_RECIPES = {
    "Ледяной Меч": {
        "ingredients": ["Ледяной Осколок", "Ледяной Осколок"],
        "chance": 0.7,
        "desc": "Меч, выкованный из двух ледяных осколков.",
        "rarity": "rare"
    },
    "Кристалл Бури": {
        "ingredients": ["Ледяной Меч", "Огненный Камень"],
        "chance": 0.4,
        "desc": "Мощный кристалл, соединяющий лёд и огонь.",
        "rarity": "epic"
    },
    "Корона Зимы": {
        "ingredients": ["Кристалл Бури", "Кристалл Бури"],
        "chance": 0.2,
        "desc": "Легендарная корона, символ власти над льдом.",
        "rarity": "legendary"
    },
}

RARITY_EMOJI = {
    "rare":      "🔵",
    "epic":      "🟣",
    "legendary": "🟡",
}

# ================================================================

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
    users.create_index("balance")

    logger.info("База данных подключена успешно")
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
                "inventory": [],
                "wins": 0,
                "rp": 0,            # НОВОЕ: рейтинговые очки
                "total_burned": 0.0 # НОВОЕ: всего сожжено
            }
            users.insert_one(u)
        else:
            if first_name and u.get("first_name") != first_name:
                users.update_one({"_id": uid}, {"$set": {"first_name": first_name}})
                u["first_name"] = first_name
        return u
    except Exception as e:
        logger.error(f"Ошибка get_user: {e}")
        return None

def farm_amount(level):
    return round((level * 0.5) + random.uniform(0.1, 1.0), 1)

def upgrade_price(level):
    return round(1 + level * 0.8, 2)

def fmt(x):
    try:
        val = float(x)
        return "{:,.2f}".format(val).replace(",", " ").replace(".00", "")
    except:
        return str(x)

def is_subscribed(m):
    try:
        status = bot.get_chat_member(CHANNEL_ID, m.from_user.id).status
        if status in ["member", "administrator", "creator"]:
            return True
    except Exception as e:
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
    """Главное меню — оригинал + 3 новые кнопки"""
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("🏅 Достижения")
    kb.add("⛏ Фарм", "⏫ Улучшить")
    kb.add("🏆 Топ", "💸 Отправить")
    kb.add("👤 Профиль", "🎒 Инвентарь")
    kb.add("👥 Рефералы")
    kb.add("⚗️ Крафт", "🔥 Сжечь ICE")  # НОВОЕ
    kb.add("⚔️ Моя лига")               # НОВОЕ
    return kb

# ---------- WEBHOOK ----------

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
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
    return jsonify({
        "status": "online",
        "bot": "ICECOIN",
        "version": "2.1"
    })

@app.route("/set_webhook")
def set_webhook():
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
    try:
        if m.chat.type != "private":
            return

        uid = m.from_user.id
        ref_id = None

        if len(m.text.split()) > 1:
            payload = m.text.split()[1]
            if payload.startswith("ref_"):
                try:
                    ref_id = int(payload.replace("ref_", ""))
                except:
                    ref_id = None

        is_new_user = users.find_one({"_id": uid}) is None

        u = get_user(uid, m.from_user.username, m.from_user.first_name)
        if not u:
            bot.send_message(m.chat.id, "❌ Ошибка получения данных")
            return

        if is_new_user and ref_id and ref_id != uid:
            referrer = users.find_one({"_id": ref_id})
            if referrer:
                is_vip = referrer.get("is_vip", False)
                bonus = 15 if is_vip else 10
                users.update_one({"_id": ref_id}, {"$inc": {"balance": bonus}})
                users.update_one({"_id": uid}, {"$set": {"referrer": ref_id}})
                try:
                    bot.send_message(ref_id, f"💎 У вас новый реферал! Вам начислено <b>+{bonus} ICE</b>", parse_mode="HTML")
                except:
                    pass

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
        bot.send_message(
            m.chat.id,
            txt,
            reply_markup=create_main_keyboard(),
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Ошибка start: {e}")
        bot.send_message(m.chat.id, "❌ Произошла ошибка")

@bot.message_handler(commands=["fix_db"])
def fix_database(m):
    if m.from_user.id != 6395348885: return

    count = 0
    for user in users.find():
        try:
            old_balance = user.get("balance", 0)
            new_balance = float(str(old_balance).replace(",", "."))
            users.update_one(
                {"_id": user["_id"]},
                {"$set": {"balance": new_balance}}
            )
            count += 1
        except:
            continue

    bot.reply_to(m, f"✅ База исправлена! Перенастроено {count} профилей. Теперь ТОП будет работать верно.")

# ---------- PROFILE ----------

@bot.message_handler(func=lambda m: m.text in ["👤 Профиль", "/profile"])
def profile(m):
    try:
        t_id = getattr(m, 'message_thread_id', None)
        u = get_user(m.from_user.id, m.from_user.username)

        my_achs = u.get("achievements", [])
        mythic = u.get("mythic_achs", [])

        icons = []
        if 'ACHIEVEMENTS' in globals():
            icons = [ACHIEVEMENTS[a]["name"].split()[0] for a in my_achs if a in ACHIEVEMENTS]

        m_icons = [ma["name"].split()[0] for ma in mythic if isinstance(ma, dict) and "name" in ma]
        achs_line = " ".join(icons + m_icons) if (icons or m_icons) else "Нет"

        now = int(time.time())
        next_farm = u.get("farm", 0) + FARM_CD - now
        farm_status = "✅ Доступен" if next_farm <= 0 else f"⏳ {next_farm // 60} мин"

        is_vip = u.get("is_vip", False)
        status_emoji = u.get("vip_emoji", "👤") if is_vip else "👤"

        # НОВОЕ: ранг сжигания и лига
        burned = u.get("total_burned", 0.0)
        burn_rank, burn_emoji = get_burn_rank(burned)
        rp = u.get("rp", 0)
        league_name, _ = get_league(rp)

        txt = (
            f"╔═ {status_emoji} <b>ПРОФИЛЬ ИГРОКА</b> ═╗\n"
            f"┃ <b>Юзер:</b> @{u['username']}\n"
            f"┣━━━━━━━━━━━━━━━━━━\n"
            f"┃ 💰 <b>Баланс:</b>    <code>{fmt(u['balance'])} ICE</code>\n"
            f"┃ ⛏ <b>Уровень:</b>    <code>{u['level']}</code>\n"
            f"┃ 📈 <b>Доход:</b>      <code>{farm_amount(u['level'])} ICE</code>\n"
            f"┃ ⏫ <b>Апгрейд:</b>    <code>{upgrade_price(u['level'])} ICE</code>\n"
            f"┃ 🏆 <b>Победы:</b>    <code>{u.get('wins', 0)}</code>\n"
            f"┃ ⚔️ <b>Лига:</b>      <code>{league_name} ({rp} RP)</code>\n"
            f"┃ 🔥 <b>Сожжено:</b>   <code>{fmt(burned)} ICE</code> {burn_emoji}\n"
            f"┣━━━━━━━━━━━━━━━━━━\n"
            f"┃ ⛏ <b>Майнинг:</b>    {farm_status}\n"
            f"╚══════════════════╝"
        )

        bg = u.get("vip_background")
        if is_vip and bg:
            if u.get("vip_type") == "photo":
                bot.send_photo(m.chat.id, bg, caption=txt, parse_mode="HTML", message_thread_id=t_id)
            else:
                bot.send_animation(m.chat.id, bg, caption=txt, parse_mode="HTML", message_thread_id=t_id)
        else:
            bot.send_message(m.chat.id, txt, parse_mode="HTML", message_thread_id=t_id)

    except Exception as e:
        logger.error(f"Ошибка профиля: {e}")
        t_id = getattr(m, 'message_thread_id', None)
        bot.send_message(m.chat.id, "❌ Ошибка при генерации профиля.", message_thread_id=t_id)

# ---------- FARM ----------

@bot.message_handler(func=lambda m: m.text == "⛏ Фарм" or m.text == "/farm")
def farm(m):
    if not is_subscribed(m): return

    try:
        u = get_user(m.from_user.id, m.from_user.username)
        if not u:
            bot.send_message(m.chat.id, "❌ Ошибка получения данных", message_thread_id=m.message_thread_id)
            return

        now = int(time.time())
        last_farm_time = u.get("farm", 0)
        time_passed = now - last_farm_time

        if time_passed < FARM_CD:
            wait = FARM_CD - time_passed
            hours = wait // 3600
            minutes = (wait % 3600) // 60
            bot.send_message(
                m.chat.id,
                f"⏳ Следующий фарм через: <b>{hours}ч {minutes}м</b>",
                parse_mode="HTML",
                message_thread_id=m.message_thread_id
            )
            return

        gain = farm_amount(u["level"])

        if u.get("is_vip", False):
            gain += 0.5
            vip_text = "✨ (VIP Бонус +0.5)"
        else:
            vip_text = ""

        current_balance = u.get("balance", 0.0)
        final_balance = current_balance + gain

        users.update_one(
            {"_id": u["_id"]},
            {"$set": {"farm": now, "balance": final_balance}}
        )

        bot.send_message(
            m.chat.id,
            f"❄️ Вы добыли <b>{gain} ICE</b> {vip_text}\n💰 Баланс: <b>{fmt(final_balance)} ICE</b>",
            parse_mode="HTML",
            message_thread_id=m.message_thread_id
        )

    except Exception as e:
        logger.error(f"Ошибка farm: {e}")
        bot.send_message(m.chat.id, "❌ Произошла ошибка", message_thread_id=m.message_thread_id)

# ---------- UPGRADE ----------

@bot.message_handler(func=lambda m: m.text == "⏫ Улучшить" or m.text == "/upgrade")
def upgrade(m):
    try:
        u = get_user(m.from_user.id, m.from_user.username)
        if not u: return

        price = upgrade_price(u["level"])
        current_balance = float(u.get("balance", 0))

        if current_balance < price:
            bot.send_message(
                m.chat.id,
                f"❌ Недостаточно средств!\nНужно: <b>{price} ICE</b>\nУ вас: <b>{fmt(current_balance)} ICE</b>",
                parse_mode="HTML", message_thread_id=getattr(m, 'message_thread_id', None)
            )
            return

        new_level = u["level"] + 1
        new_balance = round(current_balance - price, 2)
        new_farm_amount = farm_amount(new_level)

        users.update_one({"_id": u["_id"]}, {"$set": {"balance": new_balance, "level": new_level}})

        bot.send_message(
            m.chat.id,
            f"✅ <b>Уровень фарма повышен!</b>\n\n"
            f"⛏ Новый уровень: <b>{new_level}</b>\n"
            f"📈 Добыча за фарм: <b>{new_farm_amount} ICE</b>\n"
            f"💰 Остаток: <b>{fmt(new_balance)} ICE</b>",
            parse_mode="HTML", message_thread_id=getattr(m, 'message_thread_id', None)
        )
    except Exception as e:
        logger.error(f"Ошибка upgrade: {e}")
        bot.send_message(m.chat.id, "❌ Произошла ошибка", message_thread_id=getattr(m, 'message_thread_id', None))

# ---------- INVENTORY ----------

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
            rarity_icon = RARITY_EMOJI.get(item.get("rarity", ""), "🖼")
            kb.add(types.InlineKeyboardButton(f"{rarity_icon} {item['name']}", callback_data=f"view_nft_{i}"))

        bot.send_message(m.chat.id, "🎒 <b>Твой инвентарь:</b>", reply_markup=kb, parse_mode="HTML", message_thread_id=t_id)
    except Exception as e:
        logger.error(f"Ошибка инвентаря: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("view_nft_"))
def view_nft_callback(c):
    try:
        t_id = getattr(c.message, 'message_thread_id', None)
        u = users.find_one({"_id": c.from_user.id})
        index = int(c.data.split("_")[2])
        inv = u.get("inventory", [])

        if index < len(inv):
            nft = inv[index]
            rarity_icon = RARITY_EMOJI.get(nft.get("rarity", ""), "🖼")
            text = f"{rarity_icon} NFT: <b>{nft['name']}</b>\n"
            if nft.get('desc'): text += f"📜 <i>{nft['desc']}</i>"

            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("🎁 Передать игроку", callback_data=f"transfer_nft_{index}"))

            if nft["type"] == "photo":
                bot.send_photo(c.message.chat.id, nft["file_id"], caption=text, parse_mode="HTML", reply_markup=kb, message_thread_id=t_id)
            elif nft["type"] == "video":
                bot.send_video(c.message.chat.id, nft["file_id"], caption=text, parse_mode="HTML", reply_markup=kb, message_thread_id=t_id)
            else:
                bot.send_animation(c.message.chat.id, nft["file_id"], caption=text, parse_mode="HTML", reply_markup=kb, message_thread_id=t_id)

        bot.answer_callback_query(c.id)
    except Exception as e:
        logger.error(f"Error: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("transfer_nft_"))
def transfer_nft_start(c):
    index = int(c.data.split("_")[2])
    msg = bot.send_message(c.message.chat.id, "👤 Введите <b>ID получателя</b>, которому хотите подарить этот предмет:", parse_mode="HTML")
    bot.register_next_step_handler(msg, process_nft_transfer, index)
    bot.answer_callback_query(c.id)

def process_nft_transfer(m, index):
    try:
        target_id = int(m.text.strip())
        u = users.find_one({"_id": m.from_user.id})
        inv = u.get("inventory", [])
        if index >= len(inv):
            bot.send_message(m.chat.id, "❌ Предмет не найден.")
            return
        target = users.find_one({"_id": target_id})
        if not target:
            bot.send_message(m.chat.id, "❌ Игрок не найден.")
            return
        nft = inv.pop(index)
        users.update_one({"_id": m.from_user.id}, {"$set": {"inventory": inv}})
        users.update_one({"_id": target_id}, {"$push": {"inventory": nft}})
        bot.send_message(m.chat.id, f"✅ Предмет <b>{nft['name']}</b> передан!", parse_mode="HTML")
        try:
            bot.send_message(target_id, f"🎁 Вам передан предмет: <b>{nft['name']}</b>!", parse_mode="HTML")
        except:
            pass
    except Exception as e:
        bot.send_message(m.chat.id, f"❌ Ошибка: {e}")

# ---------- ACHIEVEMENTS ----------

@bot.message_handler(func=lambda m: m.text in ["🏅 Достижения", "🏆 Достижения", "/achs"])
def show_achievements(m):
    try:
        t_id = getattr(m, 'message_thread_id', None)
        u = get_user(m.from_user.id, m.from_user.username)

        user_achs = u.get("achievements", [])
        mythic = u.get("mythic_achs", [])

        if not user_achs and not mythic:
            text = "<b>🏆 Ваши достижения</b>\n\n<i>У вас пока нет наград. Будьте активнее!</i>"
        else:
            text = "<b>🏆 ВАШИ НАГРАДЫ:</b>\n\n"
            for aid in user_achs:
                if 'ACHIEVEMENTS' in globals() and aid in ACHIEVEMENTS:
                    text += f"• {ACHIEVEMENTS[aid]['name']}\n"
            for ma in mythic:
                text += f"• {ma['name']}\n"

        bot.send_message(m.chat.id, text, parse_mode="HTML", message_thread_id=t_id)
    except Exception as e:
        logger.error(f"Ошибка достижений: {e}")

# ---------- SEND ----------

@bot.message_handler(func=lambda m: m.text == "💸 Отправить" or (m.text and m.text.startswith("/send")))
def send(m):
    if not is_subscribed(m): return

    if m.text == "💸 Отправить":
        bot.reply_to(m, "💡 Чтобы отправить ICE, используйте команду:\n<code>/send ID СУММА</code>\nИли ответьте на сообщение игрока: <code>/send СУММА</code>", parse_mode="HTML")
        return

    try:
        parts = m.text.split()
        to_id = None
        amount = 0.0

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

        amount_to_receive = round(amount - FEE, 8)

        users.update_one({"_id": u["_id"]}, {"$inc": {"balance": -amount}})
        users.update_one({"_id": to_id}, {"$inc": {"balance": amount_to_receive}})

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
    if not is_subscribed(m): return

    kb = types.InlineKeyboardMarkup(row_width=2)
    b1 = types.InlineKeyboardButton("💰 По балансу", callback_data="top_balance")
    b2 = types.InlineKeyboardButton("🎖 По уровню", callback_data="top_level")
    b3 = types.InlineKeyboardButton("⚔️ По победам", callback_data="top_wins")
    b4 = types.InlineKeyboardButton("🏅 По рейтингу", callback_data="top_rp")  # НОВОЕ

    kb.add(b1, b2)
    kb.add(b3, b4)

    bot.send_message(
        m.chat.id,
        "<b>Выберите таблицу лидеров:</b>",
        parse_mode="HTML",
        reply_markup=kb,
        message_thread_id=getattr(m, 'message_thread_id', None)
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("top_"))
def top_callback(c):
    try:
        data = c.data
        if data == "top_balance":
            sort_field, title, unit = "balance", "🏆 <b>ТОП-10 БОГАТЕЕВ (ICE)</b>", "ICE"
        elif data == "top_level":
            sort_field, title, unit = "level", "🎖 <b>ТОП-10 МАСТЕРОВ ФАРМА</b>", "LVL"
        elif data == "top_wins":
            sort_field, title, unit = "wins", "⚔️ <b>ТОП-10 ГЛАДИАТОРОВ</b>", "побед"
        else:
            sort_field, title, unit = "rp", "🏅 <b>ТОП-10 ПО РЕЙТИНГУ</b>", "RP"  # НОВОЕ

        top_users = users.find().sort(sort_field, -1).limit(10)

        text = f"{title}\n\n"
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}

        for i, user in enumerate(top_users, 1):
            name = user.get("first_name") or user.get("username") or f"Игрок {user['_id']}"
            name = str(name).replace("<", "").replace(">", "").replace("@", "")
            val = user.get(sort_field, 0)
            prefix = medals.get(i, f"{i}.")
            val_fmt = fmt(val) if sort_field == "balance" else int(val)
            text += f"{prefix} <b>{name}</b> — {val_fmt} {unit}\n"

        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, parse_mode="HTML")
        bot.answer_callback_query(c.id)

    except Exception as e:
        logger.error(f"Ошибка топа: {e}")
        bot.answer_callback_query(c.id, "❌ Ошибка загрузки данных")

# ---------- BATTLE ----------

@bot.message_handler(commands=["batle"])
def battle_call(m):
    if not m.reply_to_message:
        return bot.send_message(m.chat.id, "❌ Ответьте на сообщение игрока!", message_thread_id=m.message_thread_id)

    challenger = m.from_user
    opponent = m.reply_to_message.from_user

    if opponent.is_bot:
        return bot.send_message(m.chat.id, "❌ Вы не можете вызвать бота на дуэль! Найдите реального противника.", message_thread_id=m.message_thread_id)

    if challenger.id == opponent.id:
        return bot.send_message(m.chat.id, "❌ Нельзя вызвать самого себя!", message_thread_id=m.message_thread_id)

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

    text = (f"🔔 <b>{opponent.first_name}</b>, вам брошен вызов!\n"
            f"⚔️ <b>{challenger.first_name}</b> зовет вас помериться удачей в кубах!")

    bot.send_message(m.chat.id, text, reply_markup=kb, parse_mode="HTML", message_thread_id=m.message_thread_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("b_"))
def battle_callback(c):
    try:
        data = c.data.split("_")
        action = data[1]
        bid = ObjectId(data[2])
        battle = battles.find_one({"_id": bid})

        if not battle:
            return bot.answer_callback_query(c.id, "❌ Баттл не найден или уже завершен.")

        if action == "den":
            if c.from_user.id != battle["opponent_id"]:
                return bot.answer_callback_query(c.id, "Это не ваш вызов!")
            bot.edit_message_text("❌ Баттл отклонен.", battle["chat_id"], c.message.message_id)
            battles.delete_one({"_id": bid})

        elif action == "acc":
            if c.from_user.id != battle["opponent_id"]:
                return bot.answer_callback_query(c.id, "Это не ваш вызов!")

            kb = types.InlineKeyboardMarkup(row_width=3)
            btns = [types.InlineKeyboardButton(f"{x} ❄️", callback_data=f"b_bet_{bid}_{x}") for x in [1, 5, 10, 25, 50, 100]]
            kb.add(*btns)
            bot.edit_message_text("💰 Выберите ставку:", battle["chat_id"], c.message.message_id, reply_markup=kb)

        elif action == "bet":
            bet = float(data[3])
            if c.from_user.id != battle["opponent_id"]:
                return bot.answer_callback_query(c.id, "Ставку выбирает тот, кого вызвали!")

            p1 = get_user(battle["challenger_id"], None)
            p2 = get_user(battle["opponent_id"], None)

            if p1["balance"] < bet or p2["balance"] < bet:
                bot.send_message(battle["chat_id"], "❌ Недостаточно ICE у одного из игроков!", message_thread_id=battle["thread_id"])
                battles.delete_one({"_id": bid})
                bot.delete_message(battle["chat_id"], c.message.message_id)
                return

            bot.delete_message(battle["chat_id"], c.message.message_id)
            run_battle(battle, bet)

    except Exception as e:
        print(f"Ошибка Callback: {e}")

# НОВОЕ: run_battle с начислением RP
def run_battle(battle, bet):
    try:
        chat_id = battle["chat_id"]
        t_id = battle.get("thread_id")

        bot.send_message(chat_id, f"🎲 <b>{battle['challenger_name']}</b> бросает куб...", parse_mode="HTML", message_thread_id=t_id)
        d1 = bot.send_dice(chat_id, message_thread_id=t_id)
        v1 = d1.dice.value
        time.sleep(4)

        bot.send_message(chat_id, f"🎲 <b>{battle['opponent_name']}</b> бросает куб...", parse_mode="HTML", message_thread_id=t_id)
        d2 = bot.send_dice(chat_id, message_thread_id=t_id)
        v2 = d2.dice.value
        time.sleep(4)

        if v1 > v2:
            win_id   = battle["challenger_id"]
            win_name = battle["challenger_name"]
            lose_id  = battle["opponent_id"]
        elif v2 > v1:
            win_id   = battle["opponent_id"]
            win_name = battle["opponent_name"]
            lose_id  = battle["challenger_id"]
        else:
            bot.send_message(chat_id, "🤝 <b>Ничья!</b> ICE возвращены.", parse_mode="HTML", message_thread_id=t_id)
            battles.delete_one({"_id": battle["_id"]})
            return

        # Начисляем ICE
        users.update_one({"_id": win_id},  {"$inc": {"balance": bet, "wins": 1}})
        users.update_one({"_id": lose_id}, {"$inc": {"balance": -bet}})

        # НОВОЕ: начисляем RP
        winner_data = users.find_one({"_id": win_id})
        loser_data  = users.find_one({"_id": lose_id})
        winner_rp = max(0, winner_data.get("rp", 0) + RP_WIN)
        loser_rp  = max(0, loser_data.get("rp", 0)  + RP_LOSS)
        users.update_one({"_id": win_id},  {"$set": {"rp": winner_rp}})
        users.update_one({"_id": lose_id}, {"$set": {"rp": loser_rp}})

        win_league,  _ = get_league(winner_rp)
        lose_league, _ = get_league(loser_rp)

        bot.send_message(
            chat_id,
            f"🏆 Победил <b>{win_name}</b>!\n"
            f"💰 Выигрыш: <b>{bet} ICE</b>\n\n"
            f"📊 <b>Рейтинг:</b>\n"
            f"✅ Победитель: +{RP_WIN} RP → {winner_rp} RP ({win_league})\n"
            f"❌ Проигравший: {RP_LOSS} RP → {loser_rp} RP ({lose_league})",
            parse_mode="HTML",
            message_thread_id=t_id
        )

        battles.delete_one({"_id": battle["_id"]})

    except Exception as e:
        print(f"Ошибка в run_battle: {e}")

# ---------- ADMIN PANEL ----------

@bot.message_handler(commands=["admin"])
def admin_panel(m):
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
               f"/give_nft — Создать НФТ\n"
               f"/setprice ЦЕНА — Изменение курса\n"
               f"/reset_season — Новый сезон лиги\n"
               f"/add_recipe — Добавить рецепт крафта")
        bot.send_message(m.chat.id, txt, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка в админ-панели: {e}")

@bot.message_handler(commands=["stats"])
def admin_manage_user(m):
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
               f"🏆 Побед: {u.get('wins', 0)}\n"
               f"⚔️ RP: {u.get('rp', 0)}\n"
               f"🔥 Сожжено: {fmt(u.get('total_burned', 0))} ICE")

        bot.send_message(m.chat.id, txt, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        bot.reply_to(m, f"❌ Ошибка: {e}")

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

# ---------- BROADCAST ----------

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
        except:
            continue
    bot.send_message(m.chat.id, f"✅ Рассылка завершена: {count} чел.")

# ---------- GIVE ----------

@bot.message_handler(commands=["give"])
def admin_give(m):
    if m.from_user.id != ADMIN_ID:
        bot.send_message(m.chat.id, "❌ У вас нет прав администратора!", message_thread_id=m.message_thread_id)
        return
    try:
        parts = m.text.split()
        if len(parts) != 3:
            bot.send_message(m.chat.id, "🔧 Формат: <code>/give ID СУММА</code>", parse_mode="HTML", message_thread_id=m.message_thread_id)
            return

        to_id = int(parts[1])
        amount = float(parts[2])

        result = users.update_one({"_id": to_id}, {"$inc": {"balance": amount}})

        if result.matched_count > 0:
            bot.send_message(m.chat.id, f"✅ Начислено <b>{amount} ICE</b> пользователю <code>{to_id}</code>", parse_mode="HTML", message_thread_id=m.message_thread_id)
            try:
                bot.send_message(to_id, f"🎁 Админ начислил вам <b>{amount} ICE</b>!", parse_mode="HTML")
            except:
                pass
        else:
            bot.send_message(m.chat.id, "❌ Пользователь не найден в базе!", message_thread_id=m.message_thread_id)

    except Exception as e:
        logger.error(f"Ошибка give: {e}")
        bot.send_message(m.chat.id, "❌ Ошибка при выполнении команды", message_thread_id=m.message_thread_id)

# ---------- NFT ----------

@bot.message_handler(commands=['give_nft'])
def start_nft_creation(m):
    if m.from_user.id != ADMIN_ID: return
    msg = bot.reply_to(m, "👤 Введите <b>ID игрока</b>, которому дарим NFT:", parse_mode="HTML")
    bot.register_next_step_handler(msg, get_nft_target)

def get_nft_target(m):
    try:
        target_id = int(m.text)
        msg = bot.send_message(m.chat.id, "🖼 Теперь пришлите <b>медиа</b> (фото, гиф или видео):", parse_mode="HTML")
        bot.register_next_step_handler(msg, get_nft_media, target_id)
    except:
        bot.send_message(m.chat.id, "❌ ID должен быть числом. Отмена.")

def get_nft_media(m, target_id):
    file_id = None
    file_type = None

    if m.content_type == 'photo':
        file_id = m.photo[-1].file_id
        file_type = 'photo'
    elif m.content_type == 'animation':
        file_id = m.animation.file_id
        file_type = 'animation'
    elif m.content_type == 'video':
        file_id = m.video.file_id
        file_type = 'video'

    if not file_id:
        bot.send_message(m.chat.id, "❌ Это не медиа. Отмена.")
        return

    msg = bot.send_message(m.chat.id, "🏷 Введите <b>Название</b> предмета:", parse_mode="HTML")
    bot.register_next_step_handler(msg, get_nft_name, target_id, file_id, file_type)

def get_nft_name(m, target_id, file_id, file_type):
    name = m.text
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    kb.add("Пропустить")
    msg = bot.send_message(m.chat.id, "📝 Введите <b>Описание</b> (или нажмите кнопку Пропустить):", reply_markup=kb, parse_mode="HTML")
    bot.register_next_step_handler(msg, final_nft_step, target_id, file_id, file_type, name)

def final_nft_step(m, target_id, file_id, file_type, name):
    desc = m.text if m.text != "Пропустить" else ""

    nft_data = {
        "name": name,
        "desc": desc,
        "file_id": file_id,
        "type": file_type,
        "date": int(time.time())
    }

    users.update_one({"_id": target_id}, {"$push": {"inventory": nft_data}})

    bot.send_message(m.chat.id, f"✅ NFT «{name}» успешно выдано!", reply_markup=create_main_keyboard())
    try:
        bot.send_message(target_id, f"🎁 Вы получили NFT: <b>{name}</b>\n<i>{desc}</i>", parse_mode="HTML")
    except:
        pass

# ---------- VIP ----------

@bot.message_handler(commands=['vipon'])
def vip_on_start(m):
    if m.from_user.id != ADMIN_ID: return
    msg = bot.reply_to(m, "👤 Введите <b>ID игрока</b>, которому выдаем VIP:", parse_mode="HTML")
    bot.register_next_step_handler(msg, vip_step_emoji)

def vip_step_emoji(m):
    try:
        target_id = int(m.text)
        msg = bot.send_message(m.chat.id, "🍀 Введите <b>один эмодзи</b> для профиля (например: 💎 или 🔥):", parse_mode="HTML")
        bot.register_next_step_handler(msg, vip_step_media, target_id)
    except:
        bot.send_message(m.chat.id, "❌ Ошибка в ID. Отмена.")

def vip_step_media(m, target_id):
    emoji = m.text or "🍀"
    msg = bot.send_message(m.chat.id, "🖼 Теперь пришлите <b>фото/гиф</b> для фона (или /skip):", parse_mode="HTML")
    bot.register_next_step_handler(msg, vip_final, target_id, emoji)

def vip_final(m, target_id, emoji):
    bg_id = None
    bg_type = None

    if m.content_type in ['photo', 'animation']:
        bg_id = m.photo[-1].file_id if m.content_type == 'photo' else m.animation.file_id
        bg_type = m.content_type

    users.update_one({"_id": target_id}, {
        "$set": {
            "is_vip": True,
            "vip_emoji": emoji,
            "vip_background": bg_id,
            "vip_type": bg_type
        }
    })
    bot.send_message(m.chat.id, f"✅ VIP для <code>{target_id}</code> настроен!", parse_mode="HTML")

# ---------- SET PRICE ----------

@bot.message_handler(commands=["setprice"])
def set_price(m):
    if m.from_user.id != ADMIN_ID: return
    try:
        new_price = m.text.split()[1]
        db.settings.update_one({"_id": "ice_price"}, {"$set": {"value": new_price}}, upsert=True)
        bot.reply_to(m, f"✅ Курс обновлен: 1 ICE = {new_price} GOLD")
    except:
        bot.reply_to(m, "❌ Ошибка. Используйте: <code>/setprice 8000</code>", parse_mode="HTML")

def get_current_price():
    price_doc = db.settings.find_one({"_id": "ice_price"})
    return price_doc["value"] if price_doc else "не установлен"

# ---------- WITHDRAW ----------

@bot.message_handler(commands=["withdraw"])
def withdraw(m):
    if not is_subscribed(m): return

    price_doc = settings.find_one({"_id": "ice_price"})
    if not price_doc:
        bot.reply_to(m, "❌ Курс обмена еще не установлен админом.")
        return

    try:
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

    if action == "bot":
        fee = FEE_BOT_TRANSFER
        ice_to_send = amount - fee
        gold_to_receive = int(ice_to_send * rate)

        users.update_one({"_id": m.from_user.id}, {"$inc": {"balance": -amount}})

        try:
            bank_db.update_one(
                {"uid": str(m.from_user.id)},
                {"$inc": {"balance": gold_to_receive}, "$set": {"name": m.from_user.first_name}},
                upsert=True
            )
            bot.reply_to(m, f"✅ <b>Успешный перевод!</b>\n\n"
                            f"💰 Списано: <b>{amount} ICE</b>\n"
                            f"💳 Комиссия: <b>{fee} ICE</b>\n"
                            f"🏦 В Rucoy Bank: <b>{gold_to_receive:,} GOLD</b>", parse_mode="HTML")
            bot.send_message(ADMIN_ID, f"🔔 <b>Авто-вывод</b>\nЮзер: @{u.get('username')} (<code>{u['_id']}</code>)\nСумма: {amount} ICE -> {gold_to_receive} GOLD", parse_mode="HTML")

        except Exception as e:
            users.update_one({"_id": m.from_user.id}, {"$inc": {"balance": amount}})
            logger.error(f"Ошибка БД при выводе: {e}")
            bot.reply_to(m, "❌ Ошибка базы данных Rucoy Bank. Попробуйте позже.")

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

# ---------- REFERRALS ----------

@bot.message_handler(func=lambda m: m.text == "👥 Рефералы")
def referral_menu(m):
    uid = m.from_user.id
    bot_info = bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{uid}"

    u = get_user(uid, m.from_user.username, m.from_user.first_name)
    is_vip = u.get("is_vip", False)
    bonus = 15 if is_vip else 10

    text = (f"<b>👥 Реферальная программа</b>\n\n"
            f"Приглашайте друзей и получайте бонусы за каждого новичка!\n\n"
            f"💰 Ваша награда: <b>{bonus} ICE</b> за друга\n"
            f"🔗 Ваша ссылка:\n<code>{ref_link}</code>\n\n"
            f"<i>Просто отправьте эту ссылку другу. Бонус начислится, когда он нажмет Start.</i>")

    bot.send_message(m.chat.id, text, parse_mode="HTML")

# ================================================================
# НОВОЕ: СЖИГАНИЕ МОНЕТ 🔥
# ================================================================

@bot.message_handler(commands=["burn"])
@bot.message_handler(func=lambda m: m.text == "🔥 Сжечь ICE")
def burn_coins(m):
    t_id = getattr(m, "message_thread_id", None)

    # Если нажали кнопку — показываем инфо и просим ввести сумму
    is_button = (m.text == "🔥 Сжечь ICE")
    parts = m.text.split() if not is_button else ["/burn"]

    u = get_user(m.from_user.id, m.from_user.username)
    burned = u.get("total_burned", 0.0)
    rank_name, rank_emoji = get_burn_rank(burned)

    next_rank_text = ""
    for threshold in sorted(BURN_RANKS):
        if burned < threshold:
            need = threshold - burned
            next_rank_text = f"\n⬆️ До следующего ранга: <b>{fmt(need)} ICE</b>"
            break

    if len(parts) < 2:
        bot.send_message(
            m.chat.id,
            f"🔥 <b>СЖИГАНИЕ МОНЕТ</b>\n\n"
            f"Всего сожжено: <b>{fmt(burned)} ICE</b>\n"
            f"Ваш ранг: <b>{rank_name}</b> {rank_emoji}"
            f"{next_rank_text}\n\n"
            f"<b>Ранги сжигания:</b>\n"
            f"🧊 Лёд — 0 ICE\n"
            f"🔥 Горящий — 100 ICE\n"
            f"💀 Пепел — 500 ICE\n"
            f"☄️ Метеор — 1 000 ICE\n"
            f"🌋 Вулкан — 5 000 ICE\n\n"
            f"Чтобы сжечь: <code>/burn СУММА</code>",
            parse_mode="HTML",
            message_thread_id=t_id
        )
        return

    try:
        amount = float(parts[1].replace(",", "."))
    except ValueError:
        bot.reply_to(m, "❌ Укажите корректную сумму. Пример: <code>/burn 50</code>", parse_mode="HTML")
        return

    if amount < 1:
        bot.reply_to(m, "❌ Минимальная сумма сжигания: <b>1 ICE</b>", parse_mode="HTML")
        return

    if u["balance"] < amount:
        bot.reply_to(m, f"❌ Недостаточно средств.\nБаланс: <b>{fmt(u['balance'])} ICE</b>", parse_mode="HTML")
        return

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("🔥 Да, сжечь!", callback_data=f"burn_confirm_{amount}"),
        types.InlineKeyboardButton("❌ Отмена",     callback_data="burn_cancel")
    )
    bot.send_message(
        m.chat.id,
        f"⚠️ <b>Вы уверены?</b>\n\nСжечь <b>{fmt(amount)} ICE</b> безвозвратно?\n<i>Монеты будут уничтожены навсегда.</i>",
        reply_markup=kb,
        parse_mode="HTML",
        message_thread_id=t_id
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("burn_"))
def burn_callback(c):
    if c.data == "burn_cancel":
        bot.edit_message_text("❌ Сжигание отменено.", c.message.chat.id, c.message.message_id)
        return

    try:
        amount = float(c.data.split("_")[2])
    except Exception:
        bot.answer_callback_query(c.id, "❌ Ошибка")
        return

    u = users.find_one({"_id": c.from_user.id})
    if not u or u["balance"] < amount:
        bot.edit_message_text("❌ Недостаточно средств.", c.message.chat.id, c.message.message_id)
        return

    old_burned  = u.get("total_burned", 0.0)
    new_burned  = round(old_burned + amount, 2)
    new_balance = round(u["balance"] - amount, 2)
    old_rank, _ = get_burn_rank(old_burned)
    new_rank, new_emoji = get_burn_rank(new_burned)

    users.update_one(
        {"_id": c.from_user.id},
        {"$set": {"balance": new_balance, "total_burned": new_burned, "burn_emoji": new_emoji}}
    )

    rank_up_text = ""
    if old_rank != new_rank:
        rank_up_text = f"\n\n🎉 <b>Новый ранг: {new_rank} {new_emoji}</b>"

    bot.edit_message_text(
        f"🔥 <b>Сожжено {fmt(amount)} ICE!</b>\n\n"
        f"Всего сожжено: <b>{fmt(new_burned)} ICE</b>\n"
        f"Ранг: <b>{new_rank}</b> {new_emoji}\n"
        f"Остаток: <b>{fmt(new_balance)} ICE</b>"
        f"{rank_up_text}",
        c.message.chat.id,
        c.message.message_id,
        parse_mode="HTML"
    )
    bot.answer_callback_query(c.id, f"🔥 -{amount} ICE сожжено!")

# ================================================================
# НОВОЕ: КРАФТ ПРЕДМЕТОВ ⚗️
# ================================================================

@bot.message_handler(commands=["craft"])
@bot.message_handler(func=lambda m: m.text == "⚗️ Крафт")
def craft_menu(m):
    t_id = getattr(m, "message_thread_id", None)
    u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    inv = u.get("inventory", [])

    recipes_text = "\n".join(
        f"{RARITY_EMOJI.get(v['rarity'], '⚪')} <b>{k}</b> = {' + '.join(v['ingredients'])} "
        f"(шанс {int(v['chance']*100)}%)"
        for k, v in CRAFT_RECIPES.items()
    )

    if len(inv) < 2:
        bot.send_message(
            m.chat.id,
            f"⚗️ <b>Крафт предметов</b>\n\nДля крафта нужно минимум <b>2 предмета</b> в инвентаре.\n\n"
            f"<b>Известные рецепты:</b>\n{recipes_text}",
            parse_mode="HTML",
            message_thread_id=t_id
        )
        return

    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, item in enumerate(inv):
        rarity_icon = RARITY_EMOJI.get(item.get("rarity", ""), "🖼")
        kb.add(types.InlineKeyboardButton(f"[{i+1}] {rarity_icon} {item['name']}", callback_data=f"craft_pick1_{i}"))

    bot.send_message(
        m.chat.id,
        f"⚗️ <b>КРАФТ</b>\nВыберите <b>первый</b> предмет:\n\n<b>Рецепты:</b>\n{recipes_text}",
        reply_markup=kb,
        parse_mode="HTML",
        message_thread_id=t_id
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("craft_pick1_"))
def craft_pick_first(c):
    idx1 = int(c.data.split("_")[2])
    u = users.find_one({"_id": c.from_user.id})
    inv = u.get("inventory", [])

    if idx1 >= len(inv):
        bot.answer_callback_query(c.id, "❌ Предмет не найден")
        return

    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, item in enumerate(inv):
        if i == idx1: continue
        rarity_icon = RARITY_EMOJI.get(item.get("rarity", ""), "🖼")
        kb.add(types.InlineKeyboardButton(f"[{i+1}] {rarity_icon} {item['name']}", callback_data=f"craft_pick2_{idx1}_{i}"))

    bot.edit_message_text(
        f"⚗️ Выбран: <b>{inv[idx1]['name']}</b>\n\nВыберите <b>второй</b> предмет:",
        c.message.chat.id, c.message.message_id,
        reply_markup=kb, parse_mode="HTML"
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("craft_pick2_"))
def craft_pick_second(c):
    parts = c.data.split("_")
    idx1, idx2 = int(parts[2]), int(parts[3])

    u = users.find_one({"_id": c.from_user.id})
    inv = u.get("inventory", [])

    if idx1 >= len(inv) or idx2 >= len(inv):
        bot.answer_callback_query(c.id, "❌ Предмет не найден")
        return

    item1 = inv[idx1]
    item2 = inv[idx2]

    recipe_name = None
    recipe = None
    for rname, rdata in CRAFT_RECIPES.items():
        if sorted(rdata["ingredients"]) == sorted([item1["name"], item2["name"]]):
            recipe_name = rname
            recipe = rdata
            break

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("⚗️ Крафтить!", callback_data=f"craft_do_{idx1}_{idx2}"),
        types.InlineKeyboardButton("❌ Отмена",    callback_data="craft_cancel")
    )

    if recipe:
        rarity_emoji = RARITY_EMOJI.get(recipe["rarity"], "⚪")
        text = (f"⚗️ <b>Рецепт найден!</b>\n\n"
                f"{item1['name']} + {item2['name']}\n"
                f"➡️ {rarity_emoji} <b>{recipe_name}</b>\n"
                f"🎲 Шанс успеха: <b>{int(recipe['chance']*100)}%</b>\n\n"
                f"<i>При неудаче оба предмета уничтожаются.</i>")
    else:
        text = (f"⚗️ <b>Рецепт не найден</b>\n\n"
                f"{item1['name']} + {item2['name']}\n\n"
                f"<i>Попробовать всё равно? Шанс: <b>5%</b></i>")

    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data == "craft_cancel")
def craft_cancel(c):
    bot.edit_message_text("❌ Крафт отменён.", c.message.chat.id, c.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("craft_do_"))
def craft_do(c):
    parts = c.data.split("_")
    idx1, idx2 = int(parts[2]), int(parts[3])

    u = users.find_one({"_id": c.from_user.id})
    inv = u.get("inventory", [])

    if idx1 >= len(inv) or idx2 >= len(inv):
        bot.edit_message_text("❌ Предметы уже не существуют.", c.message.chat.id, c.message.message_id)
        return

    item1 = inv[idx1]
    item2 = inv[idx2]

    recipe_name = None
    recipe = None
    for rname, rdata in CRAFT_RECIPES.items():
        if sorted(rdata["ingredients"]) == sorted([item1["name"], item2["name"]]):
            recipe_name = rname
            recipe = rdata
            break

    chance = recipe["chance"] if recipe else 0.05

    # Удаляем оба предмета (с большего индекса)
    for idx in sorted([idx1, idx2], reverse=True):
        inv.pop(idx)

    success = random.random() < chance

    if success:
        if recipe:
            new_item = {
                "name": recipe_name,
                "desc": recipe["desc"],
                "file_id": item1.get("file_id"),
                "type": item1.get("type", "photo"),
                "rarity": recipe["rarity"],
                "date": int(time.time())
            }
            result_text = (
                f"✅ <b>Крафт успешен!</b>\n\n"
                f"{RARITY_EMOJI.get(recipe['rarity'], '⚪')} Получен: <b>{recipe_name}</b>\n"
                f"<i>{recipe['desc']}</i>"
            )
        else:
            new_item = {
                "name": "Загадочный Осколок",
                "desc": "Результат неизвестного крафта.",
                "file_id": item1.get("file_id"),
                "type": item1.get("type", "photo"),
                "rarity": "rare",
                "date": int(time.time())
            }
            result_text = "✅ <b>Удача! Получен Загадочный Осколок!</b>"

        inv.append(new_item)
    else:
        result_text = (
            f"💥 <b>Крафт провалился!</b>\n\n"
            f"<i>{item1['name']}</i> и <i>{item2['name']}</i> уничтожены.\n"
            f"Попробуй снова!"
        )

    users.update_one({"_id": c.from_user.id}, {"$set": {"inventory": inv}})
    bot.edit_message_text(result_text, c.message.chat.id, c.message.message_id, parse_mode="HTML")
    bot.answer_callback_query(c.id)

# ================================================================
# НОВОЕ: ЛИГА БАТТЛОВ ⚔️
# ================================================================

@bot.message_handler(commands=["league"])
@bot.message_handler(func=lambda m: m.text == "⚔️ Моя лига")
def show_league(m):
    t_id = getattr(m, "message_thread_id", None)
    u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)

    rp = u.get("rp", 0)
    league_name, _ = get_league(rp)

    next_league_text = ""
    for threshold, name, _ in LEAGUES:
        if rp < threshold:
            next_league_text = f"\n⬆️ До <b>{name}</b>: <b>{threshold - rp} RP</b>"
            break

    top5 = list(users.find({}, {"first_name": 1, "username": 1, "rp": 1}).sort("rp", -1).limit(5))
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    top_text = "\n".join(
        f"{medals.get(i, f'{i}.')} {p.get('first_name') or p.get('username', '?')} — {p.get('rp', 0)} RP"
        for i, p in enumerate(top5, 1)
    )

    bot.send_message(
        m.chat.id,
        f"⚔️ <b>ЛИГА БАТТЛОВ</b>\n\n"
        f"Ваш рейтинг: <b>{rp} RP</b>\n"
        f"Лига: <b>{league_name}</b>"
        f"{next_league_text}\n\n"
        f"<b>🏆 Топ-5 сезона:</b>\n{top_text}\n\n"
        f"✅ Победа: +{RP_WIN} RP\n"
        f"❌ Поражение: {RP_LOSS} RP",
        parse_mode="HTML",
        message_thread_id=t_id
    )

# ================================================================
# НОВОЕ: СБРОС СЕЗОНА (ADMIN)
# ================================================================

@bot.message_handler(commands=["reset_season"])
def reset_season(m):
    if m.from_user.id != ADMIN_ID: return

    top3 = list(users.find({}, {"_id": 1, "first_name": 1, "username": 1, "rp": 1}).sort("rp", -1).limit(3))
    prizes = [500, 200, 100]

    prize_text = ""
    for i, (p, prize) in enumerate(zip(top3, prizes), 1):
        users.update_one({"_id": p["_id"]}, {"$inc": {"balance": prize}})
        name = p.get("first_name") or p.get("username", "?")
        prize_text += f"{['🥇','🥈','🥉'][i-1]} {name} — +{prize} ICE\n"
        try:
            bot.send_message(
                p["_id"],
                f"🏆 <b>Конец сезона!</b>\nВы заняли <b>{i} место</b> в рейтинге!\n🎁 Приз: <b>+{prize} ICE</b>",
                parse_mode="HTML"
            )
        except:
            pass

    users.update_many({}, {"$set": {"rp": 0}})
    bot.send_message(m.chat.id, f"✅ <b>Новый сезон начат!</b>\n\nПризёры:\n{prize_text}", parse_mode="HTML")

# ================================================================
# НОВОЕ: ДОБАВИТЬ РЕЦЕПТ КРАФТА (ADMIN)
# ================================================================

@bot.message_handler(commands=["add_recipe"])
def add_recipe_start(m):
    if m.from_user.id != ADMIN_ID: return
    msg = bot.reply_to(m, "📝 Введите название <b>результата</b> крафта:", parse_mode="HTML")
    bot.register_next_step_handler(msg, add_recipe_name)

def add_recipe_name(m):
    result_name = m.text.strip()
    msg = bot.send_message(m.chat.id, "🧩 Два ингредиента через запятую:\n<i>Пример: Ледяной Осколок, Огненный Камень</i>", parse_mode="HTML")
    bot.register_next_step_handler(msg, add_recipe_ingredients, result_name)

def add_recipe_ingredients(m, result_name):
    parts = [x.strip() for x in m.text.split(",")]
    if len(parts) != 2:
        bot.send_message(m.chat.id, "❌ Нужно ровно 2 ингредиента через запятую. Отмена.")
        return
    msg = bot.send_message(m.chat.id, "🎲 Шанс успеха от 0.01 до 1.0 (пример: 0.5 = 50%):")
    bot.register_next_step_handler(msg, add_recipe_chance, result_name, parts)

def add_recipe_chance(m, result_name, ingredients):
    try:
        chance = float(m.text.replace(",", "."))
        assert 0 < chance <= 1
    except:
        bot.send_message(m.chat.id, "❌ Неверный шанс. Отмена.")
        return
    msg = bot.send_message(m.chat.id, "⭐ Редкость: <code>rare</code> / <code>epic</code> / <code>legendary</code>", parse_mode="HTML")
    bot.register_next_step_handler(msg, add_recipe_rarity, result_name, ingredients, chance)

def add_recipe_rarity(m, result_name, ingredients, chance):
    rarity = m.text.strip().lower()
    if rarity not in ("rare", "epic", "legendary"):
        bot.send_message(m.chat.id, "❌ Допустимо: rare, epic, legendary. Отмена.")
        return
    msg = bot.send_message(m.chat.id, "📜 Введите описание предмета:")
    bot.register_next_step_handler(msg, add_recipe_final, result_name, ingredients, chance, rarity)

def add_recipe_final(m, result_name, ingredients, chance, rarity):
    desc = m.text.strip()
    CRAFT_RECIPES[result_name] = {
        "ingredients": ingredients,
        "chance": chance,
        "desc": desc,
        "rarity": rarity
    }
    bot.send_message(
        m.chat.id,
        f"✅ Рецепт добавлен!\n\n"
        f"{RARITY_EMOJI.get(rarity, '⚪')} <b>{result_name}</b>\n"
        f"= {' + '.join(ingredients)}\n"
        f"Шанс: {int(chance*100)}% | {rarity}\n"
        f"<i>{desc}</i>",
        parse_mode="HTML"
    )

# ================================================================

# ---------- UNKNOWN ----------

@bot.message_handler(func=lambda m: True)
def unknown_command(m):
    if m.chat.type != 'private': return
    bot.reply_to(m, "❓ Неизвестная команда. Используйте меню или /start")

# ---------- RUN ----------

if __name__ == "__main__":
    logger.info("Запуск ICECOIN...")

    if WEBHOOK and "http" in WEBHOOK:
        try:
            bot.remove_webhook()
            time.sleep(1)
            bot.set_webhook(url=f"{WEBHOOK}/{TOKEN}")
            logger.info(f"Webhook установлен: {WEBHOOK}/{TOKEN}")
            port = int(os.environ.get("PORT", 10000))
            app.run(host="0.0.0.0", port=port)
        except Exception as e:
            logger.error(f"Ошибка при установке Webhook: {e}")
    else:
        logger.info("Запуск через Long Polling (локально)...")
        bot.remove_webhook()
        bot.infinity_polling()
