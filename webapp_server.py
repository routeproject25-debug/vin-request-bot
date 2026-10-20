"""HTTP-сервер міні-додатка (Telegram Web App), що працює в одному процесі з ботом.

Сторінка форми — webapp/index.html. API приймає запити лише з підписом Telegram
(заголовок X-Telegram-Init-Data), тож заявку від імені іншого користувача не створити.
"""
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import web
from telegram import MenuButtonWebApp, User, WebAppInfo

import db
import novaposhta
import publishing
import services

STATIC_DIR = Path(__file__).resolve().parent / "webapp"
BOT_KEY = web.AppKey("bot", object)
RUNNER_KEY = "webapp_runner"


def _json(data: Dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(
        data,
        status=status,
        dumps=lambda obj: json.dumps(obj, ensure_ascii=False, default=str),
    )


def _error(status: int, message: str, **extra: Any) -> web.Response:
    return _json({"ok": False, "error": message, **extra}, status=status)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    if request.path.startswith("/api/"):
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        parsed = services.validate_webapp_init_data(init_data, os.getenv("TELEGRAM_BOT_TOKEN", ""))
        if not parsed:
            return _error(401, "Не вдалося підтвердити користувача Telegram. Відкрийте форму з бота ще раз.")
        tg_user = parsed["user"]
        request["tg_user"] = User(
            id=int(tg_user["id"]),
            first_name=tg_user.get("first_name") or "",
            is_bot=False,
            last_name=tg_user.get("last_name"),
            username=tg_user.get("username"),
        )
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as e:
        logging.exception(f"Web app error on {request.path}: {e}")
        return _error(500, "Внутрішня помилка сервера. Спробуйте ще раз або скористайтеся чатом з ботом.")


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def health(_: web.Request) -> web.Response:
    return web.Response(text="ok")


async def get_config(request: web.Request) -> web.Response:
    user: User = request["tg_user"]
    contacts = await asyncio.to_thread(db.get_user_contacts, user.id)
    last_initiator = await asyncio.to_thread(db.get_last_initiator, user.id)
    return _json({
        "ok": True,
        "departments": services.DEPARTMENTS,
        "vehicle_types": services.VEHICLE_TYPES,
        "companies": services.COMPANIES,
        "cargo_types": services.CARGO_TYPES,
        "crop_cargo_types": sorted(services.CROP_CARGO_TYPES),
        "crop_types": services.CROP_TYPES,
        "size_types": services.SIZE_TYPES,
        "liquid_cargo": sorted(services.LIQUID_BULK_CARGO),
        "contacts": list(dict.fromkeys(c["value"] for c in contacts if c.get("value")))[:10],
        "last_initiator": last_initiator,
        "is_admin": services.is_admin_user(user),
        "today": services.kyiv_today().isoformat(),
    })


async def search_cities(request: web.Request) -> web.Response:
    query = (request.query.get("q") or "").strip()
    if len(query) < 2:
        return _json({"ok": True, "items": []})
    items = await novaposhta.search_cities(query)
    return _json({"ok": True, "items": [item["display"] for item in items]})


async def _load_own_request(request_id: str, user: User):
    """Повертає (row, error_response)."""
    row = await asyncio.to_thread(db.get_request, request_id)
    if not row:
        return None, _error(404, f"Заявку {request_id} не знайдено")
    if int(row.get("user_id", 0)) != int(user.id) and not services.is_admin_user(user):
        return None, _error(403, "Можна працювати тільки з власними заявками")
    return row, None


async def get_request(request: web.Request) -> web.Response:
    user: User = request["tg_user"]
    request_id = services.resolve_parent_request_id(request.match_info["request_id"])
    mode = request.query.get("mode", "edit")
    row, error = await _load_own_request(request_id, user)
    if error is not None:
        return error

    data = row.get("request_data") if isinstance(row.get("request_data"), dict) else {}
    status = services.normalize_status(row.get("status"))
    is_admin = services.is_admin_user(user)

    if mode == "edit":
        reason = services.edit_block_reason(status, data, is_admin=is_admin)
        if reason:
            return _error(409, reason, can_copy=status != services.STATUS_DELETED)
        form = services.request_data_to_form(data)
    else:
        if status == services.STATUS_DELETED:
            return _error(409, "Заявка видалена — створити копію неможливо")
        # Минулу дату в копію не переносимо: користувач обирає нову.
        form = services.request_data_to_form(data, include_dates=not services.is_delivery_expired(data))

    return _json({"ok": True, "request_id": request_id, "status": status, "form": form})


async def submit_request(request: web.Request) -> web.Response:
    user: User = request["tg_user"]
    bot = request.app[BOT_KEY]
    try:
        body = await request.json()
    except Exception:
        return _error(400, "Некоректні дані форми")

    mode = body.get("mode") or "create"
    is_admin = services.is_admin_user(user)
    try:
        data = services.normalize_form_payload(body.get("form") or {}, allow_past=is_admin)
    except services.FormValidationError as e:
        return _error(400, str(e))

    unload_cities = services.split_location_values(data.get("unload_city"))

    if mode == "edit":
        request_id = services.resolve_parent_request_id(str(body.get("request_id") or ""))
        row, error = await _load_own_request(request_id, user)
        if error is not None:
            return error
        existing = row.get("request_data") if isinstance(row.get("request_data"), dict) else {}
        merged = services.sanitize_request_data(existing)
        merged.update(data)
        merged["request_id"] = request_id
        if len(unload_cities) > 1:
            merged["unload_distribution_ids"] = services.assign_distribution_ids(unload_cities, existing, request_id)
        else:
            merged.pop("unload_distribution", None)
            merged.pop("unload_distribution_ids", None)
        result = await publishing.publish_request(bot, user, merged, is_edit=True, original_data=existing)
    else:
        request_id = services.generate_request_id()
        data["request_id"] = request_id
        source_id = services.resolve_parent_request_id(str(body.get("source_id") or ""))
        if mode == "copy" and source_id:
            data["copy_source_id"] = source_id
        if len(unload_cities) > 1:
            data["unload_distribution_ids"] = services.assign_distribution_ids(unload_cities, None, request_id)
        result = await publishing.publish_request(bot, user, data, is_edit=False, is_copy=True)

    if result.blocked_reason:
        return _error(409, result.blocked_reason, can_copy=True)
    if not result.ok:
        return _error(500, result.error or "Не вдалося надіслати заявку")

    action_text = "оновлено" if mode == "edit" else "надіслано"
    try:
        await bot.send_message(
            chat_id=user.id,
            text=(
                f"✅ Заявку {result.request_id} {action_text} через форму.\n"
                f"📊 Експорт у Google Sheets: {'успішно' if result.export_success else '❌ не вдалося'}\n"
                f"Переглянути та змінити: /my_requests"
            ),
        )
    except Exception as e:
        logging.info(f"Failed to send web app confirmation to {user.id}: {e}")

    return _json({
        "ok": True,
        "request_id": result.request_id,
        "export_success": result.export_success,
        "chat_updated": result.chat_updated,
    })


def create_app(bot: Any) -> web.Application:
    app = web.Application(middlewares=[auth_middleware], client_max_size=256 * 1024)
    app[BOT_KEY] = bot
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/config", get_config)
    app.router.add_get("/api/cities", search_cities)
    app.router.add_get("/api/requests/{request_id}", get_request)
    app.router.add_post("/api/requests", submit_request)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)
    return app


def webapp_url() -> Optional[str]:
    return (os.getenv("WEBAPP_URL") or "").strip() or None


async def start(application: Any) -> None:
    url = webapp_url()
    if not url:
        logging.info("WEBAPP_URL не задано — міні-додаток вимкнено")
        return

    runner = web.AppRunner(create_app(application.bot))
    await runner.setup()
    port = int(os.getenv("PORT") or os.getenv("WEBAPP_PORT") or "8080")
    await web.TCPSite(runner, host="0.0.0.0", port=port).start()
    application.bot_data[RUNNER_KEY] = runner
    logging.info(f"Web app server started on port {port}")

    # Кнопка «🧾 Заявка» біля поля вводу в приватному чаті з ботом.
    try:
        await application.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="🧾 Заявка", web_app=WebAppInfo(url=url))
        )
    except Exception as e:
        logging.warning(f"Failed to set web app menu button: {e}")


async def stop(application: Any) -> None:
    runner = application.bot_data.pop(RUNNER_KEY, None)
    if runner:
        await runner.cleanup()
