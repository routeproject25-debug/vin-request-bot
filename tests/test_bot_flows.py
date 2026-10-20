"""Інтеграційні сценарії чат-бота через справжній Application.process_update.

Telegram API, PostgreSQL і Google Sheets підмінені фейками в пам'яті.
"""
import asyncio
import copy
import itertools
from datetime import datetime, timedelta, timezone

import pytest

telegram = pytest.importorskip("telegram")

import db  # noqa: E402
import services  # noqa: E402
import sheets  # noqa: E402

USER_ID = 501
LOGIST_ID = 777
GROUP_ID = -100500


# ---------- фейки ----------

class FakeDB:
    def __init__(self):
        self.rows = {}

    def install(self, monkeypatch, bot_module):
        for name in (
            "get_request", "save_request", "update_request_data", "get_user_requests", "get_user_templates",
            "get_user_contacts", "save_contacts", "set_request_status", "mark_request_as_deleted",
            "get_requests_due_for_reminder", "get_requests_with_reminders", "set_reminder_message",
            "init_db", "get_last_initiator",
        ):
            monkeypatch.setattr(db, name, getattr(self, name))
        monkeypatch.setattr(bot_module, "get_user_contacts", self.get_user_contacts)

    def add(self, request_id, user_id, data, status="АКТИВНА", message_id=1000):
        period = services.extract_delivery_range(data)
        self.rows[request_id] = {
            "request_id": request_id, "user_id": user_id, "status": status, "message_id": message_id,
            "thread_id": 4, "request_data": dict(data, request_id=request_id),
            "created_at": datetime(2026, 9, 1, 8, 0), "delivery_date_to": period[1] if period else None,
            "reminder_chat_id": None, "reminder_message_id": None,
        }

    def init_db(self):
        pass

    def get_request(self, request_id):
        row = self.rows.get(str(request_id).upper())
        return copy.deepcopy(row) if row else None

    def save_request(self, request_id, user_id, request_data, message_id=None, thread_id=None):
        self.add(request_id, user_id, services.sanitize_request_data(request_data), message_id=message_id)
        self.rows[request_id]["thread_id"] = thread_id
        return True

    def update_request_data(self, request_id, request_data):
        row = self.rows[request_id]
        row["request_data"] = services.sanitize_request_data(request_data)
        period = services.extract_delivery_range(row["request_data"])
        row["delivery_date_to"] = period[1] if period else None
        return True

    def set_request_status(self, request_id, status, changed_by=""):
        self.rows[request_id]["status"] = status
        return True

    def mark_request_as_deleted(self, request_id, changed_by=""):
        return self.set_request_status(request_id, "ВИДАЛЕНО")

    def get_user_requests(self, user_id, limit=50):
        return [copy.deepcopy(r) for r in self.rows.values() if r["user_id"] == user_id and r["status"] != "ВИДАЛЕНО"]

    def get_user_templates(self, user_id):
        return []

    def get_user_contacts(self, user_id):
        return []

    def save_contacts(self, user_id, contacts):
        return True

    def get_last_initiator(self, user_id):
        return ""

    def get_requests_due_for_reminder(self, until_date, since_date, limit=50):
        return [copy.deepcopy(r) for r in self.rows.values()
                if r["status"] == "АКТИВНА" and r["delivery_date_to"] and since_date <= r["delivery_date_to"] <= until_date]

    def get_requests_with_reminders(self):
        return [copy.deepcopy(r) for r in self.rows.values() if r["reminder_message_id"]]

    def set_reminder_message(self, request_id, chat_id, message_id):
        self.rows[request_id]["reminder_chat_id"] = chat_id
        self.rows[request_id]["reminder_message_id"] = message_id
        return True


class Recorder:
    """Підміняє методи telegram.Bot і записує всі виклики."""

    def __init__(self):
        self.calls = []
        self._ids = itertools.count(2000)

    def of(self, method):
        return [kwargs for name, kwargs in self.calls if name == method]

    def texts(self, chat_id=None):
        return [c.get("text", "") for c in self.of("send_message") if chat_id is None or c["chat_id"] == chat_id]

    def install(self, monkeypatch):
        recorder = self

        def chat(chat_id):
            return telegram.Chat(id=int(chat_id), type="private" if int(chat_id) > 0 else "supergroup")

        async def send_message(self, chat_id, text, **kwargs):
            recorder.calls.append(("send_message", dict(kwargs, chat_id=int(chat_id), text=text)))
            message = telegram.Message(next(recorder._ids), datetime.now(timezone.utc), chat(chat_id), text=text)
            message.set_bot(self)
            return message

        async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
            recorder.calls.append(("edit_message_text", dict(kwargs, chat_id=chat_id, message_id=message_id, text=text)))
            return True

        async def generic(name):
            async def method(self, *args, **kwargs):
                recorder.calls.append((name, kwargs))
                return True
            return method

        async def get_me(self, *args, **kwargs):
            self._bot_user = telegram.User(id=1, first_name="Bot", is_bot=True, username="test_bot")
            return self._bot_user

        async def get_chat(self, chat_id, **kwargs):
            return telegram.Chat(id=chat_id, type="private", username="owner")

        monkeypatch.setattr(telegram.Bot, "send_message", send_message)
        monkeypatch.setattr(telegram.Bot, "edit_message_text", edit_message_text)
        monkeypatch.setattr(telegram.Bot, "get_me", get_me)
        monkeypatch.setattr(telegram.Bot, "get_chat", get_chat)
        for name in ("delete_message", "answer_callback_query", "edit_message_reply_markup", "set_chat_menu_button"):
            monkeypatch.setattr(telegram.Bot, name, asyncio.run(generic(name)))


# ---------- середовище ----------

@pytest.fixture
def harness(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:TEST")
    monkeypatch.setenv("TARGET_CHAT_ID", str(GROUP_ID))
    monkeypatch.delenv("WEBAPP_URL", raising=False)
    for name in ("ADMIN_USER_IDS", "ADMIN_USER_ID", "ADMIN_USERNAMES", "LOGIST_USER_IDS", "LOGIST_USERNAMES"):
        monkeypatch.delenv(name, raising=False)

    import bot as bot_module

    fake_db = FakeDB()
    fake_db.install(monkeypatch, bot_module)
    sheet_calls = []
    monkeypatch.setattr(sheets, "export_to_sheets", lambda data, status="АКТИВНА": sheet_calls.append(("export", data, status)) or (True, ""))
    monkeypatch.setattr(sheets, "set_request_status", lambda *a, **k: sheet_calls.append(("status", a)) or (True, ""))
    monkeypatch.setattr(sheets, "mark_request_deleted_exact", lambda *a, **k: sheet_calls.append(("del_exact", a)) or (True, ""))
    monkeypatch.setattr(sheets, "mark_request_deleted", lambda *a, **k: sheet_calls.append(("del", a)) or (True, ""))
    recorder = Recorder()
    recorder.install(monkeypatch)

    try:
        app = bot_module.build_app()
    except AttributeError as e:  # PTB 20.7 не працює на Python 3.13+
        pytest.skip(f"python-telegram-bot 20.7 is incompatible with this Python: {e}")

    class Harness:
        pass

    h = Harness()
    h.app, h.db, h.rec, h.sheets, h.bot_module = app, fake_db, recorder, sheet_calls, bot_module
    h.update_ids = itertools.count(1)
    h.message_ids = itertools.count(100)

    def user(uid):
        return {"id": uid, "is_bot": False, "first_name": f"U{uid}", "username": f"user{uid}"}

    def private_chat(uid):
        return {"id": uid, "type": "private", "first_name": f"U{uid}"}

    def text(message_text, uid=USER_ID):
        message = {"message_id": next(h.message_ids), "date": 0, "chat": private_chat(uid), "from": user(uid),
                   "text": message_text}
        if message_text.startswith("/"):
            command_length = len(message_text.split(" ", 1)[0])
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": command_length}]
        return {"update_id": next(h.update_ids), "message": message}

    def callback(data, uid=USER_ID, chat=None):
        chat = chat or private_chat(uid)
        return {
            "update_id": next(h.update_ids),
            "callback_query": {
                "id": str(next(h.update_ids)), "from": user(uid), "chat_instance": "ci", "data": data,
                "message": {"message_id": next(h.message_ids), "date": 0, "chat": chat, "from": user(1), "text": "…"},
            },
        }

    loop = asyncio.new_event_loop()
    loop.run_until_complete(app.initialize())

    async def run(*updates):
        for raw in updates:
            await app.process_update(telegram.Update.de_json(raw, app.bot))

    h.text, h.callback, h.group_chat = text, callback, {"id": GROUP_ID, "type": "supergroup", "title": "Логістика"}
    h.run = lambda *updates: loop.run_until_complete(run(*updates))
    h.await_ = loop.run_until_complete
    yield h
    loop.run_until_complete(app.shutdown())
    loop.close()


def _request_data(date_period, **extra):
    data = {
        "department": "Виробництво", "thread_id": 4, "vehicle_type": "Самоскид", "initiator": "Петренко",
        "company": "Агрокряж", "cargo_type": "Зерно: Кукурудза", "size_type": "Насип", "big_bag_weight": "—",
        "volume": "25", "notes": "—", "date_period": date_period, "load_city": "Вінниця", "load_place": "—",
        "load_method": "—", "load_contact": "—", "unload_city": "Одеса", "unload_place": "—",
        "unload_method": "Самоскид", "unload_contact": "—", "created_date": "01.09.2026", "created_time": "10:00",
        "created_by_mention": "@user501",
    }
    data.update(extra)
    return data


def _keyboard_callbacks(call):
    markup = call.get("reply_markup")
    if not markup or not hasattr(markup, "inline_keyboard"):
        return []
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def _date(days):
    return services.kyiv_today() + timedelta(days=days)


# ---------- сценарії ----------

def test_expired_request_offers_copy_with_new_date(harness):
    h = harness
    h.db.add("OLD00001", USER_ID, _request_data("01.09.2026"))
    new_day = _date(2)

    h.run(
        h.text("/my_requests"),
        h.callback("REQACT:EDIT:OLD00001"),
    )
    listing = next(c for c in h.rec.of("send_message") if "OLD00001" in c["text"] and "Маршрут" in c["text"])
    assert "REQACT:COPYNEW:OLD00001" in _keyboard_callbacks(listing)
    assert "REQACT:EDIT:OLD00001" not in _keyboard_callbacks(listing)
    blocked = next(c for c in h.rec.of("send_message") if c["text"].startswith("⛔"))
    assert "вже минула" in blocked["text"]
    assert "REQACT:COPYNEW:OLD00001" in _keyboard_callbacks(blocked)
    assert h.db.rows["OLD00001"]["request_data"]["date_period"] == "01.09.2026"

    h.run(
        h.callback("REQACT:COPYNEW:OLD00001"),
        h.text("📅 Разове перевезення"),
        h.callback(f"CAL:D:{new_day.isoformat()}"),
        h.text("ТАК"),
    )

    new_ids = [rid for rid in h.db.rows if rid != "OLD00001"]
    assert len(new_ids) == 1
    new_row = h.db.rows[new_ids[0]]
    assert new_row["request_data"]["date_period"] == new_day.strftime("%d.%m.%Y")
    assert new_row["request_data"]["copy_source_id"] == "OLD00001"
    assert new_row["request_data"]["created_date"] == services.kyiv_today().strftime("%d.%m.%Y")
    assert "question_index" not in new_row["request_data"]
    group_posts = h.rec.texts(GROUP_ID)
    assert any(new_ids[0] in text for text in group_posts)
    assert h.db.rows["OLD00001"]["request_data"]["date_period"] == "01.09.2026"  # стара не змінилась


def test_active_request_edit_still_works(harness):
    h = harness
    h.db.add("LIVE0001", USER_ID, _request_data(_date(3).strftime("%d.%m.%Y")))

    h.run(
        h.text("/edit_request LIVE0001"),
        h.callback("EDIT_FIELD:6"),  # Кількість / Обсяг
        h.text("30"),
        h.text("ТАК"),
    )
    row = h.db.rows["LIVE0001"]
    assert row["request_data"]["volume"] == "30"
    assert "Вніс зміни" in row["request_data"]["last_edit_footer"]
    edited = h.rec.of("edit_message_text")
    assert edited and edited[-1]["message_id"] == 1000
    assert any("📊 Обсяг: 25 → 30" in text for text in h.rec.texts(GROUP_ID))


def test_admin_can_edit_expired_request(harness, monkeypatch):
    h = harness
    monkeypatch.setenv("ADMIN_USER_ID", str(USER_ID))
    h.db.add("OLD00002", USER_ID, _request_data("01.09.2026"))
    h.run(
        h.text("/edit_request OLD00002"),
        h.callback("EDIT_FIELD:6"),
        h.text("40"),
        h.text("ТАК"),
    )
    assert h.db.rows["OLD00002"]["request_data"]["volume"] == "40"


def test_child_edit_of_expired_request_blocked(harness):
    h = harness
    h.db.add("SPLIT001", USER_ID, _request_data(
        "01.09.2026", unload_city="Київ; Львів", unload_distribution={"Київ": 10, "Львів": 15}))
    h.run(h.callback("REQACT:EDIT:SPLIT001-02"))
    assert any(text.startswith("⛔") for text in h.rec.texts(USER_ID))


def test_child_edit_of_active_request_works(harness):
    """Раніше падало з TypeError через неправильні аргументи _build_child_items_from_data."""
    h = harness
    h.db.add("SPLIT002", USER_ID, _request_data(
        _date(2).strftime("%d.%m.%Y"), unload_city="Київ; Львів", unload_distribution={"Київ": 10, "Львів": 15}))
    h.run(
        h.callback("REQACT:EDIT:SPLIT002-02"),
        h.text("⚖️ Змінити обсяг точки"),
        h.text("20"),
        h.text("✅ Зберегти зміни"),
    )
    data = h.db.rows["SPLIT002"]["request_data"]
    assert data["unload_distribution"] == {"Київ": 10.0, "Львів": 20.0}
    assert data["volume"] == "30"


def test_reminder_done_and_reschedule(harness):
    h = harness
    today_text = services.kyiv_today().strftime("%d.%m.%Y")
    # Нагадуємо: кінцева дата рівно через 2 дні, заявка створена раніше
    h.db.add("DUE00001", USER_ID, _request_data(_date(2).strftime("%d.%m.%Y")))
    h.db.add("DUE00002", USER_ID, _request_data(
        f"{_date(-3).strftime('%d.%m.%Y')} - {_date(2).strftime('%d.%m.%Y')}"))
    # Не нагадуємо: інший термін або заявку створено сьогодні
    h.db.add("FUTURE01", USER_ID, _request_data(_date(5).strftime("%d.%m.%Y")))
    h.db.add("ENDTODAY", USER_ID, _request_data(today_text))
    h.db.add("OVERDUE1", USER_ID, _request_data(_date(-1).strftime("%d.%m.%Y")))
    h.db.add("FRESH001", USER_ID, _request_data(_date(2).strftime("%d.%m.%Y"), created_date=today_text))
    h.db.add("FRESH002", USER_ID, _request_data(_date(1).strftime("%d.%m.%Y"), created_date=today_text))

    import reminders

    assert h.await_(reminders.send_due_reminders(h.app.bot)) == 2
    reminder_texts = h.rec.texts(GROUP_ID)
    assert len(reminder_texts) == 2
    expected_when = f"Через 2 дн. ({_date(2).strftime('%d.%m')}) закінчується термін перевезення"
    assert any(expected_when in t and "DUE00001" in t for t in reminder_texts)
    assert any("DUE00002" in t for t in reminder_texts)
    assert h.db.rows["DUE00001"]["reminder_message_id"]

    new_end = _date(4)
    h.run(
        h.callback("RM:DONE:DUE00001", uid=LOGIST_ID, chat=h.group_chat),
        h.callback("RM:MOVE:DUE00002", uid=LOGIST_ID, chat=h.group_chat),
        h.callback(f"RMC|DUE00002:D:{new_end.isoformat()}", uid=LOGIST_ID, chat=h.group_chat),
    )

    assert h.db.rows["DUE00001"]["status"] == "ВИКОНАНО"
    assert "ВИКОНАНО" in h.db.rows["DUE00001"]["request_data"]["status_footer"]
    assert h.db.rows["DUE00001"]["reminder_message_id"] is None
    assert ("status", ("DUE00001", "ВИКОНАНО", "@user777", True)) in h.sheets

    moved = h.db.rows["DUE00002"]["request_data"]["date_period"]
    assert moved == f"{_date(-3).strftime('%d.%m.%Y')} - {new_end.strftime('%d.%m.%Y')}"
    assert h.db.rows["DUE00002"]["status"] == "АКТИВНА"
    assert len(h.rec.of("delete_message")) == 2  # обидва нагадування прибрані з чату
    owner_notes = h.rec.texts(USER_ID)
    assert any("DUE00001" in t for t in owner_notes) and any("перенесено" in t for t in owner_notes)
    group_edits = [c["text"] for c in h.rec.of("edit_message_text")]
    assert any("Статус: ✅ ВИКОНАНО" in t for t in group_edits)
    assert any("📅 Перенесено" in t for t in group_edits)

    # Після «Виконано» заявку користувач більше не редагує
    h.run(h.text("/edit_request DUE00001"))
    assert any("виконану" in t for t in h.rec.texts(USER_ID))


def test_reminder_days_before_is_configurable(harness, monkeypatch):
    h = harness
    monkeypatch.setenv("REMINDER_DAYS_BEFORE", "3")
    h.db.add("DUE3DAYS", USER_ID, _request_data(_date(3).strftime("%d.%m.%Y")))
    h.db.add("DUE2DAYS", USER_ID, _request_data(_date(2).strftime("%d.%m.%Y")))
    import reminders
    assert h.await_(reminders.send_due_reminders(h.app.bot)) == 1
    assert "DUE3DAYS" in h.rec.texts(GROUP_ID)[0]


def test_reminder_rejects_past_date_and_non_logist(harness, monkeypatch):
    h = harness
    h.db.add("DUE00003", USER_ID, _request_data(_date(2).strftime("%d.%m.%Y")))
    monkeypatch.setenv("LOGIST_USER_IDS", str(LOGIST_ID))
    h.run(
        h.callback("RM:DONE:DUE00003", uid=999, chat=h.group_chat),
        h.callback(f"RMC|DUE00003:D:{_date(-1).isoformat()}", uid=LOGIST_ID, chat=h.group_chat),
    )
    assert h.db.rows["DUE00003"]["status"] == "АКТИВНА"
    alerts = [c.get("text") for c in h.rec.of("answer_callback_query")]
    assert "Ця дія доступна лише логістам" in alerts
    assert "Новий термін не може бути в минулому" in alerts
