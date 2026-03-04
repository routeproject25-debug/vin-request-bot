import os
import re
import logging
from typing import Dict, Any, Tuple, Optional
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)

# Scope для Google Sheets API
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]


def _parse_contact(contact_str: str) -> Tuple[str, str]:
    """
    Розпарсити контактну інформацію на ПІБ та номер телефону
    
    Приклади:
    - "Іван Петренко 050-123-45-67" → ("Іван Петренко", "050-123-45-67")
    - "0501234567" → ("", "050-123-45-67")
    - "Петренко" → ("Петренко", "")
    """
    if not contact_str or contact_str == "—":
        return ("—", "—")
    
    # Патерни для номерів телефону (українські формати)
    phone_patterns = [
        r'\+?38\s?0[0-9]{2}[\s\-]?[0-9]{3}[\s\-]?[0-9]{2}[\s\-]?[0-9]{2}',  # +380501234567, 0501234567
        r'0[0-9]{2}[\s\-]?[0-9]{3}[\s\-]?[0-9]{2}[\s\-]?[0-9]{2}',  # 050-123-45-67
        r'\([0-9]{3}\)\s?[0-9]{3}[\s\-]?[0-9]{2}[\s\-]?[0-9]{2}',  # (050) 123-45-67
        r'[0-9]{3}\s[0-9]{3}\s[0-9]{2}\s[0-9]{2}',  # 888 888 88 88 (з пробілами)
        r'[0-9]{2}[\s\-][0-9]{8}',  # 00 00000000, 00-00000000
        r'[0-9]{10}',  # 0000000000 (10 цифр підряд)
    ]
    
    phone_number = ""
    name = contact_str.strip()
    
    # Шукаємо номер телефону
    for pattern in phone_patterns:
        match = re.search(pattern, contact_str)
        if match:
            phone_number = match.group(0)
            # Нормалізувати формат: 050-123-45-67
            digits = re.sub(r'\D', '', phone_number)
            if digits.startswith('38'):
                digits = digits[2:]  # Видалити +38
            if len(digits) == 10:
                phone_number = f"{digits[0:3]}-{digits[3:6]}-{digits[6:8]}-{digits[8:10]}"
            
            # Видалити номер з тексту щоб залишилося ПІБ
            name = contact_str.replace(match.group(0), '').strip()
            break
    
    # Якщо залишилося порожньо - значить був тільки номер
    if not name:
        name = "—"
    if not phone_number:
        phone_number = "—"
    
    return (name, phone_number)


def get_sheets_client():
    """Створити клієнт Google Sheets"""
    try:
        # Спробувати прочитати з JSON змінної середовища (для Railway)
        creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            import json
            creds_dict = json.loads(creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
            client = gspread.authorize(creds)
            logger.info("Google Sheets client created from GOOGLE_CREDENTIALS_JSON env variable")
            return client
        
        # Якщо немає в ENV - спробувати прочитати з файлу (локально)
        creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "google-credentials.json")
        if os.path.exists(creds_file):
            creds = Credentials.from_service_account_file(creds_file, scopes=SCOPES)
            client = gspread.authorize(creds)
            logger.info(f"Google Sheets client created from file: {creds_file}")
            return client
        
        logger.error("No Google credentials found (neither GOOGLE_CREDENTIALS_JSON nor file)")
        return None
        
    except Exception as e:
        logger.error(f"Error creating Google Sheets client: {e}")
        return None


def export_to_sheets(data: Dict[str, Any]) -> bool:
    """
    Експортувати заявку в Google Sheets
    
    Структура таблиці (колонки):
    1. Дата
    2. Час
    3. Ініціатор заявки
    4. Підприємство
    5. Тип авто
    6. Вид вантажу
    7. Габарит / негабарит
    8. Обсяг
    9. Примітка
    10. Дата початку
    11. Дата кінця
    12. Населений пункт завантаження
    13. Склад завантаження
    14. Спосіб завантаження
    15. Контакт на завантаженні
    16. Номер телефона на завантаженні
    17. Населений пункт розвантаження
    18. Склад розвантаження
    19. Спосіб розвантаження
    20. Контакт на розвантаженні
    21. Номер телефона на розвантаженні
    """
    try:
        client = get_sheets_client()
        if not client:
            return False
        
        spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID")
        worksheet_name = os.getenv("GOOGLE_WORKSHEET_NAME", "ЗАЯВКА")
        
        if not spreadsheet_id:
            logger.error("GOOGLE_SPREADSHEET_ID not set")
            return False
        
        # Відкрити таблицю
        spreadsheet = client.open_by_key(spreadsheet_id)
        worksheet = spreadsheet.worksheet(worksheet_name)
        
        # Отримати поточну дату/час у Київському часі
        kyiv_tz = pytz.timezone('Europe/Kyiv')
        now = datetime.now(kyiv_tz)
        date_str = now.strftime("%d.%m.%Y")
        time_str = now.strftime("%H:%M")
        
        # Розпарсити контакти
        load_contact = data.get("load_contact", "—")
        unload_contact = data.get("unload_contact", "—")
        
        load_name, load_phone = _parse_contact(load_contact)
        unload_name, unload_phone = _parse_contact(unload_contact)
        
        # Форматувати обсяг з одиницею
        size_type = data.get("size_type", "")
        big_bag_weight = data.get("big_bag_weight", "")
        volume = data.get("volume", "—")
        
        # Для Біг-бегу показати вагу в колонці "Габарит / негабарит"
        if size_type == "Біг-бег" and big_bag_weight and big_bag_weight != "—":
            size_type_display = f"Біг-бег - {big_bag_weight} кг/шт"
        else:
            size_type_display = size_type
        
        # Форматувати обсяг
        if volume != "—":
            if data.get("size_type") == "Біг-бег":
                volume_display = f"{volume} шт"
            elif data.get("size_type") in ["Насип", "Рідкі"]:
                volume_display = f"{volume} т"
            else:
                volume_display = f"{volume} т"
        else:
            volume_display = "—"
        
        # Обробити дату/період
        date_period = data.get("date_period", "—")
        date_start = "—"
        date_end = "—"
        
        if date_period and date_period != "—":
            # Перевірити, чи це період (містить " - ")
            if " - " in date_period:
                # Розділити на дату початку і кінця
                parts = date_period.split(" - ")
                if len(parts) == 2:
                    date_start = parts[0].strip()
                    date_end = parts[1].strip()
                else:
                    date_start = date_period
            else:
                # Одна дата - це дата початку
                date_start = date_period
        
        # Підготувати рядок даних по заголовках таблиці
        headers = worksheet.row_values(1)
        if not headers:
            logger.error(f"Sheet '{worksheet_name}' has empty header row")
            return False
        header_map = {h.strip(): idx for idx, h in enumerate(headers) if h and h.strip()}
        row = ["" for _ in headers]
        values_by_header = {
            "ID заявки": data.get("request_id", "—"),
            "Статус": "АКТИВНА",
            "Дата": date_str,
            "Час": time_str,
            "Ініціатор заявки": data.get("initiator", "—"),
            "Підприємство": data.get("company", "—"),
            "Тип авто": data.get("vehicle_type", "—"),
            "Вид вантажу": data.get("cargo_type", "—"),
            "Габарит / негабарит": size_type_display,
            "Обсяг": volume_display,
            "Примітка": data.get("notes", "—"),
            "Дата початку": date_start,
            "Дата кінця": date_end,
            "Населений пункт завантаження": data.get("load_city", "—"),
            "Склад завантаження": data.get("load_place", "—"),
            "Спосіб завантаження": data.get("load_method", "—"),
            "Контакт на завантаженні": load_name,
            "Номер телефона на завантаженні": load_phone,
            "Населений пункт розвантаження": data.get("unload_city", "—"),
            "Склад розвантаження": data.get("unload_place", "—"),
            "Спосіб розвантаження": data.get("unload_method", "—"),
            "Контакт на розвантаженні": unload_name,
            "Номер телефона на розвантаженні": unload_phone,
        }

        for header, value in values_by_header.items():
            idx = header_map.get(header)
            if idx is None:
                logger.warning(f"Header '{header}' not found in sheet '{worksheet_name}'")
                continue
            row[idx] = value
        
        # Додати рядок в перший вільний рядок (найновіші внизу)
        worksheet.append_row(row, value_input_option='USER_ENTERED')
        
        logger.info(f"Successfully exported request to Google Sheets (spreadsheet: {spreadsheet_id})")
        return True
        
    except Exception as e:
        logger.error(f"Error exporting to Google Sheets: {e}")
        return False


def mark_request_deleted(request_id: str, deleted_by: str = "") -> Tuple[bool, str]:
    """Позначити заявку як видалену за ID заявки."""
    try:
        if not request_id:
            return False, "Порожній ID заявки"

        client = get_sheets_client()
        if not client:
            return False, "Не вдалося створити клієнт Google Sheets"

        spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID")
        worksheet_name = os.getenv("GOOGLE_WORKSHEET_NAME", "ЗАЯВКА")

        if not spreadsheet_id:
            return False, "GOOGLE_SPREADSHEET_ID не задано"

        spreadsheet = client.open_by_key(spreadsheet_id)
        worksheet = spreadsheet.worksheet(worksheet_name)

        headers = worksheet.row_values(1)
        if not headers:
            return False, "У таблиці порожній рядок заголовків"

        header_map = {h.strip(): idx for idx, h in enumerate(headers) if h and h.strip()}
        id_idx = header_map.get("ID заявки")
        status_idx = header_map.get("Статус")

        if id_idx is None:
            return False, "У таблиці немає колонки 'ID заявки'"
        if status_idx is None:
            return False, "У таблиці немає колонки 'Статус'"

        id_column_values = worksheet.col_values(id_idx + 1)
        normalized_request_id = request_id.strip().upper()

        matched_row = None
        for row_number in range(2, len(id_column_values) + 1):
            row_id = (id_column_values[row_number - 1] or "").strip().upper()
            if row_id == normalized_request_id:
                matched_row = row_number
                break

        if not matched_row:
            return False, f"Заявку з ID '{request_id}' не знайдено"

        current_status = (worksheet.cell(matched_row, status_idx + 1).value or "").strip().upper()
        if current_status == "ВИДАЛЕНО":
            return True, f"Заявка {request_id} вже позначена як ВИДАЛЕНО"

        worksheet.update_cell(matched_row, status_idx + 1, "ВИДАЛЕНО")

        comment_idx = header_map.get("Коментар видалення")
        if comment_idx is not None:
            kyiv_tz = pytz.timezone('Europe/Kyiv')
            timestamp = datetime.now(kyiv_tz).strftime("%d.%m.%Y %H:%M")
            by_text = deleted_by.strip() if deleted_by else "Невідомий користувач"
            worksheet.update_cell(matched_row, comment_idx + 1, f"{timestamp} | {by_text}")

        return True, f"Заявка {request_id} позначена як ВИДАЛЕНО"

    except Exception as e:
        logger.error(f"Error marking request deleted: {e}")
        return False, f"Помилка при позначенні заявки: {e}"


def restore_request(request_id: str, restored_by: str = "") -> Tuple[bool, str]:
    """Відновити заявку (змінити статус на АКТИВНА) за ID заявки."""
    try:
        if not request_id:
            return False, "Порожній ID заявки"

        client = get_sheets_client()
        if not client:
            return False, "Не вдалося створити клієнт Google Sheets"

        spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID")
        worksheet_name = os.getenv("GOOGLE_WORKSHEET_NAME", "ЗАЯВКА")

        if not spreadsheet_id:
            return False, "GOOGLE_SPREADSHEET_ID не задано"

        spreadsheet = client.open_by_key(spreadsheet_id)
        worksheet = spreadsheet.worksheet(worksheet_name)

        headers = worksheet.row_values(1)
        if not headers:
            return False, "У таблиці порожній рядок заголовків"

        header_map = {h.strip(): idx for idx, h in enumerate(headers) if h and h.strip()}
        id_idx = header_map.get("ID заявки")
        status_idx = header_map.get("Статус")

        if id_idx is None:
            return False, "У таблиці немає колонки 'ID заявки'"
        if status_idx is None:
            return False, "У таблиці немає колонки 'Статус'"

        id_column_values = worksheet.col_values(id_idx + 1)
        normalized_request_id = request_id.strip().upper()

        matched_row = None
        for row_number in range(2, len(id_column_values) + 1):
            row_id = (id_column_values[row_number - 1] or "").strip().upper()
            if row_id == normalized_request_id:
                matched_row = row_number
                break

        if not matched_row:
            return False, f"Заявку з ID '{request_id}' не знайдено"

        current_status = (worksheet.cell(matched_row, status_idx + 1).value or "").strip().upper()
        if current_status == "АКТИВНА":
            return True, f"Заявка {request_id} вже має статус АКТИВНА"

        worksheet.update_cell(matched_row, status_idx + 1, "АКТИВНА")

        comment_idx = header_map.get("Коментар відновлення")
        if comment_idx is not None:
            kyiv_tz = pytz.timezone('Europe/Kyiv')
            timestamp = datetime.now(kyiv_tz).strftime("%d.%m.%Y %H:%M")
            by_text = restored_by.strip() if restored_by else "Невідомий користувач"
            worksheet.update_cell(matched_row, comment_idx + 1, f"{timestamp} | {by_text}")

        return True, f"Заявка {request_id} відновлена (статус: АКТИВНА)"

    except Exception as e:
        logger.error(f"Error restoring request: {e}")
        return False, f"Помилка при відновленні заявки: {e}"
