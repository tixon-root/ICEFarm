import os
import re
import time
import random
import uuid
import threading
import hmac
import hashlib
import json
from flask import Flask, request, jsonify
import telebot
from telebot import types
from pymongo import MongoClient
from dotenv import load_dotenv
from bson.objectid import ObjectId
from urllib.parse import parse_qsl, unquote
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

# ================================================================
# НОВОЕ: КИРКИ — предметы, находимые во время фарма
# ================================================================

# НОВОЕ v3: VIP-гиф фарма удалена навсегда — теперь гифка фарма это NFT-предмет (активируется в инвентаре)
VIP_FARM_BONUS_PCT = 0.15  # VIP бонус = 15% от базового (обычного) фарма

PICK_DATA = {
    "golden_pick": {
        "name": "Golden Pick", "legendary_name": "Legendary Golden Pick",
        "emoji": "⛏️🟡",
        "desc": "+10% ICE от добытого айса за фарм.",
        "legendary_desc": "+20% ICE от добытого айса за фарм (x2 от обычной версии).",
        "bonus_pct": 0.10, "legendary_bonus_pct": 0.20,
    },
    "fire_pick": {
        "name": "Fire Pick", "legendary_name": "Legendary Fire Pick",
        "emoji": "⛏️🔥",
        "desc": "Уменьшает время фарма на 45 минут (у обоих фармов — ICE и Snow Jewel).",
        "legendary_desc": "Уменьшает время фарма на 90 минут у обоих фармов (x2 от обычной версии).",
        "cd_reduction": 2700, "legendary_cd_reduction": 5400,
    },
    "swift_pick": {
        "name": "Swift Pick", "legendary_name": "Legendary Swift Pick",
        "emoji": "⛏️💨",
        "desc": "Даёт больше айсов на 20% за фарм.",
        "legendary_desc": "Даёт больше айсов на 40% за фарм (x2 от обычной версии).",
        "bonus_pct": 0.20, "legendary_bonus_pct": 0.40,
    },
}

PICK_FIND_CHANCES = {
    "golden_pick": 0.01,
    "fire_pick":   0.01,
    "swift_pick":  0.008,
}

ICE_PILE_CHANCE = 0.01
ICE_PILE_MIN = 200
ICE_PILE_MAX = 1000

LEGENDARY_ITEM_COST  = 4
LEGENDARY_JEWEL_COST = 10

def count_owned_pick(inv, pick_key, legendary=False):
    return sum(1 for it in inv if it.get("pick_key") == pick_key and bool(it.get("legendary")) == legendary)

def get_owned_pick(inv, pick_key):
    has_normal = count_owned_pick(inv, pick_key, False) > 0
    has_legendary = count_owned_pick(inv, pick_key, True) > 0
    return has_normal, has_legendary

def get_pick_bonus_pct(inv):
    """Суммарный % бонуса к добыче ICE от кирок в инвентаре + строки для сообщения."""
    total = 0.0
    lines = []
    for key in ("golden_pick", "swift_pick"):
        data = PICK_DATA[key]
        has_normal, has_legendary = get_owned_pick(inv, key)
        if has_legendary:
            total += data["legendary_bonus_pct"]
            lines.append(f"💠 <i>{data['legendary_name']}</i>: +{int(data['legendary_bonus_pct']*100)}% ICE")
        elif has_normal:
            total += data["bonus_pct"]
            lines.append(f"💠 <i>{data['name']}</i>: +{int(data['bonus_pct']*100)}% ICE")
    return total, lines

def get_pick_cd_reduction(inv):
    has_normal, has_legendary = get_owned_pick(inv, "fire_pick")
    if has_legendary:
        return PICK_DATA["fire_pick"]["legendary_cd_reduction"]
    if has_normal:
        return PICK_DATA["fire_pick"]["cd_reduction"]
    return 0

def roll_farm_find(uid, inv):
    """Проверяет находки во время фарма: кирки (в инвентарь) и кучки льда (сразу в баланс)."""
    found_lines = []
    bonus_ice = 0.0
    for key, chance in PICK_FIND_CHANCES.items():
        if random.random() < chance:
            item = {
                "name": PICK_DATA[key]["name"],
                "pick_key": key,
                "legendary": False,
                "type": "item",
                "rarity": "epic",
                "desc": PICK_DATA[key]["desc"],
                "date": int(time.time()),
            }
            users.update_one({"_id": uid}, {"$push": {"inventory": item}})
            inv.append(item)
            found_lines.append(f"🎉 Находка: {PICK_DATA[key]['emoji']} {PICK_DATA[key]['name']}")
    if random.random() < ICE_PILE_CHANCE:
        pile = round(random.uniform(ICE_PILE_MIN, ICE_PILE_MAX), 1)
        bonus_ice += pile
        found_lines.append(f"🎉 Находка: +{fmt(pile)} ICE")
    return found_lines, bonus_ice

# --- Крафт рецепты ---
# НОВОЕ: убраны демо-рецепты ("Ледяной Меч" и т.д.) — их ингредиенты нигде не
# выдавались игрокам, поэтому рецепты были нерабочими "заглушками". Реальные
# рецепты для NFT-предметов админ добавляет командой /add_recipe (см. ниже по
# файлу), а крафт легендарных кирок реализован отдельным механизмом (LEGENDARY_*).
CRAFT_RECIPES = {}

RARITY_EMOJI = {
    "rare":      "🔵",
    "epic":      "🟣",
    "legendary": "🟡",
}

# ================================================================
# НОВОЕ: SNOW JEWEL, КЕЙСЫ, ДОСТИЖЕНИЯ 4 УРОВНЯ, КАПСУЛЫ, КАРТЫ
# ================================================================

SNOW_EMOJI = "💠"  # Snow Jewel

# --- Типы кейсов ---
CASE_TYPES = {
    "common": {"name": "⚪ Обычный кейс",  "emoji": "📦"},
    "rare":   {"name": "🔵 Изумрудный кейс", "emoji": "🎁"},
    "epic":   {"name": "⚫️ Черный кейс",   "emoji": "💠"},
}

# --- Цены кейсов в ICE (покупка) ---
CASE_PRICES = {
    "common": 175,
    "rare":   275,
    "epic":   400,
}

def buy_case_for_user(uid, case_type):
    """Покупает 1 кейс типа case_type за ICE. Возвращает (ok, msg)."""
    price = CASE_PRICES.get(case_type)
    if price is None:
        return False, "❌ Неизвестный тип кейса."

    u = users.find_one({"_id": uid})
    if not u:
        return False, "❌ Пользователь не найден."

    balance = float(u.get("balance", 0))
    if balance < price:
        return False, f"❌ Недостаточно ICE!\nНужно: <b>{price} ICE</b>\nУ вас: <b>{fmt(balance)} ICE</b>"

    res = users.update_one(
        {"_id": uid, "balance": {"$gte": price}},
        {"$inc": {"balance": -price, f"cases.{case_type}": 1}}
    )
    if res.modified_count == 0:
        return False, "❌ Не удалось купить кейс, попробуйте ещё раз."

    return True, f"🛒 Вы купили {CASE_TYPES[case_type]['emoji']} <b>{CASE_TYPES[case_type]['name']}</b> за <b>{price} ICE</b>!"

# --- Тиры карт ---
# F = 1ур (базовый дроп), D=2, C=3, B=4, A=5, S=6 (бесконечная прокачка)
CARD_TIER_LABEL = {"F": 1, "D": 2, "C": 3, "B": 4, "A": 5, "S": 6}

# Названия и эмодзи редкости по тиру (6 редкостей = 6 тиров)
CARD_RARITY_NAME = {
    "F": "Обычная",
    "D": "Редкая",
    "C": "Ультра Редкая",
    "B": "Эпическая",
    "A": "Легендарный",
    "S": "Мифический",
}
CARD_RARITY_EMOJI = {
    "F": "⚡️",
    "D": "⭐",
    "C": "🌟",
    "B": "💥",
    "A": "🍁",
    "S": "🐦‍🔥",
}

def card_tier(card):
    """Возвращает букву тира (F..S) для инстанса карты."""
    if card["kind"] == "class":
        return "S"
    return CARD_DATA[card["key"]]["tier"]

# Диапазон урона "туда-сюда" по тиру. None = урон фиксированный, без разброса.
DMG_RANGE_BY_TIER = {"F": None, "D": 7, "C": 10, "B": 20, "A": 30, "S": 100}

# Каждый обычный моб (F-A) имеет 3 уровня прокачки (levels[0]=база из кейса, дальше апгрейды).
# cost = цена апгрейда В ICE, jewel_cost = доп. цена в Snow Jewel (0 если нет).
CARD_DATA = {
    # ---- F (уровень 1) ----
    "assassin": {"name": "Assassin", "tier": "F", "levels": [
        {"hp": 14, "dmg": 4,  "def": 0.10, "cost": 0,  "jewel_cost": 0},
        {"hp": 17, "dmg": 7,  "def": 0.12, "cost": 30, "jewel_cost": 0},
        {"hp": 21, "dmg": 12, "def": 0.14, "cost": 50, "jewel_cost": 0},
    ]},
    "vampire": {"name": "Vampire", "tier": "F", "levels": [
        {"hp": 25, "dmg": 16, "def": 0.12, "cost": 0,  "jewel_cost": 0},
        {"hp": 31, "dmg": 19, "def": 0.16, "cost": 80, "jewel_cost": 0},
        {"hp": 37, "dmg": 23, "def": 0.19, "cost": 90, "jewel_cost": 0},
    ]},
    "skeleton": {"name": "Skeleton", "tier": "F", "levels": [
        {"hp": 30, "dmg": 19, "def": 0.12, "cost": 0,  "jewel_cost": 0},
        {"hp": 36, "dmg": 24, "def": 0.18, "cost": 80, "jewel_cost": 0},
        {"hp": 41, "dmg": 28, "def": 0.21, "cost": 90, "jewel_cost": 0},
    ]},
    "crow": {"name": "Crow", "tier": "F", "levels": [
        {"hp": 4,  "dmg": 1, "def": 0.08, "cost": 0,  "jewel_cost": 0},
        {"hp": 6,  "dmg": 3, "def": 0.10, "cost": 30, "jewel_cost": 0},
        {"hp": 10, "dmg": 5, "def": 0.14, "cost": 50, "jewel_cost": 0},
    ]},
    "mummy": {"name": "Mummy", "tier": "F", "levels": [
        {"hp": 11, "dmg": 4,  "def": 0.09, "cost": 0,  "jewel_cost": 0},
        {"hp": 15, "dmg": 7,  "def": 0.11, "cost": 70, "jewel_cost": 0},
        {"hp": 19, "dmg": 10, "def": 0.15, "cost": 90, "jewel_cost": 0},
    ]},
    "goblin": {"name": "Goblin", "tier": "F", "levels": [
        {"hp": 10, "dmg": 4, "def": 0.08, "cost": 0,  "jewel_cost": 0},
        {"hp": 14, "dmg": 6, "def": 0.12, "cost": 70, "jewel_cost": 0},
        {"hp": 18, "dmg": 9, "def": 0.15, "cost": 90, "jewel_cost": 0},
    ]},
    # ---- D (уровень 2) ----
    "lizard": {"name": "Lizard", "tier": "D", "levels": [
        {"hp": 69, "dmg": 35, "def": 0.23, "cost": 0,   "jewel_cost": 0},
        {"hp": 78, "dmg": 41, "def": 0.31, "cost": 300, "jewel_cost": 0},
        {"hp": 83, "dmg": 46, "def": 0.38, "cost": 400, "jewel_cost": 0},
    ]},
    "gargoyle": {"name": "Gargoyle", "tier": "D", "levels": [
        {"hp": 75, "dmg": 37, "def": 0.25, "cost": 0,   "jewel_cost": 0},
        {"hp": 81, "dmg": 40, "def": 0.29, "cost": 300, "jewel_cost": 0},
        {"hp": 92, "dmg": 47, "def": 0.35, "cost": 400, "jewel_cost": 0},
    ]},
    # ---- C (уровень 3) ----
    "red_dragon": {"name": "Red Dragon", "tier": "C", "levels": [
        {"hp": 135, "dmg": 80,  "def": 0.43, "cost": 0,   "jewel_cost": 0},
        {"hp": 148, "dmg": 97,  "def": 0.70, "cost": 500, "jewel_cost": 0},
        {"hp": 159, "dmg": 103, "def": 0.83, "cost": 600, "jewel_cost": 0},
    ]},
    "ice_dragon": {"name": "ICE Dragon", "tier": "C", "levels": [
        {"hp": 135, "dmg": 80,  "def": 0.40, "cost": 0,   "jewel_cost": 0},
        {"hp": 141, "dmg": 98,  "def": 0.62, "cost": 500, "jewel_cost": 0},
        {"hp": 152, "dmg": 111, "def": 0.70, "cost": 600, "jewel_cost": 0},
    ]},
    "regular_dragon": {"name": "Regular Dragon", "tier": "C", "levels": [
        {"hp": 130, "dmg": 76,  "def": 0.38, "cost": 0,   "jewel_cost": 0},
        {"hp": 150, "dmg": 91,  "def": 0.49, "cost": 500, "jewel_cost": 0},
        {"hp": 161, "dmg": 104, "def": 0.67, "cost": 600, "jewel_cost": 0},
    ]},
    # ---- B (уровень 4) ----
    "yeti": {"name": "Yeti", "tier": "B", "levels": [
        {"hp": 230, "dmg": 120, "def": 0.52, "cost": 0,   "jewel_cost": 0},
        {"hp": 248, "dmg": 137, "def": 0.75, "cost": 400, "jewel_cost": 0},
        {"hp": 264, "dmg": 151, "def": 0.85, "cost": 500, "jewel_cost": 0.5},
    ]},
    "golem": {"name": "Golem", "tier": "B", "levels": [
        {"hp": 225, "dmg": 135, "def": 0.53, "cost": 0,   "jewel_cost": 0},
        {"hp": 251, "dmg": 139, "def": 0.79, "cost": 400, "jewel_cost": 0},
        {"hp": 268, "dmg": 150, "def": 0.94, "cost": 500, "jewel_cost": 0.5},
    ]},
    "outhrus": {"name": "Outhrus", "tier": "B", "levels": [
        {"hp": 260, "dmg": 165, "def": 0.93, "cost": 0,   "jewel_cost": 0},
        {"hp": 279, "dmg": 177, "def": 1.00, "cost": 500, "jewel_cost": 0},
        {"hp": 288, "dmg": 189, "def": 1.20, "cost": 600, "jewel_cost": 0.5},
    ]},
    # ---- A (уровень 5) ----
    "demon": {"name": "Demon", "tier": "A", "levels": [
        {"hp": 460, "dmg": 210, "def": 1.1, "cost": 0,   "jewel_cost": 0},
        {"hp": 500, "dmg": 239, "def": 1.3, "cost": 800, "jewel_cost": 0},
        {"hp": 536, "dmg": 254, "def": 1.9, "cost": 800, "jewel_cost": 0.5},
    ]},
}

# ---- S (уровень 6, бесконечная прокачка) — Mage/Dist/Melle ----
# Только из Эпического (Чёрный) кейса. Одна картинка на все уровни, номер = обычная цифра (1,2,3...).
CLASS_DATA = {
    "mage": {"name": "Mage",  "hp": 300, "dmg": 240, "def": 2.0, "upgrade_cost": 700},
    "dist": {"name": "Dist",  "hp": 320, "dmg": 210, "def": 3.0, "upgrade_cost": 700},
    "melle": {"name": "Melle", "hp": 340, "dmg": 210, "def": 3.0, "upgrade_cost": 700},
}
CLASS_UPGRADE_STEP = {"hp": 10, "dmg": 10, "def": 0.20, "cost": 20}  # прибавка и рост цены за каждый апгрейд

def class_stats(class_key, level):
    """level >= 1. level 1 = базовые статы. Каждый след. уровень +10HP+10DM+0.20%DEF."""
    base = CLASS_DATA[class_key]
    n = max(level - 1, 0)
    return {
        "hp": base["hp"] + CLASS_UPGRADE_STEP["hp"] * n,
        "dmg": base["dmg"] + CLASS_UPGRADE_STEP["dmg"] * n,
        "def": round(base["def"] + CLASS_UPGRADE_STEP["def"] * n, 2),
    }

def class_upgrade_cost(class_key, current_level):
    """Цена перехода с current_level на current_level+1."""
    base = CLASS_DATA[class_key]["upgrade_cost"]
    n = max(current_level - 1, 0)
    return base + CLASS_UPGRADE_STEP["cost"] * n

# --- Box'ы (кейсы) -> вероятности тиров карт ---
# common = Железный, rare = Изумрудный, epic = Черный
BOX_TIER_WEIGHTS = {
    "common": {"F": 75, "D": 22, "C": 3},
    "rare":   {"F": 30, "D": 25, "C": 25, "B": 17, "A": 3},
    "epic":   {"F": 10, "D": 25, "C": 30, "B": 22, "A": 10, "S": 3},
}

TIER_TO_MOBS = {}
for _mob_key, _mob in CARD_DATA.items():
    TIER_TO_MOBS.setdefault(_mob["tier"], []).append(_mob_key)

def roll_case(case_type):
    """Возвращает ('mob', mob_key) или ('class', class_key) по вероятностям box'а."""
    weights = BOX_TIER_WEIGHTS[case_type]
    tiers = list(weights.keys())
    probs = list(weights.values())
    tier = random.choices(tiers, weights=probs, k=1)[0]
    if tier == "S":
        class_key = random.choice(list(CLASS_DATA.keys()))
        return ("class", class_key)
    mob_key = random.choice(TIER_TO_MOBS.get(tier, []))
    return ("mob", mob_key)

def new_card_id():
    return uuid.uuid4().hex[:10]

def create_card_instance(kind, key):
    """kind='mob'|'class'. Создаёт новую карточку в инвентаре игрока (level=1)."""
    return {"id": new_card_id(), "kind": kind, "key": key, "level": 1}

def card_stats(card):
    """Возвращает (display_name, hp, dmg, def_pct, dmg_range) для инстанса карты."""
    if card["kind"] == "class":
        s = class_stats(card["key"], card["level"])
        name = f"{CLASS_DATA[card['key']]['name']} {card['level']}"
        return name, s["hp"], s["dmg"], s["def"], DMG_RANGE_BY_TIER["S"]
    mob = CARD_DATA[card["key"]]
    lvl_idx = min(card["level"] - 1, len(mob["levels"]) - 1)
    lvl = mob["levels"][lvl_idx]
    roman = ["", "II", "III"][lvl_idx] if lvl_idx > 0 else ""
    name = f"{mob['name']}{(' ' + roman) if roman else ''}"
    return name, lvl["hp"], lvl["dmg"], lvl["def"], DMG_RANGE_BY_TIER[mob["tier"]]

def card_upgrade_cost(card):
    """Возвращает (ice_cost, jewel_cost, max_reached: bool) для след. апгрейда карты."""
    if card["kind"] == "class":
        return class_upgrade_cost(card["key"], card["level"]), 0, False
    mob = CARD_DATA[card["key"]]
    next_idx = card["level"]  # текущий level=1 -> апгрейд ведёт на levels[1]
    if next_idx >= len(mob["levels"]):
        return 0, 0, True
    nxt = mob["levels"][next_idx]
    return nxt["cost"], nxt.get("jewel_cost", 0), False

def card_convert_value(card):
    """Сколько Snow Jewel даёт разбор карты (= той же 'Ценности', что показана на карточке)."""
    if card["kind"] == "class":
        return EPIC_CLASS_TO_JEWEL
    mob = CARD_DATA[card["key"]]
    tier_level = CARD_TIER_LABEL[mob["tier"]]
    tier_base = CARD_TO_JEWEL.get(tier_level, 0)
    return round(tier_base * card["level"], 2)

def card_caption(card):
    """Единый формат подписи карточки: эмодзи редкости, имя, атака/хп курсивом, редкость курсивом, ценность."""
    name, hp, dmg, defp, rng = card_stats(card)
    tier = card_tier(card)
    rarity_emoji = CARD_RARITY_EMOJI.get(tier, "⚪️")
    rarity_name = CARD_RARITY_NAME.get(tier, "?")
    jewel_value = card_convert_value(card)
    return (
        f"{rarity_emoji} <b>{name}</b>\n\n"
        f"⚔️ Атака: <i>{dmg}</i>\n"
        f"❤️ Здоровье: <i>{hp}</i>\n"
        f"🎖 Редкость: <i>{rarity_name}</i>\n\n"
        f"💎 Ценность: {jewel_value} {SNOW_EMOJI}"
    )

# Папка с картинками карт лежит рядом с этим файлом (репо ICEFarm/Cards)
CARDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "Cards")

# Явное соответствие ключ карты -> базовое имя файла (совпадает с реальными файлами в /Cards)
MOB_IMAGE_NAME = {
    "assassin":       "Assassin",
    "vampire":        "Vampire",
    "skeleton":       "Skeleton",
    "crow":           "Crow",
    "mummy":          "Mummy",
    "goblin":         "Goblin",
    "lizard":         "Lizard",
    "gargoyle":       "Gargoyle",
    "red_dragon":     "Red_Dragon",
    "ice_dragon":     "ICE_Dragon",
    "regular_dragon": "Regular_Dragon",
    "yeti":           "Yeti",
    "golem":          "Golem",
    "outhrus":        "Outhrus",
    "demon":          "Demon",
}

# Исключения по расширению: (базовое_имя, суффикс_уровня) -> расширение.
# По умолчанию .png, но часть файлов Gargoyle загружены как .jpg
IMAGE_EXT_OVERRIDE = {
    ("Gargoyle", ""):   "jpg",
    ("Gargoyle", "_3"): "jpg",
}

def open_case_for_user(uid, case_type):
    """Открывает 1 кейс типа case_type для юзера. Возвращает (ok, msg, card|None).
    НОВОЕ v3: если выпала карта, которая уже есть у игрока — это повторка, её ценность
    автоматически зачисляется в Snow Jewel (место в хранилище не нужно)."""
    u = users.find_one({"_id": uid})
    if not u:
        return False, "❌ Пользователь не найден.", None
    if u.get("cases", {}).get(case_type, 0) <= 0:
        return False, "❌ У вас нет таких кейсов.", None
    cards = u.get("cards", [])

    kind, key = roll_case(case_type)
    card = create_card_instance(kind, key)

    is_dup = any(x.get("kind") == kind and x.get("key") == key for x in cards)
    if is_dup:
        value = card_convert_value(card)
        res = users.update_one(
            {"_id": uid, f"cases.{case_type}": {"$gt": 0}},
            {"$inc": {f"cases.{case_type}": -1, "snow_jewels": value}}
        )
        if res.modified_count == 0:
            return False, "❌ Не удалось открыть кейс, попробуйте ещё раз.", None
        caption = (
            f"♻️ <b>Выпала повторка!</b>\n\n{card_caption(card)}\n\n"
            f"✅ Ценность карточки <b>+{fmt(value)} {SNOW_EMOJI}</b> автоматически зачислена на ваш баланс."
        )
        return True, caption, card

    if len(cards) >= CARD_MAX_STORAGE:
        return False, "❌ Нет места в хранилище (Character). Освободите слот (передайте или разберите карту).", None

    res = users.update_one(
        {"_id": uid, f"cases.{case_type}": {"$gt": 0}},
        {"$inc": {f"cases.{case_type}": -1}, "$push": {"cards": card}}
    )
    if res.modified_count == 0:
        return False, "❌ Не удалось открыть кейс, попробуйте ещё раз.", None

    caption = f"🎉 Из кейса выпала карточка!\n\n{card_caption(card)}"
    return True, caption, card

CARD_MAX_STORAGE = 5      # макс. карт в Character
CARD_MAX_UPGRADES = 3     # апгрейдов на одну карту

# Конвертация карты -> Snow Jewel по тиру обычных мобов (1..5)
CARD_TO_JEWEL = {1: 0.1, 2: 0.2, 3: 0.4, 4: 0.6, 5: 1.0}
EPIC_CLASS_TO_JEWEL = 20  # Mage / Dist / Mele -> Snow Jewel

# --- Капсула фарма Snow Jewel ---
CAPSULE_PRICE = 2000          # цена покупки самой капсулы (в ICE)
CAPSULE_BASE_YIELD = 0.1      # базовый фарм за цикл
CAPSULE_BASE_CD = 86400       # 24 часа
CAPSULE_CD_STEP = 300         # -5 минут за апгрейд
CAPSULE_CD_MIN = 1800         # не меньше 30 минут

def capsule_upgrade_price(next_level: int) -> int:
    """Цена апгрейда капсулы до уровня next_level (1,2,3,...)."""
    if next_level <= 1:
        return 500
    if next_level == 2:
        return 1000
    return 1000 + (next_level - 2) * 200

def capsule_stats(level: int):
    """Возвращает (доход за фарм, кулдаун в секундах) для уровня капсулы."""
    yield_amount = round(CAPSULE_BASE_YIELD * max(level, 1), 2)
    cd = max(CAPSULE_CD_MIN, CAPSULE_BASE_CD - CAPSULE_CD_STEP * max(level - 1, 0))
    return yield_amount, cd

# --- Достижения (4 уровня, награда = кейс (+Snow Jewel на макс. уровне самых сложных)) ---
# track — поле в документе юзера, из которого берём прогресс.
# НОВОЕ: у всех достижений теперь 5 уровней вместо 4, пороги и награды увеличены,
# плюс добавлено новое достижение "Коллекционер льда" (по Snow Jewel).
ACHIEVEMENTS = {
    "farm_master": {
        "title": "⛏ Мастер Фарма",
        "track": "farm_count",
        "tiers": [50, 200, 600, 2000, 5000],
        "case":  ["common", "common", "rare", "epic", "epic"],
        "jewel": [0, 0, 1, 2, 5],
    },
    "burner": {
        "title": "🔥 Поджигатель",
        "track": "total_burned",
        "tiers": [200, 2000, 10000, 50000, 150000],
        "case":  ["common", "rare", "rare", "epic", "epic"],
        "jewel": [0, 0, 1, 3, 8],
    },
    "warrior": {
        "title": "⚔️ Воин Лиги",
        "track": "wins",
        "tiers": [10, 50, 200, 600, 1500],
        "case":  ["common", "rare", "epic", "epic", "epic"],
        "jewel": [0, 0, 1, 3, 7],
    },
    "recruiter": {
        "title": "👥 Рекрутёр",
        "track": "ref_count",
        "tiers": [3, 10, 30, 100, 300],
        "case":  ["common", "rare", "rare", "epic", "epic"],
        "jewel": [0, 0, 1, 2, 5],
    },
    "climber": {
        "title": "🏔 Восхождение",
        "track": "level",
        "tiers": [15, 35, 70, 150, 300],
        "case":  ["common", "common", "rare", "epic", "epic"],
        "jewel": [0, 0, 1, 2, 5],
    },
    "league_legend": {
        "title": "🌟 Легенда Лиги",
        "track": "rp",
        "tiers": [50, 150, 300, 1000, 2500],
        "case":  ["common", "rare", "epic", "epic", "epic"],
        "jewel": [0, 0, 1, 3, 7],
    },
    "ice_collector": {
        "title": "💠 Коллекционер Льда",
        "track": "snow_jewels",
        "tiers": [5, 20, 60, 150, 400],
        "case":  ["common", "rare", "rare", "epic", "epic"],
        "jewel": [0, 0, 1, 3, 8],
    },
}

def grant_case(uid, case_type, qty=1):
    users.update_one({"_id": uid}, {"$inc": {f"cases.{case_type}": qty}})

def grant_jewel(uid, amount):
    if amount:
        users.update_one({"_id": uid}, {"$inc": {"snow_jewels": round(float(amount), 2)}})

def check_achievements(uid):
    """Проверяет все достижения юзера и выдаёт награды за новые открытые уровни."""
    try:
        u = users.find_one({"_id": uid})
        if not u:
            return
        levels = u.get("ach_levels", {})
        unlocked_msgs = []

        for key, ach in ACHIEVEMENTS.items():
            current = int(levels.get(key, 0))
            if current >= len(ach["tiers"]):
                continue
            progress = float(u.get(ach["track"], 0))

            new_level = current
            for i in range(current, len(ach["tiers"])):
                if progress >= ach["tiers"][i]:
                    new_level = i + 1
                else:
                    break

            if new_level > current:
                for i in range(current, new_level):
                    case_type = ach["case"][i]
                    jewel = ach["jewel"][i]
                    grant_case(uid, case_type)
                    grant_jewel(uid, jewel)
                    line = f"• <b>{ach['title']}</b> — ур. {i+1}/{len(ach['tiers'])} → {CASE_TYPES[case_type]['name']}"
                    if jewel:
                        line += f" + {jewel} {SNOW_EMOJI}"
                    unlocked_msgs.append(line)
                users.update_one({"_id": uid}, {"$set": {f"ach_levels.{key}": new_level}})

        if unlocked_msgs:
            try:
                bot.send_message(
                    uid,
                    "🏅 <b>Новое достижение открыто!</b>\n\n" + "\n".join(unlocked_msgs),
                    parse_mode="HTML"
                )
            except Exception:
                pass
    except Exception as e:
        logger.error(f"Ошибка check_achievements: {e}")

# ================================================================

# ---------- INIT ----------
bot = telebot.TeleBot(TOKEN, threaded=False)
app = Flask(__name__)

# ---------- DB ----------
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000)

    db = client["icecoin"]
    users = db["users"]
    pixels = db["pixels"]
    pixels.create_index([("x", 1), ("y", 1)], unique=True)
    battles = db["battles"]
    settings = db["settings"]

    yeti_db = client["rucoy"]
    bank_db = yeti_db["bank"]

    users.create_index("username")
    users.create_index("balance")

    # НОВОЕ v3: каталог предметов и кланы
    catalog = db["catalog"]
    clans = db["clans"]
    tournaments = db["tournaments"]   # НОВОЕ v4
    clan_battles = db["clan_battles"]
    payments = db["payments"]
    clan_invites = db["clan_invites"]   # v5: вызовы на войну
    shop_pages = db["shop_pages"]       # v5: страницы магазина от админа
    boss_tpl = db["boss_tpl"]           # v5: шаблоны боссов
    boss_inst = db["boss_inst"]         # v5: боссы в игре
    try:
        users.create_index("nick_lower", unique=True, partialFilterExpression={"nick_lower": {"$type": "string"}})
    except Exception as _e:
        logger.warning(f"Индекс ников не создан: {_e}")

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
                "total_burned": 0.0, # НОВОЕ: всего сожжено
                "farm_count": 0,          # НОВОЕ: кол-во фармов
                "ref_count": 0,            # НОВОЕ: кол-во приглашённых
                "ach_levels": {},          # НОВОЕ: {achievement_key: 0-4}
                "snow_jewels": 0.0,        # НОВОЕ: редкая валюта
                "cases": {"common": 0, "rare": 0, "epic": 0},  # НОВОЕ
                "cards": [],               # НОВОЕ: карточки в Character (макс 3)
                "active_card_id": None,    # НОВОЕ: карта, используемая в Fight
                "capsule": {"owned": False, "level": 0, "last_farm": 0},  # НОВОЕ
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

def mention_html(uid, name):
    """
    Настоящее упоминание игрока (пингует и присылает уведомление),
    даже если у игрока не задан публичный @username в Telegram.
    """
    safe_name = str(name or uid).replace("<", "").replace(">", "").replace("&", "")
    return f'<a href="tg://user?id={uid}">{safe_name}</a>'

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
    kb.add("🎁 Кейсы", "🧬 Character")   # НОВОЕ
    kb.add("💊 Капсула", "🍂 Пасс")      # НОВОЕ
    kb.add("🏰 Клан", "🛒 Магазин")       # НОВОЕ v4
    kb.add("👹 Боссы")                    # НОВОЕ v5
    return kb

# ---------- WEBHOOK ----------

@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    try:
        json_data = request.get_json(force=True)
        update = telebot.types.Update.de_json(json_data)
        # Обрабатываем апдейт в фоновом потоке и сразу отвечаем Telegram 200 OK.
        # Иначе, пока бот грузит фото карточек (~1-2 сек каждое), Telegram может
        # решить, что запрос завис, повторно прислать тот же апдейт — и тогда
        # он обрабатывается дважды, отсюда "message to be replied not found"
        # и прочие странности у новых команд.
        threading.Thread(target=bot.process_new_updates, args=([update],), daemon=True).start()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error(f"Ошибка webhook: {e}")
        # Отвечаем 200 даже при ошибке разбора, чтобы Telegram не долбил повторами
        return jsonify({"status": "error"}), 200

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
                users.update_one({"_id": ref_id}, {"$inc": {"balance": bonus, "ref_count": 1}})
                users.update_one({"_id": uid}, {"$set": {"referrer": ref_id}})
                check_achievements(ref_id)
                try:
                    bot.send_message(ref_id, f"💎 У вас новый реферал! Вам начислено <b>+{bonus} ICE</b>", parse_mode="HTML")
                except:
                    pass

        # НОВОЕ v3: ссылка-приглашение в клан и выбор ника
        clan_code = None
        parts = m.text.split()
        if len(parts) > 1 and parts[1].startswith("clan_"):
            clan_code = parts[1][len("clan_"):]

        if not u.get("nick"):
            if clan_code:
                users.update_one({"_id": uid}, {"$set": {"pending_clan": clan_code}})
            return nick_prompt(m.chat.id, "👋 Добро пожаловать в ICECOIN!")

        if clan_code:
            bot.send_message(m.chat.id, clan_join_by_code(uid, clan_code), parse_mode="HTML")
        if len(parts) > 1 and parts[1] == "clan":
            return clan_home(m.chat.id, uid)
        send_welcome(m.chat.id, uid)

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

# (старый профиль заменён новым — см. блок «НОВОЕ v3» ниже)

# ---------- FARM ----------

@bot.message_handler(func=lambda m: m.text == "⛏ Фарм" or m.text == "/farm")
def farm(m):
    """
    Одна кнопка запускает СРАЗУ оба фарма — ICE и Snow Jewel (капсула) —
    и показывает компактный, аккуратно оформленный результат обоих.
    """
    if not is_subscribed(m): return
    t_id = getattr(m, "message_thread_id", None)

    try:
        u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
        if not u:
            bot.send_message(m.chat.id, "❌ Ошибка получения данных", message_thread_id=t_id)
            return

        inv = u.get("inventory", [])
        cd_reduction = get_pick_cd_reduction(inv)   # Fire Pick — влияет на ОБА фарма
        effective_cd = max(600, FARM_CD - cd_reduction)

        now = int(time.time())
        last_farm_time = u.get("farm", 0)
        time_passed = now - last_farm_time
        ice_ready = time_passed >= effective_cd

        blocks = []
        final_balance = float(u.get("balance", 0.0))

        # ---------- 1) ICE ФАРМ (основная + доп. ячейки) ----------
        cells = u.get("farm_cells") or {}
        ice_cells = 1 + max(0, min(int(cells.get("ice", 0)), MAX_ICE_CELLS - 1))
        jewel_cells = 1 + max(0, min(int(cells.get("jewel", 0)), MAX_JEWEL_CELLS - 1))
        ice_lines = []
        if ice_ready:
            is_vip = u.get("is_vip", False)
            item_bonus_pct, _ = get_pick_bonus_pct(inv)
            total_gain = 0.0
            for _i in range(ice_cells):
                base_gain = farm_amount(u["level"])
                gain = base_gain
                if item_bonus_pct:
                    gain += round(base_gain * item_bonus_pct, 2)
                vip_bonus = 0
                if is_vip:
                    vip_bonus = round(base_gain * VIP_FARM_BONUS_PCT, 2)
                    gain += vip_bonus
                gain = round(gain, 2)
                total_gain += gain
                q = [f"┃ Добыто: <code>{fmt(gain)} ICE</code>"]
                if is_vip:
                    q.append(f"┃ ✨ VIP Бонус: <code>+{fmt(vip_bonus)}</code>")
                ice_lines.append("<blockquote>" + "\n".join(q) + "</blockquote>")

            found_lines, bonus_ice = roll_farm_find(u["_id"], inv)
            total_gain = round(total_gain + bonus_ice, 2)
            final_balance = round(final_balance + total_gain, 2)

            users.update_one(
                {"_id": u["_id"]},
                {"$set": {"farm": now, "balance": final_balance}, "$inc": {"farm_count": 1}}
            )
            check_achievements(u["_id"])

            for fl in found_lines:
                ice_lines.append(f"┃ {fl}")
            ice_lines.append(f"┃ 💰 Баланс: <code>{fmt(final_balance)} ICE</code>")
        else:
            wait = effective_cd - time_passed
            hours, minutes = wait // 3600, (wait % 3600) // 60
            ice_lines.append(f"┃ ⏳ Через {hours}ч {minutes}м")

        blocks.append("❄️ <b>Фарм ICE</b>\n" + "\n".join(ice_lines))

        # ---------- 2) SNOW JEWEL ФАРМ (капсула + доп. ячейки) ----------
        jewel_lines = []
        cap = u.get("capsule", {"owned": False, "level": 0, "last_farm": 0})
        if not cap.get("owned"):
            jewel_lines.append("┃ ❌ Оборудование не куплено")
        else:
            level = cap.get("level", 1)
            yield_amount, base_cap_cd = capsule_stats(level)
            cap_cd = max(600, base_cap_cd - cd_reduction)
            cap_left = cap_cd - (now - cap.get("last_farm", 0))
            if cap_left <= 0:
                users.update_one(
                    {"_id": u["_id"]},
                    {"$inc": {"snow_jewels": round(yield_amount * jewel_cells, 2)}, "$set": {"capsule.last_farm": now}}
                )
                check_achievements(u["_id"])
                for _i in range(jewel_cells):
                    jewel_lines.append(f"<blockquote>┃ Добыто: <code>{yield_amount} {SNOW_EMOJI}</code></blockquote>")
            else:
                h, mnt = cap_left // 3600, (cap_left % 3600) // 60
                jewel_lines.append(f"┃ ⏳ Через {h}ч {mnt}м")

        blocks.append(f"{SNOW_EMOJI} <b>Фарм Jewel</b>\n" + "\n".join(jewel_lines))

        text = "\n\n".join(blocks)
        # НОВОЕ v3: активированная NFT-гифка фарма
        farm_gif = (u.get("active") or {}).get("farm_gif")
        if farm_gif and farm_gif.get("file_id"):
            try:
                send_media(m.chat.id, farm_gif.get("media_type", "animation"), farm_gif["file_id"], text, None, t_id)
                return
            except Exception as e:
                logger.warning(f"Не удалось отправить NFT-гиф фарма ({e}), отправляю обычным сообщением")

        bot.send_message(m.chat.id, text, parse_mode="HTML", message_thread_id=t_id)

    except Exception as e:
        logger.error(f"Ошибка farm: {e}")
        bot.send_message(m.chat.id, "❌ Произошла ошибка", message_thread_id=t_id)

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
        lo_f, hi_f = farm_range(new_level)
        old_lo, old_hi = farm_range(u["level"])

        users.update_one({"_id": u["_id"]}, {"$set": {"balance": new_balance, "level": new_level}})
        check_achievements(u["_id"])

        bot.send_message(
            m.chat.id,
            f"✅ <b>Уровень фарма повышен!</b>\n\n"
            f"⛏ Новый уровень: <b>{new_level}</b>\n"
            f"📈 Добыча за фарм: <b>{lo_f}–{hi_f} ICE</b> (было {old_lo}–{old_hi})\n"
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
            cos = nft.get("cosmetic")
            iid = nft.get("iid")
            if iid and cos == "id_changer":
                kb.add(types.InlineKeyboardButton("🪪 Использовать (сменить ник)", callback_data=f"idc_{iid}"))
            elif iid and cos == "vip_pass":
                kb.add(types.InlineKeyboardButton("👑 Активировать VIP", callback_data=f"vp_a_{iid}"))
            elif iid and cos in ("farm_cell_ice", "farm_cell_jewel"):
                kb.add(types.InlineKeyboardButton("⚙️ Активировать ячейку", callback_data=f"cell_{iid}"))
            elif iid and cos in COSMETIC_SLOT:
                slot = COSMETIC_SLOT[cos]
                cur = (u.get("active") or {}).get(slot) or {}
                if cur.get("iid") == iid:
                    kb.add(types.InlineKeyboardButton("❌ Снять", callback_data=f"unact_{slot}"))
                else:
                    kb.add(types.InlineKeyboardButton("✅ Активировать", callback_data=f"act_{iid}"))
            kb.add(types.InlineKeyboardButton("🎁 Передать игроку", callback_data=f"transfer_nft_{index}"))

            if nft.get("type") == "item" or not nft.get("file_id"):
                # кирки и прочие предметы без медиа
                bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb, message_thread_id=t_id)
            else:
                send_media(c.message.chat.id, nft["type"], nft["file_id"], text, kb, t_id)

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
        if nft.get("iid"):
            act = u.get("active") or {}
            unset = {f"active.{sl}": "" for sl in COSMETIC_SLOT.values() if (act.get(sl) or {}).get("iid") == nft["iid"]}
            if unset:
                users.update_one({"_id": m.from_user.id}, {"$unset": unset})
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
        check_achievements(u["_id"])  # подхватить прогресс, если что-то уже выполнено
        u = users.find_one({"_id": u["_id"]})
        levels = u.get("ach_levels", {})

        text = "<b>🏆 Достижения</b>\n\n"
        for key, ach in ACHIEVEMENTS.items():
            total_tiers = len(ach["tiers"])
            lvl = int(levels.get(key, 0))
            progress = float(u.get(ach["track"], 0))
            text += f"<b>{ach['title']}</b> — {lvl}/{total_tiers}\n"
            if lvl < total_tiers:
                nxt = ach["tiers"][lvl]
                text += f"  прогресс: {fmt(progress)} / {fmt(nxt)}\n"
            else:
                text += "  ✅ выполнено полностью\n"
        text += "\n<i>Награда за уровень — Кейс, на самых сложных уровнях ещё и Snow Jewel 🔷</i>"

        bot.send_message(m.chat.id, text, parse_mode="HTML", message_thread_id=t_id)
    except Exception as e:
        logger.error(f"Ошибка достижений: {e}")

# ---------- SEND ----------

@bot.message_handler(func=lambda m: m.text == "💸 Отправить" or (m.text and (m.text == "/send" or m.text.startswith("/send "))))
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
        check_achievements(win_id)
        check_achievements(lose_id)

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

# ================================================================
# НОВОЕ: КЕЙСЫ (открытие)
# ================================================================

@bot.message_handler(func=lambda m: m.text == "🎁 Кейсы")
def cases_menu(m):
    try:
        t_id = getattr(m, "message_thread_id", None)
        u = get_user(m.from_user.id, m.from_user.username)
        cases = u.get("cases", {"common": 0, "rare": 0, "epic": 0})
        text = f"🎁 <b>Ваши кейсы</b>\n💰 Баланс: <b>{fmt(u.get('balance', 0))} ICE</b>\n\n"
        kb = types.InlineKeyboardMarkup(row_width=1)
        for ctype, info in CASE_TYPES.items():
            cnt = cases.get(ctype, 0)
            price = CASE_PRICES.get(ctype, 0)
            text += f"{info['emoji']} {info['name']}: <b>{cnt}</b> (цена: {price} ICE)\n"
            if cnt > 0:
                kb.add(types.InlineKeyboardButton(f"📦 Открыть {info['name']}", callback_data=f"case_open_{ctype}"))
            kb.add(types.InlineKeyboardButton(f"🛒 Купить {info['name']} ({price} ICE)", callback_data=f"case_buy_{ctype}"))
        bot.send_message(m.chat.id, text, reply_markup=kb, parse_mode="HTML", message_thread_id=t_id)
    except Exception as e:
        logger.error(f"Ошибка cases_menu: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("case_buy_"))
def case_buy_callback(c):
    try:
        case_type = c.data.replace("case_buy_", "")
        ok, msg = buy_case_for_user(c.from_user.id, case_type)
        bot.answer_callback_query(c.id, "✅ Куплено!" if ok else "❌ Не удалось купить", show_alert=not ok)
        bot.send_message(c.message.chat.id, msg, parse_mode="HTML", message_thread_id=getattr(c.message, "message_thread_id", None))
    except Exception as e:
        logger.error(f"Ошибка case_buy_callback: {e}")
        bot.answer_callback_query(c.id, "❌ Ошибка")

@bot.callback_query_handler(func=lambda c: c.data.startswith("case_open_"))
def case_open_callback(c):
    try:
        t_id = getattr(c.message, "message_thread_id", None)
        case_type = c.data.replace("case_open_", "")
        ok, msg, card = open_case_for_user(c.from_user.id, case_type)
        bot.answer_callback_query(c.id)
        if ok and card:
            send_card_photo(c.message.chat.id, card, msg, message_thread_id=t_id)
        else:
            bot.send_message(c.message.chat.id, msg, parse_mode="HTML", message_thread_id=t_id)
        if ok:
            check_achievements(c.from_user.id)
    except Exception as e:
        logger.error(f"Ошибка case_open_callback: {e}")
        bot.answer_callback_query(c.id, "❌ Ошибка")

# ================================================================
# НОВОЕ: CHARACTER (хранилище карт, апгрейд, конвертация, передача)
# ================================================================

# Хранит id последнего UI-сообщения Character для пары (chat_id, uid),
# чтобы всегда удалять старое и присылать новое вместо спама.
CHAR_UI_STATE = {}

def _char_ui_replace(chat_id, uid, send_fn):
    key = (chat_id, uid)
    old_id = CHAR_UI_STATE.get(key)
    if old_id:
        try:
            bot.delete_message(chat_id, old_id)
        except Exception:
            pass
    msg = send_fn()
    if msg:
        CHAR_UI_STATE[key] = msg.message_id
    return msg

def render_character_list(chat_id, uid, username=None, first_name=None, thread_id=None):
    u = get_user(uid, username, first_name) if (username or first_name) else (users.find_one({"_id": uid}) or {})
    cards = u.get("cards", [])
    active_id = u.get("active_card_id")

    text = f"🧬 <b>Character</b> — слотов {len(cards)}/{CARD_MAX_STORAGE}"
    kb = None
    if cards:
        kb = types.InlineKeyboardMarkup(row_width=1)
        for card in cards:
            name, *_ = card_stats(card)
            mark = "⭐ " if card["id"] == active_id else ""
            kb.add(types.InlineKeyboardButton(f"{mark}{name}", callback_data=f"card_view_{card['id']}"))
    else:
        text += "\n\n📭 У вас пока нет карточек. Откройте кейс, чтобы получить первую!"

    _char_ui_replace(chat_id, uid, lambda: bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML", message_thread_id=thread_id))

def render_character_card(chat_id, uid, card_id, thread_id=None):
    u = users.find_one({"_id": uid}) or {}
    card = next((x for x in u.get("cards", []) if x["id"] == card_id), None)
    if not card:
        return render_character_list(chat_id, uid, thread_id=thread_id)

    ice_cost, jewel_cost, maxed = card_upgrade_cost(card)
    jewel_value = card_convert_value(card)
    is_active = u.get("active_card_id") == card_id
    caption = card_caption(card)

    kb = types.InlineKeyboardMarkup(row_width=1)
    if not is_active:
        kb.add(types.InlineKeyboardButton("⭐ Сделать активной для Fight", callback_data=f"card_active_{card_id}"))
    if not maxed:
        cost_txt = f"{ice_cost} ICE" + (f" + {jewel_cost} {SNOW_EMOJI}" if jewel_cost else "")
        kb.add(types.InlineKeyboardButton(f"⏫ Улучшить ({cost_txt})", callback_data=f"card_upg_{card_id}"))
    kb.add(types.InlineKeyboardButton(f"💎 Разобрать (+{jewel_value} {SNOW_EMOJI})", callback_data=f"card_convert_{card_id}"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="card_back"))

    _char_ui_replace(chat_id, uid, lambda: send_card_photo(chat_id, card, caption, reply_markup=kb, message_thread_id=thread_id))

@bot.message_handler(func=lambda m: m.text == "🧬 Character")
def character_menu(m):
    if m.chat.type != "private":
        return
    try:
        render_character_list(m.chat.id, m.from_user.id, m.from_user.username, m.from_user.first_name,
                               thread_id=getattr(m, "message_thread_id", None))
    except Exception as e:
        logger.error(f"Ошибка character_menu: {e}")

# /character — та же панель, но работает и в группах, и в лс
@bot.message_handler(commands=["character"])
def character_cmd(m):
    try:
        render_character_list(m.chat.id, m.from_user.id, m.from_user.username, m.from_user.first_name,
                               thread_id=getattr(m, "message_thread_id", None))
    except Exception as e:
        logger.error(f"Ошибка character_cmd: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("card_view_"))
def card_view_callback(c):
    try:
        card_id = c.data.replace("card_view_", "")
        render_character_card(c.message.chat.id, c.from_user.id, card_id,
                               thread_id=getattr(c.message, "message_thread_id", None))
        bot.answer_callback_query(c.id)
    except Exception as e:
        logger.error(f"Ошибка card_view_callback: {e}")
        bot.answer_callback_query(c.id, "❌ Ошибка")

@bot.callback_query_handler(func=lambda c: c.data == "card_back")
def card_back_callback(c):
    try:
        render_character_list(c.message.chat.id, c.from_user.id,
                               thread_id=getattr(c.message, "message_thread_id", None))
        bot.answer_callback_query(c.id)
    except Exception as e:
        logger.error(f"Ошибка card_back_callback: {e}")
        bot.answer_callback_query(c.id, "❌ Ошибка")

@bot.callback_query_handler(func=lambda c: c.data.startswith("card_active_"))
def card_active_callback(c):
    try:
        card_id = c.data.replace("card_active_", "")
        users.update_one({"_id": c.from_user.id}, {"$set": {"active_card_id": card_id}})
        bot.answer_callback_query(c.id, "⭐ Карта выбрана основной для Fight")
        render_character_card(c.message.chat.id, c.from_user.id, card_id,
                               thread_id=getattr(c.message, "message_thread_id", None))
    except Exception as e:
        logger.error(f"Ошибка card_active_callback: {e}")
        bot.answer_callback_query(c.id, "❌ Ошибка")

@bot.callback_query_handler(func=lambda c: c.data.startswith("card_upg_"))
def card_upgrade_callback(c):
    try:
        card_id = c.data.replace("card_upg_", "")
        u = users.find_one({"_id": c.from_user.id})
        cards = u.get("cards", [])
        idx = next((i for i, x in enumerate(cards) if x["id"] == card_id), None)
        if idx is None:
            return bot.answer_callback_query(c.id, "❌ Карта не найдена")

        card = cards[idx]
        ice_cost, jewel_cost, maxed = card_upgrade_cost(card)
        if maxed:
            return bot.answer_callback_query(c.id, "✅ Максимальный уровень уже достигнут")

        if u.get("balance", 0) < ice_cost or u.get("snow_jewels", 0) < jewel_cost:
            return bot.answer_callback_query(c.id, "❌ Недостаточно ICE или Snow Jewel", show_alert=True)

        cards[idx]["level"] += 1
        users.update_one(
            {"_id": c.from_user.id},
            {"$inc": {"balance": -ice_cost, "snow_jewels": -jewel_cost}, "$set": {"cards": cards}}
        )
        bot.answer_callback_query(c.id, "✅ Улучшено!")
        render_character_card(c.message.chat.id, c.from_user.id, card_id,
                               thread_id=getattr(c.message, "message_thread_id", None))
    except Exception as e:
        logger.error(f"Ошибка card_upgrade_callback: {e}")
        bot.answer_callback_query(c.id, "❌ Ошибка")

@bot.callback_query_handler(func=lambda c: c.data.startswith("card_convert_"))
def card_convert_callback(c):
    try:
        card_id = c.data.replace("card_convert_", "")
        u = users.find_one({"_id": c.from_user.id})
        cards = u.get("cards", [])
        card = next((x for x in cards if x["id"] == card_id), None)
        if not card:
            return bot.answer_callback_query(c.id, "❌ Карта не найдена")

        jewel_value = card_convert_value(card)
        new_cards = [x for x in cards if x["id"] != card_id]
        update = {"$set": {"cards": new_cards}, "$inc": {"snow_jewels": jewel_value}}
        if u.get("active_card_id") == card_id:
            update["$set"]["active_card_id"] = None
        users.update_one({"_id": c.from_user.id}, update)

        bot.answer_callback_query(c.id, f"💎 Получено {jewel_value} {SNOW_EMOJI}")
        render_character_list(c.message.chat.id, c.from_user.id,
                               thread_id=getattr(c.message, "message_thread_id", None))
    except Exception as e:
        logger.error(f"Ошибка card_convert_callback: {e}")
        bot.answer_callback_query(c.id, "❌ Ошибка")

# /sendcard — передача карты по ответу на сообщение, или /sendcard <id>
@bot.message_handler(commands=["sendcard"])
def sendcard_cmd(m):
    try:
        t_id = getattr(m, "message_thread_id", None)
        target_id = None
        parts = m.text.split()
        if m.reply_to_message:
            target_id = m.reply_to_message.from_user.id
        elif len(parts) > 1:
            try:
                target_id = int(parts[1])
            except ValueError:
                return bot.reply_to(m, "❌ Укажите корректный ID: <code>/sendcard 123456</code>", parse_mode="HTML", message_thread_id=t_id)
        else:
            return bot.reply_to(m, "💡 Ответьте на сообщение игрока командой <code>/sendcard</code> или укажите <code>/sendcard ID</code>", parse_mode="HTML", message_thread_id=t_id)

        if target_id == m.from_user.id:
            return bot.reply_to(m, "❌ Нельзя передать карту самому себе.", message_thread_id=t_id)

        u = users.find_one({"_id": m.from_user.id})
        cards = u.get("cards", [])
        if not cards:
            return bot.reply_to(m, "❌ У вас нет карт для передачи.", message_thread_id=t_id)

        target = users.find_one({"_id": target_id})
        if not target:
            return bot.reply_to(m, "❌ Получатель не найден (пусть напишет боту /start).", message_thread_id=t_id)
        if len(target.get("cards", [])) >= CARD_MAX_STORAGE:
            return bot.reply_to(m, "❌ У получателя нет места на хранилище.", message_thread_id=t_id)

        kb = types.InlineKeyboardMarkup(row_width=1)
        for card in cards:
            name, *_ = card_stats(card)
            kb.add(types.InlineKeyboardButton(name, callback_data=f"sendcard_{target_id}_{card['id']}"))
        bot.reply_to(m, "Выберите карту для передачи:", reply_markup=kb, message_thread_id=t_id)
    except Exception as e:
        logger.error(f"Ошибка sendcard_cmd: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("sendcard_"))
def sendcard_callback(c):
    try:
        t_id = getattr(c.message, "message_thread_id", None)
        _, target_id, card_id = c.data.split("_", 2)
        target_id = int(target_id)

        sender = users.find_one({"_id": c.from_user.id})
        cards = sender.get("cards", [])
        card = next((x for x in cards if x["id"] == card_id), None)
        if not card:
            return bot.answer_callback_query(c.id, "❌ Карта уже недоступна")

        target = users.find_one({"_id": target_id})
        if not target or len(target.get("cards", [])) >= CARD_MAX_STORAGE:
            return bot.answer_callback_query(c.id, "❌ У получателя нет места на хранилище", show_alert=True)

        new_cards = [x for x in cards if x["id"] != card_id]
        upd = {"$set": {"cards": new_cards}}
        if sender.get("active_card_id") == card_id:
            upd["$set"]["active_card_id"] = None
        users.update_one({"_id": c.from_user.id}, upd)
        users.update_one({"_id": target_id}, {"$push": {"cards": card}})

        name, *_ = card_stats(card)
        bot.answer_callback_query(c.id, "✅ Передано!")
        send_card_photo(c.message.chat.id, card, f"🎁 Карта <b>{name}</b> передана!", message_thread_id=t_id)
        try:
            send_card_photo(target_id, card, f"🎁 Вам передали карту: <b>{name}</b>!")
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Ошибка sendcard_callback: {e}")
        bot.answer_callback_query(c.id, "❌ Ошибка")

# ================================================================
# НОВОЕ: КАПСУЛА ФАРМА SNOW JEWEL
# ================================================================

@bot.message_handler(func=lambda m: m.text == "💊 Капсула")
def capsule_menu(m):
    if m.chat.type != "private":
        return
    try:
        t_id = getattr(m, "message_thread_id", None)
        u = get_user(m.from_user.id, m.from_user.username)
        cap = u.get("capsule", {"owned": False, "level": 0, "last_farm": 0})

        if not cap.get("owned"):
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(f"💊 Купить капсулу ({CAPSULE_PRICE} ICE)", callback_data="capsule_buy"))
            bot.send_message(m.chat.id, f"💊 <b>Капсула фарма Snow Jewel</b>\n\nПозволяет фармить {SNOW_EMOJI} раз в 24ч.\nЦена: <b>{CAPSULE_PRICE} ICE</b>",
                              reply_markup=kb, parse_mode="HTML", message_thread_id=t_id)
            return

        level = cap.get("level", 1)
        yield_amount, cd = capsule_stats(level)
        next_price = capsule_upgrade_price(level + 1)
        now = int(time.time())
        last_farm = cap.get("last_farm", 0)
        left = cd - (now - last_farm)

        text = (f"💊 <b>Ваша капсула</b> — уровень {level}\n"
                f"Доход: <b>{yield_amount} {SNOW_EMOJI}</b> / {cd // 3600}ч {(cd % 3600) // 60}м\n\n")
        kb = types.InlineKeyboardMarkup(row_width=1)
        if left <= 0:
            kb.add(types.InlineKeyboardButton(f"❄️ Собрать {yield_amount} {SNOW_EMOJI}", callback_data="capsule_farm"))
        else:
            text += f"⏳ До сбора: <b>{left // 3600}ч {(left % 3600) // 60}м</b>\n\n"
        kb.add(types.InlineKeyboardButton(f"⏫ Улучшить капсулу ({next_price} ICE)", callback_data="capsule_upgrade"))
        bot.send_message(m.chat.id, text, reply_markup=kb, parse_mode="HTML", message_thread_id=t_id)
    except Exception as e:
        logger.error(f"Ошибка capsule_menu: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "capsule_buy")
def capsule_buy_callback(c):
    u = users.find_one({"_id": c.from_user.id})
    if u.get("balance", 0) < CAPSULE_PRICE:
        return bot.answer_callback_query(c.id, "❌ Недостаточно ICE", show_alert=True)
    users.update_one(
        {"_id": c.from_user.id},
        {"$inc": {"balance": -CAPSULE_PRICE}, "$set": {"capsule": {"owned": True, "level": 1, "last_farm": 0}}}
    )
    bot.answer_callback_query(c.id, "✅ Капсула куплена!")
    bot.send_message(c.message.chat.id, "💊 Капсула куплена! Открой меню «💊 Капсула», чтобы собрать первый урожай.",
                      message_thread_id=getattr(c.message, "message_thread_id", None))

@bot.callback_query_handler(func=lambda c: c.data == "capsule_farm")
def capsule_farm_callback(c):
    u = users.find_one({"_id": c.from_user.id})
    cap = u.get("capsule", {})
    if not cap.get("owned"):
        return bot.answer_callback_query(c.id, "❌ У вас нет капсулы")
    level = cap.get("level", 1)
    yield_amount, cd = capsule_stats(level)
    now = int(time.time())
    left = cd - (now - cap.get("last_farm", 0))
    if left > 0:
        return bot.answer_callback_query(c.id, f"⏳ Ещё рано, осталось {left // 60} мин.", show_alert=True)

    users.update_one({"_id": c.from_user.id}, {"$inc": {"snow_jewels": yield_amount}, "$set": {"capsule.last_farm": now}})
    bot.answer_callback_query(c.id, f"❄️ +{yield_amount} {SNOW_EMOJI}")
    bot.send_message(c.message.chat.id, f"❄️ Собрано: <b>{yield_amount} {SNOW_EMOJI}</b>",
                      message_thread_id=getattr(c.message, "message_thread_id", None))

@bot.callback_query_handler(func=lambda c: c.data == "capsule_upgrade")
def capsule_upgrade_callback(c):
    u = users.find_one({"_id": c.from_user.id})
    cap = u.get("capsule", {})
    if not cap.get("owned"):
        return bot.answer_callback_query(c.id, "❌ У вас нет капсулы")
    level = cap.get("level", 1)
    price = capsule_upgrade_price(level + 1)
    if u.get("balance", 0) < price:
        return bot.answer_callback_query(c.id, "❌ Недостаточно ICE", show_alert=True)
    users.update_one({"_id": c.from_user.id}, {"$inc": {"balance": -price}, "$set": {"capsule.level": level + 1}})
    bot.answer_callback_query(c.id, "✅ Капсула улучшена!")
    bot.send_message(c.message.chat.id, f"⏫ Капсула улучшена до уровня <b>{level + 1}</b>!", parse_mode="HTML",
                      message_thread_id=getattr(c.message, "message_thread_id", None))

# ================================================================
# НОВОЕ: SNOW JEWEL — перевод и выдача админом
# ================================================================

@bot.message_handler(commands=["sendjewel"])
def sendjewel_cmd(m):
    t_id = getattr(m, "message_thread_id", None)
    try:
        parts = m.text.split()
        if len(parts) < 3:
            return bot.reply_to(m, f"💡 Формат: <code>/sendjewel ID сумма</code>", parse_mode="HTML", message_thread_id=t_id)
        target_id = int(parts[1])
        amount = float(parts[2].replace(",", "."))
        if amount <= 0:
            return bot.reply_to(m, "❌ Некорректная сумма.", message_thread_id=t_id)
        if target_id == m.from_user.id:
            return bot.reply_to(m, "❌ Нельзя перевести себе.", message_thread_id=t_id)

        u = users.find_one({"_id": m.from_user.id})
        if u.get("snow_jewels", 0) < amount:
            return bot.reply_to(m, f"❌ Недостаточно {SNOW_EMOJI}. Баланс: {u.get('snow_jewels', 0)}", message_thread_id=t_id)
        target = users.find_one({"_id": target_id})
        if not target:
            return bot.reply_to(m, "❌ Получатель не найден.", message_thread_id=t_id)

        users.update_one({"_id": m.from_user.id}, {"$inc": {"snow_jewels": -amount}})
        users.update_one({"_id": target_id}, {"$inc": {"snow_jewels": amount}})
        bot.reply_to(m, f"✅ Переведено {amount} {SNOW_EMOJI} игроку <code>{target_id}</code>", parse_mode="HTML", message_thread_id=t_id)
        try:
            bot.send_message(target_id, f"🔷 Вам перевели {amount} {SNOW_EMOJI}!")
        except Exception:
            pass
    except Exception as e:
        bot.reply_to(m, f"❌ Ошибка: {e}", message_thread_id=t_id)

@bot.message_handler(commands=["givejewel"])
def givejewel_cmd(m):
    if m.from_user.id != ADMIN_ID:
        return
    t_id = getattr(m, "message_thread_id", None)
    try:
        parts = m.text.split()
        target_id = int(parts[1])
        amount = float(parts[2].replace(",", "."))
        grant_jewel(target_id, amount)
        bot.reply_to(m, f"✅ Выдано {amount} {SNOW_EMOJI} игроку {target_id}", message_thread_id=t_id)
        try:
            bot.send_message(target_id, f"🔷 Админ начислил вам {amount} {SNOW_EMOJI}!")
        except Exception:
            pass
    except Exception as e:
        bot.reply_to(m, f"❌ Формат: /givejewel ID сумма ({e})")

# ---------------------------------------------------------------
# НОВОЕ: /give_jewel (только админ) и /send_jewl (все игроки)
# Работают двумя способами: /команда ID СУММА  или ответом на сообщение: /команда СУММА
# ---------------------------------------------------------------

def _jewel_parse_args(m):
    """Возвращает (target_id, amount) или (None, None), если формат неверный."""
    parts = (m.text or "").split()
    rm = getattr(m, "reply_to_message", None)
    try:
        if rm is not None and getattr(rm, "from_user", None) is not None:
            if len(parts) < 2:
                return None, None
            return rm.from_user.id, round(float(parts[1].replace(",", ".")), 2)
        if len(parts) < 3:
            return None, None
        return int(parts[1]), round(float(parts[2].replace(",", ".")), 2)
    except (ValueError, IndexError):
        return None, None

@bot.message_handler(commands=["give_jewel"])
def give_jewel_admin_cmd(m):
    if m.from_user.id != ADMIN_ID:
        return
    t_id = getattr(m, "message_thread_id", None)
    try:
        target_id, amount = _jewel_parse_args(m)
        if target_id is None or amount is None:
            return bot.reply_to(
                m, "🔧 Формат: <code>/give_jewel ID СУММА</code>\nили ответом на сообщение: <code>/give_jewel СУММА</code>",
                parse_mode="HTML", message_thread_id=t_id
            )
        if amount <= 0:
            return bot.reply_to(m, "❌ Сумма должна быть больше нуля.", message_thread_id=t_id)

        res = users.update_one({"_id": target_id}, {"$inc": {"snow_jewels": amount}})
        if res.matched_count == 0:
            return bot.reply_to(m, "❌ Пользователь не найден в базе!", message_thread_id=t_id)

        check_achievements(target_id)
        bot.reply_to(m, f"✅ Выдано <b>{fmt(amount)}</b> {SNOW_EMOJI} игроку <code>{target_id}</code>",
                     parse_mode="HTML", message_thread_id=t_id)
        try:
            bot.send_message(target_id, f"🔷 Админ начислил вам <b>{fmt(amount)}</b> {SNOW_EMOJI}!", parse_mode="HTML")
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Ошибка give_jewel: {e}")
        bot.reply_to(m, "❌ Ошибка при выполнении команды", message_thread_id=t_id)

@bot.message_handler(commands=["send_jewl"])
def send_jewl_cmd(m):
    t_id = getattr(m, "message_thread_id", None)
    try:
        target_id, amount = _jewel_parse_args(m)
        if target_id is None or amount is None:
            return bot.reply_to(
                m, "💡 Формат: <code>/send_jewl ID СУММА</code>\nили ответом на сообщение: <code>/send_jewl СУММА</code>",
                parse_mode="HTML", message_thread_id=t_id
            )
        if amount <= 0:
            return bot.reply_to(m, "❌ Некорректная сумма.", message_thread_id=t_id)
        if target_id == m.from_user.id:
            return bot.reply_to(m, "❌ Нельзя перевести самому себе.", message_thread_id=t_id)

        sender = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
        if not sender:
            return bot.reply_to(m, "❌ Ошибка получения данных", message_thread_id=t_id)

        target = users.find_one({"_id": target_id})
        if not target:
            return bot.reply_to(m, "❌ Получатель не найден в базе бота.", message_thread_id=t_id)

        # атомарное списание: пройдёт только если хватает Snow Jewel
        res = users.update_one(
            {"_id": m.from_user.id, "snow_jewels": {"$gte": amount}},
            {"$inc": {"snow_jewels": -amount}}
        )
        if res.modified_count == 0:
            cur = (users.find_one({"_id": m.from_user.id}) or {}).get("snow_jewels", 0)
            return bot.reply_to(m, f"❌ Недостаточно {SNOW_EMOJI}. Ваш баланс: <b>{fmt(cur)}</b>",
                                parse_mode="HTML", message_thread_id=t_id)

        users.update_one({"_id": target_id}, {"$inc": {"snow_jewels": amount}})
        check_achievements(target_id)

        bot.reply_to(m, f"✅ Переведено <b>{fmt(amount)}</b> {SNOW_EMOJI} игроку <code>{target_id}</code>",
                     parse_mode="HTML", message_thread_id=t_id)
        try:
            bot.send_message(target_id, f"🔷 Вам перевели <b>{fmt(amount)}</b> {SNOW_EMOJI}!", parse_mode="HTML")
        except Exception:
            pass
    except Exception as e:
        logger.error(f"Ошибка send_jewl: {e}")
        bot.reply_to(m, "❌ Произошла ошибка при переводе", message_thread_id=t_id)

# ================================================================
# НОВОЕ: КАРТОЧНАЯ БОЁВКА /fight
# ================================================================

FIGHT_STAKES = [0, 50, 100, 300, 500]
FIGHT_TURN_SECONDS = 40
active_fights = {}  # bid(str) -> state

def get_active_card(uid):
    u = users.find_one({"_id": uid})
    if not u:
        return None
    cards = u.get("cards", [])
    if not cards:
        return None
    active_id = u.get("active_card_id")
    for card in cards:
        if card["id"] == active_id:
            return card
    return cards[0]

@bot.message_handler(commands=["fight"])
def fight_call(m):
    if not m.reply_to_message:
        return bot.send_message(m.chat.id, "❌ Ответьте на сообщение игрока, чтобы вызвать его на Fight!", message_thread_id=m.message_thread_id)

    challenger = m.from_user
    opponent = m.reply_to_message.from_user

    if opponent.is_bot:
        return bot.send_message(m.chat.id, "❌ Нельзя вызвать бота.", message_thread_id=m.message_thread_id)
    if challenger.id == opponent.id:
        return bot.send_message(m.chat.id, "❌ Нельзя вызвать самого себя!", message_thread_id=m.message_thread_id)

    if not get_active_card(challenger.id):
        return bot.send_message(m.chat.id, "❌ У вас нет активной карточки. Откройте кейс и выберите карту в Character.", message_thread_id=m.message_thread_id)
    if not get_active_card(opponent.id):
        return bot.send_message(m.chat.id, f"❌ У {opponent.first_name} нет активной карточки для боя.", message_thread_id=m.message_thread_id)

    bid = uuid.uuid4().hex[:10]
    active_fights[bid] = {
        "stage": "pending",
        "chat_id": m.chat.id,
        "thread_id": m.message_thread_id,
        "challenger_id": challenger.id,
        "challenger_name": challenger.first_name,
        "opponent_id": opponent.id,
        "opponent_name": opponent.first_name,
    }

    kb = types.InlineKeyboardMarkup()
    kb.add(
        types.InlineKeyboardButton("✅ Принять", callback_data=f"cf_acc_{bid}"),
        types.InlineKeyboardButton("❌ Отказаться", callback_data=f"cf_den_{bid}")
    )
    bot.send_message(
        m.chat.id,
        f"⚔️ {mention_html(opponent.id, opponent.first_name)}, вас вызывают на Fight!\n"
        f"{mention_html(challenger.id, challenger.first_name)} бросает вызов!",
        reply_markup=kb, parse_mode="HTML", message_thread_id=m.message_thread_id
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("cf_"))
def cardfight_callback(c):
    try:
        parts = c.data.split("_")
        action = parts[1]
        bid = parts[2]
        fight = active_fights.get(bid)
        if not fight:
            return bot.answer_callback_query(c.id, "❌ Вызов не найден или истёк")

        if action == "den":
            if c.from_user.id != fight["opponent_id"]:
                return bot.answer_callback_query(c.id, "Это не ваш вызов!")
            bot.edit_message_text("❌ Fight не состоялся. Вызов отклонён.", fight["chat_id"], c.message.message_id)
            del active_fights[bid]

        elif action == "acc":
            if c.from_user.id != fight["opponent_id"]:
                return bot.answer_callback_query(c.id, "Это не ваш вызов!")
            kb = types.InlineKeyboardMarkup(row_width=3)
            kb.add(*[types.InlineKeyboardButton(f"{x} ❄️", callback_data=f"cf_bet_{bid}_{x}") for x in FIGHT_STAKES])
            bot.edit_message_text("💰 Выберите ставку на кон:", fight["chat_id"], c.message.message_id, reply_markup=kb)

        elif action == "bet":
            if c.from_user.id != fight["opponent_id"]:
                return bot.answer_callback_query(c.id, "Ставку выбирает тот, кого вызвали!")
            bet = float(parts[3])
            fight["bet"] = bet
            kb = types.InlineKeyboardMarkup()
            kb.add(
                types.InlineKeyboardButton("✅ Да", callback_data=f"cf_conf_{bid}_y"),
                types.InlineKeyboardButton("❌ Нет", callback_data=f"cf_conf_{bid}_n")
            )
            bot.edit_message_text(
                f"{mention_html(fight['opponent_id'], fight['opponent_name'])} выбрал <b>{bet}</b> на кон битвы. "
                f"{mention_html(fight['challenger_id'], fight['challenger_name'])}, вы согласны?",
                fight["chat_id"], c.message.message_id, reply_markup=kb, parse_mode="HTML"
            )

        elif action == "conf":
            if c.from_user.id != fight["challenger_id"]:
                return bot.answer_callback_query(c.id, "Подтвердить может только вызвавший!")
            decision = parts[3]
            if decision == "n":
                bot.edit_message_text("❌ Fight не состоялся. Участники не определили ставку.", fight["chat_id"], c.message.message_id)
                del active_fights[bid]
                return

            bet = fight.get("bet", 0)
            p1 = users.find_one({"_id": fight["challenger_id"]})
            p2 = users.find_one({"_id": fight["opponent_id"]})
            if bet > 0 and (p1.get("balance", 0) < bet or p2.get("balance", 0) < bet):
                bot.edit_message_text("❌ У одного из игроков недостаточно ICE для такой ставки.", fight["chat_id"], c.message.message_id)
                del active_fights[bid]
                return

            # НЕ удаляем сообщение — переиспользуем его же для всего боя,
            # чтобы бот не спамил чат новыми сообщениями на каждый ход
            fight["message_id"] = c.message.message_id
            start_card_fight(bid)

        bot.answer_callback_query(c.id)
    except Exception as e:
        logger.error(f"Ошибка cardfight_callback: {e}")
        bot.answer_callback_query(c.id, "❌ Ошибка")

def start_card_fight(bid):
    fight = active_fights.get(bid)
    if not fight:
        return

    c1 = get_active_card(fight["challenger_id"])
    c2 = get_active_card(fight["opponent_id"])
    n1, hp1, dmg1, def1, rng1 = card_stats(c1)
    n2, hp2, dmg2, def2, rng2 = card_stats(c2)

    fight.update({
        "stage": "fighting",
        "p1": {"uid": fight["challenger_id"], "name": fight["challenger_name"], "card_name": n1, "card": c1,
               "hp": hp1, "max_hp": hp1, "dmg": dmg1, "def": def1, "rng": rng1, "shield": False},
        "p2": {"uid": fight["opponent_id"], "name": fight["opponent_name"], "card_name": n2, "card": c2,
               "hp": hp2, "max_hp": hp2, "dmg": dmg2, "def": def2, "rng": rng2, "shield": False},
        "turn": "p1",
        "turn_token": 0,
    })
    send_turn_message(bid)

def send_turn_message(bid, action_text=""):
    """
    НОВОЕ: каждый ход — это НОВОЕ сообщение (старое сообщение боя удаляется),
    а не редактирование одного и того же. Так сразу видно, что пришло новое
    сообщение про ход, и понятно, чей сейчас ход, а не что оно просто обновилось.
    """
    fight = active_fights.get(bid)
    if not fight or fight["stage"] != "fighting":
        return
    mover = fight[fight["turn"]]
    other = fight["p2"] if fight["turn"] == "p1" else fight["p1"]

    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("⚔️ Атаковать", callback_data=f"cfm_{bid}_atk"),
        types.InlineKeyboardButton("🛡 Защита", callback_data=f"cfm_{bid}_def")
    )
    action_line = f"{action_text}\n\n" if action_text else ""
    text = (
        f"{action_line}"
        f"🃏 <b>{mover['card_name']}</b> ({mention_html(mover['uid'], mover['name'])})\n"
        f"❤️ Здоровье: {mover['hp']}/{mover['max_hp']}   |   Соперник: {other['hp']}/{other['max_hp']}\n\n"
        f"👉 {mention_html(mover['uid'], mover['name'])}, у вас есть {FIGHT_TURN_SECONDS} секунд, чтобы сделать ход, иначе поражение!\n"
        f"⚔️ Атаковать — нанести урон.   🛡 Защита — снизить след. полученный урон."
    )

    old_msg_id = fight.get("message_id")
    if old_msg_id:
        try:
            bot.delete_message(fight["chat_id"], old_msg_id)
        except Exception:
            pass  # старое сообщение уже могло быть удалено вручную — не критично

    # НОВОЕ: показываем картинку карточки того, чей сейчас ход
    mover_card = mover.get("card")
    msg = None
    if mover_card:
        try:
            msg = send_card_photo(fight["chat_id"], mover_card, text, reply_markup=kb, message_thread_id=fight.get("thread_id"))
        except Exception as e:
            logger.warning(f"Не удалось отправить карточку боя ({e}), отправляю текстом")
    if not msg:
        msg = bot.send_message(fight["chat_id"], text, reply_markup=kb, parse_mode="HTML", message_thread_id=fight.get("thread_id"))
    fight["message_id"] = msg.message_id

    fight["turn_token"] += 1
    token = fight["turn_token"]

    timer = threading.Timer(FIGHT_TURN_SECONDS, fight_timeout, args=[bid, token])
    timer.daemon = True
    timer.start()

def fight_timeout(bid, token):
    fight = active_fights.get(bid)
    if not fight or fight["stage"] != "fighting" or fight["turn_token"] != token:
        return  # ход уже сделан
    mover_key = fight["turn"]
    loser = fight[mover_key]
    winner_key = "p2" if mover_key == "p1" else "p1"
    winner = fight[winner_key]
    reason = f"⏱ {mention_html(loser['uid'], loser['name'])} не успел сделать ход — поражение!"
    finish_fight(bid, winner["uid"], loser["uid"], reason=reason)

@bot.callback_query_handler(func=lambda c: c.data.startswith("cfm_"))
def cardfight_move_callback(c):
    try:
        _, bid, move = c.data.split("_", 2)
        fight = active_fights.get(bid)
        if not fight or fight["stage"] != "fighting":
            return bot.answer_callback_query(c.id, "❌ Бой уже завершён")

        mover_key = fight["turn"]
        mover = fight[mover_key]
        if c.from_user.id != mover["uid"]:
            return bot.answer_callback_query(c.id, "Сейчас не ваш ход!")

        other_key = "p2" if mover_key == "p1" else "p1"
        other = fight[other_key]

        if move == "def":
            mover["shield"] = True
            action_text = f"🛡 {mention_html(mover['uid'], mover['name'])} занял защитную стойку."
        else:
            base = mover["dmg"]
            rng = mover["rng"]
            dmg = base if rng is None else random.randint(max(0, base - rng), base + rng)
            if other["shield"]:
                dmg = round(dmg * (1 - other["def"] / 100), 0)
                other["shield"] = False
            dmg = max(0, int(dmg))
            other["hp"] = max(0, other["hp"] - dmg)
            action_text = f"💥 {mention_html(mover['uid'], mover['name'])} нанёс <b>{dmg}</b> урона игроку {mention_html(other['uid'], other['name'])}!"

            if other["hp"] <= 0:
                bot.answer_callback_query(c.id)
                finish_fight(bid, mover["uid"], other["uid"], reason=action_text)
                return

        fight["turn"] = other_key
        bot.answer_callback_query(c.id)
        send_turn_message(bid, action_text)
    except Exception as e:
        logger.error(f"Ошибка cardfight_move_callback: {e}")
        bot.answer_callback_query(c.id, "❌ Ошибка")

def finish_fight(bid, winner_uid, loser_uid, reason=None):
    fight = active_fights.get(bid)
    if not fight:
        return
    fight["stage"] = "done"
    bet = fight.get("bet", 0)

    if bet > 0:
        users.update_one({"_id": winner_uid}, {"$inc": {"balance": bet}})
        users.update_one({"_id": loser_uid}, {"$inc": {"balance": -bet}})
    users.update_one({"_id": winner_uid}, {"$inc": {"wins": 1, "rp": RP_WIN}})
    users.update_one({"_id": loser_uid}, {"$inc": {"rp": RP_LOSS}})
    check_achievements(winner_uid)
    check_achievements(loser_uid)

    winner_entry = fight["p1"] if fight["p1"]["uid"] == winner_uid else fight["p2"]
    winner_mention = mention_html(winner_entry["uid"], winner_entry["name"])

    text = f"{reason}\n\n" if reason else ""
    text += f"🏆 Победил {winner_mention}!"
    if bet > 0:
        text += f"\n💰 Выигрыш: <b>{bet} ICE</b>"

    # НОВОЕ: как и в ходах — удаляем предыдущее сообщение боя и присылаем новое,
    # с картинкой карточки победителя, чтобы результат сразу бросался в глаза.
    msg_id = fight.get("message_id")
    if msg_id:
        try:
            bot.delete_message(fight["chat_id"], msg_id)
        except Exception:
            pass

    winner_card = winner_entry.get("card")
    sent = None
    if winner_card:
        try:
            sent = send_card_photo(fight["chat_id"], winner_card, text, message_thread_id=fight.get("thread_id"))
        except Exception as e:
            logger.warning(f"Не удалось отправить карточку победителя ({e}), отправляю текстом")
    if not sent:
        bot.send_message(fight["chat_id"], text, parse_mode="HTML", message_thread_id=fight.get("thread_id"))

    del active_fights[bid]

# ---------- ADMIN PANEL ----------

@bot.message_handler(commands=["admin"])
def admin_panel(m):
    if m.from_user.id != ADMIN_ID: return
    try:
        bot.send_message(m.chat.id, admin_stats_text(), parse_mode="HTML", reply_markup=admin_main_kb())
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
            f"🍂 Сожжённый ICE идёт в сезонный пасс: /pass\n\n"
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
    check_achievements(c.from_user.id)

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
    pass_add_burn(c.from_user.id, amount)   # НОВОЕ v3: прогресс сезонного пасса

# ================================================================
# НОВОЕ: КРАФТ ПРЕДМЕТОВ ⚗️
# ================================================================

@bot.message_handler(commands=["craft"])
@bot.message_handler(func=lambda m: m.text == "⚗️ Крафт")
def craft_menu(m):
    """
    НОВОЕ: главное меню крафта — реальная инструкция вместо фейковых демо-рецептов.
    Показывает, где взять кирки и как скрафтить их Legendary-версию (4 шт. + 10 💠).
    Обычный 2-предметный крафт (для NFT из /add_recipe) вынесен в отдельную кнопку.
    """
    t_id = getattr(m, "message_thread_id", None)
    u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    inv = u.get("inventory", [])

    picks_info = "\n".join(
        f"{PICK_DATA[k]['emoji']} <b>{PICK_DATA[k]['name']}</b> — {PICK_DATA[k]['desc']}\n"
        f"   шанс найти во время фарма: <i>{PICK_FIND_CHANCES[k]*100:.1f}%</i>"
        for k in PICK_DATA
    )

    text = (
        f"⚗️ <b>Крафт предметов</b>\n\n"
        f"🪓 <b>Кирки</b> можно найти во время обычного фарма ⛏:\n{picks_info}\n\n"
        f"🌟 <b>Легендарный крафт:</b> соберите <b>{LEGENDARY_ITEM_COST}</b> одинаковых кирки "
        f"+ <b>{LEGENDARY_JEWEL_COST} {SNOW_EMOJI}</b> Snow Jewel — и получите Legendary-версию "
        f"этой кирки с <b>x2</b> эффектом от обычной."
    )

    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(
        f"🌟 Крафт Легендарной кирки ({LEGENDARY_ITEM_COST} шт. + {LEGENDARY_JEWEL_COST} {SNOW_EMOJI})",
        callback_data="craft_leg_menu"
    ))
    if len(inv) >= 2:
        kb.add(types.InlineKeyboardButton("🧪 Крафт из 2 предметов (NFT)", callback_data="craft_2item_start"))

    bot.send_message(m.chat.id, text, reply_markup=kb, parse_mode="HTML", message_thread_id=t_id)

@bot.callback_query_handler(func=lambda c: c.data == "craft_2item_start")
def craft_2item_start(c):
    u = users.find_one({"_id": c.from_user.id})
    inv = u.get("inventory", [])
    if len(inv) < 2:
        return bot.answer_callback_query(c.id, "❌ Нужно минимум 2 предмета", show_alert=True)

    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, item in enumerate(inv):
        rarity_icon = RARITY_EMOJI.get(item.get("rarity", ""), "🖼")
        kb.add(types.InlineKeyboardButton(f"[{i+1}] {rarity_icon} {item['name']}", callback_data=f"craft_pick1_{i}"))

    bot.edit_message_text(
        "⚗️ <b>Крафт из 2 предметов</b>\nВыберите <b>первый</b> предмет:",
        c.message.chat.id, c.message.message_id,
        reply_markup=kb, parse_mode="HTML"
    )
    bot.answer_callback_query(c.id)

# ---- НОВОЕ: Легендарный крафт кирок (4 одинаковых + 10 Snow Jewel) ----

@bot.callback_query_handler(func=lambda c: c.data in ("craft_leg_menu", "craft_back"))
def craft_leg_menu(c):
    u = users.find_one({"_id": c.from_user.id})
    inv = u.get("inventory", [])
    jewels = float(u.get("snow_jewels", 0))

    if c.data == "craft_back":
        # Вернуться в главное меню крафта (перерисовать то же сообщение)
        picks_info = "\n".join(
            f"{PICK_DATA[k]['emoji']} <b>{PICK_DATA[k]['name']}</b> — {PICK_DATA[k]['desc']}\n"
            f"   шанс найти во время фарма: <i>{PICK_FIND_CHANCES[k]*100:.1f}%</i>"
            for k in PICK_DATA
        )
        text = (
            f"⚗️ <b>Крафт предметов</b>\n\n"
            f"🪓 <b>Кирки</b> можно найти во время обычного фарма ⛏:\n{picks_info}\n\n"
            f"🌟 <b>Легендарный крафт:</b> соберите <b>{LEGENDARY_ITEM_COST}</b> одинаковых кирки "
            f"+ <b>{LEGENDARY_JEWEL_COST} {SNOW_EMOJI}</b> Snow Jewel — и получите Legendary-версию "
            f"этой кирки с <b>x2</b> эффектом от обычной."
        )
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton(
            f"🌟 Крафт Легендарной кирки ({LEGENDARY_ITEM_COST} шт. + {LEGENDARY_JEWEL_COST} {SNOW_EMOJI})",
            callback_data="craft_leg_menu"
        ))
        if len(inv) >= 2:
            kb.add(types.InlineKeyboardButton("🧪 Крафт из 2 предметов (NFT)", callback_data="craft_2item_start"))
        bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")
        return bot.answer_callback_query(c.id)

    kb = types.InlineKeyboardMarkup(row_width=1)
    lines = []
    for key, data in PICK_DATA.items():
        have = count_owned_pick(inv, key, legendary=False)
        lines.append(f"{data['emoji']} {data['name']}: <b>{have}</b>/{LEGENDARY_ITEM_COST}")
        if have >= LEGENDARY_ITEM_COST and jewels >= LEGENDARY_JEWEL_COST:
            kb.add(types.InlineKeyboardButton(f"🌟 Скрафтить {data['legendary_name']}", callback_data=f"craft_leg_{key}"))

    text = (f"🌟 <b>Легендарный крафт</b>\n\n"
            f"{SNOW_EMOJI} Snow Jewel: <b>{fmt(jewels)}</b> (нужно {LEGENDARY_JEWEL_COST})\n\n" +
            "\n".join(lines))

    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="craft_back"))
    bot.edit_message_text(text, c.message.chat.id, c.message.message_id, reply_markup=kb, parse_mode="HTML")
    bot.answer_callback_query(c.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("craft_leg_"))
def craft_leg_do(c):
    key = c.data.split("craft_leg_", 1)[1]
    if key not in PICK_DATA:
        return bot.answer_callback_query(c.id, "❌ Неизвестный предмет")

    u = users.find_one({"_id": c.from_user.id})
    inv = u.get("inventory", [])
    jewels = float(u.get("snow_jewels", 0))

    if count_owned_pick(inv, key, legendary=False) < LEGENDARY_ITEM_COST:
        return bot.answer_callback_query(c.id, f"❌ Нужно {LEGENDARY_ITEM_COST} шт. {PICK_DATA[key]['name']}", show_alert=True)
    if jewels < LEGENDARY_JEWEL_COST:
        return bot.answer_callback_query(c.id, f"❌ Нужно {LEGENDARY_JEWEL_COST} {SNOW_EMOJI}", show_alert=True)

    removed = 0
    new_inv = []
    for it in inv:
        if removed < LEGENDARY_ITEM_COST and it.get("pick_key") == key and not it.get("legendary"):
            removed += 1
            continue
        new_inv.append(it)

    new_inv.append({
        "name": PICK_DATA[key]["legendary_name"],
        "pick_key": key,
        "legendary": True,
        "type": "item",
        "rarity": "legendary",
        "desc": PICK_DATA[key]["legendary_desc"],
        "date": int(time.time()),
    })

    users.update_one(
        {"_id": c.from_user.id},
        {"$set": {"inventory": new_inv}, "$inc": {"snow_jewels": -LEGENDARY_JEWEL_COST}}
    )
    check_achievements(c.from_user.id)

    bot.edit_message_text(
        f"✅ <b>Крафт успешен!</b>\n\n{PICK_DATA[key]['emoji']} Получено: <b>{PICK_DATA[key]['legendary_name']}</b>\n"
        f"<i>{PICK_DATA[key]['legendary_desc']}</i>",
        c.message.chat.id, c.message.message_id, parse_mode="HTML"
    )
    bot.answer_callback_query(c.id, "🌟 Легендарная вещь создана!")

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

# ================================================================
# НОВОЕ v3: НИКИ, СЕЗОННЫЙ ПАСС, КАТАЛОГ ПРЕДМЕТОВ, КЛАНЫ, /balance
# ================================================================
import html as _html
from pymongo.errors import DuplicateKeyError

def safe(t):
    return _html.escape(str(t if t is not None else ""))

def is_admin(uid):
    return uid == ADMIN_ID

def parse_num(text):
    try:
        v = float(str(text).replace(",", ".").strip())
        return v if v > 0 else None
    except Exception:
        return None

def adm_cancelled(m):
    return bool(m.text) and m.text.strip() == "/cancel"

def adm_ask(chat_id, prompt, handler, *args):
    msg = bot.send_message(chat_id, prompt + "\n\n<i>/cancel — отмена</i>", parse_mode="HTML")
    bot.register_next_step_handler(msg, handler, *args)

def del_msg(c):
    try:
        bot.delete_message(c.message.chat.id, c.message.message_id)
    except Exception:
        pass

_BOT_USERNAME = {"v": None}
def bot_username():
    if not _BOT_USERNAME["v"]:
        _BOT_USERNAME["v"] = bot.get_me().username
    return _BOT_USERNAME["v"]

# ---------------------------------------------------------------
# МЕДИА
# ---------------------------------------------------------------

def send_media(chat_id, media_type, file_id, caption="", reply_markup=None, thread_id=None):
    kw = dict(caption=caption, parse_mode="HTML", reply_markup=reply_markup, message_thread_id=thread_id)
    if media_type == "photo":
        return bot.send_photo(chat_id, file_id, **kw)
    if media_type == "video":
        return bot.send_video(chat_id, file_id, **kw)
    if media_type == "document":
        return bot.send_document(chat_id, file_id, **kw)
    return bot.send_animation(chat_id, file_id, **kw)

def send_media_safe(chat_id, media_type, file_id, caption="", reply_markup=None, thread_id=None):
    """Пробует отправить медиа, при ошибке — обычным текстом."""
    if file_id:
        try:
            return send_media(chat_id, media_type, file_id, caption, reply_markup, thread_id)
        except Exception as e:
            logger.warning(f"send_media не удалось ({e}), отправляю текстом")
    return bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=reply_markup, message_thread_id=thread_id)

def extract_media(m, allow_url=True):
    """Возвращает (file_id_or_url, media_type) из сообщения или (None, None). Любой размер."""
    ct = m.content_type
    if ct == "photo":
        return m.photo[-1].file_id, "photo"
    if ct == "animation":
        return m.animation.file_id, "animation"
    if ct == "video":
        return m.video.file_id, "video"
    if ct == "document":
        return m.document.file_id, "document"
    if allow_url and ct == "text" and m.text and m.text.strip().lower().startswith("http"):
        url = m.text.strip()
        path = url.lower().split("?")[0]
        return url, ("animation" if path.endswith((".gif", ".mp4")) else "photo")
    return None, None

# ---------------------------------------------------------------
# ФИЛЬТР 18+ / НИКИ
# ---------------------------------------------------------------

NICK_MIN, NICK_MAX = 3, 20
NICK_RE = re.compile(r"^[^\W_]+$", re.UNICODE)
RESERVED_NICKS = {"admin", "administrator", "moderator", "support", "icecoin", "bot", "админ", "админинистратор", "модератор"}

_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_LAT2CYR = str.maketrans({"a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м", "o": "о", "p": "р", "t": "т", "x": "х", "y": "у"})
_CYR2LAT = str.maketrans({"а": "a", "в": "b", "с": "c", "е": "e", "н": "h", "к": "k", "м": "m", "о": "o", "р": "p", "т": "t", "х": "x", "у": "y"})

BAD_STEMS = [
    # RU
    "хуй", "хуе", "хуя", "хули", "пизд", "пезд", "ебан", "ебат", "ебал", "ебуч", "еблан", "блят", "бляд",
    "сука", "мудак", "мудил", "залуп", "пидор", "пидар", "педик", "гандон", "шлюх", "дроч", "порно",
    "секс", "сиськ", "письк", "хуев",
    # EN
    "fuck", "fuk", "shit", "porn", "sex", "nude", "xxx", "cock", "dick", "pussy", "bitch", "nigg",
    "hentai", "whore", "slut", "cunt", "penis", "vagina", "blowjob",
]

def _norm_variants(text):
    low = str(text).lower()
    leet = low.translate(_LEET)
    vs = {low, leet, leet.translate(_LAT2CYR), leet.translate(_CYR2LAT), low.translate(_LAT2CYR), low.translate(_CYR2LAT)}
    vs |= {re.sub(r"(.)\1+", r"\1", v) for v in list(vs)}
    vs |= {re.sub(r"[^\w]", "", v) for v in list(vs)}
    return vs

def is_bad_text(text):
    variants = _norm_variants(text)
    for stem in BAD_STEMS:
        for v in variants:
            if stem in v:
                return True
    return False

def validate_nick(nick):
    if len(nick) < NICK_MIN:
        return False, f"❌ Ник слишком короткий (минимум {NICK_MIN} символа)."
    if len(nick) > NICK_MAX:
        return False, f"❌ Ник слишком длинный (максимум {NICK_MAX} символов)."
    if not NICK_RE.match(nick):
        return False, "❌ В нике можно только буквы любых языков и цифры — без пробелов и символов."
    if nick.lower() in RESERVED_NICKS:
        return False, "❌ Этот ник зарезервирован."
    if is_bad_text(nick):
        return False, "❌ Недопустимый ник. Выберите другой."
    return True, ""

def set_nick(uid, nick):
    ok, err = validate_nick(nick)
    if not ok:
        return False, err
    low = nick.lower()
    other = users.find_one({"nick_lower": low}, {"_id": 1})
    if other and other["_id"] != uid:
        return False, "❌ Этот ник уже занят."
    try:
        users.update_one({"_id": uid}, {"$set": {"nick": nick, "nick_lower": low}})
    except DuplicateKeyError:
        return False, "❌ Этот ник уже занят."
    return True, ""

def disp_name(u):
    return u.get("nick") or u.get("first_name") or u.get("username") or f"User_{u.get('_id')}"

def user_names(ids):
    res = {}
    for d in users.find({"_id": {"$in": list(ids)}}, {"nick": 1, "first_name": 1, "username": 1}):
        res[d["_id"]] = d.get("nick") or d.get("first_name") or d.get("username") or str(d["_id"])
    return res

def nick_prompt(chat_id, prefix=""):
    msg = bot.send_message(
        chat_id,
        (prefix + "\n\n" if prefix else "") +
        "✍️ <b>Придумайте свой ник</b>\n\n"
        f"• от {NICK_MIN} до {NICK_MAX} символов\n"
        "• только буквы (любые языки) и цифры\n"
        "• без пробелов и лишних символов",
        parse_mode="HTML", reply_markup=types.ReplyKeyboardRemove()
    )
    bot.register_next_step_handler(msg, nick_step_first)

def nick_step_first(m):
    try:
        uid = m.from_user.id
        if not m.text:
            return nick_prompt(m.chat.id, "❌ Отправьте ник текстом.")
        if m.text.startswith("/start"):
            return start(m)
        if m.text.startswith("/"):
            return nick_prompt(m.chat.id, "❌ Сначала выберите ник.")
        ok, err = set_nick(uid, m.text.strip())
        if not ok:
            return nick_prompt(m.chat.id, err)
        bot.send_message(m.chat.id, f"✅ Ник <b>{safe(m.text.strip())}</b> сохранён!", parse_mode="HTML")
        pc = (users.find_one({"_id": uid}) or {}).get("pending_clan")
        if pc:
            users.update_one({"_id": uid}, {"$unset": {"pending_clan": ""}})
            bot.send_message(m.chat.id, clan_join_by_code(uid, pc), parse_mode="HTML")
        send_welcome(m.chat.id, uid)
    except Exception as e:
        logger.error(f"Ошибка nick_step_first: {e}")

def send_welcome(chat_id, uid):
    u = users.find_one({"_id": uid}) or {}
    uname = u.get("username")
    uname_line = f" (@{safe(uname)})" if uname and not str(uname).startswith("user_") else ""
    txt = (
        f"❄️ <b>ICECOIN - Криптовалютная игра</b>\n\n"
        f"👤 {safe(disp_name(u))}{uname_line}\n"
        f"🆔 <code>{u.get('_id')}</code>\n"
        f"💰 Баланс: <b>{fmt(u.get('balance', 0))} ICE</b>\n"
        f"⛏ Уровень фарма: <b>{u.get('level', 1)}</b>\n"
        f"🏆 Побед в батлах: <b>{u.get('wins', 0)}</b>\n\n"
        f"<i>Выберите действие из меню:</i>"
    )
    bot.send_message(chat_id, txt, reply_markup=create_main_keyboard(), parse_mode="HTML")

@bot.message_handler(commands=["nick"])
def nick_cmd(m):
    if m.chat.type != "private":
        return bot.reply_to(m, "ℹ️ Эта команда работает в личке с ботом.")
    u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
    if not u.get("nick"):
        return nick_prompt(m.chat.id)
    bot.send_message(
        m.chat.id,
        f"🪪 Ваш ник: <b>{safe(u['nick'])}</b>\n\nСменить ник можно предметом <b>ID Changer</b> из 🎒 Инвентаря.",
        parse_mode="HTML"
    )

def idchanger_step(m, iid):
    try:
        if adm_cancelled(m):
            return bot.send_message(m.chat.id, "❌ Отменено. Предмет остался в инвентаре.")
        if not m.text or m.text.startswith("/"):
            return bot.send_message(m.chat.id, "❌ Нужно отправить ник текстом. Нажмите «Использовать» ещё раз.")
        uid = m.from_user.id
        nick = m.text.strip()
        ok, err = validate_nick(nick)
        if not ok:
            return bot.send_message(m.chat.id, err + "\nПредмет не потрачен — нажмите «Использовать» ещё раз.")
        other = users.find_one({"nick_lower": nick.lower()}, {"_id": 1})
        if other and other["_id"] != uid:
            return bot.send_message(m.chat.id, "❌ Этот ник уже занят. Предмет не потрачен.")
        try:
            res = users.update_one(
                {"_id": uid, "inventory.iid": iid},
                {"$set": {"nick": nick, "nick_lower": nick.lower()}, "$pull": {"inventory": {"iid": iid}}}
            )
        except DuplicateKeyError:
            return bot.send_message(m.chat.id, "❌ Этот ник уже занят. Предмет не потрачен.")
        if res.modified_count == 0:
            return bot.send_message(m.chat.id, "❌ Предмет уже недоступен.")
        bot.send_message(m.chat.id, f"✅ Ник изменён на <b>{safe(nick)}</b>!", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка idchanger_step: {e}")

# ---------------------------------------------------------------
# /balance
# ---------------------------------------------------------------

@bot.message_handler(commands=["balance", "bal"])
def balance_cmd(m):
    try:
        t_id = getattr(m, "message_thread_id", None)
        u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
        bot.send_message(
            m.chat.id,
            f"💰 <b>Баланс</b> — {safe(disp_name(u))}\n\n"
            f"❄️ ICE: <b>{fmt(u.get('balance', 0))}</b>\n"
            f"{SNOW_EMOJI} Snow Jewel: <b>{fmt(u.get('snow_jewels', 0))}</b>",
            parse_mode="HTML", message_thread_id=t_id
        )
    except Exception as e:
        logger.error(f"Ошибка balance: {e}")

# ---------------------------------------------------------------
# VIP: купить
# ---------------------------------------------------------------

@bot.message_handler(commands=["vip"])
@bot.message_handler(func=lambda m: m.text == "👑 Купить VIP")
def vip_buy(m):
    try:
        t_id = getattr(m, "message_thread_id", None)
        u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
        if u.get("is_vip"):
            return bot.send_message(m.chat.id, "👑 У вас уже есть <b>VIP</b>! Спасибо, что с нами 💙", parse_mode="HTML", message_thread_id=t_id)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("💬 Купить VIP — написать @herozvz", url="https://t.me/herozvz"))
        bot.send_message(
            m.chat.id,
            "👑 <b>VIP-статус</b>\n\n"
            f"• +{int(VIP_FARM_BONUS_PCT*100)}% к добыче ICE за фарм\n"
            "• +15 ICE за каждого реферала (вместо 10)\n"
            "• 🍂 Бонусы в сезонном пассе: +% к прогрессу и доп. награды на каждом ранге\n"
            "• Особый эмодзи и фон в профиле\n\n"
            "Нажмите кнопку ниже — откроется чат с @herozvz 👇",
            parse_mode="HTML", reply_markup=kb, message_thread_id=t_id
        )
    except Exception as e:
        logger.error(f"Ошибка vip_buy: {e}")

# ---------------------------------------------------------------
# КАТАЛОГ ПРЕДМЕТОВ (шаблоны) и косметика
# ---------------------------------------------------------------

ITEM_KINDS = {
    "nft":        "🖼 NFT (коллекционный)",
    "emoji":      "😀 Эмодзи профиля",
    "profile_bg": "🌄 Фото/гиф профиля",
    "farm_gif":   "🎞 NFT-гиф фарма",
    "id_changer": "🪪 ID Changer",
    "title":      "🏷 Титул",
}
KIND_ICON = {"nft": "🖼", "emoji": "😀", "profile_bg": "🌄", "farm_gif": "🎞", "id_changer": "🪪", "title": "🏷"}
COSMETIC_SLOT = {"emoji": "emoji", "profile_bg": "bg", "farm_gif": "farm_gif", "title": "title"}
MEDIA_REQUIRED_KINDS = ("nft", "profile_bg", "farm_gif")
ADM_DRAFT = {}

def item_plain(tpl):
    icon = tpl.get("emoji") if tpl.get("kind") == "emoji" and tpl.get("emoji") else KIND_ICON.get(tpl.get("kind"), "🎁")
    return f"{icon} {tpl.get('name', '?')}"

def item_label(tpl):
    return safe(item_plain(tpl))

def make_item_instance(tpl):
    has_media = bool(tpl.get("file_id"))
    kind = tpl.get("kind")
    return {
        "iid": uuid.uuid4().hex[:10],
        "name": tpl.get("name", "Предмет"),
        "desc": tpl.get("desc", ""),
        "type": (tpl.get("media_type") or "photo") if has_media else "item",
        "file_id": tpl.get("file_id"),
        "rarity": tpl.get("rarity", "epic"),
        "cosmetic": kind if kind != "nft" else None,
        "emoji": tpl.get("emoji"),
        "title": tpl.get("title"),
        "tpl": tpl["_id"],
        "date": int(time.time()),
    }

def give_item(uid, tpl):
    inst = make_item_instance(tpl)
    users.update_one({"_id": uid}, {"$push": {"inventory": inst}})
    return inst

# ---------------------------------------------------------------
# СЕЗОННЫЙ ПАСС
# ---------------------------------------------------------------

PASS_BANNER_URL = "https://i.ibb.co/bggHRC9K/file-00000000cf588208a97fbd7885eb07f2.png"
AUTUMN_GIRL_URL = "https://i.ibb.co/bM5dmw9p/b0a357be-b5c4-4aa6-8004-9460ffb69b69.jpg"

DEFAULT_ITEMS = [
    {"_id": "autumn_petal", "name": "Лепесток осени", "desc": "Осенний эмодзи для профиля. Активируйте в инвентаре.",
     "kind": "emoji", "emoji": "🍂", "rarity": "rare"},
    {"_id": "autumn_girl", "name": "Autumn Girl", "desc": "Эксклюзивное фото профиля сезона Autumn.",
     "kind": "profile_bg", "file_id": AUTUMN_GIRL_URL, "media_type": "photo", "rarity": "epic"},
    # Временная картинка. Замените на настоящую гифку: /items → Autumn Farm → «Заменить медиа»
    {"_id": "autumn_farm", "name": "Autumn Farm", "desc": "NFT-гиф, которая показывается при фарме. Активируйте в инвентаре.",
     "kind": "farm_gif", "file_id": PASS_BANNER_URL, "media_type": "photo", "rarity": "epic"},
    {"_id": "id_changer", "name": "ID Changer", "desc": "Позволяет сменить ник. Одноразовый.",
     "kind": "id_changer", "rarity": "rare"},
    {"_id": "autumn_title", "name": "Autumn Legend", "desc": "Титул за максимальный ранг сезона Autumn.",
     "kind": "title", "title": "🍁 Autumn Legend", "rarity": "legendary"},
]

DEFAULT_PASS = {
    "season_id": "autumn",
    "name": "🍂 Autumn Season Pass",
    "desc": "Сжигай ICE, расти в рангах и забирай осенние награды!",
    "banner": PASS_BANNER_URL,
    "banner_type": "photo",
    "vip_progress_bonus": 0.10,
    "started": int(time.time()),
    "ranks": [
        {"need": 100,   "reward": {"type": "ice", "amount": 100},                         "vip": {"type": "ice", "amount": 20}},
        {"need": 500,   "reward": {"type": "ice", "amount": 400},                         "vip": {"type": "jewel", "amount": 1}},
        {"need": 1000,  "reward": {"type": "item", "item": "autumn_petal"},               "vip": {"type": "ice", "amount": 200}},
        {"need": 2000,  "reward": {"type": "jewel", "amount": 5},                         "vip": {"type": "jewel", "amount": 2}},
        {"need": 3500,  "reward": {"type": "case", "case": "rare", "amount": 1},          "vip": {"type": "case", "case": "common", "amount": 1}},
        {"need": 5000,  "reward": {"type": "jewel", "amount": 10},                        "vip": {"type": "jewel", "amount": 3}},
        {"need": 7500,  "reward": {"type": "item", "item": "autumn_girl"},                "vip": {"type": "ice", "amount": 500}},
        {"need": 10000, "reward": {"type": "item", "item": "autumn_farm"},                "vip": {"type": "jewel", "amount": 5}},
        {"need": 12500, "reward": {"type": "item", "item": "id_changer"},                 "vip": {"type": "case", "case": "epic", "amount": 1}},
        {"need": 15000, "reward": {"type": "item", "item": "autumn_title"},               "vip": {"type": "jewel", "amount": 15}},
    ],
}

def seed_defaults():
    try:
        for tpl in DEFAULT_ITEMS:
            body = {k: v for k, v in tpl.items() if k != "_id"}
            catalog.update_one({"_id": tpl["_id"]}, {"$setOnInsert": body}, upsert=True)
        settings.update_one({"_id": "season_pass"}, {"$setOnInsert": DEFAULT_PASS}, upsert=True)
    except Exception as e:
        logger.error(f"Ошибка seed_defaults: {e}")

def pass_cfg():
    doc = settings.find_one({"_id": "season_pass"})
    if not doc:
        seed_defaults()
        doc = settings.find_one({"_id": "season_pass"})
    return doc

def reward_text(r):
    if not r or r.get("type") in (None, "none"):
        return "—"
    t = r["type"]
    if t == "ice":
        return f"{fmt(r.get('amount', 0))} ICE"
    if t == "jewel":
        return f"{fmt(r.get('amount', 0))} {SNOW_EMOJI}"
    if t == "case":
        info = CASE_TYPES.get(r.get("case"), {"name": "Кейс"})
        return f"{int(r.get('amount', 1))}× {info['name']}"
    if t == "item":
        tpl = catalog.find_one({"_id": r.get("item")})
        return item_label(tpl) if tpl else "🎁 Предмет (удалён)"
    return "?"

def grant_reward(uid, r):
    if not r:
        return
    t = r.get("type")
    if t == "ice":
        users.update_one({"_id": uid}, {"$inc": {"balance": round(float(r.get("amount", 0)), 2)}})
    elif t == "jewel":
        grant_jewel(uid, r.get("amount", 0))
    elif t == "case":
        grant_case(uid, r.get("case", "common"), int(r.get("amount", 1)))
    elif t == "item":
        tpl = catalog.find_one({"_id": r.get("item")})
        if tpl:
            give_item(uid, tpl)

def pass_state(u, cfg):
    sid = cfg["season_id"]
    p = (u.get("season_pass") or {}).get(sid, {})
    burned = float(p.get("burned", 0))
    rank = sum(1 for r in cfg.get("ranks", []) if burned >= r["need"])
    return burned, rank, p

def pass_sync(uid, notify=True):
    """Выдаёт все не полученные награды за достигнутые ранги (+VIP-награды). Возвращает список строк."""
    cfg = pass_cfg()
    sid = cfg["season_id"]
    u = users.find_one({"_id": uid})
    if not u:
        return []
    burned, _, p = pass_state(u, cfg)
    is_vip = bool(u.get("is_vip"))
    lines = []
    for i, rk in enumerate(cfg.get("ranks", []), 1):
        if burned < rk["need"]:
            break
        if i not in p.get("claimed", []):
            res = users.update_one(
                {"_id": uid, f"season_pass.{sid}.claimed": {"$ne": i}},
                {"$addToSet": {f"season_pass.{sid}.claimed": i}}
            )
            if res.modified_count:
                grant_reward(uid, rk.get("reward"))
                lines.append(f"• Ранг {i}: {reward_text(rk.get('reward'))}")
        vr = rk.get("vip")
        if is_vip and vr and vr.get("type") not in (None, "none") and i not in p.get("vip_claimed", []):
            res = users.update_one(
                {"_id": uid, f"season_pass.{sid}.vip_claimed": {"$ne": i}},
                {"$addToSet": {f"season_pass.{sid}.vip_claimed": i}}
            )
            if res.modified_count:
                grant_reward(uid, vr)
                lines.append(f"• Ранг {i} 👑 VIP: {reward_text(vr)}")
    if lines and notify:
        try:
            bot.send_message(uid, f"🍂 <b>Награды сезонного пасса!</b>\n\n" + "\n".join(lines), parse_mode="HTML")
        except Exception:
            pass
    return lines

def pass_add_burn(uid, amount, notify=True):
    try:
        cfg = pass_cfg()
        sid = cfg["season_id"]
        u = users.find_one({"_id": uid}) or {}
        add = float(amount)
        if u.get("is_vip"):
            add *= (1 + float(cfg.get("vip_progress_bonus", 0.10)))
        users.update_one({"_id": uid}, {"$inc": {f"season_pass.{sid}.burned": round(add, 2)}})
        return pass_sync(uid, notify=notify)
    except Exception as e:
        logger.error(f"Ошибка pass_add_burn: {e}")
        return []

def pass_bar(cur, total, n=12):
    filled = n if total <= 0 else min(n, int(n * cur / total))
    return "▰" * filled + "▱" * (n - filled)

def pass_caption(u, cfg):
    burned, rank, _ = pass_state(u, cfg)
    ranks = cfg.get("ranks", [])
    total = ranks[-1]["need"] if ranks else 0
    lines = [f"<b>{safe(cfg.get('name'))}</b>"]
    if cfg.get("desc"):
        lines.append(f"<i>{safe(cfg['desc'])}</i>")
    lines += [
        "",
        f"🔥 Сожжено в сезоне: <b>{fmt(burned)}</b> / {fmt(total)} ICE",
        f"🏆 Ранг: <b>{rank}/{len(ranks)}</b>",
        pass_bar(burned, total),
    ]
    if rank < len(ranks):
        nxt = ranks[rank]
        lines += ["", f"⏭ Ранг {rank + 1} — ещё <b>{fmt(nxt['need'] - burned)}</b> ICE:",
                  f"🎁 {reward_text(nxt.get('reward'))}"]
        if nxt.get("vip") and nxt["vip"].get("type") not in (None, "none"):
            lines.append(f"👑 VIP-бонус: {reward_text(nxt['vip'])}")
    else:
        lines += ["", "🎉 <b>Все ранги сезона пройдены!</b>"]
    if u.get("is_vip"):
        lines += ["", f"👑 VIP: +{int(float(cfg.get('vip_progress_bonus', 0.1)) * 100)}% к прогрессу и доп. награды"]
    lines += ["", "🔥 Жечь ICE: <code>/burn СУММА</code>"]
    return "\n".join(lines)

def pass_all_text(u, cfg):
    burned, _, _ = pass_state(u, cfg)
    lines = [f"🎁 <b>Награды — {safe(cfg.get('name'))}</b>", ""]
    for i, r in enumerate(cfg.get("ranks", []), 1):
        mark = "✅" if burned >= r["need"] else "🔒"
        lines.append(f"{mark} <b>Ранг {i}</b> · 🔥 {fmt(r['need'])} ICE — {reward_text(r.get('reward'))}")
        if r.get("vip") and r["vip"].get("type") not in (None, "none"):
            lines.append(f"      👑 VIP: {reward_text(r['vip'])}")
    return "\n".join(lines)

@bot.message_handler(commands=["pass"])
@bot.message_handler(func=lambda m: m.text == "🍂 Пасс")
def pass_menu(m):
    try:
        t_id = getattr(m, "message_thread_id", None)
        u = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
        pass_sync(m.from_user.id)  # VIP-догонка наград
        u = users.find_one({"_id": m.from_user.id}) or u
        cfg = pass_cfg()
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton("🎁 Все награды", callback_data="pass_all"))
        if not u.get("is_vip"):
            kb.add(types.InlineKeyboardButton("👑 Купить VIP (бонусы пасса)", url="https://t.me/herozvz"))
        send_media_safe(m.chat.id, cfg.get("banner_type", "photo"), cfg.get("banner"), pass_caption(u, cfg), kb, t_id)
    except Exception as e:
        logger.error(f"Ошибка pass_menu: {e}")

@bot.callback_query_handler(func=lambda c: c.data == "pass_all")
def pass_all_callback(c):
    try:
        u = users.find_one({"_id": c.from_user.id}) or {}
        bot.send_message(c.message.chat.id, pass_all_text(u, pass_cfg()), parse_mode="HTML",
                         message_thread_id=getattr(c.message, "message_thread_id", None))
        bot.answer_callback_query(c.id)
    except Exception as e:
        logger.error(f"Ошибка pass_all_callback: {e}")

# ---------------------------------------------------------------
# ИНВЕНТАРЬ: активация косметики
# ---------------------------------------------------------------

@bot.callback_query_handler(func=lambda c: c.data.startswith(("act_", "unact_", "idc_")))
def cosmetic_callback(c):
    try:
        uid = c.from_user.id
        u = users.find_one({"_id": uid}) or {}
        inv = u.get("inventory", [])
        d = c.data
        if d.startswith("unact_"):
            slot = d[len("unact_"):]
            if slot in COSMETIC_SLOT.values():
                users.update_one({"_id": uid}, {"$unset": {f"active.{slot}": ""}})
            return bot.answer_callback_query(c.id, "❌ Снято")
        if d.startswith("act_"):
            iid = d[len("act_"):]
            item = next((x for x in inv if x.get("iid") == iid), None)
            if not item or not item.get("cosmetic"):
                return bot.answer_callback_query(c.id, "❌ Предмет не найден", show_alert=True)
            kind = item["cosmetic"]
            slot = COSMETIC_SLOT.get(kind)
            if not slot:
                return bot.answer_callback_query(c.id, "❌ Этот предмет не активируется", show_alert=True)
            val = {"iid": iid}
            if kind == "emoji":
                val["emoji"] = item.get("emoji") or "✨"
            elif kind == "title":
                val["title"] = item.get("title") or item.get("name")
            else:
                if not item.get("file_id"):
                    return bot.answer_callback_query(c.id, "❌ У предмета нет медиа", show_alert=True)
                val["file_id"] = item["file_id"]
                val["media_type"] = item.get("type", "photo")
            users.update_one({"_id": uid}, {"$set": {f"active.{slot}": val}})
            return bot.answer_callback_query(c.id, "✅ Активировано!")
        if d.startswith("idc_"):
            iid = d[len("idc_"):]
            item = next((x for x in inv if x.get("iid") == iid and x.get("cosmetic") == "id_changer"), None)
            if not item:
                return bot.answer_callback_query(c.id, "❌ Предмет не найден", show_alert=True)
            bot.answer_callback_query(c.id)
            msg = bot.send_message(
                uid,
                "🪪 <b>ID Changer</b>\n\nВведите новый ник:\n"
                f"• {NICK_MIN}–{NICK_MAX} символов, только буквы и цифры\n\n<i>/cancel — отмена</i>",
                parse_mode="HTML"
            )
            bot.register_next_step_handler(msg, idchanger_step, iid)
    except Exception as e:
        logger.error(f"Ошибка cosmetic_callback: {e}")
        try:
            bot.answer_callback_query(c.id, "❌ Ошибка")
        except Exception:
            pass

# ---------------------------------------------------------------
# ПРОФИЛЬ
# ---------------------------------------------------------------

def build_profile(u, is_self):
    active = u.get("active") or {}
    is_vip = u.get("is_vip", False)
    status_emoji = u.get("vip_emoji", "👑") if is_vip else "👤"
    custom_emoji = (active.get("emoji") or {}).get("emoji") or ""
    title = (active.get("title") or {}).get("title") or ""

    burned = u.get("total_burned", 0.0)
    burn_rank, burn_emoji = get_burn_rank(burned)
    rp = u.get("rp", 0)
    league_name, _ = get_league(rp)

    cfg = pass_cfg()
    _, pass_rank, _ = pass_state(u, cfg)

    clan_line = ""
    if u.get("clan_id"):
        cl = clans.find_one({"_id": u["clan_id"]}, {"name": 1, "level": 1})
        if cl:
            clan_line = f"┃ 🏰 <b>Клан:</b>      <code>{safe(cl['name'])} · ур. {clan_level(cl)}</code>\n"

    name = safe(disp_name(u))
    head = f"╔═ {status_emoji}{custom_emoji} <b>ПРОФИЛЬ ИГРОКА</b> ═╗\n"
    lines = [head, f"┃ <b>Ник:</b> {name}"]
    if title:
        lines.append(f"┃ <b>Титул:</b> {safe(title)}")
    if u.get("username") and not str(u["username"]).startswith("user_"):
        lines.append(f"┃ <b>Юзер:</b> @{safe(u['username'])}")
    lines += [
        f"┃ 🆔 <code>{u['_id']}</code>",
        "┣━━━━━━━━━━━━━━━━━━",
        f"┃ 💰 <b>Баланс:</b>    <code>{fmt(u.get('balance', 0))} ICE</code>",
        f"┃ {SNOW_EMOJI} <b>Jewel:</b>      <code>{fmt(u.get('snow_jewels', 0))}</code>",
        f"┃ ⛏ <b>Уровень:</b>    <code>{u.get('level', 1)}</code>",
        f"┃ 🏆 <b>Победы:</b>    <code>{u.get('wins', 0)}</code>",
        f"┃ ⚔️ <b>Лига:</b>      <code>{league_name} ({rp} RP)</code>",
        f"┃ 🔥 <b>Сожжено:</b>   <code>{fmt(burned)} ICE</code> {burn_emoji}",
        f"┃ 🍂 <b>Пасс:</b>      <code>ранг {pass_rank}/{len(cfg.get('ranks', []))}</code>",
        f"┃ 🧬 <b>Карт:</b>      <code>{len(u.get('cards', []))}/{CARD_MAX_STORAGE}</code>",
    ]
    if clan_line:
        lines.append(clan_line.rstrip("\n"))
    if is_vip:
        lines.append("┃ 👑 <b>VIP</b>")
    if is_self:
        now = int(time.time())
        next_farm = u.get("farm", 0) + FARM_CD - now
        farm_status = "✅ Доступен" if next_farm <= 0 else f"⏳ {next_farm // 60} мин"
        lines += ["┣━━━━━━━━━━━━━━━━━━", f"┃ ⛏ <b>Майнинг:</b>    {farm_status}"]
    lines.append("╚══════════════════╝")
    txt = "\n".join(lines)

    bg = active.get("bg")
    if bg and bg.get("file_id"):
        return txt, bg["file_id"], bg.get("media_type", "photo")
    if is_vip and u.get("vip_background"):
        return txt, u["vip_background"], ("photo" if u.get("vip_type") == "photo" else "animation")
    return txt, None, None

@bot.message_handler(commands=["profile"])
@bot.message_handler(func=lambda m: m.text == "👤 Профиль")
def profile(m):
    t_id = getattr(m, "message_thread_id", None)
    try:
        text = (m.text or "").strip()
        arg = None
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                arg = parts[1].strip()

        target = None
        is_self = False
        rm = getattr(m, "reply_to_message", None)

        if arg:
            q = arg.lstrip("@")
            target = users.find_one({"nick_lower": q.lower()})
            if not target:
                target = users.find_one({"username": {"$regex": f"^{re.escape(q)}$", "$options": "i"}})
            if not target and q.isdigit():
                target = users.find_one({"_id": int(q)})
            if not target:
                return bot.send_message(m.chat.id, "❌ Игрок с таким ником не найден.", message_thread_id=t_id)
        elif (rm is not None and getattr(rm, "from_user", None) is not None and not rm.from_user.is_bot
              and not getattr(rm, "forum_topic_created", None)):
            target = users.find_one({"_id": rm.from_user.id})
            if not target:
                return bot.send_message(m.chat.id, "❌ Этот игрок ещё не запускал бота.", message_thread_id=t_id)
        else:
            target = get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)

        is_self = target["_id"] == m.from_user.id
        txt, bg_id, bg_type = build_profile(target, is_self)
        send_media_safe(m.chat.id, bg_type, bg_id, txt, None, t_id)
    except Exception as e:
        logger.error(f"Ошибка профиля: {e}")
        bot.send_message(m.chat.id, "❌ Ошибка при генерации профиля.", message_thread_id=t_id)

# ---------------------------------------------------------------
# КЛАНЫ
# ---------------------------------------------------------------

CLAN_CREATE_COST = 50
CLAN_MEDIA_COST = 30
CLAN_RENAME_COST = 30
CLAN_MAX_MEMBERS = 30
CLAN_MAX_LINKS = 3
CLAN_NAME_MIN, CLAN_NAME_MAX = 3, 24
CLAN_DESC_MAX = 300
CLAN_NAME_RE = re.compile(r"^[\w\- ]+$", re.UNICODE)
CLAN_DRAFT = {}

def clan_name_ok(name, exclude_id=None):
    name = (name or "").strip()
    if len(name) < CLAN_NAME_MIN or len(name) > CLAN_NAME_MAX:
        return False, f"❌ Название: от {CLAN_NAME_MIN} до {CLAN_NAME_MAX} символов."
    if not CLAN_NAME_RE.match(name):
        return False, "❌ В названии можно только буквы, цифры, пробел, «-» и «_»."
    if is_bad_text(name):
        return False, "❌ Недопустимое название."
    q = {"name_lower": name.lower()}
    if exclude_id:
        q["_id"] = {"$ne": exclude_id}
    if clans.find_one(q):
        return False, "❌ Клан с таким названием уже существует."
    return True, ""

def clan_desc_ok(desc):
    desc = (desc or "").strip()
    if not desc or len(desc) > CLAN_DESC_MAX:
        return False, f"❌ Описание: от 1 до {CLAN_DESC_MAX} символов."
    if is_bad_text(desc):
        return False, "❌ Недопустимое описание."
    return True, ""

def clan_caption(clan):
    cfg = clan_cfg()
    leader = clan["leader"]
    deps = set(clan.get("deputies", []))
    ids = [leader] + [x for x in clan.get("members", []) if x != leader]
    names = user_names(ids)
    shown = ids[:15]
    mem = [f"{'👑' if i == leader else ('🛡' if i in deps else '▫️')} {safe(names.get(i, i))}" for i in shown]
    if len(ids) > len(shown):
        mem.append(f"… и ещё {len(ids) - len(shown)}")
    cur, need = clan_progress(clan, cfg)
    lvl_line = f"⭐ <b>Уровень {clan_level(clan)}</b>" + (f" · XP {fmt(cur)}/{fmt(need)}" if need else " · MAX")
    titles = clan.get("titles") or []
    title_line = ("\n🏅 " + " · ".join(safe(t) for t in titles[-3:])) if titles else ""
    return (
        f"🏰 <b>Клан:</b> {safe(clan['name'])}\n{lvl_line}{title_line}\n\n"
        f"<blockquote>{safe(clan.get('desc') or '—')}</blockquote>\n\n"
        f"👥 <b>Участники</b> ({len(ids)}/{clan_max_members(clan, cfg)}):\n" + "\n".join(mem)
    )

def send_clan_card(chat_id, clan, kb=None):
    return send_media_safe(chat_id, clan.get("media_type", "photo"), clan.get("file_id"), clan_caption(clan), kb)

def pay_jewels(uid, cost):
    res = users.update_one({"_id": uid, "snow_jewels": {"$gte": cost}}, {"$inc": {"snow_jewels": -cost}})
    return res.modified_count > 0

def refund_jewels(uid, cost):
    users.update_one({"_id": uid}, {"$inc": {"snow_jewels": cost}})

def get_user_clan(uid):
    u = users.find_one({"_id": uid}) or {}
    cid = u.get("clan_id")
    if not cid:
        return None
    clan = clans.find_one({"_id": cid})
    if not clan or uid not in clan.get("members", []):
        users.update_one({"_id": uid}, {"$unset": {"clan_id": ""}})
        return None
    return clan

def clan_home(chat_id, uid):
    clan = get_user_clan(uid)
    u = users.find_one({"_id": uid}) or {}
    if not clan:
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton(f"➕ Создать клан ({CLAN_CREATE_COST} {SNOW_EMOJI})", callback_data="cl_create"))
        kb.add(types.InlineKeyboardButton("🔎 Список кланов", callback_data="cl_list"))
        return bot.send_message(
            chat_id,
            "🏰 <b>Кланы</b>\n\nВы пока не состоите в клане.\n"
            f"Создать свой клан: <b>{CLAN_CREATE_COST} {SNOW_EMOJI}</b> (у вас: <b>{fmt(u.get('snow_jewels', 0))}</b>)\n"
            "Или вступите по ссылке-приглашению / отправьте заявку из списка.",
            parse_mode="HTML", reply_markup=kb
        )
    is_leader = clan["leader"] == uid
    mgr = is_mgr(clan, uid)
    kb = types.InlineKeyboardMarkup(row_width=2)
    if mgr:
        n = len(clan.get("requests", []))
        kb.add(types.InlineKeyboardButton(f"📩 Заявки({n})" if n else "📩 Заявки", callback_data="cl_req"),
               types.InlineKeyboardButton("⚙️ Настройки", callback_data="cl_set"))
        kb.add(types.InlineKeyboardButton("🔗 Ссылки для вступления", callback_data="cl_links"))
    kb.add(types.InlineKeyboardButton("🏆 Турнир", callback_data="cx_t"),
           types.InlineKeyboardButton("⚔️ Клановый бой", callback_data="cx_w"))
    kb.add(types.InlineKeyboardButton("👹 Босс клана", callback_data="cx_b"))
    kb.add(types.InlineKeyboardButton("💰 Казна", callback_data="cx_tr"),
           types.InlineKeyboardButton("👥 Участники", callback_data="cl_mem"))
    if is_leader:
        kb.add(types.InlineKeyboardButton("🗑 Распустить клан", callback_data="cl_disband"))
    else:
        kb.add(types.InlineKeyboardButton("🚪 Покинуть клан", callback_data="cl_leave"))
    return send_clan_card(chat_id, clan, kb)

@bot.message_handler(commands=["clan"])
@bot.message_handler(func=lambda m: m.text == "🏰 Клан")
def clan_menu(m):
    try:
        get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
        if m.chat.type != "private":
            return clan_group_card(m)
        clan_home(m.chat.id, m.from_user.id)
    except Exception as e:
        logger.error(f"Ошибка clan_menu: {e}")

# --- создание клана ---

def clan_step_media(m):
    try:
        if adm_cancelled(m):
            return bot.send_message(m.chat.id, "❌ Создание клана отменено.")
        fid, mt = extract_media(m, allow_url=False)
        if not fid or mt not in ("photo", "animation"):
            return adm_ask(m.chat.id, "❌ Нужна <b>картинка или гифка</b>. Отправьте ещё раз:", clan_step_media)
        CLAN_DRAFT[m.from_user.id] = {"file_id": fid, "media_type": mt}
        adm_ask(m.chat.id, f"🏷 Теперь введите <b>название клана</b> ({CLAN_NAME_MIN}–{CLAN_NAME_MAX} символов):", clan_step_name)
    except Exception as e:
        logger.error(f"Ошибка clan_step_media: {e}")

def clan_step_name(m):
    try:
        if adm_cancelled(m):
            return bot.send_message(m.chat.id, "❌ Создание клана отменено.")
        ok, err = clan_name_ok(m.text or "")
        if not ok:
            return adm_ask(m.chat.id, err + "\nВведите название ещё раз:", clan_step_name)
        CLAN_DRAFT.setdefault(m.from_user.id, {})["name"] = m.text.strip()
        adm_ask(m.chat.id, f"📝 Теперь введите <b>описание клана</b> (до {CLAN_DESC_MAX} символов):", clan_step_desc)
    except Exception as e:
        logger.error(f"Ошибка clan_step_name: {e}")

def clan_step_desc(m):
    try:
        if adm_cancelled(m):
            return bot.send_message(m.chat.id, "❌ Создание клана отменено.")
        ok, err = clan_desc_ok(m.text or "")
        if not ok:
            return adm_ask(m.chat.id, err + "\nВведите описание ещё раз:", clan_step_desc)
        uid = m.from_user.id
        d = CLAN_DRAFT.setdefault(uid, {})
        d["desc"] = m.text.strip()
        preview = {"name": d["name"], "desc": d["desc"], "leader": uid, "members": [uid],
                   "file_id": d.get("file_id"), "media_type": d.get("media_type", "photo")}
        kb = types.InlineKeyboardMarkup(row_width=1)
        kb.add(types.InlineKeyboardButton(f"✅ Создать ({CLAN_CREATE_COST} {SNOW_EMOJI})", callback_data="cl_ok"),
               types.InlineKeyboardButton("❌ Отмена", callback_data="cl_no"))
        bot.send_message(m.chat.id, "👀 <b>Так будет выглядеть ваш клан:</b>", parse_mode="HTML")
        send_clan_card(m.chat.id, preview, kb)
    except Exception as e:
        logger.error(f"Ошибка clan_step_desc: {e}")

def clan_create_final(uid, chat_id):
    d = CLAN_DRAFT.get(uid)
    if not d or not all(k in d for k in ("file_id", "name", "desc")):
        return "❌ Черновик клана потерян. Начните заново: 🏰 Клан → Создать."
    ok, err = clan_name_ok(d["name"])
    if not ok:
        return err
    cid = uuid.uuid4().hex[:8]
    res = users.update_one(
        {"_id": uid, "snow_jewels": {"$gte": CLAN_CREATE_COST}, "clan_id": None},
        {"$inc": {"snow_jewels": -CLAN_CREATE_COST}, "$set": {"clan_id": cid}}
    )
    if res.modified_count == 0:
        return f"❌ Нужно {CLAN_CREATE_COST} {SNOW_EMOJI} и отсутствие клана."
    try:
        clans.insert_one({
            "_id": cid, "name": d["name"], "name_lower": d["name"].lower(), "desc": d["desc"],
            "file_id": d["file_id"], "media_type": d.get("media_type", "photo"),
            "leader": uid, "members": [uid], "requests": [], "links": [], "created": int(time.time()),
            "level": 1, "xp": 0, "gifted": 0, "deputies": [], "titles": [], "treasury": {"ice": 0.0, "jewel": 0.0},
        })
    except Exception as e:
        logger.error(f"Ошибка создания клана: {e}")
        refund_jewels(uid, CLAN_CREATE_COST)
        users.update_one({"_id": uid}, {"$unset": {"clan_id": ""}})
        return "❌ Не удалось создать клан (возможно, название занято). Jewel возвращены."
    CLAN_DRAFT.pop(uid, None)
    return None

# --- вступление ---

def clan_add_member(clan, uid):
    """Атомарно добавляет игрока в клан. Возвращает текст ошибки или None."""
    u = users.find_one({"_id": uid})
    if not u:
        return "❌ Игрок не найден."
    if u.get("clan_id"):
        return "❌ Игрок уже состоит в клане."
    mx = clan_max_members(clan)
    res = clans.update_one(
        {"_id": clan["_id"], f"members.{mx - 1}": {"$exists": False}},
        {"$addToSet": {"members": uid}, "$pull": {"requests": uid}}
    )
    if res.modified_count == 0:
        return "❌ В клане нет свободных мест."
    users.update_one({"_id": uid}, {"$set": {"clan_id": clan["_id"]}})
    return None

def clan_join_by_code(uid, code):
    clan = clans.find_one({"links": code})
    if not clan:
        return "❌ Ссылка-приглашение недействительна или удалена."
    u = users.find_one({"_id": uid}) or {}
    if u.get("clan_id") == clan["_id"]:
        return f"ℹ️ Вы уже в клане <b>{safe(clan['name'])}</b>."
    err = clan_add_member(clan, uid)
    if err:
        return err
    try:
        bot.send_message(clan["leader"], f"🏰 <b>{safe(disp_name(u))}</b> вступил(а) в ваш клан по ссылке!", parse_mode="HTML")
    except Exception:
        pass
    return f"✅ Вы вступили в клан <b>{safe(clan['name'])}</b>! Откройте 🏰 Клан."

# --- настройки: ввод ---

def clan_set_media_step(m, cid):
    try:
        if adm_cancelled(m):
            return bot.send_message(m.chat.id, "❌ Отменено.")
        fid, mt = extract_media(m, allow_url=False)
        if not fid or mt not in ("photo", "animation"):
            return adm_ask(m.chat.id, "❌ Нужна картинка или гифка. Отправьте ещё раз:", clan_set_media_step, cid)
        uid = m.from_user.id
        clan = clans.find_one({"_id": cid})
        if not clan or clan["leader"] != uid:
            return bot.send_message(m.chat.id, "❌ Нет доступа.")
        if not pay_jewels(uid, CLAN_MEDIA_COST):
            return bot.send_message(m.chat.id, f"❌ Нужно {CLAN_MEDIA_COST} {SNOW_EMOJI}.")
        clans.update_one({"_id": cid}, {"$set": {"file_id": fid, "media_type": mt}})
        bot.send_message(m.chat.id, f"✅ Медиа клана обновлено (−{CLAN_MEDIA_COST} {SNOW_EMOJI}).", parse_mode="HTML")
        clan_home(m.chat.id, uid)
    except Exception as e:
        logger.error(f"Ошибка clan_set_media_step: {e}")

def clan_set_name_step(m, cid):
    try:
        if adm_cancelled(m):
            return bot.send_message(m.chat.id, "❌ Отменено.")
        uid = m.from_user.id
        clan = clans.find_one({"_id": cid})
        if not clan or clan["leader"] != uid:
            return bot.send_message(m.chat.id, "❌ Нет доступа.")
        ok, err = clan_name_ok(m.text or "", exclude_id=cid)
        if not ok:
            return adm_ask(m.chat.id, err + "\nВведите название ещё раз:", clan_set_name_step, cid)
        if not pay_jewels(uid, CLAN_RENAME_COST):
            return bot.send_message(m.chat.id, f"❌ Нужно {CLAN_RENAME_COST} {SNOW_EMOJI}.")
        name = m.text.strip()
        try:
            clans.update_one({"_id": cid}, {"$set": {"name": name, "name_lower": name.lower()}})
        except Exception:
            refund_jewels(uid, CLAN_RENAME_COST)
            return bot.send_message(m.chat.id, "❌ Не удалось сменить название.")
        bot.send_message(m.chat.id, f"✅ Название изменено (−{CLAN_RENAME_COST} {SNOW_EMOJI}).", parse_mode="HTML")
        clan_home(m.chat.id, uid)
    except Exception as e:
        logger.error(f"Ошибка clan_set_name_step: {e}")

def clan_set_desc_step(m, cid):
    try:
        if adm_cancelled(m):
            return bot.send_message(m.chat.id, "❌ Отменено.")
        uid = m.from_user.id
        clan = clans.find_one({"_id": cid})
        if not clan or clan["leader"] != uid:
            return bot.send_message(m.chat.id, "❌ Нет доступа.")
        ok, err = clan_desc_ok(m.text or "")
        if not ok:
            return adm_ask(m.chat.id, err + "\nВведите описание ещё раз:", clan_set_desc_step, cid)
        clans.update_one({"_id": cid}, {"$set": {"desc": m.text.strip()}})
        bot.send_message(m.chat.id, "✅ Описание обновлено.")
        clan_home(m.chat.id, uid)
    except Exception as e:
        logger.error(f"Ошибка clan_set_desc_step: {e}")

def clan_remove_member(clan, target_uid):
    clans.update_one({"_id": clan["_id"]}, {"$pull": {"members": target_uid, "deputies": target_uid}})
    users.update_one({"_id": target_uid, "clan_id": clan["_id"]}, {"$unset": {"clan_id": ""}})

def show_requests(chat_id, cid):
    clan = clans.find_one({"_id": cid})
    reqs = (clan or {}).get("requests", [])
    if not reqs:
        return bot.send_message(chat_id, "📭 Новых заявок нет.")
    names = user_names(reqs)
    kb = types.InlineKeyboardMarkup(row_width=2)
    for i in reqs:
        kb.add(types.InlineKeyboardButton(f"✅ {names.get(i, i)}", callback_data=f"cl_ra_{i}"),
               types.InlineKeyboardButton("❌", callback_data=f"cl_rd_{i}"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="cl_home"))
    return bot.send_message(chat_id, "📩 <b>Заявки на вступление:</b>", parse_mode="HTML", reply_markup=kb)

# --- единый обработчик кнопок клана ---

@bot.callback_query_handler(func=lambda c: c.data.startswith("cl_"))
def clan_callback(c):
    uid = c.from_user.id
    chat_id = c.message.chat.id
    d = c.data
    try:
        u = users.find_one({"_id": uid})
        if not u:
            return bot.answer_callback_query(c.id, "Сначала /start")
        clan = get_user_clan(uid)
        is_leader = bool(clan and clan["leader"] == uid)
        is_manager = is_mgr(clan, uid)

        # ввод текста работает только в личке (в чатах next-step ловил бы чужие сообщения)
        if d in ("cl_create", "cl_s_media", "cl_s_name", "cl_s_desc") and c.message.chat.type != "private":
            return bot.answer_callback_query(c.id, "Откройте бота в личке — там идёт ввод данных", show_alert=True)

        if d == "cl_home":
            del_msg(c); bot.answer_callback_query(c.id)
            return clan_home(chat_id, uid)

        # ----- создание -----
        if d == "cl_create":
            if clan:
                return bot.answer_callback_query(c.id, "Вы уже состоите в клане", show_alert=True)
            if float(u.get("snow_jewels", 0)) < CLAN_CREATE_COST:
                return bot.answer_callback_query(c.id, f"Нужно {CLAN_CREATE_COST} Snow Jewel", show_alert=True)
            bot.answer_callback_query(c.id)
            CLAN_DRAFT.pop(uid, None)
            return adm_ask(uid, "📸 Отправьте <b>картинку или гифку</b> для клана:", clan_step_media)
        if d == "cl_no":
            CLAN_DRAFT.pop(uid, None)
            del_msg(c)
            return bot.answer_callback_query(c.id, "Отменено")
        if d == "cl_ok":
            err = clan_create_final(uid, chat_id)
            if err:
                return bot.answer_callback_query(c.id, err[:190], show_alert=True)
            del_msg(c); bot.answer_callback_query(c.id, "🏰 Клан создан!")
            return clan_home(chat_id, uid)

        # ----- список / заявки -----
        if d == "cl_list":
            bot.answer_callback_query(c.id)
            top = list(clans.find().sort([("created", -1)]).limit(15))
            if not top:
                return bot.send_message(chat_id, "📭 Пока нет ни одного клана. Создайте первый!")
            kb = types.InlineKeyboardMarkup(row_width=1)
            for cl in top:
                kb.add(types.InlineKeyboardButton(f"🏰 {cl['name']} ({len(cl.get('members', []))}/{clan_max_members(cl)})", callback_data=f"cl_v_{cl['_id']}"))
            return bot.send_message(chat_id, "🔎 <b>Кланы:</b>", parse_mode="HTML", reply_markup=kb)
        if d.startswith("cl_v_"):
            cl = clans.find_one({"_id": d[len("cl_v_"):]})
            if not cl:
                return bot.answer_callback_query(c.id, "Клан не найден", show_alert=True)
            kb = None
            if not clan:
                kb = types.InlineKeyboardMarkup()
                kb.add(types.InlineKeyboardButton("📨 Подать заявку", callback_data=f"cl_ap_{cl['_id']}"))
            bot.answer_callback_query(c.id)
            return send_clan_card(chat_id, cl, kb)
        if d.startswith("cl_ap_"):
            cl = clans.find_one({"_id": d[len("cl_ap_"):]})
            if not cl:
                return bot.answer_callback_query(c.id, "Клан не найден", show_alert=True)
            if clan:
                return bot.answer_callback_query(c.id, "Вы уже в клане", show_alert=True)
            if uid in cl.get("requests", []):
                return bot.answer_callback_query(c.id, "Заявка уже отправлена", show_alert=True)
            if len(cl.get("members", [])) >= clan_max_members(cl):
                return bot.answer_callback_query(c.id, "В клане нет мест", show_alert=True)
            clans.update_one({"_id": cl["_id"]}, {"$addToSet": {"requests": uid}})
            bot.answer_callback_query(c.id, "✅ Заявка отправлена!")
            try:
                bot.send_message(cl["leader"], f"📩 Новая заявка в клан <b>{safe(cl['name'])}</b> от <b>{safe(disp_name(u))}</b>. Откройте 🏰 Клан → Заявки.", parse_mode="HTML")
            except Exception:
                pass
            return

        # ----- дальше нужен клан -----
        if not clan:
            return bot.answer_callback_query(c.id, "Вы не состоите в клане", show_alert=True)

        if d == "cl_mem":
            ids = [clan["leader"]] + [x for x in clan.get("members", []) if x != clan["leader"]]
            names = user_names(ids)
            deps = set(clan.get("deputies", []))
            kb = types.InlineKeyboardMarkup(row_width=2)
            lines = [f"👥 <b>Участники клана</b> ({len(ids)}/{clan_max_members(clan)})\n"]
            for i in ids:
                mark = "👑" if i == clan["leader"] else ("🛡" if i in deps else "▫️")
                lines.append(f"{mark} {mention_html(i, names.get(i, i))}")
                if i == clan["leader"]:
                    continue
                btns = []
                if is_leader:
                    btns.append(types.InlineKeyboardButton("⬇️ Снять зама" if i in deps else "⬆️ Сделать замом",
                                                           callback_data=f"cl_dm_{i}" if i in deps else f"cl_pm_{i}"))
                if is_leader or (is_manager and i not in deps):
                    btns.append(types.InlineKeyboardButton(f"🚫 {names.get(i, i)}"[:30], callback_data=f"cl_k_{i}"))
                if btns:
                    kb.add(*btns)
            kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="cl_home"))
            bot.answer_callback_query(c.id)
            return bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML", reply_markup=kb)

        if d == "cl_leave":
            if is_leader:
                return bot.answer_callback_query(c.id, "Лидер не может выйти — распустите клан", show_alert=True)
            clan_remove_member(clan, uid)
            del_msg(c); bot.answer_callback_query(c.id, "Вы покинули клан")
            return clan_home(chat_id, uid)

        # ----- лидер и замы -----
        if not is_manager:
            return bot.answer_callback_query(c.id, "Только для лидера и замов клана", show_alert=True)

        if d.startswith("cl_pm_") or d.startswith("cl_dm_"):
            if not is_leader:
                return bot.answer_callback_query(c.id, "Назначать замов может только лидер", show_alert=True)
            target = int(d.split("_")[2])
            if target == clan["leader"] or target not in clan.get("members", []):
                return bot.answer_callback_query(c.id, "Нельзя", show_alert=True)
            if d.startswith("cl_pm_"):
                clans.update_one({"_id": clan["_id"]}, {"$addToSet": {"deputies": target}})
                try:
                    bot.send_message(target, f"🛡 Вас назначили замом клана <b>{safe(clan['name'])}</b>!", parse_mode="HTML")
                except Exception:
                    pass
                bot.answer_callback_query(c.id, "✅ Назначен замом")
            else:
                clans.update_one({"_id": clan["_id"]}, {"$pull": {"deputies": target}})
                bot.answer_callback_query(c.id, "Зам снят")
            del_msg(c)
            return clan_home(chat_id, uid)

        if d == "cl_req":
            bot.answer_callback_query(c.id)
            return show_requests(chat_id, clan["_id"])
        if d.startswith("cl_ra_") or d.startswith("cl_rd_"):
            target = int(d.split("_")[2])
            if target not in clan.get("requests", []):
                return bot.answer_callback_query(c.id, "Заявка уже обработана", show_alert=True)
            if d.startswith("cl_ra_"):
                err = clan_add_member(clan, target)
                if err:
                    clans.update_one({"_id": clan["_id"]}, {"$pull": {"requests": target}})
                    return bot.answer_callback_query(c.id, err[:190], show_alert=True)
                try:
                    bot.send_message(target, f"🎉 Вашу заявку приняли! Вы в клане <b>{safe(clan['name'])}</b>.", parse_mode="HTML")
                except Exception:
                    pass
                bot.answer_callback_query(c.id, "✅ Принят")
            else:
                clans.update_one({"_id": clan["_id"]}, {"$pull": {"requests": target}})
                try:
                    bot.send_message(target, f"❌ Заявка в клан <b>{safe(clan['name'])}</b> отклонена.", parse_mode="HTML")
                except Exception:
                    pass
                bot.answer_callback_query(c.id, "Отклонено")
            del_msg(c)
            return show_requests(chat_id, clan["_id"])

        if d.startswith("cl_k_"):
            target = int(d[len("cl_k_"):])
            if target == clan["leader"] or target not in clan.get("members", []):
                return bot.answer_callback_query(c.id, "Нельзя выгнать", show_alert=True)
            if target in clan.get("deputies", []) and not is_leader:
                return bot.answer_callback_query(c.id, "Зама может исключить только лидер", show_alert=True)
            clan_remove_member(clan, target)
            try:
                bot.send_message(target, f"🚫 Вас исключили из клана <b>{safe(clan['name'])}</b>.", parse_mode="HTML")
            except Exception:
                pass
            del_msg(c); bot.answer_callback_query(c.id, "Игрок исключён")
            return clan_home(chat_id, uid)

        if d == "cl_set":
            kb = types.InlineKeyboardMarkup(row_width=1)
            kb.add(types.InlineKeyboardButton(f"🖼 Фото/гиф клана ({CLAN_MEDIA_COST} {SNOW_EMOJI})", callback_data="cl_s_media"),
                   types.InlineKeyboardButton(f"✏️ Название ({CLAN_RENAME_COST} {SNOW_EMOJI})", callback_data="cl_s_name"),
                   types.InlineKeyboardButton("📝 Описание (бесплатно)", callback_data="cl_s_desc"),
                   types.InlineKeyboardButton("⬅️ Назад", callback_data="cl_home"))
            bot.answer_callback_query(c.id)
            return bot.send_message(chat_id, f"⚙️ <b>Настройки клана</b>\nУ вас: <b>{fmt(u.get('snow_jewels', 0))}</b> {SNOW_EMOJI}", parse_mode="HTML", reply_markup=kb)
        if d == "cl_s_media":
            if float(u.get("snow_jewels", 0)) < CLAN_MEDIA_COST:
                return bot.answer_callback_query(c.id, f"Нужно {CLAN_MEDIA_COST} Snow Jewel", show_alert=True)
            bot.answer_callback_query(c.id)
            return adm_ask(uid, "📸 Отправьте новую <b>картинку или гифку</b> клана:", clan_set_media_step, clan["_id"])
        if d == "cl_s_name":
            if float(u.get("snow_jewels", 0)) < CLAN_RENAME_COST:
                return bot.answer_callback_query(c.id, f"Нужно {CLAN_RENAME_COST} Snow Jewel", show_alert=True)
            bot.answer_callback_query(c.id)
            return adm_ask(uid, "🏷 Введите <b>новое название</b> клана:", clan_set_name_step, clan["_id"])
        if d == "cl_s_desc":
            bot.answer_callback_query(c.id)
            return adm_ask(uid, f"📝 Введите <b>новое описание</b> (до {CLAN_DESC_MAX} символов):", clan_set_desc_step, clan["_id"])

        if d in ("cl_links", "cl_lnew") or d.startswith("cl_ld_"):
            if d == "cl_lnew":
                if len(clan.get("links", [])) >= CLAN_MAX_LINKS:
                    return bot.answer_callback_query(c.id, f"Максимум {CLAN_MAX_LINKS} ссылки", show_alert=True)
                clans.update_one({"_id": clan["_id"]}, {"$push": {"links": uuid.uuid4().hex[:8]}})
            elif d.startswith("cl_ld_"):
                clans.update_one({"_id": clan["_id"]}, {"$pull": {"links": d[len("cl_ld_"):]}})
            clan = clans.find_one({"_id": clan["_id"]})
            links = clan.get("links", [])
            uname = bot_username()
            text = (f"🔗 <b>Ссылки для вступления</b> ({len(links)}/{CLAN_MAX_LINKS})\n"
                    "<i>Вступление по ссылке — без заявок.</i>\n\n")
            kb = types.InlineKeyboardMarkup(row_width=1)
            for i, code in enumerate(links, 1):
                text += f"{i}. <code>https://t.me/{uname}?start=clan_{code}</code>\n"
                kb.add(types.InlineKeyboardButton(f"🗑 Удалить ссылку {i}", callback_data=f"cl_ld_{code}"))
            if len(links) < CLAN_MAX_LINKS:
                kb.add(types.InlineKeyboardButton("➕ Создать ссылку", callback_data="cl_lnew"))
            kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="cl_home"))
            bot.answer_callback_query(c.id)
            if d != "cl_links":
                del_msg(c)
            return bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

        if d in ("cl_disband", "cl_disband_yes") and not is_leader:
            return bot.answer_callback_query(c.id, "Только лидер может распустить клан", show_alert=True)
        if d == "cl_disband":
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("⚠️ Да, распустить", callback_data="cl_disband_yes"),
                   types.InlineKeyboardButton("❌ Нет", callback_data="cl_home"))
            bot.answer_callback_query(c.id)
            return bot.send_message(chat_id, "⚠️ <b>Распустить клан?</b> Все участники будут исключены, действие необратимо.", parse_mode="HTML", reply_markup=kb)
        if d == "cl_disband_yes":
            members = list(clan.get("members", []))
            clans.delete_one({"_id": clan["_id"]})
            users.update_many({"_id": {"$in": members}}, {"$unset": {"clan_id": ""}})
            for mid in members:
                if mid != uid:
                    try:
                        bot.send_message(mid, f"🗑 Клан <b>{safe(clan['name'])}</b> был распущен.", parse_mode="HTML")
                    except Exception:
                        pass
            del_msg(c)
            return bot.answer_callback_query(c.id, "Клан распущен")

        bot.answer_callback_query(c.id)
    except Exception as e:
        logger.error(f"Ошибка clan_callback ({d}): {e}")
        try:
            bot.answer_callback_query(c.id, "❌ Ошибка")
        except Exception:
            pass

# ---------------------------------------------------------------
# АДМИН: ГЛАВНОЕ МЕНЮ (кнопки)
# ---------------------------------------------------------------

def admin_main_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("🍂 Сезонный пасс", callback_data="am_pass"),
           types.InlineKeyboardButton("🎁 Каталог предметов", callback_data="am_items"))
    kb.add(types.InlineKeyboardButton("🏰 Кланы и турниры", callback_data="am_clans"))
    kb.add(types.InlineKeyboardButton("👤 Игрок по ID", callback_data="am_user"),
           types.InlineKeyboardButton("📢 Рассылка", callback_data="am_bc"))
    kb.add(types.InlineKeyboardButton("❌ Закрыть", callback_data="am_close"))
    return kb

def admin_stats_text():
    total_users = users.count_documents({})
    result = list(users.aggregate([{"$group": {"_id": None, "total": {"$sum": "$balance"}}}]))
    total_sum = result[0]["total"] if result else 0
    return (f"👑 <b>АДМИН-ПАНЕЛЬ</b>\n\n"
            f"👥 Всего пользователей: <b>{total_users}</b>\n"
            f"💰 Всего в обороте: <b>{fmt(total_sum)} ICE</b>\n"
            f"🏰 Кланов: <b>{clans.count_documents({})}</b>\n\n"
            f"<b>Ещё команды:</b>\n"
            f"/stats ID — управление игроком\n"
            f"/give ID СУММА — выдать ICE\n"
            f"/vipon — выдать VIP\n"
            f"/reset_season — новый сезон лиги\n"
            f"/add_recipe — рецепт крафта\n"
            f"/passadmin — пасс · /items — каталог\n"
            f"/clanadmin — кланы и турниры")

def user_stats_step(m):
    if not is_admin(m.from_user.id) or adm_cancelled(m):
        return
    try:
        m.text = f"/stats {int((m.text or '').strip())}"
        admin_manage_user(m)
    except Exception:
        bot.send_message(m.chat.id, "❌ ID должен быть числом.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("am_"))
def admin_menu_callback(c):
    if not is_admin(c.from_user.id):
        return bot.answer_callback_query(c.id)
    d = c.data
    chat_id = c.message.chat.id
    try:
        bot.answer_callback_query(c.id)
        if d == "am_close":
            return del_msg(c)
        if d == "am_home":
            del_msg(c)
            return bot.send_message(chat_id, admin_stats_text(), parse_mode="HTML", reply_markup=admin_main_kb())
        if d == "am_pass":
            del_msg(c)
            return pass_admin_home(chat_id)
        if d == "am_items":
            del_msg(c)
            return items_admin_home(chat_id)
        if d == "am_clans":
            del_msg(c)
            return ct_home(chat_id)
        if d == "am_user":
            return adm_ask(chat_id, "🆔 Введите <b>ID игрока</b>:", user_stats_step)
        if d == "am_bc":
            return bot.register_next_step_handler(
                bot.send_message(chat_id, "Введите текст или пришлите фото. /cancel для отмены"), start_broadcast)
    except Exception as e:
        logger.error(f"Ошибка admin_menu_callback: {e}")

# ---------------------------------------------------------------
# АДМИН: КАТАЛОГ ПРЕДМЕТОВ (/items)
# ---------------------------------------------------------------

def items_admin_home(chat_id):
    items = list(catalog.find().sort("name", 1).limit(60))
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("➕ Создать предмет", callback_data="it_new"))
    for t in items:
        kb.add(types.InlineKeyboardButton(item_plain(t), callback_data=f"it_v_{t['_id']}"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="am_home"))
    bot.send_message(
        chat_id,
        "🎁 <b>Каталог предметов</b>\n\nЗдесь создаются NFT, эмодзи, фото профиля, NFT-гифки фарма, ID Changer и титулы — "
        "любого размера. Их можно выдавать игрокам и ставить наградами в сезонном пассе.",
        parse_mode="HTML", reply_markup=kb
    )

@bot.message_handler(commands=["items"])
def items_cmd(m):
    if is_admin(m.from_user.id):
        items_admin_home(m.chat.id)

def it_step_name(m, aid):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    if not m.text:
        return adm_ask(m.chat.id, "❌ Введите название текстом:", it_step_name, aid)
    ADM_DRAFT[aid]["name"] = m.text.strip()
    adm_ask(m.chat.id, "📝 Введите <b>описание</b> (или «-» чтобы пропустить):", it_step_desc, aid)

def it_step_desc(m, aid):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    dr = ADM_DRAFT[aid]
    dr["desc"] = "" if (m.text or "").strip() == "-" else (m.text or "").strip()
    if dr["kind"] == "emoji":
        return adm_ask(m.chat.id, "😀 Отправьте <b>эмодзи</b> (один), который появится в профиле:", it_step_emoji, aid)
    if dr["kind"] == "title":
        return adm_ask(m.chat.id, "🏷 Введите <b>текст титула</b> (например «🍁 Autumn Legend»):", it_step_title, aid)
    it_ask_media(m.chat.id, aid)

def it_step_emoji(m, aid):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    if not m.text or len(m.text.strip()) > 8:
        return adm_ask(m.chat.id, "❌ Нужен один эмодзи. Отправьте ещё раз:", it_step_emoji, aid)
    ADM_DRAFT[aid]["emoji"] = m.text.strip()
    it_ask_media(m.chat.id, aid)

def it_step_title(m, aid):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    if not m.text or len(m.text.strip()) > 40:
        return adm_ask(m.chat.id, "❌ Титул до 40 символов. Введите ещё раз:", it_step_title, aid)
    ADM_DRAFT[aid]["title"] = m.text.strip()
    it_ask_media(m.chat.id, aid)

def it_ask_media(chat_id, aid):
    dr = ADM_DRAFT[aid]
    required = dr["kind"] in MEDIA_REQUIRED_KINDS
    adm_ask(chat_id,
            "🖼 Пришлите <b>медиа</b>: фото, гиф, видео или файл (любой размер), либо прямую ссылку."
            + ("" if required else "\nМожно пропустить — отправьте «-»."),
            it_step_media, aid)

def it_ask_rarity(chat_id):
    kb = types.InlineKeyboardMarkup(row_width=3)
    kb.add(types.InlineKeyboardButton("🔵 rare", callback_data="it_r_rare"),
           types.InlineKeyboardButton("🟣 epic", callback_data="it_r_epic"),
           types.InlineKeyboardButton("🟡 legendary", callback_data="it_r_legendary"))
    bot.send_message(chat_id, "💎 Выберите <b>редкость</b>:", parse_mode="HTML", reply_markup=kb)

def it_step_media(m, aid):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    dr = ADM_DRAFT[aid]
    required = dr["kind"] in MEDIA_REQUIRED_KINDS
    if m.content_type == "text" and (m.text or "").strip() == "-" and not required:
        dr["file_id"], dr["media_type"] = None, None
        return it_ask_rarity(m.chat.id)
    fid, mt = extract_media(m)
    if not fid:
        return it_ask_media(m.chat.id, aid)
    dr["file_id"], dr["media_type"] = fid, mt
    it_ask_rarity(m.chat.id)

def it_step_give(m, tpl_id):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    try:
        target = int((m.text or "").strip())
    except Exception:
        return bot.send_message(m.chat.id, "❌ ID должен быть числом.")
    tpl = catalog.find_one({"_id": tpl_id})
    if not tpl or not users.find_one({"_id": target}):
        return bot.send_message(m.chat.id, "❌ Предмет или игрок не найден.")
    give_item(target, tpl)
    bot.send_message(m.chat.id, f"✅ «{safe(tpl['name'])}» выдан игроку <code>{target}</code>.", parse_mode="HTML")
    try:
        bot.send_message(target, f"🎁 Вы получили предмет: <b>{safe(tpl['name'])}</b>\n<i>{safe(tpl.get('desc', ''))}</i>", parse_mode="HTML")
    except Exception:
        pass

def it_step_edit(m, tpl_id, field):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    if not m.text:
        return bot.send_message(m.chat.id, "❌ Нужен текст.")
    catalog.update_one({"_id": tpl_id}, {"$set": {field: m.text.strip()}})
    bot.send_message(m.chat.id, "✅ Сохранено. Новые выдачи будут с изменением (у уже выданных предметов данные не меняются).")

def it_step_newmedia(m, tpl_id):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    fid, mt = extract_media(m)
    if not fid:
        return adm_ask(m.chat.id, "❌ Это не медиа. Пришлите фото/гиф/видео/файл или ссылку:", it_step_newmedia, tpl_id)
    catalog.update_one({"_id": tpl_id}, {"$set": {"file_id": fid, "media_type": mt}})
    bot.send_message(m.chat.id, "✅ Медиа заменено. Применится к новым выдачам.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("it_"))
def items_callback(c):
    if not is_admin(c.from_user.id):
        return bot.answer_callback_query(c.id)
    d = c.data
    aid = c.from_user.id
    chat_id = c.message.chat.id
    try:
        bot.answer_callback_query(c.id)
        if d == "it_list":
            del_msg(c)
            return items_admin_home(chat_id)
        if d == "it_new":
            kb = types.InlineKeyboardMarkup(row_width=1)
            for k, label in ITEM_KINDS.items():
                kb.add(types.InlineKeyboardButton(label, callback_data=f"it_k_{k}"))
            return bot.send_message(chat_id, "Выберите <b>тип предмета</b>:", parse_mode="HTML", reply_markup=kb)
        if d.startswith("it_k_"):
            kind = d[len("it_k_"):]
            if kind not in ITEM_KINDS:
                return
            ADM_DRAFT[aid] = {"kind": kind}
            return adm_ask(chat_id, "🏷 Введите <b>название</b> предмета:", it_step_name, aid)
        if d.startswith("it_r_"):
            dr = ADM_DRAFT.pop(aid, None)
            if not dr or "name" not in dr:
                return bot.send_message(chat_id, "❌ Черновик потерян. Начните заново: /items")
            tid = uuid.uuid4().hex[:10]
            doc = {"_id": tid, "name": dr["name"], "desc": dr.get("desc", ""), "kind": dr["kind"],
                   "emoji": dr.get("emoji"), "title": dr.get("title"), "file_id": dr.get("file_id"),
                   "media_type": dr.get("media_type"), "rarity": d[len("it_r_"):]}
            catalog.insert_one(doc)
            del_msg(c)
            bot.send_message(chat_id, f"✅ Предмет <b>{safe(dr['name'])}</b> создан (ID <code>{tid}</code>).", parse_mode="HTML")
            return items_admin_home(chat_id)
        if d.startswith("it_v_"):
            tpl = catalog.find_one({"_id": d[len("it_v_"):]})
            if not tpl:
                return bot.send_message(chat_id, "❌ Предмет не найден.")
            kb = types.InlineKeyboardMarkup(row_width=2)
            kb.add(types.InlineKeyboardButton("🎁 Выдать игроку", callback_data=f"it_give_{tpl['_id']}"),
                   types.InlineKeyboardButton("🖼 Заменить медиа", callback_data=f"it_media_{tpl['_id']}"))
            kb.add(types.InlineKeyboardButton("✏️ Название", callback_data=f"it_ed_name_{tpl['_id']}"),
                   types.InlineKeyboardButton("📝 Описание", callback_data=f"it_ed_desc_{tpl['_id']}"))
            kb.add(types.InlineKeyboardButton("🗑 Удалить", callback_data=f"it_del_{tpl['_id']}"),
                   types.InlineKeyboardButton("⬅️ К списку", callback_data="it_list"))
            text = (f"{item_label(tpl)}\n"
                    f"Тип: <b>{ITEM_KINDS.get(tpl.get('kind'), '?')}</b>\n"
                    f"Редкость: <b>{tpl.get('rarity', '-')}</b>\n"
                    f"ID: <code>{tpl['_id']}</code>\n"
                    f"<i>{safe(tpl.get('desc', ''))}</i>")
            return send_media_safe(chat_id, tpl.get("media_type"), tpl.get("file_id"), text, kb)
        if d.startswith("it_give_"):
            return adm_ask(chat_id, "🆔 Введите <b>ID игрока</b>, которому выдать предмет:", it_step_give, d[len("it_give_"):])
        if d.startswith("it_media_"):
            return adm_ask(chat_id, "🖼 Пришлите новое медиа (фото/гиф/видео/файл любого размера или ссылку):", it_step_newmedia, d[len("it_media_"):])
        if d.startswith("it_ed_"):
            _, _, field, tid = d.split("_", 3)
            if field not in ("name", "desc"):
                return
            return adm_ask(chat_id, "✏️ Введите новый текст:", it_step_edit, tid, field)
        if d.startswith("it_delyes_"):
            catalog.delete_one({"_id": d[len("it_delyes_"):]})
            del_msg(c)
            return bot.send_message(chat_id, "🗑 Предмет удалён из каталога (у игроков он остаётся).")
        if d.startswith("it_del_"):
            tid = d[len("it_del_"):]
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("⚠️ Да, удалить", callback_data=f"it_delyes_{tid}"),
                   types.InlineKeyboardButton("❌ Нет", callback_data="it_list"))
            return bot.send_message(chat_id, "Удалить предмет из каталога? Если он стоит наградой в пассе — награда пропадёт.", reply_markup=kb)
    except Exception as e:
        logger.error(f"Ошибка items_callback ({d}): {e}")

# ---------------------------------------------------------------
# АДМИН: СЕЗОННЫЙ ПАСС (/passadmin)
# ---------------------------------------------------------------

def pass_admin_text(cfg):
    lines = [
        "🍂 <b>Управление сезонным пассом</b>",
        f"ID сезона: <code>{cfg['season_id']}</code>",
        f"Название: {safe(cfg.get('name'))}",
        f"Описание: {safe(cfg.get('desc'))}",
        f"Баннер: {'✅' if cfg.get('banner') else '❌'}",
        f"VIP-бонус прогресса: +{int(float(cfg.get('vip_progress_bonus', 0)) * 100)}%",
        "", "<b>Ранги:</b>",
    ]
    for i, r in enumerate(cfg.get("ranks", []), 1):
        line = f"{i}. 🔥{fmt(r['need'])} → {reward_text(r.get('reward'))}"
        if r.get("vip") and r["vip"].get("type") not in (None, "none"):
            line += f" | 👑 {reward_text(r['vip'])}"
        lines.append(line)
    return "\n".join(lines)

def pass_admin_home(chat_id):
    cfg = pass_cfg()
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("📝 Название", callback_data="ps_name"),
           types.InlineKeyboardButton("📜 Описание", callback_data="ps_desc"))
    kb.add(types.InlineKeyboardButton("🖼 Баннер", callback_data="ps_banner"),
           types.InlineKeyboardButton("👑 VIP-бонус %", callback_data="ps_vipb"))
    kb.add(types.InlineKeyboardButton("🏆 Ранги и награды", callback_data="ps_ranks"))
    kb.add(types.InlineKeyboardButton("🆕 Новый сезон", callback_data="ps_new"),
           types.InlineKeyboardButton("♻️ Обнулить прогресс", callback_data="ps_reset"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="am_home"))
    bot.send_message(chat_id, pass_admin_text(cfg), parse_mode="HTML", reply_markup=kb)

@bot.message_handler(commands=["passadmin"])
def passadmin_cmd(m):
    if is_admin(m.from_user.id):
        pass_admin_home(m.chat.id)

def ps_save(**fields):
    settings.update_one({"_id": "season_pass"}, {"$set": fields})

def ps_rank_menu(chat_id, idx):
    cfg = pass_cfg()
    ranks = cfg.get("ranks", [])
    if idx >= len(ranks):
        return pass_admin_ranks(chat_id)
    r = ranks[idx]
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("🔥 Порог", callback_data=f"ps_rn_{idx}"),
           types.InlineKeyboardButton("🎁 Награда", callback_data=f"ps_rw_{idx}_f"))
    kb.add(types.InlineKeyboardButton("👑 VIP-награда", callback_data=f"ps_rw_{idx}_v"),
           types.InlineKeyboardButton("🗑 Удалить ранг", callback_data=f"ps_rd_{idx}"))
    kb.add(types.InlineKeyboardButton("⬅️ К рангам", callback_data="ps_ranks"))
    bot.send_message(
        chat_id,
        f"🏆 <b>Ранг {idx + 1}</b>\n🔥 Порог: <b>{fmt(r['need'])}</b> ICE\n"
        f"🎁 Награда: {reward_text(r.get('reward'))}\n👑 VIP: {reward_text(r.get('vip'))}",
        parse_mode="HTML", reply_markup=kb
    )

def pass_admin_ranks(chat_id):
    cfg = pass_cfg()
    kb = types.InlineKeyboardMarkup(row_width=1)
    for i, r in enumerate(cfg.get("ranks", [])):
        plain = re.sub(r"<[^>]+>", "", reward_text(r.get("reward")))
        kb.add(types.InlineKeyboardButton(f"{i + 1}. 🔥{fmt(r['need'])} → {plain}", callback_data=f"ps_r_{i}"))
    kb.add(types.InlineKeyboardButton("➕ Добавить ранг", callback_data="ps_radd"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="ps_home"))
    bot.send_message(chat_id, "🏆 <b>Ранги сезона</b> — выберите ранг для редактирования:", parse_mode="HTML", reply_markup=kb)

def ps_sort_ranks(ranks):
    return sorted(ranks, key=lambda r: r["need"])

def ps_step_text(m, field, limit):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    if not m.text or len(m.text.strip()) > limit:
        return adm_ask(m.chat.id, f"❌ Нужен текст до {limit} символов. Введите ещё раз:", ps_step_text, field, limit)
    ps_save(**{field: m.text.strip()})
    pass_admin_home(m.chat.id)

def ps_step_banner(m):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    fid, mt = extract_media(m)
    if not fid:
        return adm_ask(m.chat.id, "❌ Нужно фото/гиф или ссылка. Пришлите ещё раз:", ps_step_banner)
    ps_save(banner=fid, banner_type=mt)
    pass_admin_home(m.chat.id)

def ps_step_vipb(m):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    try:
        pct = float((m.text or "").replace(",", ".").strip())
        if pct < 0 or pct > 500:
            raise ValueError
    except Exception:
        return adm_ask(m.chat.id, "❌ Введите число процентов (0–500):", ps_step_vipb)
    ps_save(vip_progress_bonus=round(pct / 100, 4))
    pass_admin_home(m.chat.id)

def ps_step_need(m, idx):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    v = parse_num(m.text or "")
    if v is None:
        return adm_ask(m.chat.id, "❌ Введите число больше нуля:", ps_step_need, idx)
    cfg = pass_cfg()
    ranks = cfg.get("ranks", [])
    if idx >= len(ranks):
        return pass_admin_ranks(m.chat.id)
    ranks[idx]["need"] = int(v) if float(v).is_integer() else v
    ps_save(ranks=ps_sort_ranks(ranks))
    pass_admin_ranks(m.chat.id)

def ps_step_add(m):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    v = parse_num(m.text or "")
    if v is None:
        return adm_ask(m.chat.id, "❌ Введите число больше нуля:", ps_step_add)
    cfg = pass_cfg()
    ranks = cfg.get("ranks", [])
    ranks.append({"need": int(v) if float(v).is_integer() else v, "reward": {"type": "none"}, "vip": {"type": "none"}})
    ranks = ps_sort_ranks(ranks)
    ps_save(ranks=ranks)
    pass_admin_ranks(m.chat.id)

def ps_set_reward(idx, slot, reward):
    cfg = pass_cfg()
    ranks = cfg.get("ranks", [])
    if idx < len(ranks):
        ranks[idx]["reward" if slot == "f" else "vip"] = reward
        ps_save(ranks=ranks)

def ps_step_amount(m, idx, slot, rtype, case_key):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    v = parse_num(m.text or "")
    if v is None:
        return adm_ask(m.chat.id, "❌ Введите число больше нуля:", ps_step_amount, idx, slot, rtype, case_key)
    if rtype == "case":
        reward = {"type": "case", "case": case_key, "amount": int(v)}
    else:
        reward = {"type": rtype, "amount": v}
    ps_set_reward(idx, slot, reward)
    ps_rank_menu(m.chat.id, idx)

def ps_step_newseason(m):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    if not m.text or len(m.text.strip()) > 60:
        return adm_ask(m.chat.id, "❌ Название до 60 символов. Введите ещё раз:", ps_step_newseason)
    sid = "s" + str(int(time.time()))
    ps_save(season_id=sid, name=m.text.strip(), started=int(time.time()))
    bot.send_message(m.chat.id, f"✅ Новый сезон запущен: <code>{sid}</code>. Прогресс у всех начался с нуля, ранги и награды сохранены — отредактируйте их в панели.", parse_mode="HTML")
    pass_admin_home(m.chat.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("ps_"))
def pass_admin_callback(c):
    if not is_admin(c.from_user.id):
        return bot.answer_callback_query(c.id)
    d = c.data
    chat_id = c.message.chat.id
    try:
        bot.answer_callback_query(c.id)
        if d == "ps_home":
            del_msg(c)
            return pass_admin_home(chat_id)
        if d == "ps_name":
            return adm_ask(chat_id, "📝 Введите <b>название</b> сезона:", ps_step_text, "name", 60)
        if d == "ps_desc":
            return adm_ask(chat_id, "📜 Введите <b>описание</b> сезона (до 200 символов):", ps_step_text, "desc", 200)
        if d == "ps_banner":
            return adm_ask(chat_id, "🖼 Пришлите <b>баннер</b> сезона (фото/гиф или ссылка):", ps_step_banner)
        if d == "ps_vipb":
            return adm_ask(chat_id, "👑 Введите <b>VIP-бонус к прогрессу</b> в процентах (например 10):", ps_step_vipb)
        if d == "ps_ranks":
            del_msg(c)
            return pass_admin_ranks(chat_id)
        if d == "ps_radd":
            return adm_ask(chat_id, "🔥 Сколько ICE нужно сжечь для нового ранга?", ps_step_add)
        if d == "ps_new":
            return adm_ask(chat_id, "🆕 Введите <b>название нового сезона</b> (например «❄️ Winter Season Pass»).\nПрогресс всех игроков начнётся с нуля.", ps_step_newseason)
        if d == "ps_reset":
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("⚠️ Да, обнулить всем", callback_data="ps_reset_yes"),
                   types.InlineKeyboardButton("❌ Нет", callback_data="ps_home"))
            return bot.send_message(chat_id, "♻️ Обнулить прогресс и полученные награды пасса <b>у всех игроков</b> текущего сезона? Уже выданные предметы останутся.", parse_mode="HTML", reply_markup=kb)
        if d == "ps_reset_yes":
            sid = pass_cfg()["season_id"]
            res = users.update_many({}, {"$unset": {f"season_pass.{sid}": ""}})
            del_msg(c)
            bot.send_message(chat_id, f"✅ Прогресс сезона обнулён у {res.modified_count} игроков.")
            return pass_admin_home(chat_id)

        if d.startswith("ps_rd_"):
            idx = int(d[len("ps_rd_"):])
            cfg = pass_cfg()
            ranks = cfg.get("ranks", [])
            if idx < len(ranks):
                ranks.pop(idx)
                ps_save(ranks=ranks)
            del_msg(c)
            return pass_admin_ranks(chat_id)
        if d.startswith("ps_rn_"):
            return adm_ask(chat_id, "🔥 Введите новый порог (сколько ICE нужно сжечь):", ps_step_need, int(d[len("ps_rn_"):]))
        if d.startswith("ps_rw_"):
            _, _, idx, slot = d.split("_")
            kb = types.InlineKeyboardMarkup(row_width=2)
            kb.add(types.InlineKeyboardButton("❄️ ICE", callback_data=f"ps_rt_{idx}_{slot}_ice"),
                   types.InlineKeyboardButton(f"{SNOW_EMOJI} Jewel", callback_data=f"ps_rt_{idx}_{slot}_jewel"))
            kb.add(types.InlineKeyboardButton("📦 Кейс", callback_data=f"ps_rt_{idx}_{slot}_case"),
                   types.InlineKeyboardButton("🎁 Предмет из каталога", callback_data=f"ps_rt_{idx}_{slot}_item"))
            kb.add(types.InlineKeyboardButton("🚫 Без награды", callback_data=f"ps_rt_{idx}_{slot}_none"))
            return bot.send_message(chat_id, "Выберите <b>тип награды</b>:", parse_mode="HTML", reply_markup=kb)
        if d.startswith("ps_rt_"):
            _, _, idx, slot, rtype = d.split("_", 4)
            idx = int(idx)
            if rtype == "none":
                ps_set_reward(idx, slot, {"type": "none"})
                return ps_rank_menu(chat_id, idx)
            if rtype in ("ice", "jewel"):
                return adm_ask(chat_id, "Введите <b>количество</b>:", ps_step_amount, idx, slot, rtype, None)
            if rtype == "case":
                kb = types.InlineKeyboardMarkup(row_width=1)
                for key, info in CASE_TYPES.items():
                    kb.add(types.InlineKeyboardButton(info["name"], callback_data=f"ps_rc_{idx}_{slot}_{key}"))
                return bot.send_message(chat_id, "Выберите <b>тип кейса</b>:", parse_mode="HTML", reply_markup=kb)
            if rtype == "item":
                items = list(catalog.find().sort("name", 1).limit(60))
                if not items:
                    return bot.send_message(chat_id, "📭 Каталог пуст. Сначала создайте предмет: /items")
                kb = types.InlineKeyboardMarkup(row_width=1)
                for t in items:
                    kb.add(types.InlineKeyboardButton(item_plain(t), callback_data=f"ps_ri_{idx}_{slot}_{t['_id']}"))
                return bot.send_message(chat_id, "Выберите <b>предмет</b>:", parse_mode="HTML", reply_markup=kb)
        if d.startswith("ps_rc_"):
            _, _, idx, slot, case_key = d.split("_", 4)
            return adm_ask(chat_id, "Введите <b>количество кейсов</b>:", ps_step_amount, int(idx), slot, "case", case_key)
        if d.startswith("ps_ri_"):
            _, _, idx, slot, item_id = d.split("_", 4)
            ps_set_reward(int(idx), slot, {"type": "item", "item": item_id})
            return ps_rank_menu(chat_id, int(idx))
        if d.startswith("ps_r_"):
            del_msg(c)
            return ps_rank_menu(chat_id, int(d[len("ps_r_"):]))
    except Exception as e:
        logger.error(f"Ошибка pass_admin_callback ({d}): {e}")

# ---------------------------------------------------------------
# ЗАПУСК: индексы и значения по умолчанию
# ---------------------------------------------------------------

seed_defaults()


# ================================================================
# НОВОЕ v4: УРОВНИ КЛАНОВ, ТУРНИРЫ, КЛАНОВЫЕ БИТВЫ, МАГАЗИН, ЗВЁЗДЫ, ЯЧЕЙКИ ФАРМА
# ================================================================
from pymongo import ReturnDocument
from urllib.parse import quote as _urlquote

# ---------------------------------------------------------------
# НАСТРОЙКИ КЛАНОВ / ТУРНИРОВ (всё меняется из админ-панели)
# ---------------------------------------------------------------

CLAN_CFG_DEFAULT = {
    "xp_base": 1000,        # XP до 2 ур. = xp_base, до 3 ур. = +2*xp_base, и т.д. (растёт линейно)
    "max_level": 50,
    "base_members": 10,     # мест в клане на 1 уровне
    "slots_per5": 2,        # +мест за каждые 5 уровней
    "gift_ice": 500,        # подарок казне за каждые 5 ур. (умножается на номер "пятёрки")
    "gift_jewel": 2,
    "round_minutes": 120,   # длительность боя
    "break_minutes": 120,   # перерыв между боями
    "win_xp": 300,          # XP клану за победу в раунде
    "part_xp": 100,         # XP клану за участие в раунде
    "prizes": {
        "1": {"xp": 5000, "ice": 5000, "jewel": 25, "title": "🏆 Чемпион турнира", "item": ""},
        "2": {"xp": 3000, "ice": 2500, "jewel": 10, "title": "🥈 Финалист турнира", "item": ""},
        "3": {"xp": 1500, "ice": 1000, "jewel": 5,  "title": "🥉 Призёр турнира", "item": ""},
    },
}

def clan_cfg():
    doc = settings.find_one({"_id": "clan_cfg"}) or {}
    cfg = {k: doc.get(k, v) for k, v in CLAN_CFG_DEFAULT.items() if k != "prizes"}
    pr = doc.get("prizes") or {}
    cfg["prizes"] = {p: {**d, **(pr.get(p) or {})} for p, d in CLAN_CFG_DEFAULT["prizes"].items()}
    return cfg

def xp_to_reach(level, base):
    """Сколько всего XP нужно, чтобы достичь уровня level."""
    return int(base) * level * (level - 1) // 2

def level_from_xp(xp, base, max_level):
    lvl = 1
    while lvl < int(max_level) and xp >= xp_to_reach(lvl + 1, base):
        lvl += 1
    return lvl

def clan_level(clan):
    return int(clan.get("level", 1) or 1)

def clan_max_members(clan, cfg=None):
    cfg = cfg or clan_cfg()
    return int(cfg["base_members"]) + int(cfg["slots_per5"]) * (clan_level(clan) // 5)

def clan_progress(clan, cfg):
    """(текущий XP на уровне, XP до следующего) или (xp, None) на максимуме."""
    lvl = clan_level(clan)
    xp = int(clan.get("xp", 0) or 0)
    if lvl >= int(cfg["max_level"]):
        return xp, None
    c0 = xp_to_reach(lvl, cfg["xp_base"])
    c1 = xp_to_reach(lvl + 1, cfg["xp_base"])
    return max(0, xp - c0), c1 - c0

def is_mgr(clan, uid):
    """Управлять кланом могут только лидер и замы."""
    return bool(clan) and (clan["leader"] == uid or uid in clan.get("deputies", []))

def clan_notify(cid, text, kb=None, only_mgr=False):
    clan = clans.find_one({"_id": cid})
    if not clan:
        return
    ids = set(clan.get("members", []))
    if only_mgr:
        ids = {clan["leader"]} | set(clan.get("deputies", []))
    for uid in ids:
        try:
            bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
        time.sleep(0.05)

def clan_add_xp(cid, amount):
    """Начисляет XP клану, повышает уровень, выдаёт подарки в казну за каждые 5 уровней."""
    try:
        amount = int(amount)
        if amount <= 0:
            return
        cfg = clan_cfg()
        clan = clans.find_one_and_update({"_id": cid}, {"$inc": {"xp": amount}}, return_document=ReturnDocument.AFTER)
        if not clan:
            return
        new_lvl = level_from_xp(int(clan.get("xp", 0)), cfg["xp_base"], cfg["max_level"])
        old = clans.find_one_and_update(
            {"_id": cid, "$or": [{"level": {"$lt": new_lvl}}, {"level": {"$exists": False}}]},
            {"$set": {"level": new_lvl}}, return_document=ReturnDocument.BEFORE
        )
        if not old:
            return
        old_lvl = int(old.get("level", 1) or 1)
        if new_lvl <= old_lvl:
            return
        lines = [f"🎉 <b>Клан «{safe(clan['name'])}» достиг {new_lvl} уровня!</b>"]
        for k in range(1, new_lvl // 5 + 1):
            r = clans.update_one(
                {"_id": cid, "gifted": k - 1},
                {"$set": {"gifted": k}, "$inc": {"treasury.ice": float(cfg["gift_ice"]) * k, "treasury.jewel": float(cfg["gift_jewel"]) * k}}
            )
            if r.modified_count:
                lines.append(f"🎁 Уровень {k * 5}: в казну +{fmt(float(cfg['gift_ice']) * k)} ICE и +{fmt(float(cfg['gift_jewel']) * k)} {SNOW_EMOJI}")
                lines.append(f"👥 Мест в клане +{cfg['slots_per5']}")
        clan_notify(cid, "\n".join(lines))
    except Exception as e:
        logger.error(f"Ошибка clan_add_xp: {e}")

def clan_add_title(cid, title):
    clans.update_one({"_id": cid}, {"$addToSet": {"titles": title}})

def seed_clan_stuff():
    try:
        settings.update_one({"_id": "clan_cfg"}, {"$setOnInsert": {k: v for k, v in CLAN_CFG_DEFAULT.items()}}, upsert=True)
        clans.update_many({"level": {"$exists": False}}, {"$set": {"level": 1}})
        clans.update_many({"xp": {"$exists": False}}, {"$set": {"xp": 0}})
        clans.update_many({"gifted": {"$exists": False}}, {"$set": {"gifted": 0}})
        clans.update_many({"deputies": {"$exists": False}}, {"$set": {"deputies": []}})
        clans.update_many({"titles": {"$exists": False}}, {"$set": {"titles": []}})
        clans.update_many({"treasury": {"$exists": False}}, {"$set": {"treasury": {"ice": 0.0, "jewel": 0.0}}})
    except Exception as e:
        logger.error(f"Ошибка seed_clan_stuff: {e}")

# ---------------------------------------------------------------
# /clan В ЧАТАХ (карточка клана + только просмотр)
# ---------------------------------------------------------------

def clan_group_card(m):
    t_id = getattr(m, "message_thread_id", None)
    uid = m.from_user.id
    parts = (m.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    rm = getattr(m, "reply_to_message", None)
    clan = None
    if arg:
        clan = clans.find_one({"name_lower": arg.lower()})
    elif rm is not None and getattr(rm, "from_user", None) is not None and not rm.from_user.is_bot:
        clan = get_user_clan(rm.from_user.id)
    else:
        clan = get_user_clan(uid)
    uname = bot_username()
    kb = types.InlineKeyboardMarkup(row_width=2)
    if not clan:
        kb.add(types.InlineKeyboardButton("🏰 Открыть кланы в боте", url=f"https://t.me/{uname}?start=clan"))
        return bot.send_message(m.chat.id, "🏰 Клан не найден. Создать клан или вступить можно в личке с ботом 👇",
                                reply_markup=kb, message_thread_id=t_id)
    kb.add(types.InlineKeyboardButton("👥 Участники", callback_data=f"cx_m_{clan['_id']}"))
    b = clan_battles.find_one({"status": "active", "$or": [{"a": clan["_id"]}, {"b": clan["_id"]}]})
    if b:
        kb.add(types.InlineKeyboardButton("⚔️ Клановый бой", callback_data=f"bt_o_{b['_id']}"))
    kb.add(types.InlineKeyboardButton("⚙️ Управление (в боте)", url=f"https://t.me/{uname}?start=clan"))
    send_media_safe(m.chat.id, clan.get("media_type", "photo"), clan.get("file_id"), clan_caption(clan), kb, t_id)

# ---------------------------------------------------------------
# КАЗНА КЛАНА
# ---------------------------------------------------------------

def clan_treasury_screen(chat_id, clan, uid):
    tr = clan.get("treasury") or {}
    leader = clan["leader"] == uid
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("💸 Внести ICE", callback_data="cx_trd_ice"),
           types.InlineKeyboardButton(f"💸 Внести {SNOW_EMOJI}", callback_data="cx_trd_jewel"))
    if leader:
        kb.add(types.InlineKeyboardButton("🏦 Вывести ICE", callback_data="cx_trw_ice"),
               types.InlineKeyboardButton(f"🏦 Вывести {SNOW_EMOJI}", callback_data="cx_trw_jewel"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="cl_home"))
    bot.send_message(
        chat_id,
        f"💰 <b>Казна клана «{safe(clan['name'])}»</b>\n\n"
        f"❄️ ICE: <b>{fmt(tr.get('ice', 0))}</b>\n"
        f"{SNOW_EMOJI} Jewel: <b>{fmt(tr.get('jewel', 0))}</b>\n\n"
        "Казну пополняют подарки за каждые 5 уровней клана, призы турниров и взносы участников.\n"
        + ("Выводить из казны может только лидер." if not leader else "Вы можете вывести средства себе на баланс."),
        parse_mode="HTML", reply_markup=kb
    )

def cx_donate_step(m, cid, res):
    try:
        if adm_cancelled(m):
            return bot.send_message(m.chat.id, "❌ Отменено.")
        uid = m.from_user.id
        a = parse_num(m.text or "")
        if a is None:
            return adm_ask(m.chat.id, "❌ Введите число больше нуля:", cx_donate_step, cid, res)
        a = round(a, 2)
        clan = get_user_clan(uid)
        if not clan or clan["_id"] != cid:
            return bot.send_message(m.chat.id, "❌ Вы не состоите в этом клане.")
        field, bal_field = ("ice", "balance") if res == "ice" else ("jewel", "snow_jewels")
        r = users.update_one({"_id": uid, bal_field: {"$gte": a}}, {"$inc": {bal_field: -a}})
        if r.modified_count == 0:
            return bot.send_message(m.chat.id, "❌ Недостаточно средств.")
        clans.update_one({"_id": cid}, {"$inc": {f"treasury.{field}": a}})
        bot.send_message(m.chat.id, f"✅ Внесено в казну: <b>{fmt(a)}</b> {'ICE' if res == 'ice' else SNOW_EMOJI}", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка cx_donate_step: {e}")

def cx_withdraw_step(m, cid, res):
    try:
        if adm_cancelled(m):
            return bot.send_message(m.chat.id, "❌ Отменено.")
        uid = m.from_user.id
        a = parse_num(m.text or "")
        if a is None:
            return adm_ask(m.chat.id, "❌ Введите число больше нуля:", cx_withdraw_step, cid, res)
        a = round(a, 2)
        field, bal_field = ("ice", "balance") if res == "ice" else ("jewel", "snow_jewels")
        r = clans.update_one({"_id": cid, "leader": uid, f"treasury.{field}": {"$gte": a}}, {"$inc": {f"treasury.{field}": -a}})
        if r.modified_count == 0:
            return bot.send_message(m.chat.id, "❌ В казне недостаточно средств (или вы не лидер).")
        users.update_one({"_id": uid}, {"$inc": {bal_field: a}})
        bot.send_message(m.chat.id, f"✅ Выведено из казны: <b>{fmt(a)}</b> {'ICE' if res == 'ice' else SNOW_EMOJI}", parse_mode="HTML")
    except Exception as e:
        logger.error(f"Ошибка cx_withdraw_step: {e}")

# ---------------------------------------------------------------
# СИЛА (PTS) И СОСТАВЫ
# ---------------------------------------------------------------

def card_pts(card):
    """Сила одной карточки = HP + урон."""
    try:
        _, hp, dmg, _, _ = card_stats(card)
        return int(hp + dmg)
    except Exception:
        return 0

def user_pts(u):
    """Общая сила игрока: силы всех его карточек (до 5) складываются."""
    return sum(card_pts(c) for c in (u.get("cards") or []))

def clan_power(clan):
    tot = 0
    for d in users.find({"_id": {"$in": clan.get("members", [])}}, {"cards": 1}):
        tot += user_pts(d)
    return tot

def build_roster(clan, n):
    docs = list(users.find({"_id": {"$in": clan.get("members", [])}}, {"cards": 1}))
    arr = sorted(((d["_id"], user_pts(d)) for d in docs), key=lambda x: -x[1])[:n]
    return [{"uid": uid, "pts": p, "att": 1, "won": False} for uid, p in arr]

# ---------------------------------------------------------------
# КЛАНОВЫЕ БИТВЫ
# ---------------------------------------------------------------

def battle_create(tid, rnd, ca, cb, end_ts):
    n = min(len(ca.get("members", [])), len(cb.get("members", [])))   # составы равного размера
    ra, rb = build_roster(ca, n), build_roster(cb, n)
    bid = uuid.uuid4().hex[:10]
    clan_battles.insert_one({
        "_id": bid, "tid": tid, "round": rnd, "a": ca["_id"], "b": cb["_id"], "ra": ra, "rb": rb,
        "pa": sum(x["pts"] for x in ra), "pb": sum(x["pts"] for x in rb),
        "sa": 0, "sb": 0, "tba": 0, "tbb": 0, "status": "active", "end_ts": end_ts, "log": [],
    })
    return bid

def battle_winner(b):
    if b["sa"] != b["sb"]:
        return b["a"] if b["sa"] > b["sb"] else b["b"]
    if b["tba"] != b["tbb"]:
        return b["a"] if b["tba"] > b["tbb"] else b["b"]
    if b["pa"] != b["pb"]:
        return b["a"] if b["pa"] > b["pb"] else b["b"]
    ca, cb = clans.find_one({"_id": b["a"]}) or {}, clans.find_one({"_id": b["b"]}) or {}
    if clan_level(ca) != clan_level(cb):
        return b["a"] if clan_level(ca) > clan_level(cb) else b["b"]
    return random.choice([b["a"], b["b"]])

def battle_attack(bid, attacker, target):
    """У каждого одна атака на любого врага. Защитник свою атаку не теряет — бот отыгрывает его защиту."""
    now = int(time.time())
    b = clan_battles.find_one({"_id": bid})
    if not b or b.get("status") != "active" or now >= b["end_ts"]:
        return False, "⌛ Бой уже завершён.", False
    if any(x["uid"] == attacker for x in b["ra"]):
        my, en, side = "ra", "rb", "a"
    elif any(x["uid"] == attacker for x in b["rb"]):
        my, en, side = "rb", "ra", "b"
    else:
        return False, "❌ Вы не в составе этого боя.", False
    tgt = next((x for x in b[en] if x["uid"] == target), None)
    me = next(x for x in b[my] if x["uid"] == attacker)
    if not tgt:
        return False, "❌ Цель не найдена.", False
    r = clan_battles.update_one({"_id": bid, my: {"$elemMatch": {"uid": attacker, "att": 1}}}, {"$set": {f"{my}.$.att": 0}})
    if r.modified_count == 0:
        return False, "⚔️ Ваша атака уже использована.", False
    a_roll = me["pts"] * random.uniform(0.85, 1.15)
    d_roll = tgt["pts"] * random.uniform(0.85, 1.15)
    win = a_roll > d_roll
    names = user_names([attacker, target])
    line = f"{'🏆' if win else '🛡'} {names.get(attacker, attacker)} → {names.get(target, target)}"
    upd = {"$push": {"log": {"$each": [line], "$slice": -8}}}
    if win:
        upd["$inc"] = {f"s{side}": 1, f"tb{side}": tgt["pts"]}
        upd["$set"] = {f"{my}.$.won": True}
    clan_battles.update_one({"_id": bid, my: {"$elemMatch": {"uid": attacker}}}, upd)
    txt = (f"🏆 <b>Победа!</b> Ваши {me['pts']} PTS сильнее {tgt['pts']} PTS соперника (+1 к счёту клана)."
           if win else f"🛡 <b>Поражение.</b> Ваши {me['pts']} PTS не пробили {tgt['pts']} PTS соперника.")
    return True, txt, win

def battle_board_parts(b):
    ca, cb = clans.find_one({"_id": b["a"]}) or {}, clans.find_one({"_id": b["b"]}) or {}
    names = user_names([x["uid"] for x in b["ra"] + b["rb"]])
    left = sorted(b["ra"], key=lambda x: -x["pts"])
    right = sorted(b["rb"], key=lambda x: -x["pts"])

    def mk(x):
        return "⚔️:1" if x["att"] else ("⚔️:0🏆" if x.get("won") else "⚔️:0")

    left_s = max(0, b["end_ts"] - int(time.time()))
    timer = (
        f"⏳ До конца: <b>{left_s // 3600}ч {(left_s % 3600) // 60}м</b>"
        if b.get("status") == "active" and left_s > 0
        else "⌛ Бой завершён"
    )

    rows = []
    for l, r in zip(left[:3], right[:3]):
        rows.append(
            f"[{l['uid']}] {safe(names.get(l['uid'], l['uid']))} : "
            f"{safe(names.get(r['uid'], r['uid']))} [{r['uid']}]"
        )

    head = (
        f"Game Masters ⚔️ DarkFall\n"
        f"⭐ {fmt(b['pa'])} PTS | ⭐ {fmt(b['pb'])} PTS\n"
        f"            📊 Счёт: {b['sa']} : {b['sb']} ( В центре )\n\n"
        f"<blockquote>\n"
        + "\n".join(rows)
        + f"\n</blockquote>\n\n"
        f"📜 🏆 {safe(names.get(left[0]['uid'], left[0]['uid']))} | "
        f"{safe(names.get(right[0]['uid'], right[0]['uid']))} 🤖\n"
        f"📜 👎 {safe(names.get(left[1]['uid'], left[1]['uid']))} | "
        f"{safe(names.get(right[1]['uid'], right[1]['uid']))} 🤖\n\n"
        f"➖➖➖➖➖➖➖➖\n"
        f"{timer}"
    )

    body = "\n".join(rows)
    if b.get("log"):
        ...
    return head, body

def battle_send_board(chat_id, bid, uid, thread_id=None):
    b = clan_battles.find_one({"_id": bid})
    if not b:
        return bot.send_message(chat_id, "❌ Бой не найден.", message_thread_id=thread_id)
    head, body, media = battle_board_parts(b)
    kb = types.InlineKeyboardMarkup(row_width=2)
    mine = next((x for x in b["ra"] + b["rb"] if x["uid"] == uid), None)
    if b.get("status") == "active":
        if mine and mine["att"] == 1:
            kb.add(types.InlineKeyboardButton("⚔️ Атаковать", callback_data=f"bt_a_{bid}"))
        kb.add(types.InlineKeyboardButton("🔄 Обновить", callback_data=f"bt_r_{bid}"))
    full = head + ("\n\n" + body if body else "")
    mt, fid = media.get("media_type", "photo"), media.get("file_id")
    if len(full) <= 1000:
        return send_media_safe(chat_id, mt, fid, full, kb if kb.keyboard else None, thread_id)
    send_media_safe(chat_id, mt, fid, head, None, thread_id)
    return bot.send_message(chat_id, body, parse_mode="HTML", reply_markup=kb if kb.keyboard else None, message_thread_id=thread_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("bt_"))
def clan_battle_callback(c):
    uid = c.from_user.id
    chat_id = c.message.chat.id
    t_id = getattr(c.message, "message_thread_id", None)
    d = c.data
    try:
        if d.startswith("bt_o_"):
            bot.answer_callback_query(c.id)
            return battle_send_board(chat_id, d[len("bt_o_"):], uid, t_id)
        if d.startswith("bt_r_"):
            bot.answer_callback_query(c.id)
            del_msg(c)
            return battle_send_board(chat_id, d[len("bt_r_"):], uid, t_id)
        if d.startswith("bt_a_"):
            bid = d[len("bt_a_"):]
            b = clan_battles.find_one({"_id": bid})
            if not b or b.get("status") != "active" or int(time.time()) >= b["end_ts"]:
                return bot.answer_callback_query(c.id, "⌛ Бой уже завершён", show_alert=True)
            if any(x["uid"] == uid and x["att"] == 1 for x in b["ra"]):
                enemy = b["rb"]
            elif any(x["uid"] == uid and x["att"] == 1 for x in b["rb"]):
                enemy = b["ra"]
            else:
                return bot.answer_callback_query(c.id, "У вас нет доступной атаки ⚔️:0", show_alert=True)
            names = user_names([x["uid"] for x in enemy])
            kb = types.InlineKeyboardMarkup(row_width=2)
            for x in sorted(enemy, key=lambda y: -y["pts"]):
                kb.add(types.InlineKeyboardButton(f"{names.get(x['uid'], x['uid'])} [{x['pts']}]"[:40], callback_data=f"bt_t_{bid}_{x['uid']}"))
            bot.answer_callback_query(c.id)
            return bot.send_message(chat_id, "🎯 <b>Выберите цель для атаки</b> (у вас одна атака):", parse_mode="HTML", reply_markup=kb, message_thread_id=t_id)
        if d.startswith("bt_t_"):
            _, _, bid, target = d.split("_")
            ok, txt = war_start_fight(bid, uid, int(target), chat_id, t_id)
            if not ok:
                return bot.answer_callback_query(c.id, re.sub(r"<[^>]+>", "", txt), show_alert=True)
            clan_battles.update_one({"_id": bid}, {"$set": {f"t2.{uid}": int(time.time())}})
            bot.answer_callback_query(c.id, "⚔️ Бой начался!")
            return del_msg(c)
    except Exception as e:
        logger.error(f"Ошибка battle_callback ({d}): {e}")
        try:
            bot.answer_callback_query(c.id, "❌ Ошибка")
        except Exception:
            pass

@bot.message_handler(commands=["clanwar"])
def clanwar_cmd(m):
    t_id = getattr(m, "message_thread_id", None)
    clan = get_user_clan(m.from_user.id)
    if not clan:
        return bot.send_message(m.chat.id, "❌ Вы не состоите в клане.", message_thread_id=t_id)
    b = clan_battles.find_one({"status": "active", "$or": [{"a": clan["_id"]}, {"b": clan["_id"]}]})
    if not b:
        return bot.send_message(m.chat.id, "⚔️ Сейчас у вашего клана нет активного боя. Турниры запускает администрация.", message_thread_id=t_id)
    battle_send_board(m.chat.id, b["_id"], m.from_user.id, t_id)

# ---------------------------------------------------------------
# ТУРНИРЫ: подбор равных, раунды, призы
# ---------------------------------------------------------------

def t_current():
    return tournaments.find_one({"status": {"$in": ["registration", "running"]}}, sort=[("created", -1)])

def prizes_text(prizes):
    medals = {"1": "🥇", "2": "🥈", "3": "🥉"}
    lines = []
    for p in ("1", "2", "3"):
        pr = prizes.get(p) or {}
        parts = []
        if pr.get("xp"):
            parts.append(f"{fmt(pr['xp'])} XP клану")
        if pr.get("ice"):
            parts.append(f"{fmt(pr['ice'])} ICE в казну")
        if pr.get("jewel"):
            parts.append(f"{fmt(pr['jewel'])} {SNOW_EMOJI} в казну")
        if pr.get("title"):
            parts.append(f"титул «{safe(pr['title'])}»")
        if pr.get("item"):
            tpl = catalog.find_one({"_id": pr["item"]})
            if tpl:
                parts.append("каждому участнику: " + item_label(tpl))
        lines.append(f"{medals[p]} {p} место: " + (", ".join(parts) or "—"))
    return "\n".join(lines)

def pair_clans(docs):
    """Подбор равных: сортируем кланы по общей силе и ставим в пары соседей.
    При нечётном числе сильнейший клан получает проход (bye)."""
    power = {cid: clan_power(cl) for cid, cl in docs.items()}
    order = sorted(docs.keys(), key=lambda c: -power[c])
    bye = order.pop(0) if len(order) % 2 == 1 else None
    pairs = [(order[i], order[i + 1]) for i in range(0, len(order), 2)]
    return pairs, bye

def t_start_round(tid):
    t = tournaments.find_one({"_id": tid})
    if not t:
        return
    cfg = t.get("cfg") or clan_cfg()
    rnd = int(t.get("round", 0)) + 1
    alive = list(t.get("alive") or t.get("clans") or [])
    docs = {}
    for cid in alive:
        cl = clans.find_one({"_id": cid})
        if cl:
            docs[cid] = cl
    if len(docs) <= 1:
        return t_finalize(tid, next(iter(docs), None))
    clan_battles.delete_many({"tid": tid, "round": rnd})   # защита от дублей при повторном запуске
    pairs, bye = pair_clans(docs)
    end_ts = int(time.time()) + int(cfg["round_minutes"]) * 60
    for a, b in pairs:
        bid = battle_create(tid, rnd, docs[a], docs[b], end_ts)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⚔️ Открыть бой", callback_data=f"bt_o_{bid}"))
        for me, foe in ((a, b), (b, a)):
            clan_notify(me, f"⚔️ <b>Турнир «{safe(t['name'])}» — раунд {rnd}</b>\n\n"
                            f"Ваш соперник: <b>{safe(docs[foe]['name'])}</b> (ур. {clan_level(docs[foe])})\n"
                            f"У вас {cfg['round_minutes']} мин: у каждого одна атака ⚔️:1 на любого игрока врага. "
                            f"Даже если соперник спит — вы можете атаковать его, бот отыграет защиту.", kb)
    if bye:
        clan_battles.insert_one({"_id": uuid.uuid4().hex[:10], "tid": tid, "round": rnd, "a": bye, "b": None, "status": "bye"})
        clan_notify(bye, f"✅ <b>Турнир «{safe(t['name'])}» — раунд {rnd}</b>\n\nНечётное число кланов: вы проходите дальше без боя!")
    tournaments.update_one({"_id": tid}, {"$set": {"round": rnd, "phase": "battle", "phase_end": end_ts, "alive": list(docs.keys())}})

def t_finish_round(tid):
    t = tournaments.find_one({"_id": tid})
    cfg = t.get("cfg") or clan_cfg()
    rnd = int(t["round"])
    winners = []
    for b in list(clan_battles.find({"tid": tid, "round": rnd})):
        if b.get("status") == "bye":
            winners.append(b["a"])
            continue
        if b.get("status") == "done":
            winners.append(b["winner"])
            continue
        if b.get("status") != "active":
            continue
        w = battle_winner(b)
        loser = b["b"] if w == b["a"] else b["a"]
        r = clan_battles.update_one({"_id": b["_id"], "status": "active"}, {"$set": {"status": "done", "winner": w}})
        if not r.modified_count:
            continue
        winners.append(w)
        tournaments.update_one({"_id": tid}, {"$set": {f"elim.{loser}": rnd}})
        clan_add_xp(w, cfg["win_xp"])
        clan_add_xp(w, cfg["part_xp"])
        clan_add_xp(loser, cfg["part_xp"])
        ca, cb = clans.find_one({"_id": b["a"]}) or {}, clans.find_one({"_id": b["b"]}) or {}
        score = f"{safe(ca.get('name', '?'))} <b>{b['sa']} : {b['sb']}</b> {safe(cb.get('name', '?'))}"
        for cid in (b["a"], b["b"]):
            won = cid == w
            clan_notify(cid, f"🏁 <b>Итоги боя (раунд {rnd})</b>\n{score}\n\n"
                             + (f"✅ Ваш клан победил! +{cfg['win_xp'] + cfg['part_xp']} XP" if won
                                else f"❌ Ваш клан проиграл и выбывает. +{cfg['part_xp']} XP за участие"))
    if len(winners) <= 1:
        return t_finalize(tid, winners[0] if winners else None)
    phase_end = int(time.time()) + int(cfg["break_minutes"]) * 60
    tournaments.update_one({"_id": tid}, {"$set": {"phase": "break", "phase_end": phase_end, "alive": winners}})
    for cid in winners:
        clan_notify(cid, f"⏸ <b>Перерыв {cfg['break_minutes']} мин.</b> Следующий бой турнира «{safe(t['name'])}» начнётся автоматически — "
                         f"соперник будет подобран по силе.")

def t_finalize(tid, winner):
    t = tournaments.find_one({"_id": tid})
    cfg = t.get("cfg") or clan_cfg()
    if not winner:
        tournaments.update_one({"_id": tid}, {"$set": {"status": "cancelled", "phase": None}})
        return
    R = int(t.get("round", 1))
    elim = t.get("elim") or {}
    places = {winner: 1}
    for cid, r in elim.items():
        if int(r) == R:
            places[cid] = 2
        elif int(r) == R - 1 and R - 1 >= 1:
            places[cid] = 3
    tournaments.update_one({"_id": tid}, {"$set": {"status": "finished", "phase": None, "winner": winner, "places": places}})
    for cid, place in places.items():
        pr = (cfg.get("prizes") or {}).get(str(place)) or {}
        cl = clans.find_one({"_id": cid})
        if not cl:
            continue
        inc = {}
        if pr.get("ice"):
            inc["treasury.ice"] = float(pr["ice"])
        if pr.get("jewel"):
            inc["treasury.jewel"] = float(pr["jewel"])
        if inc:
            clans.update_one({"_id": cid}, {"$inc": inc})
        if pr.get("title"):
            clan_add_title(cid, pr["title"])
        if pr.get("item"):
            tpl = catalog.find_one({"_id": pr["item"]})
            if tpl:
                for uid in cl.get("members", []):
                    give_item(uid, tpl)
        clan_add_xp(cid, int(pr.get("xp", 0) or 0))
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [f"🏁 <b>Турнир «{safe(t['name'])}» завершён!</b>", ""]
    for cid, place in sorted(places.items(), key=lambda x: x[1]):
        cl = clans.find_one({"_id": cid}) or {}
        lines.append(f"{medals[place]} {safe(cl.get('name', '?'))}")
    lines += ["", "Призы выданы в казну кланов, титулы и XP начислены."]
    for cid in (t.get("clans") or []):
        clan_notify(cid, "\n".join(lines))

def tournament_tick():
    now = int(time.time())
    t = tournaments.find_one_and_update({"status": "running", "phase": "battle", "phase_end": {"$lte": now}}, {"$set": {"phase": "processing"}})
    if t:
        try:
            t_finish_round(t["_id"])
        except Exception as e:
            logger.error(f"Ошибка t_finish_round: {e}")
            tournaments.update_one({"_id": t["_id"], "phase": "processing"}, {"$set": {"phase": "battle"}})
    t = tournaments.find_one_and_update({"status": "running", "phase": "break", "phase_end": {"$lte": now}}, {"$set": {"phase": "processing"}})
    if t:
        try:
            t_start_round(t["_id"])
        except Exception as e:
            logger.error(f"Ошибка t_start_round: {e}")
            tournaments.update_one({"_id": t["_id"], "phase": "processing"}, {"$set": {"phase": "break"}})

def _tournament_loop():
    while True:
        try:
            tournament_tick()
        except Exception as e:
            logger.error(f"Ошибка tournament_loop: {e}")
        extra_ticks()
        time.sleep(30)

# ---------------------------------------------------------------
# КЛАН: экран турнира, участники (чтение), казна — кнопки cx_
# ---------------------------------------------------------------

def clan_tournament_screen(chat_id, uid, clan, thread_id=None):
    t = t_current()
    kb = types.InlineKeyboardMarkup(row_width=1)
    if not t:
        text = "🏆 <b>Турнир кланов</b>\n\nСейчас активных турниров нет. Турниры запускает администрация — следите за новостями!"
    else:
        cfg = t.get("cfg") or clan_cfg()
        mgr = is_mgr(clan, uid)
        registered = clan["_id"] in t.get("clans", [])
        lines = [f"🏆 <b>{safe(t['name'])}</b>",
                 f"Плей-офф на выбывание. Соперники подбираются по силе. Бой — {cfg['round_minutes']} мин, перерыв — {cfg['break_minutes']} мин.",
                 "", "<b>Призы:</b>", prizes_text(cfg["prizes"]), ""]
        if t["status"] == "registration":
            lines.append(f"👥 Кланов записано: <b>{len(t.get('clans', []))}</b>")
            lines.append("✅ Ваш клан записан" if registered else "Ваш клан пока не записан")
            if mgr:
                kb.add(types.InlineKeyboardButton("❌ Снять клан с турнира" if registered else "✅ Записать клан", callback_data="cx_tl" if registered else "cx_tj"))
            else:
                lines.append("<i>Записывать клан могут лидер и замы.</i>")
        else:
            left = max(0, int(t.get("phase_end", 0)) - int(time.time()))
            lines.append(f"🔥 Раунд <b>{t.get('round', 0)}</b> · {'идёт бой' if t.get('phase') == 'battle' else 'перерыв'} · через {left // 3600}ч {(left % 3600) // 60}м")
            if not registered:
                lines.append("Ваш клан не участвует в этом турнире.")
            elif clan["_id"] not in t.get("alive", []):
                lines.append("❌ Ваш клан выбыл из турнира.")
            else:
                lines.append("✅ Ваш клан в игре!")
                b = clan_battles.find_one({"tid": t["_id"], "status": "active", "$or": [{"a": clan["_id"]}, {"b": clan["_id"]}]})
                if b:
                    kb.add(types.InlineKeyboardButton("⚔️ Открыть бой", callback_data=f"bt_o_{b['_id']}"))
        text = "\n".join(lines)
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="cl_home"))
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb, message_thread_id=thread_id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("cx_"))
def clan_x_callback(c):
    uid = c.from_user.id
    chat_id = c.message.chat.id
    t_id = getattr(c.message, "message_thread_id", None)
    d = c.data
    try:
        if d.startswith("cx_m_"):
            cl = clans.find_one({"_id": d[len("cx_m_"):]})
            if not cl:
                return bot.answer_callback_query(c.id, "Клан не найден", show_alert=True)
            ids = [cl["leader"]] + [x for x in cl.get("members", []) if x != cl["leader"]]
            names = user_names(ids)
            deps = set(cl.get("deputies", []))
            lines = [f"👥 <b>Участники «{safe(cl['name'])}»</b> ({len(ids)}/{clan_max_members(cl)})\n"]
            for i in ids:
                mark = "👑" if i == cl["leader"] else ("🛡" if i in deps else "▫️")
                lines.append(f"{mark} {safe(names.get(i, i))}")
            bot.answer_callback_query(c.id)
            return bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML", message_thread_id=t_id)

        clan = get_user_clan(uid)
        if not clan:
            return bot.answer_callback_query(c.id, "Вы не состоите в клане", show_alert=True)

        if d == "cx_t":
            bot.answer_callback_query(c.id)
            return clan_tournament_screen(chat_id, uid, clan, t_id)
        if d == "cx_w":
            bot.answer_callback_query(c.id)
            return war_open_screen(chat_id, uid, clan, t_id)
        if d == "cx_b":
            bot.answer_callback_query(c.id)
            return clan_boss_screen(chat_id, uid, clan, t_id)
        if d in ("cx_tj", "cx_tl"):
            if not is_mgr(clan, uid):
                return bot.answer_callback_query(c.id, "Только лидер и замы", show_alert=True)
            t = t_current()
            if not t or t["status"] != "registration":
                return bot.answer_callback_query(c.id, "Регистрация закрыта", show_alert=True)
            if d == "cx_tj":
                tournaments.update_one({"_id": t["_id"]}, {"$addToSet": {"clans": clan["_id"]}})
                bot.answer_callback_query(c.id, "✅ Клан записан на турнир")
            else:
                tournaments.update_one({"_id": t["_id"]}, {"$pull": {"clans": clan["_id"]}})
                bot.answer_callback_query(c.id, "Клан снят с турнира")
            del_msg(c)
            return clan_tournament_screen(chat_id, uid, clan, t_id)
        if d == "cx_tr":
            bot.answer_callback_query(c.id)
            return clan_treasury_screen(chat_id, clan, uid)
        if d.startswith("cx_trd_") or d.startswith("cx_trw_"):
            if c.message.chat.type != "private":
                return bot.answer_callback_query(c.id, "Откройте бота в личке — там ввод суммы", show_alert=True)
            res = d.split("_")[2]
            bot.answer_callback_query(c.id)
            if d.startswith("cx_trd_"):
                return adm_ask(uid, f"💸 Сколько {'ICE' if res == 'ice' else 'Jewel'} внести в казну?", cx_donate_step, clan["_id"], res)
            if clan["leader"] != uid:
                return bot.send_message(chat_id, "❌ Выводить из казны может только лидер.")
            return adm_ask(uid, f"🏦 Сколько {'ICE' if res == 'ice' else 'Jewel'} вывести из казны?", cx_withdraw_step, clan["_id"], res)
        bot.answer_callback_query(c.id)
    except Exception as e:
        logger.error(f"Ошибка clan_x_callback ({d}): {e}")
        try:
            bot.answer_callback_query(c.id, "❌ Ошибка")
        except Exception:
            pass

# ---------------------------------------------------------------
# АДМИН: КЛАНЫ И ТУРНИРЫ (/clanadmin)
# ---------------------------------------------------------------

CT_GENERAL = [
    ("xp_base", "📈 XP на 1 уровень (×ур.)", int),
    ("max_level", "🔝 Макс. уровень клана", int),
    ("base_members", "👥 Мест в клане на 1 ур.", int),
    ("slots_per5", "➕ Мест за каждые 5 ур.", int),
    ("gift_ice", "🎁 Подарок казне ICE (×N)", float),
    ("gift_jewel", "🎁 Подарок казне Jewel (×N)", float),
    ("round_minutes", "⏱ Длительность боя, мин", int),
    ("break_minutes", "⏸ Перерыв между боями, мин", int),
    ("win_xp", "🏅 XP за победу в раунде", int),
    ("part_xp", "🎖 XP за участие в раунде", int),
]
CT_PRIZE_FIELDS = [
    ("xp", "XP клану", int),
    ("ice", "ICE в казну", float),
    ("jewel", "Jewel в казну", float),
    ("title", "Титул клана", str),
    ("item", "Предмет каждому участнику (ID)", str),
]
CT_EDIT = {k: (label, typ) for k, label, typ in CT_GENERAL}
for _p in ("1", "2", "3"):
    for _k, _label, _typ in CT_PRIZE_FIELDS:
        CT_EDIT[f"prizes.{_p}.{_k}"] = (f"{_p} место — {_label}", _typ)

def ct_home(chat_id):
    cfg = clan_cfg()
    t = t_current()
    lines = ["🏰 <b>Кланы и турниры</b>", "",
             f"👥 Кланов: <b>{clans.count_documents({})}</b>",
             f"📈 XP на уровень: {cfg['xp_base']}×ур. · мест {cfg['base_members']} (+{cfg['slots_per5']} за 5 ур.)",
             f"⏱ Бой {cfg['round_minutes']} мин · перерыв {cfg['break_minutes']} мин", ""]
    if t:
        lines.append(f"🏆 Активный турнир: <b>{safe(t['name'])}</b> ({'регистрация' if t['status'] == 'registration' else 'идёт, раунд ' + str(t.get('round', 0))})")
    else:
        lines.append("🏆 Активных турниров нет")
    kb = types.InlineKeyboardMarkup(row_width=2)
    if t:
        kb.add(types.InlineKeyboardButton("🏆 Управлять активным турниром", callback_data=f"ct_v_{t['_id']}"))
    else:
        kb.add(types.InlineKeyboardButton("➕ Создать турнир", callback_data="ct_new"))
    kb.add(types.InlineKeyboardButton("📋 История турниров", callback_data="ct_list"))
    kb.add(types.InlineKeyboardButton("⚙️ Уровни, места, казна", callback_data="ct_cfg"),
           types.InlineKeyboardButton("🎁 Призы за места", callback_data="ct_pz"))
    kb.add(types.InlineKeyboardButton("✨ Выдать XP клану", callback_data="ct_gx"),
           types.InlineKeyboardButton("🏅 Выдать титул клану", callback_data="ct_gt"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="am_home"))
    bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML", reply_markup=kb)

@bot.message_handler(commands=["clanadmin"])
def clanadmin_cmd(m):
    if is_admin(m.from_user.id):
        ct_home(m.chat.id)

def ct_get(cfg, key):
    cur = cfg
    for part in key.split("."):
        cur = (cur or {}).get(part, "")
    return cur

def ct_fields_kb(cfg, fields, prefix="", back="ct_home"):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for k, label, typ in fields:
        key = prefix + k
        val = ct_get(cfg, key)
        shown = (val if val not in ("", None) else "—")
        kb.add(types.InlineKeyboardButton(f"{label}: {shown}"[:60], callback_data=f"ct_e_{key}"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data=back))
    return kb

def ct_cfg_screen(chat_id):
    bot.send_message(chat_id, "⚙️ <b>Уровни, места, казна, тайминги</b>\nНажмите на параметр, чтобы изменить.",
                     parse_mode="HTML", reply_markup=ct_fields_kb(clan_cfg(), CT_GENERAL))

def ct_prizes_screen(chat_id):
    cfg = clan_cfg()
    kb = types.InlineKeyboardMarkup(row_width=1)
    for p, medal in (("1", "🥇"), ("2", "🥈"), ("3", "🥉")):
        kb.add(types.InlineKeyboardButton(f"{medal} Приз за {p} место", callback_data=f"ct_pp_{p}"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="ct_home"))
    bot.send_message(chat_id, "🎁 <b>Призы турнира</b>\n\n" + prizes_text(cfg["prizes"]) +
                     "\n\n<i>Изменения действуют на новые турниры (в уже созданном призы фиксируются).</i>",
                     parse_mode="HTML", reply_markup=kb)

def ct_prize_screen(chat_id, place):
    cfg = clan_cfg()
    bot.send_message(chat_id, f"🎁 <b>Приз за {place} место</b>\nНажмите на параметр, чтобы изменить. «-» очищает текстовое поле.",
                     parse_mode="HTML", reply_markup=ct_fields_kb(cfg, CT_PRIZE_FIELDS, prefix=f"prizes.{place}.", back="ct_pz"))

def parse_nonneg(text):
    try:
        v = float(str(text).replace(",", ".").strip())
        return v if v >= 0 else None
    except Exception:
        return None

def ct_step_edit(m, key):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    label, typ = CT_EDIT[key]
    txt = (m.text or "").strip()
    if typ is str:
        val = "" if txt == "-" else txt[:60]
        if key.endswith(".item") and val and not catalog.find_one({"_id": val}):
            return adm_ask(m.chat.id, "❌ Предмет с таким ID не найден в каталоге (/items). Введите ID ещё раз или «-»:", ct_step_edit, key)
    else:
        v = parse_nonneg(txt)
        if v is None:
            return adm_ask(m.chat.id, "❌ Введите число (0 или больше):", ct_step_edit, key)
        val = int(v) if typ is int else float(v)
    settings.update_one({"_id": "clan_cfg"}, {"$set": {key: val}}, upsert=True)
    bot.send_message(m.chat.id, f"✅ Сохранено: {label} = {val if val != '' else '—'}")
    if key.startswith("prizes."):
        ct_prize_screen(m.chat.id, key.split(".")[1])
    else:
        ct_cfg_screen(m.chat.id)

def ct_find_clan(q):
    q = (q or "").strip()
    return clans.find_one({"name_lower": q.lower()}) or clans.find_one({"_id": q})

def ct_step_gx_clan(m):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    cl = ct_find_clan(m.text)
    if not cl:
        return adm_ask(m.chat.id, "❌ Клан не найден. Введите название или ID клана:", ct_step_gx_clan)
    adm_ask(m.chat.id, f"Клан «{safe(cl['name'])}». Сколько XP выдать?", ct_step_gx_amount, cl["_id"])

def ct_step_gx_amount(m, cid):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    v = parse_num(m.text or "")
    if v is None:
        return adm_ask(m.chat.id, "❌ Введите число больше нуля:", ct_step_gx_amount, cid)
    clan_add_xp(cid, int(v))
    bot.send_message(m.chat.id, f"✅ Клану выдано {int(v)} XP.")

def ct_step_gt_clan(m):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    cl = ct_find_clan(m.text)
    if not cl:
        return adm_ask(m.chat.id, "❌ Клан не найден. Введите название или ID клана:", ct_step_gt_clan)
    adm_ask(m.chat.id, f"Клан «{safe(cl['name'])}». Введите текст титула (например «🏆 Чемпион зимы»):", ct_step_gt_text, cl["_id"])

def ct_step_gt_text(m, cid):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    if not m.text or len(m.text.strip()) > 40:
        return adm_ask(m.chat.id, "❌ Титул до 40 символов:", ct_step_gt_text, cid)
    clan_add_title(cid, m.text.strip())
    bot.send_message(m.chat.id, "✅ Титул выдан.")

def ct_step_new(m):
    if adm_cancelled(m) or not is_admin(m.from_user.id):
        return
    if not m.text or len(m.text.strip()) > 40:
        return adm_ask(m.chat.id, "❌ Название до 40 символов. Введите ещё раз:", ct_step_new)
    if t_current():
        return bot.send_message(m.chat.id, "❌ Уже есть активный турнир.")
    name = m.text.strip()
    tid = uuid.uuid4().hex[:8]
    tournaments.insert_one({"_id": tid, "name": name, "status": "registration", "created": int(time.time()),
                            "clans": [], "round": 0, "phase": None, "phase_end": 0, "alive": [], "elim": {}, "cfg": clan_cfg()})
    bot.send_message(m.chat.id, f"✅ Турнир «{safe(name)}» создан. Идёт регистрация кланов. Когда запишется достаточно кланов — нажмите «Начать».", parse_mode="HTML")
    for cl in clans.find({}, {"_id": 1}):
        clan_notify(cl["_id"], f"🏆 <b>Открыта регистрация на турнир «{safe(name)}»!</b>\nЛидер или зам: 🏰 Клан → 🏆 Турнир → «Записать клан».", only_mgr=True)
    ct_view(m.chat.id, tid)

def ct_view(chat_id, tid):
    t = tournaments.find_one({"_id": tid})
    if not t:
        return bot.send_message(chat_id, "❌ Турнир не найден.")
    names = []
    for cid in t.get("clans", []):
        cl = clans.find_one({"_id": cid})
        if cl:
            alive = "" if t["status"] != "running" or cid in t.get("alive", []) else " ❌"
            names.append(f"• {safe(cl['name'])} (ур. {clan_level(cl)}){alive}")
    status = {"registration": "регистрация", "running": "идёт", "finished": "завершён", "cancelled": "отменён"}.get(t["status"], t["status"])
    lines = [f"🏆 <b>{safe(t['name'])}</b>", f"Статус: <b>{status}</b>", f"Раунд: {t.get('round', 0)}"]
    if t["status"] == "running":
        left = max(0, int(t.get("phase_end", 0)) - int(time.time()))
        lines.append(f"Фаза: {t.get('phase')} · осталось {left // 3600}ч {(left % 3600) // 60}м")
    lines += ["", f"Кланы ({len(names)}):"] + (names or ["—"])
    kb = types.InlineKeyboardMarkup(row_width=1)
    if t["status"] == "registration":
        kb.add(types.InlineKeyboardButton("▶️ Начать турнир", callback_data=f"ct_go_{tid}"))
    if t["status"] == "running":
        kb.add(types.InlineKeyboardButton("⏩ Завершить текущую фазу сейчас", callback_data=f"ct_skip_{tid}"))
    if t["status"] in ("registration", "running"):
        kb.add(types.InlineKeyboardButton("🛑 Отменить турнир", callback_data=f"ct_stop_{tid}"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="ct_home"))
    bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data.startswith("ct_"))
def ct_callback(c):
    if not is_admin(c.from_user.id):
        return bot.answer_callback_query(c.id)
    d = c.data
    chat_id = c.message.chat.id
    try:
        bot.answer_callback_query(c.id)
        if d == "ct_home":
            del_msg(c)
            return ct_home(chat_id)
        if d == "ct_cfg":
            del_msg(c)
            return ct_cfg_screen(chat_id)
        if d == "ct_pz":
            del_msg(c)
            return ct_prizes_screen(chat_id)
        if d.startswith("ct_pp_"):
            del_msg(c)
            return ct_prize_screen(chat_id, d[len("ct_pp_"):])
        if d.startswith("ct_e_"):
            key = d[len("ct_e_"):]
            if key not in CT_EDIT:
                return
            label, typ = CT_EDIT[key]
            hint = "Введите новое значение" + (" (текст, «-» чтобы очистить)" if typ is str else " (число)")
            return adm_ask(chat_id, f"✏️ <b>{label}</b>\n{hint}:", ct_step_edit, key)
        if d == "ct_new":
            return adm_ask(chat_id, "🏆 Введите <b>название турнира</b>:", ct_step_new)
        if d == "ct_gx":
            return adm_ask(chat_id, "✨ Введите <b>название или ID клана</b>:", ct_step_gx_clan)
        if d == "ct_gt":
            return adm_ask(chat_id, "🏅 Введите <b>название или ID клана</b>:", ct_step_gt_clan)
        if d == "ct_list":
            kb = types.InlineKeyboardMarkup(row_width=1)
            for t in tournaments.find().sort("created", -1).limit(15):
                kb.add(types.InlineKeyboardButton(f"{t['name']} · {t['status']}"[:50], callback_data=f"ct_v_{t['_id']}"))
            kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="ct_home"))
            del_msg(c)
            return bot.send_message(chat_id, "📋 <b>Турниры</b>", parse_mode="HTML", reply_markup=kb)
        if d.startswith("ct_v_"):
            del_msg(c)
            return ct_view(chat_id, d[len("ct_v_"):])
        if d.startswith("ct_go_"):
            tid = d[len("ct_go_"):]
            t = tournaments.find_one({"_id": tid})
            ids = [x for x in (t or {}).get("clans", []) if clans.find_one({"_id": x}, {"_id": 1})]
            if not t or t["status"] != "registration" or len(ids) < 2:
                return bot.send_message(chat_id, "❌ Нужна регистрация и минимум 2 клана.")
            r = tournaments.update_one({"_id": tid, "status": "registration"},
                                       {"$set": {"status": "running", "phase": "processing", "clans": ids, "alive": ids, "round": 0}})
            if not r.modified_count:
                return
            try:
                t_start_round(tid)
            except Exception as e:
                logger.error(f"Ошибка запуска турнира: {e}")
                tournaments.update_one({"_id": tid}, {"$set": {"status": "registration", "phase": None}})
                return bot.send_message(chat_id, "❌ Не удалось запустить турнир, см. логи.")
            del_msg(c)
            bot.send_message(chat_id, "✅ Турнир запущен! Первый раунд начался.")
            return ct_view(chat_id, tid)
        if d.startswith("ct_skip_"):
            tid = d[len("ct_skip_"):]
            tournaments.update_one({"_id": tid, "status": "running", "phase": {"$in": ["battle", "break"]}}, {"$set": {"phase_end": 0}})
            tournament_tick()
            tournament_tick()
            del_msg(c)
            return ct_view(chat_id, tid)
        if d.startswith("ct_stop_"):
            tid = d[len("ct_stop_"):]
            tournaments.update_one({"_id": tid, "status": {"$in": ["registration", "running"]}}, {"$set": {"status": "cancelled", "phase": None}})
            clan_battles.update_many({"tid": tid, "status": "active"}, {"$set": {"status": "cancelled"}})
            del_msg(c)
            bot.send_message(chat_id, "🛑 Турнир отменён.")
            return ct_home(chat_id)
    except Exception as e:
        logger.error(f"Ошибка ct_callback ({d}): {e}")

# ---------------------------------------------------------------
# ЯЧЕЙКИ ФАРМА (предметы в инвентаре)
# ---------------------------------------------------------------

MAX_ICE_CELLS = 3       # всего ячеек ICE (основная + 2 доп.)
MAX_JEWEL_CELLS = 2     # всего ячеек Jewel (капсула + 1 доп.)
CELL_PRICE = {"ice": 800, "jewel": 800}   # цена ячейки в ICE
CELL_NAMES = {"ice": "Ячейка фарма ICE", "jewel": "Ячейка фарма Jewel"}

def make_cell_item(res):
    return {
        "iid": uuid.uuid4().hex[:10], "name": CELL_NAMES[res],
        "desc": f"Доп. ячейка фарма {'ICE' if res == 'ice' else 'Jewel'}. Активируйте в инвентаре — её можно передать другому игроку.",
        "type": "item", "file_id": None, "rarity": "rare", "cosmetic": f"farm_cell_{res}", "emoji": None, "title": None,
        "tpl": None, "date": int(time.time()),
    }

@bot.callback_query_handler(func=lambda c: c.data.startswith("cell_"))
def cell_activate(c):
    try:
        uid = c.from_user.id
        iid = c.data[len("cell_"):]
        u = users.find_one({"_id": uid}) or {}
        item = next((x for x in u.get("inventory", []) if x.get("iid") == iid and str(x.get("cosmetic", "")).startswith("farm_cell_")), None)
        if not item:
            return bot.answer_callback_query(c.id, "❌ Предмет не найден", show_alert=True)
        res = "ice" if item["cosmetic"] == "farm_cell_ice" else "jewel"
        cap = (MAX_ICE_CELLS if res == "ice" else MAX_JEWEL_CELLS) - 1
        if res == "jewel" and not (u.get("capsule") or {}).get("owned"):
            return bot.answer_callback_query(c.id, "Сначала купите 💊 Капсулу (основную ячейку Jewel)", show_alert=True)
        field = f"farm_cells.{res}"
        r = users.update_one(
            {"_id": uid, "inventory.iid": iid, "$or": [{field: {"$exists": False}}, {field: {"$lt": cap}}]},
            {"$inc": {field: 1}, "$pull": {"inventory": {"iid": iid}}}
        )
        if r.modified_count == 0:
            return bot.answer_callback_query(c.id, f"❌ Достигнут максимум доп. ячеек ({cap}). Предмет сохранён.", show_alert=True)
        bot.answer_callback_query(c.id, "✅ Ячейка активирована!", show_alert=True)
    except Exception as e:
        logger.error(f"Ошибка cell_activate: {e}")

# ---------------------------------------------------------------
# МАГАЗИН (вместо «Купить VIP») + ЗВЁЗДЫ TELEGRAM
# ---------------------------------------------------------------

SHOP_IMG = "https://i.ibb.co/hRd4ZknM/Meet-the-Wandering-Trader.jpg"
SHOP_CONTACT = "Herozvz"
VIP_PRICE_GOLD = "5kk"
GOLD_PER_ICE = "1k"
GOLD_PER_JEWEL = "15k"
STARS_ICE_RATE = 150      # 150 ICE = 1 ⭐
STARS_JEWEL_RATE = 1      # 1 Jewel = 1 ⭐
STAR_PACKS = [1, 10, 35]

def star_bonus_pct(n):
    if n >= 35:
        return 10
    if n >= 10:
        return 5
    return 1

def star_reward(res, n):
    base = n * (STARS_JEWEL_RATE if res == "jewel" else STARS_ICE_RATE)
    return base, round(base * (1 + star_bonus_pct(n) / 100), 2)

def gold_url(text):
    return f"https://t.me/{SHOP_CONTACT}?text=" + _urlquote(text)

@bot.message_handler(commands=["shop"])
@bot.message_handler(func=lambda m: m.text == "🛒 Магазин")
def shop_cmd(m):
    try:
        get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
        shop_send(m.chat.id, 1, getattr(m, "message_thread_id", None), m.from_user.id)
    except Exception as e:
        logger.error(f"Ошибка shop_cmd: {e}")

def stars_invoice(uid, res, n):
    n = int(n)
    base, get = star_reward(res, n)
    unit = f"{fmt(get)} Snow Jewel" if res == "jewel" else f"{fmt(get)} ICE"
    bot.send_invoice(
        chat_id=uid, title=unit[:32],
        description=f"{n}⭐ → {fmt(base)} + бонус {star_bonus_pct(n)}% = {unit}",
        invoice_payload=f"stars:{res}:{n}", provider_token="", currency="XTR",
        prices=[types.LabeledPrice(label=unit[:32], amount=n)]
    )

def shop_custom_step(m, res):
    try:
        if adm_cancelled(m):
            return bot.send_message(m.chat.id, "❌ Отменено.")
        try:
            n = int((m.text or "").strip())
            if n < 1 or n > 10000:
                raise ValueError
        except Exception:
            return adm_ask(m.chat.id, "❌ Введите целое число звёзд от 1 до 10000:", shop_custom_step, res)
        stars_invoice(m.from_user.id, res, n)
    except Exception as e:
        logger.error(f"Ошибка shop_custom_step: {e}")
        bot.send_message(m.chat.id, "❌ Не удалось создать счёт.")

@bot.pre_checkout_query_handler(func=lambda q: True)
def pre_checkout(q):
    try:
        ok = str(q.invoice_payload).startswith("stars:") and users.find_one({"_id": q.from_user.id}) is not None
        bot.answer_pre_checkout_query(q.id, ok=ok, error_message=None if ok else "Ошибка заказа, попробуйте снова")
    except Exception as e:
        logger.error(f"Ошибка pre_checkout: {e}")

@bot.message_handler(content_types=["successful_payment"])
def on_successful_payment(m):
    try:
        sp = m.successful_payment
        parts = str(sp.invoice_payload).split(":")
        if len(parts) != 3 or parts[0] != "stars" or parts[1] not in ("ice", "jewel"):
            return
        res = parts[1]
        n = int(sp.total_amount)   # берём реальную оплаченную сумму от Telegram
        try:
            payments.insert_one({"_id": sp.telegram_payment_charge_id, "uid": m.from_user.id, "res": res, "stars": n, "date": int(time.time())})
        except DuplicateKeyError:
            return
        base, get = star_reward(res, n)
        field = "snow_jewels" if res == "jewel" else "balance"
        users.update_one({"_id": m.from_user.id}, {"$inc": {field: get}})
        bot.send_message(
            m.chat.id,
            f"✅ <b>Оплата получена!</b>\n\n{n}⭐ → <b>+{fmt(get)}</b> {SNOW_EMOJI if res == 'jewel' else 'ICE'}\n"
            f"(база {fmt(base)} + бонус {star_bonus_pct(n)}%)\nСпасибо за поддержку! 💙",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка successful_payment: {e}")

# ---------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------

# ================================================================
# НОВОЕ v5: тема чата во всех ответах, бан, картинки карточек, банк gold
# ================================================================
import io
import calendar
from datetime import datetime, timedelta, timezone
from telebot.apihelper import ApiTelegramException

TZ_OFFSET_HOURS = float(os.environ.get("TZ_OFFSET_HOURS", "6"))   # по умолчанию Asia/Bishkek (UTC+6)
BANK_BOT_URL = "https://t.me/rucoy_online_robot"                  # бот с банком gold (bot.py)

def local_dt(ts=None):
    """datetime в часовом поясе админа (для показа и расписания)."""
    ts = time.time() if ts is None else ts
    return datetime.fromtimestamp(ts, tz=timezone.utc) + timedelta(hours=TZ_OFFSET_HOURS)

def fmt_dt(ts):
    return local_dt(ts).strftime("%d.%m %H:%M")

def fmt_left(sec):
    sec = max(0, int(sec))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}д {h}ч"
    if h:
        return f"{h}ч {m}м"
    return f"{m}м"

# ---------------------------------------------------------------
# ТЕМА (топик) ЧАТА: ответ всегда уходит туда, откуда вызвали команду
# ---------------------------------------------------------------

_CTX = threading.local()

def _upd_message(upd):
    for attr in ("message", "edited_message"):
        m = getattr(upd, attr, None)
        if m is not None:
            return m
    cq = getattr(upd, "callback_query", None)
    if cq is not None:
        return getattr(cq, "message", None)
    return None

def _upd_user_id(upd):
    for attr in ("message", "edited_message", "callback_query", "pre_checkout_query"):
        o = getattr(upd, attr, None)
        fu = getattr(o, "from_user", None) if o is not None else None
        if fu is not None:
            return fu.id
    return None

_BAN_CACHE = {}

def is_banned_uid(uid):
    now = time.time()
    hit = _BAN_CACHE.get(uid)
    if hit and now - hit[1] < 30:
        return hit[0]
    try:
        d = users.find_one({"_id": uid}, {"banned": 1}) or {}
        val = bool(d.get("banned"))
    except Exception:
        val = False
    _BAN_CACHE[uid] = (val, now)
    return val

def set_banned(uid, flag=True):
    users.update_one({"_id": uid}, {"$set": {"banned": bool(flag)}})
    _BAN_CACHE.pop(uid, None)

_orig_process_new_updates = bot.process_new_updates

def _process_new_updates_ctx(updates):
    for upd in updates:
        try:
            uid = _upd_user_id(upd)
            if uid and uid != ADMIN_ID and is_banned_uid(uid):
                continue                      # забаненные игроки бота не слышат
            msg = _upd_message(upd)
            chat = thread = None
            if msg is not None and getattr(msg, "chat", None) is not None:
                chat = msg.chat.id
                if getattr(msg, "is_topic_message", None) and getattr(msg, "message_thread_id", None):
                    thread = msg.message_thread_id      # настоящая тема форума
            _CTX.chat, _CTX.thread = chat, thread
            _orig_process_new_updates([upd])
        except Exception as e:
            logger.error(f"Ошибка обработки апдейта: {e}")
        finally:
            _CTX.chat = _CTX.thread = None

bot.process_new_updates = _process_new_updates_ctx

def _install_thread_sender(name):
    orig = getattr(bot, name)

    def sender(*a, **kw):
        chat = a[0] if a else kw.get("chat_id")
        ctx_chat = getattr(_CTX, "chat", None)
        if ctx_chat is not None and chat == ctx_chat:
            # в чате, где вызвали команду, ответ всегда идёт в ту же тему (или без темы, если её нет)
            kw["message_thread_id"] = getattr(_CTX, "thread", None)
        try:
            return orig(*a, **kw)
        except ApiTelegramException as e:
            if kw.get("message_thread_id") is not None and "thread not found" in str(e).lower():
                kw.pop("message_thread_id", None)
                return orig(*a, **kw)
            raise
    setattr(bot, name, sender)

for _n in ("send_message", "send_photo", "send_animation", "send_video", "send_document"):
    _install_thread_sender(_n)

@bot.message_handler(commands=["ban", "unban"])
def ban_cmd(m):
    if m.from_user.id != ADMIN_ID:
        return
    parts = (m.text or "").split()
    flag = parts[0].lstrip("/").split("@")[0] == "ban"
    rm = getattr(m, "reply_to_message", None)
    try:
        if len(parts) > 1:
            target = int(parts[1])
        elif rm is not None and getattr(rm, "from_user", None) is not None:
            target = rm.from_user.id
        else:
            return bot.reply_to(m, "Формат: /ban ID или /unban ID (или ответом на сообщение)")
        if target == ADMIN_ID:
            return bot.reply_to(m, "❌ Админа банить нельзя.")
        set_banned(target, flag)
        bot.reply_to(m, f"{'🚫 Забанен' if flag else '✅ Разбанен'}: <code>{target}</code>", parse_mode="HTML")
    except Exception as e:
        bot.reply_to(m, f"❌ Ошибка: {e}")

# ---------------------------------------------------------------
# БАНК GOLD (общая база с bot.py: база rucoy, коллекция bank, uid строкой)
# ---------------------------------------------------------------

def gold_get(uid):
    try:
        d = bank_db.find_one({"uid": str(uid)}) or {}
        return int(d.get("balance", 0))
    except Exception as e:
        logger.error(f"Ошибка gold_get: {e}")
        return None

def gold_spend(uid, amount):
    """Атомарно списывает gold. True — списано."""
    amount = int(amount)
    if amount <= 0:
        return False
    try:
        r = bank_db.update_one({"uid": str(uid), "balance": {"$gte": amount}}, {"$inc": {"balance": -amount}})
        return r.modified_count == 1
    except Exception as e:
        logger.error(f"Ошибка gold_spend: {e}")
        return False

def gold_add(uid, amount):
    try:
        bank_db.update_one({"uid": str(uid)}, {"$inc": {"balance": int(amount)}}, upsert=True)
        return True
    except Exception as e:
        logger.error(f"Ошибка gold_add: {e}")
        return False

def fmt_gold(n):
    try:
        return "{:,}".format(int(n)).replace(",", " ")
    except Exception:
        return str(n)

# ---------------------------------------------------------------
# КАРТОЧКИ: поиск файла без учёта регистра, подбор запасных, повторы через file_id
# ---------------------------------------------------------------

try:
    from PIL import Image as _PILImage
except Exception:           # Pillow не обязателен: без него просто не сжимаем картинки
    _PILImage = None

CARDS_RAW_URL = os.environ.get("CARDS_RAW_URL", "").rstrip("/")   # необязательный запасной адрес картинок
_IMG_EXTS = ("png", "jpg", "jpeg", "webp")
_CARD_INDEX = {"t": 0.0, "force_t": 0.0, "idx": {}}
_CARD_FILE_ID = {}

def _card_index(force=False):
    now = time.time()
    idx = _CARD_INDEX["idx"]
    ttl = 300 if idx else 60
    if force and now - _CARD_INDEX["force_t"] < 30:
        force = False
    if force or now - _CARD_INDEX["t"] > ttl:
        idx = {}
        try:
            for fn in os.listdir(CARDS_DIR):
                stem, ext = os.path.splitext(fn)
                if ext.lower().lstrip(".") in _IMG_EXTS:
                    idx.setdefault(stem.lower(), []).append(fn)
        except Exception as e:
            logger.warning(f"Папка карточек недоступна ({CARDS_DIR}): {e}")
        _CARD_INDEX.update(t=now, idx=idx)
        if force:
            _CARD_INDEX["force_t"] = now
    return _CARD_INDEX["idx"]

def _pick_file(stem_lower):
    files = _card_index().get(stem_lower)
    if not files:
        return None
    files = sorted(files, key=lambda f: _IMG_EXTS.index(os.path.splitext(f)[1].lower().lstrip(".")))
    return os.path.join(CARDS_DIR, files[0])

def card_image_candidates(card):
    """Имена файлов (без расширения) по убыванию приоритета: свой уровень, затем ниже."""
    if card["kind"] == "class":
        return [CLASS_DATA[card["key"]]["name"]]
    mob = CARD_DATA[card["key"]]
    bases = []
    for b in (MOB_IMAGE_NAME.get(card["key"]), mob["name"].replace(" ", "_"), card["key"]):
        if b and b not in bases:
            bases.append(b)
    lvl_idx = min(max(card["level"] - 1, 0), len(mob["levels"]) - 1, 2)
    suffixes = [["", "_2", "_3"][i] for i in range(lvl_idx, -1, -1)]
    return [b + s for s in suffixes for b in bases]

def card_image_path(card):
    """Путь к картинке карточки. Регистр и расширение не важны, нет файла уровня — берём ниже."""
    cands = card_image_candidates(card)
    for stem in cands:
        p = _pick_file(stem.lower())
        if p:
            return p
    for stem in cands:                      # вдруг файл добавили после старта бота
        _card_index(force=True)
        p = _pick_file(stem.lower())
        if p:
            return p
    return os.path.join(CARDS_DIR, cands[0] + ".png")

def _prepare_image(path, force=False):
    """JPEG-копия для Telegram, если картинка тяжёлая/огромная (или force). Иначе None."""
    if _PILImage is None:
        return None
    try:
        size = os.path.getsize(path)
        with _PILImage.open(path) as im:
            w, h = im.size
            need = (force or size > 5 * 1024 * 1024 or max(w, h) > 2560 or (w + h) > 9000
                    or max(w, h) / max(1, min(w, h)) > 15)
            if not need:
                return None
            if im.mode in ("RGBA", "LA", "P"):
                rgba = im.convert("RGBA")
                bg = _PILImage.new("RGB", rgba.size, (24, 26, 36))
                bg.paste(rgba, mask=rgba.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
            im.thumbnail((1600, 1600))
            bio = io.BytesIO()
            im.save(bio, "JPEG", quality=88, optimize=True)
            bio.seek(0)
            bio.name = "card.jpg"
            return bio
    except Exception as e:
        logger.warning(f"Не удалось подготовить картинку {path}: {e}")
        return None

def _remember_file_id(key, msg):
    try:
        ph = getattr(msg, "photo", None)
        if ph:
            _CARD_FILE_ID[key] = ph[-1].file_id
    except Exception:
        pass

def send_card_photo(chat_id, card, caption, reply_markup=None, message_thread_id=None):
    """Фото карточки с подписью. Цепочка запасных вариантов: file_id → файл → сжатая копия → документ → текст."""
    path = card_image_path(card)
    kw = dict(caption=caption, parse_mode="HTML", reply_markup=reply_markup, message_thread_id=message_thread_id)
    if os.path.isfile(path):
        try:
            st = os.stat(path)
            key = (path, int(st.st_mtime), st.st_size)
        except Exception:
            key = (path, 0, 0)
        fid = _CARD_FILE_ID.get(key)
        if fid:
            try:
                return bot.send_photo(chat_id, fid, **kw)
            except Exception:
                _CARD_FILE_ID.pop(key, None)
        pre = _prepare_image(path)
        if pre is not None:
            try:
                msg = bot.send_photo(chat_id, pre, **kw)
                _remember_file_id(key, msg)
                return msg
            except Exception as e:
                logger.warning(f"Сжатая карточка не отправилась ({path}): {e}")
        try:
            with open(path, "rb") as fh:
                msg = bot.send_photo(chat_id, fh, **kw)
            _remember_file_id(key, msg)
            return msg
        except Exception as e:
            logger.warning(f"Фото карточки не отправилось ({path}): {e}")
        if pre is None:
            forced = _prepare_image(path, force=True)
            if forced is not None:
                try:
                    msg = bot.send_photo(chat_id, forced, **kw)
                    _remember_file_id(key, msg)
                    return msg
                except Exception as e:
                    logger.warning(f"Перекодированная карточка не отправилась ({path}): {e}")
        try:
            with open(path, "rb") as fh:
                return bot.send_document(chat_id, fh, **kw)
        except Exception as e:
            logger.error(f"Карточка не отправилась даже документом ({path}): {e}")
    else:
        logger.error(f"Картинка карты не найдена: {path}")
        if CARDS_RAW_URL:
            try:
                return bot.send_photo(chat_id, f"{CARDS_RAW_URL}/{os.path.basename(path)}", **kw)
            except Exception as e:
                logger.warning(f"Запасной адрес картинки не сработал: {e}")
    return bot.send_message(chat_id, caption, parse_mode="HTML", reply_markup=reply_markup, message_thread_id=message_thread_id)

def card_image_report():
    """Проверка всех карточек: список проблем (нет файла / слишком тяжёлая / огромная)."""
    problems, ok, seen = [], 0, set()
    combos = []
    for k, mob in CARD_DATA.items():
        for lvl in range(1, len(mob["levels"]) + 1):
            combos.append({"kind": "mob", "key": k, "level": lvl})
    for k in CLASS_DATA:
        combos.append({"kind": "class", "key": k, "level": 1})
    for card in combos:
        stem_first = card_image_candidates(card)[0]
        found = None
        for stem in card_image_candidates(card):
            found = _pick_file(stem.lower())
            if found:
                break
        label = f"{card['key']} ур.{card['level']}"
        if not found:
            problems.append(f"❌ {label}: нет файла «{stem_first}.png»")
            continue
        if os.path.basename(found).lower() != (stem_first + os.path.splitext(found)[1]).lower():
            problems.append(f"⚠️ {label}: нет своего файла, берётся {os.path.basename(found)}")
        if found in seen:
            continue
        seen.add(found)
        try:
            size = os.path.getsize(found)
            if size > 10 * 1024 * 1024:
                problems.append(f"❌ {os.path.basename(found)}: {size / 1048576:.1f} МБ — Telegram не принимает фото больше 10 МБ")
                continue
            if _PILImage is not None:
                with _PILImage.open(found) as im:
                    w, h = im.size
                if (w + h) > 10000:
                    problems.append(f"❌ {os.path.basename(found)}: {w}×{h} — сумма сторон больше 10000")
                    continue
                if max(w, h) / max(1, min(w, h)) > 20:
                    problems.append(f"❌ {os.path.basename(found)}: {w}×{h} — слишком вытянутая")
                    continue
            ok += 1
        except Exception as e:
            problems.append(f"❌ {os.path.basename(found)}: не читается ({e})")
    return ok, len(seen), problems

@bot.message_handler(commands=["cardcheck"])
def cardcheck_cmd(m):
    if m.from_user.id != ADMIN_ID:
        return
    _card_index(force=True)
    ok, total, problems = card_image_report()
    head = (f"🃏 <b>Проверка картинок карточек</b>\nПапка: <code>{safe(CARDS_DIR)}</code>\n"
            f"Файлов найдено: <b>{len(_CARD_INDEX['idx'])}</b>, уникальных в игре: <b>{total}</b>, без проблем: <b>{ok}</b>\n"
            f"Pillow: {'✅ есть (тяжёлые картинки сжимаются сами)' if _PILImage is not None else '❌ нет — добавьте Pillow в requirements.txt'}")
    if problems:
        head += "\n\n" + "\n".join(safe(p) for p in problems[:40])
    else:
        head += "\n\n✅ Все файлы на месте."
    bot.reply_to(m, head[:4000], parse_mode="HTML")

@bot.message_handler(commands=["cardtest"])
def cardtest_cmd(m):
    """Админ: присылает все картинки карточек по одной и сообщает, какие не отправились как фото."""
    if m.from_user.id != ADMIN_ID:
        return
    bad = []
    sent = 0
    for k, mob in CARD_DATA.items():
        for lvl in range(1, len(mob["levels"]) + 1):
            card = {"kind": "mob", "key": k, "level": lvl, "id": "t"}
            try:
                msg = send_card_photo(m.chat.id, card, f"{mob['name']} ур.{lvl}")
                if getattr(msg, "photo", None):
                    sent += 1
                else:
                    bad.append(f"{mob['name']} ур.{lvl}")
            except Exception as e:
                bad.append(f"{mob['name']} ур.{lvl} ({e})")
            time.sleep(0.4)
    for k, c in CLASS_DATA.items():
        card = {"kind": "class", "key": k, "level": 1, "id": "t"}
        try:
            msg = send_card_photo(m.chat.id, card, c["name"])
            if getattr(msg, "photo", None):
                sent += 1
            else:
                bad.append(c["name"])
        except Exception as e:
            bad.append(f"{c['name']} ({e})")
    bot.send_message(m.chat.id, f"✅ Фото ушло: {sent}\n" + ("❌ Не как фото:\n" + "\n".join(bad) if bad else "Все карточки отправляются как фото."))

# ---------------------------------------------------------------
# ФАРМ: диапазон добычи (чтобы после апгрейда было видно рост)
# ---------------------------------------------------------------

def farm_range(level):
    """Диапазон добычи за фарм на уровне level (случайная часть от 0.1 до 1.0)."""
    return round(level * 0.5 + 0.1, 1), round(level * 0.5 + 1.0, 1)


# ================================================================
# НОВОЕ v5: КЛАНОВЫЕ ВОЙНЫ (вызовы) + пошаговые бои против бота-защитника
# ================================================================

WAR_CFG_DEFAULT = {
    "war_hours": 6,          # сколько длится война после принятия вызова
    "war_win_xp": 400,       # XP клану-победителю
    "war_lose_xp": 120,      # XP проигравшему (за участие)
    "war_draw_xp": 200,      # XP каждому при ничьей
    "war_daily_limit": 3,    # макс. войн в сутки на клан (вызовов принято/отправлено)
    "war_pair_cd_h": 12,     # пауза между войнами одной и той же пары кланов
    "war_min_members": 2,    # минимум участников в клане, чтобы воевать
    "war_invite_min": 30,    # сколько минут висит вызов
}

def war_cfg():
    doc = settings.find_one({"_id": "war_cfg"}) or {}
    return {k: doc.get(k, v) for k, v in WAR_CFG_DEFAULT.items()}

WAR_FIGHTS = {}            # fid -> состояние пошагового боя против бота
WAR_TURN_SECONDS = 45

def clan_active_war(cid):
    return clan_battles.find_one({"status": "active", "$or": [{"a": cid}, {"b": cid}]})

def war_count_today(cid):
    since = int(time.time()) - 86400
    return clan_battles.count_documents({"tid": "war", "created": {"$gte": since}, "$or": [{"a": cid}, {"b": cid}]})

def war_pair_recent(ca, cb, hours):
    since = int(time.time()) - int(hours * 3600)
    return clan_battles.find_one({"tid": "war", "created": {"$gte": since},
                                  "$or": [{"a": ca, "b": cb}, {"a": cb, "b": ca}]}) is not None

def war_create(ca, cb, end_ts):
    bid = battle_create("war", 1, ca, cb, end_ts)
    clan_battles.update_one({"_id": bid}, {"$set": {"created": int(time.time())}})
    return bid

def war_challenge_list(chat_id, clan, thread_id=None):
    cfg = war_cfg()
    docs = [c for c in clans.find({"_id": {"$ne": clan["_id"]}}) if len(c.get("members", [])) >= int(cfg["war_min_members"])]
    my_pow = clan_power(clan)
    docs.sort(key=lambda c: abs(clan_power(c) - my_pow))
    kb = types.InlineKeyboardMarkup(row_width=1)
    for c in docs[:15]:
        kb.add(types.InlineKeyboardButton(f"{c['name']} · ур.{clan_level(c)} · ⭐{fmt(clan_power(c))}"[:60], callback_data=f"wr_c_{c['_id']}"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="cl_home"))
    text = ("⚔️ <b>Бросить вызов клану</b>\n\n"
            "Выберите соперника — в списке сначала кланы, близкие вам по силе.\n"
            f"Победа в войне даёт клану <b>{cfg['war_win_xp']} XP</b>, участие — <b>{cfg['war_lose_xp']} XP</b>.")
    if not docs:
        text += "\n\n<i>Подходящих кланов пока нет.</i>"
    bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb, message_thread_id=thread_id)

def war_open_screen(chat_id, uid, clan, thread_id=None):
    """Экран «Клановый бой»: идущая война, входящие вызовы, кнопка бросить вызов."""
    cfg = war_cfg()
    kb = types.InlineKeyboardMarkup(row_width=1)
    mgr = is_mgr(clan, uid)
    b = clan_active_war(clan["_id"])
    lines = ["⚔️ <b>Клановые бои</b>", ""]
    if b:
        other = b["b"] if b["a"] == clan["_id"] else b["a"]
        oc = clans.find_one({"_id": other}) or {}
        lines.append(f"🔥 Сейчас идёт бой против <b>{safe(oc.get('name', '?'))}</b> "
                     f"({'турнир' if b.get('tid') != 'war' else 'война'}), счёт <b>{b['sa']} : {b['sb']}</b>")
        kb.add(types.InlineKeyboardButton("⚔️ Открыть бой", callback_data=f"bt_o_{b['_id']}"))
    else:
        lines.append("Сейчас боёв нет.")
    inv = list(clan_invites.find({"to": clan["_id"], "status": "pending", "exp": {"$gt": int(time.time())}}))
    if mgr and inv:
        lines += ["", "📨 <b>Входящие вызовы:</b>"]
        for i in inv:
            fc = clans.find_one({"_id": i["frm"]}) or {}
            lines.append(f"• {safe(fc.get('name', '?'))} (ур. {clan_level(fc)}, ⭐{fmt(clan_power(fc))})")
            kb.add(types.InlineKeyboardButton(f"✅ Принять: {fc.get('name', '?')}"[:50], callback_data=f"wr_y_{i['_id']}"),
                   types.InlineKeyboardButton("❌", callback_data=f"wr_n_{i['_id']}"))
    lines += ["", f"Войн сегодня: <b>{war_count_today(clan['_id'])}/{cfg['war_daily_limit']}</b>",
              "<i>Каждый участник атакует один раз: вы ходите по очереди с картой защитника-бота. "
              "Защитник свою атаку не тратит.</i>"]
    if mgr and not b:
        kb.add(types.InlineKeyboardButton("🎯 Бросить вызов", callback_data="wr_list"))
    elif not mgr:
        lines.append("<i>Бросать вызовы могут лидер и замы.</i>")
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="cl_home"))
    bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML", reply_markup=kb, message_thread_id=thread_id)

# ---------- пошаговая атака на защитника ----------

def war_card_for(uid):
    """Лучшая по силе карта игрока (защитника-бота/атакующего используем его активную)."""
    c = get_active_card(uid)
    if c:
        return c
    u = users.find_one({"_id": uid}) or {}
    cards = u.get("cards") or []
    return max(cards, key=card_pts) if cards else None

def war_fighter(uid, card, name):
    n, hp, dmg, dfn, rng = card_stats(card)
    return {"uid": uid, "name": name, "card": card, "card_name": n, "hp": hp, "max_hp": hp,
            "dmg": dmg, "def": dfn, "rng": rng, "shield": False}

def war_roll(f):
    base, rng = f["dmg"], f["rng"]
    return base if rng is None else random.randint(max(0, base - rng), base + rng)

def war_start_fight(bid, attacker, target, chat_id, thread_id):
    """Создаёт пошаговый бой: игрок атакует, защитником играет бот. Атака защитника не тратится."""
    b = clan_battles.find_one({"_id": bid})
    if not b or b.get("status") != "active" or int(time.time()) >= b["end_ts"]:
        return False, "⌛ Бой уже завершён."
    side_my = "ra" if any(x["uid"] == attacker for x in b["ra"]) else ("rb" if any(x["uid"] == attacker for x in b["rb"]) else None)
    if not side_my:
        return False, "❌ Вы не в составе этого боя."
    en = "rb" if side_my == "ra" else "ra"
    if not any(x["uid"] == target for x in b[en]):
        return False, "❌ Цель не найдена."
    # атаку забираем сразу (атомарно), чтобы нельзя было открыть два боя
    r = clan_battles.update_one({"_id": bid, side_my: {"$elemMatch": {"uid": attacker, "att": 1}}}, {"$set": {f"{side_my}.$.att": 2}})
    if r.modified_count == 0:
        return False, "⚔️ Ваша атака уже использована или идёт бой."
    ca, cd = war_card_for(attacker), war_card_for(target)
    if not ca or not cd:
        clan_battles.update_one({"_id": bid, side_my: {"$elemMatch": {"uid": attacker}}}, {"$set": {f"{side_my}.$.att": 1}})
        return False, "❌ У участника нет карточки для боя."
    names = user_names([attacker, target])
    fid = uuid.uuid4().hex[:10]
    WAR_FIGHTS[fid] = {
        "bid": bid, "my": side_my, "chat_id": chat_id, "thread_id": thread_id, "turn_token": 0, "stage": "fighting",
        "p": war_fighter(attacker, ca, names.get(attacker, str(attacker))),
        "e": war_fighter(target, cd, names.get(target, str(target)) + " 🤖"),
        "msg_id": None, "last": "",
    }
    war_turn_message(fid)
    return True, ""

def war_turn_message(fid, action_text=""):
    f = WAR_FIGHTS.get(fid)
    if not f or f["stage"] != "fighting":
        return
    p, e = f["p"], f["e"]
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("⚔️ Атаковать", callback_data=f"wf_{fid}_a"),
           types.InlineKeyboardButton("🛡 Защита", callback_data=f"wf_{fid}_d"))
    text = ((action_text + "\n\n") if action_text else "") + (
        f"⚔️ <b>Клановый бой</b>\n"
        f"🃏 Вы: <b>{safe(p['card_name'])}</b> — ❤️ {p['hp']}/{p['max_hp']}\n"
        f"🤖 Защитник {safe(e['name'])}: <b>{safe(e['card_name'])}</b> — ❤️ {e['hp']}/{e['max_hp']}\n\n"
        f"👉 Ваш ход ({WAR_TURN_SECONDS} сек, иначе поражение).\n"
        f"⚔️ Атаковать — удар.   🛡 Защита — ослабить следующий удар.")
    if f.get("msg_id"):
        try:
            bot.delete_message(f["chat_id"], f["msg_id"])
        except Exception:
            pass
    msg = None
    try:
        msg = send_card_photo(f["chat_id"], p["card"], text, reply_markup=kb, message_thread_id=f.get("thread_id"))
    except Exception as ex:
        logger.warning(f"war_turn_message photo: {ex}")
    if not msg:
        msg = bot.send_message(f["chat_id"], text, parse_mode="HTML", reply_markup=kb, message_thread_id=f.get("thread_id"))
    f["msg_id"] = msg.message_id
    f["turn_token"] += 1
    t = threading.Timer(WAR_TURN_SECONDS, war_timeout, args=[fid, f["turn_token"]])
    t.daemon = True
    t.start()

def war_timeout(fid, token):
    f = WAR_FIGHTS.get(fid)
    if not f or f["stage"] != "fighting" or f["turn_token"] != token:
        return
    war_finish(fid, False, "⏱ Вы не успели сделать ход — поражение!")

def war_bot_turn(f):
    """Ход бота-защитника: бьёт, а при низком здоровье иногда встаёт в защиту."""
    e, p = f["e"], f["p"]
    if e["hp"] <= e["max_hp"] * 0.35 and not e["shield"] and random.random() < 0.35:
        e["shield"] = True
        return f"🤖 {safe(e['name'])} занял защитную стойку."
    dmg = war_roll(e)
    if p["shield"]:
        dmg = round(dmg * (1 - p["def"] / 100), 0)
        p["shield"] = False
    dmg = max(0, int(dmg))
    p["hp"] = max(0, p["hp"] - dmg)
    return f"🤖 {safe(e['name'])} ответил ударом: <b>{dmg}</b> урона!"

@bot.callback_query_handler(func=lambda c: c.data.startswith("wf_"))
def war_fight_callback(c):
    try:
        _, fid, mv = c.data.split("_", 2)
        f = WAR_FIGHTS.get(fid)
        if not f or f["stage"] != "fighting":
            return bot.answer_callback_query(c.id, "❌ Бой уже завершён")
        if c.from_user.id != f["p"]["uid"]:
            return bot.answer_callback_query(c.id, "Это не ваш бой!", show_alert=True)
        p, e = f["p"], f["e"]
        f["turn_token"] += 1                      # гасим таймер этого хода
        if mv == "d":
            p["shield"] = True
            text = "🛡 Вы заняли защитную стойку."
        else:
            dmg = war_roll(p)
            if e["shield"]:
                dmg = round(dmg * (1 - e["def"] / 100), 0)
                e["shield"] = False
            dmg = max(0, int(dmg))
            e["hp"] = max(0, e["hp"] - dmg)
            text = f"💥 Вы нанесли <b>{dmg}</b> урона!"
            if e["hp"] <= 0:
                bot.answer_callback_query(c.id)
                return war_finish(fid, True, text)
        text += "\n" + war_bot_turn(f)
        bot.answer_callback_query(c.id)
        if p["hp"] <= 0:
            return war_finish(fid, False, text)
        war_turn_message(fid, text)
    except Exception as ex:
        logger.error(f"Ошибка war_fight_callback: {ex}")
        try:
            bot.answer_callback_query(c.id, "❌ Ошибка")
        except Exception:
            pass

def war_finish(fid, win, reason):
    f = WAR_FIGHTS.pop(fid, None)
    if not f or f["stage"] != "fighting":
        return
    f["stage"] = "done"
    bid, my = f["bid"], f["my"]
    side = "a" if my == "ra" else "b"
    p, e = f["p"], f["e"]
    b = clan_battles.find_one({"_id": bid})
    line = f"{'🏆' if win else '🛡'} {p['name']} → {e['name']}"
    upd = {"$push": {"log": {"$each": [line], "$slice": -8}}}
    sets = {f"{my}.$.att": 0}
    if win:
        tgt_pts = next((x["pts"] for x in (b["rb"] if my == "ra" else b["ra"]) if x["uid"] == e["uid"]), 0) if b else 0
        upd["$inc"] = {f"s{side}": 1, f"tb{side}": tgt_pts}
        sets[f"{my}.$.won"] = True
    r = clan_battles.update_one({"_id": bid, my: {"$elemMatch": {"uid": p["uid"], "att": 2}}}, {"$set": sets})
    if r.modified_count:
        clan_battles.update_one({"_id": bid}, upd)
    text = f"{reason}\n\n" + ("🏆 <b>Победа!</b> +1 к счёту клана." if win else "🛡 <b>Поражение.</b> Защитник устоял.")
    if f.get("msg_id"):
        try:
            bot.delete_message(f["chat_id"], f["msg_id"])
        except Exception:
            pass
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("📊 Табло боя", callback_data=f"bt_o_{bid}"))
    sent = None
    try:
        sent = send_card_photo(f["chat_id"], (p if win else e)["card"], text, reply_markup=kb, message_thread_id=f.get("thread_id"))
    except Exception:
        pass
    if not sent:
        bot.send_message(f["chat_id"], text, parse_mode="HTML", reply_markup=kb, message_thread_id=f.get("thread_id"))

def war_finish_due():
    """Закрывает войны (tid='war'), у которых вышло время; выдаёт XP."""
    now = int(time.time())
    cfg = war_cfg()
    for b in list(clan_battles.find({"tid": "war", "status": "active", "end_ts": {"$lte": now}})):
        r = clan_battles.update_one({"_id": b["_id"], "status": "active"}, {"$set": {"status": "done"}})
        if not r.modified_count:
            continue
        ca, cb = clans.find_one({"_id": b["a"]}) or {}, clans.find_one({"_id": b["b"]}) or {}
        score = f"{safe(ca.get('name', '?'))} <b>{b['sa']} : {b['sb']}</b> {safe(cb.get('name', '?'))}"
        if b["sa"] == b["sb"] and b["tba"] == b["tbb"]:
            clan_battles.update_one({"_id": b["_id"]}, {"$set": {"winner": None}})
            for cid in (b["a"], b["b"]):
                clan_add_xp(cid, int(cfg["war_draw_xp"]))
                clan_notify(cid, f"🏁 <b>Война окончена — ничья!</b>\n{score}\n+{cfg['war_draw_xp']} XP клану")
            continue
        w = b["a"] if (b["sa"], b["tba"]) > (b["sb"], b["tbb"]) else b["b"]
        l = b["b"] if w == b["a"] else b["a"]
        clan_battles.update_one({"_id": b["_id"]}, {"$set": {"winner": w}})
        clan_add_xp(w, int(cfg["war_win_xp"]))
        clan_add_xp(l, int(cfg["war_lose_xp"]))
        clan_notify(w, f"🏁 <b>Война выиграна!</b>\n{score}\n🎉 +{cfg['war_win_xp']} XP клану")
        clan_notify(l, f"🏁 <b>Война проиграна.</b>\n{score}\n+{cfg['war_lose_xp']} XP клану за участие")

@bot.callback_query_handler(func=lambda c: c.data.startswith("wr_"))
def war_callback(c):
    uid = c.from_user.id
    chat_id = c.message.chat.id
    t_id = getattr(c.message, "message_thread_id", None)
    d = c.data
    try:
        clan = get_user_clan(uid)
        if not clan:
            return bot.answer_callback_query(c.id, "Вы не состоите в клане", show_alert=True)
        cfg = war_cfg()
        if d == "wr_list":
            if not is_mgr(clan, uid):
                return bot.answer_callback_query(c.id, "Только лидер и замы", show_alert=True)
            bot.answer_callback_query(c.id)
            return war_challenge_list(chat_id, clan, t_id)
        if d.startswith("wr_c_"):
            if not is_mgr(clan, uid):
                return bot.answer_callback_query(c.id, "Только лидер и замы", show_alert=True)
            tid = d[len("wr_c_"):]
            tc = clans.find_one({"_id": tid})
            if not tc or tid == clan["_id"]:
                return bot.answer_callback_query(c.id, "Клан не найден", show_alert=True)
            if len(clan.get("members", [])) < int(cfg["war_min_members"]) or len(tc.get("members", [])) < int(cfg["war_min_members"]):
                return bot.answer_callback_query(c.id, f"В клане должно быть минимум {cfg['war_min_members']} игрока(ов)", show_alert=True)
            if clan_active_war(clan["_id"]) or clan_active_war(tid):
                return bot.answer_callback_query(c.id, "У одного из кланов уже идёт бой", show_alert=True)
            if war_count_today(clan["_id"]) >= int(cfg["war_daily_limit"]) or war_count_today(tid) >= int(cfg["war_daily_limit"]):
                return bot.answer_callback_query(c.id, "Дневной лимит войн исчерпан", show_alert=True)
            if war_pair_recent(clan["_id"], tid, cfg["war_pair_cd_h"]):
                return bot.answer_callback_query(c.id, f"С этим кланом недавно воевали — пауза {cfg['war_pair_cd_h']} ч", show_alert=True)
            if clan_invites.find_one({"frm": clan["_id"], "to": tid, "status": "pending", "exp": {"$gt": int(time.time())}}):
                return bot.answer_callback_query(c.id, "Вызов уже отправлен", show_alert=True)
            iid = uuid.uuid4().hex[:10]
            clan_invites.insert_one({"_id": iid, "frm": clan["_id"], "to": tid, "by": uid, "status": "pending",
                                     "exp": int(time.time()) + int(cfg["war_invite_min"]) * 60})
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("✅ Принять", callback_data=f"wr_y_{iid}"),
                   types.InlineKeyboardButton("❌ Отклонить", callback_data=f"wr_n_{iid}"))
            clan_notify(tid, f"⚔️ <b>Вызов на клановую войну!</b>\n\nКлан <b>{safe(clan['name'])}</b> (ур. {clan_level(clan)}, ⭐{fmt(clan_power(clan))}) "
                             f"вызывает вас. Бой — {cfg['war_hours']} ч. Победа: {cfg['war_win_xp']} XP.\n"
                             f"Вызов действует {cfg['war_invite_min']} мин. Принять может лидер или зам.", kb, only_mgr=True)
            return bot.answer_callback_query(c.id, "✅ Вызов отправлен лидеру и замам клана", show_alert=True)
        if d.startswith("wr_y_") or d.startswith("wr_n_"):
            if not is_mgr(clan, uid):
                return bot.answer_callback_query(c.id, "Только лидер и замы", show_alert=True)
            iid = d[5:]
            inv = clan_invites.find_one({"_id": iid})
            if not inv or inv["to"] != clan["_id"]:
                return bot.answer_callback_query(c.id, "Вызов не найден", show_alert=True)
            if d.startswith("wr_n_"):
                clan_invites.update_one({"_id": iid, "status": "pending"}, {"$set": {"status": "declined"}})
                clan_notify(inv["frm"], f"❌ Клан <b>{safe(clan['name'])}</b> отклонил ваш вызов.", only_mgr=True)
                bot.answer_callback_query(c.id, "Отклонено")
                return del_msg(c)
            if int(time.time()) >= inv["exp"]:
                return bot.answer_callback_query(c.id, "⌛ Вызов устарел", show_alert=True)
            fc = clans.find_one({"_id": inv["frm"]})
            if not fc:
                return bot.answer_callback_query(c.id, "Клан-соперник не найден", show_alert=True)
            if clan_active_war(clan["_id"]) or clan_active_war(fc["_id"]):
                return bot.answer_callback_query(c.id, "У одного из кланов уже идёт бой", show_alert=True)
            r = clan_invites.update_one({"_id": iid, "status": "pending"}, {"$set": {"status": "accepted"}})
            if r.modified_count == 0:
                return bot.answer_callback_query(c.id, "Вызов уже обработан", show_alert=True)
            end_ts = int(time.time()) + int(float(cfg["war_hours"]) * 3600)
            bid = war_create(fc, clan, end_ts)
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("⚔️ Открыть бой", callback_data=f"bt_o_{bid}"))
            for me, foe in ((fc, clan), (clan, fc)):
                clan_notify(me["_id"], f"🔥 <b>Клановая война началась!</b>\n\n{safe(me['name'])} 🆚 {safe(foe['name'])}\n"
                                       f"У каждого игрока одна атака: вы деретесь по очереди с картой защитника-бота (его атака не тратится). "
                                       f"Время: {cfg['war_hours']} ч.", kb)
            bot.answer_callback_query(c.id, "✅ Война началась!")
            return del_msg(c)
        bot.answer_callback_query(c.id)
    except Exception as ex:
        logger.error(f"Ошибка war_callback ({d}): {ex}")
        try:
            bot.answer_callback_query(c.id, "❌ Ошибка")
        except Exception:
            pass

# ================================================================
# НОВОЕ v5: МАГАЗИН — gold из банка (bot.py), страницы админа, скидки, VIP-предмет
# ================================================================

VIP_ITEM_PRICE_GOLD = int(os.environ.get("VIP_PRICE_GOLD_INT", "5000000"))   # 5kk
GOLD_PER_ICE_N = 1000          # 1 ICE = 1k gold
GOLD_PER_JEWEL_N = 15000       # 1 Jewel = 15k gold

def parse_gold(text):
    """5kk -> 5_000_000, 15k -> 15000, 1.5kk, 2000."""
    t = str(text).strip().lower().replace(" ", "").replace(",", ".")
    mult = 1
    if t.endswith("kk"):
        mult, t = 1_000_000, t[:-2]
    elif t.endswith("k"):
        mult, t = 1_000, t[:-1]
    try:
        v = int(round(float(t) * mult))
        return v if v > 0 else None
    except Exception:
        return None

def shop_cfg():
    d = settings.find_one({"_id": "shop_cfg"}) or {}
    return {"discount": float(d.get("discount", 0) or 0)}   # общая скидка на gold-товары, %

def gold_price(base, page_discount=0.0):
    disc = max(float(page_discount or 0), shop_cfg()["discount"])
    disc = min(max(disc, 0), 95)
    return max(1, int(round(base * (1 - disc / 100)))), disc

def make_vip_item():
    return {"iid": uuid.uuid4().hex[:10], "name": "👑 VIP-статус", "desc": "Активируйте в инвентаре и выберите эмодзи и фон профиля.",
            "type": "item", "file_id": None, "rarity": "legendary", "cosmetic": "vip_pass", "emoji": None, "title": None,
            "tpl": None, "date": int(time.time())}

SHOP_PAGES = settings  # страницы лежат в коллекции shop_pages (ниже)

def shop_pages_list():
    return list(shop_pages.find({"active": True}).sort("created", 1))

def shop_nav_row(kb, page_no, total_custom):
    return kb

def shop_send(chat_id, page, thread_id, uid):
    """page: 1 — основной, 2 — звёзды, 'p:<id>' — страница админа."""
    u = users.find_one({"_id": uid}) or {}
    kb = types.InlineKeyboardMarkup(row_width=2)
    gold = gold_get(uid)
    gold_line = f"🪙 Ваш gold: <b>{fmt_gold(gold)}</b>\n" if gold is not None else ""
    extra = shop_pages_list()
    if page == 1:
        vip_p, vip_d = gold_price(VIP_ITEM_PRICE_GOLD)
        ice_p, ice_d = gold_price(GOLD_PER_ICE_N)
        jw_p, jw_d = gold_price(GOLD_PER_JEWEL_N)
        def pr(p, base, d):
            return f"<s>{fmt_gold(base)}</s> <b>{fmt_gold(p)}</b> gold (−{int(d)}%)" if d else f"<b>{fmt_gold(p)}</b> gold"
        caption = (
            "🛒 <b>Магазин</b>\n\n"
            "🪙 <b>За gold</b> (списывается с вашего банка в @rucoy_online_robot):\n"
            f"👑 VIP (предмет) — {pr(vip_p, VIP_ITEM_PRICE_GOLD, vip_d)}\n"
            f"❄️ 1 ICE — {pr(ice_p, GOLD_PER_ICE_N, ice_d)}\n"
            f"{SNOW_EMOJI} 1 Jewel — {pr(jw_p, GOLD_PER_JEWEL_N, jw_d)}\n\n"
            "🛍 <b>За ICE</b>:\n"
            f"⛏ Ячейка фарма ICE — <b>{CELL_PRICE['ice']} ICE</b> (активировать можно до {MAX_ICE_CELLS - 1} раз)\n"
            f"⛏ Ячейка фарма Jewel — <b>{CELL_PRICE['jewel']} ICE</b> (до {MAX_JEWEL_CELLS - 1} раз)\n"
            f"💊 Капсула — <b>{CAPSULE_PRICE} ICE</b>\n\n"
            f"{gold_line}У вас: <b>{fmt(u.get('balance', 0))}</b> ICE · <b>{fmt(u.get('snow_jewels', 0))}</b> {SNOW_EMOJI}"
        )
        kb.add(types.InlineKeyboardButton("👑 Купить VIP", callback_data="sh_vip"))
        kb.add(types.InlineKeyboardButton("❄️ ICE за gold", callback_data="sh_g_ice"),
               types.InlineKeyboardButton(f"{SNOW_EMOJI} Jewel за gold", callback_data="sh_g_jewel"))
        kb.add(types.InlineKeyboardButton("⛏ Ячейка ICE", callback_data="sh_cell_ice"),
               types.InlineKeyboardButton("⛏ Ячейка Jewel", callback_data="sh_cell_jewel"))
        kb.add(types.InlineKeyboardButton("💊 Капсула", callback_data="sh_cap"))
        kb.add(types.InlineKeyboardButton("🏦 Пополнить gold (банк-бот)", url=BANK_BOT_URL))
        nav = [types.InlineKeyboardButton("⭐ Звёзды", callback_data="sh_p2")]
        for pg in extra[:6]:
            nav.append(types.InlineKeyboardButton(f"📄 {pg['title']}"[:24], callback_data=f"sh_pg_{pg['_id']}"))
        kb.add(*nav)
        return send_media_safe(chat_id, "photo", SHOP_IMG, caption, kb, thread_id)
    if page == 2:
        rows = []
        for n in STAR_PACKS:
            _, jw = star_reward("jewel", n)
            _, ic = star_reward("ice", n)
            rows.append(f"{n}⭐ → {fmt(jw)} {SNOW_EMOJI} или {fmt(ic)} ICE (бонус +{star_bonus_pct(n)}%)")
        caption = (
            "⭐ <b>Магазин — Звёзды Telegram</b>\n\n"
            f"1⭐ = <b>{STARS_JEWEL_RATE}</b> {SNOW_EMOJI} или <b>{STARS_ICE_RATE}</b> ICE\n\n"
            "🎁 <b>Бонус от суммы:</b>\n• 1–9 ⭐ — +1%\n• 10–34 ⭐ — +5%\n• 35+ ⭐ — +10%\n\n"
            + "\n".join(rows) + "\n\nМожно выбрать своё количество звёзд 👇"
        )
        kb.add(*[types.InlineKeyboardButton(f"{SNOW_EMOJI} {n}⭐", callback_data=f"sh_s_jewel_{n}") for n in STAR_PACKS])
        kb.add(*[types.InlineKeyboardButton(f"❄️ {n}⭐", callback_data=f"sh_s_ice_{n}") for n in STAR_PACKS])
        kb.add(types.InlineKeyboardButton(f"✏️ {SNOW_EMOJI} своё", callback_data="sh_c_jewel"),
               types.InlineKeyboardButton("✏️ ❄️ своё", callback_data="sh_c_ice"))
        kb.add(types.InlineKeyboardButton("⬅️ Магазин", callback_data="sh_p1"))
        return send_media_safe(chat_id, "photo", SHOP_IMG, caption, kb, thread_id)
    # страница админа
    pg = shop_pages.find_one({"_id": str(page)})
    if not pg:
        return bot.send_message(chat_id, "❌ Страница не найдена.", message_thread_id=thread_id)
    lines = [f"📄 <b>{safe(pg['title'])}</b>"]
    if pg.get("desc"):
        lines.append(f"<i>{safe(pg['desc'])}</i>")
    if pg.get("discount"):
        lines.append(f"🔥 Скидка на странице: <b>−{int(pg['discount'])}%</b>")
    lines.append("")
    for i, it in enumerate(pg.get("items", [])):
        p, d = gold_price(int(it["price"]), pg.get("discount", 0))
        stock = it.get("stock")
        lines.append(f"{i + 1}. {safe(it['label'])} — " + (f"<s>{fmt_gold(it['price'])}</s> " if d else "") + f"<b>{fmt_gold(p)}</b> gold"
                     + (f" · осталось {stock}" if stock is not None else ""))
        kb.add(types.InlineKeyboardButton(f"Купить: {it['label']}"[:60], callback_data=f"sh_b_{pg['_id']}_{i}"))
    if not pg.get("items"):
        lines.append("<i>Пока пусто.</i>")
    lines.append("")
    lines.append(gold_line.strip())
    kb.add(types.InlineKeyboardButton("⬅️ Магазин", callback_data="sh_p1"))
    if pg.get("file_id"):
        return send_media_safe(chat_id, pg.get("media_type", "photo"), pg["file_id"], "\n".join(lines), kb, thread_id)
    return bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML", reply_markup=kb, message_thread_id=thread_id)

def shop_deliver(uid, kind, qty=1, tpl_id=None, ice=0, jewel=0):
    """Автовыдача купленного."""
    if kind == "vip":
        users.update_one({"_id": uid}, {"$push": {"inventory": make_vip_item()}})
    elif kind == "ice":
        users.update_one({"_id": uid}, {"$inc": {"balance": float(qty)}})
    elif kind == "jewel":
        users.update_one({"_id": uid}, {"$inc": {"snow_jewels": float(qty)}})
    elif kind == "item" and tpl_id:
        tpl = catalog.find_one({"_id": tpl_id})
        if tpl:
            give_item(uid, tpl)
    elif kind == "bundle":
        if ice:
            users.update_one({"_id": uid}, {"$inc": {"balance": float(ice)}})
        if jewel:
            users.update_one({"_id": uid}, {"$inc": {"snow_jewels": float(jewel)}})

def shop_buy_gold(uid, price, kind, qty=1, tpl_id=None, ice=0, jewel=0, label=""):
    """Списывает gold из банка (rucoy.bank) и сразу выдаёт товар. Возвращает (ok, сообщение)."""
    bal = gold_get(uid)
    if bal is None:
        return False, "❌ Банк gold сейчас недоступен, попробуйте позже."
    if bal < price:
        return False, f"❌ Не хватает gold: нужно {fmt_gold(price)}, у вас {fmt_gold(bal)}. Пополнить — в @rucoy_online_robot."
    if not gold_spend(uid, price):
        return False, "❌ Не хватает gold (баланс изменился). Попробуйте ещё раз."
    try:
        shop_deliver(uid, kind, qty, tpl_id, ice, jewel)
    except Exception as e:
        gold_add(uid, price)                        # откат, если выдать не получилось
        logger.error(f"Ошибка выдачи покупки: {e}")
        return False, "❌ Не удалось выдать товар — gold возвращён."
    try:
        payments.insert_one({"_id": "g" + uuid.uuid4().hex, "uid": uid, "res": "gold:" + kind, "gold": price, "qty": qty, "label": label, "date": int(time.time())})
    except Exception:
        pass
    return True, f"✅ Покупка: {label or kind}. Списано {fmt_gold(price)} gold."

def shop_gold_qty_step(m, res):
    try:
        if adm_cancelled(m):
            return bot.send_message(m.chat.id, "❌ Отменено.")
        try:
            n = int((m.text or "").strip())
            if n < 1 or n > 1_000_000:
                raise ValueError
        except Exception:
            return adm_ask(m.chat.id, "❌ Введите целое количество (1–1 000 000):", shop_gold_qty_step, res)
        base = GOLD_PER_ICE_N if res == "ice" else GOLD_PER_JEWEL_N
        p, d = gold_price(base * n)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton(f"✅ Купить за {fmt_gold(p)} gold", callback_data=f"sh_gy_{res}_{n}"),
               types.InlineKeyboardButton("❌ Отмена", callback_data="sh_p1"))
        bot.send_message(m.chat.id, f"Покупка: <b>{fmt_gold(n)}</b> {'ICE' if res == 'ice' else 'Jewel'} за <b>{fmt_gold(p)}</b> gold"
                         + (f" (скидка −{int(d)}%)" if d else ""), parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.error(f"Ошибка shop_gold_qty_step: {e}")

# ---------- админ: страницы магазина ----------

SA_DRAFT = {}

def shopadmin_home(chat_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    pages = list(shop_pages.find().sort("created", 1))
    for p in pages:
        kb.add(types.InlineKeyboardButton(f"{'✅' if p.get('active') else '⏸'} {p['title']} (−{int(p.get('discount', 0))}%, {len(p.get('items', []))} шт.)"[:60],
                                          callback_data=f"sa_v_{p['_id']}"))
    kb.add(types.InlineKeyboardButton("➕ Новая страница", callback_data="sa_new"))
    kb.add(types.InlineKeyboardButton(f"🏷 Общая скидка на gold: −{int(shop_cfg()['discount'])}%", callback_data="sa_disc"))
    bot.send_message(chat_id, "🛒 <b>Админ магазина</b>\n\nСтраницы показываются игрокам кнопками под основным магазином. "
                              "Цены в gold, скидку считает бот.", parse_mode="HTML", reply_markup=kb)

@bot.message_handler(commands=["shopadmin"])
def shopadmin_cmd(m):
    if m.from_user.id != ADMIN_ID:
        return
    shopadmin_home(m.chat.id)

def sa_view(chat_id, pid):
    p = shop_pages.find_one({"_id": pid})
    if not p:
        return bot.send_message(chat_id, "❌ Страница не найдена.")
    lines = [f"📄 <b>{safe(p['title'])}</b> — {'активна' if p.get('active') else 'скрыта'}",
             f"Скидка: −{int(p.get('discount', 0))}%", f"Описание: {safe(p.get('desc') or '—')}", ""]
    for i, it in enumerate(p.get("items", [])):
        lines.append(f"{i + 1}. {safe(it['label'])} — {fmt_gold(it['price'])} gold [{it['kind']}]" + (f" · остаток {it['stock']}" if it.get("stock") is not None else ""))
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("➕ Товар", callback_data=f"sa_ai_{pid}"),
           types.InlineKeyboardButton("🗑 Убрать товар", callback_data=f"sa_di_{pid}"))
    kb.add(types.InlineKeyboardButton("🏷 Скидка %", callback_data=f"sa_dc_{pid}"),
           types.InlineKeyboardButton("✏️ Название", callback_data=f"sa_ti_{pid}"))
    kb.add(types.InlineKeyboardButton("📝 Описание", callback_data=f"sa_ds_{pid}"),
           types.InlineKeyboardButton("🖼 Картинка", callback_data=f"sa_im_{pid}"))
    kb.add(types.InlineKeyboardButton("⏸ Скрыть/показать", callback_data=f"sa_tg_{pid}"),
           types.InlineKeyboardButton("❌ Удалить страницу", callback_data=f"sa_rm_{pid}"))
    kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="sa_home"))
    bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML", reply_markup=kb)

def sa_step_title(m):
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Отменено.")
    t = (m.text or "").strip()[:30]
    if not t:
        return adm_ask(m.chat.id, "Введите название страницы:", sa_step_title)
    pid = uuid.uuid4().hex[:8]
    shop_pages.insert_one({"_id": pid, "title": t, "desc": "", "discount": 0, "items": [], "active": True, "created": int(time.time())})
    sa_view(m.chat.id, pid)

def sa_step_field(m, pid, field):
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Отменено.")
    t = (m.text or "").strip()
    if field == "discount":
        try:
            v = float(t.replace(",", ".").replace("%", ""))
            if not 0 <= v <= 95:
                raise ValueError
        except Exception:
            return adm_ask(m.chat.id, "Введите скидку от 0 до 95:", sa_step_field, pid, field)
        shop_pages.update_one({"_id": pid}, {"$set": {"discount": v}})
    elif field == "title":
        shop_pages.update_one({"_id": pid}, {"$set": {"title": t[:30]}})
    else:
        shop_pages.update_one({"_id": pid}, {"$set": {"desc": t[:400]}})
    sa_view(m.chat.id, pid)

def sa_step_image(m, pid):
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Отменено.")
    fid, mt = extract_media(m)
    if not fid:
        return adm_ask(m.chat.id, "Пришлите фото/гиф или ссылку:", sa_step_image, pid)
    shop_pages.update_one({"_id": pid}, {"$set": {"file_id": fid, "media_type": mt}})
    sa_view(m.chat.id, pid)

def sa_step_global(m):
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Отменено.")
    try:
        v = float((m.text or "").replace(",", ".").replace("%", ""))
        if not 0 <= v <= 95:
            raise ValueError
    except Exception:
        return adm_ask(m.chat.id, "Введите число 0–95:", sa_step_global)
    settings.update_one({"_id": "shop_cfg"}, {"$set": {"discount": v}}, upsert=True)
    shopadmin_home(m.chat.id)

def sa_kind_kb(pid):
    kb = types.InlineKeyboardMarkup(row_width=2)
    for k, t in (("vip", "👑 VIP"), ("ice", "❄️ ICE"), ("jewel", f"{SNOW_EMOJI} Jewel"), ("bundle", "🎁 Набор ICE+Jewel"), ("item", "🖼 Предмет из каталога")):
        kb.add(types.InlineKeyboardButton(t, callback_data=f"sa_k_{pid}_{k}"))
    return kb

def sa_step_price(m, pid, kind, extra):
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Отменено.")
    price = parse_gold(m.text or "")
    if not price:
        return adm_ask(m.chat.id, "Цена в gold (например 5kk, 250k, 12000):", sa_step_price, pid, kind, extra)
    extra["price"] = price
    adm_ask(m.chat.id, "Название кнопки/товара (как увидят игроки):", sa_step_label, pid, kind, extra)

def sa_step_label(m, pid, kind, extra):
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Отменено.")
    label = (m.text or "").strip()[:40]
    if not label:
        return adm_ask(m.chat.id, "Название:", sa_step_label, pid, kind, extra)
    extra["label"] = label
    adm_ask(m.chat.id, "Остаток штук (число) или 0 — без ограничений:", sa_step_stock, pid, kind, extra)

def sa_step_stock(m, pid, kind, extra):
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Отменено.")
    try:
        n = int((m.text or "0").strip())
    except Exception:
        n = 0
    it = {"kind": kind, "price": extra["price"], "label": extra["label"], "qty": extra.get("qty", 1),
          "tpl": extra.get("tpl"), "ice": extra.get("ice", 0), "jewel": extra.get("jewel", 0), "stock": (n if n > 0 else None)}
    shop_pages.update_one({"_id": pid}, {"$push": {"items": it}})
    sa_view(m.chat.id, pid)

def sa_step_qty(m, pid, kind):
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Отменено.")
    try:
        n = int((m.text or "").strip())
        if n < 1:
            raise ValueError
    except Exception:
        return adm_ask(m.chat.id, "Целое количество:", sa_step_qty, pid, kind)
    adm_ask(m.chat.id, "Цена в gold (например 5kk, 250k, 12000):", sa_step_price, pid, kind, {"qty": n})

def sa_step_bundle(m, pid):
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Отменено.")
    try:
        a, b = (m.text or "").replace(",", ".").split()
        ice, jw = float(a), float(b)
    except Exception:
        return adm_ask(m.chat.id, "Формат: <code>ICE Jewel</code>, например <code>5000 10</code>", sa_step_bundle, pid)
    adm_ask(m.chat.id, "Цена в gold (например 5kk, 250k, 12000):", sa_step_price, pid, "bundle", {"ice": ice, "jewel": jw})

def sa_step_tpl(m, pid):
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Отменено.")
    tpl = catalog.find_one({"_id": (m.text or "").strip()})
    if not tpl:
        return adm_ask(m.chat.id, "❌ Предмет не найден. Пришлите ID шаблона из /items:", sa_step_tpl, pid)
    adm_ask(m.chat.id, "Цена в gold (например 5kk, 250k, 12000):", sa_step_price, pid, "item", {"tpl": tpl["_id"]})

@bot.callback_query_handler(func=lambda c: c.data.startswith("sa_"))
def sa_callback(c):
    if c.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(c.id, "Только админ", show_alert=True)
    uid, chat_id, d = c.from_user.id, c.message.chat.id, c.data
    try:
        bot.answer_callback_query(c.id)
        if d == "sa_home":
            return shopadmin_home(chat_id)
        if d == "sa_new":
            return adm_ask(chat_id, "Название новой страницы:", sa_step_title)
        if d == "sa_disc":
            return adm_ask(chat_id, "Общая скидка на все gold-товары, % (0 — выключить):", sa_step_global)
        if d.startswith("sa_v_"):
            return sa_view(chat_id, d[5:])
        act, pid = d[3:5], d[6:]
        if act == "ai":
            return bot.send_message(chat_id, "Что продаём?", reply_markup=sa_kind_kb(pid))
        if d.startswith("sa_k_"):
            _, _, pid, kind = d.split("_")
            if kind in ("ice", "jewel"):
                return adm_ask(chat_id, f"Сколько {'ICE' if kind == 'ice' else 'Jewel'} в одной покупке?", sa_step_qty, pid, kind)
            if kind == "bundle":
                return adm_ask(chat_id, "Сколько ICE и Jewel в наборе? Формат: <code>5000 10</code>", sa_step_bundle, pid)
            if kind == "item":
                return adm_ask(chat_id, "ID шаблона предмета (из /items):", sa_step_tpl, pid)
            return adm_ask(chat_id, "Цена в gold (например 5kk, 250k, 12000):", sa_step_price, pid, kind, {})
        if act == "di":
            p = shop_pages.find_one({"_id": pid}) or {}
            kb = types.InlineKeyboardMarkup(row_width=1)
            for i, it in enumerate(p.get("items", [])):
                kb.add(types.InlineKeyboardButton(f"🗑 {it['label']}"[:50], callback_data=f"sa_dx_{pid}_{i}"))
            return bot.send_message(chat_id, "Какой товар убрать?", reply_markup=kb)
        if d.startswith("sa_dx_"):
            _, _, pid, i = d.split("_")
            p = shop_pages.find_one({"_id": pid}) or {}
            items = p.get("items", [])
            if int(i) < len(items):
                items.pop(int(i))
                shop_pages.update_one({"_id": pid}, {"$set": {"items": items}})
            return sa_view(chat_id, pid)
        if act == "dc":
            return adm_ask(chat_id, "Скидка страницы, % (0–95):", sa_step_field, pid, "discount")
        if act == "ti":
            return adm_ask(chat_id, "Новое название:", sa_step_field, pid, "title")
        if act == "ds":
            return adm_ask(chat_id, "Описание страницы:", sa_step_field, pid, "desc")
        if act == "im":
            return adm_ask(chat_id, "Пришлите фото/гиф для страницы:", sa_step_image, pid)
        if act == "tg":
            p = shop_pages.find_one({"_id": pid}) or {}
            shop_pages.update_one({"_id": pid}, {"$set": {"active": not p.get("active", True)}})
            return sa_view(chat_id, pid)
        if act == "rm":
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("✅ Да, удалить", callback_data=f"sa_rx_{pid}"), types.InlineKeyboardButton("Отмена", callback_data=f"sa_v_{pid}"))
            return bot.send_message(chat_id, "Удалить страницу безвозвратно?", reply_markup=kb)
        if d.startswith("sa_rx_"):
            shop_pages.delete_one({"_id": d[6:]})
            bot.send_message(chat_id, "🗑 Страница удалена.")
            return shopadmin_home(chat_id)
    except Exception as e:
        logger.error(f"Ошибка sa_callback ({d}): {e}")

# ---------- покупатель ----------

def shop_vip_warning_text():
    return ("👑 <b>VIP</b> покупается как предмет: он попадёт в 🎒 Инвентарь, там вы его активируете.\n\n"
            f"• +{int(VIP_FARM_BONUS_PCT * 100)}% к добыче ICE за фарм\n• +15 ICE за реферала\n• Бонусы в сезонном пассе\n"
            "• Свой эмодзи и свой фон (фото/гиф) в профиле\n\n"
            "⚠️ При активации вы сами выберете эмодзи и фон. Нельзя ставить 18+ контент, порно и оскорбления. "
            "Выбранное проходит автоматический фильтр и проверку администрацией — нарушителей баним <b>без возврата</b> вещи и gold.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("sh_"))
def shop_callback(c):
    uid = c.from_user.id
    chat_id = c.message.chat.id
    t_id = getattr(c.message, "message_thread_id", None)
    d = c.data
    try:
        if d in ("sh_p1", "sh_p2"):
            bot.answer_callback_query(c.id)
            del_msg(c)
            return shop_send(chat_id, 1 if d == "sh_p1" else 2, t_id, uid)
        if d.startswith("sh_pg_"):
            bot.answer_callback_query(c.id)
            del_msg(c)
            return shop_send(chat_id, d[len("sh_pg_"):], t_id, uid)
        if d == "sh_vip":
            p, dsc = gold_price(VIP_ITEM_PRICE_GOLD)
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton(f"✅ Купить за {fmt_gold(p)} gold", callback_data="sh_vy"),
                   types.InlineKeyboardButton("⬅️ Назад", callback_data="sh_p1"))
            bot.answer_callback_query(c.id)
            return bot.send_message(chat_id, shop_vip_warning_text() + f"\n\n💰 Цена: <b>{fmt_gold(p)} gold</b>", parse_mode="HTML", reply_markup=kb, message_thread_id=t_id)
        if d == "sh_vy":
            p, _ = gold_price(VIP_ITEM_PRICE_GOLD)
            ok, msg = shop_buy_gold(uid, p, "vip", label="👑 VIP (предмет)")
            if ok:
                msg += "\nПредмет в 🎒 Инвентаре → откройте его и нажмите «Активировать»."
            return bot.answer_callback_query(c.id, msg[:190], show_alert=True)
        if d.startswith("sh_g_"):
            res = d.split("_")[2]
            bot.answer_callback_query(c.id)
            return adm_ask(uid if c.message.chat.type == "private" else chat_id,
                           f"✏️ Сколько {'ICE' if res == 'ice' else 'Jewel'} хотите купить за gold?", shop_gold_qty_step, res)
        if d.startswith("sh_gy_"):
            _, _, res, n = d.split("_")
            n = int(n)
            base = GOLD_PER_ICE_N if res == "ice" else GOLD_PER_JEWEL_N
            p, _ = gold_price(base * n)
            ok, msg = shop_buy_gold(uid, p, res, qty=n, label=f"{fmt_gold(n)} {'ICE' if res == 'ice' else 'Jewel'}")
            bot.answer_callback_query(c.id, msg[:190], show_alert=True)
            if ok:
                del_msg(c)
            return
        if d.startswith("sh_b_"):
            _, _, pid, i = d.split("_")
            i = int(i)
            pg = shop_pages.find_one({"_id": pid})
            if not pg or not pg.get("active", True) or i >= len(pg.get("items", [])):
                return bot.answer_callback_query(c.id, "❌ Товар недоступен", show_alert=True)
            it = pg["items"][i]
            p, _ = gold_price(int(it["price"]), pg.get("discount", 0))
            if it.get("stock") is not None:
                r = shop_pages.update_one({"_id": pid, f"items.{i}.stock": {"$gt": 0}}, {"$inc": {f"items.{i}.stock": -1}})
                if r.modified_count == 0:
                    return bot.answer_callback_query(c.id, "😔 Товар закончился", show_alert=True)
            ok, msg = shop_buy_gold(uid, p, it["kind"], qty=it.get("qty", 1), tpl_id=it.get("tpl"), ice=it.get("ice", 0), jewel=it.get("jewel", 0), label=it["label"])
            if not ok and it.get("stock") is not None:
                shop_pages.update_one({"_id": pid}, {"$inc": {f"items.{i}.stock": 1}})
            return bot.answer_callback_query(c.id, msg[:190], show_alert=True)
        if d in ("sh_cell_ice", "sh_cell_jewel"):
            res = d.split("_")[2]
            price = CELL_PRICE[res]
            r = users.update_one({"_id": uid, "balance": {"$gte": price}},
                                 {"$inc": {"balance": -price}, "$push": {"inventory": make_cell_item(res)}})
            if r.modified_count == 0:
                return bot.answer_callback_query(c.id, f"❌ Нужно {price} ICE", show_alert=True)
            return bot.answer_callback_query(c.id, "✅ Ячейка куплена! Она в 🎒 Инвентаре — активируйте или передайте.", show_alert=True)
        if d == "sh_cap":
            u = users.find_one({"_id": uid}) or {}
            if (u.get("capsule") or {}).get("owned"):
                return bot.answer_callback_query(c.id, "💊 Капсула у вас уже есть", show_alert=True)
            r = users.update_one({"_id": uid, "balance": {"$gte": CAPSULE_PRICE}},
                                 {"$inc": {"balance": -CAPSULE_PRICE}, "$set": {"capsule": {"owned": True, "level": 1, "last_farm": 0}}})
            if r.modified_count == 0:
                return bot.answer_callback_query(c.id, f"❌ Нужно {CAPSULE_PRICE} ICE", show_alert=True)
            return bot.answer_callback_query(c.id, "✅ Капсула куплена! Теперь Jewel фармятся вместе с ICE.", show_alert=True)
        if d.startswith("sh_s_"):
            _, _, res, n = d.split("_")
            try:
                stars_invoice(uid, res, int(n))
                return bot.answer_callback_query(c.id, "Счёт отправлен в личные сообщения с ботом")
            except Exception as e:
                logger.error(f"Ошибка отправки счёта: {e}")
                return bot.answer_callback_query(c.id, "❌ Не удалось создать счёт. Откройте бота в личке и нажмите /start", show_alert=True)
        if d.startswith("sh_c_"):
            if c.message.chat.type != "private":
                return bot.answer_callback_query(c.id, "Откройте магазин в личке с ботом — там можно ввести своё количество", show_alert=True)
            bot.answer_callback_query(c.id)
            return adm_ask(uid, "✏️ Сколько звёзд ⭐ хотите потратить? (1–10000)", shop_custom_step, d.split("_")[2])
        bot.answer_callback_query(c.id)
    except Exception as e:
        logger.error(f"Ошибка shop_callback ({d}): {e}")
        try:
            bot.answer_callback_query(c.id, "❌ Ошибка")
        except Exception:
            pass

# ---------- VIP как предмет инвентаря: активация ----------

VIP_DRAFT = {}
VIP_EMOJI_MAX = 8

def _looks_like_emoji(t):
    t = (t or "").strip()
    if not t or len(t) > VIP_EMOJI_MAX:
        return False
    return not any(ch.isalnum() for ch in t)

def vip_activate_screen(chat_id, uid, iid, thread_id=None):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("✅ Понятно, продолжить", callback_data=f"vp_go_{iid}"),
           types.InlineKeyboardButton("❌ Отмена", callback_data="vp_x"))
    bot.send_message(chat_id,
        "👑 <b>Активация VIP</b>\n\nСейчас вы выберете:\n"
        "1️⃣ <b>Эмодзи</b> — оно будет стоять рядом с вашим ником в профиле, топах и кланах.\n"
        "2️⃣ <b>Фон</b> (фото или гиф) — он показывается в вашем /profile вместе с VIP-статусом. "
        "Зачем: чтобы ваш профиль выделялся, а другие игроки сразу видели, кто поддерживает проект.\n\n"
        "⚠️ <b>Важно:</b> нельзя выбирать 18+ контент, порно, шок и оскорбления. Текст проверяет автоматический фильтр, "
        "а фон дополнительно уходит на проверку администрации. За нарушение — <b>бан без возможности возврата</b> (VIP и gold не возвращаются).\n\n"
        "После подтверждения предмет будет использован и статус выдастся автоматически.",
        parse_mode="HTML", reply_markup=kb, message_thread_id=thread_id)

def vip_step_emoji_new(m, iid):
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Активация отменена. Предмет остался в инвентаре.")
    t = (m.text or "").strip()
    if not _looks_like_emoji(t) or is_bad_text(t):
        return adm_ask(m.chat.id, f"❌ Нужен один-два эмодзи (без букв и цифр, до {VIP_EMOJI_MAX} знаков). Попробуйте ещё:", vip_step_emoji_new, iid)
    VIP_DRAFT[m.from_user.id] = {"iid": iid, "emoji": t}
    adm_ask(m.chat.id, "🖼 Теперь пришлите <b>фото или гиф</b> для фона профиля (или напишите /skip, чтобы без фона).\n"
                       "Помните: 18+ запрещено — бан без возврата.", vip_step_media_new, iid)

def vip_step_media_new(m, iid):
    uid = m.from_user.id
    if adm_cancelled(m):
        VIP_DRAFT.pop(uid, None)
        return bot.send_message(m.chat.id, "❌ Активация отменена. Предмет остался в инвентаре.")
    draft = VIP_DRAFT.get(uid)
    if not draft or draft["iid"] != iid:
        return bot.send_message(m.chat.id, "❌ Сессия активации устарела, откройте предмет заново.")
    fid, mt = None, None
    if (m.text or "").strip().lower() != "/skip":
        fid, mt = extract_media(m, allow_url=False)
        if not fid:
            return adm_ask(m.chat.id, "Пришлите фото/гиф или /skip:", vip_step_media_new, iid)
        if m.content_type == "photo" and getattr(m, "caption", None) and is_bad_text(m.caption):
            return adm_ask(m.chat.id, "❌ Подпись не прошла проверку. Пришлите без подписи:", vip_step_media_new, iid)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("✅ Активировать VIP", callback_data=f"vp_ok_{iid}"),
           types.InlineKeyboardButton("❌ Отмена", callback_data="vp_x"))
    draft.update(file_id=fid, media_type=mt)
    txt = f"👑 <b>Проверьте выбор</b>\n\nЭмодзи: {safe(draft['emoji'])}\nФон: {'выбран' if fid else 'нет'}\n\nПосле подтверждения изменить нельзя."
    bot.send_message(m.chat.id, txt, parse_mode="HTML", reply_markup=kb)

def vip_apply(uid, iid):
    draft = VIP_DRAFT.get(uid)
    if not draft or draft["iid"] != iid:
        return False, "Сессия устарела, откройте предмет заново."
    r = users.update_one({"_id": uid, "inventory": {"$elemMatch": {"iid": iid, "cosmetic": "vip_pass"}}}, {"$pull": {"inventory": {"iid": iid}}})
    if r.modified_count == 0:
        return False, "Предмет не найден в инвентаре."
    users.update_one({"_id": uid}, {"$set": {"is_vip": True, "vip_emoji": draft["emoji"], "vip_background": draft.get("file_id"),
                                              "vip_type": draft.get("media_type"), "vip_since": int(time.time())}})
    VIP_DRAFT.pop(uid, None)
    # на проверку админу
    try:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🚫 Бан игрока", callback_data=f"vp_b_{uid}"),
               types.InlineKeyboardButton("✅ Ок", callback_data="vp_ok_admin"))
        cap = f"👑 Новый VIP: <code>{uid}</code>\nЭмодзи: {safe(draft['emoji'])}\nПроверьте контент (18+?)."
        if draft.get("file_id"):
            send_media_safe(ADMIN_ID, draft.get("media_type") or "photo", draft["file_id"], cap, kb)
        else:
            bot.send_message(ADMIN_ID, cap, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.warning(f"VIP не отправлен админу: {e}")
    return True, "✅ VIP активирован! Эмодзи и фон уже в вашем профиле."

@bot.callback_query_handler(func=lambda c: c.data.startswith("vp_"))
def vip_callback(c):
    uid = c.from_user.id
    d = c.data
    try:
        if d == "vp_ok_admin":
            bot.answer_callback_query(c.id, "Ок")
            return del_msg(c)
        if d.startswith("vp_b_"):
            if uid != ADMIN_ID:
                return bot.answer_callback_query(c.id, "Только админ", show_alert=True)
            target = int(d[5:])
            set_banned(target, True)
            users.update_one({"_id": target}, {"$set": {"is_vip": False}, "$unset": {"vip_background": "", "vip_emoji": ""}})
            try:
                bot.send_message(target, "🚫 Вы заблокированы за нарушение правил (18+ контент в VIP). Возврат невозможен.")
            except Exception:
                pass
            bot.answer_callback_query(c.id, "🚫 Забанен, VIP снят")
            return del_msg(c)
        if d == "vp_x":
            VIP_DRAFT.pop(uid, None)
            bot.answer_callback_query(c.id, "Отменено")
            return del_msg(c)
        if d.startswith("vp_a_"):
            iid = d[5:]
            u = users.find_one({"_id": uid}) or {}
            if u.get("is_vip"):
                return bot.answer_callback_query(c.id, "👑 VIP уже активен", show_alert=True)
            if not any(x.get("iid") == iid and x.get("cosmetic") == "vip_pass" for x in u.get("inventory", [])):
                return bot.answer_callback_query(c.id, "Предмет не найден", show_alert=True)
            bot.answer_callback_query(c.id)
            return vip_activate_screen(c.message.chat.id, uid, iid, getattr(c.message, "message_thread_id", None))
        if d.startswith("vp_go_"):
            iid = d[6:]
            bot.answer_callback_query(c.id)
            del_msg(c)
            return adm_ask(uid, "😀 Отправьте <b>эмодзи</b> для профиля (например 🔥 или 👑):", vip_step_emoji_new, iid)
        if d.startswith("vp_ok_"):
            ok, msg = vip_apply(uid, d[6:])
            bot.answer_callback_query(c.id, msg, show_alert=True)
            if ok:
                del_msg(c)
            return
        bot.answer_callback_query(c.id)
    except Exception as e:
        logger.error(f"Ошибка vip_callback ({d}): {e}")
        try:
            bot.answer_callback_query(c.id, "❌ Ошибка")
        except Exception:
            pass


# ================================================================
# НОВОЕ v5: БОССЫ — мировые (админ настраивает) и клановые (приходят сами)
# ================================================================

BOSS_TIERS = ["Обычный", "Редкий", "Эпический", "Легендарный", "Ультра", "Мифический"]
BOSS_TIER_ICON = ["⚪️", "🔵", "🟣", "🟠", "🔴", "🌈"]
BOSS_RARITY = {"common": "🟢 Обычный", "rare": "🔵 Редкий", "ultra": "🟣 Ультра-редкий"}
BOSS_CFG_DEFAULT = {
    "world_recover_h": 5,        # восстановление отряда у мирового босса
    "world_life_days": 7,        # через сколько дней мировой босс уходит, если его не добили
    "clan_life_h": 24,           # сколько часов клановый босс стоит у клана
    "min_gap_days": 3.5,         # раз в 1–2 недели: интервал между появлениями — от 3.5 до 7 дней
    "max_gap_days": 7.0,
}

CLAN_BOSS_SEED = [
    {"_id": "skeletonwarrior", "name": "Skeleton Warrior", "rarity": "rare", "weight": 25, "hp": 6000, "dmg_min": 700, "dmg_max": 1000,
     "recover_h": 2, "media": "https://s15.gifyu.com/images/bWwrd.gif", "media_type": "animation",
     "reward_ice": 5000, "reward_jewel": 600, "xp": 600},
    {"_id": "khaladrogon", "name": "Khala Drogon", "rarity": "ultra", "weight": 5, "hp": 28000, "dmg_min": 1200, "dmg_max": 1600,
     "recover_h": 2, "media": "https://s15.gifyu.com/images/bWwrW.gif", "media_type": "animation",
     "reward_ice": 11000, "reward_jewel": 1200, "xp": 1500},
    {"_id": "darksidor", "name": "Dark Sidor", "rarity": "common", "weight": 70, "hp": 2500, "dmg_min": 300, "dmg_max": 500,
     "recover_h": 2, "media": "https://s15.gifyu.com/images/bWw57.gif", "media_type": "animation",
     "reward_ice": 1100, "reward_jewel": 80, "xp": 200},
]
WORLD_BOSS_SEED = {
    "_id": "frosttitan", "name": "Frost Titan", "rarity": "rare", "weight": 10, "hp": 3_000_000, "dmg_min": 600, "dmg_max": 900,
    "recover_h": 5, "media": None, "media_type": "photo", "part_ice": 200,
    "drops": [
        {"tier": 1, "kind": "ice", "amount": 1000, "chance": 90},
        {"tier": 2, "kind": "case", "case": "common", "amount": 2, "chance": 60},
        {"tier": 3, "kind": "jewel", "amount": 10, "chance": 35},
        {"tier": 4, "kind": "case", "case": "rare", "amount": 1, "chance": 12},
        {"tier": 5, "kind": "jewel", "amount": 100, "chance": 3},
        {"tier": 6, "kind": "case", "case": "epic", "amount": 1, "chance": 1},
    ],
}

def boss_cfg():
    d = settings.find_one({"_id": "boss_cfg"}) or {}
    return {k: d.get(k, v) for k, v in BOSS_CFG_DEFAULT.items()}

def seed_bosses():
    try:
        for t in CLAN_BOSS_SEED:
            boss_tpl.update_one({"_id": t["_id"]}, {"$setOnInsert": {**t, "scope": "clan", "active": True}}, upsert=True)
        w = dict(WORLD_BOSS_SEED)
        boss_tpl.update_one({"_id": w["_id"]}, {"$setOnInsert": {**w, "scope": "world", "active": True}}, upsert=True)
    except Exception as e:
        logger.error(f"Ошибка seed_bosses: {e}")

def reward_describe(kind, amount, case=None, item=None):
    if kind == "ice":
        return f"{fmt(amount)} ICE"
    if kind == "jewel":
        return f"{fmt(amount)} {SNOW_EMOJI}"
    if kind == "case":
        return f"{amount}× кейс «{case}»"
    if kind == "item":
        tpl = catalog.find_one({"_id": item}) if item else None
        return item_plain(tpl) if tpl else "предмет"
    return "награда"

def boss_grant(uid, drop):
    k = drop["kind"]
    if k == "ice":
        users.update_one({"_id": uid}, {"$inc": {"balance": float(drop["amount"])}})
    elif k == "jewel":
        users.update_one({"_id": uid}, {"$inc": {"snow_jewels": float(drop["amount"])}})
    elif k == "case":
        grant_case(uid, drop.get("case", "common"), int(drop["amount"]))
    elif k == "item":
        tpl = catalog.find_one({"_id": drop.get("item")})
        if tpl:
            give_item(uid, tpl)

def boss_squad(u):
    """Отряд игрока: до 5 карт. Возвращает (список карт, суммарное HP, средняя защита %)."""
    cards = (u.get("cards") or [])[:5]
    hp, defs = 0, []
    for cd in cards:
        _, h, _, df, _ = card_stats(cd)
        hp += int(h)
        defs.append(float(df))
    return cards, hp, (sum(defs) / len(defs) if defs else 0.0)

def boss_squad_damage(cards):
    tot = 0
    for cd in cards:
        _, _, dmg, _, rng = card_stats(cd)
        tot += dmg if rng is None else random.randint(max(0, dmg - rng), dmg + rng)
    return int(tot)

def boss_bar(cur, total, n=14):
    f = 0 if total <= 0 else int(round(n * max(0, cur) / total))
    return "█" * f + "░" * (n - f)

def boss_spawn(tpl, scope, cid=None):
    cfg = boss_cfg()
    now = int(time.time())
    life = int(cfg["world_life_days"]) * 86400 if scope == "world" else int(float(cfg["clan_life_h"]) * 3600)
    iid = uuid.uuid4().hex[:10]
    boss_inst.insert_one({
        "_id": iid, "tpl": tpl["_id"], "scope": scope, "cid": cid, "name": tpl["name"], "rarity": tpl.get("rarity", "common"),
        "media": tpl.get("media"), "media_type": tpl.get("media_type", "photo"),
        "hp_max": int(tpl["hp"]), "hp": int(tpl["hp"]), "dmg_min": int(tpl["dmg_min"]), "dmg_max": int(tpl["dmg_max"]),
        "recover_h": float(tpl.get("recover_h", 5 if scope == "world" else 2)),
        "spawn": now, "end_ts": now + life, "status": "active", "dmg": {}, "pool": {}, "rec": {},
    })
    return iid

def boss_pick_tpl(scope):
    docs = list(boss_tpl.find({"scope": scope, "active": True}))
    if not docs:
        return None
    return random.choices(docs, weights=[max(1, int(d.get("weight", 1))) for d in docs], k=1)[0]

def boss_active(scope, cid=None):
    q = {"scope": scope, "status": "active"}
    if cid:
        q["cid"] = cid
    return boss_inst.find_one(q, sort=[("spawn", -1)])

def boss_top(inst, n=5):
    items = sorted(((int(k), int(v)) for k, v in (inst.get("dmg") or {}).items()), key=lambda x: -x[1])
    return items[:n], items

def boss_screen(chat_id, uid, inst, thread_id=None, notice=""):
    u = users.find_one({"_id": uid}) or {}
    cards, mx, _ = boss_squad(u)
    now = int(time.time())
    top, allp = boss_top(inst)
    names = user_names([x[0] for x in top])
    my = int((inst.get("dmg") or {}).get(str(uid), 0))
    pool = (inst.get("pool") or {}).get(str(uid), mx)
    rec = int((inst.get("rec") or {}).get(str(uid), 0))
    lines = []
    if notice:
        lines += [notice, ""]
    lines += [f"👹 <b>{safe(inst['name'])}</b> · {BOSS_RARITY.get(inst.get('rarity'), '')}",
              f"❤️ {fmt(max(0, inst['hp']))} / {fmt(inst['hp_max'])}",
              f"<code>{boss_bar(inst['hp'], inst['hp_max'])}</code>",
              f"⏳ Уйдёт через: <b>{fmt_left(inst['end_ts'] - now)}</b>", ""]
    if not cards:
        lines.append("🃏 У вас нет карточек — откройте кейс в 🎁 Кейсы.")
    elif rec > now:
        lines.append(f"😴 Ваш отряд восстанавливается: ещё <b>{fmt_left(rec - now)}</b>")
    else:
        if rec and pool <= 0:
            pool = mx
        lines.append(f"🛡 Здоровье отряда: <b>{fmt(pool)}/{fmt(mx)}</b>")
    lines.append(f"💥 Ваш урон: <b>{fmt(my)}</b>")
    if top:
        lines += ["", "🏅 <b>Топ-5 по урону:</b>"]
        for i, (tu, tv) in enumerate(top, 1):
            lines.append(f"{i}. {safe(names.get(tu, tu))} — {fmt(tv)}")
    lines += ["", "<i>Чем больше урона вы нанесёте, тем выше шанс награды. Сами шансы скрыты — решает рандом.</i>"]
    kb = types.InlineKeyboardMarkup(row_width=2)
    if cards and rec <= now:
        kb.add(types.InlineKeyboardButton("⚔️ Атаковать", callback_data=f"bs_a_{inst['_id']}"))
    kb.add(types.InlineKeyboardButton("🔄 Обновить", callback_data=f"bs_o_{inst['_id']}"))
    if inst["scope"] == "world":
        kb.add(types.InlineKeyboardButton("📜 Прошлый босс", callback_data="bs_l"))
    text = "\n".join(lines)
    if inst.get("media"):
        return send_media_safe(chat_id, inst.get("media_type", "photo"), inst["media"], text[:1020], kb, thread_id)
    return bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb, message_thread_id=thread_id)

def boss_attack(inst_id, uid):
    """Возвращает (ok, текст, финал_boss_id или None)."""
    inst = boss_inst.find_one({"_id": inst_id})
    now = int(time.time())
    if not inst or inst["status"] != "active" or now >= inst["end_ts"]:
        return False, "⌛ Босс уже ушёл или повержен.", None
    u = users.find_one({"_id": uid}) or {}
    if inst["scope"] == "clan":
        cl = get_user_clan(uid)
        if not cl or cl["_id"] != inst.get("cid"):
            return False, "❌ Это босс другого клана.", None
    cards, mx, avgdef = boss_squad(u)
    if not cards:
        return False, "❌ Нет карточек для боя.", None
    k = str(uid)
    rec = int((inst.get("rec") or {}).get(k, 0))
    if rec > now:
        return False, f"😴 Отряд восстанавливается ещё {fmt_left(rec - now)}.", None
    has_pool = k in (inst.get("pool") or {})
    pool = int(inst["pool"][k]) if has_pool else mx
    if pool <= 0:
        pool = mx
    dmg_p = boss_squad_damage(cards)
    dmg_b = int(random.randint(inst["dmg_min"], inst["dmg_max"]) * (1 - min(avgdef, 80) / 100))
    new_pool = max(0, pool - dmg_b)
    cond = {"_id": inst_id, "status": "active"}
    if has_pool:
        cond[f"pool.{k}"] = int(inst["pool"][k])
    else:
        cond[f"pool.{k}"] = {"$exists": False}
    sets = {f"pool.{k}": new_pool}
    if new_pool <= 0:
        sets[f"rec.{k}"] = now + int(inst["recover_h"] * 3600)
    after = boss_inst.find_one_and_update(cond, {"$set": sets, "$inc": {"hp": -dmg_p, f"dmg.{k}": dmg_p}}, return_document=ReturnDocument.AFTER)
    if not after:
        return False, "⏳ Попробуйте ещё раз.", None
    txt = f"💥 Вы нанесли <b>{fmt(dmg_p)}</b> урона!\n🩸 Босс ударил по отряду: <b>-{fmt(dmg_b)}</b>\n🛡 Отряд: {fmt(new_pool)}/{fmt(mx)}"
    if new_pool <= 0:
        txt += f"\n😴 Отряд обессилел — восстановление {fmt_left(inst['recover_h'] * 3600)}."
    final = None
    if after["hp"] <= 0:
        r = boss_inst.update_one({"_id": inst_id, "status": "active"}, {"$set": {"status": "dead", "die_ts": now, "killed_by": uid}})
        if r.modified_count:
            final = inst_id
    return True, txt, final

def boss_finalize(inst_id):
    inst = boss_inst.find_one({"_id": inst_id})
    if not inst:
        return
    if inst["scope"] == "clan":
        return boss_finalize_clan(inst)
    tpl = boss_tpl.find_one({"_id": inst["tpl"]}) or {}
    dmgs = {int(k): int(v) for k, v in (inst.get("dmg") or {}).items() if int(v) > 0}
    total = sum(dmgs.values()) or 1
    top_val = max(dmgs.values()) if dmgs else 1
    winners = []
    for uid, dv in dmgs.items():
        share = dv / total
        got = []
        part = int(tpl.get("part_ice", 0) or 0)
        if part:
            bonus = float(part) * (1 + share * 10)
            users.update_one({"_id": uid}, {"$inc": {"balance": bonus}})
            got.append(f"{fmt(bonus)} ICE (за участие)")
        factor = 0.15 + 0.85 * ((dv / top_val) ** 0.5)       # чем больше урон — тем выше шанс, но всё равно рандом
        for dr in tpl.get("drops", []):
            if random.random() * 100 < float(dr["chance"]) * factor:
                try:
                    boss_grant(uid, dr)
                    got.append(f"{BOSS_TIER_ICON[min(5, max(0, dr['tier'] - 1))]} {reward_describe(dr['kind'], dr['amount'], dr.get('case'), dr.get('item'))}")
                except Exception as e:
                    logger.error(f"Ошибка выдачи дропа: {e}")
        winners.append({"uid": uid, "dmg": dv, "got": got})
    boss_inst.update_one({"_id": inst_id}, {"$set": {"winners": winners}})
    names = user_names([w["uid"] for w in winners])
    for w in winners:
        try:
            bot.send_message(w["uid"], f"🏆 <b>Мировой босс {safe(inst['name'])} повержен!</b>\n💥 Ваш урон: {fmt(w['dmg'])}\n🎁 Награды:\n"
                             + ("\n".join("• " + safe(x) for x in w["got"]) or "• ничего не выпало, повезёт в следующий раз"), parse_mode="HTML")
        except Exception:
            pass
        time.sleep(0.05)

def boss_finalize_clan(inst):
    cl = clans.find_one({"_id": inst.get("cid")})
    tpl = boss_tpl.find_one({"_id": inst["tpl"]}) or {}
    if not cl:
        return
    ice, jw, xp = float(tpl.get("reward_ice", 0)), float(tpl.get("reward_jewel", 0)), int(tpl.get("xp", 0))
    clans.update_one({"_id": cl["_id"]}, {"$inc": {"treasury.ice": ice, "treasury.jewel": jw}})
    clan_add_xp(cl["_id"], xp)
    top, _ = boss_top(inst, 3)
    names = user_names([x[0] for x in top])
    lines = [f"🏆 <b>Клан «{safe(cl['name'])}» победил босса {safe(inst['name'])}!</b>", "",
             f"💰 В казну: <b>{fmt(ice)} ICE</b> и <b>{fmt(jw)}</b> {SNOW_EMOJI}", f"⭐ Опыт клана: <b>+{xp} XP</b>", "", "🏅 Лучшие:"]
    for i, (tu, tv) in enumerate(top, 1):
        lines.append(f"{i}. {safe(names.get(tu, tu))} — {fmt(tv)}")
    clan_notify(cl["_id"], "\n".join(lines))

def boss_last_text():
    d = boss_inst.find_one({"scope": "world", "status": {"$in": ["dead", "fled"]}}, sort=[("spawn", -1)])
    if not d:
        return "📜 Пока ни один мировой босс не завершал бой."
    if d["status"] == "fled":
        return f"📜 <b>{safe(d['name'])}</b> ушёл {fmt_dt(d['end_ts'])} — его не успели победить."
    names = user_names([w["uid"] for w in d.get("winners", [])][:20])
    lines = [f"📜 <b>Последний мировой босс: {safe(d['name'])}</b>", f"☠️ Побеждён: {fmt_dt(d.get('die_ts', d['end_ts']))}", ""]
    for w in sorted(d.get("winners", []), key=lambda x: -x["dmg"])[:10]:
        got = ", ".join(w["got"]) if w["got"] else "—"
        lines.append(f"• {safe(names.get(w['uid'], w['uid']))} ({fmt(w['dmg'])}): {safe(got)}")
    return "\n".join(lines)[:4000]

def boss_world_open(chat_id, uid, thread_id=None):
    inst = boss_active("world")
    if not inst:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("📜 Прошлый босс", callback_data="bs_l"))
        return bot.send_message(chat_id, "👹 <b>Мировой босс</b>\n\nСейчас мирового босса нет. Загляните позже — он может появиться в любой момент!",
                                parse_mode="HTML", reply_markup=kb, message_thread_id=thread_id)
    return boss_screen(chat_id, uid, inst, thread_id)

@bot.message_handler(commands=["boss"])
@bot.message_handler(func=lambda m: m.text == "👹 Боссы")
def boss_cmd(m):
    try:
        get_user(m.from_user.id, m.from_user.username, m.from_user.first_name)
        boss_world_open(m.chat.id, m.from_user.id, getattr(m, "message_thread_id", None))
    except Exception as e:
        logger.error(f"Ошибка boss_cmd: {e}")

@bot.callback_query_handler(func=lambda c: c.data.startswith("bs_"))
def boss_callback(c):
    uid, chat_id, d = c.from_user.id, c.message.chat.id, c.data
    t_id = getattr(c.message, "message_thread_id", None)
    try:
        if d == "bs_l":
            bot.answer_callback_query(c.id)
            return bot.send_message(chat_id, boss_last_text(), parse_mode="HTML", message_thread_id=t_id)
        if d.startswith("bs_o_"):
            inst = boss_inst.find_one({"_id": d[5:]})
            bot.answer_callback_query(c.id)
            if not inst or inst["status"] != "active":
                return bot.send_message(chat_id, "⌛ Этот босс уже ушёл или побеждён.", message_thread_id=t_id)
            del_msg(c)
            return boss_screen(chat_id, uid, inst, t_id)
        if d.startswith("bs_a_"):
            ok, txt, final = boss_attack(d[5:], uid)
            if not ok:
                return bot.answer_callback_query(c.id, re.sub(r"<[^>]+>", "", txt)[:190], show_alert=True)
            bot.answer_callback_query(c.id)
            del_msg(c)
            inst = boss_inst.find_one({"_id": d[5:]})
            if final:
                bot.send_message(chat_id, txt + f"\n\n☠️ <b>{safe(inst['name'])} повержен!</b>", parse_mode="HTML", message_thread_id=t_id)
                return boss_finalize(final)
            return boss_screen(chat_id, uid, inst, t_id, notice=txt)
        bot.answer_callback_query(c.id)
    except Exception as e:
        logger.error(f"Ошибка boss_callback ({d}): {e}")
        try:
            bot.answer_callback_query(c.id, "❌ Ошибка")
        except Exception:
            pass

# ---------- расписание ----------

def boss_tick():
    cfg = boss_cfg()
    now = int(time.time())
    sched = settings.find_one({"_id": "boss_sched"}) or {}
    # мировой: по времени, заданному админом, либо случайно раз в 3.5–7 дней
    if not boss_active("world"):
        t = boss_tpl.find_one({"scope": "world", "active": True, "spawn_at": {"$lte": now, "$gt": 0}})
        if t:
            boss_tpl.update_one({"_id": t["_id"]}, {"$set": {"spawn_at": 0}})
            iid = boss_spawn(t, "world")
            boss_announce_world(iid)
        else:
            nxt = int(sched.get("world_next", 0) or 0)
            if nxt == 0:
                settings.update_one({"_id": "boss_sched"}, {"$set": {"world_next": now + int(random.uniform(1, 3) * 86400)}}, upsert=True)
            elif now >= nxt and settings.find_one_and_update({"_id": "boss_sched", "world_next": nxt}, {"$set": {"world_next": now + int(random.uniform(cfg["min_gap_days"], cfg["max_gap_days"]) * 86400)}}):
                t = boss_pick_tpl("world")
                if t and not boss_active("world"):
                    boss_announce_world(boss_spawn(t, "world"))
    # уходят, кого не добили
    for b in list(boss_inst.find({"status": "active", "end_ts": {"$lte": now}})):
        if boss_inst.update_one({"_id": b["_id"], "status": "active"}, {"$set": {"status": "fled"}}).modified_count and b["scope"] == "clan":
            clan_notify(b["cid"], f"💨 Босс <b>{safe(b['name'])}</b> ушёл — клан не успел его победить.")
    # клановые
    for cl in clans.find({}, {"_id": 1, "members": 1, "boss_next": 1, "name": 1}):
        nxt = int(cl.get("boss_next", 0) or 0)
        if nxt == 0:
            clans.update_one({"_id": cl["_id"], "boss_next": {"$exists": False}}, {"$set": {"boss_next": now + int(random.uniform(0.3, 4) * 86400)}})
            continue
        if now < nxt or len(cl.get("members", [])) < 2 or boss_active("clan", cl["_id"]):
            continue
        if not clans.update_one({"_id": cl["_id"], "boss_next": nxt}, {"$set": {"boss_next": now + int(random.uniform(cfg["min_gap_days"], cfg["max_gap_days"]) * 86400)}}).modified_count:
            continue
        t = boss_pick_tpl("clan")
        if t:
            boss_spawn(t, "clan", cl["_id"])
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("👹 К боссу", callback_data="cx_b"))
            clan_notify(cl["_id"], f"👹 <b>К вашему клану явился босс: {safe(t['name'])}!</b>\n{BOSS_RARITY.get(t.get('rarity'), '')} · ❤️ {fmt(t['hp'])}\n"
                                   f"Он уйдёт через {cfg['clan_life_h']} ч. Победите его вместе — награда пойдёт в казну!", kb)

def boss_announce_world(iid):
    inst = boss_inst.find_one({"_id": iid})
    if not inst:
        return
    text = f"👹 <b>Появился мировой босс: {safe(inst['name'])}!</b>\n❤️ {fmt(inst['hp_max'])}\nЗаходите в «👹 Боссы» и бейте вместе!"
    for d in users.find({"banned": {"$ne": True}}, {"_id": 1}).limit(5000):
        try:
            bot.send_message(d["_id"], text, parse_mode="HTML")
        except Exception:
            pass
        time.sleep(0.04)

def war_reset_stuck():
    """Если бот перезапустили посреди боя, ячейка атаки (att=2) застревает — возвращаем её игроку."""
    now = int(time.time())
    live = {(f["bid"], f["p"]["uid"]) for f in WAR_FIGHTS.values()}
    for b in clan_battles.find({"status": "active"}):
        for side in ("ra", "rb"):
            for x in b.get(side, []):
                if x.get("att") == 2 and (b["_id"], x["uid"]) not in live and now - int((b.get("t2") or {}).get(str(x["uid"]), 0)) > 300:
                    clan_battles.update_one({"_id": b["_id"], side: {"$elemMatch": {"uid": x["uid"], "att": 2}}}, {"$set": {f"{side}.$.att": 1}})

def extra_ticks():
    for fn in (war_finish_due, boss_tick, war_reset_stuck):
        try:
            fn()
        except Exception as e:
            logger.error(f"Ошибка {fn.__name__}: {e}")

# ---------- босс клана ----------

def clan_boss_screen(chat_id, uid, clan, thread_id=None):
    inst = boss_active("clan", clan["_id"])
    if not inst:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="cl_home"))
        return bot.send_message(chat_id, "👹 <b>Босс клана</b>\n\nСейчас босса нет. Он приходит в случайный момент — 1–2 раза в неделю. "
                                         "Сильные боссы встречаются реже!", parse_mode="HTML", reply_markup=kb, message_thread_id=thread_id)
    return boss_screen(chat_id, uid, inst, thread_id)

# ================================================================
# АДМИН: БОССЫ (/bossadmin)
# ================================================================

BD_FIELDS = {
    "name": ("📛 Название", str), "hp": ("❤️ HP", int), "dmg_min": ("⚔️ Урон от", int), "dmg_max": ("⚔️ Урон до", int),
    "recover_h": ("😴 Восстановление, ч", float), "weight": ("🎲 Вес появления", int),
    "part_ice": ("🎁 ICE за участие (мировой)", int), "reward_ice": ("💰 ICE в казну (клан)", int),
    "reward_jewel": ("💎 Jewel в казну (клан)", int), "xp": ("⭐ XP клану", int),
}

def bd_home(chat_id):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for t in boss_tpl.find().sort([("scope", 1), ("name", 1)]):
        kb.add(types.InlineKeyboardButton(f"{'🌍' if t['scope'] == 'world' else '🏰'} {t['name']} · {fmt(t['hp'])} HP{'' if t.get('active', True) else ' (выкл.)'}"[:60], callback_data=f"bd_v_{t['_id']}"))
    kb.add(types.InlineKeyboardButton("➕ Новый мировой босс", callback_data="bd_new_world"),
           types.InlineKeyboardButton("➕ Новый клановый", callback_data="bd_new_clan"))
    kb.add(types.InlineKeyboardButton("📊 Активные боссы", callback_data="bd_act"))
    bot.send_message(chat_id, "👹 <b>Боссы — админ-панель</b>\n\nМировой босс: название, картинка/гиф, HP, дропы по уровням (1–6, до мифического), время появления.",
                     parse_mode="HTML", reply_markup=kb)

@bot.message_handler(commands=["bossadmin"])
def bossadmin_cmd(m):
    if m.from_user.id == ADMIN_ID:
        bd_home(m.chat.id)

def bd_view(chat_id, tid):
    t = boss_tpl.find_one({"_id": tid})
    if not t:
        return bot.send_message(chat_id, "❌ Босс не найден.")
    lines = [f"{'🌍 Мировой' if t['scope'] == 'world' else '🏰 Клановый'} босс <b>{safe(t['name'])}</b> ({'вкл.' if t.get('active', True) else 'выкл.'})",
             f"Редкость: {BOSS_RARITY.get(t.get('rarity'), '—')} · вес {t.get('weight', 1)}",
             f"❤️ {fmt(t['hp'])} · ⚔️ {t['dmg_min']}–{t['dmg_max']} · 😴 {t.get('recover_h')} ч",
             f"Медиа: {'есть' if t.get('media') else 'нет'}"]
    if t["scope"] == "world":
        lines.append(f"🎁 ICE за участие: {t.get('part_ice', 0)}")
        sa = int(t.get("spawn_at", 0) or 0)
        lines.append(f"🕒 Появление: {fmt_dt(sa) if sa else 'не задано (случайно раз в 3.5–7 дн.)'}")
        lines.append("\n<b>Дропы</b> (шанс — для топ-игрока, остальным ниже):")
        for i, dr in enumerate(t.get("drops", [])):
            lines.append(f"{i + 1}. {BOSS_TIER_ICON[dr['tier'] - 1]} ур.{dr['tier']} {BOSS_TIERS[dr['tier'] - 1]}: {reward_describe(dr['kind'], dr['amount'], dr.get('case'), dr.get('item'))} — {dr['chance']}%")
    else:
        lines.append(f"💰 Казна: {fmt(t.get('reward_ice', 0))} ICE + {fmt(t.get('reward_jewel', 0))} Jewel · ⭐ {t.get('xp', 0)} XP")
    kb = types.InlineKeyboardMarkup(row_width=2)
    fields = [f for f in BD_FIELDS if (t["scope"] == "world" and f not in ("reward_ice", "reward_jewel", "xp")) or (t["scope"] == "clan" and f != "part_ice")]
    kb.add(*[types.InlineKeyboardButton(BD_FIELDS[f][0], callback_data=f"bd_e_{tid}_{f}") for f in fields])
    kb.add(types.InlineKeyboardButton("🎭 Редкость", callback_data=f"bd_r_{tid}"), types.InlineKeyboardButton("🖼 Картинка/гиф", callback_data=f"bd_m_{tid}"))
    if t["scope"] == "world":
        kb.add(types.InlineKeyboardButton("➕ Дроп", callback_data=f"bd_da_{tid}"), types.InlineKeyboardButton("🗑 Убрать дроп", callback_data=f"bd_dd_{tid}"))
        kb.add(types.InlineKeyboardButton("🕒 Время появления", callback_data=f"bd_t_{tid}"))
    kb.add(types.InlineKeyboardButton("🚀 Призвать сейчас", callback_data=f"bd_s_{tid}"),
           types.InlineKeyboardButton("⏯ Вкл/выкл", callback_data=f"bd_tg_{tid}"))
    kb.add(types.InlineKeyboardButton("🗑 Удалить босса", callback_data=f"bd_x_{tid}"), types.InlineKeyboardButton("⬅️ Назад", callback_data="bd_home"))
    bot.send_message(chat_id, "\n".join(lines), parse_mode="HTML", reply_markup=kb)

def bd_step_new(m, scope):
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Отменено.")
    name = (m.text or "").strip()[:40]
    if not name:
        return adm_ask(m.chat.id, "Название босса:", bd_step_new, scope)
    tid = uuid.uuid4().hex[:8]
    base = {"_id": tid, "name": name, "scope": scope, "rarity": "common", "weight": 10, "hp": 10000, "dmg_min": 300, "dmg_max": 500,
            "recover_h": 5 if scope == "world" else 2, "media": None, "media_type": "photo", "active": True}
    if scope == "world":
        base.update(part_ice=0, drops=[])
    else:
        base.update(reward_ice=1000, reward_jewel=50, xp=200)
    boss_tpl.insert_one(base)
    bd_view(m.chat.id, tid)

def bd_step_field(m, tid, field):
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Отменено.")
    typ = BD_FIELDS[field][1]
    try:
        v = typ((m.text or "").strip().replace(",", ".")) if typ is not str else (m.text or "").strip()[:40]
        if typ is not str and v < 0:
            raise ValueError
        if typ is str and not v:
            raise ValueError
    except Exception:
        return adm_ask(m.chat.id, "❌ Некорректно, введите ещё раз:", bd_step_field, tid, field)
    boss_tpl.update_one({"_id": tid}, {"$set": {field: v}})
    bd_view(m.chat.id, tid)

def bd_step_media(m, tid):
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Отменено.")
    fid, mt = extract_media(m)
    if not fid:
        return adm_ask(m.chat.id, "Пришлите фото/гиф или прямую ссылку на картинку/гиф:", bd_step_media, tid)
    boss_tpl.update_one({"_id": tid}, {"$set": {"media": fid, "media_type": mt}})
    bd_view(m.chat.id, tid)

def bd_step_time(m, tid):
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Отменено.")
    t = (m.text or "").strip()
    try:
        if t == "0":
            ts = 0
        else:
            dt = datetime.strptime(t, "%d.%m.%Y %H:%M")
            ts = int((dt - timedelta(hours=TZ_OFFSET_HOURS) - datetime(1970, 1, 1)).total_seconds())
    except Exception:
        return adm_ask(m.chat.id, "Формат: <code>25.10.2026 18:30</code> (ваше время) или 0 — сбросить:", bd_step_time, tid)
    boss_tpl.update_one({"_id": tid}, {"$set": {"spawn_at": ts}})
    bd_view(m.chat.id, tid)

def bd_step_drop(m, tid, st):
    """Мастер добавления дропа: tier -> kind -> amount -> chance."""
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Отменено.")
    t = (m.text or "").strip()
    step = st["step"]
    try:
        if step == "tier":
            st["tier"] = int(t)
            assert 1 <= st["tier"] <= 6
            st["step"] = "kind"
            return adm_ask(m.chat.id, "Тип: <code>ice</code>, <code>jewel</code>, <code>case</code> (кейс) или <code>item</code> (предмет из /items)", bd_step_drop, tid, st)
        if step == "kind":
            assert t in ("ice", "jewel", "case", "item")
            st["kind"] = t
            st["step"] = "extra" if t in ("case", "item") else "amount"
            if t == "case":
                return adm_ask(m.chat.id, "Тип кейса: common / rare / epic", bd_step_drop, tid, st)
            if t == "item":
                return adm_ask(m.chat.id, "ID шаблона предмета (из /items):", bd_step_drop, tid, st)
            return adm_ask(m.chat.id, "Количество:", bd_step_drop, tid, st)
        if step == "extra":
            if st["kind"] == "case":
                assert t in CASE_TYPES
                st["case"] = t
            else:
                assert catalog.find_one({"_id": t})
                st["item"] = t
            st["step"] = "amount"
            return adm_ask(m.chat.id, "Количество:", bd_step_drop, tid, st)
        if step == "amount":
            st["amount"] = float(t.replace(",", ".")) if st["kind"] in ("ice", "jewel") else int(t)
            assert st["amount"] > 0
            st["step"] = "chance"
            return adm_ask(m.chat.id, "Шанс для лучшего игрока, % (0.1–100). Остальным он считается ниже по урону:", bd_step_drop, tid, st)
        if step == "chance":
            ch = float(t.replace(",", "."))
            assert 0 < ch <= 100
            dr = {"tier": st["tier"], "kind": st["kind"], "amount": st["amount"], "chance": ch}
            if "case" in st:
                dr["case"] = st["case"]
            if "item" in st:
                dr["item"] = st["item"]
            boss_tpl.update_one({"_id": tid}, {"$push": {"drops": dr}})
            return bd_view(m.chat.id, tid)
    except Exception:
        return adm_ask(m.chat.id, "❌ Не подошло, повторите ввод:", bd_step_drop, tid, st)

def bd_step_spawn_clan(m, tid):
    if adm_cancelled(m):
        return bot.send_message(m.chat.id, "❌ Отменено.")
    cl = ct_find_clan((m.text or "").strip())
    if not cl:
        return adm_ask(m.chat.id, "❌ Клан не найден. Название или ID:", bd_step_spawn_clan, tid)
    t = boss_tpl.find_one({"_id": tid})
    if boss_active("clan", cl["_id"]):
        return bot.send_message(m.chat.id, "❌ У этого клана уже есть босс.")
    boss_spawn(t, "clan", cl["_id"])
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("👹 К боссу", callback_data="cx_b"))
    clan_notify(cl["_id"], f"👹 <b>К вашему клану явился босс: {safe(t['name'])}!</b>", kb)
    bot.send_message(m.chat.id, f"✅ Босс призван в клан «{safe(cl['name'])}».", parse_mode="HTML")

@bot.callback_query_handler(func=lambda c: c.data.startswith("bd_"))
def bd_callback(c):
    if c.from_user.id != ADMIN_ID:
        return bot.answer_callback_query(c.id, "Только админ", show_alert=True)
    chat_id, d = c.message.chat.id, c.data
    try:
        bot.answer_callback_query(c.id)
        if d == "bd_home":
            return bd_home(chat_id)
        if d in ("bd_new_world", "bd_new_clan"):
            return adm_ask(chat_id, "Название босса:", bd_step_new, d.split("_")[2])
        if d == "bd_act":
            lines = ["📊 <b>Активные боссы</b>"]
            for b in boss_inst.find({"status": "active"}):
                lines.append(f"• {'🌍' if b['scope'] == 'world' else '🏰'} {safe(b['name'])}: {fmt(b['hp'])}/{fmt(b['hp_max'])} HP, уйдёт {fmt_dt(b['end_ts'])}")
            kb = types.InlineKeyboardMarkup(row_width=1)
            for b in boss_inst.find({"status": "active", "scope": "world"}):
                kb.add(types.InlineKeyboardButton(f"❌ Убрать «{b['name']}»"[:50], callback_data=f"bd_k_{b['_id']}"))
            return bot.send_message(chat_id, "\n".join(lines) if len(lines) > 1 else "Активных боссов нет.", parse_mode="HTML", reply_markup=kb)
        if d.startswith("bd_k_"):
            boss_inst.update_one({"_id": d[5:], "status": "active"}, {"$set": {"status": "fled"}})
            return bot.send_message(chat_id, "✅ Босс убран.")
        if d.startswith("bd_v_"):
            return bd_view(chat_id, d[5:])
        if d.startswith("bd_e_"):
            _, _, tid, field = d.split("_", 3)
            return adm_ask(chat_id, f"{BD_FIELDS[field][0]} — новое значение:", bd_step_field, tid, field)
        if d.startswith("bd_r_"):
            tid = d[5:]
            kb = types.InlineKeyboardMarkup(row_width=1)
            for k, v in BOSS_RARITY.items():
                kb.add(types.InlineKeyboardButton(v, callback_data=f"bd_rs_{tid}_{k}"))
            return bot.send_message(chat_id, "Редкость босса (влияет на частоту появления вместе с весом):", reply_markup=kb)
        if d.startswith("bd_rs_"):
            _, _, tid, k = d.split("_")
            boss_tpl.update_one({"_id": tid}, {"$set": {"rarity": k}})
            return bd_view(chat_id, tid)
        if d.startswith("bd_m_"):
            return adm_ask(chat_id, "Пришлите фото/гиф или прямую ссылку:", bd_step_media, d[5:])
        if d.startswith("bd_t_"):
            return adm_ask(chat_id, f"Когда появится босс? Формат <code>25.10.2026 18:30</code> (часовой пояс UTC{TZ_OFFSET_HOURS:+g}) или 0:", bd_step_time, d[5:])
        if d.startswith("bd_da_"):
            return adm_ask(chat_id, "Уровень дропа 1–6 (1 обычный … 5 ультра, 6 мифический):", bd_step_drop, d[6:], {"step": "tier"})
        if d.startswith("bd_dd_"):
            tid = d[6:]
            t = boss_tpl.find_one({"_id": tid}) or {}
            kb = types.InlineKeyboardMarkup(row_width=1)
            for i, dr in enumerate(t.get("drops", [])):
                kb.add(types.InlineKeyboardButton(f"🗑 ур.{dr['tier']} {reward_describe(dr['kind'], dr['amount'], dr.get('case'), dr.get('item'))}"[:50], callback_data=f"bd_dx_{tid}_{i}"))
            return bot.send_message(chat_id, "Какой дроп убрать?", reply_markup=kb)
        if d.startswith("bd_dx_"):
            _, _, tid, i = d.split("_")
            t = boss_tpl.find_one({"_id": tid}) or {}
            dr = t.get("drops", [])
            if int(i) < len(dr):
                dr.pop(int(i))
                boss_tpl.update_one({"_id": tid}, {"$set": {"drops": dr}})
            return bd_view(chat_id, tid)
        if d.startswith("bd_s_"):
            tid = d[5:]
            t = boss_tpl.find_one({"_id": tid})
            if t["scope"] == "world":
                if boss_active("world"):
                    return bot.send_message(chat_id, "❌ Мировой босс уже идёт.")
                boss_announce_world(boss_spawn(t, "world"))
                return bot.send_message(chat_id, "✅ Мировой босс призван, игроки оповещены.")
            return adm_ask(chat_id, "В какой клан призвать? Название или ID:", bd_step_spawn_clan, tid)
        if d.startswith("bd_tg_"):
            t = boss_tpl.find_one({"_id": d[6:]}) or {}
            boss_tpl.update_one({"_id": d[6:]}, {"$set": {"active": not t.get("active", True)}})
            return bd_view(chat_id, d[6:])
        if d.startswith("bd_x_"):
            boss_tpl.delete_one({"_id": d[5:]})
            bot.send_message(chat_id, "🗑 Удалён.")
            return bd_home(chat_id)
    except Exception as e:
        logger.error(f"Ошибка bd_callback ({d}): {e}")

seed_bosses()


seed_clan_stuff()
if not os.environ.get("DISABLE_TOURNAMENT_LOOP"):
    threading.Thread(target=_tournament_loop, daemon=True).start()


# ---------- UNKNOWN ----------

@bot.message_handler(func=lambda m: True)
def unknown_command(m):
    if m.chat.type != 'private': return
    bot.reply_to(m, "❓ Неизвестная команда. Используйте меню или /start")


# ── Верификация Telegram initData ──────────────────────────────
def verify_telegram_init_data(init_data: str, bot_token: str) -> dict | None:
    try:
        from urllib.parse import unquote
        init_data = unquote(init_data)
        
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )

        secret_key = hmac.new(
            b"WebAppData",
            bot_token.encode("utf-8"),
            hashlib.sha256
        ).digest()

        expected_hash = hmac.new(
            secret_key,
            data_check_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected_hash, received_hash):
            return None

        return json.loads(parsed.get("user", "{}"))

    except Exception as e:
        logger.error(f"verify error: {e}")
        return None

# ── CORS — разрешаем запросы из Mini App ──────────────────────
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Init-Data"
    return response
 
@app.route("/api/<path:path>", methods=["OPTIONS"])
def options_handler(path):
    return "", 204
 
 
# ── Хелпер: достать uid из initData ───────────────────────────
def get_uid_from_request():
    init_data = request.headers.get("X-Init-Data", "")
    
    logger.info(f"Init data received: {init_data[:50] if init_data else 'EMPTY'}")
    
    if not init_data:
        return None, jsonify({"error": "No init data"}), 401

    from urllib.parse import unquote
    init_data = unquote(init_data)
    
    tg_user = verify_telegram_init_data(init_data, TOKEN)
    logger.info(f"TG user result: {tg_user}")
    
    if not tg_user:
        return None, jsonify({"error": "Invalid init data", "debug": init_data[:100]}), 403

    return tg_user.get("id"), None, None
 
 
# ================================================================
# GET /api/user — профиль игрока
# ================================================================
@app.route("/api/user", methods=["GET"])
def api_get_user():
    try:
        uid, err_response, err_code = get_uid_from_request()
        if err_response:
            return err_response, err_code
 
        u = users.find_one({"_id": uid})
        if not u:
            return jsonify({"error": "User not found"}), 404
 
        now = int(time.time())
        price_doc = settings.find_one({"_id": "ice_price"})
        current_price = price_doc["value"] if price_doc else "?"
 
        # Считаем статус фарма
        last_farm = u.get("farm", 0)
        farm_ready = (now - last_farm) >= FARM_CD
        farm_wait_sec = max(0, FARM_CD - (now - last_farm))
 
        return jsonify({
            "uid":          u["_id"],
            "username":     u.get("username", ""),
            "first_name":   u.get("first_name", ""),
            "balance":      round(float(u.get("balance", 0)), 2),
            "level":        u.get("level", 1),
            "wins":         u.get("wins", 0),
            "rp":           u.get("rp", 0),
            "total_burned": round(float(u.get("total_burned", 0)), 2),
            "farm_ready":   farm_ready,
            "farm_wait_sec": farm_wait_sec,
            "is_vip":       u.get("is_vip", False),
            "ice_price":    current_price,
        })
 
    except Exception as e:
        logger.error(f"api_get_user error: {e}")
        return jsonify({"error": "Server error"}), 500
 
 
# ================================================================
# POST /api/farm — выполнить фарм
# ================================================================
@app.route("/api/farm", methods=["POST"])
def api_farm():
    try:
        uid, err_response, err_code = get_uid_from_request()
        if err_response:
            return err_response, err_code
 
        u = users.find_one({"_id": uid})
        if not u:
            return jsonify({"error": "User not found"}), 404
 
        now = int(time.time())
        last_farm = u.get("farm", 0)
 
        if (now - last_farm) < FARM_CD:
            wait = FARM_CD - (now - last_farm)
            return jsonify({
                "success": False,
                "error": "cooldown",
                "wait_sec": wait
            }), 429
 
        gain = farm_amount(u["level"])
        if u.get("is_vip", False):
            gain += 0.5
 
        new_balance = round(float(u.get("balance", 0)) + gain, 2)
 
        users.update_one(
            {"_id": uid},
            {"$set": {"farm": now, "balance": new_balance}}
        )
 
        return jsonify({
            "success":     True,
            "gained":      gain,
            "new_balance": new_balance,
            "level":       u["level"],
            "is_vip":      u.get("is_vip", False),
        })
 
    except Exception as e:
        logger.error(f"api_farm error: {e}")
        return jsonify({"error": "Server error"}), 500
 
 
# ================================================================
# POST /api/burn — сжечь ICE
# ================================================================
@app.route("/api/burn", methods=["POST"])
def api_burn():
    try:
        uid, err_response, err_code = get_uid_from_request()
        if err_response:
            return err_response, err_code
 
        data = request.get_json(force=True)
        amount = float(data.get("amount", 0))
 
        if amount < 1:
            return jsonify({"error": "Min burn is 1 ICE"}), 400
 
        u = users.find_one({"_id": uid})
        if not u:
            return jsonify({"error": "User not found"}), 404
 
        balance = float(u.get("balance", 0))
        if balance < amount:
            return jsonify({"error": "Insufficient balance"}), 400
 
        new_balance = round(balance - amount, 2)
        new_burned  = round(float(u.get("total_burned", 0)) + amount, 2)
 
        # Определяем ранг сжигания
        _, burn_emoji = get_burn_rank(new_burned)
 
        users.update_one(
            {"_id": uid},
            {"$set": {
                "balance":      new_balance,
                "total_burned": new_burned,
                "burn_emoji":   burn_emoji
            }}
        )
 
        rank_name, _ = get_burn_rank(new_burned)
        pass_add_burn(uid, amount)   # НОВОЕ v3: прогресс сезонного пасса
 
        return jsonify({
            "success":      True,
            "burned":       amount,
            "new_balance":  new_balance,
            "total_burned": new_burned,
            "rank":         rank_name,
        })
 
    except Exception as e:
        logger.error(f"api_burn error: {e}")
        return jsonify({"error": "Server error"}), 500
 
 
# ================================================================
# POST /api/game — результат игры (dice / slots / crash / flip)
# ================================================================
@app.route("/api/game", methods=["POST"])
def api_game():
    try:
        uid, err_response, err_code = get_uid_from_request()
        if err_response:
            return err_response, err_code
 
        data    = request.get_json(force=True)
        game    = data.get("game")       # "dice" | "slots" | "crash" | "flip"
        bet     = float(data.get("bet", 0))
        won     = bool(data.get("won", False))
        payout  = float(data.get("payout", 0))  # итоговая выплата (уже посчитана на клиенте)
 
        if bet <= 0:
            return jsonify({"error": "Invalid bet"}), 400
 
        u = users.find_one({"_id": uid})
        if not u:
            return jsonify({"error": "User not found"}), 404
 
        balance = float(u.get("balance", 0))
 
        # Для списания — если клиент ещё не снял (зависит от игры)
        # Здесь логика: клиент шлёт ставку и финальную выплату
        # Итог = payout - bet (может быть отрицательным)
        delta = round(payout - bet, 2)
        new_balance = round(balance + delta, 2)
 
        # Защита от отрицательного баланса
        if new_balance < 0:
            return jsonify({"error": "Insufficient balance"}), 400
 
        update_fields = {"balance": new_balance}
 
        if won and game == "dice":
            update_fields["wins"]  = u.get("wins", 0) + 1
            update_fields["rp"]    = max(0, u.get("rp", 0) + RP_WIN)
        elif not won and game == "dice":
            update_fields["rp"]    = max(0, u.get("rp", 0) + RP_LOSS)
 
        users.update_one({"_id": uid}, {"$set": update_fields})
 
        return jsonify({
            "success":     True,
            "new_balance": new_balance,
            "delta":       delta,
            "wins":        update_fields.get("wins", u.get("wins", 0)),
            "rp":          update_fields.get("rp", u.get("rp", 0)),
        })
 
    except Exception as e:
        logger.error(f"api_game error: {e}")
        return jsonify({"error": "Server error"}), 500
 
 
# ================================================================
# GET /api/top?field=balance — таблица лидеров
# ================================================================
@app.route("/api/top", methods=["GET"])
def api_top():
    try:
        field = request.args.get("field", "balance")
        if field not in ("balance", "level", "wins", "rp"):
            field = "balance"
 
        top = list(
            users.find(
                {},
                {"_id": 1, "username": 1, "first_name": 1,
                 "balance": 1, "level": 1, "wins": 1, "rp": 1}
            ).sort(field, -1).limit(10)
        )
 
        result = []
        for u in top:
            result.append({
                "uid":        u["_id"],
                "name":       u.get("first_name") or u.get("username") or f"User_{u['_id']}",
                "username":   u.get("username", ""),
                "balance":    round(float(u.get("balance", 0)), 2),
                "level":      u.get("level", 1),
                "wins":       u.get("wins", 0),
                "rp":         u.get("rp", 0),
            })
 
        return jsonify({"success": True, "top": result, "field": field})
 
    except Exception as e:
        logger.error(f"api_top error: {e}")
        return jsonify({"error": "Server error"}), 500


PIXEL_CD = 600  # 10 минут
 
 
# ================================================================
# GET /api/pixels — все пиксели на карте
# ================================================================
@app.route("/api/pixels", methods=["GET"])
def api_get_pixels():
    try:
        all_pixels = list(pixels.find(
            {},
            {"_id": 0, "x": 1, "y": 1, "color": 1,
             "username": 1, "first_name": 1, "placed_at": 1}
        ))
        return jsonify({"success": True, "pixels": all_pixels})
    except Exception as e:
        logger.error(f"api_get_pixels error: {e}")
        return jsonify({"error": "Server error"}), 500
 
 
# ================================================================
# GET /api/pixel/cooldown — кулдаун текущего игрока
# ================================================================
@app.route("/api/pixel/cooldown", methods=["GET"])
def api_pixel_cooldown():
    try:
        uid, err_response, err_code = get_uid_from_request()
        if err_response:
            return err_response, err_code
 
        u = users.find_one({"_id": uid}, {"pixel_ts": 1})
        if not u:
            return jsonify({"wait_sec": 0})
 
        now = int(time.time())
        last_pixel = u.get("pixel_ts", 0)
        wait = max(0, PIXEL_CD - (now - last_pixel))
 
        return jsonify({"success": True, "wait_sec": wait})
 
    except Exception as e:
        logger.error(f"api_pixel_cooldown error: {e}")
        return jsonify({"error": "Server error"}), 500
 
 
@app.route("/api/pixel", methods=["POST"])
def api_place_pixel():
    try:
        uid, err_response, err_code = get_uid_from_request()
        if err_response:
            return err_response, err_code
 
        data  = request.get_json(force=True)
        x     = int(data.get("x", -1))
        y     = int(data.get("y", -1))
        color = str(data.get("color", "#ffffff")).strip()
 
        # Валидация координат
        if not (0 <= x < 500 and 0 <= y < 500):
            return jsonify({"error": "Invalid coordinates"}), 400
 
        # Валидация цвета — должен быть HEX
        import re as _re
        if not _re.match(r'^#[0-9a-fA-F]{6}$', color):
            return jsonify({"error": "Invalid color"}), 400
 
        # Проверка кулдауна
        u = users.find_one({"_id": uid})
        if not u:
            return jsonify({"error": "User not found"}), 404
 
        now = int(time.time())
        last_pixel = u.get("pixel_ts", 0)
        wait = PIXEL_CD - (now - last_pixel)
 
        if wait > 0:
            return jsonify({
                "error": f"Cooldown! Wait {wait} sec",
                "wait_sec": wait
            }), 429
 
        username   = u.get("username", "")
        first_name = u.get("first_name", "")
 
        # Сохраняем/обновляем пиксель (upsert)
        pixels.update_one(
            {"x": x, "y": y},
            {"$set": {
                "x":          x,
                "y":          y,
                "color":      color,
                "uid":        uid,
                "username":   username,
                "first_name": first_name,
                "placed_at":  now,
            }},
            upsert=True
        )
 
        # Обновляем timestamp последнего пикселя у пользователя
        users.update_one({"_id": uid}, {"$set": {"pixel_ts": now}})
 
        return jsonify({
            "success":    True,
            "x":          x,
            "y":          y,
            "color":      color,
            "username":   username,
            "first_name": first_name,
            "placed_at":  now,
        })
 
    except Exception as e:
        logger.error(f"api_place_pixel error: {e}")
        return jsonify({"error": "Server error"}), 500
 
# ---------- RUN ----------
if __name__ == "__main__":
    logger.info("Запуск ICECOIN...")
    if WEBHOOK and "http" in WEBHOOK:
        try:
            bot.remove_webhook()
            time.sleep(1)
            bot.set_webhook(url=f"{WEBHOOK}/{TOKEN}")
            port = int(os.environ.get("PORT", 10000))
            app.run(host="0.0.0.0", port=port)
        except Exception as e:
            logger.error(f"Ошибка: {e}")
            port = int(os.environ.get("PORT", 10000))
            app.run(host="0.0.0.0", port=port)
    else:
        bot.remove_webhook()
        bot.infinity_polling()
