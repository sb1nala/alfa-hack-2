"""Lifecycle without Telegram polling, real sleeps, knowledge data or credentials."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import main
from app.reminders import worker
from app.storage import db


async def test_worker_scans_immediately_then_waits_and_cancels(monkeypatch):
    steps = []
    waiting = asyncio.Event()

    async def once(bot, clock):
        steps.append('scan')

    async def sleep(interval):
        assert interval == 30
        steps.append('wait')
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, 'run_once', once)
    monkeypatch.setattr(worker.asyncio, 'sleep', sleep)
    task = asyncio.create_task(worker.run(object()))
    await asyncio.wait_for(waiting.wait(), 3)
    assert steps == ['scan', 'wait']
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_worker_database_error_does_not_kill_loop(monkeypatch, caplog):
    waiting = asyncio.Event()
    monkeypatch.setattr(worker, 'run_once', AsyncMock(side_effect=RuntimeError('secret DATABASE_URL')))

    async def sleep(interval):
        waiting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker.asyncio, 'sleep', sleep)
    task = asyncio.create_task(worker.run(object()))
    await asyncio.wait_for(waiting.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert 'RuntimeError' in caplog.text and 'secret DATABASE_URL' not in caplog.text


@pytest.mark.parametrize('failure', [None, 'polling', 'connect', 'bot', 'router', 'cancel'])
async def test_main_stops_worker_before_closing_resources(monkeypatch, failure):
    steps = []
    entered = asyncio.Event()
    stopped = asyncio.Event()
    settings = SimpleNamespace(database_url='synthetic', data_dir='unused', llm_enabled=False,
                               telegram_bot_token='unused')
    monkeypatch.setattr(main, 'load_settings', lambda: settings)
    monkeypatch.setattr(main.kb, 'load', lambda _: [])

    async def connect(url):
        steps.append('connect')
        if failure == 'connect':
            raise RuntimeError('connect failure')

    async def close_db():
        if entered.is_set():
            assert stopped.is_set()
        steps.append('db close')

    async def close_session():
        if entered.is_set():
            assert stopped.is_set()
        steps.append('session close')

    bot = SimpleNamespace(session=SimpleNamespace(close=close_session))

    def create_bot(*args, **kwargs):
        if failure == 'bot':
            raise RuntimeError('invalid token')
        return bot

    class Dispatcher:
        def include_router(self, router):
            if failure == 'router':
                raise RuntimeError('router error')

        async def start_polling(self, actual_bot, **kwargs):
            assert actual_bot is bot and kwargs['close_bot_session'] is False
            await entered.wait()
            if failure == 'polling':
                raise RuntimeError('polling failure')
            if failure == 'cancel':
                raise asyncio.CancelledError

    async def background(actual_bot):
        assert actual_bot is bot
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
            steps.append('worker stopped')

    monkeypatch.setattr(main.db, 'connect', connect)
    monkeypatch.setattr(main.db, 'close', close_db)
    monkeypatch.setattr(main, 'Bot', create_bot)
    monkeypatch.setattr(main, 'Dispatcher', Dispatcher)
    monkeypatch.setattr(main.worker, 'run', background)
    if failure:
        with pytest.raises(asyncio.CancelledError if failure == 'cancel' else RuntimeError):
            await main.run()
    else:
        await main.run()
    assert 'db close' in steps
    if failure not in {'connect', 'bot'}:
        assert steps[-1] == 'session close'
    if failure in {None, 'polling', 'cancel'}:
        assert steps.index('worker stopped') < steps.index('db close') < steps.index('session close')


async def test_connect_schema_failure_closes_unpublished_pool(monkeypatch):
    connection = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError('invalid schema')))

    class Acquire:
        async def __aenter__(self):
            return connection

        async def __aexit__(self, *args):
            pass

    pool = SimpleNamespace(acquire=lambda: Acquire(), close=AsyncMock())
    monkeypatch.setattr(db.asyncpg, 'create_pool', AsyncMock(return_value=pool))
    with pytest.raises(RuntimeError, match='invalid schema'):
        await db.connect('synthetic')
    pool.close.assert_awaited_once()
