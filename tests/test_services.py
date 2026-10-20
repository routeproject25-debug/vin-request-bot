import hashlib
import hmac
import json
import time
from datetime import date
from urllib.parse import urlencode

import pytest

import services as s

TODAY = date(2026, 10, 20)


# --- дати ---------------------------------------------------------------------

@pytest.mark.parametrize("period, expected", [
    ("20.10.2026", (date(2026, 10, 20), date(2026, 10, 20))),
    ("18.10.2026 - 25.10.2026", (date(2026, 10, 18), date(2026, 10, 25))),
    ("25.10.2026 - 18.10.2026", (date(2026, 10, 18), date(2026, 10, 25))),
    ("'20.10.2026", (date(2026, 10, 20), date(2026, 10, 20))),
    ("—", None),
    ("", None),
    ("завтра", None),
])
def test_extract_delivery_range(period, expected):
    assert s.extract_delivery_range({"date_period": period}) == expected


@pytest.mark.parametrize("period, expired", [
    ("19.10.2026", True),               # вчора
    ("20.10.2026", False),              # сьогодні — ще можна
    ("01.10.2026 - 19.10.2026", True),  # період закінчився вчора
    ("01.10.2026 - 20.10.2026", False), # період ще триває
    ("—", False),                       # без дати — не блокуємо
])
def test_is_delivery_expired(period, expired):
    assert s.is_delivery_expired({"date_period": period}, today=TODAY) is expired


def test_edit_block_reason_rules():
    past = {"date_period": "01.09.2026"}
    future = {"date_period": "25.10.2026"}

    assert s.edit_block_reason("АКТИВНА", future, is_admin=False, today=TODAY) is None
    assert "минула" in s.edit_block_reason("АКТИВНА", past, is_admin=False, today=TODAY)
    assert s.edit_block_reason("АКТИВНА", past, is_admin=True, today=TODAY) is None
    assert "виконану" in s.edit_block_reason("ВИКОНАНО", future, is_admin=False, today=TODAY)
    assert "скасовано" in s.edit_block_reason("СКАСОВАНО", future, is_admin=False, today=TODAY)
    # Видалену не редагує навіть адмін (спершу відновлення)
    assert "ВИДАЛЕНО" in s.edit_block_reason("ВИДАЛЕНО", future, is_admin=True, today=TODAY)
    # Порожній статус старих записів = АКТИВНА
    assert s.edit_block_reason(None, future, is_admin=False, today=TODAY) is None


@pytest.mark.parametrize("old, new_end, expected", [
    ("20.10.2026", date(2026, 10, 23), "23.10.2026"),
    ("18.10.2026 - 20.10.2026", date(2026, 10, 25), "18.10.2026 - 25.10.2026"),
    ("18.10.2026 - 20.10.2026", date(2026, 10, 18), "18.10.2026"),
    ("18.10.2026 - 20.10.2026", date(2026, 10, 10), "10.10.2026"),
    ("—", date(2026, 10, 22), "22.10.2026"),
])
def test_reschedule_period(old, new_end, expected):
    assert s.reschedule_period(old, new_end) == expected


# --- дані заявки --------------------------------------------------------------

def test_sanitize_drops_dialog_state():
    data = {
        "volume": "25",
        "request_id": "A1B2C3D4",
        "question_index": 17,
        "my_requests_cache": [{"x": 1}],
        "original_request_data": {"original_request_data": {}},
        "_field_values_for_edit": {},
    }
    assert s.sanitize_request_data(data) == {"volume": "25", "request_id": "A1B2C3D4"}


def test_prepare_copy_data_resets_identity():
    source = {
        "request_id": "OLD00001",
        "department": "Тваринництво",
        "date_period": "01.09.2026",
        "date_type": "single",
        "created_date": "01.09.2026",
        "created_by_mention": "@someone",
        "unload_city": "Київ; Львів",
        "unload_distribution": {"Київ": 10, "Львів": 15},
        "unload_distribution_ids": {"Київ": "OLD00001-01", "Львів": "OLD00001-02"},
        "status_footer": "Статус: ✅",
        "question_index": 5,
    }
    copy = s.prepare_copy_data(source, "NEW00001", "old00001", clear_dates=True)
    assert copy["request_id"] == "NEW00001"
    assert copy["copy_source_id"] == "OLD00001"
    assert copy["thread_id"] == s.THREAD_IDS["Тваринництво"]
    for key in ("unload_distribution_ids", "created_date", "created_by_mention", "status_footer",
                "date_period", "date_type", "question_index"):
        assert key not in copy
    # Нова заявка пише в таблицю рядки зі СВОЇМИ дочірніми ID
    assert s.sheet_row_ids(copy) == ["NEW00001-01", "NEW00001-02"]


def test_child_items_roundtrip_and_ids():
    data = {
        "request_id": "ABCDEF12",
        "unload_city": "Київ; Львів",
        "unload_distribution": {"Київ": "10", "Львів": 15},
    }
    items = s.build_child_items(data)
    assert [i["child_id"] for i in items] == ["ABCDEF12-01", "ABCDEF12-02"]
    unload, distribution, ids_map, total = s.rebuild_distribution(items)
    assert unload == "Київ; Львів"
    assert distribution == {"Київ": 10.0, "Львів": 15.0}
    assert total == 25.0
    assert ids_map == {"Київ": "ABCDEF12-01", "Львів": "ABCDEF12-02"}
    # parent_id з аргументу має пріоритет
    assert s.build_child_items({"unload_city": "Київ"}, "XYZ")[0]["child_id"] == "XYZ-01"


def test_sheet_row_ids_single_and_multi():
    assert s.sheet_row_ids({"request_id": "AAAA1111", "unload_city": "Київ"}) == ["AAAA1111"]
    multi = {
        "request_id": "AAAA1111",
        "unload_city": "Київ; Львів",
        "unload_distribution": {"Київ": 1, "Львів": 2},
        "unload_distribution_ids": {"Львів": "AAAA1111-05"},
    }
    assert s.sheet_row_ids(multi) == ["AAAA1111-01", "AAAA1111-05"]


def test_assign_distribution_ids_keeps_existing():
    previous = {
        "request_id": "P1",
        "unload_city": "Київ; Львів; Одеса",
        "unload_distribution": {"Київ": 1, "Львів": 1, "Одеса": 1},
    }
    ids = s.assign_distribution_ids(["Львів", "Дніпро"], previous, "P1")
    assert ids == {"Львів": "P1-02", "Дніпро": "P1-04"}
    assert s.assign_distribution_ids(["A", "B"], None, "N1") == {"A": "N1-01", "B": "N1-02"}


@pytest.mark.parametrize("rid, parent, child", [
    ("abcdef12", "ABCDEF12", None),
    ("ABCDEF12-03", "ABCDEF12", ("ABCDEF12", 3)),
    ("ABC-DEF", "ABC-DEF", None),
])
def test_request_id_parsing(rid, parent, child):
    assert s.resolve_parent_request_id(rid) == parent
    assert s.parse_child_request_id(rid) == child


def test_generate_request_id():
    rid = s.generate_request_id()
    assert len(rid) == 8 and rid == rid.upper()


# --- форма міні-додатка ------------------------------------------------------

def _form(**overrides):
    form = {
        "department": "Виробництво",
        "vehicle_type": "Самоскид",
        "initiator": "Петренко Іван",
        "company": "Агрокряж",
        "cargo_type": "Зерно",
        "crop": "Кукурудза",
        "size_type": "Насип",
        "volume": "25,5",
        "date_type": "single",
        "date_from": "2026-10-21",
        "load_cities": ["Вінниця (Вінницька обл.)"],
        "unload_cities": ["Одеса (Одеська обл.)"],
        "unload_method": "Бокове",
    }
    form.update(overrides)
    return form


def test_normalize_form_happy_path():
    data = s.normalize_form_payload(_form(), today=TODAY)
    assert data["department"] == "Виробництво"
    assert data["thread_id"] == s.THREAD_IDS["Виробництво"]
    assert data["cargo_type"] == "Зерно: Кукурудза"
    assert data["volume"] == "25.5"
    assert data["date_period"] == "21.10.2026"
    assert data["big_bag_weight"] == "—"
    assert data["unload_method"] == "Самоскид"  # Насип → завжди самоскид
    assert data["load_place"] == "—"
    assert data["notes"] == "—"


def test_normalize_form_other_and_liquid():
    data = s.normalize_form_payload(_form(
        vehicle_type="Інше", vehicle_type_custom="Маніпулятор",
        company="Інше", company_custom="ТОВ Ромашка",
        cargo_type="КАС", size_type="Рідкі", load_method="Насос", unload_method="Насос",
        date_type="period", date_from="2026-10-20", date_to="2026-10-24",
    ), today=TODAY)
    assert data["vehicle_type"] == "Інше: Маніпулятор"
    assert data["company"] == "Інше: ТОВ Ромашка"
    assert data["cargo_type"] == "КАС"
    assert data["load_method"] == "—" and data["unload_method"] == "—"
    assert data["date_period"] == "20.10.2026 - 24.10.2026"
    assert data["date_type"] == "period"


def test_normalize_form_distribution():
    form = _form(volume="30", unload_cities=["Київ", "Львів"], distribution={"Київ": "10", "Львів": "20"},
                 unload_place="Склад 1")
    data = s.normalize_form_payload(form, today=TODAY)
    assert data["unload_city"] == "Київ; Львів"
    assert data["unload_distribution"] == {"Київ": 10.0, "Львів": 20.0}
    assert data["unload_place"] == "—"

    with pytest.raises(s.FormValidationError, match="Сума розподілу"):
        s.normalize_form_payload(_form(volume="30", unload_cities=["Київ", "Львів"],
                                       distribution={"Київ": "10", "Львів": "10"}), today=TODAY)


@pytest.mark.parametrize("overrides, message", [
    ({"department": "Склад"}, "від кого"),
    ({"initiator": "  "}, "ініціатора"),
    ({"crop": ""}, "культуру"),
    ({"size_type": "Біг-бег", "big_bag_weight": ""}, "біг-бегу"),
    ({"volume": "багато"}, "Обсяг"),
    ({"date_from": ""}, "дату"),
    ({"date_from": "2026-10-19"}, "минулому"),
    ({"date_type": "period", "date_from": "2026-10-25", "date_to": "2026-10-22"}, "раніше"),
    ({"unload_cities": []}, "розвантаження"),
    ({"vehicle_type": "Інше", "vehicle_type_custom": ""}, "Тип авто"),
])
def test_normalize_form_validation(overrides, message):
    with pytest.raises(s.FormValidationError, match=message):
        s.normalize_form_payload(_form(**overrides), today=TODAY)


def test_admin_may_use_past_dates():
    data = s.normalize_form_payload(_form(date_from="2026-10-01"), today=TODAY, allow_past=True)
    assert data["date_period"] == "01.10.2026"


def test_form_roundtrip():
    original = s.normalize_form_payload(_form(
        cargo_type="Насіння", crop="Соя", size_type="Біг-бег", big_bag_weight="1000", volume="40",
        vehicle_type="Інше", vehicle_type_custom="Шаланда",
        date_type="period", date_from="2026-10-21", date_to="2026-10-23",
        unload_cities=["Київ", "Львів"], distribution={"Київ": "15", "Львів": "25"},
        load_contact="Іван 0501234567",
    ), today=TODAY)
    form = s.request_data_to_form(original)
    assert form["cargo_type"] == "Насіння" and form["crop"] == "Соя"
    assert form["vehicle_type"] == "Інше" and form["vehicle_type_custom"] == "Шаланда"
    assert form["date_type"] == "period" and form["date_to"] == "2026-10-23"
    again = s.normalize_form_payload(form, today=TODAY)
    for key in s.FORM_FIELD_KEYS:
        assert again.get(key) == original.get(key), key
    assert again["unload_distribution"] == original["unload_distribution"]


def test_form_from_chat_request_with_custom_values():
    chat_data = {"cargo_type": "Зерно: Ячмінь", "company": "Вінницький ХАБ", "vehicle_type": "Тент",
                 "date_period": "01.09.2026"}
    form = s.request_data_to_form(chat_data, include_dates=False)
    assert form["cargo_type"] == "Зерно" and form["crop"] == "Ячмінь"
    assert form["company"] == "Інше" and form["company_custom"] == "Вінницький ХАБ"
    assert form["date_from"] == ""


# --- доступи ------------------------------------------------------------------

class _User:
    def __init__(self, uid, username=None):
        self.id = uid
        self.username = username


def test_admin_and_logist_detection(monkeypatch):
    for name in ("ADMIN_USER_IDS", "ADMIN_USER_ID", "ADMIN_USERNAMES", "LOGIST_USER_IDS", "LOGIST_USERNAMES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ADMIN_USER_ID", "1")
    monkeypatch.setenv("ADMIN_USERNAMES", "@Boss")
    assert s.is_admin_user(_User(1))
    assert s.is_admin_user(_User(5, "boss"))
    assert not s.is_admin_user(_User(2))

    # Без списку логістів — будь-хто з робочої групи
    assert s.is_logist_user(_User(2))
    monkeypatch.setenv("LOGIST_USER_IDS", "7, 8")
    assert s.is_logist_user(_User(7))
    assert s.is_logist_user(_User(1))  # адмін теж може
    assert not s.is_logist_user(_User(2))


# --- підпис Telegram WebApp ---------------------------------------------------

def _sign(fields, token):
    data_check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return dict(fields, hash=hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest())


def test_validate_webapp_init_data():
    token = "123:ABC"
    now = time.time()
    fields = {"auth_date": str(int(now)), "query_id": "q1",
              "user": json.dumps({"id": 42, "first_name": "Іван", "username": "ivan"})}
    init_data = urlencode(_sign(fields, token))

    parsed = s.validate_webapp_init_data(init_data, token, now=now)
    assert parsed["user"]["id"] == 42

    assert s.validate_webapp_init_data(init_data, "123:OTHER", now=now) is None
    tampered = init_data.replace("q1", "q2")
    assert s.validate_webapp_init_data(tampered, token, now=now) is None
    assert s.validate_webapp_init_data(init_data, token, now=now + 2 * 86400) is None
    assert s.validate_webapp_init_data(urlencode(fields), token, now=now) is None
    assert s.validate_webapp_init_data("", token) is None
