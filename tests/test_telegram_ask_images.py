from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from automation_tool import telegram_bot as tg
from automation_tool.openai_prompt_flow import run_text_followup_responses
from automation_tool.telegram_listen import (
    _collect_ask_image_file_ids,
    _command_text_from_envelope,
    _handle_telegram_ask_followup,
)


def test_command_text_from_envelope_prefers_text() -> None:
    env = {"text": "/ask resp_1 hello", "caption": "/ask resp_2 ignored"}
    assert _command_text_from_envelope(env) == "/ask resp_1 hello"


def test_command_text_from_envelope_uses_caption() -> None:
    env = {"caption": "/ask-high resp_1 phân tích chart"}
    assert _command_text_from_envelope(env) == "/ask-high resp_1 phân tích chart"


def test_image_file_ids_from_photo_uses_largest_size() -> None:
    env = {
        "photo": [
            {"file_id": "small", "width": 90},
            {"file_id": "large", "width": 1280},
        ]
    }
    assert tg.image_file_ids_from_message(env) == ["large"]


def test_image_file_ids_from_image_document() -> None:
    env = {
        "document": {
            "file_id": "doc_img",
            "mime_type": "image/png",
            "file_name": "chart.png",
        }
    }
    assert tg.image_file_ids_from_message(env) == ["doc_img"]


def test_image_file_ids_ignores_non_image_document() -> None:
    env = {
        "document": {
            "file_id": "doc_pdf",
            "mime_type": "application/pdf",
            "file_name": "notes.pdf",
        }
    }
    assert tg.image_file_ids_from_message(env) == []


def test_collect_ask_image_file_ids_from_reply() -> None:
    env = {
        "text": "/ask resp_1 mô tả ảnh reply",
        "reply_to_message": {
            "photo": [{"file_id": "reply_photo", "width": 800}],
        },
    }
    assert _collect_ask_image_file_ids(env) == ["reply_photo"]


def test_collect_ask_image_file_ids_prefers_current_message() -> None:
    env = {
        "caption": "/ask-high resp_1 chart đính kèm",
        "photo": [{"file_id": "current_photo", "width": 800}],
        "reply_to_message": {
            "photo": [{"file_id": "reply_photo", "width": 800}],
        },
    }
    assert _collect_ask_image_file_ids(env) == ["current_photo"]


def test_download_telegram_images_writes_files(tmp_path: Path, monkeypatch) -> None:
    def fake_get_file_path(**kwargs):
        assert kwargs["file_id"] == "fid_1"
        return "photos/file_1.jpg"

    def fake_download(**kwargs):
        kwargs["dest"].write_bytes(b"\xff\xd8\xfffake-jpeg")
        return kwargs["dest"]

    monkeypatch.setattr(tg, "telegram_get_file_path", fake_get_file_path)
    monkeypatch.setattr(tg, "download_telegram_file", fake_download)

    paths = tg.download_telegram_images(
        bot_token="token",
        file_ids=["fid_1"],
        dest_dir=tmp_path,
    )
    assert len(paths) == 1
    assert paths[0].name == "telegram_1.jpg"
    assert paths[0].read_bytes() == b"\xff\xd8\xfffake-jpeg"


def test_run_text_followup_responses_with_images(tmp_path: Path, monkeypatch) -> None:
    img = tmp_path / "chart.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")

    captured: dict = {}

    class FakeResponse:
        id = "resp_new"
        output_text = "done"

    class FakeResponses:
        def create(self, **kwargs):
            captured.update(kwargs)
            return FakeResponse()

    class FakeClient:
        responses = FakeResponses()

    monkeypatch.setattr(
        "automation_tool.openai_prompt_flow.OpenAI",
        lambda api_key: FakeClient(),
    )

    out, new_id = run_text_followup_responses(
        api_key="key",
        user_text="[RETROSPECTIVE_ANALYSIS]\nphân tích",
        previous_response_id="resp_old",
        vector_store_ids=[],
        store=True,
        include=[],
        model="gpt-5.4",
        image_paths=[img],
    )
    assert out == "done"
    assert new_id == "resp_new"
    inp = captured["input"]
    assert inp[-1]["type"] == "message"
    content = inp[-1]["content"]
    assert content[0]["type"] == "input_text"
    assert any(part.get("type") == "input_image" for part in content)


def test_handle_telegram_ask_followup_downloads_images(monkeypatch, tmp_path: Path) -> None:
    sent: list[str] = []
    downloaded: list[list[str]] = []
    followup_kwargs: dict = {}

    def fake_download(**kwargs):
        downloaded.append(list(kwargs["file_ids"]))
        p = Path(kwargs["dest_dir"]) / "telegram_1.jpg"
        p.write_bytes(b"jpeg")
        return [p]

    def fake_followup(**kwargs):
        followup_kwargs.update(kwargs)
        return ("answer", "resp_new")

    def fake_send_output(**kwargs):
        sent.append(kwargs["raw"])

    monkeypatch.setattr(
        "automation_tool.telegram_listen.download_telegram_images",
        fake_download,
    )
    monkeypatch.setattr(
        "automation_tool.telegram_listen.run_text_followup_responses",
        fake_followup,
    )
    monkeypatch.setattr(
        "automation_tool.telegram_listen.send_openai_output_to_telegram",
        fake_send_output,
    )

    settings = SimpleNamespace(
        openai_api_key="key",
        openai_vector_store_ids=["vs_should_not_be_used"],
        openai_responses_store=True,
        openai_responses_include=[],
        telegram_bot_token="token",
        telegram_parse_mode=None,
    )
    env = {
        "caption": "/ask-high resp_old mô tả chart",
        "photo": [{"file_id": "photo_1", "width": 1024}],
    }

    _handle_telegram_ask_followup(
        settings=settings,
        listen_chat_id="789",
        reply_to_message_id=100,
        message_thread_id=None,
        message_env=env,
        args_text="resp_old mô tả chart",
        model="gpt-5.4",
        cmd_label="/ask-high",
    )

    assert downloaded == [["photo_1"]]
    assert len(followup_kwargs["image_paths"]) == 1
    assert followup_kwargs["model"] == "gpt-5.4"
    assert followup_kwargs["vector_store_ids"] == []
    assert sent[0].startswith("(openai_response_id=resp_new)")
