# Архитектура

Один Python-процесс в Docker: бот на long polling (публичный адрес не нужен), SQLite в volume, поиск по базе знаний внутри процесса, фоновый цикл рассылки напоминаний. Наружу ходим только в Telegram и в LLM API.

**Стек:** Python 3.12, aiogram 3, SQLite, поиск BM25 со стеммингом (эмбеддинги — под вопросом, см. [team-plan.md](team-plan.md)), чтение выгрузки через `pgdumplib`, LLM через OpenAI-совместимый клиент (`LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`), pytest, Docker Compose.

## Как это работает

```
Telegram ──> bot (aiogram) ──> assistant.reply()
                                 ├─ profile: что знаем о бизнесе, что нового в сообщении
                                 ├─ намерение: вопрос | напоминание | разговор
                                 ├─ kb.search(): фрагменты базы знаний
                                 ├─ recs.recommend(): материалы под профиль и вопрос
                                 └─ llm: ответ строго по фрагментам
цикл раз в 30 с ──> reminders.due() ──> отправка ──> пересчёт следующего срока
всё важное ──> events (для метрик)
```

**Ответ на вопрос.** Ищем фрагменты; LLM получает только их и возвращает один из трёх типов:

- `answer` — ответ по фрагментам + источники + следующий шаг;
- `clarify` — вопрос слишком общий, один уточняющий вопрос с кнопками-вариантами;
- `no_answer` — релевантного в базе нет: честно говорим об этом и предлагаем ближайшие темы.

Если лучший результат поиска ниже порога — `no_answer` без обращения к LLM (порог подбираем на тестовом наборе).

**Профиль.** Поля: стадия бизнеса, сфера, форма (ИП/ООО/самозанятый), налоговый режим, опыт, текущая задача, часовой пояс. Анкета не обязательна: на старте два-три вопроса кнопками с «Пропустить», остальное бот достаёт из сообщений. `/profile` — посмотреть, исправить, удалить. При пустом профиле бот всё равно полезен: отвечает на вопрос и даёт общую стартовую подборку.

**Рекомендации.** До трёх материалов: отбор по теме вопроса и тегу-задаче, затем порядок по совпадению с профилем (отрасль, опыт), редакторским связям и популярности. У каждого — одна строка «почему вам». Платный: польза в одном предложении и ссылка, без пересказа. Уже показанное не повторяем.

**Напоминания.** Три вида: своё («напомни в пятницу сверить кассу»), учебное («напоминай продолжать курс по вторникам»), налоговое (по режиму из профиля, из `tax_calendar.yaml`). Текст на естественном языке разбирает LLM → бот показывает, что понял → пользователь подтверждает кнопкой. Повторы храним как RRULE. `/reminders` — список, сменить частоту, пауза, удалить. Цикл читает сроки из SQLite, поэтому перезапуск ничего не теряет.

**Не раздражать.** Тихие часы 21:00–09:00 по времени пользователя (сообщение уезжает на утро), не больше двух сообщений по инициативе бота в день, кнопки «Отключить» и «Реже» под каждым напоминанием, никаких рассылок без подписки.

## Структура кода и договорённости между модулями

```
app/
  main.py            запуск бота и цикла напоминаний
  config.py          переменные окружения
  models.py          общие типы (ниже)
  llm/client.py      complete()
  kb/ingest.py       выгрузка -> материалы и фрагменты
  kb/search.py       search()
  assistant/core.py  reply()
  profile/service.py get(), update_from_message(), reset()
  recs/service.py    recommend()
  reminders/service.py   create(), list_for(), set_schedule(), pause(), delete(), due()
  reminders/parse.py     parse_request()
  reminders/tax_calendar.yaml
  storage/db.py      SQLite, миграция при старте
  events.py          log_event()
  bot/               хендлеры, клавиатуры, тексты
tests/
eval/                тестовые вопросы и профили (придуманные, не из выгрузки)
data/                выгрузка кейса — НЕ в git
```

Общие типы — их меняем только по согласованию в чате команды:

```python
@dataclass
class UserProfile:
    user_id: int
    business_stage: str | None = None   # "idea" | "starting" | "running" | "growing"
    industry: str | None = None
    legal_form: str | None = None       # "ip" | "ooo" | "self_employed"
    tax_regime: str | None = None       # "usn_income" | "usn_income_expense" | "ausn" | "psn" | "npd" | "osno"
    experience: str | None = None       # "beginner" | "experienced"
    current_goal: str | None = None
    timezone: str = "Europe/Moscow"

@dataclass
class Material:
    id: str
    kind: Literal["article", "course", "lesson", "document", "glossary", "webinar", "podcast", "calculator"]
    access: Literal["open", "subscription", "paid"]   # всё, кроме "open", считаем платным
    title: str
    url: str                            # полный адрес на kurs.alfabank.ru, см. [data.md](data.md)
    summary: str
    topics: list[str]                   # после чистки дублей
    tags: list[str]                     # задачи: «Выбор налогового режима» и т.п.
    industries: list[str]               # отрасли и метки «С опытом» / «Без опыта»
    popularity: float = 0.0             # из записей на курсы и оценок

@dataclass
class Chunk:
    material: Material
    text: str
    score: float

@dataclass
class Recommendation:
    material: Material
    reason: str

@dataclass
class Answer:
    kind: Literal["answer", "clarify", "no_answer"]
    text: str
    sources: list[Material]
    recommendations: list[Recommendation]
    options: list[str]                  # варианты для clarify, иначе пусто

@dataclass
class Reminder:
    id: int
    user_id: int
    kind: Literal["custom", "learning", "tax"]
    text: str
    next_run_at: datetime               # UTC
    rrule: str | None                   # None = разовое
    active: bool
```

Функции на границах модулей:

```python
# llm/client.py
async def complete(messages: list[dict], json_schema: dict | None = None) -> str | dict
# kb/search.py
def search(query: str, k: int = 6) -> list[Chunk]
# assistant/core.py
async def reply(user_id: int, text: str) -> Answer
# profile/service.py
def get(user_id: int) -> UserProfile
async def update_from_message(user_id: int, text: str) -> UserProfile
def reset(user_id: int) -> None
# recs/service.py
def recommend(profile: UserProfile, query: str, exclude_ids: set[str], k: int = 3) -> list[Recommendation]
# reminders/parse.py
async def parse_request(text: str, profile: UserProfile, now: datetime) -> Reminder | str   # str = что уточнить
# reminders/service.py
def create(reminder: Reminder) -> Reminder
def list_for(user_id: int) -> list[Reminder]
def set_schedule(reminder_id: int, rrule: str | None, next_run_at: datetime) -> Reminder
def pause(reminder_id: int, active: bool) -> None
def delete(reminder_id: int) -> None
def due(now: datetime) -> list[Reminder]
# events.py
def log_event(user_id: int, name: str, payload: dict | None = None) -> None
```

Поля `Material` сверены с выгрузкой, откуда что берётся — в [data.md](data.md).
