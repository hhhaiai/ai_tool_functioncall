"""Request-boundary media validation for all supported downstream protocols."""
from __future__ import annotations

import base64
import copy
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from unittest import mock

import pytest

from src.gateway_errors import BadRequestError
from src.gateway_protocol import _convert_request_to_upstream
from src.gateway_proxy import NativeProxyClient


Json = dict[str, Any]
RequestFactory = Callable[[Json], tuple[str, Json, str]]


def _chat_request(part: Json, upstream_protocol: str = "openai_chat") -> tuple[str, Json, str]:
    return (
        "/v1/chat/completions",
        {"model": "media-model", "messages": [{"role": "user", "content": [part]}]},
        upstream_protocol,
    )


def _responses_request(
    part: Json,
    upstream_protocol: str = "openai_responses",
) -> tuple[str, Json, str]:
    return (
        "/v1/responses",
        {
            "model": "media-model",
            "input": [{"role": "user", "content": [part]}],
        },
        upstream_protocol,
    )


def _anthropic_request(
    part: Json,
    upstream_protocol: str = "anthropic_messages",
) -> tuple[str, Json, str]:
    return (
        "/v1/messages",
        {
            "model": "media-model",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": [part]}],
        },
        upstream_protocol,
    )


def _chat_image_url(url: str) -> Json:
    return {"type": "image_url", "image_url": {"url": url}}


def _responses_image_url(url: str) -> Json:
    return {"type": "input_image", "image_url": url}


def _anthropic_image_url(url: str) -> Json:
    return {"type": "image", "source": {"type": "url", "url": url}}


def _anthropic_base64(data: str, media_type: str = "image/png") -> Json:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


@pytest.mark.parametrize(
    ("request_factory", "part"),
    [
        (_chat_request, _chat_image_url("https://cdn.example.test/input.png")),
        (_responses_request, _responses_image_url("https://cdn.example.test/input.png")),
        (_anthropic_request, _anthropic_image_url("https://cdn.example.test/input.png")),
        (_chat_request, _chat_image_url("data:image/png;base64,YWJj")),
        (_responses_request, _responses_image_url("data:image/webp;base64,YWJj")),
        (_anthropic_request, _anthropic_base64("YWJj", "image/jpeg")),
    ],
    ids=[
        "chat-https",
        "responses-https",
        "anthropic-https",
        "chat-base64",
        "responses-base64",
        "anthropic-base64",
    ],
)
def test_valid_images_pass_matching_protocol_without_mutating_request(
    request_factory: RequestFactory,
    part: Json,
) -> None:
    path, body, upstream_protocol = request_factory(part)
    original = copy.deepcopy(body)

    upstream_path, converted = _convert_request_to_upstream(path, body, upstream_protocol)

    assert body == original
    assert converted == body
    assert upstream_path in {"/v1/chat/completions", "/v1/responses", "/v1/messages"}


@pytest.mark.parametrize(
    ("request_factory", "part", "upstream_protocol", "expected_media_type"),
    [
        (
            _chat_request,
            _chat_image_url("data:image/png;base64,YWJj"),
            "anthropic_messages",
            "'type': 'image'",
        ),
        (
            _responses_request,
            _responses_image_url("data:image/png;base64,YWJj"),
            "openai_chat",
            "'type': 'image_url'",
        ),
        (
            _anthropic_request,
            _anthropic_base64("YWJj"),
            "openai_responses",
            "'type': 'input_image'",
        ),
    ],
    ids=["chat-to-anthropic", "responses-to-chat", "anthropic-to-responses"],
)
def test_valid_images_survive_cross_protocol_conversion(
    request_factory: RequestFactory,
    part: Json,
    upstream_protocol: str,
    expected_media_type: str,
) -> None:
    path, body, _ = request_factory(part)

    _, converted = _convert_request_to_upstream(path, body, upstream_protocol)

    assert "YWJj" in str(converted)
    assert expected_media_type in str(converted)


def test_responses_text_and_image_survive_anthropic_conversion() -> None:
    body = {
        "model": "media-model",
        "input": [
            {
                "role": "user",
                "content": [
                    _responses_image_url("data:image/png;base64,YWJj"),
                    {"type": "input_text", "text": "describe"},
                ],
            }
        ],
    }

    upstream_path, converted = _convert_request_to_upstream(
        "/v1/responses",
        body,
        "anthropic_messages",
    )

    assert upstream_path == "/v1/messages"
    assert converted["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": "YWJj",
                    },
                },
                {"type": "text", "text": "describe"},
            ],
        }
    ]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/private.png",
        "http://10.0.0.8/private.png",
        "http://localhost/private.png",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/private.png",
        "http://0x7f.0.0.1/private.png",
        "http://0x7f.0x0.0x0.0x1/private.png",
        "http://①②⑦.⓪.⓪.①/private.png",
        "https://user:password@cdn.example.test/private.png",
    ],
    ids=[
        "file",
        "loopback",
        "private",
        "localhost",
        "metadata",
        "ipv6-loopback",
        "hex-ip",
        "component-hex-ip",
        "unicode-ip",
        "credentials",
    ],
)
@pytest.mark.parametrize(
    ("request_factory", "part_factory"),
    [
        (_chat_request, _chat_image_url),
        (_responses_request, _responses_image_url),
        (_anthropic_request, _anthropic_image_url),
    ],
    ids=["chat", "responses", "anthropic"],
)
def test_unsafe_image_urls_are_rejected_in_matching_protocol_passthrough(
    request_factory: RequestFactory,
    part_factory: Callable[[str], Json],
    url: str,
) -> None:
    path, body, upstream_protocol = request_factory(part_factory(url))

    with pytest.raises(BadRequestError) as caught:
        _convert_request_to_upstream(path, body, upstream_protocol)

    assert caught.value.status == 400
    assert caught.value.detail == {"failure_type": "invalid_media_input"}


@pytest.mark.parametrize(
    ("request_factory", "part"),
    [
        (_chat_request, _chat_image_url("data:image/png;base64,%%%")),
        (_responses_request, _responses_image_url("data:image/png;base64,%%%")),
        (_anthropic_request, _anthropic_base64("%%%")),
    ],
    ids=["chat", "responses", "anthropic"],
)
def test_invalid_base64_is_rejected(
    request_factory: RequestFactory,
    part: Json,
) -> None:
    path, body, upstream_protocol = request_factory(part)

    with pytest.raises(BadRequestError, match="base64") as caught:
        _convert_request_to_upstream(path, body, upstream_protocol)

    assert caught.value.detail == {"failure_type": "invalid_media_input"}


@pytest.mark.parametrize(
    ("request_factory", "part"),
    [
        (_chat_request, _chat_image_url("data:image/svg+xml;base64,YWJj")),
        (_responses_request, _responses_image_url("data:text/html;base64,YWJj")),
        (_anthropic_request, _anthropic_base64("YWJj", "application/octet-stream")),
    ],
    ids=["chat", "responses", "anthropic"],
)
def test_invalid_image_mime_is_rejected(
    request_factory: RequestFactory,
    part: Json,
) -> None:
    path, body, upstream_protocol = request_factory(part)

    with pytest.raises(BadRequestError, match="media type"):
        _convert_request_to_upstream(path, body, upstream_protocol)


@pytest.mark.parametrize(
    ("request_factory", "part"),
    [
        (_chat_request, _chat_image_url("data:image/png;base64,YWJjZA==")),
        (_responses_request, _responses_image_url("data:image/png;base64,YWJjZA==")),
        (_anthropic_request, _anthropic_base64("YWJjZA==")),
    ],
    ids=["chat", "responses", "anthropic"],
)
def test_decoded_media_size_limit_is_enforced(
    request_factory: RequestFactory,
    part: Json,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_MAX_MEDIA_INPUT_BYTES", "3")
    path, body, upstream_protocol = request_factory(part)

    with pytest.raises(BadRequestError) as caught:
        _convert_request_to_upstream(path, body, upstream_protocol)

    assert caught.value.detail == {"failure_type": "media_input_too_large"}


def test_oversized_base64_is_rejected_before_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import gateway_protocol

    monkeypatch.setenv("GATEWAY_MAX_MEDIA_INPUT_BYTES", "3")
    path, body, upstream_protocol = _chat_request(
        _chat_image_url("data:image/png;base64,YWJjZA==")
    )
    decode = mock.Mock(side_effect=AssertionError("oversized media must not be decoded"))
    monkeypatch.setattr(gateway_protocol.base64, "b64decode", decode)

    with pytest.raises(BadRequestError) as caught:
        _convert_request_to_upstream(path, body, upstream_protocol)

    assert caught.value.detail == {"failure_type": "media_input_too_large"}
    decode.assert_not_called()


def test_decoded_media_limit_is_cumulative_across_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_MAX_MEDIA_INPUT_BYTES", "5")
    body = {
        "model": "media-model",
        "messages": [
            {
                "role": "user",
                "content": [
                    _chat_image_url("data:image/png;base64,YWJj"),
                    _chat_image_url("data:image/png;base64,ZGVm"),
                ],
            }
        ],
    }

    with pytest.raises(BadRequestError) as caught:
        _convert_request_to_upstream("/v1/chat/completions", body, "openai_chat")

    assert caught.value.detail == {"failure_type": "media_input_too_large"}


@pytest.mark.parametrize("audio_format", ["mp3", "wav"])
def test_openai_chat_native_input_audio_accepts_mp3_and_wav(audio_format: str) -> None:
    part = {
        "type": "input_audio",
        "input_audio": {
            "data": base64.b64encode(b"audio").decode("ascii"),
            "format": audio_format,
        },
    }
    path, body, upstream_protocol = _chat_request(part)

    _, converted = _convert_request_to_upstream(path, body, upstream_protocol)

    assert converted["messages"][0]["content"][0] == part


@pytest.mark.parametrize("upstream_protocol", ["anthropic_messages", "openai_responses"])
def test_input_audio_fails_closed_when_target_protocol_has_no_transport(
    upstream_protocol: str,
) -> None:
    part = {
        "type": "input_audio",
        "input_audio": {"data": "YXVkaW8=", "format": "mp3"},
    }
    path, body, _ = _chat_request(part)

    with pytest.raises(BadRequestError) as caught:
        _convert_request_to_upstream(path, body, upstream_protocol)

    assert caught.value.detail == {"failure_type": "unsupported_media_transport"}


def test_responses_file_id_image_passes_only_matching_protocol() -> None:
    part = {"type": "input_image", "file_id": "file_abc123", "detail": "auto"}
    path, body, upstream_protocol = _responses_request(part)

    _, converted = _convert_request_to_upstream(path, body, upstream_protocol)

    assert converted == body

    for unsupported_protocol in ("openai_chat", "anthropic_messages"):
        with pytest.raises(BadRequestError) as caught:
            _convert_request_to_upstream(path, body, unsupported_protocol)
        assert caught.value.detail == {"failure_type": "unsupported_media_transport"}


@pytest.mark.parametrize(
    ("request_factory", "part"),
    [
        (_chat_request, {"type": "video_url", "video_url": {"url": "https://cdn.example.test/v.mp4"}}),
        (_responses_request, {"type": "input_video", "video_url": "https://cdn.example.test/v.mp4"}),
        (
            _anthropic_request,
            {"type": "video", "source": {"type": "url", "url": "https://cdn.example.test/v.mp4"}},
        ),
        (_responses_request, {"type": "input_audio", "input_audio": {"data": "YQ==", "format": "mp3"}}),
        (_anthropic_request, {"type": "audio", "source": {"type": "base64", "data": "YQ=="}}),
    ],
    ids=["chat-video", "responses-video", "anthropic-video", "responses-audio", "anthropic-audio"],
)
def test_undefined_audio_and_video_schemas_fail_closed(
    request_factory: RequestFactory,
    part: Json,
) -> None:
    path, body, upstream_protocol = request_factory(part)

    with pytest.raises(BadRequestError) as caught:
        _convert_request_to_upstream(path, body, upstream_protocol)

    assert caught.value.detail == {"failure_type": "unsupported_media_transport"}


def test_invalid_media_is_rejected_before_upstream_transport() -> None:
    client = NativeProxyClient(
        base_url="https://upstream.example.test",
        api_key="",
        model="media-model",
    )
    client._do_request = mock.Mock(return_value={"choices": []})  # type: ignore[method-assign]
    path, body, _ = _chat_request(_chat_image_url("file:///server/workspace/secret.png"))

    with pytest.raises(BadRequestError):
        client._forward_once(path, body)

    client._do_request.assert_not_called()


def test_media_validation_is_request_local_under_concurrent_users() -> None:
    def validate(index: int) -> str:
        url = (
            f"https://tenant-{index}.example.test/image.png"
            if index % 2 == 0
            else "http://127.0.0.1/private.png"
        )
        path, body, upstream_protocol = _chat_request(_chat_image_url(url))
        try:
            _convert_request_to_upstream(path, body, upstream_protocol)
        except BadRequestError:
            return "rejected"
        return "accepted"

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(validate, range(96)))

    assert outcomes == ["accepted" if index % 2 == 0 else "rejected" for index in range(96)]
