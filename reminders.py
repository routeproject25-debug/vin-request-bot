"""Щоденні нагадування логістам у робочій групі про заявки, термін яких спливає.

Під нагадуванням — кнопки «Виконано», «Перенести» (з календарем) і «Скасовано».
Після дії нагадування видаляється; при наступному запуску старі нагадування
теж прибираються, щоб не засмічувати чат.
"""
import asyncio
import logging
import os
from datetime import date, time as dtime, timedelta
from typing import Any, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import db
import formatting
import publishing
import services
import sheets

CALLBACK_PATTERN = r"^RM(:|C\|)"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def reminder_time() -> dtime:
    raw = (os.getenv("REMINDER_TIME") or "16:00").strip()
    try:
        hours, minutes = (int(part) for part in raw.split(":", 1))
        return dtime(hour=hours, minute=minutes, tzinfo=services.KYIV_TZ)
    except ValueError:
        logging.warning(f"Invalid REMINDER_TIME={raw!r}, using 16:00")
        return dtime(hour=16, minute=0, tzinfo=services.KYIV_TZ)


def reminders_enabled() -> bool:
    flag = (os.getenv("REMINDERS_ENABLED") or "1").strip().lower()
    return flag not in {"0", "false", "no", "off"} and bool(os.getenv("TARGET_CHAT_ID"))


def _reminder_keyboard(request_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(text="✅ Виконано", callback_data=f"RM:DONE:{request_id}"),
            InlineKeyboardButton(text="📅 Перенести", callback_data=f"RM:MOVE:{request_id}"),
        ],
        [InlineKeyboardButton(text="🚫 Скасовано", callback_data=f"RM:CANCEL:{request_id}")],
    ])


def _calendar_prefix(request_id: str) -> str:
    return f"RMC|{request_id}"


def _reschedule_calendar(request_id: str, year: int, month: int) -> InlineKeyboardMarkup:
    back_row = [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"RM:BACK:{request_id}")]
    return formatting.build_month_calendar(year, month, prefix=_calendar_prefix(request_id), extra_rows=[back_row])


def build_reminder_text(row: Dict[str, Any], today: date) -> str:
    data = row.get("request_data") if isinstance(row.get("request_data"), dict) else {}
    end = row.get("delivery_date_to")
    days_left = (end - today).days if isinstance(end, date) else None
    if days_left is None:
        when = "⏰ Нагадування"
    elif days_left < 0:
        when = f"⚠️ Термін минув {-days_left} дн. тому"
    elif days_left == 0:
        when = "⏰ Сьогодні останній день перевезення"
    else:
        when = f"⏳ До кінця терміну: {days_left} дн."

    volume_value = services.to_float(data.get("volume"))
    volume = (
        formatting.format_volume_for_size(str(data.get("size_type") or ""), volume_value)
        if volume_value is not None else "—"
    )
    return (
        f"{when}\n"
        f"🆔 {row.get('request_id')} · {data.get('department') or '—'}\n"
        f"📦 {data.get('cargo_type') or '—'}, {volume}\n"
        f"📍 {formatting.build_compact_route(data)}\n"
        f"📅 {data.get('date_period') or '—'}\n"
        f"👤 {data.get('initiator') or '—'} ({data.get('created_by_mention') or '—'})\n\n"
        "Заявку виконано чи потрібно перенести термін?"
    )


async def clear_old_reminders(bot: Any) -> int:
    removed = 0
    for row in await asyncio.to_thread(db.get_requests_with_reminders):
        try:
            await bot.delete_message(chat_id=row["reminder_chat_id"], message_id=row["reminder_message_id"])
            removed += 1
        except Exception as e:
            logging.info(f"Reminder message for {row['request_id']} already gone: {e}")
        await asyncio.to_thread(db.set_reminder_message, row["request_id"], None, None)
    return removed


async def send_due_reminders(bot: Any) -> int:
    """Надіслати нагадування по всіх активних заявках, термін яких спливає. Повертає кількість."""
    chat_id = os.getenv("TARGET_CHAT_ID")
    if not chat_id:
        return 0

    today = services.kyiv_today()
    days_ahead = _int_env("REMINDER_DAYS_AHEAD", 0)
    lookback = _int_env("REMINDER_LOOKBACK_DAYS", 7)
    max_count = _int_env("REMINDER_MAX_PER_RUN", 30)

    await clear_old_reminders(bot)
    rows = await asyncio.to_thread(
        db.get_requests_due_for_reminder,
        today + timedelta(days=days_ahead),
        today - timedelta(days=lookback),
        max_count,
    )

    sent = 0
    for row in rows:
        request_id = row["request_id"]
        kwargs: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": build_reminder_text(row, today),
            "reply_markup": _reminder_keyboard(request_id),
        }
        if row.get("thread_id"):
            kwargs["message_thread_id"] = row["thread_id"]
        if row.get("message_id"):
            kwargs["reply_to_message_id"] = row["message_id"]
            kwargs["allow_sending_without_reply"] = True
        try:
            message = await bot.send_message(**kwargs)
        except Exception as e:
            logging.error(f"Failed to send reminder for {request_id}: {e}")
            continue
        await asyncio.to_thread(db.set_reminder_message, request_id, message.chat_id, message.message_id)
        sent += 1

    logging.info(f"Reminders sent: {sent}")
    return sent


async def reminder_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_due_reminders(context.bot)


async def _delete_reminder_message(query: Any, request_id: str) -> None:
    try:
        await query.message.delete()
    except Exception as e:
        logging.info(f"Failed to delete reminder message: {e}")
    await asyncio.to_thread(db.set_reminder_message, request_id, None, None)


async def _notify_owner(bot: Any, row: Dict[str, Any], text: str) -> None:
    owner_id = row.get("user_id")
    if not owner_id:
        return
    try:
        await bot.send_message(chat_id=int(owner_id), text=text)
    except Exception as e:
        logging.info(f"Failed to notify request owner {owner_id}: {e}")


async def close_request(bot: Any, row: Dict[str, Any], status: str, user: Any) -> None:
    """Позначити заявку виконаною або скасованою: БД, Sheets, повідомлення в групі, автор."""
    request_id = row["request_id"]
    data = dict(row.get("request_data") or {})
    by_text = formatting.build_user_mention(user)
    label = "✅ ВИКОНАНО" if status == services.STATUS_DONE else "🚫 СКАСОВАНО"
    data["status_footer"] = formatting.build_status_footer(f"Статус: {label}", user)

    await asyncio.to_thread(db.set_request_status, request_id, status, by_text)
    await asyncio.to_thread(db.update_request_data, request_id, data)
    ok, message = await asyncio.to_thread(
        sheets.set_request_status, request_id, status, by_text, status == services.STATUS_DONE
    )
    if not ok:
        logging.error(f"Sheets status update failed for {request_id}: {message}")
    await publishing.refresh_group_message(bot, row, data)
    await _notify_owner(bot, row, f"{label}: заявку {request_id} ({data.get('date_period') or '—'}) позначив {by_text}.")


async def reschedule_request(bot: Any, row: Dict[str, Any], new_end: date, user: Any) -> str:
    """Перенести кінцевий термін заявки. Повертає новий період."""
    request_id = row["request_id"]
    data = dict(row.get("request_data") or {})
    old_period = str(data.get("date_period") or "—")
    new_period = services.reschedule_period(old_period, new_end)
    data["date_period"] = new_period
    data["date_type"] = "period" if " - " in new_period else "single"
    data["status_footer"] = formatting.build_status_footer(f"📅 Перенесено: {old_period} → {new_period}", user)

    await asyncio.to_thread(db.update_request_data, request_id, data)
    await publishing.export_request_to_sheets(bot, data, user)
    await publishing.refresh_group_message(bot, row, data)
    await _notify_owner(
        bot, row,
        f"📅 Термін заявки {request_id} перенесено: {old_period} → {new_period} "
        f"({formatting.build_user_mention(user)}).",
    )
    return new_period


async def handle_reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    user = update.effective_user
    if not services.is_logist_user(user):
        await query.answer("Ця дія доступна лише логістам", show_alert=True)
        return

    payload = query.data or ""
    if payload.startswith("RMC|"):
        await _handle_calendar(query, context, user, payload)
        return

    parts = payload.split(":", 2)
    if len(parts) != 3:
        await query.answer()
        return
    _, action, request_id = parts

    row = await asyncio.to_thread(db.get_request, request_id)
    if not row:
        await query.answer("Заявку не знайдено", show_alert=True)
        await _delete_reminder_message(query, request_id)
        return

    status = services.normalize_status(row.get("status"))
    if status != services.STATUS_ACTIVE:
        await query.answer(f"Заявка вже має статус {status}", show_alert=True)
        await _delete_reminder_message(query, request_id)
        return

    if action == "DONE":
        await close_request(context.bot, row, services.STATUS_DONE, user)
        await query.answer(f"✅ Заявку {request_id} позначено виконаною")
        await _delete_reminder_message(query, request_id)
        return

    if action == "CANCEL":
        await query.answer()
        await query.edit_message_reply_markup(InlineKeyboardMarkup([[
            InlineKeyboardButton(text="🚫 Так, скасувати", callback_data=f"RM:CANCELYES:{request_id}"),
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"RM:BACK:{request_id}"),
        ]]))
        return

    if action == "CANCELYES":
        await close_request(context.bot, row, services.STATUS_CANCELLED, user)
        await query.answer(f"🚫 Заявку {request_id} скасовано")
        await _delete_reminder_message(query, request_id)
        return

    if action == "BACK":
        await query.answer()
        await query.edit_message_reply_markup(_reminder_keyboard(request_id))
        return

    if action == "MOVE":
        await query.answer("Оберіть новий кінцевий термін")
        today = services.kyiv_today()
        await query.edit_message_reply_markup(_reschedule_calendar(request_id, today.year, today.month))
        return

    await query.answer()


async def _handle_calendar(query: Any, context: ContextTypes.DEFAULT_TYPE, user: Any, payload: str) -> None:
    prefix = payload.split(":", 1)[0]
    request_id = prefix.split("|", 1)[1] if "|" in prefix else ""
    action, value = formatting.parse_calendar_callback(payload, prefix=prefix)

    if action == "NAV" and value:
        await query.answer()
        year_str, month_str = value.split("-")
        await query.edit_message_reply_markup(_reschedule_calendar(request_id, int(year_str), int(month_str)))
        return

    if action != "DATE" or not value:
        await query.answer()
        return

    new_end = date.fromisoformat(value)
    if new_end < services.kyiv_today():
        await query.answer("Новий термін не може бути в минулому", show_alert=True)
        return

    row = await asyncio.to_thread(db.get_request, request_id)
    if not row or services.normalize_status(row.get("status")) != services.STATUS_ACTIVE:
        await query.answer("Заявка вже неактивна", show_alert=True)
        await _delete_reminder_message(query, request_id)
        return

    new_period = await reschedule_request(context.bot, row, new_end, user)
    await query.answer(f"📅 Заявку {request_id} перенесено: {new_period}")
    await _delete_reminder_message(query, request_id)
