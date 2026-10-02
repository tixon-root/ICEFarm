import os
import re
import time
import random
import uuid
import json
import logging
import hashlib
import hmac
from urllib.parse import parse_qsl, unquote

from flask import Flask, request, jsonify
import telebot
from telebot import types
from pymongo import MongoClient
from dotenv import load_dotenv

# ============================================================
# ICEFarm v1.1.07 - новая версия
# ============================================================

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
WEBHOOK = os.getenv("WEBHOOK")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6395348885"))

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)
db = client["icecoin"]
users = db["users"]
clans = db["clans"]
settings = db["settings"]
seasonal_passes = db["seasonal_passes"]
users.create_index("username")
users.create_index("nickname")
users.create_index("clan_id")
clans.create_index("leader_uid")

# ============================================================
# Utils
# ============================================================

def fmt(x):
    try:
        val = float(x)
        return "{:,.2f}".format(val).replace(",", " ").replace(".00", "")
    except Exception:
        return str(x)

BAD_WORDS = [
    "porn", "sex", "xxx", "anal", "horny", "nsfw", "18+",
    "отсос", "трах", "порно", "секс", "хентай", "эрот"
]

NICKNAME_RE = re.compile(r'^[A-Za-zА-Яа-яЁё0-9_]{3,20}$')


def safe_text(value):
    if value is None:
        return ""
    return str(value).replace("<", "&lt;").replace(">", "&gt;").replace("&", "&amp;")


def is_valid_nickname(name):
    if not name:
        return False
    n = str(name).strip()
    if len(n) < 3:
        return False
    if not NICKNAME_RE.fullmatch(n):
        return False
    lower = n.lower()
    for bad in BAD_WORDS:
        if bad.lower() in lower:
            return False
    return True


def normalize_nickname(name):
    n = str(name or "").strip().replace(" ", "_")
    return n[:20]


def get_user(uid, username=None, first_name=None):
    u = users.find_one({"_id": uid})
    if u:
        if first_name and u.get("first_name") != first_name:
            users.update_one({"_id": uid}, {"$set": {"first_name": first_name}})
            u["first_name"] = first_name
        if username and u.get("username") != username:
            users.update_one({"_id": uid}, {"$set": {"username": username}})
            u["username"] = username
        return u

    nick = normalize_nickname(username) if username else None
    default_nick = f"user_{uid}"
    u = {
        "_id": uid,
        "username": username or default_nick,
        "first_name": first_name or (username or f"User_{uid}"),
        "nickname": nick or default_nick,
        "balance": 0.0,
        "snow_jewels": 0.0,
        "level": 1,
        "wins": 0,
        "farm": 0,
        "rp": 0,
        "total_burned": 0.0,
        "inventory": [],
        "cards": [],
        "active_card_id": None,
        "card_storage_limit": 5,
        "is_vip": False,
        "vip_emoji": "💎",
        "vip_background": None,
        "vip_type": None,
        "clan_id": None,
        "clan_requests": [],
        "needs_nickname_setup": True,
        "season_pass": {"season": "autumn", "level": 0, "rewards_claimed": []},
        "profile_photo": None,
        "profile_emoji": "👤",
        "id_changer": 0,
        "farm_nft_gif": None,
        "farm_nft_message": "",
        "farm_report_mode": "default",
        "created_at": int(time.time())
    }
    users.insert_one(u)
    return u


def user_by_nickname(nickname):
    if not nickname:
        return None
    return users.find_one({"nickname": nickname}) or users.find_one({"username": nickname})


def mention_html(uid, name):
    safe_name = safe_text(name or uid)
    return f'<a href="tg://user?id={uid}">{safe_name}</a>'


# ============================================================
# Burn rank / seasonal pass
# ============================================================

BURN_RANKS = {
    0: ("🧊 Лёд", ""),
    100: ("🔥 Горящий", "🔥"),
    500: ("💀 Пепел", "💀"),
    1000: ("☄️ Метеор", "☄️"),
    5000: ("🌋 Вулкан", "🌋"),
    15000: ("🌌 Кристалл", "🌌"),
}


def get_burn_rank(total_burned):
    rank_name, rank_emoji = "🧊 Лёд", ""
    for threshold in sorted(BURN_RANKS):
        if total_burned >= threshold:
            rank_name, rank_emoji = BURN_RANKS[threshold]
    return rank_name, rank_emoji


SEASONAL_PASS = {
    "autumn": {
        "title": "Season Autumn",
        "image_url": "https://ibb.co/MDD85t0C",
        "profile_preview": "https://ibb.co/nNMkmdJh",
        "levels": [
            {"id": 1,  "threshold": 100,   "name": "🧺 Starter Reward",   "reward": {"type": "ice", "value": 100}},
            {"id": 2,  "threshold": 400,   "name": "💰 Rich Harvest",     "reward": {"type": "ice", "value": 400}},
            {"id": 3,  "threshold": 900,   "name": "💄 Autumn Lipstick",   "reward": {"type": "item", "key": "autumn_lipstick", "name": "Autumn Lipstick Emoji"}},
            {"id": 4,  "threshold": 1800,  "name": "💎 5 Jewels",         "reward": {"type": "jewel", "value": 5}},
            {"id": 5,  "threshold": 3500,  "name": "💎 10 Jewels",        "reward": {"type": "jewel", "value": 10}},
            {"id": 6,  "threshold": 6000,  "name": "📸 Profile Photo",     "reward": {"type": "profile_photo", "name": "Autumn Profile Photo"}},
            {"id": 7,  "threshold": 9000,  "name": "🧩 NFT Farm Gif",     "reward": {"type": "nft_gif", "name": "Autumn Farm NFT"}},
            {"id": 8,  "threshold": 12500, "name": "🎁 VIP Bonus Pack",    "reward": {"type": "vip_bonus", "name": "VIP Pass Bonus"}},
            {"id": 9,  "threshold": 15000, "name": "✨ Premium Avatar Set","reward": {"type": "item", "key": "autumn_avatar_pack", "name": "Autumn Avatar Pack"}},
        ]
    }
}


def get_pass_data(season):
    return SEASONAL_PASS.get(season, SEASONAL_PASS["autumn"])


def season_pass_level_for_total(total_burned):
    season = get_pass_data("autumn")
    achieved = 0
    for level in season["levels"]:
        if total_burned >= level["threshold"]:
            achieved = max(achieved, level["id"])
    return achieved


def grant_reward_to_user(uid, reward):
    u = users.find_one({"_id": uid})
    if not u:
        return False

    rtype = reward.get("type")
    if rtype == "ice":
        users.update_one({"_id": uid}, {"$inc": {"balance": float(reward.get("value", 0))}})
        return True

    if rtype == "jewel":
        users.update_one({"_id": uid}, {"$inc": {"snow_jewels": float(reward.get("value", 0))}})
        return True

    if rtype == "item":
        item = {
            "type": "item",
            "item_key": reward.get("key"),
            "name": reward.get("name", "Season Reward"),
            "desc": "Награда за сезон Autumn",
            "emoji": "🎁",
            "rarity": "rare",
            "date": int(time.time()),
        }
        users.update_one({"_id": uid}, {"$push": {"inventory": item}})
        return True

    if rtype == "profile_photo":
        users.update_one({"_id": uid}, {"$set": {"profile_photo": SEASONAL_PASS["autumn"]["profile_preview"]}})
        return True

    if rtype == "nft_gif":
        users.update_one({"_id": uid}, {"$set": {"farm_nft_gif": SEASONAL_PASS["autumn"]["image_url"], "farm_report_mode": "season_autumn"}})
        return True

    if rtype == "vip_bonus":
        users.update_one({"_id": uid}, {"$set": {"is_vip": True, "vip_emoji": "💎", "vip_background": SEASONAL_PASS["autumn"]["profile_preview"], "vip_type": "photo"}})
        return True

    return False


def check_season_pass(uid):
    u = users.find_one({"_id": uid})
    if not u:
        return

    total = float(u.get("total_burned", 0.0))
    season = get_pass_data("autumn")
    claimed = set(u.get("season_pass", {}).get("rewards_claimed", []))
    unlocked = []
    for level in season["levels"]:
        if total >= level["threshold"]:
            unlocked.append(level)

    new_claims = []
    for level in unlocked:
        if str(level["id"]) not in claimed:
            granted = grant_reward_to_user(uid, level["reward"])
            if granted:
                new_claims.append(str(level["id"]))

    if new_claims:
        current = u.get("season_pass", {})
        current["season"] = "autumn"
        current["level"] = max(level['id'] for level in unlocked)
        current["rewards_claimed"] = list(set(current.get("rewards_claimed", []) + new_claims))
        users.update_one({"_id": uid}, {"$set": {"season_pass": current}})
        try:
            bot.send_message(uid, "🎉 <b>Новый уровень сезонного пропуска!</b>\n\nНаграды зачислены в ваш аккаунт.", parse_mode="HTML")
        except Exception:
            pass


# ============================================================
# Card system
# ============================================================

CARD_MAX_STORAGE = 5
CARD_TO_JEWEL = {1: 0.1, 2: 0.2, 3: 0.4, 4: 0.6, 5: 1.0}

CARD_DATA = {
    "assassin": {"name": "Assassin", "tier": "F", "levels": [{"hp": 14, "dmg": 4, "def": 0.10}, {"hp": 17, "dmg": 7, "def": 0.12}, {"hp": 21, "dmg": 12, "def": 0.14}]},
    "vampire": {"name": "Vampire", "tier": "F", "levels": [{"hp": 25, "dmg": 16, "def": 0.12}, {"hp": 31, "dmg": 19, "def": 0.16}, {"hp": 37, "dmg": 23, "def": 0.19}]},
    "skeleton": {"name": "Skeleton", "tier": "F", "levels": [{"hp": 30, "dmg": 19, "def": 0.12}, {"hp": 36, "dmg": 24, "def": 0.18}, {"hp": 41, "dmg": 28, "def": 0.21}]},
    "crow": {"name": "Crow", "tier": "F", "levels": [{"hp": 4, "dmg": 1, "def": 0.08}, {"hp": 6, "dmg": 3, "def": 0.10}, {"hp": 10, "dmg": 5, "def": 0.14}]},
    "goblin": {"name": "Goblin", "tier": "F", "levels": [{"hp": 10, "dmg": 4, "def": 0.08}, {"hp": 14, "dmg": 6, "def": 0.12}, {"hp": 18, "dmg": 9, "def": 0.15}]},
    "lizard": {"name": "Lizard", "tier": "D", "levels": [{"hp": 69, "dmg": 35, "def": 0.23}, {"hp": 78, "dmg": 41, "def": 0.31}, {"hp": 83, "dmg": 46, "def": 0.38}]},
    "gargoyle": {"name": "Gargoyle", "tier": "D", "levels": [{"hp": 75, "dmg": 37, "def": 0.25}, {"hp": 81, "dmg": 40, "def": 0.29}, {"hp": 92, "dmg": 47, "def": 0.35}]},
    "red_dragon": {"name": "Red Dragon", "tier": "C", "levels": [{"hp": 135, "dmg": 80, "def": 0.43}, {"hp": 148, "dmg": 97, "def": 0.70}, {"hp": 159, "dmg": 103, "def": 0.83}]},
    "ice_dragon": {"name": "ICE Dragon", "tier": "C", "levels": [{"hp": 135, "dmg": 80, "def": 0.40}, {"hp": 141, "dmg": 98, "def": 0.62}, {"hp": 152, "dmg": 111, "def": 0.70}]},
    "yeti": {"name": "Yeti", "tier": "B", "levels": [{"hp": 230, "dmg": 120, "def": 0.52}, {"hp": 248, "dmg": 137, "def": 0.75}, {"hp": 264, "dmg": 151, "def": 0.85}]},
    "golem": {"name": "Golem", "tier": "B", "levels": [{"hp": 225, "dmg": 135, "def": 0.53}, {"hp": 251, "dmg": 139, "def": 0.79}, {"hp": 268, "dmg": 150, "def": 0.94}]},
    "demon": {"name": "Demon", "tier": "A", "levels": [{"hp": 460, "dmg": 210, "def": 1.1}, {"hp": 500, "dmg": 239, "def": 1.3}, {"hp": 536, "dmg": 254, "def": 1.9}]},
}

USER_CARD_KEYS = [key for key in CARD_DATA]


def create_card_instance(kind, key):
    return {"id": uuid.uuid4().hex[:10], "kind": kind, "key": key, "level": 1}


def card_convert_value(card):
    if card["kind"] == "class":
        return 20
    mob = CARD_DATA[card["key"]]
    tier_num = {"F": 1, "D": 2, "C": 3, "B": 4, "A": 5}.get(mob["tier"], 1)
    return round(float(CARD_TO_JEWEL.get(tier_num, 0.1)) * card.get("level", 1), 2)


def card_name(card):
    if card["kind"] == "class":
        return f"Class {card['key']} lvl {card.get('level', 1)}"
    mob = CARD_DATA[card['key']]
    return f"{mob['name']} lvl {card.get('level', 1)}"


def duplicate_card_value(card):
    return card_convert_value(card)


def open_case_for_user(uid, case_type):
    u = users.find_one({"_id": uid})
    if not u:
        return False, "❌ Пользователь не найден.", None

    if u.get("cases", {}).get(case_type, 0) <= 0:
        return False, "❌ У вас нет этого кейса.", None

    cards = u.get("cards", [])
    if len(cards) >= u.get("card_storage_limit", CARD_MAX_STORAGE):
        return False, "❌ Нет места в хранилище. Освободите слот.", None

    # Simple weighted roll
    roll_pool = [
        ("mob", "assassin"), ("mob", "vampire"), ("mob", "skeleton"), ("mob", "crow"), ("mob", "goblin"),
        ("mob", "lizard"), ("mob", "gargoyle"), ("mob", "red_dragon"), ("mob", "ice_dragon"), ("mob", "yeti"),
        ("mob", "golem"), ("mob", "demon")
    ]
    if case_type == "epic":
        roll_pool = [("mob", key) for key in ["red_dragon", "ice_dragon", "yeti", "golem", "demon"]]
    elif case_type == "rare":
        roll_pool = [("mob", key) for key in ["lizard", "gargoyle", "red_dragon", "ice_dragon"]]

    kind, key = random.choice(roll_pool)
    card = create_card_instance(kind, key)

    duplicate = None
    for existing in cards:
        if existing.get("kind") == kind and existing.get("key") == key:
            duplicate = existing
            break

    if duplicate:
        value = duplicate_card_value(duplicate)
        users.update_one({"_id": uid}, {"$inc": {"snow_jewels": value}, "$inc": {f"cases.{case_type}": -1}})
        return True, f"🔁 <b>Повторка!</b> Вы уже имели карту <b>{card_name(duplicate)}</b>\n\n+{value} {SNOW_EMOJI} за дубликат", duplicate

    users.update_one({"_id": uid}, {"$inc": {f"cases.{case_type}": -1}, "$push": {"cards": card}})
    return True, f"🎉 Из кейса выпала карта!\n\n{card_name(card)}", card


# ============================================================
# Currency & cases
# ============================================================

SNOW_EMOJI = "💠"
CASE_TYPES = {
    "common": {"name": "⚪ Обычный кейс", "emoji": "📦"},
    "rare": {"name": "🔵 Редкий кейс", "emoji": "🎁"},
    "epic": {"name": "⚫️ Эпический кейс", "emoji": "💠"},
}
CASE_PRICES = {"common": 175, "rare": 275, "epic": 400}


def buy_case_for_user(uid, case_type):
    u = users.find_one({"_id": uid})
    if not u:
        return False, "❌ Пользователь не найден."

    price = CASE_PRICES.get(case_type)
    if price is None:
        return False, "❌ Некорректный тип кейса."

    if float(u.get("balance", 0)) < price:
        return False, f"❌ Недостаточно ICE. Нужно {price} ICE."

    res = users.update_one({"_id": uid, "balance": {"$gte": price}}, {"$inc": {"balance": -price, f"cases.{case_type}": 1}})
    if res.modified_count == 0:
        return False, "❌ Не удалось купить кейс."

    return True, f"🛒 Кейс {CASE_TYPES[case_type]['emoji']} {CASE_TYPES[case_type]['name']} куплен за {price} ICE"


# ============================================================
# Inventory / NFT items
# ============================================================

RARITY_EMOJI = {"common": "⚪", "rare": "🔵", "epic": "🟣", "legendary": "🟡"}


def make_item(name, desc, item_type="item", rarity="rare", item_key=None, file_id=None, emoji="🎁"):
    return {
        "name": name,
        "desc": desc,
        "type": item_type,
        "rarity": rarity,
        "item_key": item_key,
        "emoji": emoji,
        "file_id": file_id,
        "date": int(time.time())
    }


# ============================================================
# Clan system
# ============================================================


def clan_payload_from_user(uid):
    u = users.find_one({"_id": uid})
    clan_id = u.get("clan_id") if u else None
    if not clan_id:
        return None
    return clans.find_one({"_id": clan_id})


def valid_clan_name(name):
    return bool(name and len(name.strip()) >= 3 and len(name.strip()) <= 25)


def valid_clan_description(desc):
    return bool(desc and len(desc.strip()) <= 180)


@bot.message_handler(commands=["balance", "bal"])
def cmd_balance(m):
    u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    text = (
        f"💰 <b>Баланс</b>\n\n"
        f"ICE: <b>{fmt(u.get('balance', 0))}</b>\n"
        f"{SNOW_EMOJI} Jewels: <b>{fmt(u.get('snow_jewels', 0))}</b>\n"
        f"🏆 Burn Rank: <b>{get_burn_rank(float(u.get('total_burned', 0.0)))[0]}</b>"
    )
    bot.send_message(m.chat.id, text, parse_mode="HTML")


@bot.message_handler(commands=["start"])
def start(m):
    if m.chat.type != "private":
        return

    uid = m.from_user.id
    u = get_user(uid, m.from_user.username, m.from_user.first_name)

    if not is_valid_nickname(u.get("nickname", "")):
        bot.send_message(
            m.chat.id,
            "👤 <b>Выберите никнейм</b>\n\n"
            "Правила:\n"
            "- от 3 до 20 символов\n"
            "- только буквы, цифры и _\n"
            "- без нежелательного контента\n\n"
            "Пример: <code>IceUser_01</code>",
            parse_mode="HTML",
            reply_markup=types.ReplyKeyboardRemove()
        )
        msg = bot.send_message(m.chat.id, "Введите ваш никнейм:")
        bot.register_next_step_handler(msg, handle_nickname_setup)
        return

    bot.send_message(
        m.chat.id,
        "✅ <b>ICEFarm</b> запущен.\n\n"
        "Воспользуйтесь меню или командами:\n"
        "- /profile\n"
        "- /balance\n"
        "- /clan\n"
        "- /vip\n",
        parse_mode="HTML",
        reply_markup=create_main_keyboard()
    )


def handle_nickname_setup(m):
    name = normalize_nickname(m.text)
    if not is_valid_nickname(name):
        msg = bot.send_message(m.chat.id, "❌ Неверный никнейм. Попробуйте ещё раз.\nПример: IceUser_01")
        bot.register_next_step_handler(msg, handle_nickname_setup)
        return

    users.update_one({"_id": m.from_user.id}, {"$set": {"nickname": name, "username": name, "needs_nickname_setup": False}})
    bot.send_message(m.chat.id, f"✅ Никнейм установлен: <b>{name}</b>", parse_mode="HTML")


# ============================================================
# Main keyboard
# ============================================================


def create_main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("⛏ Фарм", "💰 Баланс")
    kb.add("🎁 Кейсы", "🧬 Character")
    kb.add("👤 Профиль", "⚗️ Крафт")
    kb.add("🛡 Клан", "💎 VIP")
    kb.add("🔥 Сжечь ICE")
    return kb


@bot.message_handler(func=lambda m: m.text in ["💰 Баланс", "/balance"])
def balance_button(m):
    cmd_balance(m)


@bot.message_handler(func=lambda m: m.text in ["👤 Профиль", "/profile"])
def profile_command(m):
    target = None
    if m.reply_to_message:
        target = m.reply_to_message.from_user.id
    else:
        parts = m.text.split(maxsplit=1)
        if len(parts) > 1:
            target_name = parts[1].strip()
            target = user_by_nickname(target_name)
            if target:
                target = target["_id"]

    if not target:
        target = m.from_user.id

    show_profile(target, m.chat.id, m.message_thread_id)


def show_profile(uid, chat_id, thread_id=None):
    u = users.find_one({"_id": uid})
    if not u:
        bot.send_message(chat_id, "❌ Профиль не найден.", message_thread_id=thread_id)
        return

    rank_name, rank_emoji = get_burn_rank(float(u.get("total_burned", 0.0)))
    clan = None
    if u.get("clan_id"):
        clan = clans.find_one({"_id": u["clan_id"]})

    clan_text = "Нет клана"
    if clan:
        clan_text = f"{clan.get('name')}"

    text = (
        f"╔══ <b>ПРОФИЛЬ</b> ══╗\n"
        f"┃ Ник: <b>{safe_text(u.get('nickname', u.get('username', '')))}</b>\n"
        f"┃ Имя: <b>{safe_text(u.get('first_name', ''))}</b>\n"
        f"┃ Клан: <b>{safe_text(clan_text)}</b>\n"
        f"┃ ICE: <b>{fmt(u.get('balance', 0))}</b>\n"
        f"┃ {SNOW_EMOJI} Jewels: <b>{fmt(u.get('snow_jewels', 0))}</b>\n"
        f"┃ Уровень: <b>{u.get('level', 1)}</b>\n"
        f"┃ Победы: <b>{u.get('wins', 0)}</b>\n"
        f"┃ Burn: <b>{rank_name}</b> {rank_emoji}\n"
        f"╚══════════════╝"
    )
    bot.send_message(chat_id, text, parse_mode="HTML", message_thread_id=thread_id)


@bot.message_handler(commands=["profile"])
def profile_explicit(m):
    profile_command(m)


# ============================================================
# VIP menu
# ============================================================

@bot.message_handler(func=lambda m: m.text in ["💎 VIP", "/vip"])
def vip_menu(m):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("💎 Купить VIP", url="https://t.me/herozvz"))
    kb.add(types.InlineKeyboardButton("✅ Проверить VIP", callback_data="check_vip"))
    bot.send_message(
        m.chat.id,
        "💎 <b>VIP</b>\n\nКупить VIP — откроется чат с @herozvz.\n\nТакже в сезонном пропуске есть бонусы для VIP.",
        parse_mode="HTML",
        reply_markup=kb
    )


@bot.callback_query_handler(func=lambda c: c.data == "check_vip")
def check_vip_cb(c):
    u = users.find_one({"_id": c.from_user.id})
    if u and u.get("is_vip"):
        bot.answer_callback_query(c.id, "✅ У вас активирован VIP")
    else:
        bot.answer_callback_query(c.id, "❌ VIP не активирован")


# ============================================================
# Game helpers: farming / burn / cases
# ============================================================

@bot.message_handler(func=lambda m: m.text in ["⛏ Фарм", "/farm"])
def farm_button(m):
    u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    farm_cd = 10800
    now = int(time.time())
    last = u.get("farm", 0)
    if now - last < farm_cd:
        remaining = farm_cd - (now - last)
        bot.send_message(m.chat.id, f"⏳ Фарм готов через <b>{remaining // 60} минут</b>", parse_mode="HTML")
        return

    gain = round(float(u.get("level", 1)) * 0.5 + random.uniform(0.1, 1.0), 2)
    if u.get("is_vip"):
        gain += round(gain * 0.15, 2)

    users.update_one({"_id": m.from_user.id}, {"$set": {"farm": now}, "$inc": {"balance": gain}})
    check_season_pass(m.from_user.id)

    if u.get("farm_nft_gif"):
        try:
            bot.send_animation(m.chat.id, u["farm_nft_gif"], caption=f"📦 <b>Фарм отчёт</b>\n✅ +{fmt(gain)} ICE", parse_mode="HTML")
            return
        except Exception:
            pass

    bot.send_message(m.chat.id, f"✅ <b>Фарм завершён</b>\n+{fmt(gain)} ICE", parse_mode="HTML")


@bot.message_handler(commands=["burn"])
@bot.message_handler(func=lambda m: m.text == "🔥 Сжечь ICE")
def burn_command(m):
    if m.text == "🔥 Сжечь ICE":
        parts = ["/burn"]
    else:
        parts = m.text.split()

    if len(parts) < 2:
        u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
        burned = float(u.get("total_burned", 0.0))
        rank_name, rank_emoji = get_burn_rank(burned)
        bot.send_message(
            m.chat.id,
            f"🔥 <b>Сжигание ICE</b>\n\n"
            f"Всего сожжено: <b>{fmt(burned)}</b>\n"
            f"Ранг: <b>{rank_name}</b> {rank_emoji}\n\n"
            f"Пример: <code>/burn 100</code>",
            parse_mode="HTML"
        )
        return

    try:
        amount = float(parts[1].replace(",", "."))
    except ValueError:
        bot.send_message(m.chat.id, "❌ Некорректная сумма", parse_mode="HTML")
        return

    u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if float(u.get("balance", 0)) < amount:
        bot.send_message(m.chat.id, "❌ Недостаточно ICE", parse_mode="HTML")
        return

    new_burned = float(u.get("total_burned", 0)) + amount
    users.update_one({"_id": m.from_user.id}, {"$inc": {"balance": -amount, "total_burned": amount}, "$set": {"burn_rank": get_burn_rank(new_burned)[0]}})
    check_season_pass(m.from_user.id)
    rank_name, rank_emoji = get_burn_rank(new_burned)
    bot.send_message(m.chat.id, f"🔥 <b>Сожжено {fmt(amount)} ICE</b>\nРанг: <b>{rank_name}</b> {rank_emoji}", parse_mode="HTML")


@bot.message_handler(commands=["cases", "case"])
def case_menu(m):
    u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    cases = u.get("cases", {"common": 0, "rare": 0, "epic": 0})
    kb = types.InlineKeyboardMarkup(row_width=1)
    for key, info in CASE_TYPES.items():
        cnt = cases.get(key, 0)
        btn = types.InlineKeyboardButton(f"{info['emoji']} {info['name']} ({cnt})", callback_data=f"case_open_{key}")
        kb.add(btn)
        kb.add(types.InlineKeyboardButton(f"🛒 Купить {info['name']} ({CASE_PRICES[key]} ICE)", callback_data=f"case_buy_{key}"))
    bot.send_message(m.chat.id, "🎁 Ваши кейсы:", reply_markup=kb, parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data.startswith("case_buy_"))
def case_buy_callback(c):
    case_type = c.data.replace("case_buy_", "")
    ok, msg = buy_case_for_user(c.from_user.id, case_type)
    if ok:
        bot.answer_callback_query(c.id, "✅ Куплено")
    else:
        bot.answer_callback_query(c.id, "❌ Ошибка", show_alert=True)
    bot.send_message(c.message.chat.id, msg, parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data.startswith("case_open_"))
def case_open_callback(c):
    case_type = c.data.replace("case_open_", "")
    ok, msg, card = open_case_for_user(c.from_user.id, case_type)
    if ok and card:
        if card.get("kind") == "mob" and card.get("key"):
            bot.answer_callback_query(c.id, "🎉 Карта в инвентаре")
        else:
            bot.answer_callback_query(c.id, "🎁 Выпала карта")
        bot.send_message(c.message.chat.id, msg, parse_mode="HTML")
    else:
        bot.answer_callback_query(c.id, "❌ Не удалось открыть кейс", show_alert=True)
        bot.send_message(c.message.chat.id, msg, parse_mode="HTML")


# ============================================================
# Character storage = 5
# ============================================================

@bot.message_handler(func=lambda m: m.text in ["🧬 Character", "/character"])
def character_menu(m):
    u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    cards = u.get("cards", [])
    text = f"🧬 <b>Character</b> — {len(cards)}/{u.get('card_storage_limit', 5)} слотов\n\n"
    if not cards:
        text += "📭 Нет карт. Откройте кейс."
    else:
        for idx, card in enumerate(cards):
            text += f"{idx + 1}. {card_name(card)}\n"
    bot.send_message(m.chat.id, text, parse_mode="HTML")


# ============================================================
# Admin panel
# ============================================================

@bot.message_handler(commands=["admin"])
def admin_panel(m):
    if m.from_user.id != ADMIN_ID:
        return
    text = (
        "👑 <b>Админ-панель</b>\n\n"
        "- /give_nft\n"
        "- /add_season_reward\n"
        "- /admin_rewards\n"
        "- /reset_season_pass\n"
        "- /stats ID"
    )
    bot.send_message(m.chat.id, text, parse_mode="HTML")


@bot.message_handler(commands=["admin_rewards"])
def admin_rewards(m):
    if m.from_user.id != ADMIN_ID:
        return
    bot.send_message(m.chat.id, "🛠 Админские награды доступны через настраиваемый список season rewards. Используйте /add_season_reward", parse_mode="HTML")


@bot.message_handler(commands=["add_season_reward"])
def add_season_reward_start(m):
    if m.from_user.id != ADMIN_ID:
        return
    msg = bot.send_message(m.chat.id, "📝 Введите название награды:")
    bot.register_next_step_handler(msg, add_season_reward_name)


def add_season_reward_name(m):
    name = m.text.strip()
    msg = bot.send_message(m.chat.id, "🎁 Введите тип награды (ice, jewel, item, profile_photo, nft_gif, vip_bonus):")
    bot.register_next_step_handler(msg, add_season_reward_type, name)


def add_season_reward_type(m, reward_name):
    rtype = m.text.strip().lower()
    msg = bot.send_message(m.chat.id, "💎 Введите значение (например: 100 для ICE / 5 для Jewels / название предмета):")
    bot.register_next_step_handler(msg, add_season_reward_value, reward_name, rtype)


def add_season_reward_value(m, reward_name, rtype):
    value = m.text.strip()
    settings.update_one({"_id": "season_rewards"}, {"$push": {"items": {"name": reward_name, "type": rtype, "value": value}}}, upsert=True)
    bot.send_message(m.chat.id, f"✅ Награда добавлена: <b>{reward_name}</b> ({rtype})", parse_mode="HTML")


@bot.message_handler(commands=["reset_season_pass"])
def reset_season_pass(m):
    if m.from_user.id != ADMIN_ID:
        return
    users.update_many({}, {"$set": {"season_pass": {"season": "autumn", "level": 0, "rewards_claimed": []}}})
    bot.send_message(m.chat.id, "✅ Сезонный пропуск сброшен.", parse_mode="HTML")


# ============================================================
# NFT creation (admin)
# ============================================================

@bot.message_handler(commands=["give_nft"])
def start_nft_creation(m):
    if m.from_user.id != ADMIN_ID:
        return
    msg = bot.reply_to(m, "👤 Введите ID игрока, которому выдать NFT:")
    bot.register_next_step_handler(msg, get_nft_target)


def get_nft_target(m):
    try:
        target_id = int(m.text)
        msg = bot.send_message(m.chat.id, "🖼 Пришлите фото/гиф/видео или вставьте ссылку:")
        bot.register_next_step_handler(msg, get_nft_media, target_id)
    except Exception:
        bot.send_message(m.chat.id, "❌ ID должен быть числом.")


def get_nft_media(m, target_id):
    file_id = None
    file_type = None
    if m.content_type == "photo":
        file_id = m.photo[-1].file_id
        file_type = "photo"
    elif m.content_type == "animation":
        file_id = m.animation.file_id
        file_type = "animation"
    elif m.content_type == "video":
        file_id = m.video.file_id
        file_type = "video"
    elif m.content_type in ["text"]:
        file_id = m.text.strip()
        file_type = "link"

    if not file_id:
        bot.send_message(m.chat.id, "❌ Это не медиа и не ссылка.")
        return

    msg = bot.send_message(m.chat.id, "🏷 Введите название предмета:")
    bot.register_next_step_handler(msg, get_nft_name, target_id, file_id, file_type)


def get_nft_name(m, target_id, file_id, file_type):
    name = m.text.strip()
    msg = bot.send_message(m.chat.id, "📝 Введите описание (или Пропустить):")
    bot.register_next_step_handler(msg, final_nft_step, target_id, file_id, file_type, name)


def final_nft_step(m, target_id, file_id, file_type, name):
    desc = m.text if m.text != "Пропустить" else ""
    nft = {
        "name": name,
        "desc": desc,
        "file_id": file_id,
        "type": file_type,
        "date": int(time.time()),
        "rarity": "rare",
        "item_key": "admin_nft",
    }
    users.update_one({"_id": target_id}, {"$push": {"inventory": nft}})
    bot.send_message(m.chat.id, f"✅ NFT <b>{name}</b> выдано!", parse_mode="HTML")


# ============================================================
# Clan management
# ============================================================

@bot.message_handler(func=lambda m: m.text in ["🛡 Клан", "/clan"])
def clan_menu(m):
    u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    clan = clans.find_one({"_id": u.get("clan_id")}) if u.get("clan_id") else None

    if not clan:
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("🆕 Создать клан (50 Jewels)", callback_data="clan_create"))
        kb.add(types.InlineKeyboardButton("🔗 Присоединиться по ссылке", callback_data="clan_join_link"))
        bot.send_message(m.chat.id, "🛡 <b>Клан</b>\n\nУ вас пока нет клана.", parse_mode="HTML", reply_markup=kb)
        return

    members = clan.get("members", [])
    text = (
        f"🛡 <b>{clan.get('name')}</b>\n"
        f"{clan.get('description', '')}\n\n"
        f"👥 Участников: {len(members)}\n"
        f"🧑‍💼 Лидер: {clan.get('leader_name')}"
    )
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📥 Заявки", callback_data=f"clan_manage_requests_{clan['_id']}"))
    kb.add(types.InlineKeyboardButton("⚙️ Настройки", callback_data=f"clan_manage_settings_{clan['_id']}"))
    kb.add(types.InlineKeyboardButton("🔗 Ссылки для вступления", callback_data=f"clan_manage_links_{clan['_id']}"))
    bot.send_message(m.chat.id, text, parse_mode="HTML", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data == "clan_create")
def clan_create_start(c):
    u = users.find_one({"_id": c.from_user.id})
    if u.get("clan_id"):
        bot.answer_callback_query(c.id, "❌ У вас уже есть клан")
        return

    if float(u.get("snow_jewels", 0)) < 50:
        bot.answer_callback_query(c.id, "❌ Нужно 50 Jewels", show_alert=True)
        return

    users.update_one({"_id": c.from_user.id}, {"$inc": {"snow_jewels": -50}})
    msg = bot.send_message(c.message.chat.id, "🖼 Отправьте изображение или гифку для клана. Можно пропустить, отправив /skip")
    bot.register_next_step_handler(msg, clan_step_icon)
    bot.answer_callback_query(c.id)


def clan_step_icon(m):
    file_id = None
    file_type = None
    if m.content_type == "photo":
        file_id = m.photo[-1].file_id
        file_type = "photo"
    elif m.content_type == "animation":
        file_id = m.animation.file_id
        file_type = "animation"
    elif m.text and m.text.strip() == "/skip":
        file_id = None
        file_type = "none"
    else:
        msg = bot.send_message(m.chat.id, "❌ Неверный формат. Пришлите фото/гифку или /skip")
        bot.register_next_step_handler(msg, clan_step_icon)
        return

    msg = bot.send_message(m.chat.id, "🏷 Введите название клана:")
    bot.register_next_step_handler(msg, clan_step_name, file_id, file_type)


def clan_step_name(m, file_id, file_type):
    name = m.text.strip()
    if not valid_clan_name(name):
        msg = bot.send_message(m.chat.id, "❌ Название должно быть от 3 до 25 символов. Попробуйте ещё раз:")
        bot.register_next_step_handler(msg, clan_step_name, file_id, file_type)
        return

    msg = bot.send_message(m.chat.id, "📝 Введите описание клана:")
    bot.register_next_step_handler(msg, clan_step_desc, name, file_id, file_type)


def clan_step_desc(m, name, file_id, file_type):
    desc = m.text.strip()
    if not valid_clan_description(desc):
        msg = bot.send_message(m.chat.id, "❌ Описание должно быть до 180 символов. Попробуйте ещё раз:")
        bot.register_next_step_handler(msg, clan_step_desc, name, file_id, file_type)
        return

    clan_id = uuid.uuid4().hex[:8]
    data = {
        "_id": clan_id,
        "name": name,
        "description": desc,
        "icon": file_id,
        "icon_type": file_type,
        "leader_uid": m.from_user.id,
        "leader_name": m.from_user.first_name,
        "members": [{"uid": m.from_user.id, "name": m.from_user.first_name, "role": "leader"}],
        "join_links": [],
        "join_requests": [],
        "created_at": int(time.time())
    }
    clans.insert_one(data)
    users.update_one({"_id": m.from_user.id}, {"$set": {"clan_id": clan_id}})
    bot.send_message(m.chat.id, f"✅ Клан <b>{name}</b> создан!", parse_mode="HTML")


@bot.message_handler(commands=["clan_join"])
def clan_join_command(m):
    if len(m.text.split()) < 2:
        bot.send_message(m.chat.id, "💡 Формат: /clan_join LINK_ID", parse_mode="HTML")
        return
    link_id = m.text.split()[1]
    clan = clans.find_one({"join_links.link_id": link_id})
    if not clan:
        bot.send_message(m.chat.id, "❌ Ссылка не найдена.", parse_mode="HTML")
        return
    u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if u.get("clan_id"):
        bot.send_message(m.chat.id, "❌ Вы уже состоите в клане.", parse_mode="HTML")
        return
    clans.update_one({"_id": clan["_id"]}, {"$push": {"join_requests": {"uid": m.from_user.id, "name": m.from_user.first_name}}})
    bot.send_message(m.chat.id, f"✅ Заявка на вступление отправлена в клан <b>{clan['name']}</b>", parse_mode="HTML")


# ============================================================
# Manage join links / settings
# ============================================================

@bot.callback_query_handler(func=lambda c: c.data.startswith("clan_manage_settings_"))
def clan_manage_settings(c):
    clan_id = c.data.replace("clan_manage_settings_", "")
    clan = clans.find_one({"_id": clan_id})
    if not clan or clan["leader_uid"] != c.from_user.id:
        bot.answer_callback_query(c.id, "❌ Нет прав")
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("🖼 Сменить фото", callback_data=f"clan_set_icon_{clan_id}"))
    kb.add(types.InlineKeyboardButton("✏️ Изменить название (30 Jewels)", callback_data=f"clan_set_name_{clan_id}"))
    kb.add(types.InlineKeyboardButton("📝 Изменить описание", callback_data=f"clan_set_desc_{clan_id}"))
    bot.edit_message_text("⚙️ Управление кланом", c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("clan_set_name_"))
def clan_set_name_start(c):
    clan_id = c.data.replace("clan_set_name_", "")
    msg = bot.send_message(c.message.chat.id, "🏷 Введите новое название клана:")
    bot.register_next_step_handler(msg, clan_set_name_done, clan_id)
    bot.answer_callback_query(c.id)


def clan_set_name_done(m, clan_id):
    name = m.text.strip()
    if not valid_clan_name(name):
        bot.send_message(m.chat.id, "❌ Некорректное название.")
        return
    clan = clans.find_one({"_id": clan_id})
    if clan and clan["leader_uid"] == m.from_user.id:
        clans.update_one({"_id": clan_id}, {"$set": {"name": name}})
        bot.send_message(m.chat.id, f"✅ Название изменено на <b>{name}</b>", parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data.startswith("clan_set_desc_"))
def clan_set_desc_start(c):
    clan_id = c.data.replace("clan_set_desc_", "")
    msg = bot.send_message(c.message.chat.id, "📝 Введите новое описание:")
    bot.register_next_step_handler(msg, clan_set_desc_done, clan_id)
    bot.answer_callback_query(c.id)


def clan_set_desc_done(m, clan_id):
    desc = m.text.strip()
    if not valid_clan_description(desc):
        bot.send_message(m.chat.id, "❌ Описание не подходит.")
        return
    clan = clans.find_one({"_id": clan_id})
    if clan and clan["leader_uid"] == m.from_user.id:
        clans.update_one({"_id": clan_id}, {"$set": {"description": desc}})
        bot.send_message(m.chat.id, "✅ Описание изменено.", parse_mode="HTML")


@bot.callback_query_handler(func=lambda c: c.data.startswith("clan_set_icon_"))
def clan_set_icon_start(c):
    clan_id = c.data.replace("clan_set_icon_", "")
    msg = bot.send_message(c.message.chat.id, "🖼 Пришлите новое фото/гифку для клана:")
    bot.register_next_step_handler(msg, clan_set_icon_done, clan_id)
    bot.answer_callback_query(c.id)


def clan_set_icon_done(m, clan_id):
    if m.content_type == "photo":
        file_id = m.photo[-1].file_id
    elif m.content_type == "animation":
        file_id = m.animation.file_id
    else:
        bot.send_message(m.chat.id, "❌ Неверный формат.")
        return
    clan = clans.find_one({"_id": clan_id})
    if clan and clan["leader_uid"] == m.from_user.id:
        clans.update_one({"_id": clan_id}, {"$set": {"icon": file_id}})
        bot.send_message(m.chat.id, "✅ Иконка клана обновлена.")


@bot.callback_query_handler(func=lambda c: c.data.startswith("clan_manage_links_"))
def clan_manage_links(c):
    clan_id = c.data.replace("clan_manage_links_", "")
    clan = clans.find_one({"_id": clan_id})
    if not clan or clan["leader_uid"] != c.from_user.id:
        bot.answer_callback_query(c.id, "❌ Нет прав")
        return

    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, link in enumerate(clan.get("join_links", []), 1):
        kb.add(types.InlineKeyboardButton(f"🗑 Удалить {link['link_id']}", callback_data=f"clan_del_link_{clan_id}_{link['link_id']}"))
    kb.add(types.InlineKeyboardButton("➕ Создать ссылку", callback_data=f"clan_add_link_{clan_id}"))
    bot.edit_message_text("🔗 Ссылки для вступления", c.message.chat.id, c.message.message_id, reply_markup=kb)
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("clan_add_link_"))
def clan_add_link(c):
    clan_id = c.data.replace("clan_add_link_", "")
    clan = clans.find_one({"_id": clan_id})
    if clan and clan["leader_uid"] == c.from_user.id:
        link_id = uuid.uuid4().hex[:6]
        link = f"https://t.me/{(bot.get_me()).username}?start=clan_{link_id}"
        clans.update_one({"_id": clan_id}, {"$push": {"join_links": {"link_id": link_id, "link": link}}})
        bot.send_message(c.message.chat.id, f"✅ Ссылка создана: <code>{link}</code>", parse_mode="HTML")
    bot.answer_callback_query(c.id)


@bot.callback_query_handler(func=lambda c: c.data.startswith("clan_del_link_"))
def clan_del_link(c):
    _, clan_id, link_id = c.data.split("_", 3)
    clan = clans.find_one({"_id": clan_id})
    if clan and clan["leader_uid"] == c.from_user.id:
        clans.update_one({"_id": clan_id}, {"$pull": {"join_links": {"link_id": link_id}}})
        bot.send_message(c.message.chat.id, "✅ Ссылка удалена.", parse_mode="HTML")
    bot.answer_callback_query(c.id)


# ============================================================
# Nickname changer / ID changer
# ============================================================

@bot.message_handler(commands=["change_nick"])
def change_nick_cmd(m):
    u = users.find_one({"_id": m.from_user.id})
    if u.get("id_changer", 0) <= 0:
        bot.send_message(m.chat.id, "❌ У вас нет ID Changer. Используйте предмет из инвентаря.")
        return
    msg = bot.send_message(m.chat.id, "✏️ Введите новый никнейм (3-20 символов, только letters/digits/_):")
    bot.register_next_step_handler(msg, set_new_nick)


def set_new_nick(m):
    name = normalize_nickname(m.text)
    if not is_valid_nickname(name):
        msg = bot.send_message(m.chat.id, "❌ Ник недействителен. Попробуйте ещё раз:")
        bot.register_next_step_handler(msg, set_new_nick)
        return

    users.update_one({"_id": m.from_user.id}, {"$set": {"nickname": name, "username": name}, "$inc": {"id_changer": -1}})
    bot.send_message(m.chat.id, f"✅ Никнейм изменён на <b>{name}</b>", parse_mode="HTML")


# ============================================================
# inventory interaction shortcuts
# ============================================================

@bot.message_handler(commands=["inventory", "inv"])
def inventory_menu(m):
    u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    inv = u.get("inventory", [])
    if not inv:
        bot.send_message(m.chat.id, "📭 Инвентарь пуст.")
        return

    text = "🎒 Инвентарь:\n\n"
    for i, item in enumerate(inv):
        name = item.get("name", "Предмет")
        desc = item.get("desc", "")
        text += f"{i + 1}. <b>{name}</b>\n{desc}\n\n"
        if item.get("item_key") == "id_changer":
            text += "💡 Используйте /change_nick\n\n"
    bot.send_message(m.chat.id, text, parse_mode="HTML")


# ============================================================
# Complex seasonal pass: preview
# ============================================================

@bot.message_handler(commands=["season", "pass"])
def season_pass_menu(m):
    total = float(get_user(m.from_user.id, m.from_user.username, m.from_user.first_name).get("total_burned", 0.0))
    season = get_pass_data("autumn")
    progress = season_pass_level_for_total(total)
    text = "🎃 <b>Season Autumn</b>\n\n"
    for level in season["levels"]:
        done = total >= level["threshold"]
        marker = "✅" if done else "⬜"
        text += f"{marker} lvl {level['id']} — {level['name']} ({level['threshold']} burned)\n"
    text += f"\nТекущий прогресс: <b>{total}</b> burned"
    bot.send_message(m.chat.id, text, parse_mode="HTML")


# ============================================================
# Generic Telegram webhook handlers
# ============================================================

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    try:
        json_data = request.get_json(force=True)
        update = telebot.types.Update.de_json(json_data)
        bot.process_new_updates([update])
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.exception(e)
        return jsonify({"status": "error"}), 200


@app.route("/")
def index():
    return jsonify({"status": "online", "bot": "ICEFarm", "version": "1.1.07"})


@app.route("/set_webhook")
def set_webhook_route():
    try:
        bot.remove_webhook()
        time.sleep(1)
        result = bot.set_webhook(url=f"{WEBHOOK}/{TOKEN}")
        return jsonify({"webhook_set": result})
    except Exception as e:
        logger.exception(e)
        return jsonify({"error": str(e)}), 500


# ============================================================
# unknown handler
# ============================================================

@bot.message_handler(func=lambda m: True)
def unknown_command(m):
    if m.chat.type == "private":
        bot.reply_to(m, "❓ Неизвестная команда. Используйте меню или /start")


# ============================================================
# run
# ============================================================
if __name__ == "__main__":
    if WEBHOOK and "http" in WEBHOOK:
        try:
            bot.remove_webhook()
            time.sleep(1)
            bot.set_webhook(url=f"{WEBHOOK}/{TOKEN}")
            port = int(os.environ.get("PORT", 10000))
            app.run(host="0.0.0.0", port=port)
        except Exception as e:
            logger.exception(e)
            app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
    else:
        bot.remove_webhook()
        bot.infinity_polling()
