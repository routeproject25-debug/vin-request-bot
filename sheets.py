import os
import re
import logging
from typing import Dict, Any, Tuple, Optional, List
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import pytz

logger = logging.getLogger(__name__)

DEFAULT_LOGISTS = [
    "Нетудихата К.",
    "Покотило Д.",
    "Тимошенко Ю.",
]

# Scope для Google Sheets API
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]


def _column_to_letter(col_num: int) -> str:
    """Convert 1-based column number to Google Sheets column letter."""
    result = ""
    n = col_num
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(65 + remainder) + result
    return result


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
        r'\+?38[\s\-]?0\s?[0-9]{2}[\s\-]?[0-9]{3}[\s\-]?[0-9]{2}[\s\-]?[0-9]{2}',  # +380 96 477 39 40
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


def _safe_sheet_value(value: Any) -> Any:
    """Підготувати значення для запису в Google Sheets без спроби обчислення формул."""
    if value is None:
        return "—"
    if not isinstance(value, str):
        return value

    cleaned = value.strip()
    if not cleaned:
        return "—"

    # Прибираємо випадковий апостроф-префікс, щоб значення не ставали текстом у таблиці.
    cleaned = re.sub(r"^'+\s*", "", cleaned)
    if not cleaned:
        return "—"

    # Якщо текст починається з '+', Sheets у USER_ENTERED може трактувати це як формулу.
    if cleaned.startswith("+"):
        return "'" + cleaned
    return cleaned


def _split_multi_values(value: Any) -> List[str]:
    if value is None:
        return []
    text = str(value).strip()
    if not text or text == "—":
        return []
    return [part.strip() for part in text.split(";") if part.strip()]


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except Exception:
        return None


def _format_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _format_number_with_comma(value: float) -> str:
    """Format number with comma as decimal separator."""
    return _format_number(value).replace(".", ",")


def _format_volume_with_unit(size_type: str, volume_value: Any) -> str:
    numeric = _to_float(volume_value)
    if numeric is None:
        return "—"
    # Для Google Sheets зберігаємо тільки число без одиниць,
    # щоб можна було сумувати та будувати зведені таблиці.
    return _format_number_with_comma(numeric)


def _get_logists_list() -> List[str]:
    """Return logist names sorted alphabetically from env or defaults."""
    env_value = (os.getenv("LOGISTS_LIST") or "").strip()
    if env_value:
        raw_items = re.split(r"[,;\n]", env_value)
        items = [item.strip() for item in raw_items if item.strip()]
        if items:
            return sorted(items, key=str.casefold)
    return sorted(DEFAULT_LOGISTS, key=str.casefold)


def _build_grid_range(sheet_id: int, row_number: int, col_index_zero_based: int) -> Dict[str, int]:
    """Build Google Sheets GridRange for a single cell."""
    return {
        "sheetId": sheet_id,
        "startRowIndex": row_number - 1,
        "endRowIndex": row_number,
        "startColumnIndex": col_index_zero_based,
        "endColumnIndex": col_index_zero_based + 1,
    }


def _apply_logist_and_execution_validations(
    worksheet,
    header_map: Dict[str, int],
    target_row_ids: List[str],
) -> None:
    """Apply logist dropdown and execution checkbox for target rows."""
    logist_idx = header_map.get("Логіст")
    execution_idx = header_map.get("Виконання")
    id_idx = header_map.get("ID заявки")
    if logist_idx is None or execution_idx is None or id_idx is None:
        return

    unique_ids = [rid for rid in dict.fromkeys(target_row_ids) if rid]
    if not unique_ids:
        return

    id_column_values = worksheet.col_values(id_idx + 1)
    row_by_id: Dict[str, int] = {}
    for row_number in range(2, len(id_column_values) + 1):
        row_id = (id_column_values[row_number - 1] or "").strip().upper()
        if row_id:
            row_by_id[row_id] = row_number

    sheet_id = worksheet.id
    logists = _get_logists_list()
    one_of_list_values = [{"userEnteredValue": name} for name in logists]

    requests = []
    for rid in unique_ids:
        row_number = row_by_id.get(rid)
        if not row_number:
            continue

        requests.append(
            {
                "setDataValidation": {
                    "range": _build_grid_range(sheet_id, row_number, logist_idx),
                    "rule": {
                        "condition": {
                            "type": "ONE_OF_LIST",
                            "values": one_of_list_values,
                        },
                        "strict": True,
                        "showCustomUi": True,
                    },
                }
            }
        )
        requests.append(
            {
                "setDataValidation": {
                    "range": _build_grid_range(sheet_id, row_number, execution_idx),
                    "rule": {
                        "condition": {
                            "type": "BOOLEAN",
                            "values": [],
                        },
                        "strict": True,
                        "showCustomUi": True,
                    },
                }
            }
        )

    if requests:
        worksheet.spreadsheet.batch_update({"requests": requests})


def _normalize_date_text(value: Any) -> str:
    """Normalize date text and drop accidental leading apostrophe for dd.mm.yyyy values."""
    if value is None:
        return "—"

    text = str(value).strip()
    if not text:
        return "—"

    # Sheets/API clients may pass text date like `'22.03.2026`; keep only the date part.
    match = re.match(r"^'+\s*(\d{2}\.\d{2}\.\d{4})$", text)
    if match:
        return match.group(1)

    return text


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


def export_to_sheets(data: Dict[str, Any]) -> Tuple[bool, str]:
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
        logger.info("=== Starting export to Google Sheets ===")
        logger.info(f"Request ID: {data.get('request_id', 'N/A')}")
        
        client = get_sheets_client()
        if not client:
            error_msg = "Не вдалося створити Google Sheets client"
            logger.error(error_msg)
            return False, error_msg
        logger.info("✓ Google Sheets client created")
        
        spreadsheet_id = os.getenv("GOOGLE_SPREADSHEET_ID")
        worksheet_name = os.getenv("GOOGLE_WORKSHEET_NAME", "ЗАЯВКА")
        
        if not spreadsheet_id:
            error_msg = "GOOGLE_SPREADSHEET_ID не задано"
            logger.error(error_msg)
            return False, error_msg
        
        logger.info(f"Opening spreadsheet: {spreadsheet_id}, worksheet: {worksheet_name}")
        
        # Відкрити таблицю
        spreadsheet = client.open_by_key(spreadsheet_id)
        logger.info(f"✓ Spreadsheet opened: {spreadsheet.title}")
        
        worksheet = spreadsheet.worksheet(worksheet_name)
        logger.info(f"✓ Worksheet opened: {worksheet.title}")
        
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
        volume_display = _format_volume_with_unit(data.get("size_type", ""), volume)
        
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
                    date_start = _normalize_date_text(parts[0])
                    date_end = _normalize_date_text(parts[1])
                else:
                    date_start = _normalize_date_text(date_period)
            else:
                # Одна дата - це дата початку
                date_start = _normalize_date_text(date_period)
        
        # Підготувати рядок(и) даних по заголовках таблиці
        headers = worksheet.row_values(1)
        logger.info(f"✓ Headers read: {len(headers)} columns")
        logger.debug(f"Headers: {headers}")
        
        if not headers:
            error_msg = f"Аркуш '{worksheet_name}' має порожній рядок заголовків"
            logger.error(error_msg)
            return False, error_msg
        
        header_map = {h.strip(): idx for idx, h in enumerate(headers) if h and h.strip()}
        logger.info(f"✓ Header map created with {len(header_map)} entries")
        request_id = (data.get("request_id") or "").strip().upper()
        base_values_by_header = {
            "ID заявки": request_id or "—",
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

        if "Головний ID заявки" in header_map:
            base_values_by_header["Головний ID заявки"] = request_id or "—"

        # Якщо є декілька НП розвантаження і розподіл — формуємо окремі рядки по кожному НП.
        unload_cities = _split_multi_values(data.get("unload_city"))
        raw_distribution = data.get("unload_distribution")
        distribution = raw_distribution if isinstance(raw_distribution, dict) else {}
        raw_ids_map = data.get("unload_distribution_ids")
        ids_map = raw_ids_map if isinstance(raw_ids_map, dict) else {}

        rows_payload: List[Dict[str, Any]] = []
        if len(unload_cities) > 1 and distribution:
            for idx, city in enumerate(unload_cities, start=1):
                city_volume = distribution.get(city)
                if city_volume is None:
                    continue

                city_values = dict(base_values_by_header)
                if request_id:
                    stable_child_id = str(ids_map.get(city) or "").strip().upper()
                    city_values["ID заявки"] = stable_child_id if stable_child_id else f"{request_id}-{idx:02d}"
                city_values["Населений пункт розвантаження"] = city
                city_values["Склад розвантаження"] = "—"
                city_values["Обсяг"] = _format_volume_with_unit(data.get("size_type", ""), city_volume)
                rows_payload.append(city_values)

            if rows_payload:
                logger.info(f"✓ Prepared {len(rows_payload)} split rows for unload cities")

        if not rows_payload:
            # fallback: стандартний один рядок
            if len(unload_cities) > 1:
                base_values_by_header["Склад розвантаження"] = "—"
            rows_payload = [base_values_by_header]

        rows: List[List[Any]] = []
        for values_by_header in rows_payload:
            row = ["" for _ in headers]
            for header, value in values_by_header.items():
                idx = header_map.get(header)
                if idx is None:
                    logger.warning(f"Header '{header}' not found in sheet '{worksheet_name}'")
                    continue
                row[idx] = _safe_sheet_value(value)
            rows.append(row)

        logger.info(f"✓ Prepared {len(rows)} row(s) for export")

        # Ідемпотентний upsert: оновити рядки з тим самим ID заявки, нові — вставити.
        id_idx = header_map.get("ID заявки")
        if id_idx is None:
            return False, "У таблиці немає колонки 'ID заявки'"

        id_column_values = worksheet.col_values(id_idx + 1)
        existing_by_id: Dict[str, int] = {}
        for row_number in range(2, len(id_column_values) + 1):
            row_id = (id_column_values[row_number - 1] or "").strip().upper()
            if row_id:
                existing_by_id[row_id] = row_number

        target_row_ids: List[str] = []
        for row in rows:
            row_id = ""
            if id_idx < len(row):
                row_id = (str(row[id_idx]) or "").strip().upper()
                if row_id:
                    target_row_ids.append(row_id)

            if row_id and row_id in existing_by_id:
                row_number = existing_by_id[row_id]
                safe_row = [_safe_sheet_value(value) for value in row]
                end_col_letter = _column_to_letter(len(safe_row))
                worksheet.update(
                    f"A{row_number}:{end_col_letter}{row_number}",
                    [safe_row],
                    value_input_option="USER_ENTERED",
                )
            else:
                safe_row = [_safe_sheet_value(value) for value in row]
                worksheet.insert_row(safe_row, index=2, value_input_option='USER_ENTERED')

        try:
            _apply_logist_and_execution_validations(worksheet, header_map, target_row_ids)
        except Exception as e:
            logger.warning(f"Failed to apply logist/execution validations: {e}")
        
        logger.info(f"✅ Successfully exported request to Google Sheets (spreadsheet: {spreadsheet_id})")
        return True, ""
        
    except Exception as e:
        import traceback
        logger.error(f"❌ Error exporting to Google Sheets: {e}")
        logger.error(f"Full traceback:\n{traceback.format_exc()}")
        return False, str(e)


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
        parent_request_id = normalized_request_id.split("-", 1)[0] if "-" in normalized_request_id else normalized_request_id

        matched_rows: List[int] = []
        for row_number in range(2, len(id_column_values) + 1):
            row_id = (id_column_values[row_number - 1] or "").strip().upper()
            if (
                row_id == normalized_request_id
                or row_id == parent_request_id
                or row_id.startswith(f"{parent_request_id}-")
            ):
                matched_rows.append(row_number)

        if not matched_rows:
            return False, f"Заявку з ID '{request_id}' не знайдено"

        updated_count = 0
        for row_number in matched_rows:
            current_status = (worksheet.cell(row_number, status_idx + 1).value or "").strip().upper()
            if current_status == "ВИДАЛЕНО":
                continue
            worksheet.update_cell(row_number, status_idx + 1, "ВИДАЛЕНО")
            updated_count += 1

        comment_idx = header_map.get("Коментар видалення")
        if comment_idx is not None:
            kyiv_tz = pytz.timezone('Europe/Kyiv')
            timestamp = datetime.now(kyiv_tz).strftime("%d.%m.%Y %H:%M")
            by_text = deleted_by.strip() if deleted_by else "Невідомий користувач"
            for row_number in matched_rows:
                worksheet.update_cell(row_number, comment_idx + 1, f"{timestamp} | {by_text}")

        if updated_count == 0:
            return True, f"Заявка {request_id} вже позначена як ВИДАЛЕНО"
        return True, f"Заявка {request_id} позначена як ВИДАЛЕНО ({updated_count} рядків)"

    except Exception as e:
        logger.error(f"Error marking request deleted: {e}")
        return False, f"Помилка при позначенні заявки: {e}"


def mark_request_deleted_exact(request_id: str, deleted_by: str = "") -> Tuple[bool, str]:
    """Позначити як ВИДАЛЕНО тільки один конкретний рядок за точним ID заявки."""
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

        normalized_request_id = request_id.strip().upper()
        id_column_values = worksheet.col_values(id_idx + 1)

        matched_row = None
        for row_number in range(2, len(id_column_values) + 1):
            row_id = (id_column_values[row_number - 1] or "").strip().upper()
            if row_id == normalized_request_id:
                matched_row = row_number
                break

        if not matched_row:
            return False, f"Заявку з ID '{request_id}' не знайдено"

        current_status = (worksheet.cell(matched_row, status_idx + 1).value or "").strip().upper()
        if current_status != "ВИДАЛЕНО":
            worksheet.update_cell(matched_row, status_idx + 1, "ВИДАЛЕНО")

        comment_idx = header_map.get("Коментар видалення")
        if comment_idx is not None:
            kyiv_tz = pytz.timezone('Europe/Kyiv')
            timestamp = datetime.now(kyiv_tz).strftime("%d.%m.%Y %H:%M")
            by_text = deleted_by.strip() if deleted_by else "Невідомий користувач"
            worksheet.update_cell(matched_row, comment_idx + 1, f"{timestamp} | {by_text}")

        return True, f"Заявка {request_id} позначена як ВИДАЛЕНО"
    except Exception as e:
        logger.error(f"Error marking request deleted exactly: {e}")
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
        parent_request_id = normalized_request_id.split("-", 1)[0] if "-" in normalized_request_id else normalized_request_id

        matched_rows: List[int] = []
        for row_number in range(2, len(id_column_values) + 1):
            row_id = (id_column_values[row_number - 1] or "").strip().upper()
            if (
                row_id == normalized_request_id
                or row_id == parent_request_id
                or row_id.startswith(f"{parent_request_id}-")
            ):
                matched_rows.append(row_number)

        if not matched_rows:
            return False, f"Заявку з ID '{request_id}' не знайдено"

        updated_count = 0
        for row_number in matched_rows:
            current_status = (worksheet.cell(row_number, status_idx + 1).value or "").strip().upper()
            if current_status == "АКТИВНА":
                continue
            worksheet.update_cell(row_number, status_idx + 1, "АКТИВНА")
            updated_count += 1

        comment_idx = header_map.get("Коментар відновлення")
        if comment_idx is not None:
            kyiv_tz = pytz.timezone('Europe/Kyiv')
            timestamp = datetime.now(kyiv_tz).strftime("%d.%m.%Y %H:%M")
            by_text = restored_by.strip() if restored_by else "Невідомий користувач"
            for row_number in matched_rows:
                worksheet.update_cell(row_number, comment_idx + 1, f"{timestamp} | {by_text}")

        if updated_count == 0:
            return True, f"Заявка {request_id} вже має статус АКТИВНА"
        return True, f"Заявка {request_id} відновлена (статус: АКТИВНА, {updated_count} рядків)"

    except Exception as e:
        logger.error(f"Error restoring request: {e}")
        return False, f"Помилка при відновленні заявки: {e}"
