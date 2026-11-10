"""Превращение блоков содержимого в обычный текст и нарезка на фрагменты."""

import json
import re
from html import unescape

_BREAK = re.compile(r"<\s*(br\s*/?|/p|/div|/h[1-6]|/li|/tr|/ul|/ol|/blockquote)\s*>", re.I)
_LI = re.compile(r"<\s*li[^>]*>", re.I)
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\r\f\v ​]+")
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")

# Типы блоков *EmbedItems, в которых текст лежит в JSON-поле Params.
TIP, QUOTE, LIST, QA = 2, 3, 4, 13


def html_to_text(html: str | None) -> str:
    """HTML -> текст: один абзац на строку, без тегов."""
    if not html:
        return ""
    text = _BREAK.sub("\n", html)
    text = _LI.sub("• ", text)
    text = unescape(_TAG.sub("", text))
    lines = (_SPACES.sub(" ", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line and line != "•")


def block_text(block_type: int, html: str | None, params: str | None) -> str:
    """Текст одного блока. Картинки, видео, файлы и баннеры дают пустую строку."""
    if block_type == 0:
        return html_to_text(html)
    if block_type not in (TIP, QUOTE, LIST, QA) or not params:
        return ""
    try:
        data = json.loads(params)
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    parts: list[str] = []
    if block_type in (TIP, LIST, QA):
        parts.append(html_to_text(_as_str(data.get("title"))))
    if block_type == TIP:
        parts.append(html_to_text(_as_str(data.get("description"))))
    elif block_type in (QUOTE, QA):
        parts.append(html_to_text(_as_str(data.get("text"))))
    elif block_type == LIST:
        items = data.get("items")
        if isinstance(items, list):
            for item in items:
                item_text = html_to_text(_as_str(item)).replace("\n", " ")
                if item_text:
                    parts.append(f"• {item_text}")
    return "\n".join(part for part in parts if part)


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def split_chunks(text: str, max_chars: int = 1000) -> list[str]:
    """Нарезать текст на фрагменты не длиннее max_chars по границам абзацев и предложений."""
    pieces: list[str] = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            pieces.append(paragraph)
            continue
        for sentence in _SENTENCE_END.split(paragraph):
            while len(sentence) > max_chars:
                pieces.append(sentence[:max_chars])
                sentence = sentence[max_chars:]
            if sentence:
                pieces.append(sentence)

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 1 + len(piece) > max_chars:
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks
