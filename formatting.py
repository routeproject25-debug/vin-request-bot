"""Форматування текстів заявок і спільні клавіатури (календар)."""
import calendar
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import services

MONTH_NAMES_UK = [
    "Січень", "Лютий", "Березень", "Квітень", "Травень", "Червень",
    "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень",
]
WEEKDAYS_UK = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]


def format_volume_with_unit(size_type: str, volume: str) -> str:
    """Форматувати відповідь вага/обсяг з правильною одиницею"""
    if not volume:
        return volume
    if size_type == "Біг-бег":
        return f"{volume} шт"
    return f"{volume} т"


def format_volume_for_size(size_type: str, value: float) -> str:
    unit = "шт" if size_type == "Біг-бег" else "т"
    return f"{services.format_number(value)} {unit}"


def short_city_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text == "—":
        return "—"
    first = text.split(";")[0].strip()
    first = first.split(",")[0].strip()
    first = first.split("(")[0].strip()
    first = re.sub(r"^(м\.?|с\.?|смт\.?|селище|село)\s+", "", first, flags=re.IGNORECASE)
    return first or "—"


def build_compact_route(data: Dict[str, Any]) -> str:
    load_city = short_city_name(data.get("load_city"))
    unload_cities = services.split_location_values(data.get("unload_city"))
    unload_main = short_city_name(unload_cities[0] if unload_cities else data.get("unload_city"))
    if len(unload_cities) > 1:
        unload_main = f"{unload_main}+{len(unload_cities) - 1}"
    return f"{load_city}→{unload_main}"


def format_application(data: Dict[str, Any]) -> str:
    def val(key: str) -> str:
        value = data.get(key)
        return value if value else "—"

    # Дата/час створення заявки мають залишатися незмінними під час редагування.
    # Якщо метадані ще відсутні (старі записи), використовуємо поточний час Києва.
    now = services.kyiv_now()
    date_str = str(data.get("created_date") or "").strip() or now.strftime("%d.%m.%Y")
    time_str = str(data.get("created_time") or "").strip() or now.strftime("%H:%M")

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
        volume_value = format_volume_with_unit(size_type_for_unit, volume_value)

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


def build_user_mention(user: Any, fallback: str = "Користувач") -> str:
    """Побудувати згадку користувача у форматі @username або ПІБ."""
    if not user:
        return fallback
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    full_name = getattr(user, "full_name", None)
    if full_name:
        return full_name
    return fallback


def initialize_creation_metadata(data: Dict[str, Any], user: Any, force: bool = False) -> None:
    """Заповнити метадані створення заявки (хто/коли)."""
    now = services.kyiv_now()
    if force or not str(data.get("created_date") or "").strip():
        data["created_date"] = now.strftime("%d.%m.%Y")
    if force or not str(data.get("created_time") or "").strip():
        data["created_time"] = now.strftime("%H:%M")
    if force or not str(data.get("created_by_mention") or "").strip():
        data["created_by_mention"] = build_user_mention(user)


def ensure_creation_metadata_from_request_row(data: Dict[str, Any], request_row: Optional[Dict[str, Any]]) -> None:
    """Дозаповнити created_date/created_time з БД для старих заявок без цих полів."""
    if not request_row:
        return
    created_at = request_row.get("created_at")
    if not created_at:
        return
    if getattr(created_at, "tzinfo", None) is None:
        # PostgreSQL часто повертає naive timestamp у UTC.
        created_at = pytz.UTC.localize(created_at)
    created_at_kyiv = created_at.astimezone(services.KYIV_TZ)
    if not str(data.get("created_date") or "").strip():
        data["created_date"] = created_at_kyiv.strftime("%d.%m.%Y")
    if not str(data.get("created_time") or "").strip():
        data["created_time"] = created_at_kyiv.strftime("%H:%M")


async def ensure_creator_mention_from_request_owner(
    data: Dict[str, Any],
    request_row: Optional[Dict[str, Any]],
    bot: Any,
) -> None:
    """Спробувати відновити created_by_mention по owner user_id для старих заявок."""
    if str(data.get("created_by_mention") or "").strip():
        return
    if not request_row:
        return

    owner_user_id = request_row.get("user_id")
    if owner_user_id is None:
        return

    try:
        owner_chat = await bot.get_chat(int(owner_user_id))
        data["created_by_mention"] = build_user_mention(owner_chat)
    except Exception as e:
        logging.warning(f"Failed to resolve request owner mention for user_id={owner_user_id}: {e}")


def build_edit_footer(editor_user: Any) -> str:
    """Побудувати короткий блок про останнє редагування заявки."""
    now = services.kyiv_now()
    edited_by = build_user_mention(editor_user)
    return (
        f"Вніс зміни: {edited_by}\n"
        f"Дата і час: {now.strftime('%d.%m.%Y')} - {now.strftime('%H:%M')}"
    )


def build_status_footer(icon_text: str, by_user: Any) -> str:
    now = services.kyiv_now()
    return f"{icon_text}\n{build_user_mention(by_user)}, {now.strftime('%d.%m.%Y %H:%M')}"


def build_request_notification(
    request_id: str,
    request_data: Dict[str, Any],
    fallback_user: Any,
    edit_footer: Optional[str] = None,
) -> str:
    """Зібрати повний текст повідомлення заявки для групи."""
    creator_mention = str(request_data.get("created_by_mention") or "").strip() or build_user_mention(fallback_user)
    application_text = format_application(request_data)
    footers = [
        edit_footer or str(request_data.get("last_edit_footer") or "").strip(),
        str(request_data.get("status_footer") or "").strip(),
    ]
    footers = [f for f in footers if f]
    if footers:
        application_text = f"{application_text}\n\n-------\n" + "\n\n".join(footers)
    return f"📋 {creator_mention} створив нову заявку:\n🆔 ID заявки: {request_id}\n\n{application_text}"


CHANGE_FIELD_LABELS = {
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


def get_changes_text(original_data: dict, new_data: dict) -> str:
    """Створити текст з переліком змін між оригінальними та новими даними."""
    def normalize_value(value: Any) -> str:
        if value is None:
            return "—"
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned if cleaned else "—"
        return str(value)

    changes = []
    for field, label in CHANGE_FIELD_LABELS.items():
        old_val = normalize_value(original_data.get(field))
        new_val = normalize_value(new_data.get(field))
        if old_val != new_val:
            changes.append(f"{label}: {old_val} → {new_val}")

    if changes:
        return "✏️ Зміни:\n" + "\n".join(changes)
    return "✏️ Заявку відредаговано"


def build_month_calendar(
    year: int,
    month: int,
    prefix: str = "CAL",
    extra_rows: Optional[List[List[InlineKeyboardButton]]] = None,
) -> InlineKeyboardMarkup:
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
    rows.extend(extra_rows or [])
    return InlineKeyboardMarkup(rows)


def parse_calendar_callback(data: str, prefix: str = "CAL") -> Tuple[str, Optional[str]]:
    if not data or not data.startswith(f"{prefix}:"):
        return "IGNORE", None
    rest = data[len(prefix) + 1:]
    parts = rest.split(":", 1)
    action = parts[0]
    if action == "X":
        return "IGNORE", None
    if action == "T":
        today = services.kyiv_today()
        return "NAV", f"{today.year:04d}-{today.month:02d}"
    if len(parts) < 2:
        return "IGNORE", None
    payload = parts[1]
    if action == "N":
        return "NAV", payload
    if action == "D":
        return "DATE", payload
    return "IGNORE", None
