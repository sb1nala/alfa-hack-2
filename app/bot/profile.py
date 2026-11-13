"""Russian onboarding/profile screens backed by durable single-use sessions."""

import logging
from datetime import datetime, timezone
from html import escape
from uuid import UUID

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app import events
from app.bot import onboarding
from app.models import UserProfile
from app.profile import service
from app.storage import profile_ui as storage

log = logging.getLogger(__name__)
LABELS = {
    'business_stage': 'Стадия бизнеса', 'industry': 'Отрасль', 'legal_form': 'Форма бизнеса',
    'tax_regime': 'Налоговый режим', 'experience': 'Опыт', 'current_goal': 'Текущая задача',
    'timezone': 'Часовой пояс',
}
CHOICES = {
    'business_stage': (('idea', 'Идея'), ('starting', 'Начинаю'),
                       ('running', 'Уже работаю'), ('growing', 'Развиваю')),
    'legal_form': (('ip', 'ИП'), ('ooo', 'ООО'), ('self_employed', 'Самозанятый')),
    'tax_regime': (('usn_income', 'УСН «доходы»'),
                   ('usn_income_expense', 'УСН «доходы минус расходы»'),
                   ('ausn', 'АУСН'), ('psn', 'Патент'), ('npd', 'НПД'), ('osno', 'ОСНО')),
    'experience': (('beginner', 'Начинающий'), ('experienced', 'С опытом')),
    'timezone': (('Europe/Moscow', 'Москва'), ('Asia/Yekaterinburg', 'Екатеринбург'),
                 ('Asia/Novosibirsk', 'Новосибирск'), ('Asia/Vladivostok', 'Владивосток')),
}
START_TEXT = (
    'Здравствуйте! Я помощник «Альфа-Курса» для предпринимателей. '
    'Отвечаю на вопросы о бизнесе и помогаю найти полезные материалы.\n\n'
    'Можно ответить на три коротких вопроса или сразу задать свой. '
    'Любой вопрос анкеты можно пропустить. Напоминания появятся только по вашей просьбе.'
)
DEFAULT_NOTE = 'Если часовой пояс не выбран, по умолчанию используется Europe/Moscow (Москва).'
ZONE_NOTE = ('Новый часовой пояс применяется к новым напоминаниям. '
             'Уже созданные сохранят своё расписание.')
DONE_TEXT = 'Теперь задайте вопрос о бизнесе. Анкета необязательна; профиль доступен через /profile.'
PRIVATE_TEXT = 'Профиль и настройка доступны в личном чате. Напишите мне /profile или /start там.'
FAILED_TEXT = 'Не удалось сохранить или прочитать данные. Повторите действие чуть позже.'
STATUS_TEXT = {
    'stale': 'Эта кнопка уже использована или меню сменилось. Откройте /profile или /start.',
    'forbidden': 'Эта кнопка принадлежит другому пользователю.',
    'expired': 'Время настройки истекло. Сохранённые поля остались. Откройте /profile или /start.',
    'invalid': 'Выберите доступную кнопку в текущем меню.',
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _short_html(value: str) -> str:
    # Budget the escaped UTF-16 representation too, including legacy/learned data.
    parts, used = [], 0
    for character in value:
        part = escape(character)
        size = len(part.encode('utf-16-le')) // 2
        if used + size > 350:
            return ''.join(parts) + '…'
        parts.append(part)
        used += size
    return ''.join(parts)


def render(profile: UserProfile) -> str:
    lines = ['<b>Ваш профиль</b>']
    for field, label in LABELS.items():
        value = getattr(profile, field)
        if field in CHOICES and field != 'timezone':
            value = dict(CHOICES[field]).get(value, 'Не указано')
        elif field == 'timezone':
            city = dict(CHOICES['timezone']).get(value)
            value = f'{city} ({value})' if city else value
        lines.append(f'{label}: {_short_html(value or "Не указано")}')
    return '\n'.join(lines)


def keyboard(state: storage.Session, rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f'pf:{state.token.hex}:{state.revision}:{action}')
        for label, action in row] for row in rows])


async def screen(
    message: Message, state: storage.Session, note: str = '', *, starter: bool = False,
) -> None:
    rows: list[list[tuple[str, str]]] = []
    if state.mode == 'offer':
        text = START_TEXT + '\n\n' + DEFAULT_NOTE
        rows = [[('Настроить профиль', 'begin')], [('Пропустить всю анкету', 'all')]]
    elif state.mode == 'home':
        text = 'С возвращением! Задайте вопрос о бизнесе или откройте /profile.'
        rows = [[('Повторить настройку', 'begin')], [('Закрыть', 'close')]]
    elif state.mode == 'menu':
        text = render(await service.get(state.user_id))
        rows = [[(f'Изменить: {label}', f'f{index}')]
                for index, label in enumerate(LABELS.values())]
        rows += [[('Повторить настройку', 'begin')], [('Удалить профиль', 'delete'), ('Закрыть', 'close')]]
    elif state.mode == 'delete':
        text = ('Удалить профиль? Сведения о бизнесе будут удалены. Напоминания, пользователь '
                'и история останутся. Для новых напоминаний снова будет использоваться '
                'Europe/Moscow (Москва). Это удаление только профиля.')
        rows = [[('Удалить', 'yes'), ('Отмена', 'back')]]
    elif state.mode in {'edit', 'onboarding'}:
        field = state.field
        prefix = (f'Вопрос {storage.QUESTIONS.index(field) + 1} из 3. '
                  if state.mode == 'onboarding' else '')
        text = prefix + LABELS[field] + '.'
        if state.expects_text:
            if field == 'timezone':
                text += '\nВведите IANA-зону, например Europe/Moscow или Asia/Vladivostok.'
            else:
                text += f'\nВведите значение поля «{LABELS[field]}» текстом (до {storage.TEXT_LIMIT} символов).'
            text += '\n/cancel — прекратить ввод без удаления сохранённых данных.'
        else:
            text += '\nВыберите вариант кнопкой. Можно задать обычный вопрос о бизнесе.'
            rows = [[(label, f's{index}')] for index, (_, label) in enumerate(CHOICES[field])]
            if field == 'timezone':
                rows.append([('Другая IANA-зона', 'other')])
            elif field == 'legal_form' and state.mode == 'onboarding':
                rows.append([('Пока не указана', 'skip')])
        if field == 'timezone':
            text += '\n' + DEFAULT_NOTE
        if state.mode == 'onboarding':
            rows += [[('Пропустить вопрос', 'skip')], [('Пропустить оставшуюся анкету', 'all')]]
        else:
            if field != 'timezone':
                rows.append([('Очистить поле', 'clear')])
            rows.append([('Отмена', 'back')])
    else:
        text = DONE_TEXT + '\n' + DEFAULT_NOTE
        if starter:
            try:
                pack = onboarding.starter_pack(await service.get(state.user_id))
                if pack:
                    text += '\n\n' + pack
            except Exception as error:
                log.warning('starter profile read failed: %s', type(error).__name__)
    if note:
        text = note + '\n\n' + text
    await message.answer(text, reply_markup=keyboard(state, rows) if rows else None)


async def metric(user_id: int, name: str, payload: dict | None = None) -> None:
    try:
        await events.log_event(user_id, name, payload)
    except Exception as error:
        log.warning('profile metric failed: %s', type(error).__name__)


async def open_screen(message: Message, purpose: str) -> None:
    if message.chat.type != 'private':
        await message.answer(PRIVATE_TEXT)
        return
    if message.from_user is None:
        return
    if purpose == 'start':
        await metric(message.from_user.id, events.BOT_STARTED)
    try:
        state = await storage.open_menu(message.from_user.id, purpose, utcnow())
        await screen(message, state)
    except Exception as error:
        log.warning('profile open failed: %s', type(error).__name__)
        text = START_TEXT + '\n\n' + DEFAULT_NOTE + '\n\n' if purpose == 'start' else ''
        await message.answer(text + FAILED_TEXT)


async def finish(result: storage.Result) -> str:
    state = result.session
    if result.reset:
        await metric(state.user_id, events.PROFILE_RESET)
        return 'Профиль удалён. Напоминания и история сохранены.'
    if result.fields:
        await metric(state.user_id, events.PROFILE_UPDATED, {'fields': list(result.fields)})
        return 'Сохранено.' + ('\n' + ZONE_NOTE if 'timezone' in result.fields else '')
    return ''


async def on_callback(callback: CallbackQuery) -> None:
    alert = STATUS_TEXT['stale']
    result = None
    try:
        if not isinstance(callback.message, Message) or callback.message.chat.type != 'private':
            alert = PRIVATE_TEXT
        else:
            _, key, version, action = (callback.data or '').split(':')
            token, revision = UUID(key), int(version)
            state = await storage.lookup(token)
            if state is None:
                alert = STATUS_TEXT['stale']
            elif state.user_id != callback.from_user.id:
                alert = STATUS_TEXT['forbidden']
            elif state.revision != revision or state.mode == 'idle':
                alert = STATUS_TEXT['stale']
            elif state.expires_at <= utcnow():
                alert = STATUS_TEXT['expired']
            else:
                field, value = state.field, None
                text_input = False
                if action.startswith('f') and action[1:].isdigit():
                    field = storage.FIELDS[int(action[1:])]
                    action = 'field'
                elif action.startswith('s') and action[1:].isdigit():
                    value = CHOICES[field][int(action[1:])][0]
                    action = 'save'
                elif action == 'clear' and field != 'timezone' and state.mode == 'edit':
                    action, text_input = 'save', state.expects_text
                result = await storage.transition(
                    callback.from_user.id, token, revision, action, utcnow(),
                    field=field, value=value, text_input=text_input)
                alert = 'Готово.' if result.status == 'ok' else STATUS_TEXT[result.status]
    except (ValueError, TypeError, KeyError, IndexError):
        alert = STATUS_TEXT['invalid']
    except Exception as error:
        log.warning('profile callback failed: %s', type(error).__name__)
        alert = FAILED_TEXT + ' Кнопку можно нажать ещё раз.'
    # Every callback is acknowledged, even malformed/foreign/expired buttons.
    await callback.answer(alert, show_alert=alert != 'Готово.')
    if result is not None and result.status == 'ok':
        note = await finish(result)
        try:
            await screen(callback.message, result.session, note,
                         starter=action in {'save', 'skip', 'all'})
        except Exception as error:
            log.warning('profile screen failed: %s', type(error).__name__)
            await callback.message.answer('Действие выполнено. Откройте /profile для просмотра.')


async def on_text(message: Message) -> bool:
    """Only explicit free input is consumed; button steps allow normal questions."""
    if message.chat.type != 'private' or message.from_user is None or not message.text:
        return False
    try:
        state = await storage.current(message.from_user.id)
        if state is None or state.mode not in {'edit', 'onboarding'}:
            return False
        if not state.expects_text:
            # A typed option is not a business question; keep the step unmodified.
            options = CHOICES.get(state.field, ())
            if message.text.casefold() in {s.casefold() for pair in options for s in pair}:
                await message.answer('Выберите этот вариант кнопкой или используйте /cancel.')
                return True
            return False
        if state.expires_at <= utcnow():
            # Do not cancel a newer session opened concurrently with this read.
            await storage.cancel(state.user_id, token=state.token, revision=state.revision)
            await message.answer(STATUS_TEXT['expired'])
            return True
        value = message.text.strip()
        if not value or len(value) > storage.TEXT_LIMIT or '\x00' in value:
            await message.answer(f'Введите непустое значение до {storage.TEXT_LIMIT} символов. /cancel — отмена.')
            return True
        result = await storage.transition(
            state.user_id, state.token, state.revision, 'save', utcnow(),
            field=state.field, value=value, text_input=True)
        if result.status != 'ok':
            await message.answer(STATUS_TEXT[result.status])
        else:
            note = await finish(result)
            try:
                await screen(message, result.session, note, starter=True)
            except Exception as error:
                log.warning('profile screen failed: %s', type(error).__name__)
                await message.answer('Действие выполнено. Откройте /profile для просмотра.')
    except ValueError:
        await message.answer('Некорректная IANA-зона. Например: Europe/Moscow. Повторите ввод или /cancel.')
    except Exception as error:
        log.warning('profile input failed: %s', type(error).__name__)
        await message.answer(FAILED_TEXT + ' Повторите ввод или /cancel.')
    return True
