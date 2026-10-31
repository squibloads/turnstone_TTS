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
import time
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
from turnstone.core import web_helpers
from turnstone.core.audio import (
    AudioBackendError,
    AudioUnavailableError,
    TranscriptionResult,
)
from turnstone.core.auth import AuthResult
from turnstone.core.model_backend_auth import BackendAuthUnavailableError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

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
    middleware: list[Middleware] | None = None,
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
        middleware=[Middleware(_AuthMiddleware)] if middleware is None else middleware,
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


class _PureAuthMiddleware:
    """``_AuthMiddleware`` as plain ASGI, which is what the node's and console's stacks are.

    ``_AuthMiddleware`` is a BaseHTTPMiddleware, and that ends the response itself
    once the app inside it stops, failed or not, so it would hide the body's end.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope.setdefault("state", {})["auth_result"] = AuthResult(
                user_id="userA",
                scopes=frozenset({"read", "write", "approve"}),
                token_source="test",
                permissions=frozenset({_PERMS}),
            )
        await self.app(scope, receive, send)


class _RecordSends:
    """Keep every ASGI message the app sends, in order."""

    def __init__(self, app: ASGIApp, *, sent: list[Message]) -> None:
        self.app = app
        self.sent = sent

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def _send(message: Message) -> None:
            self.sent.append(message)
            await send(message)

        await self.app(scope, receive, _send)


def _stream_on_the_wire(deltas: Iterator[str]) -> tuple[list[Message], BaseException | None]:
    """POST a streamed dictation through plain ASGI; the messages sent and what escaped."""
    sent: list[Message] = []
    client = _client(
        middleware=[Middleware(_RecordSends, sent=sent), Middleware(_PureAuthMiddleware)]
    )
    escaped: BaseException | None = None
    with patch("turnstone.core.audio.transcribe_stream", return_value=deltas):
        try:
            client.post(_url(stream=True), files=_audio())
        except AudioBackendError as exc:
            escaped = exc
    return sent, escaped


def test_complete_stream_sends_its_last_chunk() -> None:
    # The control for the next test: this harness does see a body's end.
    sent, escaped = _stream_on_the_wire(iter(["hello ", "world"]))
    assert escaped is None
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200
    assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}


def test_stream_that_fails_mid_way_never_sends_its_last_chunk() -> None:
    """A backend failure after the first delta (1.9.2's request deadline cutting a
    stream that is still producing text, say) must fail the response.  Ending the
    chunked body normally hands the browser a 200 whose partial text it cannot
    tell from a whole transcript, and voice mode would send it as the user's turn.
    Without the last chunk the server drops the connection and the read fails.
    """

    def _deltas() -> Iterator[str]:
        yield "Please start three agents "
        raise AudioBackendError("HTTP request deadline exceeded")

    sent, escaped = _stream_on_the_wire(_deltas())
    assert isinstance(escaped, AudioBackendError)
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert b"".join(m.get("body", b"") for m in bodies) == b"Please start three agents "
    assert all(m.get("more_body", False) for m in bodies)


def test_stream_whose_caller_left_still_ends_quietly() -> None:
    """Only a backend failure fails the body: once the caller has gone (the
    request's abort ref is set), the stream's own error is the abort, not news."""

    def _call(**kwargs: Any) -> Iterator[str]:
        def _deltas() -> Iterator[str]:
            yield "partial "
            kwargs["cancel_ref"].abort()
            raise AudioBackendError("aborted")

        return _deltas()

    with patch("turnstone.core.audio.transcribe_stream", side_effect=_call):
        resp = _client().post(_url(stream=True), files=_audio())
    assert resp.status_code == 200
    assert resp.text == "partial "


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


# -- the console's dictation budget (1.9.2, #1509) ---------------------------
#
# Coordinator dictation gets one backend attempt within a budget from arrival,
# as a node's does, instead of the client default (600 s per attempt, retried),
# which held speech capacity through the retries.


def _transcriber(stream: bool) -> tuple[str, Any]:
    """The audio call a route makes, and a reply for its patch."""
    if stream:
        return "transcribe_stream", iter(["ok"])
    return "transcribe", _result()


@pytest.mark.parametrize("stream", [False, True])
def test_dictation_deadline_is_the_console_budget_from_arrival(stream: bool) -> None:
    name, reply = _transcriber(stream)
    client = _client()
    before = time.monotonic()
    with patch(f"turnstone.core.audio.{name}", return_value=reply) as call:
        resp = client.post(_url(stream=stream), files=_audio())
    after = time.monotonic()
    assert resp.status_code == 200, resp.text
    kwargs = call.call_args.kwargs
    budget = web_helpers.STT_REQUEST_TIMEOUT_S
    assert before + budget <= kwargs["deadline"] <= after + budget
    # The slot wait comes out of the console's admission budget, also from arrival.
    assert 0 <= kwargs["admission_wait_s"] <= web_helpers.STT_ADMISSION_WAIT_S


@pytest.mark.parametrize("stream", [False, True])
def test_dictation_credential_lookup_shares_the_call_deadline(stream: bool) -> None:
    # The patched call never mints, so the test calls the resolver it was given.
    name, reply = _transcriber(stream)
    with (
        patch(f"turnstone.core.audio.{name}", return_value=reply) as call,
        patch(
            "turnstone.core.model_backend_auth.resolve_model_backend_auth_token",
            return_value="minted",
        ) as resolve,
    ):
        resp = _client().post(_url(stream=stream), files=_audio())
        assert resp.status_code == 200, resp.text
        kwargs = call.call_args.kwargs
        assert kwargs["backend_auth_resolver"]("voice-stt", _cfg("whisper-1")) == "minted"
    assert resolve.call_args.kwargs["deadline"] == kwargs["deadline"]


def test_console_admission_wait_leaves_room_in_its_request_budget() -> None:
    # The console's twin of the node's test_dictation_wait_leaves_room_in_the_console_wait:
    # a slot wait as long as the whole budget would leave no time to transcribe.
    assert 0 < web_helpers.STT_ADMISSION_WAIT_S < web_helpers.STT_REQUEST_TIMEOUT_S


def test_console_dictation_budget_is_the_nodes() -> None:
    # One contract, so the coordinator mic behaves like a node's: the console
    # keeps the node's budget until it is given one of its own on purpose.
    import turnstone.server as srv

    assert web_helpers.STT_REQUEST_TIMEOUT_S == srv._STT_REQUEST_TIMEOUT_S


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
