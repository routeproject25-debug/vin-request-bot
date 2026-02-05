import os
import json
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor

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
                user_id INTEGER NOT NULL,
                template_name TEXT NOT NULL,
                template_data JSONB NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Таблиця контактів
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL,
                contact_type TEXT NOT NULL,
                contact_value TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Індекси для швидкості
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_templates_user_id 
            ON templates(user_id)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_contacts_user_id 
            ON contacts(user_id)
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
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute(
            """
            INSERT INTO templates (user_id, template_name, template_data)
            VALUES (%s, %s, %s)
            """,
            (user_id, template_name, json.dumps(template_data))
        )
        
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"Template '{template_name}' saved for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving template: {e}")
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
        
        # Додати нові
        for contact in contacts:
            cursor.execute(
                """
                INSERT INTO contacts (user_id, contact_type, contact_value)
                VALUES (%s, %s, %s)
                """,
                (user_id, contact.get("type", "general"), contact.get("value", ""))
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
