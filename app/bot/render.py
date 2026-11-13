"""Превращение Answer в текст сообщения Telegram (HTML). Роль B."""

from html import escape

from app.events import tracked_url
from app.models import Answer, Material


def _link(material: Material, campaign: str) -> str:
    """Ссылка на материал; у ссылок на платформу — метки, по которым виден переход из бота."""
    url = tracked_url(material.url, campaign, material.kind)
    return f'<a href="{escape(url, quote=True)}">{escape(material.title)}</a>'


def render_answer(answer: Answer) -> str:
    parts = [escape(answer.text)]
    if answer.sources:
        parts.append(
            "Источники:\n" + "\n".join(f"• {_link(m, 'answer')}" for m in answer.sources)
        )
    if answer.recommendations:
        lines = []
        for rec in answer.recommendations:
            mark = " (платный)" if rec.material.is_paid else ""
            link = _link(rec.material, "recommendation")
            lines.append(f"• {link}{mark} — {escape(rec.reason)}")
        parts.append("Что посмотреть дальше:\n" + "\n".join(lines))
    return "\n\n".join(parts)
