"""Сведения о бизнесе из сообщения пользователя. Чистые функции, без сети и базы. Роль 3.

Разбор — по правилам, без LLM: сообщение никуда не уходит, результат предсказуем и проверяется
тестами. Форму бизнеса и налоговый режим берём только из утверждений о себе («я ИП на патенте»),
но не из вопросов и планов («чем ИП отличается от ООО?», «хочу перейти на патент»).
Отрасль и текущую задачу приводим к словарю из базы знаний, если он передан.
"""

import re
from dataclasses import dataclass, field

SERVICE_LABELS = frozenset({"без опыта", "с опытом", "new", "популярное"})


@dataclass(frozen=True)
class Vocabulary:
    """Названия отраслей и тегов-задач из базы знаний (Material.industries и Material.tags)."""

    industries: tuple[str, ...] = ()
    goals: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Concept:
    name: str  # запасное название, если в словаре базы ничего похожего нет
    pattern: re.Pattern[str]  # как об этом пишут пользователи
    hints: tuple[str, ...] = field(default=())  # части слов, по которым ищем название в словаре


def _concept(name: str, pattern: str, *hints: str) -> _Concept:
    return _Concept(name, re.compile(pattern), hints)


_SENTENCE = re.compile(r"[^.?!\n]+[.?!]*")
_INTENT = re.compile(
    r"\b(хочу|хотим|хотел\w*|планиру\w+|собира\w+|дума\w+|перейти|перехо\w+|выбрать|выбира\w+|"
    r"стоит ли|можно ли|могу ли|если|или|лучше|что такое|чем отлича\w+|нужно ли|надо ли"
    r"|как(?!\s+(?:самозанят|ип\b|ооо\b|индивидуальн))\w*)\b"
)

_IP = r"(?:ип|индивидуальн\w+ предпринимател\w+)"
_OOO = r"(?:ооо|общество с ограниченной ответственностью)"
_SELF = r"(?:самозанят\w+)"
_OWN = r"(?:сво[йеёя]\s+|мо[йеёя]\s+|наш[ае]?\s+|уже\s+|сейчас\s+|есть\s+|открыт[оа]?\s+)*"


def _stated(term: str) -> re.Pattern[str]:
    """Утверждение о себе: «я ИП», «у нас ООО», «открыл ИП», «ИП на патенте», «работаю как ИП»."""
    return re.compile(
        rf"\b(?:я|мы)\s+{_OWN}(?:работа\w+\s+(?:как\s+)?)?{term}\b"
        rf"|\bу\s+(?:меня|нас)\s+{_OWN}{term}\b"
        rf"|\b(?:открыл\w*|зарегистрировал\w*|оформил\w*)\s+{_OWN}{term}\b"
        rf"|\bработа\w+\s+как\s+{term}\b"
        rf"|^\s*{term}\s+(?:на|с|без)\s"
    )


_LEGAL_FORMS = (
    ("self_employed", _stated(_SELF)),
    ("ooo", _stated(_OOO)),
    ("ip", _stated(_IP)),
)

_USN = r"(?:усн|упрощ[её]нк\w+|упрощ[её]нн\w+(?:\s+систем\w+)?)"
_REGIME_CONTEXT = r"\b(?:на|применя\w+|плачу|платим)\s+"
_REGIMES = (
    ("usn_income_expense", re.compile(rf"{_REGIME_CONTEXT}{_USN}[^,;]{{0,25}}(?:15\s*%|15 процент\w*|доходы минус расходы)")),
    ("usn_income", re.compile(rf"{_REGIME_CONTEXT}{_USN}[^,;]{{0,25}}(?:6\s*%|6 процент\w*|доходы)")),
    ("ausn", re.compile(rf"{_REGIME_CONTEXT}(?:аусн|автоусн|автоматизированн\w+ упрощ[её]н\w+)")),
    ("psn", re.compile(rf"{_REGIME_CONTEXT}(?:патент\w*|псн)\b")),
    ("npd", re.compile(rf"{_REGIME_CONTEXT}(?:нпд|налог\w* на профессиональный доход)\b")),
    ("osno", re.compile(rf"{_REGIME_CONTEXT}(?:осно|осн|общ\w+ (?:систем\w+|режим\w+))\b|\bработа\w+ с ндс\b")),
)

# Режим берём, только если человек говорит о себе, а не о ком-то другом.
_ABOUT_ME = re.compile(rf"^\s*(?:{_IP}|{_OOO})\b|\b(?:я|мы|у меня|у нас|работа\w+|применя\w+|плачу|платим)\b")
_ABOUT_OTHERS = re.compile(
    r"\b(?:друг\w*|подруг\w*|знаком\w+|сосед\w*|брат\w*|сестр\w+|муж\w*|жен[аыеу]|клиент\w*|"
    r"партнер\w*|конкурент\w*|коллег\w*)\b"
)
# Отрасль берём, только если человек говорит о своём деле, а не спрашивает о теме вообще.
_MY_BUSINESS = re.compile(
    r"\b(?:я|мы|у меня|у нас|мо[йеяю]|сво[йеяю]|наш\w*|продаю|продаем|делаю|делаем|торгую|торгуем|"
    r"открыл\w*|открыть|открываю|держу|занимаюсь|занимаемся)\b"
)

_YEARS = r"(?:\d+|втор\w+|трет\w+|четверт\w+|пят\w+|несколько|много)\s+(?:год\w*|лет)"
_STAGES = (
    ("growing", re.compile(
        r"\bрасширя\w+|\bмасштабир\w+|\bоткрыва\w+\s+(?:втор\w+|трет\w+|нов\w+|ещ[её])"
        r"|\b(?:втор\w+|трет\w+)\s+(?:точк\w+|магазин\w*|филиал\w*|салон\w*)"
    )),
    ("idea", re.compile(
        r"\b(?:хочу|хотим|планирую|собираюсь|думаю|мечтаю)\s+(?:\w+\s+)?(?:открыть|начать|запустить|создать)"
        r"|\bесть идея\b|\bтолько (?:думаю|планирую)\b|\bбизнеса (?:пока|ещ[её]) нет\b"
    )),
    ("starting", re.compile(
        r"\b(?:только|недавно|только что)\s+(?:что\s+)?(?:открыл\w*|начал\w*|запустил\w*|зарегистрировал\w*|оформил\w*)"
        r"|\bначинающ\w+ предпринимател\w+|\bперв\w+ (?:месяц\w*|недел\w+) (?:работа\w+|в бизнесе)"
    )),
    ("running", re.compile(
        rf"\bуже\s+{_YEARS}|\b{_YEARS}\s+(?:в бизнесе|работа\w+|продаю|продаем|торгую)"
        rf"|\bработа\w+\s+(?:уже\s+)?{_YEARS}|\bдавно (?:работа\w+|в бизнесе|продаю|торгую)"
        r"|\bв штате\s+\d+|\b\d+\s+(?:сотрудник\w*|работник\w*)\b"
    )),
)
_BEGINNER = re.compile(r"\bнович\w+|\bс нуля\b|\bбез опыта\b|\bперв\w+ бизнес\w*|\bникогда не (?:вел|вела|занимал\w+)")
_EXPERIENCED = re.compile(r"\bопытн\w+ предпринимател\w+|\bне перв\w+ бизнес\w*|\bбольшой опыт\b")
_STAGE_EXPERIENCE = {"idea": "beginner", "starting": "beginner", "running": "experienced", "growing": "experienced"}

_TIMEZONES = (
    ("Europe/Kaliningrad", "калининград"),
    ("Europe/Moscow", "москв|санкт-петербург|петербург|питер|казан|нижн\\w+ новгород|краснодар|ростов|воронеж|сочи"),
    ("Europe/Samara", "самар|тольятти|ижевск"),
    ("Asia/Yekaterinburg", "екатеринбург|челябинск|перм|уф|тюмен"),
    ("Asia/Omsk", "омск"),
    ("Asia/Novosibirsk", "новосибирск"),
    ("Asia/Krasnoyarsk", "красноярск|кемеров|новокузнецк"),
    ("Asia/Irkutsk", "иркутск|улан-удэ"),
    ("Asia/Yakutsk", "якутск|чит|благовещенск"),
    ("Asia/Vladivostok", "владивосток|хабаровск"),
    ("Asia/Magadan", "магадан"),
    ("Asia/Sakhalin", "сахалин|южно-сахалинск"),
    ("Asia/Kamchatka", "камчатк|петропавловск"),
)
_CITY_CONTEXT = r"\b(?:в|во|из|живу в|город\w*)\s+(?:город\w*\s+)?"
# После основы названия допускаем только падежное окончание (до трёх букв): «в Чите», «из Уфы»,
# но не «в читальном зале» и не «в уфологии» — иначе напоминания уйдут не в тот часовой пояс.
_TIMEZONE_PATTERNS = tuple(
    (zone, re.compile(rf"{_CITY_CONTEXT}(?:{cities})\w{{0,3}}\b")) for zone, cities in _TIMEZONES
)

# Порядок важен: более точное понятие стоит раньше общего.
_INDUSTRIES = (
    _concept("Маркетплейсы", r"\bвайлдбер\w+|\bwildberries\b|\bна вб\b|\bозон\w*|\bozon\b|\bмаркетплейс\w*|\bяндекс маркет\w*",
             "маркетплейс", "онлайн-торг", "интернет-торг", "e-com"),
    _concept("Общепит", r"\bкофейн\w+|\bкафе\b|\bресторан\w*|\bстолов\w+|\bпекарн\w+|\bкондитерск\w+|\bобщепит\w*|\bпиццери\w+|\bбар\b|\bфудтрак\w*",
             "общепит", "питани", "ресторан", "кафе", "horeca"),
    _concept("Красота", r"\bпарикмахерск\w+|\bсалон\w* красоты|\bбарбершоп\w*|\bманикюр\w*|\bкосметолог\w*|\bбьюти\b|\bвизажист\w*",
             "красот", "бьюти"),
    _concept("Производство", r"\bпроизводств\w+|\bпроизводим\b|\bизготавлива\w+|\bизготовлени\w+|\bцех\w*\b|\bделаем мебель|\bшвейн\w+",
             "производ"),
    _concept("Строительство", r"\bстроительств\w+|\bстройк\w+|\bстроим\b|\bотделк\w+|\bремонт\w* квартир",
             "строител", "ремонт"),
    _concept("Перевозки", r"\bтакси\b|\bгрузоперевоз\w+|\bлогистик\w+|\bперевозк\w+|\bкурьерск\w+",
             "перевоз", "транспорт", "логист"),
    _concept("IT", r"\bразработк\w+ (?:сайт\w*|приложени\w+|программ\w*)|\bпрограммист\w*|\bайти\b|\bit\b|\bвеб-студи\w+",
             "it", "ит-", "разработ", "технолог"),
    _concept("Образование", r"\bрепетитор\w*|\bонлайн-школ\w+|\bобучаю\b|\bдетск\w+ (?:центр\w*|сад\w*)|\bязыков\w+ школ\w+",
             "образован", "обучен"),
    _concept("Медицина", r"\bклиник\w+|\bстоматолог\w+|\bмедицинск\w+|\bаптек\w+", "медицин", "здоров"),
    _concept("Туризм", r"\bотел\w+|\bгостиниц\w+|\bхостел\w*|\bтурагентств\w+|\bглэмпинг\w*", "туризм", "гостини"),
    _concept("Сельское хозяйство", r"\bферм\w+|\bсельск\w+ хозяйств\w+|\bтеплиц\w+", "сельск", "агро"),
    _concept("Услуги", r"\bдизайн\w*|\bфотограф\w*|\bклининг\w*|\bремонт\w*|\bуслуг\w+|\bконсультир\w+|\bавтосервис\w*|\bателье\b",
             "услуг"),
    _concept("Розничная торговля", r"\bмагазин\w*|\bторгую\b|\bторгуем\b|\bрозниц\w+|\bторгов\w+ точк\w+|\bпродаю\b|\bпродаем\b",
             "розни", "торговл", "ритейл", "магазин"),
)

_GOALS = (
    _concept("Поиск сотрудников", r"\b(?:нанять|найти|ищу|ищем|нужен|нужны|подобрать)\s+(?:\w+\s+){0,4}(?:сотрудник\w*|работник\w*|продавц\w*|персонал\w*|человека|людей)|\bна[йе]м\w* сотрудник\w+|\bпоиск сотрудник\w+",
             "сотрудник", "персонал", "найм", "наём"),
    _concept("Выход на маркетплейсы", r"\bвыйти на (?:маркетплейс\w*|вайлдбер\w+|озон\w*|wildberries|ozon)|\bначать (?:продавать|торговать) на (?:маркетплейс\w*|вайлдбер\w+|озон\w*)",
             "маркетплейс"),
    _concept("Поиск клиентов", r"\b(?:найти|искать|ищу|ищем|привлечь|привлекать|где брать|где искать)\s+(?:\w+\s+)?(?:клиент\w*|покупател\w+|заказчик\w*)|\bклиент\w* ищу|\bнет клиентов|\bмало клиентов",
             "клиент", "привлечен", "продвижен"),
    _concept("Увеличение продаж", r"\b(?:увеличить|поднять|повысить|нарастить)\s+(?:\w+\s+)?(?:продаж\w*|выручк\w+|оборот\w*)|\bмало продаж|\bпродажи (?:упали|падают|не растут)|\bрост\w* продаж",
             "продаж"),
    _concept("Продвижение и реклама", r"\bреклам\w+|\bпродвижени\w+|\bраскрутить|\bраскрутк\w+|\bпродвигать",
             "реклам", "продвижен", "маркетинг"),
    _concept("Выбор налогового режима", r"\b(?:как\w+|выбрать|выбор\w*)\s+(?:\w+\s+)?(?:систем\w+ налогообложени\w+|налогов\w+ режим\w*|налогообложени\w+)|\bкакой налог\w* выбрать",
             "налогов", "налогообложен"),
    _concept("Финансирование", r"\bкредит\w*|\bинвестор\w*|\bинвестици\w+|\bденьги на (?:развитие|открытие|старт)|\bсубсиди\w+|\bгрант\w*",
             "финансир", "кредит", "инвест"),
    _concept("Бухгалтерия и отчётность", r"\bбухгалтери\w+|\bотч[её]тност\w+|\bвести уч[её]т|\bсда(?:ть|вать) отч[её]т\w*",
             "бухгалтер", "отчетност", "отчётност", "учет", "учёт"),
    _concept("Запуск бизнеса", r"\bс чего (?:\w+\s+)?начать|\bоткрыть (?:сво\w+ |собственн\w+ )?(?:бизнес|дело|ип|ооо|\w+)|\bзапустить (?:сво\w+ )?(?:бизнес|дело|проект)|\bзарегистрировать (?:ип|ооо|бизнес)",
             "запуск", "старт", "регистрац", "открыт", "начал"),
)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _pick(concept: _Concept, labels: tuple[str, ...]) -> str:
    """Название из словаря базы, если там есть похожее; иначе своё запасное."""
    usable = [label for label in labels if _norm(label) not in SERVICE_LABELS]
    for hint in concept.hints:
        for label in usable:
            if hint in _norm(label):
                return label
    return concept.name


def _literal(text: str, labels: tuple[str, ...]) -> str | None:
    """Пользователь назвал отрасль или задачу теми же словами, что и в базе."""
    best = None
    for label in labels:
        name = _norm(label)
        if name in SERVICE_LABELS or len(name) < 4:
            continue
        if re.search(rf"\b{re.escape(name)}\b", text) and (best is None or len(name) > len(_norm(best))):
            best = label
    return best


def _first(pairs, text: str) -> str | None:
    return next((value for value, pattern in pairs if pattern.search(text)), None)


def _facts_about_self(text: str) -> str:
    """Части сообщения, где человек говорит о себе, а не спрашивает и не строит планы.

    Из предложения-вопроса берём только прямое «я ИП» / «у нас ООО»: «я самозанятый, могу ли…?».
    """
    kept = []
    for sentence in _SENTENCE.findall(text):
        question = sentence.rstrip().endswith("?")
        for clause in sentence.split(","):
            clause = clause.strip()
            if _INTENT.search(clause):
                continue
            if not question or _first(_LEGAL_FORMS, clause):
                kept.append(clause)
    return "\n".join(kept)


def extract(text: str, vocabulary: Vocabulary = Vocabulary()) -> dict[str, str]:
    """Что сообщение говорит о бизнесе: {поле UserProfile: значение}. Пусто — ничего не нашли."""
    text = _norm(text).replace("ё", "е")
    if not text:
        return {}
    found: dict[str, str] = {}

    for clause in _facts_about_self(text).split("\n"):
        if _ABOUT_OTHERS.search(clause):
            continue
        if "legal_form" not in found and (form := _first(_LEGAL_FORMS, clause)):
            found["legal_form"] = form
        if (
            "tax_regime" not in found
            and _ABOUT_ME.search(clause)
            and (regime := _first(_REGIMES, clause))
        ):
            found["tax_regime"] = regime
    if found.get("legal_form") == "self_employed":
        found.setdefault("tax_regime", "npd")
    if found.get("tax_regime") == "psn":
        found.setdefault("legal_form", "ip")  # патент доступен только ИП

    if stage := _first(_STAGES, text):
        found["business_stage"] = stage
    if _BEGINNER.search(text):
        found["experience"] = "beginner"
    elif _EXPERIENCED.search(text):
        found["experience"] = "experienced"
    elif stage:
        found["experience"] = _STAGE_EXPERIENCE[stage]

    if zone := _first(_TIMEZONE_PATTERNS, text):
        found["timezone"] = zone

    industry = _literal(text, vocabulary.industries) if _MY_BUSINESS.search(text) else None
    if industry is None and _MY_BUSINESS.search(text):
        concept = next((c for c in _INDUSTRIES if c.pattern.search(text)), None)
        industry = _pick(concept, vocabulary.industries) if concept else None
    if industry:
        found["industry"] = industry

    goal = _literal(text, vocabulary.goals)
    if goal is None:
        concept = next((c for c in _GOALS if c.pattern.search(text)), None)
        goal = _pick(concept, vocabulary.goals) if concept else None
    if goal:
        found["current_goal"] = goal
    return found
