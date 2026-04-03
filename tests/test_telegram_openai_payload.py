"""Tests for OpenAI → Telegram JSON payload parsing."""

from automation_tool.telegram_bot import (
    TelegramChunk,
    build_t_me_c_message_url,
    parse_openai_telegram_payload,
    send_openai_output_to_telegram,
    split_output_chi_tiet_ngan_gon,
)


def test_parse_telegram_wrapper_single():
    raw = '{"telegram": {"text": "Hello", "parse_mode": "HTML"}}'
    got = parse_openai_telegram_payload(raw)
    assert got == [TelegramChunk(text="Hello", parse_mode="HTML", html_ready=False)]


def test_parse_shorthand_top_level():
    raw = '{"text": "Hi", "parse_mode": "HTML", "html_ready": true}'
    got = parse_openai_telegram_payload(raw)
    assert got == [TelegramChunk(text="Hi", parse_mode="HTML", html_ready=True)]


def test_parse_messages_list_with_parent_parse_mode():
    raw = """{"telegram": {"parse_mode": "HTML", "messages": [
        {"text": "A"},
        {"text": "B", "parse_mode": null}
    ]}}"""
    got = parse_openai_telegram_payload(raw)
    assert got == [
        TelegramChunk(text="A", parse_mode="HTML", html_ready=False),
        TelegramChunk(text="B", parse_mode=None, html_ready=False),
    ]


def test_parse_code_fence():
    raw = """```json
{"telegram": {"text": "**X**", "parse_mode": "HTML"}}
```"""
    got = parse_openai_telegram_payload(raw)
    assert got == [TelegramChunk(text="**X**", parse_mode="HTML", html_ready=False)]


def test_parse_multi_batch_separator():
    raw = """{"telegram": {"text": "One", "parse_mode": "HTML"}}

---

{"telegram": {"text": "Two", "parse_mode": "HTML"}}"""
    got = parse_openai_telegram_payload(raw)
    assert got == [
        TelegramChunk(text="One", parse_mode="HTML", html_ready=False),
        TelegramChunk(text="Two", parse_mode="HTML", html_ready=False),
    ]


def test_parse_legacy_freeform_returns_none():
    raw = "Just **markdown** analysis without JSON."
    assert parse_openai_telegram_payload(raw) is None


def test_body_alias_content():
    raw = '{"telegram": {"content": "C", "parse_mode": "HTML"}}'
    got = parse_openai_telegram_payload(raw)
    assert got == [TelegramChunk(text="C", parse_mode="HTML", html_ready=False)]


def test_send_openai_legacy_no_network(monkeypatch):
    calls: list[dict] = []

    def fake_send(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        "automation_tool.telegram_bot.send_message",
        fake_send,
    )
    send_openai_output_to_telegram(
        bot_token="t",
        chat_id="1",
        raw="plain text",
        default_parse_mode="HTML",
    )
    assert len(calls) == 1
    assert calls[0]["text"] == "plain text"
    assert calls[0]["parse_mode"] == "HTML"
    assert calls[0]["html_ready"] is False


def test_split_dual_markers():
    raw = """[OUTPUT_CHI_TIET]

Detail line one
Detail two

[OUTPUT_NGAN_GON]

Short one
Short two"""
    got = split_output_chi_tiet_ngan_gon(raw)
    assert got is not None
    a, b = got
    assert "Detail line one" in a
    assert "Short one" in b
    assert "[OUTPUT" not in a and "[OUTPUT" not in b


def test_split_dual_case_insensitive():
    raw = """[output_chi_tiet]
A
[Output_Ngan_Gon]
B"""
    got = split_output_chi_tiet_ngan_gon(raw)
    assert got == ("A", "B")


def test_split_missing_returns_none():
    assert split_output_chi_tiet_ngan_gon("[OUTPUT_CHI_TIET]\nonly") is None
    assert split_output_chi_tiet_ngan_gon("no markers") is None


def test_build_t_me_c_message_url():
    assert build_t_me_c_message_url("-1001234567890", 99) == "https://t.me/c/1234567890/99"
    assert build_t_me_c_message_url("1003884334166", 1) == "https://t.me/c/1003884334166/1"
    assert build_t_me_c_message_url("main", 1) is None


def test_send_openai_dual_summary_fallback_to_main(monkeypatch):
    calls: list[dict] = []

    def fake_send(**kwargs):
        calls.append(kwargs)
        cid = kwargs.get("chat_id")
        if cid == "-1001234567890":
            return 42
        if cid == "bad_summary":
            raise RuntimeError(
                'Telegram sendMessage failed: 400 {"ok":false,"description":"Bad Request: chat not found"}'
            )
        return None

    monkeypatch.setattr(
        "automation_tool.telegram_bot.send_message",
        fake_send,
    )
    raw = """[OUTPUT_CHI_TIET]
FULL
[OUTPUT_NGAN_GON]
SHORT"""
    send_openai_output_to_telegram(
        bot_token="t",
        chat_id="-1001234567890",
        raw=raw,
        default_parse_mode="HTML",
        summary_chat_id="bad_summary",
    )
    assert len(calls) == 3
    assert calls[0]["chat_id"] == "-1001234567890"
    assert calls[1]["chat_id"] == "bad_summary"
    assert calls[2]["chat_id"] == "-1001234567890"
    assert "https://t.me/c/1234567890/42" in calls[2]["text"]


def test_send_openai_dual_detail_channel(monkeypatch):
    """Chi tiết sang detail_chat_id; tóm tắt sang summary_chat_id."""
    calls: list[dict] = []

    def fake_send(**kwargs):
        calls.append(kwargs)
        cid = kwargs.get("chat_id")
        if cid == "-100detail":
            return 7
        return None

    monkeypatch.setattr(
        "automation_tool.telegram_bot.send_message",
        fake_send,
    )
    raw = """[OUTPUT_CHI_TIET]
FULL
[OUTPUT_NGAN_GON]
SHORT"""
    send_openai_output_to_telegram(
        bot_token="t",
        chat_id="main",
        raw=raw,
        default_parse_mode="HTML",
        summary_chat_id="sum",
        detail_chat_id="-100detail",
    )
    assert len(calls) == 2
    assert calls[0]["chat_id"] == "-100detail"
    assert calls[0]["text"] == "FULL"
    assert calls[1]["chat_id"] == "sum"
    assert "SHORT" in calls[1]["text"]


def test_send_openai_dual_routes(monkeypatch):
    calls: list[dict] = []

    def fake_send(**kwargs):
        calls.append(kwargs)
        if kwargs.get("chat_id") == "-1001234567890":
            return 42
        return None

    monkeypatch.setattr(
        "automation_tool.telegram_bot.send_message",
        fake_send,
    )
    raw = """[OUTPUT_CHI_TIET]
FULL
[OUTPUT_NGAN_GON]
SHORT"""
    send_openai_output_to_telegram(
        bot_token="t",
        chat_id="-1001234567890",
        raw=raw,
        default_parse_mode="HTML",
        summary_chat_id="sum",
    )
    assert len(calls) == 2
    assert calls[0]["chat_id"] == "-1001234567890"
    assert calls[0]["text"] == "FULL"
    assert calls[1]["chat_id"] == "sum"
    assert "SHORT" in calls[1]["text"]
    assert "https://t.me/c/1234567890/42" in calls[1]["text"]
    assert calls[1]["parse_mode"] == "HTML"
    assert calls[1]["html_ready"] is True


def test_send_openai_json_multiple(monkeypatch):
    calls: list[dict] = []

    def fake_send(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        "automation_tool.telegram_bot.send_message",
        fake_send,
    )
    raw = '{"telegram": {"messages": [{"text": "a"}, {"text": "b"}]}}'
    send_openai_output_to_telegram(
        bot_token="t",
        chat_id="1",
        raw=raw,
        default_parse_mode="HTML",
    )
    assert len(calls) == 2
    assert calls[0]["text"] == "a"
    assert calls[1]["text"] == "b"
