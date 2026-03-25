import os
import json
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor, Json

# Отримуємо DATABASE_URL з змінних середовища
DATABASE_URL = os.getenv("DATABASE_URL")

logger = logging.getLogger(__name__)


def get_connection():
    """Отримати з'єднання з PostgreSQL"""
    if not DATABASE_URL:
        logger.error("DATABASE_URL is not set")
        raise RuntimeError("DATABASE_URL environment variable is not set")
    
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logger.error(f"Error connecting to database: {e}")
        raise


def init_db():
    """Ініціалізація БД та таблиць"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
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
        
        # Міграція: оновити тип user_id з INTEGER на BIGINT (якщо таблиці вже існують)
        try:
            cursor.execute("""
                ALTER TABLE templates 
                ALTER COLUMN user_id TYPE BIGINT
            """)
            logger.info("Migrated templates.user_id to BIGINT")
        except Exception as e:
            logger.debug(f"Templates.user_id migration skipped: {e}")
        
        try:
            cursor.execute("""
                ALTER TABLE contacts 
                ALTER COLUMN user_id TYPE BIGINT
            """)
            logger.info("Migrated contacts.user_id to BIGINT")
        except Exception as e:
            logger.debug(f"Contacts.user_id migration skipped: {e}")
        
        # Індекси для швидкості
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_templates_user_id 
            ON templates(user_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_contacts_user_id 
            ON contacts(user_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_requests_request_id 
            ON requests(request_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_requests_user_id 
            ON requests(user_id)
        """)
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")


def save_template(user_id: int, template_name: str, template_data: Dict[str, Any]) -> bool:
    """Зберегти шаблон заявки"""
    try:
        # Перевірити зв'язок
        conn = get_connection()
        if not conn:
            logger.error("Failed to establish database connection")
            return False
        
        cursor = conn.cursor()
        
        # Логувати що збігаємо
        logger.info(f"Attempting to save template '{template_name}' for user {user_id}")
        logger.info(f"Template data keys: {list(template_data.keys())}")
        
        cursor.execute(
            """
            INSERT INTO templates (user_id, template_name, template_data)
            VALUES (%s, %s, %s)
            """,
            (user_id, template_name, Json(template_data))
        )
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"✅ Template '{template_name}' saved successfully for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"❌ Error saving template '{template_name}' for user {user_id}")
        logger.error(f"Exception type: {type(e).__name__}")
        logger.error(f"Exception message: {e}")
        logger.error(f"Template data: {template_data}")
        import traceback
        logger.error(f"Full traceback:\n{traceback.format_exc()}")
        return False


def get_user_templates(user_id: int) -> List[Dict[str, Any]]:
    """Отримати всі шаблони користувача"""
    try:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
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
        cursor.close()
        conn.close()

        return [
            {
                "id": t["id"],
                "name": t["template_name"],
                "created_at": t["created_at"],
            }
            for t in templates
        ]
    except Exception as e:
        logger.error(f"Error fetching templates: {e}")
        return []


def get_template(template_id: int) -> Optional[Dict[str, Any]]:
    """Отримати конкретний шаблон"""
    try:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute(
            """
            SELECT id, template_name, template_data
            FROM templates
            WHERE id = %s
            """,
            (template_id,)
        )
        
        template = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if template:
            raw_data = template["template_data"]
            data = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
            return {
                "id": template["id"],
                "name": template["template_name"],
                "data": data,
            }
        return None
    except Exception as e:
        logger.error(f"Error fetching template: {e}")
        return None


def delete_template(template_id: int) -> bool:
    """Видалити шаблон"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            "DELETE FROM templates WHERE id = %s",
            (template_id,)
        )
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"Template {template_id} deleted")
        return True
    except Exception as e:
        logger.error(f"Error deleting template: {e}")
        return False


def save_contacts(user_id: int, contacts: List[Dict[str, str]]) -> bool:
    """Зберегти контакти користувача"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Видалити старі контакти
        cursor.execute("DELETE FROM contacts WHERE user_id = %s", (user_id,))
        
        # Додати нові (без дублікатів)
        seen_values = set()
        for contact in contacts:
            value = (contact.get("value", "") or "").strip()
            if not value or value in seen_values:
                continue
            seen_values.add(value)
            cursor.execute(
                """
                INSERT INTO contacts (user_id, contact_type, contact_value)
                VALUES (%s, %s, %s)
                """,
                (user_id, contact.get("type", "general"), value)
            )
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"Contacts saved for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving contacts: {e}")
        return False


def get_user_contacts(user_id: int) -> List[Dict[str, str]]:
    """Отримати контакти користувача"""
    try:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
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
        cursor.close()
        conn.close()
        
        return [
            {"type": c["contact_type"], "value": c["contact_value"]}
            for c in contacts
        ]
    except Exception as e:
        logger.error(f"Error fetching contacts: {e}")
        return []


def save_request(request_id: str, user_id: int, request_data: Dict[str, Any], message_id: int = None, thread_id: int = None) -> bool:
    """Зберегти заявку в БД"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            """
            INSERT INTO requests (request_id, user_id, request_data, message_id, thread_id, status)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (request_id, user_id, Json(request_data), message_id, thread_id, "АКТИВНА")
        )
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"Request {request_id} saved for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving request: {e}")
        return False


def get_request(request_id: str) -> Optional[Dict[str, Any]]:
    """Отримати заявку за ID"""
    try:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute(
            """
            SELECT * FROM requests WHERE request_id = %s
            """,
            (request_id,)
        )
        
        request = cursor.fetchone()
        cursor.close()
        conn.close()
        
        return dict(request) if request else None
    except Exception as e:
        logger.error(f"Error fetching request: {e}")
        return None


def update_request_data(request_id: str, request_data: Dict[str, Any]) -> bool:
    """Оновити дані заявки"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            """
            UPDATE requests 
            SET request_data = %s, updated_at = CURRENT_TIMESTAMP
            WHERE request_id = %s
            """,
            (Json(request_data), request_id)
        )
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"Request {request_id} data updated")
        return True
    except Exception as e:
        logger.error(f"Error updating request data: {e}")
        return False


def update_request_message_id(request_id: str, message_id: int) -> bool:
    """Оновити message_id заявки"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            """
            UPDATE requests 
            SET message_id = %s, updated_at = CURRENT_TIMESTAMP
            WHERE request_id = %s
            """,
            (message_id, request_id)
        )
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"Request {request_id} message_id updated to {message_id}")
        return True
    except Exception as e:
        logger.error(f"Error updating request message_id: {e}")
        return False


def mark_request_as_deleted(request_id: str) -> bool:
    """Позначити заявку як видалену"""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            """
            UPDATE requests 
            SET status = %s, updated_at = CURRENT_TIMESTAMP
            WHERE request_id = %s
            """,
            ("ВИДАЛЕНО", request_id)
        )
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"Request {request_id} marked as deleted")
        return True
    except Exception as e:
        logger.error(f"Error marking request as deleted: {e}")
        return False


def get_user_requests(user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    """Отримати всі заявки користувача (окрім видалених)"""
    try:
        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
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
        cursor.close()
        conn.close()
        
        return [dict(r) for r in requests]
    except Exception as e:
        logger.error(f"Error fetching user requests: {e}")
        return []


def get_requests_by_date(
    date_str: str,
    department: Optional[str] = None,
    active_only: bool = False,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Отримати заявки за конкретну дату з додатковими фільтрами."""
    try:
        day_start = datetime.strptime(date_str, "%d.%m.%Y")
        day_end = day_start + timedelta(days=1)

        conn = get_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)

        dept_filter = (department or "").strip()
        if dept_filter.upper() == "ALL":
            dept_filter = ""

        cursor.execute(
            """
            SELECT *
            FROM requests
            WHERE created_at >= %s
              AND created_at < %s
              AND (%s = '' OR COALESCE(request_data->>'department', '') = %s)
              AND (%s = FALSE OR COALESCE(status, 'АКТИВНА') != 'ВИДАЛЕНО')
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (day_start, day_end, dept_filter, dept_filter, active_only, limit),
        )

        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error fetching requests by date: {e}")
        return []
