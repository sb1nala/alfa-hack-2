from app.bot.handlers import router


def test_router_has_start_help_and_text_handlers():
    names = {handler.callback.__name__ for handler in router.message.handlers}
    assert names == {"on_start", "on_profile", "on_help", "on_cancel", "on_remind",
                     "on_taxes", "on_reminders", "on_unknown_command", "on_text"}
    order = [handler.callback.__name__ for handler in router.message.handlers]
    assert order.index("on_remind") < order.index("on_text")
