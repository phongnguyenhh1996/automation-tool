"""Tests for Cloudinary raw JSON upload (retry + purge)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from cloudinary.exceptions import Error as CloudinaryError

import automation_tool.cloudinary_json as cj


@pytest.fixture(autouse=True)
def _reset_cloudinary_config() -> None:
    cj._configured = False
    yield
    cj._configured = False


def test_upload_retries_once_on_cloudinary_error() -> None:
    with patch.dict(
        os.environ,
        {
            "CLOUDINARY_CLOUD_NAME": "demo",
            "CLOUDINARY_API_KEY": "k",
            "CLOUDINARY_API_SECRET": "secret",
        },
    ), patch(
        "automation_tool.cloudinary_json.cloudinary.uploader.upload",
    ) as up:
        up.side_effect = [
            CloudinaryError("transient"),
            {"secure_url": "https://res.cloudinary.com/demo/raw/upload/v1/x.json"},
        ]
        url = cj.upload_json_bytes_for_responses(b"{}", "f.json")
    assert url == "https://res.cloudinary.com/demo/raw/upload/v1/x.json"
    assert up.call_count == 2


def test_upload_raises_after_second_cloudinary_error() -> None:
    with patch.dict(
        os.environ,
        {
            "CLOUDINARY_CLOUD_NAME": "demo",
            "CLOUDINARY_API_KEY": "k",
            "CLOUDINARY_API_SECRET": "secret",
        },
    ), patch(
        "automation_tool.cloudinary_json.cloudinary.uploader.upload",
    ) as up:
        up.side_effect = [CloudinaryError("a"), CloudinaryError("b")]
        with pytest.raises(CloudinaryError):
            cj.upload_json_bytes_for_responses(b"{}", "f.json")
    assert up.call_count == 2
