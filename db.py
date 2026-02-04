import sqlite3
import json
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime

DB_PATH = "requests.db"

logger = logging.getLogger(__name__)


def init_db():
    """Ініціалізація БД та таблиць"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                template_name TEXT NOT NULL,
                template_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                contact_type TEXT NOT NULL,
                contact_value TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")


def save_template(user_id: int, template_name: str, template_data: Dict[str, Any]) -> bool:
    """Зберегти шаблон заявки"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        template_json = json.dumps(template_data, ensure_ascii=False)
        cursor.execute(
            """
            INSERT INTO templates (user_id, template_name, template_data)
            VALUES (?, ?, ?)
            """,
            (user_id, template_name, template_json)
        )
        
        conn.commit()
        conn.close()
        logger.info(f"Template '{template_name}' saved for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving template: {e}")
        return False


def get_user_templates(user_id: int) -> List[Dict[str, Any]]:
    """Отримати список всіх шаблонів користувача"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute(
            """
            SELECT id, template_name, created_at 
            FROM templates 
            WHERE user_id = ? 
            ORDER BY created_at DESC
            """,
            (user_id,)
        )
        
        rows = cursor.fetchall()
        conn.close()
        
        return [
            {
                "id": row[0],
                "name": row[1],
                "created_at": row[2]
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"Error getting templates: {e}")
        return []


def get_template(template_id: int) -> Optional[Dict[str, Any]]:
    """Отримати конкретний шаблон"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute(
            """
            SELECT id, template_name, template_data 
            FROM templates 
            WHERE id = ?
            """,
            (template_id,)
        )
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return {
                "id": row[0],
                "name": row[1],
                "data": json.loads(row[2])
            }
        return None
    except Exception as e:
        logger.error(f"Error getting template: {e}")
        return None


def delete_template(template_id: int) -> bool:
    """Видалити шаблон"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        
        conn.commit()
        conn.close()
        logger.info(f"Template {template_id} deleted")
        return True
    except Exception as e:
        logger.error(f"Error deleting template: {e}")
        return False


def save_contacts(user_id: int, contact_type: str, contact_value: str) -> bool:
    """Зберегти контакт"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute(
            """
            INSERT INTO contacts (user_id, contact_type, contact_value)
            VALUES (?, ?, ?)
            """,
            (user_id, contact_type, contact_value)
        )
        
        conn.commit()
        conn.close()
        logger.info(f"Contact saved for user {user_id}")
        return True
    except Exception as e:
        logger.error(f"Error saving contact: {e}")
        return False


def get_user_contacts(user_id: int, contact_type: str) -> List[str]:
    """Отримати контакти користувача за типом"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute(
            """
            SELECT DISTINCT contact_value 
            FROM contacts 
            WHERE user_id = ? AND contact_type = ? 
            ORDER BY created_at DESC 
            LIMIT 5
            """,
            (user_id, contact_type)
        )
        
        rows = cursor.fetchall()
        conn.close()
        
        return [row[0] for row in rows]
    except Exception as e:
        logger.error(f"Error getting contacts: {e}")
        return []
