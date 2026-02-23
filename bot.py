import os
import re
import logging
import calendar
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

START, DEPARTMENT, QUESTION, CUSTOM_INPUT, CROP_TYPE, CONFIRM, EDIT, DATE_TYPE, DATE_CALENDAR, DATE_PERIOD_END, LOAD_TEMPLATE, TEMPLATE_SELECT, SAVE_TEMPLATE_NAME, SAVE_TEMPLATE_CONFIRM, DELETE_TEMPLATE_CONFIRM, CITY_SEARCH_LOAD, CITY_SELECT_LOAD, CITY_SEARCH_UNLOAD, CITY_SELECT_UNLOAD, UNLOAD_ADD_MORE = range(20)

THREAD_IDS = {
    "Тваринництво": 2,
    "Виробництво": 4,
}

CROP_TYPES = ["Кукурудза", "Пшениця", "Соя", "Ріпак", "Соняшник"]

LIQUID_BULK_CARGO = {"КАС", "РКД", "АМ вода"}

CAL_PREFIX = "CAL"
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
        "label": "Контакт на завантаженні (ПІБ, телефон)",
        "prompt": "Контакт на завантаженні (ПІБ, телефон):",
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
        "options": None,
    },
    {
        "key": "unload_contact",
        "label": "Контакт на розвантаженні (ПІБ, телефон)",
        "prompt": "Контакт на розвантаженні (ПІБ, телефон):",
        "options": None,
    },
    {
        "key": "unload_points",
        "label": "Точки розвантаження",
        "prompt": "Точки розвантаження:",
        "options": None,
    },
]


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


def _build_month_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    header_text = f"{MONTH_NAMES_UK[month - 1]} {year}"
    rows.append([InlineKeyboardButton(text=header_text, callback_data=f"{CAL_PREFIX}:X")])
    rows.append([InlineKeyboardButton(text=day, callback_data=f"{CAL_PREFIX}:X") for day in WEEKDAYS_UK])

    cal = calendar.Calendar(firstweekday=0)
    for week in cal.monthdayscalendar(year, month):
        row: List[InlineKeyboardButton] = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data=f"{CAL_PREFIX}:X"))
            else:
                row.append(
                    InlineKeyboardButton(
                        text=str(day),
                        callback_data=f"{CAL_PREFIX}:D:{year:04d}-{month:02d}-{day:02d}",
                    )
                )
        rows.append(row)

    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    rows.append(
        [
            InlineKeyboardButton(text="«", callback_data=f"{CAL_PREFIX}:N:{prev_year:04d}-{prev_month:02d}"),
            InlineKeyboardButton(text="Сьогодні", callback_data=f"{CAL_PREFIX}:T"),
            InlineKeyboardButton(text="»", callback_data=f"{CAL_PREFIX}:N:{next_year:04d}-{next_month:02d}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _parse_calendar_callback(data: str) -> Tuple[str, Optional[str]]:
    if not data or not data.startswith(f"{CAL_PREFIX}:"):
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


def _format_application(data: Dict[str, Any]) -> str:
    def val(key: str) -> str:
        return data.get(key, "—")
    
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

    # --- Вивід декількох точок розвантаження ---
    if data.get("unload_points"):
        unloads = ""
        for i, point in enumerate(data["unload_points"], 1):
            unloads += (
                f"\nТочка розвантаження {i}:\n"
                f"  Населений пункт: {point.get('unload_city', '—')}\n"
                f"  Склад: {point.get('unload_place', '—')}\n"
                f"  Спосіб: {point.get('unload_method', '—')}\n"
                f"  Контакт: {point.get('unload_contact', '—')}\n"
            )
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
            f"Контакт на завантаженні: {val('load_contact')}\n"
            f"{unloads}"
        )
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
        f"Контакт на завантаженні: {val('load_contact')}\n"
        f"{unloads}"
    )


async def show_start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показати початкове меню: нова заявка або завантажити шаблон"""
    user_id = update.effective_user.id
    templates = db.get_user_templates(user_id)
    
    buttons = [
        [KeyboardButton(text="📝 Нова заявка")],
        [KeyboardButton(text="⚡ Швидка заявка")]
    ]
    
    if templates:
        buttons.append([KeyboardButton(text="📋 Завантажити шаблон")])
        buttons.append([KeyboardButton(text="🗑️ Видалити шаблон")])
    
    keyboard = ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)
    await update.message.reply_text(
        "Що робитимемо?",
        reply_markup=keyboard
    )
    return LOAD_TEMPLATE


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
    text = (update.message.text or "").strip()
    
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
        show_back = index > 0
        buttons = []
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
    # --- Додаємо цикл для декількох точок розвантаження ---
    if question["key"] == "unload_contact":
        # Додаємо точку до списку
        point = {
            "unload_city": context.user_data.get("unload_city", "—"),
            "unload_place": context.user_data.get("unload_place", "—"),
            "unload_method": context.user_data.get("unload_method", "—"),
            "unload_contact": text,
        }
        if "unload_points" not in context.user_data:
            context.user_data["unload_points"] = []
        context.user_data["unload_points"].append(point)
        # Очищаємо поля для наступної точки
        for k in ["unload_city", "unload_place", "unload_method", "unload_contact"]:
            context.user_data.pop(k, None)
        # Кнопки для додавання ще точки або завершення
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(text="➕ Додати ще точку розвантаження")], [KeyboardButton(text="✅ Завершити введення точок")]],
