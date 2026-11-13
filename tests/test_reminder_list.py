"""Management UI and durable actions on real PostgreSQL."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Update
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from app.bot import handlers, reminder_list as ui
from app.models import Reminder
from app.reminders import service, worker
from app.storage import reminders, profiles, reminder_controls as store
from tests.test_reminder_commands import FakeSession

NOW = datetime(2026, 11, 13, 7, tzinfo=timezone.utc)
USER = 8200000001


async def create(*, user=USER, rule=None, active=True, when=None, kind="custom"):
    return await service.create(
        Reminder(
            0,
            user,
            kind,
            "Сверить <кассу>",
            when or NOW + timedelta(days=1),
            rule,
            active,
        )
    )


async def current(r):
    return next(v for v in await store.list_for(r.user_id) if v.reminder.id == r.id)


async def propose(r, action):
    v = await current(r)
    return await store.propose(r.user_id, r.id, v.revision, action, NOW)


@pytest.mark.parametrize(
    "text", ["покажи мои напоминания", "список напоминаний", "какие у меня напоминания"]
)
def test_plain_request(text):
    assert ui.is_list_request(text)
    assert not ui.is_list_request("напомни завтра в 10")


def test_render_empty_pagination_and_escaping():
    assert ui.render([], "Europe/Moscow") == (ui.EMPTY_TEXT, None)
    items = [
        Reminder(i, USER, "custom", "Длинное <дело> " + "x" * 200, NOW, None, False)
        for i in range(19)
    ]
    seen = []
    for page in range(3):
        text, kb = ui.render(items, "Asia/Vladivostok", page=page)
        assert "Пауза" in text and "Asia/Vladivostok" in text and "&lt;дело&gt;" in text
        seen += [
            int(b.callback_data.split(":")[2])
            for row in kb.inline_keyboard
            for b in row
            if b.callback_data.startswith("rm:v:")
        ]
    assert seen == list(range(19))


async def test_owner_version_confirmation_pause_resume_delete(storage_db):
    r = await create(rule="FREQ=DAILY;COUNT=5")
    other = await create(user=USER + 1)
    v = await current(r)
    with pytest.raises(ValueError):
        await store.propose(USER + 1, r.id, v.revision, "delete", NOW)
    token, _, _ = await propose(r, "pause")
    assert (await reminders.get(r.id)).active
    await store.apply(USER, token, NOW)
    assert not (await reminders.get(r.id)).active
    with pytest.raises(ValueError):
        await store.apply(USER, token, NOW)
    token, _, run = await propose(r, "resume")
    assert run > NOW
    await store.apply(USER, token, NOW)
    old, _, _ = await propose(r, "delete")
    await service.pause(r.id, False)
    with pytest.raises(ValueError):
        await store.apply(USER, old, NOW)
    token, _, _ = await propose(r, "delete")
    await store.apply(USER, token, NOW)
    assert await reminders.get(r.id) is None
    assert await reminders.get(other.id) == other


async def test_less_double_click_profile_zone_and_pause(storage_db):
    r = await create(rule="FREQ=DAILY;COUNT=10", active=False)
    await profiles.update(USER, {"timezone": "Asia/Vladivostok"})
    token, rule, run = await propose(r, "less")
    assert (
        "INTERVAL=2" in rule
        and "COUNT=5" in rule
        and rule.splitlines()[0] == r.rrule.splitlines()[0]
    )
    assert await reminders.get(r.id) == r
    result = await asyncio.gather(
        store.apply(USER, token, NOW),
        store.apply(USER, token, NOW),
        return_exceptions=True,
    )
    assert sum(isinstance(x, ValueError) for x in result) == 1
    changed = await reminders.get(r.id)
    assert not changed.active and changed.rrule == rule and changed.next_run_at == run


async def test_completed_and_overdue_one_off(storage_db):
    r = await create(when=NOW - timedelta(days=1), active=False)
    with pytest.raises(ValueError, match="Срок прошёл"):
        await propose(r, "resume")
    token, _, _ = await propose(r, "now")
    await store.apply(USER, token, NOW + timedelta(seconds=1))
    bot = type(
        "Bot",
        (),
        {"send_message": AsyncMock(return_value=type("M", (), {"message_id": 1})())},
    )()
    await worker.run_once(bot, lambda: NOW + timedelta(seconds=1))
    assert (await current(r)).status == "Завершено"
    with pytest.raises(ValueError, match="завершено"):
        await propose(r, "resume")


@pytest.mark.parametrize(
    "kind,rule", [("tax", "FREQ=DAILY"), ("tax", None), ("custom", None)]
)
async def test_forged_less_rejected(storage_db, kind, rule):
    r = await create(kind=kind, rule=rule)
    with pytest.raises(ValueError):
        await propose(r, "less")


@pytest.fixture
async def chat(storage_db, monkeypatch):
    session = FakeSession()
    bot = Bot("123456:fake-test-token", session=session)
    dp = Dispatcher()
    dp.message.handlers = handlers.router.message.handlers.copy()
    dp.callback_query.handlers = handlers.router.callback_query.handlers.copy()
    monkeypatch.setattr(ui, "utcnow", lambda: NOW)

    async def feed(text=None, data=None, group=False, user=USER):
        message = {
            "message_id": 1,
            "date": 1,
            "chat": {"id": user, "type": "group" if group else "private"},
        }
        person = {"id": user, "is_bot": False, "first_name": "Тест"}
        payload = (
            {"update_id": 1, "message": {**message, "from": person, "text": text}}
            if data is None
            else {
                "update_id": 1,
                "callback_query": {
                    "id": "test",
                    "from": person,
                    "chat_instance": "test",
                    "data": data,
                    "message": message,
                },
            }
        )
        session.calls.clear()
        await dp.feed_update(bot, Update.model_validate(payload))
        return list(session.calls)

    yield feed
    await session.close()


def screen(calls):
    return next(c for c in calls if isinstance(c, (SendMessage, EditMessageText)))


def buttons(message):
    return [
        b.callback_data for row in message.reply_markup.inline_keyboard for b in row
    ]


async def test_routed_list_pages_detail_confirm_foreign_group(chat):
    rs = [await create() for _ in range(10)]
    await create(user=USER + 1)
    for text in ("/reminders", "покажи мои напоминания"):
        page = screen(await chat(text))
        assert len([b for b in buttons(page) if b.startswith("rm:v:")]) == 8
    page = screen(await chat(data="rm:list:1"))
    assert f"rm:v:{rs[-1].id}" in buttons(page)
    detail = screen(await chat(data=f"rm:v:{rs[0].id}"))
    delete = next(b for b in buttons(detail) if b.endswith(":delete"))
    preview = screen(await chat(data=delete))
    assert "без возможности восстановления" in preview.text
    assert await reminders.get(rs[0].id)
    confirmation = next(b for b in buttons(preview) if b.startswith("rm:yes:"))
    foreign = await chat(data=confirmation, user=USER + 1)
    assert any(isinstance(c, AnswerCallbackQuery) for c in foreign)
    await chat(data=confirmation)
    assert await reminders.get(rs[0].id) is None
    for callback in (
        confirmation,
        delete,
        "rm:s:1:daily",
        "rm:p:1:bad:less",
        "rm:garbage",
    ):
        assert any(
            isinstance(c, AnswerCallbackQuery) for c in await chat(data=callback)
        )
    assert screen(await chat("/reminders", group=True)).text == ui.PRIVATE_ONLY_TEXT
    assert len(await service.list_for(USER)) == 9


async def test_delivery_less_button_and_stale_after_change(chat, storage_db):
    r = await create(rule="FREQ=DAILY;COUNT=8", when=NOW)
    bot = type(
        "Bot",
        (),
        {"send_message": AsyncMock(return_value=type("M", (), {"message_id": 1})())},
    )()
    await worker.run_once(bot, lambda: NOW)
    data = (
        bot.send_message.await_args.kwargs["reply_markup"]
        .inline_keyboard[0][1]
        .callback_data
    )
    preview = screen(await chat(data=data))
    assert "интервал 2" in preview.text
    yes = next(b for b in buttons(preview) if b.startswith("rm:yes:"))
    await chat(data=yes)
    changed = await reminders.get(r.id)
    await chat(data=yes)
    await chat(data=data)
    assert await reminders.get(r.id) == changed


async def test_cancel_expire_and_forged_now_repeat(storage_db):
    r = await create(rule="FREQ=DAILY;UNTIL=20261120T070000Z", active=False)
    with pytest.raises(ValueError):
        await propose(r, "now")
    token, _, _ = await propose(r, "delete")
    await store.cancel(USER, token)
    with pytest.raises(ValueError):
        await store.apply(USER, token, NOW)
    token, _, _ = await propose(r, "delete")
    with pytest.raises(ValueError):
        await store.apply(USER, token, NOW + timedelta(minutes=15))
    assert await reminders.get(r.id) == r


async def test_resume_paused_repeat_after_sent_and_http_mutation(storage_db):
    r = await create(rule="FREQ=DAILY;COUNT=8", when=NOW)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def send(*args, **kwargs):
        entered.set()
        await release.wait()
        return type("M", (), {"message_id": 1})()

    b = type("Bot", (), {"send_message": AsyncMock(side_effect=send)})()
    task = asyncio.create_task(worker.run_once(b, lambda: NOW))
    await asyncio.wait_for(entered.wait(), 3)
    try:
        token, _, _ = await propose(r, "pause")
        await store.apply(USER, token, NOW)
        paused = await reminders.get(r.id)
    finally:
        release.set()
        await task
    assert await reminders.get(r.id) == paused
    assert (await current(r)).status == "Пауза"
    token, _, run = await propose(r, "resume")
    assert run == NOW + timedelta(days=1)
    await store.apply(USER, token, NOW)
    assert (await reminders.get(r.id)).next_run_at == run


@pytest.mark.parametrize("frequency", ["daily", "weekly", "monthly", "once"])
async def test_frequency_replacement_preview_pause_and_anchor(storage_db, frequency):
    r = await create(rule="FREQ=DAILY;COUNT=90", active=False)
    await profiles.update(USER, {"timezone": "Asia/Vladivostok"})
    token, rule, run = await propose(r, frequency)
    assert await reminders.get(r.id) == r
    if frequency == "once":
        assert rule is None
    else:
        assert rule.splitlines()[0] == r.rrule.splitlines()[0]
        assert "COUNT=" in rule and "FREQ=" + frequency.upper() in rule
    await store.apply(USER, token, NOW)
    result = await reminders.get(r.id)
    assert not result.active and result.next_run_at == run and result.rrule == rule


async def test_oneoff_to_repeat_is_explicit_and_tax_type_forgery_fails(storage_db):
    r = await create()
    token, rule, run = await propose(r, "weekly")
    assert "FREQ=WEEKLY" in rule and not r.rrule
    await store.apply(USER, token, NOW)
    assert (await reminders.get(r.id)).rrule == rule
    tax = await create(kind="tax")
    with pytest.raises(ValueError):
        await propose(tax, "daily")
