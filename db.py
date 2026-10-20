import os
import json
import logging
import threading
from contextlib import contextmanager
from typing import Optional, List, Dict, Any
from datetime import date, datetime, timedelta

from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor, Json

import services

# Отримуємо DATABASE_URL з змінних середовища
DATABASE_URL = os.getenv("DATABASE_URL")

logger = logging.getLogger(__name__)

_pool: Optional[pg_pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()


def _get_pool() -> pg_pool.ThreadedConnectionPool:
    """Ліниво створити пул з'єднань (одне з'єднання на запит більше не відкривається)."""
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            if not DATABASE_URL:
                logger.error("DATABASE_URL is not set")
                raise RuntimeError("DATABASE_URL environment variable is not set")
            max_conn = int(os.getenv("DB_POOL_MAX", "10"))
            _pool = pg_pool.ThreadedConnectionPool(1, max_conn, DATABASE_URL)
    return _pool


@contextmanager
def _cursor(dict_rows: bool = False):
    """Курсор з автоматичним commit/rollback і поверненням з'єднання в пул."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        cursor = conn.cursor(cursor_factory=RealDictCursor if dict_rows else None)
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
    finally:
        # Розірване з'єднання (наприклад, після рестарту БД) не повертаємо в обіг.
        pool.putconn(conn, close=bool(conn.closed))


def _delivery_dates(request_data: Dict[str, Any]):
    period = services.extract_delivery_range(request_data)
    return (period[0], period[1]) if period else (None, None)


def init_db():
    """Ініціалізація БД та таблиць"""
    try:
        with _cursor() as cursor:
            # Таблиця шаблонів
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS templates (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    template_name TEXT NOT NULL,
                    template_data JSONB NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Таблиця контактів
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS contacts (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    contact_type TEXT NOT NULL,
                    contact_value TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Таблиця заявок (requests)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS requests (
                    id SERIAL PRIMARY KEY,
                    request_id VARCHAR(8) NOT NULL UNIQUE,
                    message_id BIGINT,
                    user_id BIGINT NOT NULL,
                    thread_id INTEGER,
                    request_data JSONB NOT NULL,
                    status VARCHAR(20) DEFAULT 'АКТИВНА',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Міграції: user_id → BIGINT (ідемпотентно)
            cursor.execute("ALTER TABLE templates ALTER COLUMN user_id TYPE BIGINT")
            cursor.execute("ALTER TABLE contacts ALTER COLUMN user_id TYPE BIGINT")

            # Міграції: дати перевезення, історія статусу, нагадування
            for column_sql in (
                "delivery_date_from DATE",
                "delivery_date_to DATE",
                "status_changed_at TIMESTAMP",
                "status_changed_by TEXT",
                "reminder_chat_id BIGINT",
                "reminder_message_id BIGINT",
            ):
                cursor.execute(f"ALTER TABLE requests ADD COLUMN IF NOT EXISTS {column_sql}")

            # Індекси для швидкості
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_templates_user_id ON templates(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_contacts_user_id ON contacts(user_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_requests_request_id ON requests(request_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_requests_user_id ON requests(user_id)")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_requests_status_delivery ON requests(status, delivery_date_to)"
            )

        backfilled = _backfill_delivery_dates()
        if backfilled:
            logger.info(f"Backfilled delivery dates for {backfilled} requests")
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")


def _backfill_delivery_dates() -> int:
    """Заповнити delivery_date_from/to для старих заявок з request_data.date_period."""
    with _cursor(dict_rows=True) as cursor:
        cursor.execute(
            """
            SELECT request_id, request_data FROM requests
            WHERE delivery_date_to IS NULL AND request_data ? 'date_period'
            """
        )
        rows = cursor.fetchall()
        updated = 0
        for row in rows:
            data = row["request_data"] if isinstance(row["request_data"], dict) else {}
            date_from, date_to = _delivery_dates(data)
            if not date_to:
                continue
            cursor.execute(
                "UPDATE requests SET delivery_date_from = %s, delivery_date_to = %s WHERE request_id = %s",
                (date_from, date_to, row["request_id"]),
            )
            updated += 1
    return updated


def save_template(user_id: int, template_name: str, template_data: Dict[str, Any]) -> bool:
    """Зберегти шаблон заявки"""
    try:
        with _cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO templates (user_id, template_name, template_data)
                VALUES (%s, %s, %s)
                """,
                (user_id, template_name, Json(template_data))
            )
        logger.info(f"✅ Template '{template_name}' saved successfully for user {user_id}")
        return True
    except Exception as e:
        logger.exception(f"❌ Error saving template '{template_name}' for user {user_id}: {e}")
        return False


def get_user_templates(user_id: int) -> List[Dict[str, Any]]:
    """Отримати всі шаблони користувача"""
    try:
        with _cursor(dict_rows=True) as cursor:
            cursor.execute(
                """
                SELECT id, template_name, created_at
                FROM templates
                WHERE user_id = %s
                ORDER BY created_at DESC
                """,
                (user_id,)
            )
            templates = cursor.fetchall()
        return [
            {"id": t["id"], "name": t["template_name"], "created_at": t["created_at"]}
            for t in templates
        ]
    except Exception as e:
        logger.error(f"Error fetching templates: {e}")
        return []


def get_template(template_id: int) -> Optional[Dict[str, Any]]:
    """Отримати конкретний шаблон"""
    try:
        with _cursor(dict_rows=True) as cursor:
            cursor.execute(
                "SELECT id, template_name, template_data FROM templates WHERE id = %s",
                (template_id,)
            )
            template = cursor.fetchone()
        if template:
            raw_data = template["template_data"]
            data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
            return {"id": template["id"], "name": template["template_name"], "data": data}
        return None
    except Exception as e:
        logger.error(f"Error fetching template: {e}")
        return None


def delete_template(template_id: int) -> bool:
    """Видалити шаблон"""
    try:
        with _cursor() as cursor:
            cursor.execute("DELETE FROM templates WHERE id = %s", (template_id,))
        logger.info(f"Template {template_id} deleted")
        return True
    except Exception as e:
        logger.error(f"Error deleting template: {e}")
        return False


def save_contacts(user_id: int, contacts: List[Dict[str, str]]) -> bool:
    """Зберегти контакти користувача (нові — першими, без дублікатів, до 20 штук)."""
    try:
        with _cursor(dict_rows=True) as cursor:
            cursor.execute(
                "SELECT contact_type, contact_value FROM contacts WHERE user_id = %s ORDER BY created_at DESC",
                (user_id,),
            )
            previous = [{"type": r["contact_type"], "value": r["contact_value"]} for r in cursor.fetchall()]

            cursor.execute("DELETE FROM contacts WHERE user_id = %s", (user_id,))

            seen_values = set()
            merged = []
            for contact in list(contacts) + previous:
                value = (contact.get("value", "") or "").strip()
                if not value or value == "—" or value in seen_values:
                    continue
                seen_values.add(value)
                merged.append((contact.get("type", "general"), value))

            # Вставляємо у зворотному порядку, щоб найновіші мали найпізніший created_at.
            base_time = datetime.utcnow()
            for offset, (contact_type, value) in enumerate(reversed(merged[:20])):
                cursor.execute(
                    """
                    INSERT INTO contacts (user_id, contact_type, contact_value, created_at)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (user_id, contact_type, value, base_time + timedelta(microseconds=offset)),
                )
        logger.info(f"Contacts saved for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving contacts: {e}")
        return False


def get_user_contacts(user_id: int) -> List[Dict[str, str]]:
    """Отримати контакти користувача"""
    try:
        with _cursor(dict_rows=True) as cursor:
            cursor.execute(
                """
                SELECT contact_type, contact_value
                FROM contacts
                WHERE user_id = %s
                ORDER BY created_at DESC
                """,
                (user_id,)
            )
            contacts = cursor.fetchall()
        return [{"type": c["contact_type"], "value": c["contact_value"]} for c in contacts]
    except Exception as e:
        logger.error(f"Error fetching contacts: {e}")
        return []


def save_request(request_id: str, user_id: int, request_data: Dict[str, Any], message_id: int = None, thread_id: int = None) -> bool:
    """Зберегти заявку в БД"""
    try:
        clean_data = services.sanitize_request_data(request_data)
        date_from, date_to = _delivery_dates(clean_data)
        with _cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO requests (request_id, user_id, request_data, message_id, thread_id, status,
                                      delivery_date_from, delivery_date_to)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (request_id, user_id, Json(clean_data), message_id, thread_id, services.STATUS_ACTIVE,
                 date_from, date_to)
            )
        logger.info(f"Request {request_id} saved for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving request: {e}")
        return False


def get_request(request_id: str) -> Optional[Dict[str, Any]]:
    """Отримати заявку за ID"""
    try:
        with _cursor(dict_rows=True) as cursor:
            cursor.execute(
                """
                SELECT *
                FROM requests
                WHERE request_id = %s
                   OR UPPER(request_id) = UPPER(%s)
                ORDER BY CASE WHEN request_id = %s THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (request_id, request_id, request_id)
            )
            request = cursor.fetchone()
        return dict(request) if request else None
    except Exception as e:
        logger.error(f"Error fetching request: {e}")
        return None


def update_request_data(request_id: str, request_data: Dict[str, Any]) -> bool:
    """Оновити дані заявки"""
    try:
        clean_data = services.sanitize_request_data(request_data)
        date_from, date_to = _delivery_dates(clean_data)
        with _cursor() as cursor:
            cursor.execute(
                """
                UPDATE requests
                SET request_data = %s, delivery_date_from = %s, delivery_date_to = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE request_id = %s
                """,
                (Json(clean_data), date_from, date_to, request_id)
            )
        logger.info(f"Request {request_id} data updated")
        return True
    except Exception as e:
        logger.error(f"Error updating request data: {e}")
        return False


def update_request_message_id(request_id: str, message_id: int) -> bool:
    """Оновити message_id заявки"""
    try:
        with _cursor() as cursor:
            cursor.execute(
                """
                UPDATE requests
                SET message_id = %s, updated_at = CURRENT_TIMESTAMP
                WHERE request_id = %s
                """,
                (message_id, request_id)
            )
        logger.info(f"Request {request_id} message_id updated to {message_id}")
        return True
    except Exception as e:
        logger.error(f"Error updating request message_id: {e}")
        return False


def set_request_status(request_id: str, status: str, changed_by: str = "") -> bool:
    """Змінити статус заявки (АКТИВНА / ВИКОНАНО / СКАСОВАНО / ВИДАЛЕНО)."""
    try:
        with _cursor() as cursor:
            cursor.execute(
                """
                UPDATE requests
                SET status = %s, status_changed_at = CURRENT_TIMESTAMP, status_changed_by = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE request_id = %s
                """,
                (services.normalize_status(status), changed_by or None, request_id)
            )
        logger.info(f"Request {request_id} status set to {status}")
        return True
    except Exception as e:
        logger.error(f"Error setting request status: {e}")
        return False


def mark_request_as_deleted(request_id: str, changed_by: str = "") -> bool:
    """Позначити заявку як видалену"""
    return set_request_status(request_id, services.STATUS_DELETED, changed_by)


def get_user_requests(user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    """Отримати всі заявки користувача (окрім видалених)"""
    try:
        with _cursor(dict_rows=True) as cursor:
            cursor.execute(
                """
                SELECT * FROM requests
                WHERE user_id = %s AND (status IS NULL OR status != 'ВИДАЛЕНО')
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (user_id, limit)
            )
            requests = cursor.fetchall()
        return [dict(r) for r in requests]
    except Exception as e:
        logger.error(f"Error fetching user requests: {e}")
        return []


def get_last_initiator(user_id: int) -> str:
    """ПІБ ініціатора з останньої заявки користувача (для автозаповнення форми)."""
    try:
        with _cursor(dict_rows=True) as cursor:
            cursor.execute(
                """
                SELECT request_data->>'initiator' AS initiator FROM requests
                WHERE user_id = %s AND COALESCE(request_data->>'initiator', '') NOT IN ('', '—')
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id,),
            )
            row = cursor.fetchone()
        return (row or {}).get("initiator") or ""
    except Exception as e:
        logger.error(f"Error fetching last initiator: {e}")
        return ""


def get_requests_by_date(
    date_str: str,
    department: Optional[str] = None,
    active_only: bool = False,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Отримати заявки за конкретну дату створення з додатковими фільтрами."""
    try:
        day_start = datetime.strptime(date_str, "%d.%m.%Y")
        day_end = day_start + timedelta(days=1)

        dept_filter = (department or "").strip()
        if dept_filter.upper() == "ALL":
            dept_filter = ""

        with _cursor(dict_rows=True) as cursor:
            cursor.execute(
                """
                SELECT *
                FROM requests
                WHERE created_at >= %s
                  AND created_at < %s
                  AND (%s = '' OR COALESCE(request_data->>'department', '') = %s)
                  AND (%s = FALSE OR COALESCE(status, 'АКТИВНА') = 'АКТИВНА')
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (day_start, day_end, dept_filter, dept_filter, active_only, limit),
            )
            rows = cursor.fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error fetching requests by date: {e}")
        return []


def get_requests_for_delivery_summary(
    department: Optional[str] = None,
    active_only: bool = False,
    limit: int = 5000,
    target_date: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Отримати заявки для зведення по даті перевезення (фільтр по даті — в SQL)."""
    try:
        dept_filter = (department or "").strip()
        if dept_filter.upper() == "ALL":
            dept_filter = ""

        with _cursor(dict_rows=True) as cursor:
            cursor.execute(
                """
                SELECT *
                FROM requests
                WHERE (%s = '' OR COALESCE(request_data->>'department', '') = %s)
                  AND (%s = FALSE OR COALESCE(status, 'АКТИВНА') = 'АКТИВНА')
                  AND (%s::date IS NULL OR (delivery_date_from <= %s::date AND delivery_date_to >= %s::date))
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (dept_filter, dept_filter, active_only, target_date, target_date, target_date, limit),
            )
            rows = cursor.fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error fetching requests for delivery summary: {e}")
        return []


def get_requests_due_for_reminder(until_date: date, since_date: date, limit: int = 50) -> List[Dict[str, Any]]:
    """Активні заявки, кінцевий термін яких у проміжку [since_date; until_date]."""
    try:
        with _cursor(dict_rows=True) as cursor:
            cursor.execute(
                """
                SELECT * FROM requests
                WHERE COALESCE(status, 'АКТИВНА') = 'АКТИВНА'
                  AND delivery_date_to IS NOT NULL
                  AND delivery_date_to <= %s
                  AND delivery_date_to >= %s
                ORDER BY delivery_date_to ASC, created_at ASC
                LIMIT %s
                """,
                (until_date, since_date, limit),
            )
            rows = cursor.fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error fetching requests due for reminder: {e}")
        return []


def get_requests_with_reminders() -> List[Dict[str, Any]]:
    """Заявки, для яких у чаті висить повідомлення-нагадування."""
    try:
        with _cursor(dict_rows=True) as cursor:
            cursor.execute(
                """
                SELECT request_id, reminder_chat_id, reminder_message_id FROM requests
                WHERE reminder_message_id IS NOT NULL
                """
            )
            rows = cursor.fetchall()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error fetching reminder messages: {e}")
        return []


def set_reminder_message(request_id: str, chat_id: Optional[int], message_id: Optional[int]) -> bool:
    try:
        with _cursor() as cursor:
            cursor.execute(
                "UPDATE requests SET reminder_chat_id = %s, reminder_message_id = %s WHERE request_id = %s",
                (chat_id, message_id, request_id),
            )
        return True
    except Exception as e:
        logger.error(f"Error saving reminder message id: {e}")
        return False
