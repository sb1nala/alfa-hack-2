from app.assistant.core import reply
from app.models import Answer


async def test_reply_returns_answer_with_user_text():
    answer = await reply(1, "как открыть ИП")
    assert isinstance(answer, Answer)
    assert answer.kind == "answer"
    assert "как открыть ИП" in answer.text
