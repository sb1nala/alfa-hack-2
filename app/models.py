"""Общие типы. Менять только по согласованию с командой (docs/architecture.md)."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

MaterialKind = Literal[
    "article", "course", "lesson", "document", "glossary", "webinar", "podcast", "calculator"
]
Access = Literal["open", "subscription", "paid"]
AnswerKind = Literal["answer", "clarify", "no_answer"]
ReminderKind = Literal["custom", "learning", "tax"]


@dataclass
class UserProfile:
    user_id: int
    business_stage: str | None = None  # "idea" | "starting" | "running" | "growing"
    industry: str | None = None
    legal_form: str | None = None  # "ip" | "ooo" | "self_employed"
    tax_regime: str | None = None  # "usn_income" | "usn_income_expense" | "ausn" | "psn" | "npd" | "osno"
    experience: str | None = None  # "beginner" | "experienced"
    current_goal: str | None = None
    timezone: str = "Europe/Moscow"


@dataclass
class Material:
    id: str
    kind: MaterialKind
    access: Access  # всё, кроме "open", считаем платным
    title: str
    url: str
    summary: str
    topics: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    popularity: float = 0.0

    @property
    def is_paid(self) -> bool:
        return self.access != "open"


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
    kind: AnswerKind
    text: str
    sources: list[Material] = field(default_factory=list)
    recommendations: list[Recommendation] = field(default_factory=list)
    options: list[str] = field(default_factory=list)  # варианты для clarify


@dataclass
class Reminder:
    id: int
    user_id: int
    kind: ReminderKind
    text: str
    next_run_at: datetime  # UTC
    rrule: str | None  # None = разовое
    active: bool = True
