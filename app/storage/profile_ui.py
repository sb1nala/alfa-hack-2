"""Durable, owner-checked profile UI. Every transition uses one connection.

A single row per user serializes concurrent clicks; UUID + revision rejects
previous menus/steps. Onboarding status survives cancellation and expiration.
Telegram/metrics are deliberately outside the transaction.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from app.storage import db, profiles
from app.storage._common import ensure_user, validate_user_id

TTL = timedelta(minutes=30)
FIELDS = ('business_stage', 'industry', 'legal_form', 'tax_regime',
          'experience', 'current_goal', 'timezone')
QUESTIONS = ('business_stage', 'legal_form', 'timezone')
TEXT_LIMIT = 300


@dataclass(frozen=True)
class Session:
    user_id: int
    token: UUID
    revision: int
    mode: str
    field: str | None
    expects_text: bool
    onboarding_status: str
    expires_at: datetime


@dataclass(frozen=True)
class Result:
    status: str
    session: Session | None = None
    fields: tuple[str, ...] = ()
    reset: bool = False


def _session(row) -> Session | None:
    return Session(**dict(row)) if row else None


async def current(user_id: int) -> Session | None:
    validate_user_id(user_id)
    return _session(await db.pool().fetchrow(
        'SELECT * FROM profile_ui_sessions WHERE user_id=$1', user_id))


async def lookup(token: UUID) -> Session | None:
    return _session(await db.pool().fetchrow(
        'SELECT * FROM profile_ui_sessions WHERE token=$1', token))


async def open_menu(user_id: int, purpose: str, now: datetime) -> Session:
    """A command always replaces the previous session, preserving saved fields."""
    validate_user_id(user_id)
    if purpose not in {'start', 'profile'}:
        raise ValueError('Unknown menu')
    async with db.pool().acquire() as connection:
        async with connection.transaction():
            await ensure_user(connection, user_id)
            await connection.execute(
                "INSERT INTO profile_ui_sessions (user_id,token,mode,expires_at) "
                "VALUES ($1,$2,'idle',$3) ON CONFLICT (user_id) DO NOTHING",
                user_id, uuid4(), now + TTL)
            previous = await connection.fetchrow(
                'SELECT * FROM profile_ui_sessions WHERE user_id=$1 FOR UPDATE', user_id)
            status = previous['onboarding_status']
            mode = 'menu' if purpose == 'profile' else ('offer' if status == 'new' else 'home')
            if purpose == 'start' and status == 'new':
                status = 'offered'
            row = await connection.fetchrow(
                'UPDATE profile_ui_sessions SET token=$2,revision=0,mode=$3,field=NULL,'
                'expects_text=false,onboarding_status=$4,expires_at=$5 WHERE user_id=$1 RETURNING *',
                user_id, uuid4(), mode, status, now + TTL)
    return _session(row)


async def cancel(user_id: int, *, token: UUID | None = None, revision: int | None = None) -> None:
    """Invalidate menus/input without creating a user/profile or deleting data."""
    validate_user_id(user_id)
    await db.pool().execute(
        "UPDATE profile_ui_sessions SET token=$2,revision=0,mode='idle',field=NULL,"
        'expects_text=false WHERE user_id=$1 '
        'AND ($3::uuid IS NULL OR (token=$3 AND revision=$4))',
        user_id, uuid4(), token, revision)


async def transition(
    user_id: int, token: UUID, revision: int, action: str, now: datetime, *,
    field: str | None = None, value: str | None = None, text_input: bool = False,
) -> Result:
    """Compare-and-advance; field write/reset and UI state commit or roll back together."""
    validate_user_id(user_id)
    async with db.pool().acquire() as connection:
        async with connection.transaction():
            row = await connection.fetchrow(
                'SELECT * FROM profile_ui_sessions WHERE token=$1 FOR UPDATE', token)
            if row is None:
                return Result('stale')
            state = _session(row)
            if state.user_id != user_id:
                return Result('forbidden')
            if state.revision != revision or state.mode == 'idle':
                return Result('stale')
            if state.expires_at <= now:
                return Result('expired')
            mode, selected = state.mode, state.field
            expects_text, status = state.expects_text, state.onboarding_status
            changed: tuple[str, ...] = ()
            reset = False
            if action == 'begin' and mode in {'offer', 'home', 'menu'}:
                mode, selected, expects_text, status = 'onboarding', QUESTIONS[0], False, 'offered'
            elif action == 'field' and mode == 'menu' and field in FIELDS:
                mode, selected = 'edit', field
                expects_text = field in {'industry', 'current_goal'}
            elif action == 'other' and mode in {'edit', 'onboarding'} and selected == 'timezone':
                expects_text = True
            elif action == 'save' and mode in {'edit', 'onboarding'} and field == selected:
                if text_input != expects_text:
                    return Result('invalid')
                if value is not None and (not value.strip() or '\x00' in value or len(value) > TEXT_LIMIT):
                    raise ValueError('Недопустимая длина или пустое значение')
                await profiles.update(user_id, {field: value}, connection=connection)
                changed = (field,)
                if mode == 'edit':
                    mode, selected, expects_text = 'menu', None, False
                else:
                    mode, selected, expects_text, status = _next(selected, status)
            elif action == 'skip' and mode == 'onboarding':
                mode, selected, expects_text, status = _next(selected, status)
            elif action == 'all' and mode in {'offer', 'home', 'onboarding'}:
                mode, selected, expects_text, status = 'idle', None, False, 'skipped'
            elif action == 'delete' and mode == 'menu':
                mode, selected, expects_text = 'delete', None, False
            elif action == 'yes' and mode == 'delete':
                await profiles.reset(user_id, connection=connection)
                mode, selected, expects_text, status = 'menu', None, False, 'skipped'
                token, revision, reset = uuid4(), -1, True
            elif action == 'back' and mode in {'edit', 'delete'}:
                mode, selected, expects_text = 'menu', None, False
            elif action == 'close':
                mode, selected, expects_text = 'idle', None, False
            else:
                return Result('invalid')
            row = await connection.fetchrow(
                'UPDATE profile_ui_sessions SET token=$2,revision=$3,mode=$4,field=$5,'
                'expects_text=$6,onboarding_status=$7,expires_at=$8 WHERE user_id=$1 RETURNING *',
                user_id, token, revision + 1, mode, selected, expects_text, status, now + TTL)
            return Result('ok', _session(row), changed, reset)


def _next(field: str, status: str) -> tuple[str, str | None, bool, str]:
    index = QUESTIONS.index(field) + 1
    if index == len(QUESTIONS):
        return 'idle', None, False, 'completed'
    return 'onboarding', QUESTIONS[index], False, status
