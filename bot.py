import os
import re
import logging
import calendar
import uuid
import io
import csv
import aiohttp
import pytz
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, date
from telegram_bot_calendar import DetailedTelegramCalendar
import db
import sheets
from db import save_contacts, get_user_contacts

from telegram import (
    Update,
    InputFile,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)


class _RedactBotTokenFilter(logging.Filter):
    """Redact Telegram bot tokens from log messages."""

    _token_pattern = re.compile(r"bot\d+:[A-Za-z0-9_-]+")
    _db_url_pattern = re.compile(r"postgres(?:ql)?://[^\s]+", re.IGNORECASE)
    _creds_pattern = re.compile(r"\"private_key\"\s*:\s*\".*?\"", re.DOTALL)
    _creds_json_pattern = re.compile(r"GOOGLE_CREDENTIALS_JSON[^\s]*", re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            msg = record.msg
            msg = self._token_pattern.sub("bot<redacted>", msg)
            msg = self._db_url_pattern.sub("postgres://<redacted>", msg)
            msg = self._creds_pattern.sub('"private_key":"<redacted>"', msg)
            msg = self._creds_json_pattern.sub("GOOGLE_CREDENTIALS_JSON=<redacted>", msg)
            record.msg = msg
        return True


logging.getLogger().addFilter(_RedactBotTokenFilter())
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

START, DEPARTMENT, QUESTION, CUSTOM_INPUT, CROP_TYPE, CONFIRM, EDIT, DATE_TYPE, DATE_CALENDAR, DATE_PERIOD_END, LOAD_TEMPLATE, TEMPLATE_SELECT, SAVE_TEMPLATE_NAME, SAVE_TEMPLATE_CONFIRM, DELETE_TEMPLATE_CONFIRM, CITY_SEARCH_LOAD, CITY_SELECT_LOAD, CITY_SEARCH_UNLOAD, CITY_SELECT_UNLOAD, UNLOAD_DISTRIBUTION, CHILD_EDIT_MENU, CHILD_EDIT_CITY, CHILD_EDIT_VOLUME, FIND_REQUEST_EDIT, SUMMARY_DATE_CALENDAR = range(25)

THREAD_IDS = {
    "Тваринництво": 2,
    "Виробництво": 4,
}

CROP_TYPES = ["Кукурудза", "Пшениця", "Соя", "Ріпак", "Соняшник"]

LIQUID_BULK_CARGO = {"КАС", "РКД", "АМ вода"}

CAL_PREFIX = "CAL"
SUMMARY_CAL_PREFIX = "SUMCAL"
MONTH_NAMES_UK = [
    "Січень",
    "Лютий",
    "Березень",
    "Квітень",
    "Травень",
    "Червень",
    "Липень",
    "Серпень",
    "Вересень",
    "Жовтень",
    "Листопад",
    "Грудень",
]
WEEKDAYS_UK = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]

QUESTIONS: List[Dict[str, Any]] = [
    {
        "key": "vehicle_type",
        "label": "Тип авто",
        "prompt": "Тип авто:",
        "options": ["ТРАЛ", "Зерновоз", "Самоскид", "Цистерна", "Тент", "Інше"],
    },
    {
        "key": "initiator",
        "label": "Ініціатор заявки (ПІБ)",
        "prompt": "Ініціатор заявки (ПІБ):",
        "options": None,
    },
    {
        "key": "company",
        "label": "Підприємство",
        "prompt": "Підприємство:",
        "options": ["Зернопродукт", "Агрокряж", "Інше"],
    },
    {
        "key": "cargo_type",
        "label": "Вид вантажу",
        "prompt": "Вид вантажу:",
        "options": ["Зерно", "Насіння", "АМ вода", "КАС", "РКД", "Інше"],
    },
    {
        "key": "size_type",
        "label": "Габарит / негабарит",
        "prompt": "Габарит / негабарит:",
        "options": ["Габарит", "Негабарит", "Насип", "Рідкі", "Біг-бег"],
    },
    {
        "key": "big_bag_weight",
        "label": "Вага 1 біг-бегу",
        "prompt": "Вага 1 біг-бегу в кг (тільки число):",
        "options": None,
        "only_for": "Біг-бег",
    },
    {
        "key": "volume",
        "label": "Кількість / Обсяг",
        "prompt": "Обсяг (залежить від типу):",
        "options": None,
        "conditional": True,
    },
    {
        "key": "notes",
        "label": "Примітки",
        "prompt": "Примітки (можна пропустити):",
        "options": ["Пропустити"],
    },
    {
        "key": "date_period",
        "label": "Дата / період перевезення",
        "prompt": "Дата / період перевезення:",
        "options": None,
    },
    {
        "key": "load_city",
        "label": "Населений пункт завантаження",
        "prompt": "Населений пункт завантаження:",
        "options": None,
        "use_city_search": True,
    },
    {
        "key": "load_place",
        "label": "Склад завантаження (якщо відомо)",
        "prompt": "Склад завантаження (якщо відомо):",
        "options": ["Пропустити"],
    },
    {
        "key": "load_method",
        "label": "Спосіб завантаження",
        "prompt": "Спосіб завантаження:",
        "options": ["Пропустити"],
    },
    {
        "key": "load_contact",
        "label": "Контакт на завантаженні",
        "prompt": "Контакт на завантаженні (приклад: Іванов Іван 0000000000):",
        "options": ["Пропустити"],
    },
    {
        "key": "unload_city",
        "label": "Населений пункт розвантаження",
        "prompt": "Населений пункт розвантаження:",
        "options": None,
        "use_city_search": True,
    },
    {
        "key": "unload_place",
        "label": "Склад розвантаження (якщо відомо)",
        "prompt": "Склад розвантаження (якщо відомо):",
        "options": ["Пропустити"],
    },
    {
        "key": "unload_method",
        "label": "Спосіб розвантаження",
        "prompt": "Спосіб розвантаження:",
        "options": ["Пропустити"],
    },
    {
        "key": "unload_contact",
        "label": "Контакт на розвантаженні",
        "prompt": "Контакт на розвантаженні (приклад: Іванов Іван 0000000000):",
        "options": ["Пропустити"],
    },
]


def _chat_type(update: Update) -> Optional[str]:
    chat = getattr(update, "effective_chat", None)
    if chat and getattr(chat, "type", None):
        return chat.type
    message = getattr(update, "message", None)
    if message and getattr(message, "chat", None):
        return message.chat.type
    return None


def _is_private_chat(update: Update) -> bool:
    return _chat_type(update) == "private"


def _is_admin_user(user) -> bool:
    """Check if current user is configured as bot admin."""
    if not user:
        return False

    admin_ids_raw = (os.getenv("ADMIN_USER_IDS") or "").strip()
    admin_ids: set = set()
    if admin_ids_raw:
        for token in re.split(r"[,;\s]+", admin_ids_raw):
            token = token.strip()
            if not token:
                continue
            try:
                admin_ids.add(int(token))
            except ValueError:
                continue

    # backward compatibility: existing single ADMIN_USER_ID
    legacy_admin_id = (os.getenv("ADMIN_USER_ID") or "").strip()
    if legacy_admin_id:
        try:
            admin_ids.add(int(legacy_admin_id))
        except ValueError:
            pass

    if admin_ids and int(getattr(user, "id", 0)) in admin_ids:
        return True

    admin_usernames_raw = (os.getenv("ADMIN_USERNAMES") or "").strip()
    if admin_usernames_raw:
        admin_usernames = {
            item.strip().lstrip("@").lower()
            for item in re.split(r"[,;\s]+", admin_usernames_raw)
            if item.strip()
        }
        username = (getattr(user, "username", "") or "").strip().lstrip("@").lower()
        if username and username in admin_usernames:
            return True

    return False


async def _send_private_only_notice(update: Update) -> None:
    message = getattr(update, "message", None)
    if not message:
        return

    await message.reply_text(
        "🚫 Створення заявки доступне лише в приватному чаті з ботом.",
        reply_markup=ReplyKeyboardRemove(),
    )

    bot_username = (os.getenv("BOT_USERNAME") or "").strip().lstrip("@")
    if bot_username:
        deep_link = f"https://t.me/{bot_username}?start=apply"
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(text="📝 Відкрити бота в приваті", url=deep_link)]]
        )
        await message.reply_text("Натисніть кнопку нижче, щоб створити заявку в приваті:", reply_markup=keyboard)


def _get_volume_prompt(size_type: str) -> str:
    """Отримати правильний prompt для питання про вагу/обсяг залежно від типу розміру"""
    if size_type == "Біг-бег":
        return "Кількість біг-бегів (тільки число):"
    elif size_type in ["Насип", "Рідкі"]:
        return "Кількість тон (тільки число):"
    else:  # Габарит, Негабарит
        return "Вага в тонах (тільки число):"


def _format_volume_with_unit(size_type: str, volume: str) -> str:
    """Форматувати відповідь вага/обсяг з правильною одиницею"""
    if not volume:
        return volume
    if size_type == "Біг-бег":
        return f"{volume} шт"
    elif size_type in ["Насип", "Рідкі"]:
        return f"{volume} т"
    else:  # Габарит, Негабарит
        return f"{volume} т"


async def search_cities_novaposhta(query: str) -> List[Dict[str, str]]:
    """Пошук населених пунктів через API Нової Пошти"""
    api_key = os.getenv("NOVAPOSHTA_API_KEY")
    if not api_key:
        logging.error("NOVAPOSHTA_API_KEY не встановлено")
        return []
    
    url = "https://api.novaposhta.ua/v2.0/json/"
    payload = {
        "apiKey": api_key,
        "modelName": "Address",
        "calledMethod": "searchSettlements",
        "methodProperties": {
            "CityName": query,
            "Limit": "10"
        }
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as response:
                data = await response.json()
                
                if not data.get("success"):
                    return []
                
                addresses = data.get("data", [{}])[0].get("Addresses", [])
                results = []
                
                for addr in addresses:
                    # Формуємо назву: "Місто (Район, Область)"
                    present = addr.get("Present", "")
                    area = addr.get("Area", "")
                    region = addr.get("Region", "")
                    
                    if area and region:
                        display = f"{present} ({area}, {region})"
                    elif region:
                        display = f"{present} ({region})"
                    else:
                        display = present
                    
                    results.append({
                        "display": display,
                        "value": present
                    })
                
                return results[:10]
    except Exception as e:
        logging.error(f"Error searching cities: {e}")
        return []


def _get_question(index: int) -> Dict[str, Any]:
    return QUESTIONS[index]


def _normalize_cargo_type(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if text.lower().startswith("культура"):
        return "Культура"
    return text


def _split_location_values(value: Optional[str]) -> List[str]:
    if not value:
        return []
    text = str(value).strip()
    if not text or text == "—":
        return []
    return [part.strip() for part in text.split(";") if part.strip()]


def _append_location_value(existing: Optional[str], new_value: str) -> str:
    locations = _split_location_values(existing)
    candidate = (new_value or "").strip()
    if candidate and candidate not in locations:
        locations.append(candidate)
    return "; ".join(locations) if locations else "—"


def _pop_last_location_value(existing: Optional[str]) -> Tuple[str, Optional[str]]:
    locations = _split_location_values(existing)
    if not locations:
        return "—", None
    removed = locations.pop()
    return ("; ".join(locations) if locations else "—"), removed


def _build_location_actions_keyboard(include_skip: bool = False) -> ReplyKeyboardMarkup:
    buttons = [
        [KeyboardButton(text="➕ Додати ще пункт")],
        [KeyboardButton(text="➖ Видалити останній пункт")],
        [KeyboardButton(text="✅ Готово")],
    ]
    if include_skip:
        buttons.append([KeyboardButton(text="Пропустити")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except Exception:
        return None


def _format_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _format_volume_for_size(size_type: str, value: float) -> str:
    unit = "шт" if size_type == "Біг-бег" else "т"
    return f"{_format_number(value)} {unit}"


def _build_distribution_actions_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(text="✏️ Змінити загальний обсяг")],
            [KeyboardButton(text="🔁 Переввести розподіл")],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def _resolve_parent_request_id(request_id: str) -> str:
    rid = (request_id or "").strip().upper()
    if not rid:
        return rid
    if "-" not in rid:
        return rid
    head, tail = rid.rsplit("-", 1)
    return head if tail.isdigit() else rid


def _parse_child_request_id(request_id: str) -> Optional[Tuple[str, int]]:
    rid = (request_id or "").strip().upper()
    if "-" not in rid:
        return None
    head, tail = rid.rsplit("-", 1)
    if not tail.isdigit():
        return None
    return head, int(tail)


def _build_child_items_from_data(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    unload_cities = _split_location_values(data.get("unload_city"))
    distribution = data.get("unload_distribution") if isinstance(data.get("unload_distribution"), dict) else {}
    ids_map = data.get("unload_distribution_ids") if isinstance(data.get("unload_distribution_ids"), dict) else {}
    parent_id = (data.get("request_id") or "").strip().upper()
    items: List[Dict[str, Any]] = []
    for idx, city in enumerate(unload_cities, start=1):
        amount = _to_float(distribution.get(city))
        if amount is None:
            amount = 0.0
        child_id = str(ids_map.get(city) or "").strip().upper()
        if not child_id:
            child_id = f"{parent_id}-{idx:02d}" if parent_id else f"-{idx:02d}"
        items.append({
            "index": idx,
            "city": city,
            "amount": amount,
            "child_id": child_id,
        })
    return items


def _rebuild_distribution_from_items(items: List[Dict[str, Any]]) -> Tuple[str, Dict[str, float], Dict[str, str], float]:
    unload_cities: List[str] = []
    distribution: Dict[str, float] = {}
    ids_map: Dict[str, str] = {}
    total = 0.0
    for item in items:
        city = str(item.get("city", "")).strip()
        if not city:
            continue
        amount = float(item.get("amount", 0.0) or 0.0)
        child_id = str(item.get("child_id", "")).strip().upper()
        unload_cities.append(city)
        distribution[city] = amount
        if child_id:
            ids_map[city] = child_id
        total += amount
    return "; ".join(unload_cities) if unload_cities else "—", distribution, ids_map, total


def _build_request_display_entries(requests: list) -> list:
    entries = []
    for req in requests:
        rid = (req.get("request_id") or "").strip().upper()
        data = req.get("request_data") or {}
        if not isinstance(data, dict):
            data = {}

        child_items = _build_child_items_from_data(data)
        if rid and len(child_items) > 1:
            added_split_entries = False
            for item in child_items:
                city = item.get("city")
                city_amount = item.get("amount")
                child_id = item.get("child_id")
                if not city:
                    continue

                child_data = dict(data)
                child_data["unload_city"] = city
                child_data["volume"] = _format_number(float(city_amount or 0.0))

                child_entry = dict(req)
                child_entry["parent_request_id"] = rid
                child_entry["display_request_id"] = child_id or rid
                child_entry["request_data"] = child_data
                child_entry["is_split_part"] = True
                entries.append(child_entry)
                added_split_entries = True

            if added_split_entries:
                continue

        normal_entry = dict(req)
        normal_entry["parent_request_id"] = rid
        normal_entry["display_request_id"] = rid or "—"
        normal_entry["is_split_part"] = False
        entries.append(normal_entry)

    return entries


def _should_skip_question(question_key: str, data: Dict[str, Any]) -> bool:
    # Пропускати big_bag_weight для всіх розмірів окрім Біг-бегу
    if question_key == "big_bag_weight" and data.get("size_type") != "Біг-бег":
        return True
    
    # У швидкій заявці пропускати деякі поля
    if data.get("quick_mode"):
        # Поля які пропускати в швидкій режимі
        quick_mode_skip = {
            "load_place",         # Склад завантаження
            "load_method",        # Спосіб завантаження
            "unload_place",       # Склад розвантаження
            "unload_method",      # Спосіб розвантаження
            "load_contact",       # Контакт на завантаженні
            "unload_contact",     # Контакт на розвантаженні
            "notes",              # Примітки
            "company",            # Підприємство (встановлюється автоматично)
        }
        # size_type і big_bag_weight НЕ пропускаються - це важлива інформація
        if question_key in quick_mode_skip:
            return True
    
    cargo_type = _normalize_cargo_type(data.get("cargo_type"))
    if cargo_type in LIQUID_BULK_CARGO and question_key in {"load_method", "unload_method"}:
        return True

    # Якщо обрано декілька НП розвантаження, склад розвантаження пропускаємо.
    if question_key == "unload_place" and len(_split_location_values(data.get("unload_city"))) > 1:
        return True

    size_type = data.get("size_type", "").strip()
    if size_type == "Насип" and question_key == "unload_method":
        return True
    return False


def _build_reply_keyboard(options: Optional[List[str]], show_back: bool = False) -> Optional[ReplyKeyboardMarkup]:
    if not options:
        keyboard = [[KeyboardButton(text="⬅️ Назад")]] if show_back else None
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True) if keyboard else None
    buttons = [[KeyboardButton(text=opt)] for opt in options]
    if "Ввести своє" not in options:
        buttons.append([KeyboardButton(text="Ввести своє")])
    if show_back:
        buttons.append([KeyboardButton(text="⬅️ Назад")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)


def _build_month_calendar(year: int, month: int, prefix: str = CAL_PREFIX) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    header_text = f"{MONTH_NAMES_UK[month - 1]} {year}"
    rows.append([InlineKeyboardButton(text=header_text, callback_data=f"{prefix}:X")])
    rows.append([InlineKeyboardButton(text=day, callback_data=f"{prefix}:X") for day in WEEKDAYS_UK])

    cal = calendar.Calendar(firstweekday=0)
    for week in cal.monthdayscalendar(year, month):
        row: List[InlineKeyboardButton] = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data=f"{prefix}:X"))
            else:
                row.append(
                    InlineKeyboardButton(
                        text=str(day),
                        callback_data=f"{prefix}:D:{year:04d}-{month:02d}-{day:02d}",
                    )
                )
        rows.append(row)

    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    rows.append(
        [
            InlineKeyboardButton(text="«", callback_data=f"{prefix}:N:{prev_year:04d}-{prev_month:02d}"),
            InlineKeyboardButton(text="Сьогодні", callback_data=f"{prefix}:T"),
            InlineKeyboardButton(text="»", callback_data=f"{prefix}:N:{next_year:04d}-{next_month:02d}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _parse_calendar_callback(data: str, prefix: str = CAL_PREFIX) -> Tuple[str, Optional[str]]:
    if not data or not data.startswith(f"{prefix}:"):
        return "IGNORE", None
    parts = data.split(":", 2)
    if len(parts) < 2:
        return "IGNORE", None
    action = parts[1]
    if action in {"X"}:
        return "IGNORE", None
    if action == "T":
        today = date.today()
        return "NAV", f"{today.year:04d}-{today.month:02d}"
    if len(parts) < 3:
        return "IGNORE", None
    payload = parts[2]
    if action == "N":
        return "NAV", payload
    if action == "D":
        return "DATE", payload
    return "IGNORE", None


SUMMARY_PAGE_SIZE = 4


def _short_city_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text == "—":
        return "—"
    first = text.split(";")[0].strip()
    first = first.split(",")[0].strip()
    first = re.sub(r"^(м\.?|с\.?|смт\.?|селище|село)\s+", "", first, flags=re.IGNORECASE)
    return first or "—"


def _build_compact_route(data: Dict[str, Any]) -> str:
    load_city = _short_city_name(data.get("load_city"))
    unload_cities = _split_location_values(data.get("unload_city"))
    unload_main = _short_city_name(unload_cities[0] if unload_cities else data.get("unload_city"))
    if len(unload_cities) > 1:
        unload_main = f"{unload_main}+{len(unload_cities) - 1}"
    return f"{load_city}→{unload_main}"


def _get_summary_filters(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    filters = context.user_data.get("summary_filters")
    if not isinstance(filters, dict):
        filters = {"department": "ALL", "active_only": False}
        context.user_data["summary_filters"] = filters
    filters.setdefault("department", "ALL")
    filters.setdefault("active_only", False)
    return filters


def _format_summary_entry_line(req: Dict[str, Any]) -> str:
    rid = (req.get("request_id") or "—").strip().upper()
    status = req.get("status") or "АКТИВНА"
    data = req.get("request_data") or {}
    if not isinstance(data, dict):
        data = {}
    initiator = str(data.get("initiator") or "—")
    cargo = str(data.get("cargo_type") or "—")
    route = _build_compact_route(data)
    volume_value = _to_float(data.get("volume"))
    volume = _format_volume_for_size(str(data.get("size_type") or ""), volume_value) if volume_value is not None else "—"
    return f"{rid} | {initiator} | {cargo} | {route} | {volume} | {status}"


def _format_summary_entry_block(req: Dict[str, Any], index: int) -> str:
    rid = (req.get("request_id") or "—").strip().upper()
    status = req.get("status") or "АКТИВНА"
    data = req.get("request_data") or {}
    if not isinstance(data, dict):
        data = {}
    initiator = str(data.get("initiator") or "—")
    cargo = str(data.get("cargo_type") or "—")
    route = _build_compact_route(data)
    volume_value = _to_float(data.get("volume"))
    volume = _format_volume_for_size(str(data.get("size_type") or ""), volume_value) if volume_value is not None else "—"
    return (
        f"{index}. 🆔 {rid}\n"
        f"   👤 Ініціатор: {initiator}\n"
        f"   📦 Вантаж: {cargo}\n"
        f"   📍 Маршрут: {route}\n"
        f"   ⚖️ Обсяг: {volume}\n"
        f"   📌 Статус: {status}"
    )


def _truncate_text(value: Any, max_len: int) -> str:
    text = str(value or "—").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _format_summary_entry_compact_line(req: Dict[str, Any], index: int) -> str:
    rid = (req.get("request_id") or "—").strip().upper()
    status = (req.get("status") or "АКТИВНА").strip().upper()
    status_short = "🟢" if status != "ВИДАЛЕНО" else "🔴"

    data = req.get("request_data") or {}
    if not isinstance(data, dict):
        data = {}

    initiator = _truncate_text(data.get("initiator") or "—", 16)
    cargo = _truncate_text(data.get("cargo_type") or "—", 14)
    route = _truncate_text(_build_compact_route(data), 22)
    volume_value = _to_float(data.get("volume"))
    volume = _format_volume_for_size(str(data.get("size_type") or ""), volume_value) if volume_value is not None else "—"

    return f"{index}. {rid} | {initiator} | {cargo} | {route} | {volume} | {status_short}"


def _build_summary_export_text(date_str: str, entries: List[Dict[str, Any]]) -> str:
    lines = [
        f"Зведення за {date_str}",
        "Формат: ID | Ініціатор | Вантаж | Маршрут | Обсяг | Статус",
        "",
    ]
    for req in entries:
        lines.append(_format_summary_entry_line(req))
    return "\n".join(lines)


async def _send_summary_csv(update: Update, context: ContextTypes.DEFAULT_TYPE, date_str: str, entries: List[Dict[str, Any]]) -> None:
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(["ID", "Ініціатор", "Вантаж", "Маршрут", "Обсяг", "Статус"])
    for req in entries:
        line = _format_summary_entry_line(req)
        parts = [part.strip() for part in line.split("|")]
        writer.writerow(parts)

    csv_bytes = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    csv_bytes.name = f"summary_{date_str.replace('.', '-')}.csv"
    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=InputFile(csv_bytes),
        caption=f"📊 CSV зведення за {date_str}",
    )


def _load_png_summary_fonts(ImageFont):
    """Load Unicode fonts for PNG summary, preferring configured/system paths."""
    configured_path = (os.getenv("SUMMARY_FONT_PATH") or "").strip()
    candidates = []

    # Try Pillow bundled fonts first (works even when OS fonts are absent in container).
    try:
        pil_module_dir = os.path.dirname(getattr(ImageFont, "__file__", "") or "")
        if pil_module_dir:
            candidates.append(os.path.join(pil_module_dir, "fonts", "DejaVuSans.ttf"))
    except Exception:
        pass

    if configured_path:
        normalized = configured_path.strip().strip('"').strip("'")
        candidates.append(normalized)
        # Railway/UI can occasionally pass path without leading '/'.
        if not normalized.startswith("/") and not re.match(r"^[A-Za-z]:[/\\]", normalized):
            candidates.append("/" + normalized.lstrip("/"))

    candidates.extend([
        # Linux (Railway/Ubuntu)
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/local/share/fonts/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        # Windows typical paths
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibri.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
    ])

    for font_path in candidates:
        if not font_path:
            continue
        try:
            font = ImageFont.truetype(font_path, 16)
            title_font = ImageFont.truetype(font_path, 18)
            return font, title_font, font_path
        except Exception:
            continue

    logging.warning("PNG summary: Unicode font not found, using fallback default font")
    fallback = ImageFont.load_default()
    return fallback, fallback, None


async def _send_summary_png(update: Update, context: ContextTypes.DEFAULT_TYPE, date_str: str, entries: List[Dict[str, Any]]) -> None:
    """Generate PNG image with compact summary and send it to chat."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        await update.effective_message.reply_text("❌ Для PNG-експорту відсутня бібліотека Pillow")
        return

    lines = [
        f"Зведення за {date_str}",
        "ID | Ініціатор | Вантаж | Маршрут | Обсяг | Статус",
        "",
    ]
    for req in entries:
        lines.append(_format_summary_entry_line(req))

    if len(lines) > 120:
        lines = lines[:120] + ["... (скорочено, використайте CSV для повного списку)"]

    font, title_font, font_path = _load_png_summary_fonts(ImageFont)

    # Do not send unreadable PNG with missing Cyrillic glyphs.
    if not font_path:
        await update.effective_message.reply_text(
            "❌ PNG не згенеровано: не знайдено Unicode-шрифт для кирилиці.\n"
            "Додайте змінну SUMMARY_FONT_PATH у Variables, наприклад:\n"
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        )
        return

    dummy_img = Image.new("RGB", (1, 1), "white")
    draw = ImageDraw.Draw(dummy_img)
    margin = 20
    line_height = 24

    max_width = 0
    for idx, line in enumerate(lines):
        current_font = title_font if idx == 0 else font
        bbox = draw.textbbox((0, 0), line, font=current_font)
        max_width = max(max_width, bbox[2] - bbox[0])

    width = min(max(900, max_width + margin * 2), 2400)
    height = margin * 2 + line_height * len(lines)
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    y = margin
    for idx, line in enumerate(lines):
        current_font = title_font if idx == 0 else font
        color = "#111111" if idx == 0 else "#222222"
        draw.text((margin, y), line, fill=color, font=current_font)
        y += line_height

    png_bytes = io.BytesIO()
    img.save(png_bytes, format="PNG", optimize=True)
    png_bytes.seek(0)
    png_bytes.name = f"summary_{date_str.replace('.', '-')}.png"

    await context.bot.send_document(
        chat_id=update.effective_chat.id,
        document=InputFile(png_bytes),
        caption=f"🖼️ PNG зведення за {date_str}",
    )


async def _render_admin_daily_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, offset: int = 0) -> int:
    date_str = context.user_data.get("summary_date")
    if not date_str:
        await update.effective_message.reply_text("❌ Спочатку оберіть дату для зведення")
        return START

    filters = _get_summary_filters(context)
    department = filters.get("department", "ALL")
    active_only = bool(filters.get("active_only", False))

    entries = db.get_requests_by_date(
        date_str=date_str,
        department=None if department == "ALL" else department,
        active_only=active_only,
        limit=1000,
    )
    context.user_data["summary_entries"] = entries

    total = len(entries)
    active_count = len([r for r in entries if (r.get("status") or "").strip().upper() != "ВИДАЛЕНО"])
    total_volume = 0.0
    for req in entries:
        data = req.get("request_data") or {}
        if isinstance(data, dict):
            v = _to_float(data.get("volume"))
            if v is not None:
                total_volume += v

    if total == 0:
        summary_text = (
            f"📊 Зведення за {date_str}\n"
            f"Фільтр відділу: {department}\n"
            f"Тільки активні: {'Так' if active_only else 'Ні'}\n\n"
            f"Заявок не знайдено"
        )
    else:
        page = entries[offset:offset + SUMMARY_PAGE_SIZE]
        lines = [
            f"📊 Зведення за {date_str}",
            f"Всього: {total} | Активні: {active_count} | Сумарний обсяг: {_format_number(total_volume)}",
            f"Фільтр відділу: {department} | Тільки активні: {'Так' if active_only else 'Ні'}",
            "",
            "N | ID | Ініціатор | Вантаж | Маршрут | Обсяг | Статус",
        ]
        for idx, req in enumerate(page, start=offset + 1):
            lines.append(_format_summary_entry_compact_line(req, idx))
        summary_text = "\n".join(lines)

    buttons: List[List[InlineKeyboardButton]] = []

    page_items = entries[offset:offset + SUMMARY_PAGE_SIZE]
    for req in page_items:
        rid = (req.get("request_id") or "").strip().upper()
        if rid:
            buttons.append([
                InlineKeyboardButton(text=rid, callback_data=f"SUMEDIT:{rid}"),
                InlineKeyboardButton(text="🗑️", callback_data=f"SUMDELASK:{rid}"),
            ])

    dept = filters.get("department", "ALL")
    buttons.append([
        InlineKeyboardButton(text=("• Всі" if dept == "ALL" else "Всі"), callback_data="SUMDEPT:ALL"),
        InlineKeyboardButton(text=("• Тваринництво" if dept == "Тваринництво" else "Тваринництво"), callback_data="SUMDEPT:Тваринництво"),
        InlineKeyboardButton(text=("• Виробництво" if dept == "Виробництво" else "Виробництво"), callback_data="SUMDEPT:Виробництво"),
    ])
    buttons.append([
        InlineKeyboardButton(
            text=f"✅ Тільки активні: {'ON' if active_only else 'OFF'}",
            callback_data="SUMTOGGLEACTIVE",
        )
    ])

    nav_row: List[InlineKeyboardButton] = []
    prev_offset = max(0, offset - SUMMARY_PAGE_SIZE)
    next_offset = offset + SUMMARY_PAGE_SIZE
    if offset > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"SUMPAGE:{prev_offset}"))
    if next_offset < total:
        nav_row.append(InlineKeyboardButton(text="➡️ Далі", callback_data=f"SUMPAGE:{next_offset}"))
    if nav_row:
        buttons.append(nav_row)

    buttons.append([
        InlineKeyboardButton(text="📄 TXT", callback_data="SUMEXP:TXT"),
        InlineKeyboardButton(text="📊 CSV", callback_data="SUMEXP:CSV"),
        InlineKeyboardButton(text="🖼️ PNG", callback_data="SUMEXP:PNG"),
    ])
    buttons.append([InlineKeyboardButton(text="📅 Інша дата", callback_data="SUMDATE")])

    await update.effective_message.reply_text(summary_text, reply_markup=InlineKeyboardMarkup(buttons))
    return START


def _format_application(data: Dict[str, Any]) -> str:
    def val(key: str) -> str:
        value = data.get(key)
        return value if value else "—"
    
    # Використовуємо часовий пояс Київа (UTC+2)
    kyiv_tz = pytz.timezone('Europe/Kyiv')
    now = datetime.now(kyiv_tz)
    date_str = now.strftime("%d.%m.%Y")
    time_str = now.strftime("%H:%M")
    
    # Форматувати size_type з big_bag_weight для Біг-бегу
    size_type = val('size_type')
    if size_type == "Біг-бег":
        big_bag_weight = val('big_bag_weight')
        if big_bag_weight != "—":
            size_type = f"Біг-бег - {big_bag_weight} кг/шт"
    
    # Форматувати обсяг/вагу з правильною одиницею
    volume_value = val('volume')
    size_type_for_unit = data.get('size_type', '')
    if volume_value != "—":
        volume_value = _format_volume_with_unit(size_type_for_unit, volume_value)

    return (
            f"Дата: {date_str}\n"
            f"Час: {time_str}\n\n"
        "ЗАЯВКА НА ПЕРЕВЕЗЕННЯ\n\n"
        f"Запит від: {val('department')}\n\n"
        "Вимоги до авто:\n"
        f"Тип авто: {val('vehicle_type')}\n\n"
        "Ініціатор заявки:\n"
        f"ПІБ: {val('initiator')}\n\n"
        "Параметри перевезення:\n"
        f"Підприємство: {val('company')}\n"
        f"Вид вантажу: {val('cargo_type')}\n"
        f"Габарит / негабарит: {size_type}\n"
        f"Обсяг: {volume_value}\n"
        f"Примітки: {val('notes')}\n\n"
        "Маршрут:\n"
        f"Дата / період перевезення: {val('date_period')}\n\n"
        f"Населений пункт завантаження: {val('load_city')}\n"
        f"Склад завантаження: {val('load_place')}\n"
        f"Спосіб завантаження: {val('load_method')}\n"
        f"Контакт на завантаженні: {val('load_contact')}\n\n"
        f"Населений пункт розвантаження: {val('unload_city')}\n"
        f"Склад розвантаження: {val('unload_place')}\n"
        f"Спосіб розвантаження: {val('unload_method')}\n"
        f"Контакт на розвантаженні: {val('unload_contact')}"
    )


async def show_start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показати початкове меню: нова заявка або завантажити шаблон"""
    if not _is_private_chat(update):
        await _send_private_only_notice(update)
        return ConversationHandler.END

    user_id = update.effective_user.id
    templates = db.get_user_templates(user_id)
    
    menu_items = [
        "📝 Нова заявка",
        "⚡ Швидка заявка",
        "📋 Мої заявки",
        "🔎 Пошук/редагування заявки",
        "🆔 Мій ID",
    ]

    if _is_admin_user(update.effective_user):
        menu_items.append("📊 Зведення за датою")
    
    if templates:
        menu_items.append("📋 Завантажити шаблон")
        menu_items.append("🗑️ Видалити шаблон")

    buttons: List[List[KeyboardButton]] = []
    for idx in range(0, len(menu_items), 2):
        row_items = menu_items[idx:idx + 2]
        buttons.append([KeyboardButton(text=item) for item in row_items])
    
    keyboard = ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(
        "Що робитимемо?",
        reply_markup=keyboard
    )
    return START


async def show_templates_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показати список шаблонів"""
    user_id = update.effective_user.id
    templates = db.get_user_templates(user_id)
    
    if not templates:
        await update.message.reply_text(
            "У вас немає збережених шаблонів.",
            reply_markup=ReplyKeyboardRemove()
        )
        return await start(update, context)
    
    buttons = [[KeyboardButton(text=t["name"])] for t in templates]
    buttons.append([KeyboardButton(text="⬅️ Назад")])
    
    keyboard = ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(
        "Оберіть шаблон для видалення:" if context.user_data.get("delete_mode") else "Оберіть шаблон:",
        reply_markup=keyboard
    )
    return TEMPLATE_SELECT


async def handle_template_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка вибору шаблону"""
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id
    
    if text == "⬅️ Назад":
        context.user_data.pop("delete_mode", None)
        return await show_start_menu(update, context)
    
    templates = db.get_user_templates(user_id)
    selected_template = None
    
    for t in templates:
        if t["name"] == text:
            selected_template = db.get_template(t["id"])
            break
    
    if context.user_data.get("delete_mode"):
        if selected_template:
            context.user_data["delete_template_id"] = selected_template["id"]
            context.user_data["delete_template_name"] = selected_template["name"]
            keyboard = ReplyKeyboardMarkup(
                [[KeyboardButton(text="✅ Так")], [KeyboardButton(text="❌ Ні")]],
                resize_keyboard=True,
                one_time_keyboard=True,
            )
            await update.message.reply_text(
                f"Видалити шаблон '{selected_template['name']}'?",
                reply_markup=keyboard,
            )
            return DELETE_TEMPLATE_CONFIRM
        await update.message.reply_text("Шаблон не знайдено.")
        return TEMPLATE_SELECT

    if not selected_template:
        await update.message.reply_text("Шаблон не знайдено.")
        return TEMPLATE_SELECT
    
    context.user_data.clear()
    context.user_data.update(selected_template["data"])
    # Якщо в шаблоні вже є department - не запитуємо, одразу до підтвердження
    if context.user_data.get("department") and context.user_data.get("thread_id"):
        context.user_data["question_index"] = len(QUESTIONS)
        await update.message.reply_text(
            f"📋 Завантажено шаблон '{text}'\n✅ Запит від: {context.user_data['department']}",
            reply_markup=ReplyKeyboardRemove()
        )
        return await ask_question(update, context)
    
    # Інакше - запитати "Запит від:" щоб встановити правильну гілку
    context.user_data.pop("department", None)
    context.user_data.pop("thread_id", None)
    context.user_data["template_loaded"] = True  # Флаг, що це шаблон
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton(text="Тваринництво")], [KeyboardButton(text="Виробництво")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    bot_message = await update.message.reply_text(
        f"📋 Завантажено шаблон '{text}'\n\nЗапит від:",
        reply_markup=keyboard,
    )
    context.user_data["last_question_message_id"] = bot_message.message_id
    return DEPARTMENT


async def handle_delete_template_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Підтвердження видалення шаблону"""
    text = (update.message.text or "").strip()

    if text == "✅ Так":
        template_id = context.user_data.get("delete_template_id")
        template_name = context.user_data.get("delete_template_name")
        if template_id:
            db.delete_template(template_id)
        if template_name:
            await update.message.reply_text(f"✅ Шаблон '{template_name}' видалено.")
        else:
            await update.message.reply_text("✅ Шаблон видалено.")
    elif text == "❌ Ні":
        await update.message.reply_text("❎ Видалення скасовано.")
    else:
        await update.message.reply_text("Оберіть: ✅ Так або ❌ Ні.")
        return DELETE_TEMPLATE_CONFIRM

    context.user_data.pop("delete_mode", None)
    context.user_data.pop("delete_template_id", None)
    context.user_data.pop("delete_template_name", None)
    return await show_start_menu(update, context)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Команда /start - початок роботи бота"""
    if not _is_private_chat(update):
        await _send_private_only_notice(update)
        return ConversationHandler.END

    # Перевірка, чи вже йде заповнення
    if context.user_data.get("question_index") is not None:
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(text="Продовжити")], [KeyboardButton(text="Почати спочатку")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text(
            "Ви вже заповнюєте заявку. Що робити?",
            reply_markup=keyboard,
        )
        return START
    
    # Показати меню вибору
    return await show_start_menu(update, context)


async def handle_start_menu_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка вибору на початковому меню (перед початком або для продовження)"""
    if not _is_private_chat(update):
        await _send_private_only_notice(update)
        context.user_data.clear()
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    
    # Обробка меню після надіслання заявки
    if text == "✏️ Редагувати заявку":
        request_id = context.user_data.get("last_request_id")
        if request_id:
            # Отримати заявку з БД
            request = db.get_request(request_id)
            if request:
                # Відновити дані заявки в context
                request_data = request.get("request_data", {})
                context.user_data.update(request_data)
                context.user_data["request_id"] = request_id
                context.user_data["editing_mode"] = True
                context.user_data["is_request_edit"] = True
                # Зберегти оригінальні дані для порівняння після редагування
                context.user_data["original_request_data"] = dict(request_data)
                
                # Показати меню полів для редагування
                return await show_edit_fields(update, context)
            else:
                await update.message.reply_text(
                    f"❌ Заявка {request_id} не знайдена",
                    reply_markup=ReplyKeyboardRemove()
                )
        else:
            await update.message.reply_text(
                "❌ ID заявки не знайдено",
                reply_markup=ReplyKeyboardRemove()
            )
        return START
    
    elif text == "🗑️ Видалити заявку":
        request_id = context.user_data.get("last_request_id")
        if request_id:
            # Позначити як видалену в БД
            db.mark_request_as_deleted(request_id)
            
            # Отримати message_id
            request = db.get_request(request_id)
            if request and request.get("message_id"):
                # Видалити з групового чату
                chat_id = os.getenv("TARGET_CHAT_ID")
                thread_id = context.user_data.get("thread_id")
                try:
                    await context.bot.delete_message(
                        chat_id=chat_id,
                        message_id=request["message_id"]
                    )
                    logging.info(f"Deleted message {request['message_id']} from chat")
                except Exception as e:
                    logging.error(f"Failed to delete message: {e}")
            
            # Оновити Sheets
            try:
                sheets.mark_request_deleted(request_id)
            except Exception as e:
                logging.error(f"Failed to mark request as deleted in sheets: {e}")
            
            await update.message.reply_text(
                f"✅ Заявку {request_id} видалено",
                reply_markup=ReplyKeyboardRemove()
            )
        else:
            await update.message.reply_text(
                "❌ ID заявки не знайдено",
                reply_markup=ReplyKeyboardRemove()
            )
        
        context.user_data.clear()
        return await show_start_menu(update, context)
    
    elif text == "✅ Готово":
        context.user_data.clear()
        return await show_start_menu(update, context)
    
    elif text == "💾 Зберегти як шаблон":
        # Перейти до збереження шаблону
        await update.message.reply_text(
            "Як назвати цей шаблон?",
            reply_markup=ReplyKeyboardRemove()
        )
        return SAVE_TEMPLATE_NAME

    elif text == "📋 Мої заявки":
        return await my_requests_command(update, context)

    elif text == "🆔 Мій ID":
        user = update.effective_user
        if not user:
            await update.message.reply_text("❌ Не вдалося визначити ваш ID")
            return START

        username = f"@{user.username}" if user.username else "—"
        await update.message.reply_text(
            f"Ваш Telegram ID: {user.id}\nUsername: {username}"
        )
        return START

    elif text == "🔎 Пошук/редагування заявки":
        await update.message.reply_text(
            "Введіть ID заявки для редагування\n"
            "Приклад: A1B2C3D4 або A1B2C3D4-01",
            reply_markup=ReplyKeyboardRemove(),
        )
        return FIND_REQUEST_EDIT

    elif text == "📊 Зведення за датою":
        if not _is_admin_user(update.effective_user):
            await update.message.reply_text("❌ Функція доступна лише адміну")
            return START

        context.user_data["summary_date"] = None
        context.user_data["summary_filters"] = {"department": "ALL", "active_only": False}
        today = date.today()
        calendar_markup = _build_month_calendar(today.year, today.month, prefix=SUMMARY_CAL_PREFIX)
        await update.message.reply_text(
            "Оберіть дату для зведення:",
            reply_markup=calendar_markup,
        )
        return SUMMARY_DATE_CALENDAR
    
    # Якщо користувач вже заповнюватиме - обробити продовження/рестарт
    if text == "Продовжити":
        await update.message.reply_text(
            "Продовжуємо заповнення...",
            reply_markup=ReplyKeyboardRemove(),
        )
        context.user_data["last_question_message_id"] = None
        return await ask_question(update, context)
    elif text == "Почати спочатку":
        context.user_data.clear()
        context.user_data["question_index"] = 0
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(text="Тваринництво")], [KeyboardButton(text="Виробництво")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        bot_message = await update.message.reply_text(
            "Запит від:",
            reply_markup=keyboard,
        )
        context.user_data["last_question_message_id"] = bot_message.message_id
        return DEPARTMENT
    # Новий вибір - нова заявка чи шаблон
    elif text == "📝 Нова заявка":
        context.user_data.clear()
        context.user_data["question_index"] = 0
        context.user_data["quick_mode"] = False
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(text="Тваринництво")], [KeyboardButton(text="Виробництво")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        bot_message = await update.message.reply_text(
            "Запит від:",
            reply_markup=keyboard,
        )
        context.user_data["last_question_message_id"] = bot_message.message_id
        return DEPARTMENT
    
    # Швидка заявка
    elif text == "⚡ Швидка заявка":
        context.user_data.clear()
        context.user_data["question_index"] = 0
        context.user_data["quick_mode"] = True
        context.user_data["company"] = "Вінницький ХАБ"  # По замовчуванню
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(text="Тваринництво")], [KeyboardButton(text="Виробництво")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        bot_message = await update.message.reply_text(
            "Запит від:",
            reply_markup=keyboard,
        )
        context.user_data["last_question_message_id"] = bot_message.message_id
        return DEPARTMENT
    
    # Завантажити шаблон
    elif text == "📋 Завантажити шаблон":
        return await show_templates_list(update, context)
    # Видалити шаблон
    elif text == "🗑️ Видалити шаблон":
        context.user_data["delete_mode"] = True
        return await show_templates_list(update, context)
    else:
        await update.message.reply_text("Будь ласка, оберіть опцію.")
        return START


async def handle_department(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text not in THREAD_IDS:
        await update.message.reply_text("Будь ласка, оберіть Тваринництво або Виробництво.")
        return DEPARTMENT

    context.user_data["department"] = text
    context.user_data["thread_id"] = THREAD_IDS[text]
    
    # Видалити повідомлення користувача та попереднє питання
    try:
        await update.message.delete()
    except:
        pass
    
    # Видалити попереднє питання "Запит від:" та показати нове з відповіддю
    try:
        last_msg_id = context.user_data.get("last_question_message_id")
        if last_msg_id:
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=last_msg_id
                )
            except:
                pass
        # Завжди показати нове повідомлення з відповіддю
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Запит від: ✅ {text}"
        )
    except:
        pass
    
    # Якщо редагується department - повернутися до підтвердження
    if context.user_data.get("editing_department"):
        context.user_data.pop("editing_department", None)
        context.user_data["question_index"] = len(QUESTIONS)
        await update.message.reply_text(
            f"✅ Змінено на '{text}'",
            reply_markup=ReplyKeyboardRemove(),
        )
        return await ask_question(update, context)
    
    # Якщо це завантажений шаблон (є флаг template_loaded) - перейти до підтвердження
    if context.user_data.get("template_loaded"):
        context.user_data.pop("template_loaded", None)
        context.user_data["question_index"] = len(QUESTIONS)
        await update.message.reply_text(
            "Форма заповнена з шаблону.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return await ask_question(update, context)
    
    # Інакше почати заповнення
    context.user_data["question_index"] = 0
    await update.message.reply_text(
        "Починаємо заповнення заявки.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return await ask_question(update, context)


async def ask_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    index = context.user_data.get("question_index", 0)
    while index < len(QUESTIONS) and _should_skip_question(QUESTIONS[index]["key"], context.user_data):
        q_key = QUESTIONS[index]["key"]
        if q_key == "unload_method" and context.user_data.get("size_type") == "Насип":
            context.user_data[q_key] = "Самоскид"
        else:
            context.user_data[q_key] = "—"
        index += 1
        context.user_data["question_index"] = index

    if index >= len(QUESTIONS):
        application_text = _format_application(context.user_data)
        
        # Для швидкої заявки запитати про додаткову інформацію ДО надіслання
        if context.user_data.get("quick_mode"):
            keyboard = ReplyKeyboardMarkup(
                [
                    [KeyboardButton(text="📤 Надіслати")],
                    [KeyboardButton(text="✏️ Додати деталі")],
                ],
                resize_keyboard=True,
                one_time_keyboard=True,
            )
            await update.message.reply_text(
                "Перевірте заявку:\n\n" + application_text + "\n\n💡 Це швидка заявка. Хочете додати додаткову інформацію або надіслати як є?",
                reply_markup=keyboard,
            )
        else:
            # Повна заявка - звичайне підтвердження
            keyboard = ReplyKeyboardMarkup(
                [[KeyboardButton(text="ТАК")], [KeyboardButton(text="✏️ Редагувати поля")]],
                resize_keyboard=True,
                one_time_keyboard=True,
            )
            await update.message.reply_text(
                "Перевірте заявку:\n\n" + application_text + "\n\nНадіслати заявку в чат?",
                reply_markup=keyboard,
            )
        return CONFIRM

    question = _get_question(index)
    
    # Якщо це питання про населений пункт - запускаємо пошук
    if question.get("use_city_search"):
        # У режимі редагування починаємо зміну НП з чистого списку,
        # але залишаємо можливість додавати ще пункти в цьому ж потоці.
        if context.user_data.get("editing_mode") and question.get("key") == "load_city":
            if not context.user_data.get("load_city_edit_reset_done"):
                context.user_data["load_city_edit_original"] = context.user_data.get("load_city", "—")
                context.user_data["load_city"] = "—"
                context.user_data["load_city_edit_reset_done"] = True

        if context.user_data.get("editing_mode") and question.get("key") == "unload_city":
            if not context.user_data.get("unload_city_edit_reset_done"):
                context.user_data["unload_city_edit_original"] = context.user_data.get("unload_city", "—")
                context.user_data["unload_city"] = "—"
                context.user_data["unload_city_edit_reset_done"] = True

        if question.get("key") == "unload_city":
            context.user_data.pop("unload_distribution", None)
            context.user_data.pop("unload_distribution_cities", None)
            context.user_data.pop("unload_distribution_total", None)
            context.user_data.pop("unload_distribution_idx", None)

        show_back = index > 0
        buttons = []
        if question.get("key") == "load_city":
            buttons.append([KeyboardButton(text="Пропустити")])
        if show_back:
            buttons.append([KeyboardButton(text="⬅️ Назад")])
        
        keyboard = ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True) if buttons else ReplyKeyboardRemove()
        
        progress = f"({index + 1}/{len(QUESTIONS)})"
        prompt_with_progress = f"{question['prompt']} {progress}\n\n💡 Почніть вводити назву населеного пункту..."
        
        bot_message = await update.message.reply_text(prompt_with_progress, reply_markup=keyboard)
        context.user_data["last_question_message_id"] = bot_message.message_id
        
        # Визначаємо стан в залежності від типу пункту
        if question["key"] == "load_city":
            return CITY_SEARCH_LOAD
        elif question["key"] == "unload_city":
            return CITY_SEARCH_UNLOAD
    
    # Якщо це питання про дату - запитуємо тип перевезення
    if question["key"] == "date_period":
        buttons = [
            [KeyboardButton(text="📅 Разове перевезення")], 
            [KeyboardButton(text="📆 Період перевезення")]
        ]
        if index > 0:
            buttons.append([KeyboardButton(text="⬅️ Назад")])
        
        keyboard = ReplyKeyboardMarkup(
            buttons,
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        bot_message = await update.message.reply_text(
            "Оберіть тип перевезення:",
            reply_markup=keyboard
        )
        context.user_data["last_question_message_id"] = bot_message.message_id
        return DATE_TYPE
    
    show_back = index > 0
    keyboard = _build_reply_keyboard(question.get("options"), show_back=show_back)
    
    # Для контактів - показати історію збережених
    if question["key"] in ["load_contact", "unload_contact"]:
        user_id = update.effective_user.id
        saved_contacts = get_user_contacts(user_id)
        
        buttons = []
        if saved_contacts:
            # Показати останні 5 унікальних контактів
            seen_values = set()
            unique_contacts = []
            for contact in saved_contacts:
                value = contact.get("value", "").strip()
                if not value or value in seen_values:
                    continue
                seen_values.add(value)
                unique_contacts.append(value)
            for value in unique_contacts[:5]:
                buttons.append([KeyboardButton(text=value)])
        
        buttons.append([KeyboardButton(text="✍️ Ввести новий контакт")])
        if question.get("options") and "Пропустити" in question["options"]:
            buttons.append([KeyboardButton(text="Пропустити")])
        if index > 0:
            buttons.append([KeyboardButton(text="⬅️ Назад")])
        
        keyboard = ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)
        progress = f"({index + 1}/{len(QUESTIONS)})"
        prompt_with_progress = f"{question['prompt']} {progress}\n\n💡 Оберіть зі списку або введіть новий:"
        bot_message = await update.message.reply_text(prompt_with_progress, reply_markup=keyboard)
        context.user_data["last_question_message_id"] = bot_message.message_id
        return QUESTION
    
    # Для умовного питання про volume - змінити prompt залежно від size_type
    prompt_text = question['prompt']
    if question["key"] == "volume" and question.get("conditional"):
        size_type = context.user_data.get("size_type", "")
        prompt_text = _get_volume_prompt(size_type)
    
    # Прогрес-бар: показувати скільки питань вміще
    progress = f"({index + 1}/{len(QUESTIONS)})"
    prompt_with_progress = f"{prompt_text} {progress}"
    # Зберегти message_id щоб потім редагувати
    bot_message = await update.message.reply_text(prompt_with_progress, reply_markup=keyboard)
    context.user_data["last_question_message_id"] = bot_message.message_id
    return QUESTION


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    index = context.user_data.get("question_index", 0)
    question = _get_question(index)

    # Обробка "Ввести новий контакт" для контактів
    if text == "✍️ Ввести новий контакт" and question["key"] in ["load_contact", "unload_contact"]:
        await update.message.reply_text(
            "Введіть контакт (ПІБ, телефон):",
            reply_markup=ReplyKeyboardRemove()
        )
        context.user_data["awaiting_new_contact"] = True
        context.user_data["new_contact_key"] = question["key"]
        return CUSTOM_INPUT

    # Обробка кнопки Назад
    if text == "⬅️ Назад":
        # Видалити повідомлення користувача
        try:
            await update.message.delete()
        except:
            pass
        # В режимі редагування - повернутися до підтвердження
        if context.user_data.get("editing_mode"):
            context.user_data.pop("editing_mode", None)
            context.user_data["question_index"] = len(QUESTIONS)
            return await ask_question(update, context)
        # Звичайний режим - попереднє питання
        if index > 0:
            context.user_data["question_index"] = index - 1
            return await ask_question(update, context)
        else:
            await update.message.reply_text("Ви вже на першому питанні.")
            return await ask_question(update, context)

    if text.lower() == "ввести своє":
        context.user_data["awaiting_custom"] = True
        await update.message.reply_text("Введіть своє значення:", reply_markup=ReplyKeyboardRemove())
        return CUSTOM_INPUT
    
    # Обробка "Інше" для vehicle_type
    if question["key"] == "vehicle_type" and text == "Інше":
        context.user_data["awaiting_custom_vehicle_type"] = True
        await update.message.reply_text("Введіть тип авто:", reply_markup=ReplyKeyboardRemove())
        return CUSTOM_INPUT
    
    # Обробка "Інше" для company
    if question["key"] == "company" and text == "Інше":
        context.user_data["awaiting_custom_company"] = True
        await update.message.reply_text("Введіть підприємство:", reply_markup=ReplyKeyboardRemove())
        return CUSTOM_INPUT

    # Якщо вибрано "зерно" або "насіння", запитати конкретну культуру
    if question["key"] == "cargo_type" and text.lower() in ["зерно", "насіння"]:
        context.user_data["cargo_type_prefix"] = text
        keyboard = _build_reply_keyboard(CROP_TYPES, show_back=True)
        
        # Видалити відповідь користувача
        try:
            await update.message.delete()
        except:
            pass
        
        # Видалити попереднє питання "Вид вантажу:"
        try:
            last_msg_id = context.user_data.get("last_question_message_id")
            if last_msg_id:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=last_msg_id
                )
        except:
            pass
        
        # Зберегти message_id нового питання про культуру
        bot_message = await update.message.reply_text("Оберіть культуру:", reply_markup=keyboard)
        context.user_data["last_question_message_id"] = bot_message.message_id
        return CROP_TYPE
    
    # Обробка "Інше" для cargo_type
    if question["key"] == "cargo_type" and text == "Інше":
        context.user_data["awaiting_custom_cargo_type"] = True
        await update.message.reply_text("Введіть тип вантажу:", reply_markup=ReplyKeyboardRemove())
        return CUSTOM_INPUT

    if question.get("options"):
        if text.lower() == "пропустити":
            context.user_data[question["key"]] = "—"
        else:
            context.user_data[question["key"]] = text
    else:
        # Валідація для volume та big_bag_weight - тільки числа
        if question["key"] in ["volume", "big_bag_weight"]:
            # Видалити всі пробіли та замінити кому на крапку
            clean_text = text.replace(" ", "").replace(",", ".")
            # Перевірити чи це число
            try:
                float(clean_text)
                context.user_data[question["key"]] = clean_text
            except ValueError:
                # Не число - показати помилку
                await update.message.reply_text(
                    f"❌ Помилка: введіть тільки число (наприклад: 25 або 25.5)\n\n{_get_volume_prompt(context.user_data.get('size_type', '')) if question['key'] == 'volume' else 'Вага 1 біг-бегу в кг (тільки число):'}",
                    reply_markup=ReplyKeyboardRemove()
                )
                return QUESTION
        elif question["key"] == "notes" and text.lower() == "пропустити":
            context.user_data[question["key"]] = "—"
        else:
            context.user_data[question["key"]] = text

    # Видалити повідомлення користувача та попереднє питання бота
    try:
        await update.message.delete()
        # Видалити попереднє питання бота
        last_msg_id = context.user_data.get("last_question_message_id")
        if last_msg_id:
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=last_msg_id
                )
            except:
                pass
        
        # Спеціальна обробка для big_bag_weight: об'єднати з size_type
        if question["key"] == "big_bag_weight":
            # Видалити попереднє повідомлення про size_type
            size_type_msg_id = context.user_data.get("size_type_msg_id")
            if size_type_msg_id:
                try:
                    await context.bot.delete_message(
                        chat_id=update.effective_chat.id,
                        message_id=size_type_msg_id
                    )
                except:
                    pass
            
            # Надіслати об'єднане повідомлення
            size_type = context.user_data.get("size_type", "—")
            big_bag_weight = context.user_data.get("big_bag_weight", "—")
            combined_text = f"Габарит / негабарит: ✅ {size_type} - {big_bag_weight} кг/шт"
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=combined_text
            )
            # Зберегти цей message_id щоб видалити при редагуванні
            context.user_data["big_bag_combined_msg_id"] = msg.message_id
        elif question["key"] == "size_type" and context.user_data.get("size_type") == "Біг-бег":
            # Для Біг-бегу зберегти message_id щоб видалити його коли отримаємо big_bag_weight
            answer_value = context.user_data.get(question["key"], "—")
            msg = await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{question['prompt']} ✅ {answer_value}"
            )
            context.user_data["size_type_msg_id"] = msg.message_id
        else:
            # Звичайна обробка для інших питань
            answer_value = context.user_data.get(question["key"], "—")
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"{question['prompt']} ✅ {answer_value}"
            )
    except Exception as e:
        # Логувати помилку
        logging.error(f"Помилка при оновленні повідомлення: {e}")
        pass

    # Якщо редагуємо - повертаємо до підтвердження
    if context.user_data.get("editing_mode"):
        context.user_data.pop("editing_mode", None)
        context.user_data["question_index"] = len(QUESTIONS)
        return await ask_question(update, context)
    
    context.user_data["question_index"] = index + 1
    return await ask_question(update, context)


async def handle_custom_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    index = context.user_data.get("question_index", 0)
    question = _get_question(index)
    
    # Обробка введення нового контакту
    if context.user_data.get("awaiting_new_contact"):
        contact_key = context.user_data.get("new_contact_key")
        context.user_data[contact_key] = text
        context.user_data.pop("awaiting_new_contact", None)
        context.user_data.pop("new_contact_key", None)
        
        # Визначити label для питання
        contact_question = next((q for q in QUESTIONS if q["key"] == contact_key), None)
        display_text = f"{contact_question['prompt']} ✅ {text}" if contact_question else f"Контакт: ✅ {text}"
        
        # Видалити повідомлення користувача та попереднє питання бота
        try:
            await update.message.delete()
            last_msg_id = context.user_data.get("last_question_message_id")
            if last_msg_id:
                try:
                    await context.bot.delete_message(
                        chat_id=update.effective_chat.id,
                        message_id=last_msg_id
                    )
                except:
                    pass
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=display_text
            )
        except Exception as e:
            logging.error(f"Помилка при обробці нового контакту: {e}")
        
        # Якщо редагуємо - повертаємо до підтвердження
        if context.user_data.get("editing_mode"):
            context.user_data.pop("editing_mode", None)
            context.user_data["question_index"] = len(QUESTIONS)
            return await ask_question(update, context)
        
        context.user_data["question_index"] = index + 1
        return await ask_question(update, context)
    
    # Обробка "Інше" типів
    if context.user_data.get("awaiting_custom_vehicle_type"):
        context.user_data["vehicle_type"] = f"Інше: {text}"
        context.user_data.pop("awaiting_custom_vehicle_type", None)
        display_text = f"Тип авто: Інше: ✅ {text}"
    elif context.user_data.get("awaiting_custom_company"):
        context.user_data["company"] = f"Інше: {text}"
        context.user_data.pop("awaiting_custom_company", None)
        display_text = f"Підприємство: Інше: ✅ {text}"
    elif context.user_data.get("awaiting_custom_cargo_type"):
        context.user_data["cargo_type"] = f"Інше: {text}"
        context.user_data.pop("awaiting_custom_cargo_type", None)
        display_text = f"Вид вантажу: Інше: ✅ {text}"
    elif context.user_data.get("awaiting_custom_crop"):
        prefix = context.user_data.get("cargo_type_prefix", "Зерно")
        context.user_data["cargo_type"] = f"{prefix}: {text}"
        context.user_data.pop("awaiting_custom_crop", None)
        context.user_data.pop("cargo_type_prefix", None)
        display_text = f"Вид вантажу: {prefix}: ✅ {text}"
    else:
        # Генеричне кастомне введення
        context.user_data[question["key"]] = text
        display_text = f"{question['prompt']} ✅ {text}"
    
    context.user_data["awaiting_custom"] = False
    
    # Видалити повідомлення користувача та попереднє питання бота
    try:
        await update.message.delete()
        # Видалити попереднє питання бота
        last_msg_id = context.user_data.get("last_question_message_id")
        if last_msg_id:
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=last_msg_id
                )
            except:
                pass
            # Надіслати нове повідомлення з відповіддю
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=display_text
            )
    except Exception as e:
        logging.error(f"Помилка при обробці кастомного введення: {e}")
        pass
    
    # Якщо редагуємо - повертаємо до підтвердження
    if context.user_data.get("editing_mode"):
        context.user_data.pop("editing_mode", None)
        context.user_data["question_index"] = len(QUESTIONS)
        return await ask_question(update, context)
    
    context.user_data["question_index"] = index + 1
    return await ask_question(update, context)


async def handle_crop_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    
    if text.lower() == "ввести своє":
        context.user_data["awaiting_custom_crop"] = True
        await update.message.reply_text("Введіть назву культури:", reply_markup=ReplyKeyboardRemove())
        return CROP_TYPE
    
    # Якщо це кастомне введення
    if context.user_data.get("awaiting_custom_crop"):
        prefix = context.user_data.get("cargo_type_prefix", "Зерно")
        context.user_data["cargo_type"] = f"{prefix}: {text}"
        context.user_data.pop("awaiting_custom_crop", None)
        context.user_data.pop("cargo_type_prefix", None)
        index = context.user_data.get("question_index", 0)
        
        # Видалити повідомлення користувача та попереднє питання
        try:
            await update.message.delete()
            last_msg_id = context.user_data.get("last_question_message_id")
            if last_msg_id:
                try:
                    await context.bot.delete_message(
                        chat_id=update.effective_chat.id,
                        message_id=last_msg_id
                    )
                except:
                    pass
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"Оберіть культуру: ✅ {text}"
                )
        except:
            pass
        
        # Якщо редагуємо - повертаємо до підтвердження
        if context.user_data.get("editing_mode"):
            context.user_data.pop("editing_mode", None)
            context.user_data["question_index"] = len(QUESTIONS)
            return await ask_question(update, context)
        
        context.user_data["question_index"] = index + 1
        return await ask_question(update, context)
    
    # Якщо вибрано зі списку
    if text in CROP_TYPES:
        prefix = context.user_data.get("cargo_type_prefix", "Зерно")
        context.user_data["cargo_type"] = f"{prefix}: {text}"
        context.user_data.pop("cargo_type_prefix", None)
        index = context.user_data.get("question_index", 0)
        
        # Видалити повідомлення користувача та попереднє питання
        try:
            await update.message.delete()
            last_msg_id = context.user_data.get("last_question_message_id")
            if last_msg_id:
                try:
                    await context.bot.delete_message(
                        chat_id=update.effective_chat.id,
                        message_id=last_msg_id
                    )
                except:
                    pass
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"Вид вантажу: {prefix} ✅ {text}"
                )
        except:
            pass
        
        # Якщо редагуємо - повертаємо до підтвердження
        if context.user_data.get("editing_mode"):
            context.user_data.pop("editing_mode", None)
            context.user_data["question_index"] = len(QUESTIONS)
            return await ask_question(update, context)
        
        context.user_data["question_index"] = index + 1
        return await ask_question(update, context)
    else:
        await update.message.reply_text("Будь ласка, оберіть культуру зі списку або натисніть 'Ввести своє'.")
        return CROP_TYPE


async def handle_date_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка вибору типу перевезення"""
    text = (update.message.text or "").strip()
    
    if text == "⬅️ Назад":
        # В режимі редагування - повернутися до підтвердження
        if context.user_data.get("editing_mode"):
            context.user_data.pop("editing_mode", None)
            context.user_data["question_index"] = len(QUESTIONS)
            return await ask_question(update, context)
        # Звичайний режим - попереднє питання
        index = context.user_data.get("question_index", 0)
        if index > 0:
            context.user_data["question_index"] = index - 1
            return await ask_question(update, context)
    
    if text == "📅 Разове перевезення":
        context.user_data["date_type"] = "single"
        # Видалити повідомлення користувача
        try:
            await update.message.delete()
        except:
            pass
        # Показати нове повідомлення з відповідю
        try:
            last_msg_id = context.user_data.get("last_question_message_id")
            if last_msg_id:
                try:
                    await context.bot.delete_message(
                        chat_id=update.effective_chat.id,
                        message_id=last_msg_id
                    )
                except:
                    pass
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Оберіть тип перевезення: 📅 Разове ✅"
            )
        except:
            pass
        today = date.today()
        calendar = _build_month_calendar(today.year, today.month)
        await update.message.reply_text(
            "Оберіть дату перевезення:",
            reply_markup=calendar
        )
        return DATE_CALENDAR
    elif text == "📆 Період перевезення":
        context.user_data["date_type"] = "period"
        # Видалити повідомлення користувача
        try:
            await update.message.delete()
        except:
            pass
        # Показати нове повідомлення з відповідю
        try:
            last_msg_id = context.user_data.get("last_question_message_id")
            if last_msg_id:
                try:
                    await context.bot.delete_message(
                        chat_id=update.effective_chat.id,
                        message_id=last_msg_id
                    )
                except:
                    pass
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="Оберіть тип перевезення: 📆 Період ✅"
            )
        except:
            pass
        today = date.today()
        calendar = _build_month_calendar(today.year, today.month)
        await update.message.reply_text(
            "Оберіть початкову дату перевезення:",
            reply_markup=calendar
        )
        return DATE_CALENDAR
    else:
        await update.message.reply_text("Будь ласка, оберіть тип перевезення.")
        return DATE_TYPE


async def handle_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка вибору дати з календаря"""
    await update.callback_query.answer()
    action, payload = _parse_calendar_callback(update.callback_query.data)
    date_type = context.user_data.get("date_type")

    if action == "NAV" and payload:
        year_str, month_str = payload.split("-")
        calendar = _build_month_calendar(int(year_str), int(month_str))
        prompt = "Оберіть дату перевезення:" if date_type == "single" else "Оберіть початкову дату перевезення:"
        await update.callback_query.edit_message_text(prompt, reply_markup=calendar)
        return DATE_CALENDAR

    if action == "DATE" and payload:
        selected_dt = datetime.strptime(payload, "%Y-%m-%d").date()
        selected_date = selected_dt.strftime("%d.%m.%Y")
        
        if date_type == "single":
            context.user_data["date_period"] = selected_date
            await update.callback_query.edit_message_text(f"Дата перевезення: {selected_date}")
            
            # Переходимо до наступного питання або підтвердження
            if context.user_data.get("editing_mode"):
                context.user_data.pop("editing_mode", None)
                context.user_data["question_index"] = len(QUESTIONS)
            else:
                index = context.user_data.get("question_index", 0)
                context.user_data["question_index"] = index + 1
            
            # Створюємо фейковий update для ask_question
            class FakeMessage:
                def __init__(self, chat_id):
                    self.chat_id = chat_id
                    self.message_id = None
                async def reply_text(self, *args, **kwargs):
                    return await update.callback_query.message.reply_text(*args, **kwargs)
            
            fake_update = type('obj', (object,), {'message': FakeMessage(update.callback_query.message.chat_id), 'effective_user': update.effective_user})()
            return await ask_question(fake_update, context)
            
        elif date_type == "period":
            if "date_period_start" not in context.user_data:
                context.user_data["date_period_start"] = selected_date
                
                # Показуємо календар для кінцевої дати
                calendar = _build_month_calendar(selected_dt.year, selected_dt.month)
                await update.callback_query.edit_message_text(
                    "Оберіть кінцеву дату перевезення:",
                    reply_markup=calendar
                )
                return DATE_PERIOD_END
    return DATE_CALENDAR


async def handle_period_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка кінцевої дати періоду"""
    await update.callback_query.answer()
    action, payload = _parse_calendar_callback(update.callback_query.data)
    if action == "NAV" and payload:
        year_str, month_str = payload.split("-")
        calendar = _build_month_calendar(int(year_str), int(month_str))
        await update.callback_query.edit_message_text(
            "Оберіть кінцеву дату перевезення:",
            reply_markup=calendar
        )
        return DATE_PERIOD_END

    if action == "DATE" and payload:
        end_dt = datetime.strptime(payload, "%Y-%m-%d").date()
        end_date = end_dt.strftime("%d.%m.%Y")
        start_date = context.user_data.get("date_period_start")
        context.user_data["date_period"] = f"{start_date} - {end_date}"
        context.user_data.pop("date_period_start", None)
        
        await update.callback_query.edit_message_text(
            f"Період перевезення: ✅ {start_date} - {end_date}"
        )
        
        # Переходимо до наступного питання
        if context.user_data.get("editing_mode"):
            context.user_data.pop("editing_mode", None)
            context.user_data["question_index"] = len(QUESTIONS)
        else:
            index = context.user_data.get("question_index", 0)
            context.user_data["question_index"] = index + 1
        
        class FakeMessage:
            def __init__(self, chat_id):
                self.chat_id = chat_id
                self.message_id = None
            async def reply_text(self, *args, **kwargs):
                return await update.callback_query.message.reply_text(*args, **kwargs)
        
        fake_update = type('obj', (object,), {'message': FakeMessage(update.callback_query.message.chat_id), 'effective_user': update.effective_user})()
        return await ask_question(fake_update, context)
    
    return DATE_PERIOD_END


async def handle_summary_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка календаря для адмін-зведення."""
    query = update.callback_query
    if not query:
        return START
    await query.answer()

    if not _is_admin_user(update.effective_user):
        await query.answer("Тільки для адміна", show_alert=True)
        return START

    action, payload = _parse_calendar_callback(query.data, prefix=SUMMARY_CAL_PREFIX)
    if action == "NAV" and payload:
        year_str, month_str = payload.split("-")
        calendar_markup = _build_month_calendar(int(year_str), int(month_str), prefix=SUMMARY_CAL_PREFIX)
        await query.edit_message_text("Оберіть дату для зведення:", reply_markup=calendar_markup)
        return SUMMARY_DATE_CALENDAR

    if action == "DATE" and payload:
        selected_dt = datetime.strptime(payload, "%Y-%m-%d").date()
        selected_date = selected_dt.strftime("%d.%m.%Y")
        context.user_data["summary_date"] = selected_date
        context.user_data["summary_offset"] = 0
        await query.edit_message_text(f"✅ Дата зведення: {selected_date}")
        return await _render_admin_daily_summary(update, context, offset=0)

    return SUMMARY_DATE_CALENDAR


async def handle_summary_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Callback-дії для адмін-зведення: фільтри, пагінація, експорт, редагування."""
    query = update.callback_query
    if not query:
        return START

    await query.answer()
    if not _is_admin_user(update.effective_user):
        await query.answer("Тільки для адміна", show_alert=True)
        return START

    payload = query.data or ""
    parts = payload.split(":", 2)
    action = parts[0]
    arg = parts[1] if len(parts) > 1 else ""

    if action == "SUMPAGE":
        try:
            offset = max(0, int(arg))
        except ValueError:
            offset = 0
        context.user_data["summary_offset"] = offset
        return await _render_admin_daily_summary(update, context, offset=offset)

    if action == "SUMTOGGLEACTIVE":
        filters = _get_summary_filters(context)
        filters["active_only"] = not bool(filters.get("active_only", False))
        context.user_data["summary_offset"] = 0
        return await _render_admin_daily_summary(update, context, offset=0)

    if action == "SUMDEPT":
        filters = _get_summary_filters(context)
        chosen = arg if arg in {"ALL", "Тваринництво", "Виробництво"} else "ALL"
        filters["department"] = chosen
        context.user_data["summary_offset"] = 0
        return await _render_admin_daily_summary(update, context, offset=0)

    if action == "SUMDATE":
        today = date.today()
        calendar_markup = _build_month_calendar(today.year, today.month, prefix=SUMMARY_CAL_PREFIX)
        await query.message.reply_text("Оберіть дату для зведення:", reply_markup=calendar_markup)
        return SUMMARY_DATE_CALENDAR

    if action == "SUMEXP":
        export_type = arg.upper()
        date_str = context.user_data.get("summary_date") or ""
        entries = context.user_data.get("summary_entries") or []
        if not date_str:
            await query.message.reply_text("❌ Спочатку сформуйте зведення за датою")
            return START
        if export_type == "TXT":
            text = _build_summary_export_text(date_str, entries)
            text_bytes = io.BytesIO(text.encode("utf-8"))
            text_bytes.name = f"summary_{date_str.replace('.', '-')}.txt"
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=InputFile(text_bytes),
                caption=f"📄 TXT зведення за {date_str}",
            )
            return START
        if export_type == "CSV":
            await _send_summary_csv(update, context, date_str, entries)
            return START
        if export_type == "PNG":
            await _send_summary_png(update, context, date_str, entries)
            return START

    if action == "SUMEDIT":
        request_id = arg.strip().upper()
        if not request_id:
            await query.answer("Порожній ID", show_alert=True)
            return START
        fake_update = type('obj', (object,), {'message': query.message, 'effective_user': update.effective_user})()
        return await _open_request_for_edit_by_id(fake_update, context, request_id)

    if action == "SUMDELASK":
        request_id = arg.strip().upper()
        if not request_id:
            await query.answer("Порожній ID", show_alert=True)
            return START
        confirm_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(text="✅ Видалити", callback_data=f"SUMDELYES:{request_id}"),
                InlineKeyboardButton(text="❌ Скасувати", callback_data=f"SUMDELNO:{request_id}"),
            ]
        ])
        await query.message.reply_text(
            f"Підтвердьте видалення заявки {request_id}",
            reply_markup=confirm_keyboard,
        )
        return START

    if action == "SUMDELNO":
        await query.answer("Видалення скасовано")
        return START

    if action == "SUMDELYES":
        request_id = arg.strip().upper()
        if not request_id:
            await query.answer("Порожній ID", show_alert=True)
            return START

        request = db.get_request(request_id)
        if not request:
            await query.answer("Заявку не знайдено", show_alert=True)
            return START

        if (request.get("status") or "").strip().upper() == "ВИДАЛЕНО":
            await query.answer("Заявка вже видалена", show_alert=True)
            return START

        db.mark_request_as_deleted(request_id)

        if request.get("message_id"):
            chat_id = os.getenv("TARGET_CHAT_ID")
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=request["message_id"],
                )
            except Exception as e:
                logging.error(f"Failed to delete group message for {request_id}: {e}")

        try:
            sheets.mark_request_deleted(request_id)
        except Exception as e:
            logging.error(f"Failed to mark request as deleted in sheets from summary: {e}")

        await query.message.reply_text(f"✅ Заявку {request_id} видалено")
        offset = int(context.user_data.get("summary_offset") or 0)
        return await _render_admin_daily_summary(update, context, offset=offset)

    await query.answer("Невідома дія", show_alert=True)
    return START


async def _ask_unload_distribution_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cities = context.user_data.get("unload_distribution_cities") or []
    idx = context.user_data.get("unload_distribution_idx", 0)
    total = context.user_data.get("unload_distribution_total")
    allocations = context.user_data.get("unload_distribution") or {}

    if idx >= len(cities):
        return await ask_question(update, context)

    current_city = cities[idx]
    allocated_sum = sum(float(v) for v in allocations.values())
    remaining = float(total) - allocated_sum
    size_type = context.user_data.get("size_type", "")
    total_text = _format_volume_for_size(size_type, float(total))
    remaining_text = _format_volume_for_size(size_type, remaining)

    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton(text="⬅️ Назад")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

    await update.message.reply_text(
        f"Розподіл обсягу по НП розвантаження ({idx + 1}/{len(cities)}):\n"
        f"📍 {current_city}\n"
        f"Загальний обсяг: {total_text}\n"
        f"Залишок до розподілу: {remaining_text}\n\n"
        f"Вкажіть кількість для цього НП (тільки число):",
        reply_markup=keyboard,
    )
    return UNLOAD_DISTRIBUTION


async def _finalize_unload_distribution(update: Update, context: ContextTypes.DEFAULT_TYPE, allocations: Dict[str, Any]) -> int:
    size_type = context.user_data.get("size_type", "")
    summary_lines = [
        f"• {city}: {_format_volume_for_size(size_type, float(amount))}"
        for city, amount in allocations.items()
    ]
    await update.message.reply_text(
        "✅ Розподіл за НП збережено:\n" + "\n".join(summary_lines),
        reply_markup=ReplyKeyboardRemove(),
    )

    context.user_data.pop("unload_distribution_awaiting_total", None)

    if context.user_data.get("editing_mode"):
        context.user_data.pop("editing_mode", None)
        context.user_data["question_index"] = len(QUESTIONS)
    else:
        index = context.user_data.get("question_index", 0)
        context.user_data["question_index"] = index + 1

    return await ask_question(update, context)


async def _review_unload_distribution(update: Update, context: ContextTypes.DEFAULT_TYPE, allocations: Dict[str, Any]) -> int:
    cities = context.user_data.get("unload_distribution_cities") or []
    total = float(context.user_data.get("unload_distribution_total", 0) or 0)
    size_type = context.user_data.get("size_type", "")

    ordered_allocations = {city: float(allocations.get(city, 0.0)) for city in cities}
    allocated_sum = sum(ordered_allocations.values())
    delta = total - allocated_sum

    context.user_data["unload_distribution"] = ordered_allocations
    context.user_data["unload_distribution_idx"] = len(cities)

    lines = [
        f"• {city}: {_format_volume_for_size(size_type, amount)}"
        for city, amount in ordered_allocations.items()
    ]

    summary = (
        "📊 Перевірка розподілу:\n"
        + "\n".join(lines)
        + "\n\n"
        + f"Загальний обсяг: {_format_volume_for_size(size_type, total)}\n"
        + f"Сума по НП: {_format_volume_for_size(size_type, allocated_sum)}\n"
        + f"Різниця (має бути 0): {_format_volume_for_size(size_type, delta)}"
    )

    if abs(delta) <= 1e-9:
        await update.message.reply_text(summary)
        return await _finalize_unload_distribution(update, context, ordered_allocations)

    await update.message.reply_text(
        summary + "\n\n"
        "❗ Різниця не 0.\n"
        "Можна змінити загальний обсяг або переввести розподіл, щоб зійшлося в 0.",
        reply_markup=_build_distribution_actions_keyboard(),
    )
    return UNLOAD_DISTRIBUTION


async def _finish_unload_city_step(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("unload_city_edit_reset_done", None)
    context.user_data.pop("unload_city_edit_original", None)

    selected = _split_location_values(context.user_data.get("unload_city"))

    # Для одного НП розподіл не потрібен.
    if len(selected) <= 1:
        context.user_data.pop("unload_distribution", None)
        context.user_data.pop("unload_distribution_cities", None)
        context.user_data.pop("unload_distribution_total", None)
        context.user_data.pop("unload_distribution_idx", None)
        if context.user_data.get("editing_mode"):
            context.user_data.pop("editing_mode", None)
            context.user_data["question_index"] = len(QUESTIONS)
            return await ask_question(update, context)

        index = context.user_data.get("question_index", 0)
        context.user_data["question_index"] = index + 1
        return await ask_question(update, context)

    total_volume = _to_float(context.user_data.get("volume"))
    if not total_volume or total_volume <= 0:
        # fallback: якщо не вдалося розпарсити загальний обсяг, ідемо без розподілу
        if context.user_data.get("editing_mode"):
            context.user_data.pop("editing_mode", None)
            context.user_data["question_index"] = len(QUESTIONS)
            return await ask_question(update, context)

        index = context.user_data.get("question_index", 0)
        context.user_data["question_index"] = index + 1
        return await ask_question(update, context)

    context.user_data["unload_distribution_cities"] = selected
    context.user_data["unload_distribution_total"] = total_volume
    context.user_data["unload_distribution_idx"] = 0
    context.user_data["unload_distribution"] = {}
    return await _ask_unload_distribution_prompt(update, context)


async def handle_unload_distribution(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()

    cities = context.user_data.get("unload_distribution_cities") or []
    idx = int(context.user_data.get("unload_distribution_idx", 0))
    total = float(context.user_data.get("unload_distribution_total", 0) or 0)
    allocations = context.user_data.get("unload_distribution") or {}

    if not cities or total <= 0:
        return await _finish_unload_city_step(update, context)

    if text == "✏️ Змінити загальний обсяг":
        context.user_data["unload_distribution_awaiting_total"] = True
        await update.message.reply_text("Введіть новий загальний обсяг (тільки число):")
        return UNLOAD_DISTRIBUTION

    if text == "🔁 Переввести розподіл":
        context.user_data["unload_distribution"] = {}
        context.user_data["unload_distribution_idx"] = 0
        context.user_data.pop("unload_distribution_awaiting_total", None)
        return await _ask_unload_distribution_prompt(update, context)

    if context.user_data.get("unload_distribution_awaiting_total"):
        new_total = _to_float(text)
        if new_total is None or new_total <= 0:
            await update.message.reply_text("❌ Введіть коректне число більше 0.")
            return UNLOAD_DISTRIBUTION

        context.user_data["unload_distribution_total"] = float(new_total)
        context.user_data.pop("unload_distribution_awaiting_total", None)
        return await _review_unload_distribution(update, context, allocations)

    if text == "⬅️ Назад":
        if idx <= 0:
            await update.message.reply_text("Повертаю до вибору НП розвантаження.")
            return CITY_SEARCH_UNLOAD

        prev_city = cities[idx - 1]
        allocations.pop(prev_city, None)
        context.user_data["unload_distribution"] = allocations
        context.user_data["unload_distribution_idx"] = idx - 1
        return await _ask_unload_distribution_prompt(update, context)

    value = _to_float(text)
    if value is None or value <= 0:
        await update.message.reply_text("❌ Введіть коректне число більше 0.")
        return UNLOAD_DISTRIBUTION

    current_city = cities[idx]
    allocations[current_city] = value
    context.user_data["unload_distribution"] = allocations
    context.user_data["unload_distribution_idx"] = idx + 1

    if idx + 1 < len(cities):
        return await _ask_unload_distribution_prompt(update, context)

    return await _review_unload_distribution(update, context, allocations)


async def handle_city_search_load(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка введення пошукового запиту для населеного пункту завантаження"""
    text = (update.message.text or "").strip()

    if text == "✅ Готово":
        selected = _split_location_values(context.user_data.get("load_city"))
        if selected:
            context.user_data.pop("load_city_edit_reset_done", None)
            context.user_data.pop("load_city_edit_original", None)
            if context.user_data.get("editing_mode"):
                context.user_data.pop("editing_mode", None)
                context.user_data["question_index"] = len(QUESTIONS)
                return await ask_question(update, context)

            index = context.user_data.get("question_index", 0)
            context.user_data["question_index"] = index + 1
            return await ask_question(update, context)

        await update.message.reply_text("Додайте хоча б один пункт або натисніть 'Пропустити'.")
        return CITY_SEARCH_LOAD

    if text == "➕ Додати ще пункт":
        await update.message.reply_text("Введіть запит для пошуку наступного населеного пункту:")
        return CITY_SEARCH_LOAD

    if text == "➖ Видалити останній пункт":
        new_value, removed = _pop_last_location_value(context.user_data.get("load_city"))
        if not removed:
            await update.message.reply_text(
                "Немає пунктів для видалення.",
                reply_markup=_build_location_actions_keyboard(include_skip=True),
            )
            return CITY_SEARCH_LOAD

        context.user_data["load_city"] = new_value
        await update.message.reply_text(
            f"🗑 Видалено: {removed}\n\nПоточний список: {new_value}",
            reply_markup=_build_location_actions_keyboard(include_skip=True),
        )
        return CITY_SEARCH_LOAD

    if context.user_data.pop("awaiting_manual_load_city", False):
        context.user_data["load_city"] = _append_location_value(context.user_data.get("load_city"), text)
        selected = context.user_data.get("load_city", "—")
        keyboard = _build_location_actions_keyboard(include_skip=True)
        await update.message.reply_text(
            f"✅ Додано пункт завантаження: {text}\n\nПоточний список: {selected}",
            reply_markup=keyboard,
        )
        return CITY_SEARCH_LOAD

    if text == "Пропустити":
        context.user_data["load_city"] = "—"
        context.user_data.pop("load_city_edit_reset_done", None)
        context.user_data.pop("load_city_edit_original", None)
        if context.user_data.get("editing_mode"):
            context.user_data.pop("editing_mode", None)
            context.user_data["question_index"] = len(QUESTIONS)
            await update.message.reply_text(
                "✅ Населений пункт завантаження пропущено",
                reply_markup=ReplyKeyboardRemove(),
            )
            return await ask_question(update, context)

        index = context.user_data.get("question_index", 0)
        context.user_data["question_index"] = index + 1
        await update.message.reply_text(
            "✅ Населений пункт завантаження пропущено",
            reply_markup=ReplyKeyboardRemove(),
        )
        return await ask_question(update, context)
    
    if text == "⬅️ Назад":
        # В режимі редагування - повернутися до підтвердження
        if context.user_data.get("editing_mode"):
            original = context.user_data.pop("load_city_edit_original", None)
            if original is not None:
                context.user_data["load_city"] = original
            context.user_data.pop("load_city_edit_reset_done", None)
            context.user_data.pop("editing_mode", None)
            context.user_data["question_index"] = len(QUESTIONS)
            return await ask_question(update, context)
        # Звичайний режим - попереднє питання
        index = context.user_data.get("question_index", 0)
        if index > 0:
            context.user_data["question_index"] = index - 1
        return await ask_question(update, context)
    
    # Пошук міст
    cities = await search_cities_novaposhta(text)
    
    if not cities:
        await update.message.reply_text(
            "🔍 Нічого не знайдено. Спробуйте інший запит або введіть повну назву вручну."
        )
        return CITY_SEARCH_LOAD
    
    # Показати варіанти
    buttons = [[KeyboardButton(text=city["display"])] for city in cities]
    buttons.append([KeyboardButton(text="✍️ Ввести вручну")])
    buttons.append([KeyboardButton(text="Пропустити")])
    
    index = context.user_data.get("question_index", 0)
    if index > 0:
        buttons.append([KeyboardButton(text="⬅️ Назад")])
    
    keyboard = ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)
    
    # Зберегти результати пошуку для подальшого вибору
    context.user_data["city_search_results"] = cities
    
    await update.message.reply_text(
        "Оберіть населений пункт зі списку або введіть вручну:",
        reply_markup=keyboard
    )
    return CITY_SELECT_LOAD


async def handle_city_select_load(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка вибору населеного пункту завантаження"""
    text = (update.message.text or "").strip()

    if text == "✅ Готово":
        selected = _split_location_values(context.user_data.get("load_city"))
        if selected:
            context.user_data.pop("load_city_edit_reset_done", None)
            context.user_data.pop("load_city_edit_original", None)
            if context.user_data.get("editing_mode"):
                context.user_data.pop("editing_mode", None)
                context.user_data["question_index"] = len(QUESTIONS)
                return await ask_question(update, context)

            index = context.user_data.get("question_index", 0)
            context.user_data["question_index"] = index + 1
            return await ask_question(update, context)

        await update.message.reply_text("Додайте хоча б один пункт або натисніть 'Пропустити'.")
        return CITY_SELECT_LOAD

    if text == "➕ Додати ще пункт":
        await update.message.reply_text("Введіть запит для пошуку наступного населеного пункту:")
        return CITY_SEARCH_LOAD

    if text == "➖ Видалити останній пункт":
        new_value, removed = _pop_last_location_value(context.user_data.get("load_city"))
        if not removed:
            await update.message.reply_text(
                "Немає пунктів для видалення.",
                reply_markup=_build_location_actions_keyboard(include_skip=True),
            )
            return CITY_SELECT_LOAD

        context.user_data["load_city"] = new_value
        await update.message.reply_text(
            f"🗑 Видалено: {removed}\n\nПоточний список: {new_value}",
            reply_markup=_build_location_actions_keyboard(include_skip=True),
        )
        return CITY_SELECT_LOAD

    if text == "Пропустити":
        context.user_data["load_city"] = "—"
        context.user_data.pop("load_city_edit_reset_done", None)
        context.user_data.pop("load_city_edit_original", None)
        if context.user_data.get("editing_mode"):
            context.user_data.pop("editing_mode", None)
            context.user_data["question_index"] = len(QUESTIONS)
            await update.message.reply_text(
                "✅ Населений пункт завантаження пропущено",
                reply_markup=ReplyKeyboardRemove(),
            )
            return await ask_question(update, context)

        index = context.user_data.get("question_index", 0)
        context.user_data["question_index"] = index + 1
        await update.message.reply_text(
            "✅ Населений пункт завантаження пропущено",
            reply_markup=ReplyKeyboardRemove(),
        )
        return await ask_question(update, context)
    
    if text == "⬅️ Назад":
        # В режимі редагування - повернутися до підтвердження
        if context.user_data.get("editing_mode"):
            original = context.user_data.pop("load_city_edit_original", None)
            if original is not None:
                context.user_data["load_city"] = original
            context.user_data.pop("load_city_edit_reset_done", None)
            context.user_data.pop("editing_mode", None)
            context.user_data["question_index"] = len(QUESTIONS)
            return await ask_question(update, context)
        # Звичайний режим - попереднє питання
        index = context.user_data.get("question_index", 0)
        if index > 0:
            context.user_data["question_index"] = index - 1
        return await ask_question(update, context)
    
    if text == "✍️ Ввести вручну":
        context.user_data["awaiting_manual_load_city"] = True
        await update.message.reply_text(
            "Введіть назву населеного пункту вручну:",
            reply_markup=ReplyKeyboardRemove()
        )
        return CITY_SEARCH_LOAD
    
    # Зберегти вибране місто
    context.user_data["load_city"] = _append_location_value(context.user_data.get("load_city"), text)
    
    # Видалити повідомлення та перейти до наступного питання
    try:
        await update.message.delete()
        last_msg_id = context.user_data.get("last_question_message_id")
        if last_msg_id:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=last_msg_id
            )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Населений пункт завантаження: ✅ {text}"
        )
    except:
        pass
    
    keyboard = _build_location_actions_keyboard(include_skip=True)
    await update.message.reply_text(
        f"Поточний список пунктів завантаження:\n{context.user_data.get('load_city', '—')}\n\nДодати ще чи завершити?",
        reply_markup=keyboard,
    )
    return CITY_SEARCH_LOAD


async def handle_city_search_unload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка введення пошукового запиту для населеного пункту розвантаження"""
    text = (update.message.text or "").strip()

    if text == "✅ Готово":
        selected = _split_location_values(context.user_data.get("unload_city"))
        if selected:
            return await _finish_unload_city_step(update, context)

        await update.message.reply_text("Додайте хоча б один пункт розвантаження.")
        return CITY_SEARCH_UNLOAD

    if text == "➕ Додати ще пункт":
        await update.message.reply_text("Введіть запит для пошуку наступного населеного пункту:")
        return CITY_SEARCH_UNLOAD

    if text == "➖ Видалити останній пункт":
        new_value, removed = _pop_last_location_value(context.user_data.get("unload_city"))
        if not removed:
            await update.message.reply_text(
                "Немає пунктів для видалення.",
                reply_markup=_build_location_actions_keyboard(),
            )
            return CITY_SEARCH_UNLOAD

        context.user_data["unload_city"] = new_value
        await update.message.reply_text(
            f"🗑 Видалено: {removed}\n\nПоточний список: {new_value}",
            reply_markup=_build_location_actions_keyboard(),
        )
        return CITY_SEARCH_UNLOAD

    if context.user_data.pop("awaiting_manual_unload_city", False):
        context.user_data["unload_city"] = _append_location_value(context.user_data.get("unload_city"), text)
        selected = context.user_data.get("unload_city", "—")
        keyboard = _build_location_actions_keyboard()
        await update.message.reply_text(
            f"✅ Додано пункт розвантаження: {text}\n\nПоточний список: {selected}",
            reply_markup=keyboard,
        )
        return CITY_SEARCH_UNLOAD
    
    if text == "⬅️ Назад":
        # В режимі редагування - повернутися до підтвердження
        if context.user_data.get("editing_mode"):
            original = context.user_data.pop("unload_city_edit_original", None)
            if original is not None:
                context.user_data["unload_city"] = original
            context.user_data.pop("unload_city_edit_reset_done", None)
            context.user_data.pop("editing_mode", None)
            context.user_data["question_index"] = len(QUESTIONS)
            return await ask_question(update, context)
        # Звичайний режим - попереднє питання
        index = context.user_data.get("question_index", 0)
        if index > 0:
            context.user_data["question_index"] = index - 1
        return await ask_question(update, context)
    
    # Пошук міст
    cities = await search_cities_novaposhta(text)
    
    if not cities:
        await update.message.reply_text(
            "🔍 Нічого не знайдено. Спробуйте інший запит або введіть повну назву вручну."
        )
        return CITY_SEARCH_UNLOAD
    
    # Показати варіанти
    buttons = [[KeyboardButton(text=city["display"])] for city in cities]
    buttons.append([KeyboardButton(text="✍️ Ввести вручну")])
    
    index = context.user_data.get("question_index", 0)
    if index > 0:
        buttons.append([KeyboardButton(text="⬅️ Назад")])
    
    keyboard = ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)
    
    # Зберегти результати пошуку для подальшого вибору
    context.user_data["city_search_results"] = cities
    
    await update.message.reply_text(
        "Оберіть населений пункт зі списку або введіть вручну:",
        reply_markup=keyboard
    )
    return CITY_SELECT_UNLOAD


async def handle_city_select_unload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка вибору населеного пункту розвантаження"""
    text = (update.message.text or "").strip()

    if text == "✅ Готово":
        selected = _split_location_values(context.user_data.get("unload_city"))
        if selected:
            return await _finish_unload_city_step(update, context)

        await update.message.reply_text("Додайте хоча б один пункт розвантаження.")
        return CITY_SELECT_UNLOAD

    if text == "➕ Додати ще пункт":
        await update.message.reply_text("Введіть запит для пошуку наступного населеного пункту:")
        return CITY_SEARCH_UNLOAD

    if text == "➖ Видалити останній пункт":
        new_value, removed = _pop_last_location_value(context.user_data.get("unload_city"))
        if not removed:
            await update.message.reply_text(
                "Немає пунктів для видалення.",
                reply_markup=_build_location_actions_keyboard(),
            )
            return CITY_SELECT_UNLOAD

        context.user_data["unload_city"] = new_value
        await update.message.reply_text(
            f"🗑 Видалено: {removed}\n\nПоточний список: {new_value}",
            reply_markup=_build_location_actions_keyboard(),
        )
        return CITY_SELECT_UNLOAD
    
    if text == "⬅️ Назад":
        # В режимі редагування - повернутися до підтвердження
        if context.user_data.get("editing_mode"):
            original = context.user_data.pop("unload_city_edit_original", None)
            if original is not None:
                context.user_data["unload_city"] = original
            context.user_data.pop("unload_city_edit_reset_done", None)
            context.user_data.pop("editing_mode", None)
            context.user_data["question_index"] = len(QUESTIONS)
            return await ask_question(update, context)
        # Звичайний режим - попереднє питання
        index = context.user_data.get("question_index", 0)
        if index > 0:
            context.user_data["question_index"] = index - 1
        return await ask_question(update, context)
    
    if text == "✍️ Ввести вручну":
        context.user_data["awaiting_manual_unload_city"] = True
        await update.message.reply_text(
            "Введіть назву населеного пункту вручну:",
            reply_markup=ReplyKeyboardRemove()
        )
        return CITY_SEARCH_UNLOAD
    
    # Зберегти вибране місто
    context.user_data["unload_city"] = _append_location_value(context.user_data.get("unload_city"), text)
    
    # Видалити повідомлення та перейти до наступного питання
    try:
        await update.message.delete()
        last_msg_id = context.user_data.get("last_question_message_id")
        if last_msg_id:
            await context.bot.delete_message(
                chat_id=update.effective_chat.id,
                message_id=last_msg_id
            )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Населений пункт розвантаження: ✅ {text}"
        )
    except:
        pass
    
    keyboard = _build_location_actions_keyboard()
    await update.message.reply_text(
        f"Поточний список пунктів розвантаження:\n{context.user_data.get('unload_city', '—')}\n\nДодати ще чи завершити?",
        reply_markup=keyboard,
    )
    return CITY_SEARCH_UNLOAD


async def show_edit_fields(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показує список полів для редагування"""
    
    # Зберегти повні значення для пошуку (буде використано в handle_edit_choice)
    context.user_data["_field_values_for_edit"] = {}
    
    # Використовуємо inline кнопки замість текстових, щоб уникнути обрізання
    buttons = []
    
    # Кнопка для редагування "Запит від"
    department = context.user_data.get("department", "—")
    buttons.append([InlineKeyboardButton(
        text=f"Запит від: {department}",
        callback_data="EDIT_FIELD:department"
    )])
    
    for idx, q in enumerate(QUESTIONS):
        field_value = context.user_data.get(q["key"], "—")
        # Показувати трохи більше для inline кнопок
        display_value = field_value[:40] + "…" if len(str(field_value)) > 40 else field_value
        button_text = f"{q['label']}: {display_value}"
        buttons.append([InlineKeyboardButton(
            text=button_text,
            callback_data=f"EDIT_FIELD:{idx}"  # Передаємо індекс питання
        )])
        # Зберегти повне значення для використання
        context.user_data["_field_values_for_edit"][f"q_{idx}"] = field_value
    
    buttons.append([InlineKeyboardButton(
        text="⬅️ Назад до підтвердження",
        callback_data="EDIT_CANCEL"
    )])
    
    keyboard = InlineKeyboardMarkup(buttons)
    
    await update.message.reply_text(
        "Оберіть поле для редагування:",
        reply_markup=keyboard
    )
    return EDIT


async def handle_edit_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка вибору поля для редагування (callback від inline кнопок)"""
    query = update.callback_query
    if not query:
        return EDIT
    
    await query.answer()
    
    data = query.data or ""
    if not data.startswith("EDIT_FIELD:"):
        if data == "EDIT_CANCEL":
            await query.edit_message_text("⬅️ Повернення до підтвердження...")
            return await ask_question(update, context)
        return EDIT
    
    # Парсимо callback_data
    field_identifier = data.split(":", 1)[1]
    
    # Обробити "Запит від"
    if field_identifier == "department":
        current_value = context.user_data.get("department", "—")
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(text="Тваринництво")], [KeyboardButton(text="Виробництво")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await query.edit_message_text(
            f"✏️ Редагування: Запит від\n\n"
            f"📌 Поточне значення:\n{current_value}\n\n"
            f"Оберіть нове значення:",
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="Оберіть відділ:",
            reply_markup=keyboard,
        )
        context.user_data["editing_department"] = True
        return DEPARTMENT
    
    # Обробити звичайні питання
    try:
        idx = int(field_identifier)
        if 0 <= idx < len(QUESTIONS):
            q = QUESTIONS[idx]
            current_value = context.user_data.get(q["key"], "—")
            
            context.user_data["question_index"] = idx
            context.user_data["editing_mode"] = True
            logging.info(f"Editing field {q['label']} (index {idx}), current value: {current_value[:50] if current_value else '—'}")
            
            # Показати поточне значення перед редагуванням
            await query.edit_message_text(
                f"✏️ Редагування: {q['label']}\n\n"
                f"📌 Поточне значення:\n{current_value}\n\n"
                f"Введіть нове значення нижче:"
            )
            
            # Створити fake update для ask_question
            class FakeMessage:
                def __init__(self, chat_id, user):
                    self.chat_id = chat_id
                    self.message_id = None
                    self.effective_user = user
                async def reply_text(self, *args, **kwargs):
                    return await context.bot.send_message(self.chat_id, *args, **kwargs)
            
            fake_update = type('obj', (object,), {
                'message': FakeMessage(query.message.chat_id, update.effective_user),
                'effective_user': update.effective_user,
                'callback_query': None
            })()
            
            return await ask_question(fake_update, context)
    except (ValueError, IndexError) as e:
        logging.error(f"Invalid field index: {field_identifier}, error: {e}")
    
    await query.answer("Помилка при виборі поля", show_alert=True)
    return EDIT


def _get_changes_text(original_data: dict, new_data: dict) -> str:
    """Створити текст з переліком змін між оригінальними та новими даними."""
    changes = []
    
    # Список полів для відстеження з їх описами
    field_labels = {
        "department": "🏷️ Запит від",
        "vehicle_type": "🚛 Тип авто",
        "initiator": "👤 Ініціатор",
        "company": "🏢 Підприємство",
        "cargo_type": "📦 Вантаж",
        "size_type": "📐 Габарит / негабарит",
        "big_bag_weight": "⚖️ Вага 1 біг-бегу",
        "volume": "📊 Обсяг",
        "notes": "💬 Примітки",
        "date_period": "📅 Дата / період",
        "load_city": "📍 Завантаження",
        "load_place": "🏭 Склад завантаження",
        "load_method": "⬆️ Спосіб завантаження",
        "unload_city": "📍 Розвантаження",
        "unload_place": "🏭 Склад розвантаження",
        "unload_method": "⬇️ Спосіб розвантаження",
        "load_contact": "📞 Контакт (завантаження)",
        "unload_contact": "📞 Контакт (розвантаження)",
    }

    def normalize_value(value: Any) -> str:
        if value is None:
            return "—"
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned if cleaned else "—"
        return str(value)
    
    for field, label in field_labels.items():
        old_val = normalize_value(original_data.get(field))
        new_val = normalize_value(new_data.get(field))
        
        if old_val != new_val:
            changes.append(f"{label}: {old_val} → {new_val}")
    
    if changes:
        return "✏️ Зміни:\n" + "\n".join(changes)
    else:
        return "✏️ Заявку відредаговано"


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    
    logging.info(f"confirm() called with text='{text}', is_request_edit={context.user_data.get('is_request_edit')}, request_id={context.user_data.get('request_id')}")

    # Швидка заявка - "Додати деталі"
    if text == "✏️ Додати деталі":
        context.user_data["quick_mode"] = False  # Виходимо зі швидкого режиму
        return await show_edit_fields(update, context)
    
    # Швидка заявка - "Надіслати"
    if text == "📤 Надіслати":
        # Надіслати заявку одразу (ті ж дії що й "ТАК")
        chat_id = os.getenv("TARGET_CHAT_ID")
        if not chat_id:
            await update.message.reply_text(
                "Не задано TARGET_CHAT_ID. Додайте змінну середовища.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return ConversationHandler.END

        request_id = context.user_data.get("request_id") or uuid.uuid4().hex[:8].upper()
        context.user_data["request_id"] = request_id
        application_text = _format_application(context.user_data)
        thread_id = context.user_data.get("thread_id")
        
        # Додаємо згадку користувача
        user = update.effective_user
        user_mention = f"@{user.username}" if user.username else user.full_name
        notification = f"📋 {user_mention} створив нову заявку:\n🆔 ID заявки: {request_id}\n\n{application_text}"
        
        # Надіслати в груповий чат та зберегти message_id
        logging.info(f"Quick request: sending {request_id} to chat {chat_id}, thread_id={thread_id}")
        try:
            message = await context.bot.send_message(
                chat_id=chat_id,
                text=notification,
                message_thread_id=thread_id,
            )
            message_id = message.message_id
            logging.info(f"✅ Quick request {request_id} sent successfully, message_id={message_id}")
        except Exception as send_error:
            logging.error(f"❌ Failed to send quick request {request_id}: {send_error}", exc_info=True)
            await update.message.reply_text(
                f"⚠️ Помилка відправки заявки в чат!\n"
                f"Спробуйте ще раз або зверніться до адміністратора.",
                reply_markup=ReplyKeyboardRemove()
            )
            return ConversationHandler.END
        
        # Зберегти заявку в БД з message_id
        try:
            db.save_request(
                request_id=request_id,
                user_id=user.id,
                request_data=context.user_data,
                message_id=message_id,
                thread_id=thread_id
            )
        except Exception as e:
            logging.error(f"Error saving request to database: {e}")
        
        # Експорт у Google Sheets
        export_success = False
        export_error = ""
        try:
            export_success, export_error = sheets.export_to_sheets(context.user_data)
            if not export_success:
                # Сповістити адміна про помилку
                admin_id = os.getenv("ADMIN_USER_ID")
                if admin_id:
                    user = update.effective_user
                    user_info = f"@{user.username}" if user.username else user.full_name
                    try:
                        await context.bot.send_message(
                            chat_id=admin_id,
                            text=f"⚠️ Помилка експорту в Google Sheets!\n\n"
                                 f"🆔 ID заявки: {request_id}\n"
                                 f"👤 Користувач: {user_info}\n"
                                 f"❌ Причина: {export_error or 'невідома'}\n"
                                 f"📄 Заявка надіслана в чат, але не експортована в таблицю.\n\n"
                                 f"Перевірте логи бота на Railway."
                        )
                    except Exception as notify_error:
                        logging.error(f"Failed to notify admin about export failure: {notify_error}")
        except Exception as e:
            logging.error(f"Failed to export to Google Sheets: {e}")
            # Сповістити адміна
            admin_id = os.getenv("ADMIN_USER_ID")
            if admin_id:
                user = update.effective_user
                user_info = f"@{user.username}" if user.username else user.full_name
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"❌ Критична помилка експорту!\n\n"
                             f"🆔 ID заявки: {request_id}\n"
                             f"👤 Користувач: {user_info}\n"
                             f"❌ Помилка: {str(e)[:200]}\n\n"
                             f"Перевірте логи Railway."
                    )
                except Exception as notify_error:
                    logging.error(f"Failed to notify admin about critical export error: {notify_error}")
        
        # Зберегти контакти для автозаповнення
        try:
            user_id = update.effective_user.id
            contacts_to_save = []
            load_contact = (context.user_data.get("load_contact") or "").strip()
            unload_contact = (context.user_data.get("unload_contact") or "").strip()
            if load_contact and load_contact != "—":
                contacts_to_save.append({
                    "type": "load",
                    "value": load_contact
                })
            if unload_contact and unload_contact != "—" and unload_contact != load_contact:
                contacts_to_save.append({
                    "type": "unload",
                    "value": unload_contact
                })
            if contacts_to_save:
                save_contacts(user_id, contacts_to_save)
                logging.info(f"Saved {len(contacts_to_save)} contacts for user {user_id}")
        except Exception as e:
            logging.error(f"Failed to save contacts: {e}")
        
        # Показати меню редагування/видалення в приватному чаті
        keyboard = ReplyKeyboardMarkup(
            [
                [KeyboardButton(text="✏️ Редагувати заявку")],
                [KeyboardButton(text="🗑️ Видалити заявку")],
                [KeyboardButton(text="💾 Зберегти як шаблон")],
                [KeyboardButton(text="✅ Готово")],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text(
            (
                f"✅ Заявку надіслано!\n"
                f"📊 Експорт у Google Sheets: {'успішно' if export_success else '❌ не вдалося'}\n"
                f"🆔 ID заявки: {request_id}\n\n"
                f"Що робити далі?"
            ),
            reply_markup=keyboard
        )
        
        # Зберегти ID заявки для наступного меню
        context.user_data["last_request_id"] = request_id
        
        return START

    if text.lower() == "✏️ редагувати поля":
        return await show_edit_fields(update, context)

    if text.lower() == "почати спочатку":
        context.user_data.clear()
        context.user_data["question_index"] = 0
        await update.message.reply_text("Заповнення скинуто. Починаємо спочатку.")
        return await ask_question(update, context)

    if text.lower() == "так":
        chat_id = os.getenv("TARGET_CHAT_ID")
        if not chat_id:
            await update.message.reply_text(
                "Не задано TARGET_CHAT_ID. Додайте змінну середовища.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return ConversationHandler.END

        request_id = context.user_data.get("request_id") or uuid.uuid4().hex[:8].upper()
        context.user_data["request_id"] = request_id
        application_text = _format_application(context.user_data)
        user = update.effective_user
        user_mention = f"@{user.username}" if user.username else user.full_name
        notification = f"📋 {user_mention} створив нову заявку:\n🆔 ID заявки: {request_id}\n\n{application_text}"
        
        # Перевірити чи це редагування існуючої заявки
        is_editing = context.user_data.get("is_request_edit", False)
        message_id = None
        
        if is_editing:
            thread_id = context.user_data.get("thread_id")
            original_data = context.user_data.get("original_request_data", {})
            changes_text = _get_changes_text(original_data, context.user_data) if original_data else ""
            chat_updated = False
            update_error = None

            # Отримати message_id з БД
            try:
                saved_request = db.get_request(request_id)
                if saved_request:
                    message_id = saved_request.get("message_id")
                    thread_id = saved_request.get("thread_id") or thread_id
                    
                    if message_id:
                        # Редагувати існуюче повідомлення в групі
                        await context.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=message_id,
                            text=notification,
                        )
                        logging.info(f"Updated message {message_id} for request {request_id}")
                        chat_updated = True
                    else:
                        update_error = f"Missing message_id for request {request_id}"
                        logging.error(update_error)
                else:
                    update_error = f"Request {request_id} not found in DB during edit"
                    logging.error(update_error)
            except Exception as e:
                update_error = str(e)
                logging.error(f"Error updating message: {e}")

            if changes_text:
                try:
                    change_notice = f"🆔 Заявка {request_id}\n{changes_text}"
                    send_kwargs = {
                        "chat_id": chat_id,
                        "text": change_notice,
                    }
                    if thread_id is not None:
                        send_kwargs["message_thread_id"] = thread_id
                    if message_id:
                        send_kwargs["reply_to_message_id"] = message_id
                    await context.bot.send_message(**send_kwargs)
                except Exception as e:
                    logging.error(f"Failed to send changes notification: {e}")

            if not chat_updated:
                await update.message.reply_text(
                    "⚠️ Зміни збережено в БД та таблиці, але початкове повідомлення в групі не вдалося оновити. "
                    "Повну заявку повторно в чат не надсилав."
                )
        else:
            # Нова заявка - надіслати нове повідомлення
            thread_id = context.user_data.get("thread_id")
            department = context.user_data.get("department")
            logging.info(f"Sending new request {request_id} to chat {chat_id}, department={department}, thread_id={thread_id}")
            logging.debug(f"Request notification text (first 200 chars): {notification[:200]}")
            try:
                message = await context.bot.send_message(
                    chat_id=chat_id,
                    text=notification,
                    message_thread_id=thread_id,
                )
                message_id = message.message_id
                logging.info(f"✅ Successfully sent request {request_id} to chat, message_id={message_id}")
            except Exception as send_error:
                logging.error(f"❌ Failed to send request {request_id} to chat: {send_error}", exc_info=True)
                # Спробувати надіслати без thread_id
                try:
                    logging.info(f"Attempting to send without thread_id for request {request_id}")
                    message = await context.bot.send_message(
                        chat_id=chat_id,
                        text=notification,
                    )
                    message_id = message.message_id
                    logging.info(f"✅ Sent request {request_id} to chat without thread_id, message_id={message_id}")
                except Exception as fallback_error:
                    logging.error(f"❌ Failed to send request {request_id} even without thread_id: {fallback_error}", exc_info=True)
                    # Повідомити користувача про помилку
                    await update.message.reply_text(
                        f"⚠️ Помилка відправки заявки в чат!\n"
                        f"Заявка збережена в БД та експортована в Sheets, але не надіслана в груповий чат.\n"
                        f"Зверніться до адміністратора.",
                        reply_markup=ReplyKeyboardRemove()
                    )
                    return ConversationHandler.END
            
            # Зберегти нову заявку в БД
            try:
                db.save_request(
                    request_id=request_id,
                    user_id=user.id,
                    request_data=context.user_data,
                    message_id=message_id,
                    thread_id=thread_id
                )
                logging.info(f"Saved request {request_id} to database")
            except Exception as e:
                logging.error(f"Error saving request to database: {e}")
        
        # Оновити дані в БД (для редагування)
        if is_editing:
            try:
                db.update_request_data(request_id, context.user_data)
                logging.info(f"Updated request data for {request_id}")
            except Exception as e:
                logging.error(f"Error updating request data: {e}")
        
        # Експорт у Google Sheets
        export_success = False
        export_error = ""
        try:
            export_success, export_error = sheets.export_to_sheets(context.user_data)
            if not export_success:
                # Сповістити адміна про помилку
                admin_id = os.getenv("ADMIN_USER_ID")
                if admin_id:
                    user_info = f"@{user.username}" if user.username else user.full_name
                    try:
                        await context.bot.send_message(
                            chat_id=admin_id,
                            text=f"⚠️ Помилка експорту в Google Sheets!\n\n"
                                 f"🆔 ID заявки: {request_id}\n"
                                 f"👤 Користувач: {user_info}\n"
                                 f"❌ Причина: {export_error or 'невідома'}\n"
                                 f"📄 Заявка надіслана в чат, але не експортована в таблицю.\n\n"
                                 f"Перевірте логи бота на Railway."
                        )
                    except Exception as notify_error:
                        logging.error(f"Failed to notify admin about export failure: {notify_error}")
        except Exception as e:
            logging.error(f"Failed to export to Google Sheets: {e}")
            # Сповістити адміна
            admin_id = os.getenv("ADMIN_USER_ID")
            if admin_id:
                user_info = f"@{user.username}" if user.username else user.full_name
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=f"❌ Критична помилка експорту!\n\n"
                             f"🆔 ID заявки: {request_id}\n"
                             f"👤 Користувач: {user_info}\n"
                             f"❌ Помилка: {str(e)[:200]}\n\n"
                             f"Перевірте логи Railway."
                    )
                except Exception as notify_error:
                    logging.error(f"Failed to notify admin about critical export error: {notify_error}")
        
        # Зберегти контакти для автозаповнення
        try:
            user_id = update.effective_user.id
            contacts_to_save = []
            if context.user_data.get("load_contact") and context.user_data["load_contact"] != "—":
                contacts_to_save.append({
                    "type": "load",
                    "value": context.user_data["load_contact"]
                })
            if context.user_data.get("unload_contact") and context.user_data["unload_contact"] != "—":
                contacts_to_save.append({
                    "type": "unload",
                    "value": context.user_data["unload_contact"]
                })
            if contacts_to_save:
                save_contacts(user_id, contacts_to_save)
                logging.info(f"Saved {len(contacts_to_save)} contacts for user {user_id}")
        except Exception as e:
            logging.error(f"Failed to save contacts: {e}")
        
        # Якщо це був режим редагування - показати меню повернення до редагування
        if is_editing:
            keyboard = ReplyKeyboardMarkup(
                [
                    [KeyboardButton(text="✏️ Редагувати заявку")],
                    [KeyboardButton(text="🗑️ Видалити заявку")],
                    [KeyboardButton(text="💾 Зберегти як шаблон")],
                    [KeyboardButton(text="✅ Готово")],
                ],
                resize_keyboard=True,
                one_time_keyboard=True,
            )
            await update.message.reply_text(
                (
                    f"✅ Заявку оновлено!\n"
                    f"📊 Експорт у Google Sheets: {'успішно' if export_success else '❌ не вдалося'}\n"
                    f"🆔 ID заявки: {request_id}\n\n"
                    f"Що робити далі?"
                ),
                reply_markup=keyboard
            )
            context.user_data["last_request_id"] = request_id
            context.user_data.pop("editing_mode", None)
            context.user_data.pop("is_request_edit", None)
            return START
        
        # Для нових заявок - також показати меню редагування/видалення
        keyboard = ReplyKeyboardMarkup(
            [
                [KeyboardButton(text="✏️ Редагувати заявку")],
                [KeyboardButton(text="🗑️ Видалити заявку")],
                [KeyboardButton(text="💾 Зберегти як шаблон")],
                [KeyboardButton(text="✅ Готово")],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text(
            (
                f"✅ Заявку надіслано!\n"
                f"📊 Експорт у Google Sheets: {'успішно' if export_success else '❌ не вдалося'}\n"
                f"🆔 ID заявки: {request_id}\n\n"
                f"Що робити далі?"
            ),
            reply_markup=keyboard
        )
        
        # Зберегти ID заявки для наступного меню
        context.user_data["last_request_id"] = request_id
        
        return START

    await update.message.reply_text("Будь ласка, оберіть ТАК або Почати спочатку.")
    return CONFIRM


async def handle_save_template_response(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка відповіді щодо збереження шаблону після відправлення заявки"""
    text = (update.message.text or "").strip()
    
    if text == "💾 Зберегти як шаблон":
        await update.message.reply_text(
            "Як назвати цей шаблон?",
            reply_markup=ReplyKeyboardRemove()
        )
        return SAVE_TEMPLATE_NAME
    elif text == "📝 Нова заявка":
        context.user_data.clear()
        return await show_start_menu(update, context)
    else:
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(text="💾 Зберегти як шаблон")], [KeyboardButton(text="📝 Нова заявка")]],
            resize_keyboard=True,
        )
        await update.message.reply_text(
            "Оберіть опцію:",
            reply_markup=keyboard
        )
        return SAVE_TEMPLATE_CONFIRM


async def handle_save_template_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка введення імені шаблону"""
    template_name = (update.message.text or "").strip()
    
    if not template_name:
        await update.message.reply_text("Назва не може бути порожною. Спробуйте ще раз:")
        return SAVE_TEMPLATE_NAME
    
    user_id = update.effective_user.id
    
    # Зберегти шаблон (лише стабільні поля)
    allowed_keys = {q["key"] for q in QUESTIONS} | {
        "department",
        "thread_id",
        "quick_mode",
        "date_type",
    }
    
    logging.info(f"Starting template save process for user {user_id}, template name: '{template_name}'")
    logging.info(f"Available context.user_data keys: {list(context.user_data.keys())}")
    logging.info(f"Allowed keys: {sorted(allowed_keys)}")
    
    # Конвертувати всі дані в JSON-сумісний формат
    template_data = {}
    for k, v in context.user_data.items():
        if k in allowed_keys:
            # Конвертувати в строку якщо це не базовий тип
            if v is None:
                template_data[k] = None
            elif isinstance(v, (str, int, float, bool)):
                template_data[k] = v
            elif isinstance(v, list):
                template_data[k] = [str(item) if item is not None else None for item in v]
            elif isinstance(v, dict):
                template_data[k] = {str(key): str(val) if val is not None else None for key, val in v.items()}
            else:
                template_data[k] = str(v)
    
    logging.info(f"Prepared template_data with {len(template_data)} keys: {list(template_data.keys())}")
    logging.debug(f"Template data content: {template_data}")
    
    success = db.save_template(user_id, template_name, template_data)
    
    if success:
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(text="📝 Нова заявка")]],
            resize_keyboard=True,
        )
        await update.message.reply_text(
            f"✅ Шаблон '{template_name}' збережено!",
            reply_markup=keyboard
        )
    else:
        await update.message.reply_text(
            "❌ Помилка при збереженні шаблону. Перевірте логи бота для деталей.",
            reply_markup=ReplyKeyboardRemove()
        )
        logging.error(f"Failed to save template. User data keys: {list(context.user_data.keys())}")
        logging.error(f"Allowed keys: {allowed_keys}")
        logging.error(f"Template data to save: {template_data}")
        return await show_start_menu(update, context)
    
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()

    if not _is_private_chat(update):
        await update.message.reply_text(
            "Заповнення скасовано.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END
    
    # Показати кнопку для нової заявки в приватному чаті
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton(text="📝 Зробити заявку")]],
        resize_keyboard=True,
    )
    await update.message.reply_text(
        "Заповнення скасовано. Натисніть кнопку нижче, щоб почати нову заявку.",
        reply_markup=keyboard
    )
    
    return ConversationHandler.END


async def request_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Працює лише в групах
    if update.message.chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(
            "Ця команда працює лише в групах. Для створення заявки натисніть /start"
        )
        return
    
    bot_username = os.getenv("BOT_USERNAME")
    if not bot_username:
        await update.message.reply_text("Не задано BOT_USERNAME.")
        return

    deep_link = f"https://t.me/{bot_username}?start=apply"
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(text="📝 Зробити заявку", url=deep_link)]]
    )
    
    msg = await update.message.reply_text(
        "👇 Натисніть кнопку для створення заявки на перевезення:",
        reply_markup=keyboard
    )
    # Спроба закріпити (потрібні права адміна у бота)
    try:
        await context.bot.pin_chat_message(
            chat_id=update.message.chat_id,
            message_id=msg.message_id,
            disable_notification=True
        )
    except Exception as e:
        logging.warning(f"Не вдалося закріпити повідомлення: {e}")


async def delete_request_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Позначити заявку як ВИДАЛЕНО в Google Sheets за ID заявки."""
    if not context.args:
        await update.message.reply_text(
            "Використання: /delete_request <ID_заявки>\n"
            "Приклад: /delete_request A1B2C3D4"
        )
        return

    request_id = context.args[0].strip().upper()
    user = update.effective_user
    deleted_by = f"@{user.username}" if user and user.username else (user.full_name if user else "Unknown")

    success, message = sheets.mark_request_deleted(request_id, deleted_by=deleted_by)
    if success:
        await update.message.reply_text(f"✅ {message}")
    else:
        await update.message.reply_text(f"❌ {message}")


async def restore_request_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Відновити заявку (змінити статус на АКТИВНА) в Google Sheets за ID заявки."""
    if not context.args:
        await update.message.reply_text(
            "Використання: /restore_request <ID_заявки>\n"
            "Приклад: /restore_request A1B2C3D4"
        )
        return

    request_id = context.args[0].strip().upper()
    user = update.effective_user
    restored_by = f"@{user.username}" if user and user.username else (user.full_name if user else "Unknown")

    success, message = sheets.restore_request(request_id, restored_by=restored_by)
    if success:
        await update.message.reply_text(f"✅ {message}")
    else:
        await update.message.reply_text(f"❌ {message}")


PAGE_SIZE = 3


async def _send_requests_page(
    send_func,
    request_entries: list,
    offset: int,
    user_id: int,
    total: int,
) -> None:
    """Надіслати одну сторінку заявок з кнопками керування."""
    page = request_entries[offset:offset + PAGE_SIZE]
    for req in page:
        rid = req.get("display_request_id", req.get("request_id", "—"))
        parent_rid = req.get("parent_request_id", req.get("request_id", "—"))
        status = req.get("status", "—")
        created_at = req.get("created_at")
        created_str = created_at.strftime("%d.%m %H:%M") if created_at else "—"
        data = req.get("request_data") or {}
        initiator = data.get("initiator", "—")
        cargo = data.get("cargo_type", "—")
        load_city = data.get("load_city", "—")
        unload_city = data.get("unload_city", "—")
        volume_text = "—"
        volume_value = _to_float(data.get("volume"))
        if volume_value is not None:
            volume_text = _format_volume_for_size(data.get("size_type", ""), volume_value)

        split_suffix = "\n  🔗 Частина заявки: {0}".format(parent_rid) if req.get("is_split_part") else ""
        info_text = (
            f"• {rid} | {status} | {created_str}\n"
            f"  👤 Ініціатор: {initiator}\n"
            f"  📦 Вантаж: {cargo}\n"
            f"  ⚖️ Обсяг: {volume_text}\n"
            f"  📍 Маршрут: {load_city} → {unload_city}"
            f"{split_suffix}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(text="✏️ Редагувати", callback_data=f"REQACT:EDIT:{rid}"),
                InlineKeyboardButton(text="🗑️ Видалити", callback_data=f"REQACT:DEL:{rid}"),
            ],
            [
                InlineKeyboardButton(text="📋 Копія заявки", callback_data=f"REQACT:COPY:{parent_rid}"),
            ]
        ])
        await send_func(info_text, reply_markup=keyboard)

    next_offset = offset + PAGE_SIZE
    if next_offset < total:
        remaining = total - next_offset
        nav_keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                text=f"Ще заявки ↓ (залишилось {remaining})",
                callback_data=f"REQACT:PAGE:{next_offset}"
            )
        ]])
        await send_func(
            f"Показано {min(offset + PAGE_SIZE, total)} з {total} заявок.",
            reply_markup=nav_keyboard
        )


async def my_requests_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показати останні заявки користувача з ID для швидкого редагування."""
    user = update.effective_user
    if not user:
        return START

    requests = db.get_user_requests(user.id, limit=50)
    if not requests:
        await update.message.reply_text(
            "У вас ще немає збережених заявок.",
            reply_markup=ReplyKeyboardRemove()
        )
        return START

    entries = _build_request_display_entries(requests)
    total = len(entries)
    context.user_data["my_requests_cache"] = entries
    await update.message.reply_text(
        f"Ваші заявки (всього {total}):",
        reply_markup=ReplyKeyboardRemove()
    )
    await _send_requests_page(
        send_func=update.message.reply_text,
        request_entries=entries,
        offset=0,
        user_id=user.id,
        total=total,
    )
    return ConversationHandler.END


def _build_child_edit_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton(text="📍 Змінити НП розвантаження")],
            [KeyboardButton(text="⚖️ Змінити обсяг точки")],
            [KeyboardButton(text="✅ Зберегти зміни")],
            [KeyboardButton(text="❌ Скасувати")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


async def _persist_child_edit_and_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    parent_request_id = context.user_data.get("child_edit_parent_id")
    child_items = context.user_data.get("child_edit_items") or []
    if not parent_request_id or not child_items:
        await update.message.reply_text("❌ Дані редагування втрачені")
        return START

    request = db.get_request(parent_request_id)
    if not request:
        await update.message.reply_text("❌ Батьківську заявку не знайдено")
        return START

    request_data = request.get("request_data") or {}
    if not isinstance(request_data, dict):
        await update.message.reply_text("❌ Пошкоджені дані заявки")
        return START

    request_data["request_id"] = parent_request_id
    unload_city, distribution, ids_map, total = _rebuild_distribution_from_items(child_items)
    request_data["unload_city"] = unload_city
    request_data["unload_distribution"] = distribution
    request_data["unload_distribution_ids"] = ids_map
    request_data["volume"] = _format_number(total)
    if len(_split_location_values(unload_city)) > 1:
        request_data["unload_place"] = "—"

    db.update_request_data(parent_request_id, request_data)

    chat_id = os.getenv("TARGET_CHAT_ID")
    user = update.effective_user
    user_mention = f"@{user.username}" if user and user.username else (user.full_name if user else "Користувач")
    notification = f"📋 {user_mention} створив нову заявку:\n🆔 ID заявки: {parent_request_id}\n\n{_format_application(request_data)}"

    try:
        if request.get("message_id") and chat_id:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=request.get("message_id"),
                text=notification,
            )
    except Exception as e:
        logging.error(f"Failed to update parent message after child edit: {e}")

    try:
        sheets.export_to_sheets(request_data)
    except Exception as e:
        logging.error(f"Failed to export child edit to sheets: {e}")

    for key in [
        "child_edit_parent_id",
        "child_edit_child_id",
        "child_edit_items",
    ]:
        context.user_data.pop(key, None)

    await update.message.reply_text("✅ Зміни підзаявки збережено", reply_markup=ReplyKeyboardRemove())
    return await show_start_menu(update, context)


async def handle_child_edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    child_id = context.user_data.get("child_edit_child_id")
    child_items = context.user_data.get("child_edit_items") or []
    target = next((item for item in child_items if item.get("child_id") == child_id), None)

    if text == "❌ Скасувати":
        for key in ["child_edit_parent_id", "child_edit_child_id", "child_edit_items"]:
            context.user_data.pop(key, None)
        await update.message.reply_text("❎ Редагування підзаявки скасовано", reply_markup=ReplyKeyboardRemove())
        return await show_start_menu(update, context)

    if not target:
        await update.message.reply_text("❌ Не знайдено підзаявку для редагування")
        return START

    if text == "📍 Змінити НП розвантаження":
        await update.message.reply_text(
            f"Поточний НП: {target.get('city')}\nВведіть новий НП розвантаження:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return CHILD_EDIT_CITY

    if text == "⚖️ Змінити обсяг точки":
        size_type = context.user_data.get("size_type", "")
        await update.message.reply_text(
            f"Поточний обсяг: {_format_volume_for_size(size_type, float(target.get('amount', 0.0)))}\n"
            f"Введіть новий обсяг (тільки число):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return CHILD_EDIT_VOLUME

    if text == "✅ Зберегти зміни":
        return await _persist_child_edit_and_sync(update, context)

    await update.message.reply_text("Оберіть дію з меню.", reply_markup=_build_child_edit_menu_keyboard())
    return CHILD_EDIT_MENU


async def handle_child_edit_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    new_city = (update.message.text or "").strip()
    if not new_city:
        await update.message.reply_text("❌ Введіть назву НП")
        return CHILD_EDIT_CITY

    child_id = context.user_data.get("child_edit_child_id")
    items = context.user_data.get("child_edit_items") or []
    target = next((item for item in items if item.get("child_id") == child_id), None)
    if not target:
        await update.message.reply_text("❌ Не знайдено підзаявку")
        return START

    duplicate = next((item for item in items if item.get("child_id") != child_id and item.get("city") == new_city), None)
    if duplicate:
        await update.message.reply_text("❌ Такий НП вже є в списку розвантаження. Вкажіть інший.")
        return CHILD_EDIT_CITY

    target["city"] = new_city
    context.user_data["child_edit_items"] = items
    await update.message.reply_text("✅ НП оновлено", reply_markup=_build_child_edit_menu_keyboard())
    return CHILD_EDIT_MENU


async def handle_child_edit_volume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    value = _to_float((update.message.text or "").strip())
    if value is None or value <= 0:
        await update.message.reply_text("❌ Введіть коректне число більше 0")
        return CHILD_EDIT_VOLUME

    child_id = context.user_data.get("child_edit_child_id")
    items = context.user_data.get("child_edit_items") or []
    target = next((item for item in items if item.get("child_id") == child_id), None)
    if not target:
        await update.message.reply_text("❌ Не знайдено підзаявку")
        return START

    target["amount"] = float(value)
    context.user_data["child_edit_items"] = items
    await update.message.reply_text("✅ Обсяг точки оновлено", reply_markup=_build_child_edit_menu_keyboard())
    return CHILD_EDIT_MENU


async def handle_request_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка кнопок дій під заявкою (редагувати/видалити)."""
    query = update.callback_query
    if not query:
        return START

    await query.answer()
    payload = query.data or ""
    parts = payload.split(":", 2)
    if len(parts) != 3:
        await query.answer("Невірна дія", show_alert=True)
        return START

    _, action, arg = parts

    # Пагінація заявок
    if action == "PAGE":
        user = update.effective_user
        if not user:
            return START
        try:
            offset = int(arg)
        except ValueError:
            return START
        request_entries = context.user_data.get("my_requests_cache")
        if not request_entries:
            requests = db.get_user_requests(user.id, limit=50)
            request_entries = _build_request_display_entries(requests)
            context.user_data["my_requests_cache"] = request_entries
        total = len(request_entries)
        await _send_requests_page(
            send_func=query.message.reply_text,
            request_entries=request_entries,
            offset=offset,
            user_id=user.id,
            total=total,
        )
        return ConversationHandler.END

    request_id = arg.strip().upper()
    parsed_child = _parse_child_request_id(request_id)
    parent_request_id = parsed_child[0] if parsed_child else _resolve_parent_request_id(request_id)
    is_child_action = parsed_child is not None

    user = update.effective_user
    request = db.get_request(request_id)
    if not request and parent_request_id != request_id:
        request = db.get_request(parent_request_id)
    if not request:
        await query.answer("Заявку не знайдено", show_alert=True)
        return START

    if int(request.get("user_id", 0)) != int(user.id if user else 0) and not _is_admin_user(user):
        await query.answer("Можна керувати тільки власними заявками", show_alert=True)
        return START

    if action == "EDIT":
        if (request.get("status") or "").strip().upper() == "ВИДАЛЕНО":
            await query.answer("Заявка видалена. Спочатку відновіть її.", show_alert=True)
            return START

        request_data = request.get("request_data") or {}
        if not isinstance(request_data, dict):
            await query.answer("Дані заявки пошкоджені", show_alert=True)
            return START

        if is_child_action:
            child_items = _build_child_items_from_data(parent_request_id, request_data)
            target = next((item for item in child_items if item.get("child_id") == request_id), None)
            if not target:
                await query.answer("Підзаявку не знайдено", show_alert=True)
                return START

            context.user_data["child_edit_parent_id"] = parent_request_id
            context.user_data["child_edit_child_id"] = request_id
            context.user_data["child_edit_items"] = child_items

            await query.message.reply_text(
                f"✏️ Редагування підзаявки {request_id}\n"
                f"НП: {target.get('city')}\n"
                f"Обсяг: {_format_number(float(target.get('amount', 0.0)))}",
                reply_markup=_build_child_edit_menu_keyboard(),
            )
            return CHILD_EDIT_MENU

        context.user_data.clear()
        context.user_data.update(request_data)
        context.user_data["request_id"] = parent_request_id
        context.user_data["last_request_id"] = parent_request_id
        context.user_data["editing_mode"] = True
        context.user_data["is_request_edit"] = True
        # Зберегти оригінальні дані для порівняння після редагування
        context.user_data["original_request_data"] = dict(request_data)

        await query.message.reply_text(f"✏️ Відкрив заявку {parent_request_id} для редагування")
        fake_update = type('obj', (object,), {'message': query.message, 'effective_user': update.effective_user})()
        return await show_edit_fields(fake_update, context)

    if action == "DEL":
        if is_child_action:
            request_data = request.get("request_data") or {}
            if not isinstance(request_data, dict):
                await query.answer("Дані заявки пошкоджені", show_alert=True)
                return START

            child_items = _build_child_items_from_data(parent_request_id, request_data)
            before_count = len(child_items)
            child_items = [item for item in child_items if item.get("child_id") != request_id]
            if len(child_items) == before_count:
                await query.answer("Підзаявку не знайдено", show_alert=True)
                return START

            if not child_items:
                db.mark_request_as_deleted(parent_request_id)

                if request.get("message_id"):
                    chat_id = os.getenv("TARGET_CHAT_ID")
                    try:
                        await context.bot.delete_message(
                            chat_id=chat_id,
                            message_id=request["message_id"]
                        )
                    except Exception as e:
                        logging.error(f"Failed to delete group message for {parent_request_id}: {e}")

                try:
                    sheets.mark_request_deleted_exact(request_id)
                except Exception as e:
                    logging.error(f"Failed to mark child request as deleted in sheets: {e}")

                try:
                    await query.edit_message_reply_markup(reply_markup=None)
                except Exception:
                    pass

                await query.message.reply_text(f"✅ Підзаявку {request_id} видалено")
                fake_update = type('obj', (object,), {'message': query.message, 'effective_user': update.effective_user})()
                return await show_start_menu(fake_update, context)

            unload_city, distribution, ids_map, total = _rebuild_distribution_from_items(child_items)
            request_data["request_id"] = parent_request_id
            request_data["unload_city"] = unload_city
            request_data["unload_distribution"] = distribution
            request_data["unload_distribution_ids"] = ids_map
            request_data["volume"] = _format_number(total)
            if len(_split_location_values(unload_city)) > 1:
                request_data["unload_place"] = "—"
            db.update_request_data(parent_request_id, request_data)

            chat_id = os.getenv("TARGET_CHAT_ID")
            user_mention = f"@{user.username}" if user and user.username else (user.full_name if user else "Користувач")
            notification = f"📋 {user_mention} створив нову заявку:\n🆔 ID заявки: {parent_request_id}\n\n{_format_application(request_data)}"
            try:
                if request.get("message_id") and chat_id:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=request.get("message_id"),
                        text=notification,
                    )
            except Exception as e:
                logging.error(f"Failed to update parent message after child delete: {e}")

            try:
                sheets.mark_request_deleted_exact(request_id)
            except Exception as e:
                logging.error(f"Failed to mark child request as deleted in sheets: {e}")
            try:
                sheets.export_to_sheets(request_data)
            except Exception as e:
                logging.error(f"Failed to sync parent request after child delete: {e}")

            await query.message.reply_text(f"✅ Підзаявку {request_id} видалено")
            fake_update = type('obj', (object,), {'message': query.message, 'effective_user': update.effective_user})()
            return await show_start_menu(fake_update, context)

        db.mark_request_as_deleted(parent_request_id)

        if request.get("message_id"):
            chat_id = os.getenv("TARGET_CHAT_ID")
            try:
                await context.bot.delete_message(
                    chat_id=chat_id,
                    message_id=request["message_id"]
                )
            except Exception as e:
                logging.error(f"Failed to delete group message for {parent_request_id}: {e}")

        try:
            sheets.mark_request_deleted(parent_request_id)
        except Exception as e:
            logging.error(f"Failed to mark request as deleted in sheets: {e}")

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        await query.message.reply_text(f"✅ Заявку {parent_request_id} видалено")
        
        # Показати меню для подальших дій
        context.user_data.clear()
        fake_update = type('obj', (object,), {'message': query.message, 'effective_user': update.effective_user})()
        return await show_start_menu(fake_update, context)

    if action == "COPY":
        # Створити копію заявки з новим ID
        request_data = request.get("request_data") or {}
        if not isinstance(request_data, dict):
            await query.answer("Дані заявки пошкоджені", show_alert=True)
            return START

        user = update.effective_user
        if not user:
            await query.answer("Не вдалося отримати дані користувача", show_alert=True)
            return START

        # Згенерувати новий ID для копії
        new_request_id = uuid.uuid4().hex[:8].upper()
        
        # Очистити context і завантажити дані
        context.user_data.clear()
        context.user_data.update(request_data)
        context.user_data["request_id"] = new_request_id
        context.user_data["last_request_id"] = new_request_id
        # Явно встановлюємо що це НЕ редагування - це нова заявка (копія)
        context.user_data["is_request_edit"] = False
        context.user_data.pop("editing_mode", None)
        context.user_data.pop("original_request_data", None)
        
        # Перевірка критичних полів
        copied_thread_id = context.user_data.get("thread_id")
        copied_department = context.user_data.get("department")
        logging.info(f"Created copy of request {request_id} with new ID {new_request_id}, department={copied_department}, thread_id={copied_thread_id}")
        
        if not copied_department:
            logging.warning(f"Copy request {new_request_id}: department not found, setting default")
            # Встановити за замовчуванням якщо немає
            context.user_data["department"] = "Виробництво"
            context.user_data["thread_id"] = THREAD_IDS.get("Виробництво")
            logging.info(f"Set default department=Виробництво, thread_id={context.user_data['thread_id']}")
        elif copied_thread_id is None:
            # Якщо department є, але thread_id немає - встановити на основі department
            logging.warning(f"Copy request {new_request_id}: thread_id is None, setting based on department={copied_department}")
            context.user_data["thread_id"] = THREAD_IDS.get(copied_department)
            logging.info(f"Set thread_id={context.user_data['thread_id']} based on department={copied_department}")
        
        try:
            # Відправити копію у прива тний чат користувача
            msg = await context.bot.send_message(
                chat_id=user.id,
                text=f"📋 Створено копію заявки {request_id}\n"
                     f"🆔 Новий ID заявки: {new_request_id}\n\n"
                     f"Дані завантажені. Перевірте та підтвердіть:"
            )
            
            # Показати меню підтвердження для копійованої заявки
            application_text = _format_application(context.user_data)
            keyboard = ReplyKeyboardMarkup(
                [[KeyboardButton(text="ТАК")], [KeyboardButton(text="✏️ Редагувати поля")]],
                resize_keyboard=True,
                one_time_keyboard=True,
            )
            await context.bot.send_message(
                chat_id=user.id,
                text="Перевірте заявку:\n\n" + application_text + "\n\nНадіслати заявку в чат?",
                reply_markup=keyboard,
            )
            
            await query.answer("📋 Копія відправлена у приватний чат")
        except Exception as e:
            logging.error(f"Failed to send copy to user DM: {e}")
            await query.answer("❌ Не вдалося відправити копію", show_alert=True)
            return START
        
        return CONFIRM

    await query.answer("Невідома дія", show_alert=True)
    return START


async def edit_request_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Відкрити конкретну заявку за ID у режимі редагування."""
    if not context.args:
        await update.message.reply_text(
            "Використання: /edit_request <ID_заявки>\n"
            "Спочатку можете подивитися ID через /my_requests"
        )
        return START

    request_id = context.args[0].strip().upper()
    return await _open_request_for_edit_by_id(update, context, request_id)


async def _open_request_for_edit_by_id(update: Update, context: ContextTypes.DEFAULT_TYPE, request_id: str) -> int:
    """Open request by ID in edit mode (supports parent and child IDs)."""
    if not request_id:
        await update.message.reply_text("❌ Вкажіть ID заявки")
        return START

    user = update.effective_user
    if not user:
        return START

    request_id = request_id.strip().upper()
    parsed_child = _parse_child_request_id(request_id)
    parent_request_id = parsed_child[0] if parsed_child else _resolve_parent_request_id(request_id)
    is_child_action = parsed_child is not None
    request = db.get_request(request_id)
    if not request and parent_request_id != request_id:
        request = db.get_request(parent_request_id)
    if not request:
        await update.message.reply_text(f"❌ Заявку з ID {request_id} не знайдено")
        return START

    if int(request.get("user_id", 0)) != int(user.id) and not _is_admin_user(user):
        await update.message.reply_text("❌ Ви можете редагувати лише власні заявки")
        return START

    if (request.get("status") or "").strip().upper() == "ВИДАЛЕНО":
        await update.message.reply_text("❌ Цю заявку позначено як ВИДАЛЕНО. Відновіть її командою /restore_request <ID>.")
        return START

    request_data = request.get("request_data") or {}
    if not isinstance(request_data, dict):
        await update.message.reply_text("❌ Не вдалося прочитати дані заявки для редагування")
        return START

    if is_child_action:
        child_items = _build_child_items_from_data(parent_request_id, request_data)
        target = next((item for item in child_items if item.get("child_id") == request_id), None)
        if not target:
            await update.message.reply_text(f"❌ Підзаявку з ID {request_id} не знайдено")
            return START

        context.user_data["child_edit_parent_id"] = parent_request_id
        context.user_data["child_edit_child_id"] = request_id
        context.user_data["child_edit_items"] = child_items
        await update.message.reply_text(
            f"✏️ Відкрив підзаявку {request_id} для редагування\n"
            f"НП: {target.get('city')}\n"
            f"Обсяг: {_format_number(float(target.get('amount', 0.0)))}",
            reply_markup=_build_child_edit_menu_keyboard(),
        )
        return CHILD_EDIT_MENU

    context.user_data.clear()
    context.user_data.update(request_data)
    context.user_data["request_id"] = parent_request_id
    context.user_data["last_request_id"] = parent_request_id
    context.user_data["editing_mode"] = True
    context.user_data["is_request_edit"] = True
    # Зберегти оригінальні дані для порівняння після редагування
    context.user_data["original_request_data"] = dict(request_data)

    await update.message.reply_text(f"✏️ Відкрив заявку {request_id} для редагування")
    return await show_edit_fields(update, context)


async def handle_find_request_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle request ID entered from start-menu search button."""
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("❌ Введіть ID заявки")
        return FIND_REQUEST_EDIT

    if text == "⬅️ Назад":
        return await show_start_menu(update, context)

    return await _open_request_for_edit_by_id(update, context, text)


async def handle_make_request_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробка кнопки 📝 Зробити заявку поза ConversationHandler"""
    if update.message.text == "📝 Зробити заявку":
        await start(update, context)


def build_app() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    app = Application.builder().token(token).build()

    # Ініціалізувати БД
    db.init_db()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("my_requests", my_requests_command),
            CommandHandler("edit_request", edit_request_command),
            CallbackQueryHandler(handle_request_action_callback, pattern=r"^REQACT:"),
            CallbackQueryHandler(handle_summary_calendar, pattern=r"^SUMCAL:"),
            CallbackQueryHandler(handle_summary_action_callback, pattern=r"^SUM(EDIT|DELASK|DELYES|DELNO|PAGE|DEPT|TOGGLEACTIVE|EXP|DATE)"),
            MessageHandler(filters.Regex("^📝 Зробити заявку$"), start),
        ],
        states={
            START: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_start_menu_choice),
                CallbackQueryHandler(handle_request_action_callback, pattern=r"^REQACT:"),
                CallbackQueryHandler(handle_summary_calendar, pattern=r"^SUMCAL:"),
                CallbackQueryHandler(handle_summary_action_callback, pattern=r"^SUM(EDIT|DELASK|DELYES|DELNO|PAGE|DEPT|TOGGLEACTIVE|EXP|DATE)"),
            ],
            LOAD_TEMPLATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_start_menu_choice),
                CommandHandler("cancel", cancel),
            ],
            TEMPLATE_SELECT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_template_select),
                CommandHandler("cancel", cancel),
            ],
            DELETE_TEMPLATE_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_delete_template_confirm),
                CommandHandler("cancel", cancel),
            ],
            DEPARTMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_department),
                CommandHandler("cancel", cancel),
            ],
            QUESTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer),
                CommandHandler("cancel", cancel),
            ],
            CUSTOM_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_input),
                CommandHandler("cancel", cancel),
            ],
            CROP_TYPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_crop_type),
                CommandHandler("cancel", cancel),
            ],
            DATE_TYPE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date_type),
                CommandHandler("cancel", cancel),
            ],
            DATE_CALENDAR: [
                CallbackQueryHandler(handle_calendar),
                CommandHandler("cancel", cancel),
            ],
            DATE_PERIOD_END: [
                CallbackQueryHandler(handle_period_end),
                CommandHandler("cancel", cancel),
            ],
            CITY_SEARCH_LOAD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_city_search_load),
                CommandHandler("cancel", cancel),
            ],
            CITY_SELECT_LOAD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_city_select_load),
                CommandHandler("cancel", cancel),
            ],
            CITY_SEARCH_UNLOAD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_city_search_unload),
                CommandHandler("cancel", cancel),
            ],
            CITY_SELECT_UNLOAD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_city_select_unload),
                CommandHandler("cancel", cancel),
            ],
            UNLOAD_DISTRIBUTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unload_distribution),
                CommandHandler("cancel", cancel),
            ],
            CHILD_EDIT_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_child_edit_menu),
                CommandHandler("cancel", cancel),
            ],
            CHILD_EDIT_CITY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_child_edit_city),
                CommandHandler("cancel", cancel),
            ],
            CHILD_EDIT_VOLUME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_child_edit_volume),
                CommandHandler("cancel", cancel),
            ],
            FIND_REQUEST_EDIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_find_request_edit_input),
                CommandHandler("cancel", cancel),
            ],
            SUMMARY_DATE_CALENDAR: [
                CallbackQueryHandler(handle_summary_calendar, pattern=r"^SUMCAL:"),
                CommandHandler("cancel", cancel),
            ],
            CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, confirm),
                CommandHandler("cancel", cancel),
            ],
            EDIT: [
                CallbackQueryHandler(handle_edit_choice, pattern=r"^EDIT_"),
                CommandHandler("cancel", cancel),
            ],
            SAVE_TEMPLATE_CONFIRM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_save_template_response),
                CommandHandler("cancel", cancel),
            ],
            SAVE_TEMPLATE_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_save_template_name),
                CommandHandler("cancel", cancel),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("my_requests", my_requests_command),
            CommandHandler("edit_request", edit_request_command),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("request", request_button))
    app.add_handler(CommandHandler("delete_request", delete_request_command))
    app.add_handler(CommandHandler("restore_request", restore_request_command))
    return app


def main() -> None:
    app = build_app()
    app.run_polling()


if __name__ == "__main__":
    main()
