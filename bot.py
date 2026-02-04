import os
import logging
import calendar
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, date
from telegram_bot_calendar import DetailedTelegramCalendar
import db

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

START, DEPARTMENT, QUESTION, CUSTOM_INPUT, CROP_TYPE, CONFIRM, EDIT, DATE_TYPE, DATE_CALENDAR, DATE_PERIOD_END, LOAD_TEMPLATE, TEMPLATE_SELECT, SAVE_TEMPLATE_NAME, SAVE_TEMPLATE_CONFIRM = range(14)

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
        "options": ["Культура", "АМ вода", "КАС", "РКД", "Насіння", "Інше"],
    },
    {
        "key": "size_type",
        "label": "Габарит / негабарит",
        "prompt": "Габарит / негабарит:",
        "options": ["Габарит", "Негабарит", "Насип", "Рідкі"],
    },
    {
        "key": "volume",
        "label": "Обсяг",
        "prompt": "Обсяг (наприклад: 22 т або 10 біг-бегів):",
        "options": None,
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
        "key": "load_place",
        "label": "Місце завантаження",
        "prompt": "Місце завантаження:",
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
        "key": "unload_place",
        "label": "Місце розвантаження",
        "prompt": "Місце розвантаження:",
        "options": None,
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
]


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
        value = data.get(key)
        return value if value else "—"
    
    now = datetime.now()
    date_str = now.strftime("%d.%m.%Y")
    time_str = now.strftime("%H:%M")

    return (
            f"Дата: {date_str}\n"
            f"Час: {time_str}\n\n"
        "ЗАЯВКА НА ПЕРЕВЕЗЕННЯ\n\n"
        "Вимоги до авто:\n"
        f"Тип авто: {val('vehicle_type')}\n\n"
        "Ініціатор заявки:\n"
        f"ПІБ: {val('initiator')}\n\n"
        "Параметри перевезення:\n"
        f"Підприємство: {val('company')}\n"
        f"Вид вантажу: {val('cargo_type')}\n"
        f"Габарит / негабарит: {val('size_type')}\n"
        f"Обсяг: {val('volume')}\n"
        f"Примітки: {val('notes')}\n\n"
        "Маршрут:\n"
        f"Дата / період перевезення: {val('date_period')}\n"
        f"Місце завантаження: {val('load_place')}\n"
        f"Спосіб завантаження: {val('load_method')}\n"
        f"Контакт на завантаженні: {val('load_contact')}\n\n"
        f"Місце розвантаження: {val('unload_place')}\n"
        f"Спосіб розвантаження: {val('unload_method')}\n"
        f"Контакт на розвантаженні: {val('unload_contact')}"
    )


async def show_start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показати початкове меню: нова заявка або завантажити шаблон"""
    user_id = update.effective_user.id
    templates = db.get_user_templates(user_id)
    
    buttons = [[KeyboardButton(text="📝 Нова заявка")]]
    
    if templates:
        buttons.append([KeyboardButton(text="📋 Завантажити шаблон")])
    
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
        "Оберіть шаблон:",
        reply_markup=keyboard
    )
    return TEMPLATE_SELECT


async def handle_template_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка вибору шаблону"""
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id
    
    if text == "⬅️ Назад":
        return await show_start_menu(update, context)
    
    templates = db.get_user_templates(user_id)
    selected_template = None
    
    for t in templates:
        if t["name"] == text:
            selected_template = db.get_template(t["id"])
            break
    
    if not selected_template:
        await update.message.reply_text("Шаблон не знайдено.")
        return TEMPLATE_SELECT
    
    context.user_data.clear()
    context.user_data.update(selected_template["data"])
    # Видалити department і thread_id зі старого шаблону
    context.user_data.pop("department", None)
    context.user_data.pop("thread_id", None)
    
    # Запитати "Запит від:" щоб встановити правильну гілку
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton(text="Тваринництво")], [KeyboardButton(text="Виробництво")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await update.message.reply_text(
        f"📋 Завантажено шаблон '{text}'\n\nЗапит від:",
        reply_markup=keyboard,
    )
    return DEPARTMENT


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
        return await ask_question(update, context)
    elif text == "Почати спочатку":
        context.user_data.clear()
        context.user_data["question_index"] = 0
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(text="Тваринництво")], [KeyboardButton(text="Виробництво")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text(
            "Запит від:",
            reply_markup=keyboard,
        )
        return DEPARTMENT
    # Новий вибір - нова заявка чи шаблон
    elif text == "📝 Нова заявка":
        context.user_data.clear()
        context.user_data["question_index"] = 0
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(text="Тваринництво")], [KeyboardButton(text="Виробництво")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text(
            "Запит від:",
            reply_markup=keyboard,
        )
        return DEPARTMENT
    elif text == "📋 Завантажити шаблон":
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
    
    # Якщо редагується department - повернутися до підтвердження
    if context.user_data.get("editing_department"):
        context.user_data.pop("editing_department", None)
        context.user_data["question_index"] = len(QUESTIONS)
        await update.message.reply_text(
            f"✅ Змінено на '{text}'",
            reply_markup=ReplyKeyboardRemove(),
        )
        return await ask_question(update, context)
    
    # Якщо це шаблон (вже є дані) - перейти до підтвердження
    if len(context.user_data) > 3:  # Більше ніж department, thread_id, question_index
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
        await update.message.reply_text(
            "Оберіть тип перевезення:",
            reply_markup=keyboard
        )
        return DATE_TYPE
    
    show_back = index > 0
    keyboard = _build_reply_keyboard(question.get("options"), show_back=show_back)
    # Зберегти message_id щоб потім редагувати
    bot_message = await update.message.reply_text(question["prompt"], reply_markup=keyboard)
    context.user_data["last_question_message_id"] = bot_message.message_id
    return QUESTION


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    index = context.user_data.get("question_index", 0)
    question = _get_question(index)

    # Обробка кнопки Назад
    if text == "⬅️ Назад":
        # Видалити повідомлення користувача
        try:
            await update.message.delete()
        except:
            pass
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

    # Якщо вибрано "культура", запитати конкретну культуру
    if question["key"] == "cargo_type" and text.lower() == "культура":
        context.user_data["cargo_type_prefix"] = "Культура"
        keyboard = _build_reply_keyboard(CROP_TYPES, show_back=True)
        await update.message.reply_text("Оберіть культуру:", reply_markup=keyboard)
        return CROP_TYPE

    if question.get("options"):
        if text.lower() == "пропустити":
            context.user_data[question["key"]] = "—"
        else:
            context.user_data[question["key"]] = text
    else:
        if question["key"] == "notes" and text.lower() == "пропустити":
            context.user_data[question["key"]] = "—"
        else:
            context.user_data[question["key"]] = text

    # Видалити повідомлення користувача та оновити питання бота
    try:
        await update.message.delete()
        # Редагувати попереднє повідомлення бота
        last_msg_id = context.user_data.get("last_question_message_id")
        if last_msg_id:
            answer_value = context.user_data.get(question["key"], "—")
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=last_msg_id,
                text=f"{question['prompt']} ✅ {answer_value}"
            )
    except Exception as e:
        # Якщо не вдалося - продовжуємо без редагування
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
    context.user_data[question["key"]] = text
    context.user_data["awaiting_custom"] = False
    
    # Видалити повідомлення користувача та оновити питання бота
    try:
        await update.message.delete()
        # Редагувати попереднє повідомлення бота
        last_msg_id = context.user_data.get("last_question_message_id")
        if last_msg_id:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=last_msg_id,
                text=f"{question['prompt']} ✅ {text}"
            )
    except Exception as e:
        # Якщо не вдалося - продовжуємо без редагування
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
        context.user_data["cargo_type"] = f"Культура: {text}"
        context.user_data.pop("awaiting_custom_crop", None)
        context.user_data.pop("cargo_type_prefix", None)
        index = context.user_data.get("question_index", 0)
        
        # Видалити повідомлення користувача
        try:
            await update.message.delete()
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
        context.user_data["cargo_type"] = f"Культура: {text}"
        context.user_data.pop("cargo_type_prefix", None)
        index = context.user_data.get("question_index", 0)
        
        # Видалити повідомлення користувача
        try:
            await update.message.delete()
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
        index = context.user_data.get("question_index", 0)
        if index > 0:
            context.user_data["question_index"] = index - 1
            return await ask_question(update, context)
    
    if text == "📅 Разове перевезення":
        context.user_data["date_type"] = "single"
        today = date.today()
        calendar = _build_month_calendar(today.year, today.month)
        await update.message.reply_text(
            "Оберіть дату перевезення:",
            reply_markup=calendar
        )
        return DATE_CALENDAR
    elif text == "📆 Період перевезення":
        context.user_data["date_type"] = "period"
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
                await update.callback_query.edit_message_text(f"Початкова дата: {selected_date}")
                
                # Показуємо календар для кінцевої дати
                calendar = _build_month_calendar(selected_dt.year, selected_dt.month)
                await update.callback_query.message.reply_text(
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
            f"Період перевезення: {start_date} - {end_date}"
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


async def show_edit_fields(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Показує список полів для редагування"""
    buttons = []
    
    # Додати "Запит від:" як перше редаговане поле
    department = context.user_data.get("department", "—")
    buttons.append([KeyboardButton(text=f"Запит від: {department}")])
    
    for q in QUESTIONS:
        field_value = context.user_data.get(q["key"], "—")
        # Обмежуємо довжину для кнопки
        display_value = field_value[:20] + "..." if len(str(field_value)) > 20 else field_value
        buttons.append([KeyboardButton(text=f"{q['label']}: {display_value}")])
    
    buttons.append([KeyboardButton(text="⬅️ Назад до підтвердження")])
    keyboard = ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)
    
    await update.message.reply_text(
        "Оберіть поле для редагування:",
        reply_markup=keyboard
    )
    return EDIT


async def handle_edit_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Обробка вибору поля для редагування"""
    text = (update.message.text or "").strip()
    
    if text == "⬅️ Назад до підтвердження":
        return await ask_question(update, context)
    
    # Перевірити, чи редагується "Запит від:"
    if text.startswith("Запит від:"):
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(text="Тваринництво")], [KeyboardButton(text="Виробництво")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text(
            "Запит від:",
            reply_markup=keyboard,
        )
        context.user_data["editing_department"] = True
        return DEPARTMENT
    
    # Знайти індекс питання за label
    for idx, q in enumerate(QUESTIONS):
        if text.startswith(q["label"]):
            context.user_data["question_index"] = idx
            context.user_data["editing_mode"] = True
            return await ask_question(update, context)
    
    await update.message.reply_text("Будь ласка, оберіть поле зі списку.")
    return EDIT


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip().lower()

    if text == "✏️ редагувати поля":
        return await show_edit_fields(update, context)

    if text == "почати спочатку":
        context.user_data.clear()
        context.user_data["question_index"] = 0
        await update.message.reply_text("Заповнення скинуто. Починаємо спочатку.")
        return await ask_question(update, context)

    if text == "так":
        chat_id = os.getenv("TARGET_CHAT_ID")
        if not chat_id:
            await update.message.reply_text(
                "Не задано TARGET_CHAT_ID. Додайте змінну середовища.",
                reply_markup=ReplyKeyboardRemove(),
            )
            return ConversationHandler.END

        application_text = _format_application(context.user_data)
        thread_id = context.user_data.get("thread_id")
        
        # Додаємо згадку користувача
        user = update.effective_user
        user_mention = f"@{user.username}" if user.username else user.full_name
        notification = f"📋 {user_mention} створив нову заявку:\n\n{application_text}"
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=notification,
            message_thread_id=thread_id,
        )
        
        # Запропонувати зберегти як шаблон
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(text="💾 Зберегти як шаблон")], [KeyboardButton(text="📝 Нова заявка")]],
            resize_keyboard=True,
        )
        await update.message.reply_text(
            "Заявку надіслано. Бажаєте зберегти дані як шаблон для повторного використання?",
            reply_markup=keyboard
        )
        context.user_data["pending_save_template"] = True
        return SAVE_TEMPLATE_CONFIRM

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
    
    # Зберегти шаблон
    template_data = {k: v for k, v in context.user_data.items() 
                    if k not in ["question_index", "pending_save_template", "thread_id"]}
    
    db.save_template(user_id, template_name, template_data)
    
    keyboard = ReplyKeyboardMarkup(
        [[KeyboardButton(text="📝 Нова заявка")]],
        resize_keyboard=True,
    )
    await update.message.reply_text(
        f"✅ Шаблон '{template_name}' збережено!",
        reply_markup=keyboard
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    
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
            MessageHandler(filters.Regex("^📝 (Зробити заявку|Нова заявка)$"), start),
        ],
        states={
            START: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_start_menu_choice)],
            LOAD_TEMPLATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_start_menu_choice)],
            TEMPLATE_SELECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_template_select)],
            DEPARTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_department)],
            QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer)],
            CUSTOM_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_input)],
            CROP_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_crop_type)],
            DATE_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date_type)],
            DATE_CALENDAR: [CallbackQueryHandler(handle_calendar)],
            DATE_PERIOD_END: [CallbackQueryHandler(handle_period_end)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
            EDIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_choice)],
            SAVE_TEMPLATE_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_save_template_response)],
            SAVE_TEMPLATE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_save_template_name)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("request", request_button))
    return app


def main() -> None:
    app = build_app()
    app.run_polling()


if __name__ == "__main__":
    main()
