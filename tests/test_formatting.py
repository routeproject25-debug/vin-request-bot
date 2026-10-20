import formatting


def test_notification_keeps_edit_and_status_footers():
    data = {
        "created_by_mention": "@author",
        "created_date": "20.10.2026",
        "created_time": "10:00",
        "last_edit_footer": "Вніс зміни: @editor",
        "status_footer": "Статус: ✅ ВИКОНАНО",
    }
    text = formatting.build_request_notification("ABCDEF12", data, None)
    assert text.startswith("📋 @author створив нову заявку:\n🆔 ID заявки: ABCDEF12")
    assert text.endswith("-------\nВніс зміни: @editor\n\nСтатус: ✅ ВИКОНАНО")

    explicit = formatting.build_request_notification("ABCDEF12", data, None, edit_footer="Вніс зміни: @new")
    assert "@new" in explicit and "@editor" not in explicit


def test_notification_without_footers():
    text = formatting.build_request_notification("X", {"created_by_mention": "@a"}, None)
    assert "-------" not in text


def test_calendar_callback_with_request_prefix():
    prefix = "RMC|ABCDEF12"
    assert formatting.parse_calendar_callback(f"{prefix}:D:2026-10-21", prefix) == ("DATE", "2026-10-21")
    assert formatting.parse_calendar_callback(f"{prefix}:N:2026-11", prefix) == ("NAV", "2026-11")
    assert formatting.parse_calendar_callback(f"{prefix}:X", prefix) == ("IGNORE", None)
    assert formatting.parse_calendar_callback("CAL:D:2026-10-21") == ("DATE", "2026-10-21")
    assert formatting.parse_calendar_callback("OTHER:D:2026-10-21") == ("IGNORE", None)

    markup = formatting.build_month_calendar(2026, 10, prefix=prefix)
    for row in markup.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64  # обмеження Telegram


def test_short_city_name_strips_region():
    assert formatting.short_city_name("Київ (Київський р-н, Київська обл.)") == "Київ"
    assert formatting.short_city_name("м. Одеса; Львів") == "Одеса"
    assert formatting.build_compact_route({"load_city": "Вінниця", "unload_city": "Київ; Львів"}) == "Вінниця→Київ+1"


def test_changes_text():
    assert formatting.get_changes_text({"volume": "10"}, {"volume": "12"}) == "✏️ Зміни:\n📊 Обсяг: 10 → 12"
    assert formatting.get_changes_text({"volume": "10"}, {"volume": "10"}) == "✏️ Заявку відредаговано"
