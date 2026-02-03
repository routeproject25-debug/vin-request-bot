import os
import logging
from typing import Dict, Any, List, Optional
from aiohttp import web

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
    filters,
)


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

START, DEPARTMENT, QUESTION, CUSTOM_INPUT, CROP_TYPE, CONFIRM = range(6)

THREAD_IDS = {
    "Тваринництво": 2,
    "Виробництво": 4,
}

CROP_TYPES = ["Кукурудза", "Пшениця", "Соя", "Ріпак", "Соняшник"]

QUESTIONS: List[Dict[str, Any]] = [
    {
        "key": "vehicle_type",
        "label": "Тип авто",
        "prompt": "Тип авто:",
        "options": ["ТРАЛ", "зерновоз", "самоскид", "цистерна", "тент", "інше"],
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
        "options": ["Зернопродукт", "Агрокряж", "інше"],
    },
    {
        "key": "cargo_type",
        "label": "Вид вантажу",
        "prompt": "Вид вантажу:",
        "options": ["культура", "АМ вода", "КАС", "РКД", "насіння", "інше"],
    },
    {
        "key": "size_type",
        "label": "Габарит / негабарит",
        "prompt": "Габарит / негабарит:",
        "options": ["Габарит", "Негабарит"],
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
        "options": None,
    },
    {
        "key": "load_method",
        "label": "Спосіб завантаження",
        "prompt": "Спосіб завантаження:",
        "options": None,
    },
    {
        "key": "load_contact",
        "label": "Контакт на завантаженні (ПІБ, телефон)",
        "prompt": "Контакт на завантаженні (ПІБ, телефон):",
        "options": None,
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


def _build_reply_keyboard(options: Optional[List[str]]) -> Optional[ReplyKeyboardMarkup]:
    if not options:
        return None
    buttons = [[KeyboardButton(text=opt)] for opt in options]
    if "Ввести своє" not in options:
        buttons.append([KeyboardButton(text="Ввести своє")])
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True, one_time_keyboard=True)


def _format_application(data: Dict[str, Any]) -> str:
    def val(key: str) -> str:
        value = data.get(key)
        return value if value else "—"

    return (
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
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


async def handle_start_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text == "Продовжити":
        await update.message.reply_text(
            "Продовжуємо заповнення...",
            reply_markup=ReplyKeyboardRemove(),
        )
        return await ask_question(update, context)
    elif text == "Почати спочатку" or text == "📝 Зробити заявку":
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
    else:
        await update.message.reply_text("Будь ласка, оберіть Продовжити або Почати спочатку.")
        return START


async def handle_department(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    if text not in THREAD_IDS:
        await update.message.reply_text("Будь ласка, оберіть Тваринництво або Виробництво.")
        return DEPARTMENT

    context.user_data["department"] = text
    context.user_data["thread_id"] = THREAD_IDS[text]
    await update.message.reply_text(
        "Починаємо заповнення заявки.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return await ask_question(update, context)


async def ask_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    index = context.user_data.get("question_index", 0)
    if index >= len(QUESTIONS):
        application_text = _format_application(context.user_data)
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(text="ТАК")], [KeyboardButton(text="Почати спочатку")]],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await update.message.reply_text(
            "Перевірте заявку:\n\n" + application_text + "\n\nНадіслати заявку в чат?",
            reply_markup=keyboard,
        )
        return CONFIRM

    question = _get_question(index)
    keyboard = _build_reply_keyboard(question.get("options"))
    await update.message.reply_text(question["prompt"], reply_markup=keyboard)
    return QUESTION


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    index = context.user_data.get("question_index", 0)
    question = _get_question(index)

    if text.lower() == "ввести своє":
        context.user_data["awaiting_custom"] = True
        await update.message.reply_text("Введіть своє значення:", reply_markup=ReplyKeyboardRemove())
        return CUSTOM_INPUT

    # Якщо вибрано "культура", запитати конкретну культуру
    if question["key"] == "cargo_type" and text.lower() == "культура":
        context.user_data["cargo_type_prefix"] = "Культура"
        keyboard = _build_reply_keyboard(CROP_TYPES)
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

    context.user_data["question_index"] = index + 1
    return await ask_question(update, context)


async def handle_custom_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    index = context.user_data.get("question_index", 0)
    question = _get_question(index)
    context.user_data[question["key"]] = text
    context.user_data["awaiting_custom"] = False
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
        context.user_data["question_index"] = index + 1
        return await ask_question(update, context)
    
    # Якщо вибрано зі списку
    if text in CROP_TYPES:
        context.user_data["cargo_type"] = f"Культура: {text}"
        context.user_data.pop("cargo_type_prefix", None)
        index = context.user_data.get("question_index", 0)
        context.user_data["question_index"] = index + 1
        return await ask_question(update, context)
    else:
        await update.message.reply_text("Будь ласка, оберіть культуру зі списку або натисніть 'Ввести своє'.")
        return CROP_TYPE


async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip().lower()

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
        await context.bot.send_message(
            chat_id=chat_id,
            text=application_text,
            message_thread_id=thread_id,
        )
        
        # Показати кнопку для нової заявки
        keyboard = ReplyKeyboardMarkup(
            [[KeyboardButton(text="📝 Зробити заявку")]],
            resize_keyboard=True,
        )
        await update.message.reply_text(
            "Заявку надіслано. Можете створити нову заявку, натиснувши кнопку нижче.",
            reply_markup=keyboard
        )
        context.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text("Будь ласка, оберіть ТАК або Почати спочатку.")
    return CONFIRM


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

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex("^📝 Зробити заявку$"), start),
        ],
        states={
            START: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_start_choice)],
            DEPARTMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_department)],
            QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer)],
            CUSTOM_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_input)],
            CROP_TYPE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_crop_type)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("request", request_button))
    return app


def main() -> None:
    app = build_app()
    
    # Простий HTTP сервер для Render
    async def health(request):
        return web.Response(text="Bot is running")
    
    async def run_bot():
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
    
    async def shutdown(app_web):
        await app.stop()
        await app.shutdown()
    
    # Запуск HTTP сервера
    web_app = web.Application()
    web_app.router.add_get('/', health)
    web_app.router.add_get('/health', health)
    web_app.on_startup.append(lambda app: run_bot())
    web_app.on_shutdown.append(shutdown)
    
    port = int(os.getenv("PORT", 10000))
    web.run_app(web_app, host='0.0.0.0', port=port)


if __name__ == "__main__":
    main()
