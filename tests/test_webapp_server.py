"""API міні-додатка: підпис Telegram, права, правило «минула дата», виклик спільного конвеєра."""
import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

import publishing
import services
import webapp_server

TOKEN = "123:TEST"


def _init_data(user_id=42, username="ivan"):
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "Іван", "username": username})}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class FakeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeDB:
    def __init__(self):
        self.rows = {}

    def get_request(self, request_id):
        return self.rows.get(request_id.upper())

    def get_user_contacts(self, user_id):
        return [{"type": "load", "value": "Іван 0501234567"}]

    def get_last_initiator(self, user_id):
        return "Петренко Іван"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    for name in ("ADMIN_USER_IDS", "ADMIN_USER_ID", "ADMIN_USERNAMES"):
        monkeypatch.delenv(name, raising=False)
    fake_db = FakeDB()
    monkeypatch.setattr(webapp_server, "db", fake_db)
    published = []

    async def fake_publish(bot, user, data, *, is_edit, original_data=None, is_copy=False):
        published.append({"user": user, "data": dict(data), "is_edit": is_edit, "original": original_data})
        if is_edit:
            row = fake_db.rows[data["request_id"]]
            reason = services.edit_block_reason(row["status"], row["request_data"], is_admin=services.is_admin_user(user))
            if reason:
                return publishing.PublishResult(False, data["request_id"], blocked_reason=reason)
        return publishing.PublishResult(True, data["request_id"], export_success=True)

    monkeypatch.setattr(webapp_server.publishing, "publish_request", fake_publish)
    return fake_db, published


def _run(coro_factory):
    async def runner():
        bot = FakeBot()
        client = TestClient(TestServer(webapp_server.create_app(bot)))
        await client.start_server()
        try:
            return await coro_factory(client, bot)
        finally:
            await client.close()
    return asyncio.run(runner())


def _future(days=3):
    return services.kyiv_today().fromordinal(services.kyiv_today().toordinal() + days)


def _valid_form(**overrides):
    form = {
        "department": "Виробництво", "vehicle_type": "Самоскид", "initiator": "Петренко Іван",
        "company": "Агрокряж", "cargo_type": "КАС", "size_type": "Рідкі", "volume": "20",
        "date_type": "single", "date_from": _future().isoformat(), "unload_cities": ["Київ"],
    }
    form.update(overrides)
    return form


def test_api_requires_telegram_signature(env):
    async def scenario(client, bot):
        assert (await client.get("/api/config")).status == 401
        bad = await client.get("/api/config", headers={"X-Telegram-Init-Data": "user=1&hash=deadbeef"})
        assert bad.status == 401
        ok = await client.get("/api/config", headers={"X-Telegram-Init-Data": _init_data()})
        body = await ok.json()
        assert ok.status == 200 and body["last_initiator"] == "Петренко Іван"
        assert body["contacts"] == ["Іван 0501234567"]
        assert (await client.get("/health")).status == 200
    _run(scenario)


def test_create_request_via_form(env):
    fake_db, published = env

    async def scenario(client, bot):
        response = await client.post(
            "/api/requests",
            json={"mode": "create", "form": _valid_form()},
            headers={"X-Telegram-Init-Data": _init_data()},
        )
        body = await response.json()
        assert response.status == 200, body
        assert published[0]["is_edit"] is False
        assert published[0]["user"].id == 42
        assert published[0]["data"]["request_id"] == body["request_id"]
        assert published[0]["data"]["load_method"] == "—"  # рідкий вантаж
        assert bot.messages and str(body["request_id"]) in bot.messages[0]["text"]
    _run(scenario)


def test_create_request_validation_error(env):
    async def scenario(client, bot):
        response = await client.post(
            "/api/requests",
            json={"mode": "create", "form": _valid_form(volume="")},
            headers={"X-Telegram-Init-Data": _init_data()},
        )
        assert response.status == 400
        assert "Обсяг" in (await response.json())["error"]
    _run(scenario)


def test_edit_expired_request_is_blocked(env):
    fake_db, published = env
    fake_db.rows["OLD00001"] = {
        "request_id": "OLD00001", "user_id": 42, "status": "АКТИВНА",
        "request_data": {"request_id": "OLD00001", "date_period": "01.01.2026", "volume": "10",
                         "department": "Виробництво", "unload_city": "Київ"},
    }

    async def scenario(client, bot):
        headers = {"X-Telegram-Init-Data": _init_data()}
        response = await client.get("/api/requests/OLD00001?mode=edit", headers=headers)
        body = await response.json()
        assert response.status == 409 and body["can_copy"] is True
        assert "минула" in body["error"]

        # Копія доступна, але без старої дати
        copy = await (await client.get("/api/requests/OLD00001?mode=copy", headers=headers)).json()
        assert copy["ok"] and copy["form"]["date_from"] == ""

        # Навіть прямий POST на редагування не пройде
        response = await client.post(
            "/api/requests", json={"mode": "edit", "request_id": "OLD00001", "form": _valid_form()}, headers=headers
        )
        assert response.status == 409
    _run(scenario)


def test_edit_foreign_request_forbidden(env):
    fake_db, _ = env
    fake_db.rows["FOREIGN1"] = {"request_id": "FOREIGN1", "user_id": 99, "status": "АКТИВНА", "request_data": {}}

    async def scenario(client, bot):
        response = await client.get("/api/requests/FOREIGN1?mode=edit", headers={"X-Telegram-Init-Data": _init_data()})
        assert response.status == 403
    _run(scenario)


def test_edit_keeps_metadata_and_child_ids(env):
    fake_db, published = env
    fake_db.rows["LIVE0001"] = {
        "request_id": "LIVE0001", "user_id": 42, "status": "АКТИВНА",
        "request_data": {
            "request_id": "LIVE0001", "created_date": "18.10.2026", "created_by_mention": "@ivan",
            "date_period": _future().strftime("%d.%m.%Y"), "unload_city": "Київ; Львів",
            "unload_distribution": {"Київ": 10, "Львів": 10}, "volume": "20",
        },
    }

    async def scenario(client, bot):
        form = _valid_form(unload_cities=["Львів", "Одеса"], distribution={"Львів": "5", "Одеса": "15"})
        response = await client.post(
            "/api/requests", json={"mode": "edit", "request_id": "LIVE0001", "form": form},
            headers={"X-Telegram-Init-Data": _init_data()},
        )
        assert response.status == 200, await response.json()
        sent = published[0]
        assert sent["is_edit"] is True
        assert sent["data"]["created_date"] == "18.10.2026"
        assert sent["data"]["unload_distribution_ids"] == {"Львів": "LIVE0001-02", "Одеса": "LIVE0001-03"}
        assert sent["original"]["unload_city"] == "Київ; Львів"
    _run(scenario)
