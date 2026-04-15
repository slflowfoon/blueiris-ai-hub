import mute_bot


def test_handle_command_uses_global_caption_defaults(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        mute_bot,
        "get_mute_bot_settings",
        lambda: {
            "poll_interval_seconds": 5,
            "caption_default_minutes": 25,
            "enabled_caption_styles": ["witty"],
        },
    )
    monkeypatch.setattr(
        mute_bot,
        "set_caption_mode",
        lambda chat_id, style, minutes: captured.update(
            {"chat_id": chat_id, "style": style, "minutes": minutes}
        ),
    )
    monkeypatch.setattr(mute_bot, "send_message", lambda *_args, **_kwargs: None)

    mute_bot.handle_command("token", "chat-1", "thread-1", "/caption witty")

    assert captured == {"chat_id": "chat-1", "style": "witty", "minutes": 25}


def test_handle_command_rejects_disabled_styles(monkeypatch):
    sent = {}

    monkeypatch.setattr(
        mute_bot,
        "get_mute_bot_settings",
        lambda: {
            "poll_interval_seconds": 5,
            "caption_default_minutes": 25,
            "enabled_caption_styles": ["witty"],
        },
    )
    monkeypatch.setattr(
        mute_bot,
        "send_message",
        lambda _token, _chat_id, _thread_id, text, reply_markup=None: sent.update(
            {"text": text, "reply_markup": reply_markup}
        ),
    )

    mute_bot.handle_command("token", "chat-1", "thread-1", "/caption rude")

    assert "Unknown style" in sent["text"]
    assert "witty" in sent["text"]
