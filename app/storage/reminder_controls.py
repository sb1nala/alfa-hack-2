"""Owner + xmin checks and single-use proposals, in the mutation transaction."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4
from app.models import Reminder
from app.reminders import controls
from app.reminders.schedule import next_occurrence
from app.storage import db


@dataclass
class View:
    reminder: Reminder
    revision: str
    sent: bool
    retry_at: datetime | None

    @property
    def status(self):
        if not self.reminder.active and self.sent:
            if not self.reminder.rrule:
                return "Завершено"
            try:
                if (
                    next_occurrence(self.reminder.rrule, self.reminder.next_run_at)
                    is None
                ):
                    return "Завершено"
            except ValueError:
                return "Ошибка расписания"
        return "Активно" if self.reminder.active else "Пауза"


def view(row):
    fields = ("id", "user_id", "kind", "text", "next_run_at", "rrule", "active")
    return View(
        Reminder(**{k: row[k] for k in fields}),
        row["revision"],
        row["sent"],
        row["retry_at"],
    )


async def list_for(user_id: int) -> list[View]:
    rows = await db.pool().fetch(
        """SELECT r.id,r.user_id,r.kind,r.text,r.next_run_at,r.rrule,r.active,
        r.xmin::text AS revision, COALESCE(d.state='sent',false) AS sent,
        CASE WHEN d.state='retry' THEN d.retry_at END AS retry_at
        FROM reminders r LEFT JOIN reminder_deliveries d
        ON d.reminder_id=r.id AND d.scheduled_at=r.next_run_at
        WHERE r.user_id=$1 ORDER BY r.next_run_at,r.id""",
        user_id,
    )
    return [view(row) for row in rows]


async def propose(user: int, id_: int, revision: str, action: str, now: datetime):
    frequency_action = action in {"daily", "weekly", "monthly", "once"}
    interval_action = action in {"i1", "i2", "i3", "i7", "i14", "i30", "i100"}
    if (
        action not in {"pause", "resume", "now", "delete", "less"}
        and not interval_action
        and not frequency_action
    ):
        raise ValueError("Кнопка устарела. Откройте /reminders.")
    async with db.pool().acquire() as c:
        async with c.transaction():
            row = await c.fetchrow(
                "SELECT r.*,r.xmin::text AS revision FROM reminders r WHERE id=$1 AND user_id=$2 FOR UPDATE",
                id_,
                user,
            )
            if row is None or row["revision"] != revision:
                raise ValueError("Запись удалена или изменена. Откройте /reminders.")
            r = Reminder(**{k: v for k, v in dict(row).items() if k != "revision"})
            run, rule = r.next_run_at, r.rrule
            if frequency_action:
                zone = (
                    await c.fetchval(
                        "SELECT timezone FROM profiles WHERE user_id=$1", user
                    )
                    or "Europe/Moscow"
                )
                rule, run = controls.frequency_change(r, now, action, zone)
            elif action == "less" or interval_action:
                rule, run = controls.less_often(
                    r, now, int(action[1:]) if interval_action else None
                )
            elif action in {"resume", "now"}:
                if action == "now" and (r.kind == "tax" or r.rrule):
                    raise ValueError(
                        "Налоговые даты не сдвигаются. Управление подпиской: /taxes."
                    )
                if r.active:
                    raise ValueError("Напоминание уже активно.")
                if not r.rrule and await c.fetchval(
                    "SELECT 1 FROM reminder_deliveries WHERE reminder_id=$1 AND scheduled_at=$2 AND state='sent'",
                    id_,
                    run,
                ):
                    raise ValueError(
                        "Напоминание завершено. Создайте новое через /remind."
                    )
                run = controls.resume(r, now, deliver_now=action == "now")
            elif action == "pause" and not r.active:
                raise ValueError("Напоминание уже на паузе.")
            token = uuid4()
            await c.execute(
                """INSERT INTO reminder_actions
                (token,reminder_id,user_id,revision,action,rrule,run_at,expires_at)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                token,
                id_,
                user,
                revision,
                action,
                rule,
                run,
                now + timedelta(minutes=15),
            )
            return token, rule, run


async def apply(user: int, token: UUID, now: datetime) -> tuple[int, str, str]:
    async with db.pool().acquire() as c:
        async with c.transaction():
            # Same order as worker: reminder before dependent event/action.
            draft = await c.fetchrow(
                "SELECT * FROM reminder_actions WHERE token=$1 AND user_id=$2",
                token,
                user,
            )
            if draft is None:
                raise ValueError(
                    "Подтверждение удалено или принадлежит другому пользователю."
                )
            r = await c.fetchrow(
                "SELECT r.*,r.xmin::text AS revision FROM reminders r WHERE id=$1 AND user_id=$2 FOR UPDATE",
                draft["reminder_id"],
                user,
            )
            draft = await c.fetchrow(
                "SELECT * FROM reminder_actions WHERE token=$1 FOR UPDATE", token
            )
            if (
                not r
                or not draft
                or draft["consumed"]
                or draft["expires_at"] <= now
                or r["revision"] != draft["revision"]
            ):
                raise ValueError(
                    "Подтверждение устарело или уже использовано. Откройте /reminders."
                )
            action = draft["action"]
            if (
                action == "less"
                or action.startswith("i")
                or action in {"daily", "weekly", "monthly", "once"}
            ) and r["kind"] == "tax":
                raise ValueError("Налоговая частота не меняется: /taxes.")
            if action == "delete":
                await c.execute(
                    "DELETE FROM reminders WHERE id=$1 AND user_id=$2", r["id"], user
                )
            else:
                if (
                    action in {"resume", "less", "daily", "weekly", "monthly", "once"}
                    or action.startswith("i")
                ) and draft["run_at"] <= now:
                    raise ValueError(
                        "Предложенный срок прошёл. Откройте напоминание заново."
                    )
                await c.execute(
                    """UPDATE reminders SET active=$3,rrule=$4,next_run_at=$5 WHERE id=$1 AND user_id=$2""",
                    r["id"],
                    user,
                    False
                    if action == "pause"
                    else True
                    if action in {"resume", "now"}
                    else r["active"],
                    draft["rrule"],
                    now.replace(microsecond=0) if action == "now" else draft["run_at"],
                )
                await c.execute(
                    "UPDATE reminder_actions SET consumed=true WHERE token=$1", token
                )
            return r["id"], r["kind"], action


async def button_revision(user: int, id_: int, token: UUID) -> str | None:
    return await db.pool().fetchval(
        """SELECT b.revision FROM reminder_delivery_buttons b
        JOIN reminders r ON r.id=b.reminder_id WHERE b.token=$1 AND r.id=$2 AND r.user_id=$3
        AND b.revision=r.xmin::text""",
        token,
        id_,
        user,
    )


async def cancel(user: int, token: UUID) -> None:
    await db.pool().execute(
        "UPDATE reminder_actions SET consumed=true WHERE token=$1 AND user_id=$2",
        token,
        user,
    )
