"""Єдиний конвеєр публікації заявки: груповий чат → БД → Google Sheets → контакти.

Використовується і чат-ботом (confirm), і міні-додатком.
"""
import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import db
import formatting
import services
import sheets


@dataclass
class PublishResult:
    ok: bool
    request_id: str = ""
    export_success: bool = False
    chat_updated: bool = True
    error: str = ""
    blocked_reason: str = ""


def _user_info(user: Any) -> str:
    if not user:
        return "—"
    return f"@{user.username}" if getattr(user, "username", None) else (getattr(user, "full_name", None) or str(user.id))


async def notify_admin(bot: Any, text: str) -> None:
    admin_id = os.getenv("ADMIN_USER_ID")
    if not admin_id:
        return
    try:
        await bot.send_message(chat_id=admin_id, text=text)
    except Exception as e:
        logging.error(f"Failed to notify admin: {e}")


async def export_request_to_sheets(
    bot: Any,
    data: Dict[str, Any],
    user: Any = None,
    status: str = services.STATUS_ACTIVE,
    previous_data: Optional[Dict[str, Any]] = None,
) -> bool:
    """Експорт у Sheets у фоновому потоці + сповіщення адміна про помилку."""
    clean = services.sanitize_request_data(data)
    request_id = clean.get("request_id", "")
    try:
        success, error = await asyncio.to_thread(sheets.export_to_sheets, clean, status)
    except Exception as e:
        logging.error(f"Failed to export to Google Sheets: {e}")
        success, error = False, str(e)

    if not success:
        await notify_admin(
            bot,
            f"⚠️ Помилка експорту в Google Sheets!\n\n"
            f"🆔 ID заявки: {request_id}\n"
            f"👤 Користувач: {_user_info(user)}\n"
            f"❌ Причина: {(error or 'невідома')[:200]}\n"
            f"📄 Заявка надіслана в чат, але не експортована в таблицю.\n\n"
            f"Перевірте логи бота на Railway.",
        )
        return False

    # Якщо після редагування змінився набір НП розвантаження — старі рядки позначаємо видаленими.
    if previous_data:
        previous = dict(previous_data)
        previous.setdefault("request_id", request_id)
        orphan_ids = set(services.sheet_row_ids(previous)) - set(services.sheet_row_ids(clean))
        for row_id in sorted(orphan_ids):
            try:
                await asyncio.to_thread(sheets.mark_request_deleted_exact, row_id, "авто: змінено НП розвантаження")
            except Exception as e:
                logging.error(f"Failed to mark orphan sheet row {row_id}: {e}")
    return True


async def _save_contacts(user: Any, data: Dict[str, Any]) -> None:
    try:
        contacts_to_save = []
        load_contact = str(data.get("load_contact") or "").strip()
        unload_contact = str(data.get("unload_contact") or "").strip()
        if load_contact and load_contact != "—":
            contacts_to_save.append({"type": "load", "value": load_contact})
        if unload_contact and unload_contact != "—" and unload_contact != load_contact:
            contacts_to_save.append({"type": "unload", "value": unload_contact})
        if contacts_to_save:
            await asyncio.to_thread(db.save_contacts, user.id, contacts_to_save)
    except Exception as e:
        logging.error(f"Failed to save contacts: {e}")


async def publish_request(
    bot: Any,
    user: Any,
    data: Dict[str, Any],
    *,
    is_edit: bool,
    original_data: Optional[Dict[str, Any]] = None,
    is_copy: bool = False,
) -> PublishResult:
    """Опублікувати нову заявку або зміни існуючої. `data` доповнюється (request_id, метадані)."""
    chat_id = os.getenv("TARGET_CHAT_ID")
    if not chat_id:
        return PublishResult(False, error="Не задано TARGET_CHAT_ID. Додайте змінну середовища.")

    request_id = data.get("request_id") or services.generate_request_id()
    data["request_id"] = request_id

    if is_edit:
        return await _publish_edit(bot, user, data, chat_id, request_id, original_data)
    return await _publish_new(bot, user, data, chat_id, request_id, is_copy)


async def _publish_new(bot, user, data, chat_id, request_id, is_copy) -> PublishResult:
    formatting.initialize_creation_metadata(data, user, force=is_copy)
    thread_id = data.get("thread_id")
    notification = formatting.build_request_notification(request_id, data, user)
    logging.info(f"Sending new request {request_id} to chat {chat_id}, thread_id={thread_id}")

    try:
        message = await bot.send_message(chat_id=chat_id, text=notification, message_thread_id=thread_id)
    except Exception as send_error:
        logging.error(f"❌ Failed to send request {request_id} to chat: {send_error}", exc_info=True)
        try:
            message = await bot.send_message(chat_id=chat_id, text=notification)
        except Exception as fallback_error:
            logging.error(f"❌ Failed to send request {request_id} even without thread_id: {fallback_error}", exc_info=True)
            return PublishResult(
                False,
                request_id,
                error="Помилка відправки заявки в чат! Заявку не збережено. Спробуйте ще раз або зверніться до адміністратора.",
            )

    saved = await asyncio.to_thread(
        db.save_request,
        request_id=request_id,
        user_id=user.id,
        request_data=data,
        message_id=message.message_id,
        thread_id=thread_id,
    )
    if not saved:
        await notify_admin(bot, f"❌ Заявку {request_id} надіслано в чат, але не збережено в БД. Перевірте логи.")

    export_success = await export_request_to_sheets(bot, data, user)
    await _save_contacts(user, data)
    return PublishResult(True, request_id, export_success=export_success)


async def _publish_edit(bot, user, data, chat_id, request_id, original_data) -> PublishResult:
    saved = await asyncio.to_thread(db.get_request, request_id)
    if not saved:
        return PublishResult(False, request_id, error=f"Заявку {request_id} не знайдено в БД")

    saved_data = saved.get("request_data") if isinstance(saved.get("request_data"), dict) else {}
    reason = services.edit_block_reason(
        saved.get("status"), saved_data, is_admin=services.is_admin_user(user)
    )
    if reason:
        return PublishResult(False, request_id, blocked_reason=reason)

    formatting.ensure_creation_metadata_from_request_row(data, saved)
    await formatting.ensure_creator_mention_from_request_owner(data, saved, bot)
    message_id = saved.get("message_id")
    thread_id = saved.get("thread_id") or data.get("thread_id")

    data["last_edit_footer"] = formatting.build_edit_footer(user)
    data.pop("status_footer", None)
    notification = formatting.build_request_notification(request_id, data, None)

    chat_updated = False
    if message_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=notification)
            chat_updated = True
        except Exception as e:
            logging.error(f"Error updating message for {request_id}: {e}")
    else:
        logging.error(f"Missing message_id for request {request_id}")

    original = original_data if original_data else saved_data
    changes_text = formatting.get_changes_text(original, data)
    try:
        send_kwargs = {"chat_id": chat_id, "text": f"🆔 Заявка {request_id}\n{changes_text}"}
        if thread_id is not None:
            send_kwargs["message_thread_id"] = thread_id
        if message_id:
            send_kwargs["reply_to_message_id"] = message_id
            send_kwargs["allow_sending_without_reply"] = True
        await bot.send_message(**send_kwargs)
    except Exception as e:
        logging.error(f"Failed to send changes notification: {e}")

    await asyncio.to_thread(db.update_request_data, request_id, data)
    export_success = await export_request_to_sheets(bot, data, user, previous_data=original)
    await _save_contacts(user, data)
    return PublishResult(True, request_id, export_success=export_success, chat_updated=chat_updated)


async def refresh_group_message(bot: Any, request_row: Dict[str, Any], data: Dict[str, Any]) -> bool:
    """Перемалювати повідомлення заявки в групі (після зміни статусу/дати)."""
    chat_id = os.getenv("TARGET_CHAT_ID")
    message_id = request_row.get("message_id")
    if not chat_id or not message_id:
        return False
    formatting.ensure_creation_metadata_from_request_row(data, request_row)
    await formatting.ensure_creator_mention_from_request_owner(data, request_row, bot)
    notification = formatting.build_request_notification(request_row.get("request_id"), data, None)
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=notification)
        return True
    except Exception as e:
        if "not modified" in str(e).lower():
            return True
        logging.error(f"Failed to refresh group message for {request_row.get('request_id')}: {e}")
        return False
