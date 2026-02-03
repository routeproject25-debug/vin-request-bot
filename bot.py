import os
import logging
from typing import Dict, Any, List, Optional

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

START, QUESTION, CUSTOM_INPUT, CONFIRM = range(4)


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
    context.user_data.clear()
    context.user_data["question_index"] = 0

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
        await context.bot.send_message(chat_id=chat_id, text=application_text)
        await update.message.reply_text(
            "Заявку надіслано.", reply_markup=ReplyKeyboardRemove()
        )
        context.user_data.clear()
        return ConversationHandler.END

    await update.message.reply_text("Будь ласка, оберіть ТАК або Почати спочатку.")
    return CONFIRM


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Заповнення скасовано.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


async def request_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    bot_username = os.getenv("BOT_USERNAME")
    if not bot_username:
        await update.message.reply_text("Не задано BOT_USERNAME.")
        return

    deep_link = f"https://t.me/{bot_username}?start=apply"
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(text="Зробити заявку", url=deep_link)]]
    )
    await update.message.reply_text(
        "Натисніть кнопку для заповнення заявки:", reply_markup=keyboard
    )


def build_app() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            QUESTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer)],
            CUSTOM_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_input)],
            CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm)],
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
