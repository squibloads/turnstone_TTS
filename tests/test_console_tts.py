"""Console-side text-to-speech: ``POST /v1/api/tts`` + role aliases on ``/v1/api/models``.

Coordinator sessions live in the console process, not on a node, so the
browser's read-aloud button needs the console to serve the same TTS contract
the node does.  These pin that contract (status codes, bodies, headers) and the
``stt_default_alias`` / ``tts_default_alias`` advertisement the UI keys its
affordances on.  ``synthesize`` is mocked: no backend is ever called.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware, _FakeConfigStore
from turnstone.console.server import list_available_models, list_tts_styles, text_to_speech
from turnstone.core.audio import AudioBackendError, AudioUnavailableError, SpeechResult
from turnstone.core.model_backend_auth import BackendAuthUnavailableError


class _Registry:
    """Just the surface ``resolve_role_alias`` + ``effective_default`` read."""

    default = ""

    def __init__(self, models: dict[str, SimpleNamespace]) -> None:
        self._models = models

    def has_alias(self, alias: str) -> bool:
        return alias in self._models

    def get_config(self, alias: str) -> SimpleNamespace:
        return self._models[alias]

    def effective_default(self, config_store: Any) -> str:
        return ""


def _cfg(model: str, capabilities: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(provider="openai", model=model, capabilities=capabilities or {})


_SETTINGS = {
    "audio.tts_model_alias": "voice-tts",
    "audio.stt_model_alias": "voice-stt",
    "audio.tts_voice": "alloy",
}


def _registry() -> _Registry:
    return _Registry(
        {
            "voice-tts": _cfg("gpt-4o-mini-tts-"),
            "voice-stt": _cfg("whisper-1"),
            "chat": _cfg("gpt-4o"),
        }
    )


def _client(
    *,
    settings: dict[str, str] | None = None,
    registry: Any = "default",
    storage: Any = None,
) -> TestClient:
    app = Starlette(
        routes=[
            Route("/v1/api/tts", text_to_speech, methods=["POST"]),
            Route("/v1/api/tts/styles", list_tts_styles),
            Route("/v1/api/models", list_available_models),
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.config_store = _FakeConfigStore(dict(_SETTINGS if settings is None else settings))
    app.state.coord_registry = _registry() if registry == "default" else registry
    app.state.auth_storage = storage
    client = TestClient(app)
    client.headers.update({"X-Test-User": "userA", "X-Test-Perms": ""})
    return client


def _speech() -> SpeechResult:
    return SpeechResult(
        audio_bytes=b"RIFFfake", media_type="audio/mpeg", model_alias="voice-tts", model="m"
    )


def test_happy_path_returns_audio_bytes_and_alias_header() -> None:
    with patch("turnstone.core.audio.synthesize", return_value=_speech()) as synth:
        resp = _client().post("/v1/api/tts", json={"text": " hello "})
    assert resp.status_code == 200, resp.text
    assert resp.content == b"RIFFfake"
    assert resp.headers["content-type"] == "audio/mpeg"
    assert resp.headers["x-model-alias"] == "voice-tts"
    kwargs = synth.call_args.kwargs
    assert kwargs["alias"] == "voice-tts"
    assert kwargs["text"] == "hello"
    # audio.tts_voice supplies the voice when the body omits one.
    assert kwargs["voice"] == "alloy"
    assert kwargs["registry"] is not None


def test_body_voice_overrides_setting() -> None:
    with patch("turnstone.core.audio.synthesize", return_value=_speech()) as synth:
        _client().post("/v1/api/tts", json={"text": "hi", "voice": "nova"})
    assert synth.call_args.kwargs["voice"] == "nova"


def test_cancel_ref_is_aborted_after_synthesis() -> None:
    with patch("turnstone.core.audio.synthesize", return_value=_speech()) as synth:
        _client().post("/v1/api/tts", json={"text": "hi"})
    # abort() in ``finally`` is what releases an in-flight backend stream when
    # the client disconnects; it must also run on the success path.
    assert synth.call_args.kwargs["cancel_ref"].aborted


# ---------------------------------------------------------------------------
# speaking styles
# ---------------------------------------------------------------------------

_STYLES = json.dumps(
    [
        {"name": "Professional", "instruction": "Calm and even.", "in_menu": True},
        {"name": "Plain", "instruction": "", "in_menu": False},
    ]
)

_INSTRUCTION_CAPS = {
    "supports_speech_synthesis": True,
    "supports_speech_instructions": True,
}


def _instruction_registry() -> _Registry:
    return _Registry(
        {
            "voice-tts": _cfg("gpt-4o-mini-tts-", _INSTRUCTION_CAPS),
            "voice-stt": _cfg("whisper-1"),
        }
    )


def _style_settings(**overrides: Any) -> dict[str, Any]:
    return {**_SETTINGS, "audio.tts_styles": _STYLES, **overrides}


def _post_tts(client: TestClient, body: dict[str, Any]) -> tuple[Any, Any]:
    with patch("turnstone.core.audio.synthesize", return_value=_speech()) as synth:
        resp = client.post("/v1/api/tts", json=body)
    return resp, synth


def test_body_style_is_resolved_to_its_instruction() -> None:
    resp, synth = _post_tts(
        _client(settings=_style_settings(), registry=_instruction_registry()),
        {"text": "hi", "style": "professional"},  # match is case-insensitive
    )
    assert resp.status_code == 200, resp.text
    assert synth.call_args.kwargs["instructions"] == "Calm and even."


@pytest.mark.parametrize("body", [{"text": "hi"}, {"text": "hi", "style": None}])
def test_absent_or_null_style_uses_the_configured_default(body: dict[str, Any]) -> None:
    resp, synth = _post_tts(
        _client(
            settings=_style_settings(**{"audio.tts_style": "Professional"}),
            registry=_instruction_registry(),
        ),
        body,
    )
    assert resp.status_code == 200, resp.text
    assert synth.call_args.kwargs["instructions"] == "Calm and even."


@pytest.mark.parametrize("style", ["No Such Style", ""])
def test_unknown_or_empty_style_sends_no_instruction(style: str) -> None:
    # An explicit "" suppresses the configured default rather than selecting it;
    # an unknown name is never a 400 — the clip still plays, plainly.
    resp, synth = _post_tts(
        _client(
            settings=_style_settings(**{"audio.tts_style": "Professional"}),
            registry=_instruction_registry(),
        ),
        {"text": "hi", "style": style},
    )
    assert resp.status_code == 200, resp.text
    assert synth.call_args.kwargs["instructions"] == ""


def test_style_with_no_instruction_sends_nothing() -> None:
    resp, synth = _post_tts(
        _client(settings=_style_settings(), registry=_instruction_registry()),
        {"text": "hi", "style": "Plain"},
    )
    assert resp.status_code == 200, resp.text
    assert synth.call_args.kwargs["instructions"] == ""


def test_style_is_ignored_when_the_model_lacks_instruction_support() -> None:
    resp, synth = _post_tts(
        _client(settings=_style_settings()),  # default registry model has no flag
        {"text": "hi", "style": "Professional"},
    )
    assert resp.status_code == 200, resp.text
    assert synth.call_args.kwargs["instructions"] == ""


def test_styles_lists_names_and_menu_flags_without_instructions() -> None:
    resp = _client(settings=_style_settings()).get("/v1/api/tts/styles")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "styles": [
            {"name": "Professional", "in_menu": True},
            {"name": "Plain", "in_menu": False},
        ],
        "default": "",
        "instructions_supported": False,
    }


def test_styles_default_is_the_configured_setting() -> None:
    resp = _client(settings=_style_settings(**{"audio.tts_style": "Professional"})).get(
        "/v1/api/tts/styles"
    )
    assert resp.json()["default"] == "Professional"


def test_styles_reports_instruction_support_for_a_capable_tts_role() -> None:
    resp = _client(settings=_style_settings(), registry=_instruction_registry()).get(
        "/v1/api/tts/styles"
    )
    assert resp.json()["instructions_supported"] is True


def test_styles_report_no_instruction_support_without_a_tts_role() -> None:
    client = _client(settings=_style_settings(), registry=_instruction_registry())
    client.app.state.config_store = _FakeConfigStore(  # type: ignore[attr-defined]
        {k: v for k, v in _style_settings().items() if k != "audio.tts_model_alias"}
    )
    assert client.get("/v1/api/tts/styles").json()["instructions_supported"] is False


@pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "   "}, {"text": None}])
def test_missing_or_empty_text_is_400(body: dict[str, Any]) -> None:
    resp = _client().post("/v1/api/tts", json=body)
    assert resp.status_code == 400
    assert resp.json() == {"error": "text is required"}


def test_over_cap_text_is_400() -> None:
    resp = _client().post("/v1/api/tts", json={"text": "x" * 8001})
    assert resp.status_code == 400
    assert resp.json() == {"error": "text too long (cap 8000 chars)"}


def test_exactly_cap_text_is_accepted() -> None:
    with patch("turnstone.core.audio.synthesize", return_value=_speech()):
        resp = _client().post("/v1/api/tts", json={"text": "x" * 8000})
    assert resp.status_code == 200


def test_cap_follows_the_tts_max_chars_setting() -> None:
    # Same per-request setting the node reads, so one Settings change governs both.
    resp = _client(settings={**_SETTINGS, "audio.tts_max_chars": "10"}).post(
        "/v1/api/tts", json={"text": "x" * 11}
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "text too long (cap 10 chars)"}


def test_text_is_normalized_for_speech_like_the_node() -> None:
    # Coordinator replies must sound like interactive ones: the console applies
    # the node's speech cleanup before synthesis.
    settings = {**_SETTINGS, "audio.tts_normalize": True}
    with patch("turnstone.core.audio.synthesize", return_value=_speech()) as synth:
        _client(settings=settings).post(
            "/v1/api/tts", json={"text": "Written 1.000.000 in German format."}
        )
    assert synth.call_args.kwargs["text"] == "Written 1 dot 000 dot 000 in German format."


_NOT_CONFIGURED = "Text-to-speech is not configured. Assign a TTS model role in Models → Roles."


def test_no_tts_alias_is_503() -> None:
    resp = _client(settings={}).post("/v1/api/tts", json={"text": "hi"})
    assert resp.status_code == 503
    assert resp.json() == {"error": _NOT_CONFIGURED}


def test_alias_not_tts_eligible_is_503() -> None:
    resp = _client(settings={"audio.tts_model_alias": "chat"}).post(
        "/v1/api/tts", json={"text": "hi"}
    )
    assert resp.status_code == 503


def test_coord_registry_none_is_503() -> None:
    resp = _client(registry=None).post("/v1/api/tts", json={"text": "hi"})
    assert resp.status_code == 503
    assert resp.json() == {"error": _NOT_CONFIGURED}


def test_backend_auth_unavailable_is_masked_503() -> None:
    with patch("turnstone.core.audio.synthesize", side_effect=BackendAuthUnavailableError("tok")):
        resp = _client().post("/v1/api/tts", json={"text": "hi"})
    assert resp.status_code == 503
    assert resp.json() == {"error": "Model backend authentication unavailable"}


def test_audio_unavailable_is_503_with_its_message() -> None:
    with patch("turnstone.core.audio.synthesize", side_effect=AudioUnavailableError("no tts")):
        resp = _client().post("/v1/api/tts", json={"text": "hi"})
    assert resp.status_code == 503
    assert resp.json() == {"error": "no tts"}


def test_backend_error_is_static_502() -> None:
    with patch(
        "turnstone.core.audio.synthesize", side_effect=AudioBackendError("internal-host:9 boom")
    ):
        resp = _client().post("/v1/api/tts", json={"text": "hi"})
    assert resp.status_code == 502
    assert resp.json() == {"error": "Speech synthesis backend failed"}


# ---------------------------------------------------------------------------
# /v1/api/models role aliases
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path: Any, sqlite_backend_factory) -> Any:
    return sqlite_backend_factory(str(tmp_path / "console_tts.db"))


def test_models_reports_stt_and_tts_default_alias(storage: Any) -> None:
    body = _client(storage=storage).get("/v1/api/models").json()
    assert body["tts_default_alias"] == "voice-tts"
    assert body["stt_default_alias"] == "voice-stt"


def test_models_role_aliases_blank_when_not_eligible(storage: Any) -> None:
    settings = {"audio.tts_model_alias": "chat", "audio.stt_model_alias": "ghost"}
    body = _client(settings=settings, storage=storage).get("/v1/api/models").json()
    assert body["tts_default_alias"] == ""
    assert body["stt_default_alias"] == ""


def test_models_role_aliases_blank_without_coord_registry(storage: Any) -> None:
    body = _client(registry=None, storage=storage).get("/v1/api/models").json()
    assert body["tts_default_alias"] == ""
    assert body["stt_default_alias"] == ""


def test_models_role_aliases_blank_without_config_store(storage: Any) -> None:
    client = _client(storage=storage)
    client.app.state.config_store = None  # type: ignore[attr-defined]
    body = client.get("/v1/api/models").json()
    assert body["tts_default_alias"] == ""
    assert body["stt_default_alias"] == ""
