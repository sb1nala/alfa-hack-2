"""Налоговый календарь: структура, допустимые значения и правило переноса сроков с нерабочих дней."""

from datetime import date, timedelta
from pathlib import Path

import yaml

CALENDAR = Path(__file__).resolve().parent.parent / "app" / "reminders" / "tax_calendar.yaml"

REGIMES = {"usn_income", "usn_income_expense", "ausn", "psn", "npd", "osno"}
LEGAL_FORMS = {"ip", "ooo", "self_employed"}
KINDS = {"payment", "report"}
FREQUENCIES = {"monthly", "quarterly", "yearly", "relative"}
EVENT_KEYS = {
    "id", "title", "regimes", "who", "kind", "frequency", "text", "rule", "law", "source",
    "search_hint", "dates",
}

# Нерабочие праздничные дни и переносы на конец 2026 и 2027 год (ст. 112 ТК РФ,
# постановление Правительства о переносе выходных в 2027 году).
DAYS_OFF = {date(2026, 12, 31)} | {date(2027, 1, day) for day in range(1, 9)} | {
    date(2027, 2, 22), date(2027, 2, 23), date(2027, 3, 8), date(2027, 5, 3), date(2027, 5, 10),
    date(2027, 6, 14), date(2027, 11, 4), date(2027, 11, 5), date(2027, 12, 31),
}
WORKING_SATURDAYS = {date(2027, 2, 20)}


def _is_working(day: date) -> bool:
    if day in WORKING_SATURDAYS:
        return True
    return day.weekday() < 5 and day not in DAYS_OFF


def _next_working(day: date) -> date:
    while not _is_working(day):
        day += timedelta(days=1)
    return day


def _calendar() -> dict:
    return yaml.safe_load(CALENDAR.read_text(encoding="utf-8"))


def _events() -> list[dict]:
    return _calendar()["events"]


def test_events_are_well_formed():
    events = _events()
    assert len({e["id"] for e in events}) == len(events)
    for e in events:
        assert set(e) == EVENT_KEYS, e["id"]
        assert set(e["regimes"]) <= REGIMES and e["regimes"], e["id"]
        assert set(e["who"]) <= LEGAL_FORMS and e["who"], e["id"]
        assert e["kind"] in KINDS, e["id"]
        assert e["frequency"] in FREQUENCIES, e["id"]
        for key in ("title", "text", "rule", "law", "search_hint"):
            assert e[key].strip(), (e["id"], key)


def test_every_event_names_an_official_source():
    for e in _events():
        assert e["source"].startswith(("https://www.nalog.gov.ru/", "https://npd.nalog.ru/")), e["id"]


def test_every_regime_has_events():
    covered = {regime for e in _events() for regime in e["regimes"]}
    assert covered == REGIMES


def test_only_relative_events_have_no_dates():
    for e in _events():
        assert (e["dates"] == []) == (e["frequency"] == "relative"), e["id"]


def test_due_dates_are_shifted_to_working_days_and_stay_in_period():
    calendar = _calendar()
    start, end = calendar["period"]["from"], calendar["period"]["to"]
    for e in calendar["events"]:
        dues = [d["due"] for d in e["dates"]]
        assert dues == sorted(set(dues)), e["id"]
        for d in e["dates"]:
            assert set(d) == {"due", "nominal", "period"}, e["id"]
            assert d["due"] == _next_working(d["nominal"]), (e["id"], d["nominal"])
            assert start <= d["due"] <= end, (e["id"], d["due"])


def test_known_deadlines():
    by_id = {e["id"]: [d["due"] for d in e["dates"]] for e in _events()}
    assert date(2026, 11, 30) in by_id["npd_tax"]  # 28.11.2026 — суббота
    assert by_id["ip_contributions_fixed"] == [date(2026, 12, 28), date(2027, 12, 28)]
    assert by_id["ip_contributions_1pct"] == [date(2027, 7, 1)]
    assert by_id["usn_advance"] == [date(2027, 4, 28), date(2027, 7, 28), date(2027, 10, 28)]
    assert by_id["usn_declaration_ip"] == [date(2027, 4, 26)]  # 25.04.2027 — воскресенье
    assert by_id["usn_tax_ooo"] == [date(2027, 3, 29)]  # 28.03.2027 — воскресенье


def test_self_employed_do_not_get_ip_contributions():
    for e in _events():
        if e["id"].startswith("ip_contributions"):
            assert "npd" not in e["regimes"] and "ausn" not in e["regimes"]
            assert e["who"] == ["ip"]
