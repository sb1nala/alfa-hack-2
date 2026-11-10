"""Журнал событий для метрик. Роль C."""


def log_event(user_id: int, name: str, payload: dict | None = None) -> None:
    raise NotImplementedError
