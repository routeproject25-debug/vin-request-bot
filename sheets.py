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
        
        # Підготувати рядок даних
        row = [
            date_str,                                    # 1. Дата
            time_str,                                    # 2. Час
            data.get("initiator", "—"),                  # 3. Ініціатор заявки
            data.get("company", "—"),                    # 4. Підприємство
            data.get("vehicle_type", "—"),               # 5. Тип авто
            data.get("cargo_type", "—"),                 # 6. Вид вантажу
            size_type_display,                           # 7. Габарит / негабарит
            volume_display,                              # 8. Обсяг
            data.get("notes", "—"),                      # 9. Примітка
            date_start,                                  # 10. Дата початку
            date_end,                                    # 11. Дата кінця
            data.get("load_city", "—"),                  # 12. Населений пункт завантаження
            data.get("load_place", "—"),                 # 13. Склад завантаження
            data.get("load_method", "—"),                # 14. Спосіб завантаження
            load_name,                                   # 15. Контакт на завантаженні
            load_phone,                                  # 16. Номер телефона на завантаженні
            data.get("unload_city", "—"),                # 17. Населений пункт розвантаження
            data.get("unload_place", "—"),               # 18. Склад розвантаження
            data.get("unload_method", "—"),              # 19. Спосіб розвантаження
            unload_name,                                 # 20. Контакт на розвантаженні
            unload_phone,                                # 21. Номер телефона на розвантаженні
        ]
        
        # Додати рядок у таблицю
        worksheet.append_row(row, value_input_option='USER_ENTERED')
        
        logger.info(f"Successfully exported request to Google Sheets (spreadsheet: {spreadsheet_id})")
        return True
        
    except Exception as e:
        logger.error(f"Error exporting to Google Sheets: {e}")
        return False
