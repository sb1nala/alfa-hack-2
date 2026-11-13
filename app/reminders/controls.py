"""Pure user actions, separate from the task 3 service interface."""

from datetime import datetime, timedelta
from app.models import Reminder
from app.reminders.schedule import _parse, _events, next_occurrence, aware_utc


def less_often(
    reminder: Reminder, now: datetime, target: int | None = None
) -> tuple[str, datetime]:
    if reminder.kind == "tax":
        raise ValueError(
            "Налоговые даты определяет календарь. Управление подпиской: /taxes; конкретное напоминание можно отключить."
        )
    if not reminder.rrule:
        raise ValueError(
            "Это однократное напоминание. Оно не станет подпиской. Откройте /reminders."
        )
    canonical, start, rule = _parse(reminder.rrule)
    fields = dict(x.split("=") for x in canonical.splitlines()[1][6:].split(";"))
    interval = int(fields.get("INTERVAL", "1"))
    if target is None and interval > 50:
        raise ValueError(
            "Удвоение превысит максимальный интервал 100. Можно поставить на паузу."
        )
    if target is not None and (not 1 <= target <= 100 or target == interval):
        raise ValueError("Выберите другой интервал от 1 до 100.")
    fields["INTERVAL"] = str(target if target is not None else interval * 2)
    if "COUNT" in fields:
        # Keep COUNT and additionally reduce it to events of the slower rule
        # within the original finite calendar horizon. Never restart the count.
        original_events = list(_events(start, rule))
        last = original_events[-1]
        candidate = (
            canonical.splitlines()[0]
            + "\nRRULE:"
            + ";".join(f"{k}={v}" for k, v in fields.items())
        )
        _, anchor, slower = _parse(candidate)
        candidate_events = list(_events(anchor, slower))
        threshold = max(aware_utc(now), reminder.next_run_at - timedelta(seconds=1))
        remaining = sum(event > threshold for event in original_events)
        past = sum(event <= threshold for event in candidate_events)
        fields["COUNT"] = str(
            min(
                int(fields["COUNT"]),
                sum(event <= last for event in candidate_events),
                past + remaining,
            )
        )
        if not remaining:
            raise ValueError("Конечный повтор завершён. Создайте новое напоминание.")
    changed = (
        canonical.splitlines()[0]
        + "\nRRULE:"
        + ";".join(f"{k}={v}" for k, v in fields.items())
    )
    next_run = next_occurrence(
        changed, max(aware_utc(now), reminder.next_run_at - timedelta(seconds=1))
    )
    if next_run is None:
        raise ValueError(
            "Конечный повтор завершён: будущих сроков с такой частотой нет."
        )
    return changed, next_run


def resume(reminder: Reminder, now: datetime, *, deliver_now: bool = False) -> datetime:
    now = aware_utc(now).replace(microsecond=0)
    if reminder.rrule:
        result = next_occurrence(reminder.rrule, now)
        if result is None:
            raise ValueError(
                "Повтор завершён по COUNT/UNTIL. Создайте новое напоминание явно."
            )
        return result
    if reminder.next_run_at > now:
        return reminder.next_run_at
    if deliver_now:
        return now
    raise ValueError(
        "Срок прошёл. Подтвердите доставку сейчас или создайте новое будущее время командой /remind."
    )


def frequency_change(
    reminder: Reminder, now: datetime, frequency: str, zone: str
) -> tuple[str | None, datetime]:
    """Explicit replacement of frequency, retaining the old local anchor and limits."""
    from app.reminders.schedule import canonicalize
    from zoneinfo import ZoneInfo

    if reminder.kind == "tax":
        raise ValueError("Налоговые даты не меняются. Управление подпиской: /taxes.")
    threshold = max(aware_utc(now), reminder.next_run_at - timedelta(seconds=1))
    old_fields = {}
    old_events = None
    if reminder.rrule:
        canonical, anchor, old_rule = _parse(reminder.rrule)
        old_fields = dict(
            x.split("=") for x in canonical.splitlines()[1][6:].split(";")
        )
        header = canonical.splitlines()[0]
        if "COUNT" in old_fields:
            old_events = list(_events(anchor, old_rule))
    else:
        anchor = reminder.next_run_at.astimezone(ZoneInfo(zone))
        header = ""
    if frequency == "once":
        # Removing finite constraints is an explicit, separately confirmed choice.
        if reminder.next_run_at <= now:
            raise ValueError("Срок прошёл. Задайте будущее время через /remind.")
        return None, reminder.next_run_at
    if frequency not in {"daily", "weekly", "monthly"}:
        raise ValueError("Неизвестная частота.")
    fields = {"FREQ": frequency.upper()}
    if frequency == "weekly":
        fields["BYDAY"] = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")[anchor.weekday()]
    elif frequency == "monthly":
        fields["BYMONTHDAY"] = str(anchor.day)
    if "UNTIL" in old_fields:
        fields["UNTIL"] = old_fields["UNTIL"]
    if old_events is not None:
        fields["COUNT"] = old_fields["COUNT"]
    short = ";".join(f"{k}={v}" for k, v in fields.items())
    if header:
        candidate = header + "\nRRULE:" + short
    else:
        candidate = canonicalize(short, reminder.next_run_at, zone)
    if old_events is not None:
        # Bound both the horizon and remaining number; changing BYDAY/FREQ may
        # otherwise reset finite progress even with an unchanged COUNT.
        remaining = sum(event > threshold for event in old_events)
        if not remaining:
            raise ValueError("Конечный повтор завершён. Создайте новое напоминание.")
        _, start, new_rule = _parse(candidate)
        generated = list(_events(start, new_rule))
        past = sum(event <= threshold for event in generated)
        future = sum(threshold < event <= old_events[-1] for event in generated)
        fields["COUNT"] = str(past + min(remaining, future))
        if not future:
            raise ValueError("До конца исходного повтора новых сроков нет.")
        candidate = (
            header + "\nRRULE:" + ";".join(f"{k}={v}" for k, v in fields.items())
        )
    run = next_occurrence(candidate, threshold)
    if run is None:
        raise ValueError("Повтор завершён по COUNT/UNTIL.")
    return candidate, run
