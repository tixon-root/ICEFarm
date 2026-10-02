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

FARM_VIP_GIF = "https://i.ibb.co/N2pfn858/PIXELIAN.gif"   # гиф фарма только для VIP
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

def card_image_path(card):
    """Абсолютный путь к картинке карточки в папке Cards."""
    if card["kind"] == "class":
        # S-тир (Mage/Dist/Melle) — одна картинка на все уровни
        name = CLASS_DATA[card["key"]]["name"]
        filename = f"{name}.png"
    else:
        mob = CARD_DATA[card["key"]]
        base_name = MOB_IMAGE_NAME.get(card["key"], mob["name"].replace(" ", "_"))
        lvl_idx = min(card["level"] - 1, len(mob["levels"]) - 1)
        suffix = ["", "_2", "_3"][lvl_idx]
        ext = IMAGE_EXT_OVERRIDE.get((base_name, suffix), "png")
        filename = f"{base_name}{suffix}.{ext}"
    return os.path.join(CARDS_DIR, filename)

def send_card_photo(chat_id, card, caption, reply_markup=None, message_thread_id=None):
    """Отправляет фото карточки с подписью. Если файл не найден — отправляет обычным текстом (fallback)."""
    path = card_image_path(card)
    try:
        if os.path.isfile(path):
            with open(path, "rb") as photo:
                return bot.send_photo(
                    chat_id, photo, caption=caption, parse_mode="HTML",
                    reply_markup=reply_markup, message_thread_id=message_thread_id
                )
        else:
            logger.error(f"Картинка карты не найдена: {path}")
    except Exception as e:
        logger.error(f"Ошибка отправки фото карты ({path}): {e}")
    return bot.send_message(
        chat_id, caption, parse_mode="HTML",
        reply_markup=reply_markup, message_thread_id=message_thread_id
    )

def open_case_for_user(uid, case_type):
    """Открывает 1 кейс типа case_type для юзера, если есть место и сам кейс. Возвращает (ok, msg, card|None)."""
    u = users.find_one({"_id": uid})
    if not u:
        return False, "❌ Пользователь не найден.", None
    if u.get("cases", {}).get(case_type, 0) <= 0:
        return False, "❌ У вас нет таких кейсов.", None
    cards = u.get("cards", [])
    if len(cards) >= CARD_MAX_STORAGE:
        return False, "❌ Нет места в хранилище (Character). Освободите слот (передайте или разберите карту).", None

    kind, key = roll_case(case_type)
    card = create_card_instance(kind, key)

    res = users.update_one(
        {"_id": uid, f"cases.{case_type}": {"$gt": 0}},
        {"$inc": {f"cases.{case_type}": -1}, "$push": {"cards": card}}
    )
    if res.modified_count == 0:
        return False, "❌ Не удалось открыть кейс, попробуйте ещё раз.", None

    caption = f"🎉 Из кейса выпала карточка!\n\n{card_caption(card)}"
    return True, caption, card

CARD_MAX_STORAGE = 3      # макс. карт в Character
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
    kb.add("💊 Капсула")                 # НОВОЕ
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

        # ---------- 1) ICE ФАРМ ----------
        ice_lines = []
        if ice_ready:
            base_gain = farm_amount(u["level"])
            gain = base_gain

            item_bonus_pct, _ = get_pick_bonus_pct(inv)
            if item_bonus_pct:
                gain += round(base_gain * item_bonus_pct, 2)

            is_vip = u.get("is_vip", False)
            if is_vip:
                vip_bonus = round(base_gain * VIP_FARM_BONUS_PCT, 2)
                gain += vip_bonus

            found_lines, bonus_ice = roll_farm_find(u["_id"], inv)
            gain = round(gain + bonus_ice, 2)

            final_balance = round(final_balance + gain, 2)

            users.update_one(
                {"_id": u["_id"]},
                {"$set": {"farm": now, "balance": final_balance}, "$inc": {"farm_count": 1}}
            )
            check_achievements(u["_id"])

            ice_lines.append(f"┃ Добыто: <code>{fmt(gain)} ICE</code>")
            if is_vip:
                ice_lines.append(f"┃ ✨ VIP Бонус: <code>+{fmt(vip_bonus)}</code>")
            for fl in found_lines:
                ice_lines.append(f"┃ {fl}")
            ice_lines.append(f"┃ 💰 Баланс: <code>{fmt(final_balance)} ICE</code>")
        else:
            wait = effective_cd - time_passed
            hours, minutes = wait // 3600, (wait % 3600) // 60
            ice_lines.append(f"┃ ⏳ Через {hours}ч {minutes}м")

        blocks.append("❄️ <b>Фарм ICE</b>\n" + "\n".join(ice_lines))

        # ---------- 2) SNOW JEWEL ФАРМ (капсула) ----------
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
                    {"$inc": {"snow_jewels": yield_amount}, "$set": {"capsule.last_farm": now}}
                )
                check_achievements(u["_id"])
                jewel_lines.append(f"┃ Добыто: <code>{yield_amount} {SNOW_EMOJI}</code>")
            else:
                h, mnt = cap_left // 3600, (cap_left % 3600) // 60
                jewel_lines.append(f"┃ ⏳ Через {h}ч {mnt}м")

        blocks.append(f"{SNOW_EMOJI} <b>Фарм Jewel</b>\n" + "\n".join(jewel_lines))

        text = "\n\n".join(blocks)
        is_vip = u.get("is_vip", False)

        if is_vip:
            try:
                bot.send_animation(m.chat.id, FARM_VIP_GIF, caption=text, parse_mode="HTML", message_thread_id=t_id)
                return
            except Exception as e:
                logger.warning(f"Не удалось отправить VIP-гиф фарма ({e}), отправляю обычным сообщением")

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
        new_farm_amount = farm_amount(new_level)

        users.update_one({"_id": u["_id"]}, {"$set": {"balance": new_balance, "level": new_level}})
        check_achievements(u["_id"])

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

            if nft.get("type") == "item":
                # НОВОЕ: кирки и прочие текстовые предметы (без file_id/фото)
                bot.send_message(c.message.chat.id, text, parse_mode="HTML", reply_markup=kb, message_thread_id=t_id)
            elif nft["type"] == "photo":
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
