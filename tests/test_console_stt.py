"""Console-side speech-to-text for coordinator workstreams.

Coordinators run in the console process, so the coordinator composer's mic
button needs ``POST /v1/api/workstreams/{ws_id}/speech-to-text[/stream]`` served
here, against the coordinator model registry, exactly as a node serves it for
interactive sessions.  ``transcribe`` / ``transcribe_stream`` are mocked: no
backend is ever called.  The access check is the coordinator one -- a ws_id that
is not a coordinator must 404 rather than leak an interactive workstream.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware, _FakeConfigStore
from turnstone.console.server import speech_to_text, speech_to_text_stream
from turnstone.core.audio import (
    AudioBackendError,
    AudioUnavailableError,
    TranscriptionResult,
)
from turnstone.core.model_backend_auth import BackendAuthUnavailableError

if TYPE_CHECKING:
    from collections.abc import Iterator

COORD_ID = "abc123"
INTERACTIVE_ID = "def456"
_PERMS = "admin.coordinator"


class _Registry:
    default = ""

    def __init__(self, models: dict[str, SimpleNamespace]) -> None:
        self._models = models

    def has_alias(self, alias: str) -> bool:
        return alias in self._models

    def get_config(self, alias: str) -> SimpleNamespace:
        return self._models[alias]

    def effective_default(self, config_store: Any) -> str:
        return ""


def _cfg(model: str) -> SimpleNamespace:
    return SimpleNamespace(provider="openai", model=model, capabilities={})


_SETTINGS = {"audio.stt_model_alias": "voice-stt"}


def _registry() -> _Registry:
    return _Registry({"voice-stt": _cfg("whisper-1"), "chat": _cfg("gpt-4o")})


def _storage() -> MagicMock:
    """Storage holding one persisted-but-unloaded interactive row (wrong kind)."""
    storage = MagicMock()
    rows = {INTERACTIVE_ID: {"ws_id": INTERACTIVE_ID, "kind": "interactive", "state": "idle"}}
    storage.get_workstream.side_effect = lambda ws_id: rows.get(ws_id)
    return storage


# The dictation pools the clients below create, shut down after each test.
_pools: list[ThreadPoolExecutor] = []


@pytest.fixture(autouse=True)
def _shutdown_pools() -> Iterator[None]:
    yield
    while _pools:
        _pools.pop().shutdown(wait=True, cancel_futures=True)


def _client(
    *,
    settings: dict[str, str] | None = None,
    registry: Any = "default",
    coord_mgr: Any = "default",
) -> TestClient:
    app = Starlette(
        routes=[
            Route("/v1/api/workstreams/{ws_id}/speech-to-text", speech_to_text, methods=["POST"]),
            Route(
                "/v1/api/workstreams/{ws_id}/speech-to-text/stream",
                speech_to_text_stream,
                methods=["POST"],
            ),
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.config_store = _FakeConfigStore(dict(_SETTINGS if settings is None else settings))
    app.state.coord_registry = _registry() if registry == "default" else registry
    if coord_mgr == "default":
        mgr = MagicMock()
        mgr.get.side_effect = lambda ws_id: (
            SimpleNamespace(user_id="owner", project_id="") if ws_id == COORD_ID else None
        )
        coord_mgr = mgr
    app.state.coord_mgr = coord_mgr
    app.state.auth_storage = _storage()
    # Dictation runs on the console's own threads (web_helpers.run_dictation).
    app.state.stt = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stt")
    _pools.append(app.state.stt)
    client = TestClient(app)
    client.headers.update({"X-Test-User": "userA", "X-Test-Perms": _PERMS})
    return client


def _audio(data: bytes = b"RIFFfake") -> dict[str, Any]:
    return {"audio": ("speech.webm", data, "audio/webm")}


def _result(text: str = "hello from speech") -> TranscriptionResult:
    return TranscriptionResult(transcript=text, model_alias="voice-stt", model="whisper-1")


def _url(ws_id: str = COORD_ID, *, stream: bool = False) -> str:
    return f"/v1/api/workstreams/{ws_id}/speech-to-text" + ("/stream" if stream else "")


# -- happy paths -------------------------------------------------------------


def test_json_happy_path_returns_transcript() -> None:
    with patch("turnstone.core.audio.transcribe", return_value=_result()) as tr:
        resp = _client().post(_url(), files=_audio())
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "status": "ok",
        "transcript": "hello from speech",
        "model_alias": "voice-stt",
    }
    kwargs = tr.call_args.kwargs
    assert kwargs["alias"] == "voice-stt"
    assert kwargs["data"] == b"RIFFfake"
    assert kwargs["filename"] == "speech.webm"
    assert kwargs["registry"] is not None
    # abort() in ``finally`` releases an in-flight backend stream on disconnect.
    assert kwargs["cancel_ref"].aborted


def test_stream_happy_path_returns_text_deltas() -> None:
    with patch("turnstone.core.audio.transcribe_stream", return_value=iter(["hello ", "world"])):
        resp = _client().post(_url(stream=True), files=_audio())
    assert resp.status_code == 200, resp.text
    assert resp.text == "hello world"
    assert resp.headers["content-type"].startswith("text/plain")


def test_stream_aborts_upstream_on_exit() -> None:
    with patch("turnstone.core.audio.transcribe_stream", return_value=iter(["a", "b"])) as stream:
        _client().post(_url(stream=True), files=_audio())
    assert stream.call_args.kwargs["cancel_ref"].aborted


def test_stream_response_aborts_even_when_cancelled_before_body(monkeypatch) -> None:
    from starlette.responses import StreamingResponse

    from turnstone.core.deadline import StreamAbortRef
    from turnstone.core.web_helpers import AbortOnExitStreamingResponse

    handle = MagicMock()
    abort_ref = StreamAbortRef()
    abort_ref.append(handle)

    async def cancel_before_body(self, scope, receive, send):
        raise asyncio.CancelledError

    monkeypatch.setattr(StreamingResponse, "__call__", cancel_before_body)
    response = AbortOnExitStreamingResponse([], abort_ref=abort_ref)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(response({}, MagicMock(), MagicMock()))
    assert abort_ref.aborted
    handle.close.assert_called()


# -- access: coordinators only -----------------------------------------------


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("ws_id", ["nope99", INTERACTIVE_ID])
def test_unknown_or_non_coordinator_ws_is_404(ws_id: str, stream: bool) -> None:
    with (
        patch("turnstone.core.audio.transcribe") as tr,
        patch("turnstone.core.audio.transcribe_stream") as trs,
    ):
        resp = _client().post(_url(ws_id, stream=stream), files=_audio())
    assert resp.status_code == 404
    assert resp.json() == {"error": "coordinator not found"}
    tr.assert_not_called()
    trs.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
def test_coordinator_subsystem_down_is_503(stream: bool) -> None:
    resp = _client(coord_mgr=None).post(_url(stream=stream), files=_audio())
    assert resp.status_code == 503
    assert "Coordinator subsystem not initialized" in resp.json()["error"]


@pytest.mark.parametrize("stream", [False, True])
def test_missing_admin_coordinator_permission_is_403(stream: bool) -> None:
    client = _client()
    client.headers.update({"X-Test-Perms": "", "X-Test-User": "userA"})
    with patch("turnstone.core.audio.transcribe") as tr:
        resp = client.post(_url(stream=stream), files=_audio())
    assert resp.status_code == 403
    tr.assert_not_called()


# -- upload validation -------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True])
def test_empty_upload_is_400(stream: bool) -> None:
    resp = _client().post(_url(stream=stream), files=_audio(b""))
    assert resp.status_code == 400
    assert resp.json() == {"error": "Empty audio upload"}


@pytest.mark.parametrize("stream", [False, True])
def test_over_cap_upload_is_rejected(stream: bool) -> None:
    with (
        patch("turnstone.core.web_helpers.STT_UPLOAD_CAP", 10000),
        patch("turnstone.core.audio.transcribe") as tr,
    ):
        resp = _client().post(_url(stream=stream), files=_audio(b"x" * 10001))
    assert resp.status_code == 413
    tr.assert_not_called()


@pytest.mark.parametrize("stream", [False, True])
def test_exactly_cap_upload_is_accepted(stream: bool) -> None:
    with (
        patch("turnstone.core.web_helpers.STT_UPLOAD_CAP", 10000),
        patch("turnstone.core.audio.transcribe", return_value=_result()),
        patch("turnstone.core.audio.transcribe_stream", return_value=iter(["ok"])),
    ):
        resp = _client().post(_url(stream=stream), files=_audio(b"x" * 10000))
    assert resp.status_code == 200


# -- role resolution and error masking ---------------------------------------

_NOT_CONFIGURED = "Speech-to-text is not configured. Assign an STT model role in Models → Roles."


@pytest.mark.parametrize("stream", [False, True])
def test_no_stt_alias_is_503(stream: bool) -> None:
    resp = _client(settings={}).post(_url(stream=stream), files=_audio())
    assert resp.status_code == 503
    assert resp.json() == {"error": _NOT_CONFIGURED}


@pytest.mark.parametrize("stream", [False, True])
def test_coord_registry_none_is_503(stream: bool) -> None:
    resp = _client(registry=None).post(_url(stream=stream), files=_audio())
    assert resp.status_code == 503
    assert resp.json() == {"error": _NOT_CONFIGURED}


def test_silence_is_422() -> None:
    with patch("turnstone.core.audio.transcribe", return_value=_result("")):
        resp = _client().post(_url(), files=_audio())
    assert resp.status_code == 422
    assert resp.json() == {"error": "No speech detected"}


@pytest.mark.parametrize("stream", [False, True])
def test_backend_auth_unavailable_is_masked_503(stream: bool) -> None:
    name = "transcribe_stream" if stream else "transcribe"
    with patch(f"turnstone.core.audio.{name}", side_effect=BackendAuthUnavailableError("tok")):
        resp = _client().post(_url(stream=stream), files=_audio())
    assert resp.status_code == 503
    assert resp.json() == {"error": "Model backend authentication unavailable"}


@pytest.mark.parametrize("stream", [False, True])
def test_audio_unavailable_is_503_with_its_message(stream: bool) -> None:
    name = "transcribe_stream" if stream else "transcribe"
    with patch(f"turnstone.core.audio.{name}", side_effect=AudioUnavailableError("no stt")):
        resp = _client().post(_url(stream=stream), files=_audio())
    assert resp.status_code == 503
    assert resp.json() == {"error": "no stt"}


@pytest.mark.parametrize("stream", [False, True])
def test_backend_error_is_static_502(stream: bool) -> None:
    name = "transcribe_stream" if stream else "transcribe"
    with patch(
        f"turnstone.core.audio.{name}", side_effect=AudioBackendError("internal-host:9 boom")
    ):
        resp = _client().post(_url(stream=stream), files=_audio())
    assert resp.status_code == 502
    assert resp.json() == {"error": "Speech transcription backend failed"}


def test_stt_prompt_setting_is_forwarded() -> None:
    settings = {**_SETTINGS, "audio.stt_prompt": "  GRT, Hodgdon  "}
    with patch("turnstone.core.audio.transcribe", return_value=_result()) as tr:
        _client(settings=settings).post(_url(), files=_audio())
    assert tr.call_args.kwargs["prompt"] == "GRT, Hodgdon"


# -- voice dictionary --------------------------------------------------------


def _dict_entry(term: str, hears: list[str]) -> dict[str, Any]:
    return {"term": term, "respelling": "spoken", "also_hears": hears}


def _dictionary(*entries: dict[str, Any]) -> str:
    return json.dumps(list(entries))


def _settings(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**_SETTINGS, **(overrides or {})}


_DICT = _dictionary(_dict_entry("BAA", ["bay a"]), _dict_entry("nginx", ["engine x"]))


def test_dictionary_terms_are_appended_to_the_stt_prompt() -> None:
    settings = _settings(
        {
            "audio.stt_prompt": "  GRT, Hodgdon  ",
            "audio.voice_dictionary": _DICT,
            "audio.stt_dictionary_hint": True,
        }
    )
    with patch("turnstone.core.audio.transcribe", return_value=_result()) as tr:
        _client(settings=settings).post(_url(), files=_audio())
    assert tr.call_args.kwargs["prompt"] == "GRT, Hodgdon Vocabulary: BAA, nginx."


def test_dictionary_hint_off_leaves_the_operator_prompt_alone() -> None:
    settings = _settings(
        {
            "audio.stt_prompt": "GRT, Hodgdon",
            "audio.voice_dictionary": _DICT,
            "audio.stt_dictionary_hint": False,
        }
    )
    with patch("turnstone.core.audio.transcribe", return_value=_result()) as tr:
        _client(settings=settings).post(_url(), files=_audio())
    assert tr.call_args.kwargs["prompt"] == "GRT, Hodgdon"


def test_transcript_phrases_are_corrected() -> None:
    settings = _settings({"audio.voice_dictionary": _DICT, "audio.stt_dictionary_correct": True})
    with patch("turnstone.core.audio.transcribe", return_value=_result("the bay a was signed")):
        resp = _client(settings=settings).post(_url(), files=_audio())
    assert resp.status_code == 200, resp.text
    assert resp.json()["transcript"] == "the BAA was signed"


def test_transcript_correction_off_returns_the_raw_text() -> None:
    settings = _settings({"audio.voice_dictionary": _DICT, "audio.stt_dictionary_correct": False})
    with patch("turnstone.core.audio.transcribe", return_value=_result("the bay a was signed")):
        resp = _client(settings=settings).post(_url(), files=_audio())
    assert resp.json()["transcript"] == "the bay a was signed"


def test_correction_runs_when_the_hint_is_off() -> None:
    settings = _settings(
        {
            "audio.voice_dictionary": _DICT,
            "audio.stt_dictionary_hint": False,
            "audio.stt_dictionary_correct": True,
        }
    )
    with patch("turnstone.core.audio.transcribe", return_value=_result("the bay a was signed")):
        resp = _client(settings=settings).post(_url(), files=_audio())
    assert resp.json()["transcript"] == "the BAA was signed"


def test_malformed_dictionary_is_ignored() -> None:
    settings = _settings(
        {
            "audio.stt_prompt": "GRT",
            "audio.voice_dictionary": "not json",
            "audio.stt_dictionary_hint": True,
            "audio.stt_dictionary_correct": True,
        }
    )
    with patch(
        "turnstone.core.audio.transcribe", return_value=_result("the bay a was signed")
    ) as tr:
        resp = _client(settings=settings).post(_url(), files=_audio())
    assert tr.call_args.kwargs["prompt"] == "GRT"
    assert resp.json()["transcript"] == "the bay a was signed"


def test_stream_prompt_includes_dictionary_terms() -> None:
    settings = _settings({"audio.voice_dictionary": _DICT, "audio.stt_dictionary_hint": True})
    with patch("turnstone.core.audio.transcribe_stream", return_value=iter(["ok"])) as stream:
        resp = _client(settings=settings).post(_url(stream=True), files=_audio())
    assert resp.status_code == 200, resp.text
    assert stream.call_args.kwargs["prompt"] == "Vocabulary: BAA, nginx."


def test_stream_deltas_are_forwarded_unrewritten() -> None:
    # The stream route has no final transcript event: raw deltas go straight
    # into the composer, so dictionary correction applies to the JSON route only.
    settings = _settings({"audio.voice_dictionary": _DICT, "audio.stt_dictionary_correct": True})
    with patch(
        "turnstone.core.audio.transcribe_stream",
        return_value=iter(["the bay ", "a was signed"]),
    ):
        resp = _client(settings=settings).post(_url(stream=True), files=_audio())
    assert resp.text == "the bay a was signed"


def test_stt_helpers_without_a_config_store_are_inert() -> None:
    from turnstone.core.voice_config import parse_dictionary
    from turnstone.core.web_helpers import correct_stt_transcript, stt_dictionary, stt_prompt

    entries = parse_dictionary(_DICT)
    assert stt_dictionary(None) == []
    assert stt_prompt(None, entries) == ""
    assert correct_stt_transcript(None, entries, "the bay a was signed") == ("the bay a was signed")
