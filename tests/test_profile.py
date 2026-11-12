"""Профиль из сообщений: что достаём, чего не достаём, как сохраняем. Все фразы придуманы."""

import pytest

from app.models import Material, UserProfile
from app.profile import service
from app.profile.extract import Vocabulary, extract

VOCABULARY = Vocabulary(
    industries=("Рестораны и кафе", "Интернет-торговля", "Сфера услуг", "С опытом", "NEW"),
    goals=("Поиск сотрудников", "Привлечение клиентов", "Выбор налогового режима"),
)


# --- утверждения о себе ---

@pytest.mark.parametrize("text,expected", [
    ("я ип на упрощенке 6%, продаю одежду на вайлдберриз уже второй год", {
        "legal_form": "ip", "tax_regime": "usn_income", "business_stage": "running",
        "experience": "experienced", "industry": "Маркетплейсы",
    }),
    ("я самозанятая, делаю дизайн интерьеров, клиентов ищу сама", {
        "legal_form": "self_employed", "tax_regime": "npd", "industry": "Услуги",
        "current_goal": "Поиск клиентов",
    }),
    ("у нас ооо, делаем мебель на заказ, в штате 15 человек, работаем с ндс", {
        "legal_form": "ooo", "tax_regime": "osno", "business_stage": "running",
        "experience": "experienced", "industry": "Производство",
    }),
    ("ип на патенте, у меня парикмахерская во владивостоке", {
        "legal_form": "ip", "tax_regime": "psn", "industry": "Красота",
        "timezone": "Asia/Vladivostok",
    }),
    ("хочу открыть свою кофейню, пока работаю в найме, с чего вообще начать?", {
        "business_stage": "idea", "experience": "beginner", "industry": "Общепит",
        "current_goal": "Запуск бизнеса",
    }),
])
def test_extracts_profile_from_self_description(text, expected):
    assert extract(text) == expected


@pytest.mark.parametrize("text,form", [
    ("Я ИП", "ip"),
    ("у меня ООО", "ooo"),
    ("недавно открыл ИП", "ip"),
    ("мы зарегистрировали ооо в прошлом году", "ooo"),
    ("работаю как самозанятый", "self_employed"),
    ("я индивидуальный предприниматель", "ip"),
])
def test_legal_form_from_statement(text, form):
    assert extract(text)["legal_form"] == form


@pytest.mark.parametrize("text,regime", [
    ("я на усн доходы", "usn_income"),
    ("мы на упрощёнке 15%", "usn_income_expense"),
    ("применяю усн доходы минус расходы", "usn_income_expense"),
    ("я на аусн", "ausn"),
    ("работаю на патенте", "psn"),
    ("я на нпд", "npd"),
    ("у нас ооо на общей системе", "osno"),
])
def test_tax_regime_from_statement(text, regime):
    assert extract(text)["tax_regime"] == regime


def test_patent_implies_ip_and_self_employment_implies_npd():
    assert extract("работаю на патенте")["legal_form"] == "ip"
    assert extract("я самозанятый")["tax_regime"] == "npd"


# --- вопросы и планы профиль не меняют ---

@pytest.mark.parametrize("text", [
    "чем отличается ип от ооо, что лучше для небольшого магазина",
    "как открыть ип? что для этого нужно",
    "хочу перейти на патент",
    "можно ли самозанятому нанимать работников?",
    "что такое усн простыми словами",
    "стоит ли мне переходить на усн 15%?",
    "что выгоднее: ип на патенте или самозанятость?",
    "если я на патенте, нужна ли касса?",
])
def test_questions_and_plans_do_not_set_form_or_regime(text):
    found = extract(text)
    assert "legal_form" not in found and "tax_regime" not in found


def test_statement_inside_a_question_still_counts():
    found = extract("я самозанятый, могу ли я нанимать работников?")
    assert found["legal_form"] == "self_employed"


@pytest.mark.parametrize("text", [
    "мой друг ип на патенте",
    "у клиента ооо на общей системе",
    "я не ип, просто интересуюсь",
])
def test_facts_about_other_people_are_ignored(text):
    found = extract(text)
    assert "legal_form" not in found and "tax_regime" not in found


@pytest.mark.parametrize("text", [
    "есть ли у вас курс по маркетплейсам?",
    "что такое кафе с точки зрения налогов",
    "расскажите про маркетплейсы",
])
def test_topic_of_a_question_is_not_an_industry(text):
    assert "industry" not in extract(text)


@pytest.mark.parametrize("text", ["", "   ", "привет", "спасибо, понятно", "какая завтра погода"])
def test_small_talk_gives_nothing(text):
    assert extract(text) == {}


# --- стадия и опыт ---

@pytest.mark.parametrize("text,stage,experience", [
    ("планирую открыть магазин", "idea", "beginner"),
    ("только что зарегистрировал ип", "starting", "beginner"),
    ("работаю уже 5 лет", "running", "experienced"),
    ("открываю вторую точку", "growing", "experienced"),
])
def test_stage_and_experience(text, stage, experience):
    found = extract(text)
    assert (found["business_stage"], found["experience"]) == (stage, experience)


def test_explicit_beginner_wins_over_stage():
    assert extract("я новичок, но уже 2 года в бизнесе")["experience"] == "beginner"


# --- часовой пояс ---

@pytest.mark.parametrize("text,zone", [
    ("живу в новосибирске", "Asia/Novosibirsk"),
    ("я из екатеринбурга", "Asia/Yekaterinburg"),
    ("у меня кафе в казани", "Europe/Moscow"),
])
def test_timezone_from_city(text, zone):
    assert extract(text)["timezone"] == zone


def test_city_without_context_is_ignored():
    assert "timezone" not in extract("поставщик присылает товар через омск")


# --- словарь из базы знаний ---

def test_industry_and_goal_are_mapped_to_vocabulary():
    found = extract("у меня кофейня, нужно нанять бариста и ещё пару сотрудников", VOCABULARY)
    assert found["industry"] == "Рестораны и кафе"
    assert found["current_goal"] == "Поиск сотрудников"


def test_exact_vocabulary_name_in_message_is_used():
    assert extract("меня интересует выбор налогового режима", VOCABULARY)["current_goal"] == (
        "Выбор налогового режима"
    )


def test_service_labels_are_never_an_industry():
    assert "industry" not in extract("я с опытом, new проект", VOCABULARY)


def test_unknown_concept_falls_back_to_own_name():
    assert extract("у меня автосервис", Vocabulary(industries=("Рестораны и кафе",)))["industry"] == "Услуги"
    assert extract("нужен кредит на оборудование", VOCABULARY)["current_goal"] == "Финансирование"


def test_more_specific_industry_wins():
    assert extract("продаю посуду на озоне", VOCABULARY)["industry"] == "Интернет-торговля"


# --- сервис: сохранение ---

@pytest.fixture
def storage(monkeypatch):
    saved: dict[int, UserProfile] = {}
    calls: list[dict] = []

    async def get(user_id):
        return saved.get(user_id, UserProfile(user_id=user_id))

    async def update(user_id, changes):
        calls.append(changes)
        base = saved.get(user_id, UserProfile(user_id=user_id))
        saved[user_id] = UserProfile(**{**base.__dict__, **changes})
        return saved[user_id]

    async def reset(user_id):
        saved.pop(user_id, None)

    monkeypatch.setattr(service.storage, "get", get)
    monkeypatch.setattr(service.storage, "update", update)
    monkeypatch.setattr(service.storage, "reset", reset)
    service.set_vocabulary([])
    yield calls
    service.set_vocabulary([])


async def test_unknown_user_has_empty_profile(storage):
    assert await service.get(7) == UserProfile(user_id=7)


async def test_message_fills_profile_and_keeps_earlier_facts(storage):
    await service.update_from_message(7, "я ип на патенте")
    profile = await service.update_from_message(7, "у меня парикмахерская во владивостоке")
    assert profile.legal_form == "ip" and profile.tax_regime == "psn"
    assert profile.industry == "Красота" and profile.timezone == "Asia/Vladivostok"


async def test_message_without_facts_does_not_write(storage):
    await service.update_from_message(7, "я ип на патенте")
    profile = await service.update_from_message(7, "спасибо!")
    assert profile.legal_form == "ip"
    assert len(storage) == 1


async def test_repeating_known_fact_does_not_write(storage):
    await service.update_from_message(7, "я ип на патенте")
    await service.update_from_message(7, "я ип на патенте")
    assert len(storage) == 1


async def test_new_fact_replaces_old_one(storage):
    await service.update_from_message(7, "я самозанятый")
    profile = await service.update_from_message(7, "недавно открыл ип, теперь я на усн доходы")
    assert (profile.legal_form, profile.tax_regime) == ("ip", "usn_income")
    assert storage[-1] == {"legal_form": "ip", "tax_regime": "usn_income", "business_stage": "starting", "experience": "beginner"}


async def test_reset_forgets_profile(storage):
    await service.update_from_message(7, "я ип на патенте")
    await service.reset(7)
    assert await service.get(7) == UserProfile(user_id=7)


async def test_vocabulary_comes_from_materials(storage):
    cafe = Material(
        id="m1", kind="article", access="open", title="Статья", url="https://example.test/m1/",
        summary="", tags=["Поиск сотрудников"], industries=["Рестораны и кафе", "Популярное"],
    )
    service.set_vocabulary([cafe])
    profile = await service.update_from_message(7, "у меня кафе, ищу сотрудников")
    assert (profile.industry, profile.current_goal) == ("Рестораны и кафе", "Поиск сотрудников")
