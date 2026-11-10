"""Превращение Answer в текст сообщения Telegram (HTML). Роль B."""

from html import escape

from app.models import Answer, Material


def _link(material: Material) -> str:
    return f'<a href="{escape(material.url, quote=True)}">{escape(material.title)}</a>'


def render_answer(answer: Answer) -> str:
    parts = [escape(answer.text)]
    if answer.sources:
        parts.append("Источники:\n" + "\n".join(f"• {_link(m)}" for m in answer.sources))
    if answer.recommendations:
        lines = []
        for rec in answer.recommendations:
            mark = " (платный)" if rec.material.is_paid else ""
            lines.append(f"• {_link(rec.material)}{mark} — {escape(rec.reason)}")
        parts.append("Что посмотреть дальше:\n" + "\n".join(lines))
    return "\n\n".join(parts)
