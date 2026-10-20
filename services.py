"""Чиста бізнес-логіка заявок без залежності від Telegram, БД чи Google Sheets.

Модуль використовується і чат-ботом, і міні-додатком, і покривається тестами.
"""
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl

import pytz

KYIV_TZ = pytz.timezone("Europe/Kyiv")

# --- Статуси заявки ---------------------------------------------------------

STATUS_ACTIVE = "АКТИВНА"
STATUS_DONE = "ВИКОНАНО"
STATUS_CANCELLED = "СКАСОВАНО"
STATUS_DELETED = "ВИДАЛЕНО"

STATUS_EMOJI = {
    STATUS_ACTIVE: "🟢",
    STATUS_DONE: "✅",
    STATUS_CANCELLED: "⚪",
    STATUS_DELETED: "🔴",
}

# --- Довідники форми --------------------------------------------------------

THREAD_IDS = {
    "Тваринництво": 2,
    "Виробництво": 4,
}
DEPARTMENTS = list(THREAD_IDS.keys())
VEHICLE_TYPES = ["ТРАЛ", "Зерновоз", "Самоскид", "Цистерна", "Тент", "Інше"]
COMPANIES = ["Зернопродукт", "Агрокряж", "Інше"]
CARGO_TYPES = ["Зерно", "Насіння", "АМ вода", "КАС", "РКД", "Інше"]
CROP_CARGO_TYPES = {"Зерно", "Насіння"}
CROP_TYPES = ["Кукурудза", "Пшениця", "Соя", "Ріпак", "Соняшник"]
SIZE_TYPES = ["Габарит", "Негабарит", "Насип", "Рідкі", "Біг-бег"]
LIQUID_BULK_CARGO = {"КАС", "РКД", "АМ вода"}

# Поля форми (збігаються з ключами QUESTIONS у bot.py).
FORM_FIELD_KEYS = (
    "vehicle_type",
    "initiator",
    "company",
    "cargo_type",
    "size_type",
    "big_bag_weight",
    "volume",
    "notes",
    "date_period",
    "load_city",
    "load_place",
    "load_method",
    "load_contact",
    "unload_city",
    "unload_place",
    "unload_method",
    "unload_contact",
)

# Усе, що реально належить заявці. Решта ключів context.user_data — службовий стан діалогу.
REQUEST_DATA_KEYS = set(FORM_FIELD_KEYS) | {
    "department",
    "thread_id",
    "quick_mode",
    "date_type",
    "request_id",
    "created_date",
    "created_time",
    "created_by_mention",
    "unload_distribution",
    "unload_distribution_ids",
    "last_edit_footer",
    "status_footer",
    "copy_source_id",
    "source",
}

EMPTY = "—"


# --- Дрібні утиліти ---------------------------------------------------------

def kyiv_now() -> datetime:
    return datetime.now(KYIV_TZ)


def kyiv_today() -> date:
    return kyiv_now().date()


def generate_request_id() -> str:
    return uuid.uuid4().hex[:8].upper()


def normalize_status(value: Any) -> str:
    text = str(value or "").strip().upper()
    return text or STATUS_ACTIVE


def to_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except Exception:
        return None


def format_number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def split_location_values(value: Any) -> List[str]:
    if not value:
        return []
    text = str(value).strip()
    if not text or text == EMPTY:
        return []
    return [part.strip() for part in text.split(";") if part.strip()]


def join_location_values(values: Iterable[str]) -> str:
    cleaned = [v.strip() for v in values if v and v.strip()]
    return "; ".join(cleaned) if cleaned else EMPTY


def is_empty(value: Any) -> bool:
    return str(value or "").strip() in {"", EMPTY}


# --- Дати перевезення -------------------------------------------------------

def parse_ua_date(text: Any) -> Optional[date]:
    raw = str(text or "").strip().lstrip("'").strip()
    if not raw:
        return None
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def format_ua_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def extract_delivery_range(request_data: Optional[Dict[str, Any]]) -> Optional[Tuple[date, date]]:
    """Повернути (початок, кінець) перевезення з поля date_period або None."""
    period_raw = str((request_data or {}).get("date_period") or "").strip()
    if not period_raw:
        return None

    if " - " in period_raw:
        left, right = period_raw.split(" - ", 1)
        start_dt = parse_ua_date(left)
        end_dt = parse_ua_date(right)
        if not start_dt or not end_dt:
            return None
        if end_dt < start_dt:
            start_dt, end_dt = end_dt, start_dt
        return start_dt, end_dt

    single_dt = parse_ua_date(period_raw)
    if not single_dt:
        return None
    return single_dt, single_dt


def build_date_period(start: date, end: Optional[date] = None) -> str:
    if end is None or end == start:
        return format_ua_date(start)
    if end < start:
        start, end = end, start
    return f"{format_ua_date(start)} - {format_ua_date(end)}"


def is_delivery_expired(request_data: Optional[Dict[str, Any]], today: Optional[date] = None) -> bool:
    """Заявка «минула», якщо кінцева дата перевезення менша за сьогодні."""
    period = extract_delivery_range(request_data)
    if not period:
        return False
    today = today or kyiv_today()
    return period[1] < today


def reschedule_period(date_period: Any, new_end: date) -> str:
    """Перенести кінцевий термін. Разова дата стає новою разовою датою, період зберігає початок."""
    period = extract_delivery_range({"date_period": date_period})
    if not period or period[0] == period[1] or new_end <= period[0]:
        return format_ua_date(new_end)
    return build_date_period(period[0], new_end)


def edit_block_reason(
    status: Any,
    request_data: Optional[Dict[str, Any]],
    *,
    is_admin: bool,
    today: Optional[date] = None,
) -> Optional[str]:
    """Пояснення, чому заявку не можна редагувати, або None, якщо можна."""
    status = normalize_status(status)
    if status == STATUS_DELETED:
        return "Цю заявку позначено як ВИДАЛЕНО. Відновіть її командою /restore_request <ID>."
    if is_admin:
        return None
    if status == STATUS_DONE:
        return "Заявку вже позначено як виконану — змінювати її не можна."
    if status == STATUS_CANCELLED:
        return "Заявку скасовано — змінювати її не можна."
    if is_delivery_expired(request_data, today=today):
        period = str((request_data or {}).get("date_period") or "").strip()
        return f"Дата перевезення ({period}) вже минула — змінювати таку заявку не можна."
    return None


# --- Дані заявки ------------------------------------------------------------

def sanitize_request_data(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Залишити тільки поля заявки (без службового стану діалогу)."""
    return {key: value for key, value in (data or {}).items() if key in REQUEST_DATA_KEYS}


def prepare_copy_data(
    source: Optional[Dict[str, Any]],
    new_request_id: str,
    source_request_id: str = "",
    clear_dates: bool = False,
) -> Dict[str, Any]:
    """Підготувати дані нової заявки на основі існуючої."""
    data = sanitize_request_data(source)
    for key in (
        "created_date",
        "created_time",
        "created_by_mention",
        "unload_distribution_ids",  # дочірні ID належать старій заявці
        "last_edit_footer",
        "status_footer",
    ):
        data.pop(key, None)
    if clear_dates:
        data.pop("date_period", None)
        data.pop("date_type", None)
    data["request_id"] = new_request_id
    if source_request_id:
        data["copy_source_id"] = source_request_id.strip().upper()
    if data.get("department") in THREAD_IDS and data.get("thread_id") is None:
        data["thread_id"] = THREAD_IDS[data["department"]]
    return data


def resolve_parent_request_id(request_id: str) -> str:
    rid = (request_id or "").strip().upper()
    if "-" not in rid:
        return rid
    head, tail = rid.rsplit("-", 1)
    return head if tail.isdigit() else rid


def parse_child_request_id(request_id: str) -> Optional[Tuple[str, int]]:
    rid = (request_id or "").strip().upper()
    if "-" not in rid:
        return None
    head, tail = rid.rsplit("-", 1)
    if not tail.isdigit():
        return None
    return head, int(tail)


def build_child_items(data: Dict[str, Any], parent_id: Optional[str] = None) -> List[Dict[str, Any]]:
    unload_cities = split_location_values(data.get("unload_city"))
    distribution = data.get("unload_distribution") if isinstance(data.get("unload_distribution"), dict) else {}
    ids_map = data.get("unload_distribution_ids") if isinstance(data.get("unload_distribution_ids"), dict) else {}
    parent_id = (parent_id or data.get("request_id") or "").strip().upper()
    items: List[Dict[str, Any]] = []
    for idx, city in enumerate(unload_cities, start=1):
        amount = to_float(distribution.get(city))
        if amount is None:
            amount = 0.0
        child_id = str(ids_map.get(city) or "").strip().upper()
        if not child_id:
            child_id = f"{parent_id}-{idx:02d}" if parent_id else f"-{idx:02d}"
        items.append({"index": idx, "city": city, "amount": amount, "child_id": child_id})
    return items


def rebuild_distribution(items: List[Dict[str, Any]]) -> Tuple[str, Dict[str, float], Dict[str, str], float]:
    unload_cities: List[str] = []
    distribution: Dict[str, float] = {}
    ids_map: Dict[str, str] = {}
    total = 0.0
    for item in items:
        city = str(item.get("city", "")).strip()
        if not city:
            continue
        amount = float(item.get("amount", 0.0) or 0.0)
        child_id = str(item.get("child_id", "")).strip().upper()
        unload_cities.append(city)
        distribution[city] = amount
        if child_id:
            ids_map[city] = child_id
        total += amount
    return join_location_values(unload_cities), distribution, ids_map, total


def sheet_row_ids(data: Dict[str, Any]) -> List[str]:
    """ID рядків, які export_to_sheets створює для заявки (дзеркало логіки sheets.py)."""
    request_id = str(data.get("request_id") or "").strip().upper()
    unload_cities = split_location_values(data.get("unload_city"))
    distribution = data.get("unload_distribution") if isinstance(data.get("unload_distribution"), dict) else {}
    ids_map = data.get("unload_distribution_ids") if isinstance(data.get("unload_distribution_ids"), dict) else {}
    if len(unload_cities) > 1 and distribution:
        row_ids = []
        for idx, city in enumerate(unload_cities, start=1):
            if distribution.get(city) is None:
                continue
            stable = str(ids_map.get(city) or "").strip().upper()
            row_ids.append(stable or f"{request_id}-{idx:02d}")
        if row_ids:
            return row_ids
    return [request_id] if request_id else []


def assign_distribution_ids(cities: List[str], previous_data: Optional[Dict[str, Any]], parent_id: str) -> Dict[str, str]:
    """Зберегти дочірні ID для тих самих НП, а новим НП видати вільні номери."""
    parent_id = parent_id.strip().upper()
    previous_ids: Dict[str, str] = {}
    if previous_data and len(split_location_values(previous_data.get("unload_city"))) > 1:
        for item in build_child_items(previous_data, parent_id):
            previous_ids[item["city"]] = item["child_id"]

    used_numbers = set()
    for child_id in previous_ids.values():
        parsed = parse_child_request_id(child_id)
        if parsed:
            used_numbers.add(parsed[1])

    result: Dict[str, str] = {}
    next_number = 1
    for city in cities:
        if city in previous_ids:
            result[city] = previous_ids[city]
            continue
        while next_number in used_numbers:
            next_number += 1
        result[city] = f"{parent_id}-{next_number:02d}"
        used_numbers.add(next_number)
    return result


# --- Форма міні-додатка -----------------------------------------------------

class FormValidationError(ValueError):
    pass


def _clean_text(value: Any, max_len: int = 300) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_len]


def _choice_with_other(choice: str, custom: str, allowed: List[str], label: str) -> str:
    if choice == "Інше":
        if not custom:
            raise FormValidationError(f"Вкажіть значення для поля «{label}»")
        return f"Інше: {custom}"
    if choice not in allowed:
        raise FormValidationError(f"Оберіть значення для поля «{label}»")
    return choice


def _split_other(value: str, allowed: List[str]) -> Tuple[str, str]:
    if value in allowed:
        return value, ""
    if value.startswith("Інше:"):
        return "Інше", value.split(":", 1)[1].strip()
    if is_empty(value):
        return "", ""
    return "Інше", value


def _parse_iso_date(value: Any, label: str) -> date:
    try:
        return datetime.strptime(str(value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        raise FormValidationError(f"Вкажіть {label}")


def _positive_number(value: Any, label: str) -> float:
    number = to_float(value)
    if number is None or number <= 0:
        raise FormValidationError(f"Поле «{label}» має бути числом більше 0")
    return number


def normalize_form_payload(form: Dict[str, Any], *, today: Optional[date] = None, allow_past: bool = False) -> Dict[str, Any]:
    """Перетворити дані форми міні-додатка у формат заявки, як у чат-боті."""
    today = today or kyiv_today()
    data: Dict[str, Any] = {}

    department = _clean_text(form.get("department"))
    if department not in THREAD_IDS:
        raise FormValidationError("Оберіть, від кого запит (Тваринництво / Виробництво)")
    data["department"] = department
    data["thread_id"] = THREAD_IDS[department]

    data["vehicle_type"] = _choice_with_other(
        _clean_text(form.get("vehicle_type")), _clean_text(form.get("vehicle_type_custom")), VEHICLE_TYPES, "Тип авто"
    )

    initiator = _clean_text(form.get("initiator"), 120)
    if not initiator:
        raise FormValidationError("Вкажіть ініціатора заявки (ПІБ)")
    data["initiator"] = initiator

    data["company"] = _choice_with_other(
        _clean_text(form.get("company")), _clean_text(form.get("company_custom")), COMPANIES, "Підприємство"
    )

    cargo = _clean_text(form.get("cargo_type"))
    if cargo in CROP_CARGO_TYPES:
        crop = _clean_text(form.get("crop")) or _clean_text(form.get("cargo_custom"))
        if not crop:
            raise FormValidationError("Оберіть культуру")
        data["cargo_type"] = f"{cargo}: {crop}"
    else:
        data["cargo_type"] = _choice_with_other(cargo, _clean_text(form.get("cargo_custom")), CARGO_TYPES, "Вид вантажу")

    size_type = _clean_text(form.get("size_type"))
    if size_type not in SIZE_TYPES:
        raise FormValidationError("Оберіть габарит / негабарит")
    data["size_type"] = size_type

    if size_type == "Біг-бег":
        data["big_bag_weight"] = format_number(_positive_number(form.get("big_bag_weight"), "Вага 1 біг-бегу"))
    else:
        data["big_bag_weight"] = EMPTY

    volume = _positive_number(form.get("volume"), "Обсяг")
    data["volume"] = format_number(volume)
    data["notes"] = _clean_text(form.get("notes"), 1000) or EMPTY

    date_type = "period" if form.get("date_type") == "period" else "single"
    start = _parse_iso_date(form.get("date_from"), "дату перевезення")
    end = _parse_iso_date(form.get("date_to"), "кінцеву дату періоду") if date_type == "period" else start
    if end < start:
        raise FormValidationError("Кінцева дата не може бути раніше початкової")
    if not allow_past and end < today:
        raise FormValidationError("Дата перевезення не може бути в минулому")
    data["date_type"] = date_type
    data["date_period"] = build_date_period(start, end)

    load_cities = [_clean_text(c, 200) for c in (form.get("load_cities") or []) if _clean_text(c, 200)]
    data["load_city"] = join_location_values(dict.fromkeys(load_cities))
    data["load_place"] = _clean_text(form.get("load_place"), 200) or EMPTY
    data["load_method"] = _clean_text(form.get("load_method"), 200) or EMPTY
    data["load_contact"] = _clean_text(form.get("load_contact"), 200) or EMPTY

    unload_cities = list(dict.fromkeys(
        _clean_text(c, 200) for c in (form.get("unload_cities") or []) if _clean_text(c, 200)
    ))
    if not unload_cities:
        raise FormValidationError("Додайте хоча б один населений пункт розвантаження")
    data["unload_city"] = join_location_values(unload_cities)
    data["unload_place"] = _clean_text(form.get("unload_place"), 200) or EMPTY
    data["unload_method"] = _clean_text(form.get("unload_method"), 200) or EMPTY
    data["unload_contact"] = _clean_text(form.get("unload_contact"), 200) or EMPTY

    if len(unload_cities) > 1:
        raw_distribution = form.get("distribution") or {}
        distribution: Dict[str, float] = {}
        for city in unload_cities:
            distribution[city] = _positive_number(raw_distribution.get(city), f"Обсяг для {city}")
        if abs(sum(distribution.values()) - volume) > 1e-6:
            raise FormValidationError(
                f"Сума розподілу ({format_number(sum(distribution.values()))}) "
                f"не дорівнює загальному обсягу ({format_number(volume)})"
            )
        data["unload_distribution"] = distribution
        data["unload_place"] = EMPTY

    # Ті самі правила пропуску полів, що й у чат-боті.
    cargo_base = data["cargo_type"].split(":", 1)[0].strip()
    if cargo_base in LIQUID_BULK_CARGO or data["cargo_type"] in LIQUID_BULK_CARGO:
        data["load_method"] = EMPTY
        data["unload_method"] = EMPTY
    if size_type == "Насип":
        data["unload_method"] = "Самоскид"

    data["quick_mode"] = False
    data["source"] = "webapp"
    return data


def request_data_to_form(data: Dict[str, Any], *, include_dates: bool = True) -> Dict[str, Any]:
    """Зворотне перетворення: заявка → значення полів форми (для редагування/копії)."""
    def text(key: str) -> str:
        value = str(data.get(key) or "").strip()
        return "" if value == EMPTY else value

    vehicle, vehicle_custom = _split_other(text("vehicle_type"), VEHICLE_TYPES)
    company, company_custom = _split_other(text("company"), COMPANIES)

    cargo_raw = text("cargo_type")
    crop = ""
    cargo_custom = ""
    head = cargo_raw.split(":", 1)[0].strip()
    if head in CROP_CARGO_TYPES and ":" in cargo_raw:
        cargo = head
        crop = cargo_raw.split(":", 1)[1].strip()
    else:
        cargo, cargo_custom = _split_other(cargo_raw, CARGO_TYPES)

    form: Dict[str, Any] = {
        "department": text("department"),
        "vehicle_type": vehicle,
        "vehicle_type_custom": vehicle_custom,
        "initiator": text("initiator"),
        "company": company,
        "company_custom": company_custom,
        "cargo_type": cargo,
        "crop": crop,
        "cargo_custom": cargo_custom,
        "size_type": text("size_type"),
        "big_bag_weight": text("big_bag_weight"),
        "volume": text("volume"),
        "notes": text("notes"),
        "date_type": "single",
        "date_from": "",
        "date_to": "",
        "load_cities": split_location_values(data.get("load_city")),
        "load_place": text("load_place"),
        "load_method": text("load_method"),
        "load_contact": text("load_contact"),
        "unload_cities": split_location_values(data.get("unload_city")),
        "unload_place": text("unload_place"),
        "unload_method": text("unload_method"),
        "unload_contact": text("unload_contact"),
        "distribution": {},
    }
    distribution = data.get("unload_distribution")
    if isinstance(distribution, dict):
        form["distribution"] = {
            city: format_number(value) for city, value in
            ((c, to_float(v)) for c, v in distribution.items()) if value is not None
        }

    if include_dates:
        period = extract_delivery_range(data)
        if period:
            form["date_from"] = period[0].isoformat()
            if period[0] != period[1]:
                form["date_type"] = "period"
                form["date_to"] = period[1].isoformat()
    return form


# --- Доступи ----------------------------------------------------------------

def _parse_id_set(raw: str) -> set:
    result = set()
    for token in re.split(r"[,;\s]+", raw or ""):
        token = token.strip()
        if not token:
            continue
        try:
            result.add(int(token))
        except ValueError:
            continue
    return result


def _parse_username_set(raw: str) -> set:
    return {
        item.strip().lstrip("@").lower()
        for item in re.split(r"[,;\s]+", raw or "")
        if item.strip()
    }


def _user_matches(user: Any, ids_env: str, usernames_env: str) -> bool:
    if not user:
        return False
    user_id = int(getattr(user, "id", 0) or 0)
    if user_id and user_id in _parse_id_set(ids_env):
        return True
    username = (getattr(user, "username", "") or "").strip().lstrip("@").lower()
    return bool(username) and username in _parse_username_set(usernames_env)


def is_admin_user(user: Any) -> bool:
    """Адміни задаються через ADMIN_USER_IDS / ADMIN_USER_ID / ADMIN_USERNAMES."""
    ids_env = " ".join(filter(None, [os.getenv("ADMIN_USER_IDS"), os.getenv("ADMIN_USER_ID")]))
    return _user_matches(user, ids_env, os.getenv("ADMIN_USERNAMES") or "")


def is_logist_user(user: Any) -> bool:
    """Хто може закривати/переносити заявки з нагадувань.

    Якщо LOGIST_USER_IDS і LOGIST_USERNAMES не задано — будь-який учасник робочої групи.
    """
    if not user:
        return False
    if is_admin_user(user):
        return True
    ids_env = os.getenv("LOGIST_USER_IDS") or ""
    usernames_env = os.getenv("LOGIST_USERNAMES") or ""
    if not ids_env.strip() and not usernames_env.strip():
        return True
    return _user_matches(user, ids_env, usernames_env)


# --- Перевірка підпису Telegram WebApp --------------------------------------

def validate_webapp_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int = 24 * 3600,
    now: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Перевірити initData міні-додатка. Повертає розпарсені поля (user як dict) або None."""
    if not init_data or not bot_token:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        return None

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    current = time.time() if now is None else now
    if max_age_seconds and current - auth_date > max_age_seconds:
        return None

    result: Dict[str, Any] = dict(pairs)
    try:
        result["user"] = json.loads(pairs.get("user") or "{}")
    except ValueError:
        return None
    if not result["user"].get("id"):
        return None
    return result
