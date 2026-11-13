"""Налоговые напоминания по режиму пользователя из tax_calendar.yaml.

Сроки в календаре — конкретные даты с учётом переноса с выходных, поэтому каждое событие
становится отдельным разовым напоминанием: правилом повтора такие переносы не описать.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from app.events import tracked_url
from app.kb import search as kb
from app.models import Reminder, UserProfile
from app.reminders import service
from app.reminders.schedule import aware_utc
from app.storage import reminder_delivery

log = logging.getLogger(__name__)

CALENDAR_PATH = Path(__file__).with_name("tax_calendar.yaml")
DAYS_BEFORE = 3  # напоминаем за три дня до срока
REMIND_HOUR = 10  # в десять утра по времени пользователя
HORIZON_DAYS = 366

REGIME_LABELS = {
    "usn_income": "УСН «доходы»",
    "usn_income_expense": "УСН «доходы минус расходы»",
    "ausn": "АУСН",
    "psn": "патент",
    "npd": "самозанятость (НПД)",
    "osno": "общая система (ОСНО)",
}
FORM_LABELS = {"ip": "ИП", "ooo": "ООО", "self_employed": "самозанятый"}
# Режимы, для которых форма бизнеса однозначна и спрашивать её не нужно.
FORM_BY_REGIME = {"npd": "self_employed", "psn": "ip"}


@dataclass(frozen=True)
class TaxDue:
    event_id: str
    title: str
    text: str
    due: date
    period: str
    source: str
    search_hint: str


@lru_cache(maxsize=1)
def load_calendar(path: Path = CALENDAR_PATH) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def effective_form(profile: UserProfile) -> str | None:
    """Форма бизнеса для подбора событий: из профиля или единственно возможная для режима."""
    if profile.tax_regime in FORM_BY_REGIME and profile.legal_form is None:
        return FORM_BY_REGIME[profile.tax_regime]
    return profile.legal_form


def missing(profile: UserProfile) -> str | None:
    """Чего не хватает, чтобы подобрать сроки: "regime", "legal_form" или None."""
    if profile.tax_regime not in REGIME_LABELS:
        return "regime"
    if effective_form(profile) not in FORM_LABELS:
        return "legal_form"
    return None


def _matching(profile: UserProfile) -> list[dict]:
    form = effective_form(profile)
    return [
        event
        for event in load_calendar()["events"]
        if profile.tax_regime in event["regimes"] and form in event["who"]
    ]


def upcoming(profile: UserProfile, today: date, horizon_days: int = HORIZON_DAYS) -> list[TaxDue]:
    """Сроки по режиму пользователя от сегодня и на horizon_days вперёд, ближайший первым."""
    last = today + timedelta(days=horizon_days)
    dues = [
        TaxDue(
            event_id=event["id"],
            title=event["title"],
            text=event["text"],
            due=item["due"],
            period=item["period"],
            source=event["source"],
            search_hint=event["search_hint"],
        )
        for event in _matching(profile)
        for item in event["dates"]
        if today <= item["due"] <= last
    ]
    return sorted(dues, key=lambda due: (due.due, due.event_id))


def has_relative_events(profile: UserProfile) -> bool:
    """Есть ли у режима сроки, которые считаются от личных дат (оплата патента)."""
    return any(event["frequency"] == "relative" for event in _matching(profile))


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Europe/Moscow")


def remind_at(due: date, now: datetime, zone_name: str) -> datetime | None:
    """Момент напоминания в UTC: за три дня до срока в 10:00 по местному времени.

    Если этот момент уже прошёл — ближайшие 10:00, но не позже дня срока. Иначе None.
    """
    zone = _zone(zone_name)
    local_now = aware_utc(now).astimezone(zone)
    day = due - timedelta(days=DAYS_BEFORE)
    moment = datetime(day.year, day.month, day.day, REMIND_HOUR, tzinfo=zone)
    if moment <= local_now:
        day = local_now.date() if local_now.hour < REMIND_HOUR else local_now.date() + timedelta(days=1)
        moment = datetime(day.year, day.month, day.day, REMIND_HOUR, tzinfo=zone)
    if moment.date() > due:
        return None
    return aware_utc(moment)


def _material_line(search_hint: str) -> str:
    """Ссылка на материал «как это сделать» из базы знаний; пусто, если ничего не нашлось."""
    try:
        chunks = kb.search(search_hint, k=1)
    except RuntimeError:  # база знаний не загружена
        return ""
    if not chunks:
        return ""
    material = chunks[0].material
    return f"Как это сделать: {material.title}\n{tracked_url(material.url, 'reminder_tax')}"


def reminder_text(due: TaxDue) -> str:
    lines = [
        due.text,
        f"Срок — {due.due:%d.%m.%Y} ({due.period}).",
        f"Проверьте дату на сайте ФНС: {due.source}",
    ]
    material = _material_line(due.search_hint)
    if material:
        lines.append(material)
    return "\n".join(lines)


def plan(profile: UserProfile, now: datetime) -> list[tuple[TaxDue, datetime]]:
    """Какие напоминания будут созданы: (срок, момент напоминания в UTC)."""
    today = aware_utc(now).astimezone(_zone(profile.timezone)).date()
    result = []
    for due in upcoming(profile, today):
        moment = remind_at(due.due, now, profile.timezone)
        if moment is not None:
            result.append((due, moment))
    return result


async def subscribe(profile: UserProfile, now: datetime) -> int:
    """Создать налоговые напоминания; уже существующие активные не дублируются."""
    existing = {
        (reminder.text, reminder.next_run_at)
        for reminder in await service.list_for(profile.user_id)
        if reminder.kind == "tax" and reminder.active
    }
    created = 0
    for due, moment in plan(profile, now):
        text = reminder_text(due)
        if (text, moment) in existing:
            continue
        await service.create(Reminder(0, profile.user_id, "tax", text, moment, None))
        created += 1
    return created


async def unsubscribe(user_id: int) -> int:
    """Отключить все активные налоговые напоминания пользователя."""
    disabled = 0
    for reminder in await service.list_for(user_id):
        if reminder.kind == "tax" and reminder.active:
            await reminder_delivery.disable(reminder.id, user_id)
            disabled += 1
    return disabled
