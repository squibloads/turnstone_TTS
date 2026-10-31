"""Starlette web-request helpers shared across HTTP servers."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from functools import cached_property
from typing import TYPE_CHECKING, Any

from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from starlette.staticfiles import StaticFiles

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Sequence

    from starlette.middleware import Middleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.types import Receive, Scope, Send

    from turnstone.core.storage import WorkstreamLeaseHeldError, WorkstreamLeaseLostError
    from turnstone.core.voice_config import DictionaryEntry


def latin1_safe_filename(name: str, *, fallback: str = "attachment") -> str:
    """A ``Content-Disposition`` ``filename`` value that is safe on the wire.

    The value is emitted as ``filename="<this>"``, and three independent
    hazards bite filenames derived from user uploads or fetched-page titles:

    * Starlette encodes header values as latin-1 and raises on anything
      outside it, so a CJK / em-dash name would 500 the serving route.
    * The HTTP server layer rejects control characters in a header value even
      when they are latin-1 encodable (h11 rejects NUL / CR / LF / FF / VT;
      httptools is stricter still), so a stray control byte 500s there.
    * ``"`` and ``\\`` are the quoted-string metacharacters — ``\\`` is the
      RFC 6266 quoted-pair escape — so either would break out of or corrupt
      the value (a trailing ``\\`` escapes the closing quote).

    Drop every non-printable character (covers CR / LF / TAB / NUL / DEL, the
    C0 / C1 control ranges, and zero-width / bidi format chars) and both
    quoted-string metacharacters, then fold any surviving non-ASCII codepoint
    to ``?``.  The result is pure printable ASCII with no ``"`` or ``\\`` —
    latin-1 clean, control-free, and safely quotable.  ``fallback`` is run
    through the same cleaning (so a caller can't reintroduce the crash via an
    unsafe fallback), backed by a safe constant if even that is empty — the
    return is always wire-safe and never ``filename=""``.
    """

    def _clean(s: str) -> str:
        kept = "".join(c for c in s if c.isprintable() and c not in '"\\')
        return kept.encode("ascii", errors="replace").decode("ascii")

    return _clean(name) or _clean(fallback) or "download"


def skill_summary_rows(storage: Any) -> list[dict[str, Any]]:
    """Build the public picker payload for ``/v1/api/skills``.

    Shared between ``turnstone/server.py`` (standalone node) and
    ``turnstone/console/server.py`` (console-managed cluster), both of
    which expose ``/v1/api/skills`` to user-facing UI.  Bodies were
    character-identical before #571 added the ``hidden_from_menu``
    filter — extraction here keeps the two surfaces from drifting on
    every future spec-uplift field (#572's ``argument_hint`` for
    autocomplete is the next likely caller).

    Filters:
    * ``enabled=False`` rows are dropped.
    * ``hidden_from_menu=True`` rows are dropped (SKILL.md spec
      ``user-invocable: false`` — model still sees the skill via the
      ``skills`` tool, but the user picker hides it).

    Filtering happens in Python rather than SQL to match the
    pre-existing ``enabled`` pattern; pushing to SQL would be a
    separate change to ``list_prompt_templates``.
    """
    rows = storage.list_prompt_templates()
    skills: list[dict[str, Any]] = []
    for r in rows:
        if not r.get("enabled", True):
            continue
        if r.get("hidden_from_menu"):
            continue
        tags: list[str] = []
        with contextlib.suppress(ValueError, TypeError, RecursionError):
            tags = json.loads(r.get("tags", "[]"))
        skills.append(
            {
                "name": r["name"],
                "category": r.get("category", ""),
                "description": r.get("description", ""),
                "tags": tags,
                "origin": r.get("origin", "manual"),
                "author": r.get("author", ""),
                "version": r.get("version", "1.0.0"),
            }
        )
    return skills


async def read_json_or_400(request: Request) -> dict[str, Any] | JSONResponse:
    """Parse a JSON request body, returning a 400 response on failure.

    Callers should check ``isinstance(result, JSONResponse)`` and return
    it early when the parse fails::

        body = await read_json_or_400(request)
        if isinstance(body, JSONResponse):
            return body
    """
    from starlette.responses import JSONResponse as _JSONResponse

    try:
        raw: Any = await request.json()
        if not isinstance(raw, dict):
            # Valid JSON, wrong shape (list/string/number at top level):
            # without this check the declared dict type is a lie and every
            # caller's first ``body.get`` raises into a 500.
            return _JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)
        body: dict[str, Any] = raw
        return body
    except (ValueError, json.JSONDecodeError, RecursionError):
        return _JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    except Exception:
        import structlog

        structlog.get_logger(__name__).warning("read_json_or_400.unexpected", exc_info=True)
        return _JSONResponse({"error": "Failed to read request body"}, status_code=500)


def lease_refusal_response(
    exc: WorkstreamLeaseHeldError | WorkstreamLeaseLostError,
) -> JSONResponse:
    """The 409 ``workstream_lease_held`` answer to an owner-lease refusal.

    The body names the holder's node when it is known, so a client can follow it.
    A loss carries no holder (the refusing process only knows it no longer owns
    the workstream), so it shares the held shape with an empty
    ``holder_node_id``, and the router finds the owner.
    """
    from starlette.responses import JSONResponse as _JSONResponse

    from turnstone.core.storage import WorkstreamLeaseHeldError as _Held

    held = exc if isinstance(exc, _Held) else _Held(exc.ws_id)
    return _JSONResponse(held.as_dict(), status_code=_Held.status_code)


def with_filename(filename: str, message: str) -> str:
    """``message`` about one file of several, led by that file's name.

    A create can carry several files and stops at the first refused one, so
    the error says which.  The name is the uploader's own, shortened and on
    one line.
    """
    name = " ".join((filename or "").split())[:100]
    return f"{name}: {message}" if name else message


class _BodyOverBudgetError(Exception):
    """A request body passed its byte budget while it was arriving."""


async def _form_within(request: Request, budget: int | None) -> Any:
    """``request.form()``, stopped once the body passes ``budget`` bytes.

    The Content-Length check can't bound a chunked body, which carries none;
    counting the body as it arrives does, before Starlette spools all of it.
    Raises :class:`_BodyOverBudgetError`.
    """
    if budget is None:
        return await request.form()
    from starlette.requests import Request as _Request

    received = 0

    async def receive() -> Any:
        nonlocal received
        message = await request.receive()
        if message.get("type") == "http.request":
            received += len(message.get("body", b""))
            if received > budget:
                raise _BodyOverBudgetError
        return message

    return await _Request(request.scope, receive).form()


async def read_multipart_create_or_400(
    request: Request,
    *,
    meta_field: str = "meta",
    file_field: str = "file",
    max_files: int = 10,
    max_per_file_bytes: int | None = None,
    max_total_bytes: int | None = None,
) -> tuple[dict[str, Any], list[tuple[str, str, bytes]]] | JSONResponse:
    """Parse a multipart create-with-attachments body.

    Expects one ``meta`` field (JSON-encoded object with create metadata)
    and zero-or-more ``file`` parts (standard UploadFile objects).  Returns
    ``(meta_dict, [(filename, content_type, bytes), ...])`` on success or a
    ``JSONResponse`` (400/413) on failure.

    Enforces a cheap ``Content-Length`` pre-check against *max_total_bytes*
    when the header is sensible, stops reading a body (chunked ones included)
    once it passes that budget, and (when ``max_per_file_bytes`` is set) a
    generic per-file cap as a defense-in-depth gate.  The caller still
    classifies each file and applies any kind-specific cap on top.
    """
    from starlette.datastructures import UploadFile
    from starlette.responses import JSONResponse as _JSONResponse

    # A 10% slack absorbs multipart framing overhead.
    budget = int(max_total_bytes * 1.1) if max_total_bytes is not None else None
    if budget is not None:
        cl_raw = request.headers.get("content-length")
        if cl_raw:
            try:
                cl = int(cl_raw)
            except ValueError:
                cl = -1
            if cl > budget:
                return _JSONResponse(
                    {
                        "error": (
                            f"Request body too large ({cl:,} bytes by Content-Length); "
                            f"cap is {max_total_bytes:,} bytes."
                        ),
                        "code": "too_large",
                    },
                    status_code=413,
                )

    try:
        form = await _form_within(request, budget)
    except _BodyOverBudgetError:
        return _JSONResponse(
            {
                "error": (
                    f"Request body too large (over {budget:,} bytes); "
                    f"cap is {max_total_bytes:,} bytes."
                ),
                "code": "too_large",
            },
            status_code=413,
        )
    except Exception:
        import structlog

        structlog.get_logger(__name__).warning(
            "read_multipart_create_or_400.parse_failed", exc_info=True
        )
        return _JSONResponse({"error": "Invalid multipart body"}, status_code=400)

    meta_raw = form.get(meta_field)
    if not isinstance(meta_raw, str):
        return _JSONResponse({"error": f"Missing '{meta_field}' JSON field"}, status_code=400)
    try:
        meta: dict[str, Any] = json.loads(meta_raw)
    except (ValueError, json.JSONDecodeError, RecursionError):
        return _JSONResponse({"error": f"'{meta_field}' field must be valid JSON"}, status_code=400)
    if not isinstance(meta, dict):
        return _JSONResponse({"error": f"'{meta_field}' must be a JSON object"}, status_code=400)

    uploads = [v for v in form.getlist(file_field) if isinstance(v, UploadFile)]
    if len(uploads) > max_files:
        return _JSONResponse(
            {
                "error": (f"Too many files ({len(uploads)}); max {max_files} per request"),
                "code": "too_many",
            },
            status_code=400,
        )

    files: list[tuple[str, str, bytes]] = []
    running_total = 0
    try:
        for upload in uploads:
            filename = upload.filename or ""
            content_type = upload.content_type or "application/octet-stream"
            try:
                data = await upload.read()
            except Exception:
                return _JSONResponse(
                    {"error": with_filename(filename, "Failed to read upload")}, status_code=400
                )
            if max_per_file_bytes is not None and len(data) > max_per_file_bytes:
                return _JSONResponse(
                    {
                        "error": with_filename(
                            filename,
                            f"File too large ({len(data):,} bytes); "
                            f"cap is {max_per_file_bytes:,} bytes.",
                        ),
                        "code": "too_large",
                    },
                    status_code=413,
                )
            running_total += len(data)
            if max_total_bytes is not None and running_total > max_total_bytes:
                return _JSONResponse(
                    {
                        "error": (
                            f"Request body too large ({running_total:,} bytes total); "
                            f"cap is {max_total_bytes:,} bytes."
                        ),
                        "code": "too_large",
                    },
                    status_code=413,
                )
            files.append((filename, content_type, data))
    finally:
        for upload in uploads:
            await upload.close()

    return meta, files


async def read_multipart_file_or_400(
    request: Request,
    field: str = "file",
    max_bytes: int | None = None,
) -> tuple[str, str, bytes] | JSONResponse:
    """Parse a single multipart-upload file field.

    Returns ``(filename, content_type, bytes)`` on success or a
    ``JSONResponse`` (400/413) on failure.  When ``max_bytes`` is set
    and a sensible ``Content-Length`` header arrives, a 413 is returned
    before the body is parsed (cheap gate against grossly oversized
    uploads).  Otherwise the body is read only until it passes the same
    budget (Starlette spools large uploads to disk beyond ~1 MiB) and
    re-checked against ``max_bytes`` post-read.
    """
    from starlette.datastructures import UploadFile
    from starlette.responses import JSONResponse as _JSONResponse

    # Cheap pre-read gate: if Content-Length grossly exceeds max_bytes,
    # reject without parsing the body.  A 10% slack absorbs multipart
    # framing overhead.  A missing or malformed Content-Length falls
    # through to the same budget, counted as the body arrives.
    budget = int(max_bytes * 1.1) if max_bytes is not None else None
    if budget is not None:
        cl_raw = request.headers.get("content-length")
        if cl_raw:
            try:
                cl = int(cl_raw)
            except ValueError:
                cl = -1
            if cl > budget:
                return _JSONResponse(
                    {
                        "error": (
                            f"File too large ({cl:,} bytes by Content-Length); "
                            f"cap is {max_bytes:,} bytes."
                        ),
                        "code": "too_large",
                    },
                    status_code=413,
                )

    try:
        form = await _form_within(request, budget)
    except _BodyOverBudgetError:
        return _JSONResponse(
            {
                "error": f"File too large (over {budget:,} bytes); cap is {max_bytes:,} bytes.",
                "code": "too_large",
            },
            status_code=413,
        )
    except Exception:
        import structlog

        structlog.get_logger(__name__).warning(
            "read_multipart_file_or_400.parse_failed", exc_info=True
        )
        return _JSONResponse({"error": "Invalid multipart body"}, status_code=400)

    upload = form.get(field)
    if not isinstance(upload, UploadFile):
        return _JSONResponse({"error": f"Missing '{field}' file field"}, status_code=400)

    filename = upload.filename or ""
    content_type = upload.content_type or "application/octet-stream"
    try:
        data = await upload.read()
    except Exception:
        return _JSONResponse({"error": "Failed to read upload"}, status_code=400)
    finally:
        await upload.close()

    if max_bytes is not None and len(data) > max_bytes:
        return _JSONResponse(
            {
                "error": (f"File too large ({len(data):,} bytes); cap is {max_bytes:,} bytes."),
                "code": "too_large",
            },
            status_code=413,
        )

    return filename, content_type, data


def require_storage_or_503(
    request: Request,
) -> tuple[Any, JSONResponse | None]:
    """Return ``(storage, None)`` or ``(None, JSONResponse(503))``.

    Usage::

        storage, err = require_storage_or_503(request)
        if err:
            return err
    """
    from starlette.responses import JSONResponse as _JSONResponse

    storage = getattr(request.app.state, "auth_storage", None)
    if storage is None:
        return None, _JSONResponse({"error": "Storage not available"}, status_code=503)
    return storage, None


def auth_user_id(request: Request) -> str:
    """Return the authenticated user's id (empty string when absent).

    Reads ``request.state.auth_result.user_id`` set by
    :class:`AuthMiddleware`. Both node and console processes carry the
    same ``AuthResult`` shape, so this helper is kind-agnostic — useful
    for handlers lifted into :mod:`turnstone.core.session_routes`.
    """
    auth = getattr(getattr(request, "state", None), "auth_result", None)
    return str(getattr(auth, "user_id", "") or "")


# Longest text one synthesis request may carry.  The browser's read-aloud sends a
# whole message in one request, so this is also the longest message it can read.
# A hosted backend may refuse first (OpenAI's speech models take at most 4,096
# characters), but a local backend has no limit of its own, so this one bounds
# the work, the buffered reply and the console's wait for it.  At ~107 chars/s,
# the slowest backend measured, a full-cap clip takes ~75 s;
# test_the_console_outwaits_a_full_cap_clip pins the deadlines against it.
TTS_TEXT_CAP = 8000  # characters per synthesis request

# How many clips a process synthesizes at once, each on a thread of its own.
TTS_MAX_CONCURRENT = 2


class SpeechCapacity:
    """A process's synthesis threads, and how many of them are taken.

    Each thread blocks for a whole clip, so they are a pool of their own: on the
    default executor a few long clips would take the workers the service
    heartbeat needs, and the console would mark a working node offline.

    A play that finds every thread taken is refused rather than queued, because
    queue time would come out of the console's wait, which a long clip needs most
    of.  Two at once lets a play start beside a long one, and lets a user who
    replaces a play start the new one before the old one's abort lands.  On a
    backend that synthesizes one clip at a time, a play started beside a long one
    waits behind it there and can still time out: after 80 s of silence at the
    node, or at the console's 90 s.  A play also needs one of the TTS alias's
    max_concurrency slots, and for the same reason is refused when none is free.

    The node and the console each hold one: the console serves coordinator
    read-aloud itself and must not borrow a node's threads.

    Only the event loop admits and releases, so the count needs no lock.
    """

    def __init__(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        self.executor = ThreadPoolExecutor(max_workers=TTS_MAX_CONCURRENT, thread_name_prefix="tts")
        self.in_flight = 0

    def try_admit(self) -> bool:
        if self.in_flight >= TTS_MAX_CONCURRENT:
            return False
        self.in_flight += 1
        return True

    def release(self) -> None:
        self.in_flight -= 1

    def shutdown(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)


def audio_busy(error: str) -> Response:
    """The capacity answer for a voice call, which ``authFetch`` waits out for
    ``Retry-After`` seconds and then retries."""
    from starlette.responses import JSONResponse as _JSONResponse

    return _JSONResponse({"error": error}, status_code=429, headers={"Retry-After": "5"})


async def _abort_when_caller_leaves(
    request: Request, abort_ref: Any, done: Any, poll_s: float
) -> None:
    """Stop a voice call when the caller hangs up.

    A handler is not cancelled when its peer disconnects, and a thread cannot be
    cancelled once it is inside the SDK read.  Aborting ``abort_ref`` is what
    stops the work: a wait for the alias's admission slot gives up its place, and
    the backend response the call published on ``abort_ref`` is closed, so the
    transport shuts down its socket, waking even a read already blocked on a
    silent backend.  Before the backend sends headers there is no response handle
    to publish.  The backend call's own timeout bounds that interval: 80 s of
    silence for a play, and the remainder of the dictation's HTTP request budget
    (25 s from arrival) for a dictation.  Neither retries.

    The caller is the browser on a direct connection, or the console, which
    closes its node request when the browser leaves (``_proxy_tts``).  Polls
    ``is_disconnected`` every *poll_s* seconds.
    """
    import asyncio

    while not done.is_set():
        await asyncio.sleep(poll_s)
        if await request.is_disconnected():
            abort_ref.abort()
            return


@contextlib.asynccontextmanager
async def caller_watched(request: Request, abort_ref: Any, poll_s: float) -> AsyncIterator[None]:
    """Run the block while :func:`_abort_when_caller_leaves` watches the caller."""
    import asyncio

    done = asyncio.Event()
    watcher = asyncio.create_task(_abort_when_caller_leaves(request, abort_ref, done, poll_s))
    try:
        yield
    finally:
        done.set()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


def audio_backend_auth_resolver(
    request: Request, *, deadline: float | None = None
) -> Callable[[str, Any], str | None]:
    """Bind one audio HTTP request to its authenticated model principal.

    Shared by node and console so a per-user OBO token is minted for the
    *requesting* user on either process, never for a workstream owner.
    A dictation passes its monotonic *deadline* so a slow credential mint
    comes out of the same HTTP budget as the backend call instead of
    outliving it; synthesis passes none.
    """
    from turnstone.core.model_backend_auth import resolve_model_backend_auth_token
    from turnstone.core.model_oauth import get_model_token_client

    principal_id = auth_user_id(request).strip()
    config_store = getattr(request.app.state, "config_store", None)
    mint_client = get_model_token_client(request.app.state)

    def _resolve(alias: str, config: Any) -> str | None:
        return resolve_model_backend_auth_token(
            alias,
            config,
            principal_id=principal_id,
            config_store=config_store,
            mint_client=mint_client,
            deadline=deadline,
        )

    return _resolve


def tts_style_instruction(
    *, config_store: Any, registry: Any | None, alias: str, style_name: str
) -> str:
    """The ``instructions`` text for one read-aloud request ("" = send none).

    ``style_name`` is the caller's selection — the request's ``style``, or
    ``audio.tts_style`` when the request omitted one.  An empty or unknown
    name, a style with no instruction, and a model whose definition lacks
    ``supports_speech_instructions`` all resolve to no instruction: a stored
    default can outlive the style it names, and speech must never fail over a
    preference.
    """
    from turnstone.core.audio import model_supports_speech_instructions
    from turnstone.core.voice_config import parse_styles, resolve_style

    if config_store is None or registry is None or not alias:
        return ""
    style = resolve_style(parse_styles(config_store.get("audio.tts_styles") or ""), style_name)
    if style is None or not style.instruction:
        return ""
    if not model_supports_speech_instructions(registry.get_config(alias)):
        return ""
    return style.instruction


def tts_styles_payload(*, config_store: Any, registry: Any | None) -> dict[str, Any]:
    """The body of ``GET /v1/api/tts/styles``: the style picker's choices, plus
    the admin playback rate every read-aloud plays at.

    Instruction text never leaves the server; the browser only needs names and
    ``in_menu``.  ``instructions_supported`` is the exact gate the POST path
    applies, so the picker can hide itself rather than offer choices the
    configured model would ignore.  An unconfigured TTS role reads as
    unsupported.
    """
    from turnstone.core.audio import model_supports_speech_instructions, resolve_role_alias
    from turnstone.core.voice_config import parse_styles

    styles = parse_styles(config_store.get("audio.tts_styles") or "") if config_store else []
    alias = resolve_role_alias(config_store=config_store, registry=registry, role="tts")
    supported = bool(
        alias
        and registry is not None
        and model_supports_speech_instructions(registry.get_config(alias))
    )
    return {
        "styles": [{"name": s.name, "in_menu": s.in_menu} for s in styles],
        "default": str(config_store.get("audio.tts_style") or "") if config_store else "",
        "instructions_supported": supported,
        "playback_rate": _tts_playback_rate(config_store),
    }


def _tts_playback_rate(config_store: Any) -> float:
    """``audio.tts_playback_rate`` for the player; anything unusable reads as 1.0.

    It rides the styles probe because that is the one read-aloud config fetch
    the player already makes per transport base.
    """
    raw = config_store.get("audio.tts_playback_rate") if config_store is not None else None
    try:
        rate = float(raw) if raw is not None else 1.0
    except (TypeError, ValueError):
        return 1.0
    return rate if 0.5 <= rate <= 2.0 else 1.0


async def text_to_speech_response(
    request: Request, *, registry: Any | None, capacity: SpeechCapacity, poll_s: float = 1.0
) -> Response:
    """Serve ``POST /v1/api/tts`` against *registry*.

    The node and the console hold their model registries under different
    ``app.state`` names (``registry`` / ``coord_registry``), so the caller
    passes it in, with its own *capacity*; everything else (validation, role
    resolution, the synthesis bounds, error masking) is one contract so
    coordinator read-aloud behaves exactly like a node's.
    """
    import asyncio
    import contextvars
    import functools

    from starlette.responses import JSONResponse as _JSONResponse

    from turnstone.core.audio import (
        AudioBackendError,
        AudioBusyError,
        AudioUnavailableError,
        resolve_role_alias,
        synthesize,
    )
    from turnstone.core.deadline import DeadlineCancelledError, StreamAbortRef
    from turnstone.core.log import get_logger
    from turnstone.core.model_backend_auth import BackendAuthUnavailableError
    from turnstone.core.speech_text import normalize_for_speech, parse_pronunciations
    from turnstone.core.voice_config import parse_dictionary

    log = get_logger(__name__)
    body = await read_json_or_400(request)
    if isinstance(body, _JSONResponse):
        return body
    text = str(body.get("text") or "").strip()
    if not text:
        return _JSONResponse({"error": "text is required"}, status_code=400)
    if len(text) > TTS_TEXT_CAP:
        return _JSONResponse(
            {"error": f"text too long (cap {TTS_TEXT_CAP} chars)"}, status_code=400
        )

    config_store = getattr(request.app.state, "config_store", None)
    alias = resolve_role_alias(config_store=config_store, registry=registry, role="tts")
    if not alias:
        return _JSONResponse(
            {
                "error": (
                    "Text-to-speech is not configured. Assign a TTS model role in Models → Roles."
                )
            },
            status_code=503,
        )

    voice = str(body.get("voice") or "").strip()
    if not voice and config_store is not None:
        voice = (config_store.get("audio.tts_voice") or "").strip()

    # An absent/null ``style`` takes the configured default; an explicit name
    # (including "") is resolved as given.  Unknown names are never an error.
    style = body.get("style")
    if style is None and config_store is not None:
        style = config_store.get("audio.tts_style")
    instructions = tts_style_instruction(
        config_store=config_store,
        registry=registry,
        alias=alias,
        style_name=str(style or ""),
    )

    # On by default (the registry default applies); a missing config store
    # means defaults, not "off".
    if config_store is None or config_store.get("audio.tts_normalize"):
        pronunciations = ""
        dictionary = ""
        if config_store is not None:
            pronunciations = config_store.get("audio.tts_pronunciations") or ""
            dictionary = config_store.get("audio.voice_dictionary") or ""
        # Every rule is linear and the length cap bounds the work; a thread would
        # not help (the regex holds the GIL).  Both parsers are tolerant, so a
        # bad stored value never breaks read-aloud.
        text = normalize_for_speech(
            text,
            parse_pronunciations(pronunciations),
            parse_dictionary(dictionary),
        )
        # Cleanup grows text (units and dates become words), and the cap is what
        # sizes the backend call and the console's wait, so it bounds the text
        # actually synthesized as well as the text sent.
        if len(text) > TTS_TEXT_CAP:
            return _JSONResponse(
                {"error": f"text too long after speech cleanup (cap {TTS_TEXT_CAP} chars)"},
                status_code=400,
            )

    # Refused, not queued, by the process's threads and by the alias's slots
    # alike: see SpeechCapacity.  429 like the node's other capacity refusals.
    busy = "Speech synthesis is busy; try again shortly"
    if not capacity.try_admit():
        return audio_busy(busy)

    abort_ref = StreamAbortRef()
    try:
        call = functools.partial(
            synthesize,
            registry=registry,
            alias=alias,
            text=text,
            voice=voice,
            instructions=instructions,
            config_store=config_store,
            backend_auth_resolver=audio_backend_auth_resolver(request),
            cancel_ref=abort_ref,
            admission_wait_s=0,
        )
        # Blocking SDK round-trip, off the event loop.  The context copy carries
        # the request's log fields, as asyncio.to_thread does.
        context = contextvars.copy_context()
        async with caller_watched(request, abort_ref, poll_s):
            speech = await asyncio.get_running_loop().run_in_executor(
                capacity.executor, context.run, call
            )
    except DeadlineCancelledError:
        # Only the watcher aborts during the call, so the caller has gone and
        # nobody reads this answer.
        log.debug("text_to_speech.caller_left")
        return _JSONResponse({"error": "Speech synthesis was cancelled"}, status_code=499)
    except AudioBusyError:
        return audio_busy(busy)
    except BackendAuthUnavailableError:
        log.warning("text_to_speech.backend_auth_unavailable", exc_info=True)
        return _JSONResponse({"error": "Model backend authentication unavailable"}, status_code=503)
    except AudioUnavailableError as exc:
        return _JSONResponse({"error": str(exc)}, status_code=503)
    except AudioBackendError as exc:
        # Static body to the caller; backend SDK detail stays in the log.
        log.warning("text_to_speech.backend_failed", error=str(exc), exc_info=True)
        return _JSONResponse({"error": "Speech synthesis backend failed"}, status_code=502)
    finally:
        abort_ref.abort()
        capacity.release()

    return Response(
        speech.audio_bytes,
        media_type=speech.media_type,
        headers={"X-Model-Alias": speech.model_alias},
    )


STT_UPLOAD_CAP = 25 * 1024 * 1024  # 25 MiB — generous for short dictation clips

_STT_NOT_CONFIGURED = (
    "Speech-to-text is not configured. Assign an STT model role in Models → Roles."
)

# The console's dictation budget and threads, which it uses for coordinator
# dictation; the node passes its own (_STT_ADMISSION_WAIT_S / _STT_MAX_THREADS /
# _STT_REQUEST_TIMEOUT_S in turnstone/server.py, where the reasoning for each
# number lives).  STT_REQUEST_TIMEOUT_S is the budget from arrival for the
# transcode, admission, credential lookup and one backend attempt together.
STT_ADMISSION_WAIT_S = 15.0
STT_REQUEST_TIMEOUT_S = 25.0
STT_MAX_THREADS = 4


async def run_dictation[T](
    request: Request,
    arrived: float,
    call: Callable[..., T],
    /,
    *,
    wait_s: float,
    max_threads: int,
    **kwargs: Any,
) -> T:
    """Run a blocking transcription *call* on the process's dictation threads.

    Two waits share one budget, *wait_s* since the request *arrived*: the wait for a
    free dictation thread (``app.state.stt``), and the call's ``admission_wait_s``
    for its model's slot.  A dictation that gets no thread in time is refused as
    one that gets no slot is, so a process whose threads are all held by long
    transcriptions still answers 429 before the console stops waiting.  The HTTP
    handlers also pass the call a ``deadline`` from arrival in *kwargs*, so
    preparation and admission reduce the backend timeout instead of restarting
    its budget.  The context copy carries the request's log fields, as
    asyncio.to_thread does.
    """
    import asyncio
    import contextvars
    import time

    from turnstone.core.audio import AudioBusyError
    from turnstone.core.log import get_logger

    def _with_remaining_wait() -> T:
        remaining = wait_s - (time.monotonic() - arrived)
        return call(**kwargs, admission_wait_s=max(0.0, remaining))

    threads = request.app.state.stt
    context = contextvars.copy_context()
    work = threads.submit(context.run, _with_remaining_wait)
    result = asyncio.wrap_future(work)
    # asyncio.wait cancels nothing it waits on, unlike wait_for: cancelling a shield whose inner
    # call later fails makes asyncio log that failure as unhandled.
    try:
        done, _ = await asyncio.wait({result}, timeout=wait_s - (time.monotonic() - arrived))
    except asyncio.CancelledError:
        result.cancel()  # as asyncio.to_thread does: a call still queued never runs
        raise
    if not done and work.cancel():
        get_logger(__name__).info(
            "speech_to_text.no_free_thread",
            threads=max_threads,
            wait_seconds=round(time.monotonic() - arrived, 3),
        )
        raise AudioBusyError("No dictation thread came free in time")
    # Done, or it started meanwhile and its own wait for a slot is bounded.
    return await result


def _console_dictation(
    request: Request, arrived: float, call: Callable[..., Any], /, **kwargs: Any
) -> Any:
    """The console's dictation runner: :func:`run_dictation` with its own budget."""
    return run_dictation(
        request, arrived, call, wait_s=STT_ADMISSION_WAIT_S, max_threads=STT_MAX_THREADS, **kwargs
    )


def stt_dictionary(config_store: Any) -> list[DictionaryEntry]:
    """The voice dictionary dictation uses, parsed tolerantly.

    ``[]`` when no config store is present or the stored value is unusable: a
    dictionary that predates a rule change must never break dictation.
    """
    if config_store is None:
        return []
    from turnstone.core.voice_config import parse_dictionary

    return parse_dictionary(config_store.get("audio.voice_dictionary") or "")


def stt_prompt(config_store: Any, dictionary: Sequence[DictionaryEntry]) -> str:
    """The transcription prompt: ``audio.stt_prompt``, plus dictionary terms.

    The terms are appended only when ``audio.stt_dictionary_hint`` is on.  A
    missing config store keeps the old behaviour (no prompt); with a store, the
    registry defaults apply, so both voice settings default on.
    """
    if config_store is None:
        return ""
    prompt = (config_store.get("audio.stt_prompt") or "").strip()
    if dictionary and config_store.get("audio.stt_dictionary_hint"):
        from turnstone.core.dictation_text import dictionary_prompt

        prompt = dictionary_prompt(prompt, dictionary)
    return prompt


def correct_stt_transcript(
    config_store: Any, dictionary: Sequence[DictionaryEntry], text: str
) -> str:
    """Apply the dictionary's ``also_hears`` replacements when enabled."""
    if config_store is None or not dictionary:
        return text
    if not config_store.get("audio.stt_dictionary_correct"):
        return text
    from turnstone.core.dictation_text import correct_transcript

    return correct_transcript(text, dictionary)


class AbortOnExitStreamingResponse(StreamingResponse):
    """Abort an upstream audio stream even before body iteration begins.

    ``StreamingResponse.__call__`` can be cancelled (client gone) before the body
    generator ever starts, in which case the generator's own ``finally`` never
    runs and the backend connection would stay pinned.
    """

    def __init__(self, *args: Any, abort_ref: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._abort_ref = abort_ref

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._abort_ref.abort()


async def speech_to_text_response(
    request: Request,
    *,
    registry: Any | None,
    authorize: Callable[[Request, str], Awaitable[JSONResponse | None]],
    run: Callable[..., Awaitable[Any]] = _console_dictation,
    poll_s: float = 1.0,
    request_timeout_s: float | None = None,
) -> JSONResponse:
    """Serve ``POST /v1/api/workstreams/{ws_id}/speech-to-text`` against *registry*.

    Multipart body with a single ``audio`` field; returns the transcript for the
    browser to place into the composer and never sends on the user's behalf.
    A node and the console hold their registries under different ``app.state``
    names and authorise ``ws_id`` differently (interactive vs coordinator), so
    both are injected; ``authorize`` returns an error response or ``None``.
    *run* runs the transcription on the process's dictation threads within its
    admission budget (:func:`run_dictation`); the node passes its own budget.
    *request_timeout_s* is the HTTP budget from arrival (the node passes its
    own; ``None`` is the console's ``STT_REQUEST_TIMEOUT_S``, read at call
    time): the credential lookup, transcode, admission and the one backend
    attempt all come out of it.
    Everything else is one contract so the coordinator mic behaves like a node's.
    """
    import time

    from starlette.responses import JSONResponse as _JSONResponse

    from turnstone.core.audio import (
        AudioBackendError,
        AudioBusyError,
        AudioUnavailableError,
        resolve_role_alias,
        transcribe,
    )
    from turnstone.core.deadline import DeadlineCancelledError, StreamAbortRef
    from turnstone.core.log import get_logger
    from turnstone.core.model_backend_auth import BackendAuthUnavailableError

    log = get_logger(__name__)
    arrived = time.monotonic()
    ws_id = request.path_params.get("ws_id", "")
    if not ws_id:
        return _JSONResponse({"error": "ws_id is required"}, status_code=400)
    err = await authorize(request, ws_id)
    if err is not None:
        return err

    config_store = getattr(request.app.state, "config_store", None)
    alias = resolve_role_alias(config_store=config_store, registry=registry, role="stt")
    if not alias:
        return _JSONResponse({"error": _STT_NOT_CONFIGURED}, status_code=503)

    got = await read_multipart_file_or_400(request, field="audio", max_bytes=STT_UPLOAD_CAP)
    if isinstance(got, _JSONResponse):
        return got
    filename, _claimed_mime, data = got
    if not data:
        return _JSONResponse({"error": "Empty audio upload"}, status_code=400)

    dictionary = stt_dictionary(config_store)
    prompt = stt_prompt(config_store, dictionary)
    abort_ref = StreamAbortRef()
    # One budget from arrival: the credential lookup and the call share it.
    # The console's budget is read now, as its admission wait is.
    budget = STT_REQUEST_TIMEOUT_S if request_timeout_s is None else request_timeout_s
    deadline = arrived + budget
    backend_auth_resolver = audio_backend_auth_resolver(request, deadline=deadline)
    try:
        # Blocking SDK round-trip, off the event loop.
        async with caller_watched(request, abort_ref, poll_s):
            result = await run(
                request,
                arrived,
                transcribe,
                registry=registry,
                alias=alias,
                data=data,
                filename=filename or "speech.webm",
                prompt=prompt,
                config_store=config_store,
                backend_auth_resolver=backend_auth_resolver,
                cancel_ref=abort_ref,
                deadline=deadline,
            )
    except DeadlineCancelledError:
        # Only the watcher aborts during the call, so the caller has gone.
        log.debug("speech_to_text.caller_left")
        return _JSONResponse({"error": "Speech transcription was cancelled"}, status_code=499)
    except AudioBusyError:
        return audio_busy("Speech transcription is busy; try again shortly")
    except BackendAuthUnavailableError:
        log.warning("speech_to_text.backend_auth_unavailable", exc_info=True)
        return _JSONResponse({"error": "Model backend authentication unavailable"}, status_code=503)
    except AudioUnavailableError as exc:
        return _JSONResponse({"error": str(exc)}, status_code=503)
    except AudioBackendError as exc:
        # str(exc) wraps the backend SDK error, which can carry upstream
        # response detail — log it, but return a static body to the caller.
        log.warning("speech_to_text.backend_failed", error=str(exc), exc_info=True)
        return _JSONResponse({"error": "Speech transcription backend failed"}, status_code=502)
    finally:
        abort_ref.abort()

    if not result.transcript:
        # Successful call that detected no speech (silence / non-speech audio)
        # is not a backend failure — surface it as 422 so the UI can say so
        # rather than treating a healthy backend as a bad gateway.
        return _JSONResponse({"error": "No speech detected"}, status_code=422)

    transcript = correct_stt_transcript(config_store, dictionary, result.transcript)
    return _JSONResponse(
        {
            "status": "ok",
            "transcript": transcript,
            "model_alias": result.model_alias,
        }
    )


async def speech_to_text_stream_response(
    request: Request,
    *,
    registry: Any | None,
    authorize: Callable[[Request, str], Awaitable[JSONResponse | None]],
    run: Callable[..., Awaitable[Any]] = _console_dictation,
    poll_s: float = 1.0,
    request_timeout_s: float | None = None,
) -> Response:
    """Serve ``POST .../speech-to-text/stream``: the transcript as plain-text deltas.

    Lower perceived latency than the JSON route.  Resolve/transcode failures
    surface as 503 / 502 before any bytes are sent.  Once streaming begins, a
    mid-stream backend failure (the request deadline included) aborts the body
    without its last chunk, so the browser's read fails instead of taking the
    partial text for a whole transcript; a caller that has gone ends quietly.
    The node serves its route through this handler too, so its composer gets
    the same signal.  Dictionary terms are hinted in the prompt like the JSON
    route, but transcript correction is JSON-route only — this route has no
    final event, only raw deltas.  *registry* / *authorize* as in
    :func:`speech_to_text_response`.
    """
    import asyncio
    import threading
    import time

    from starlette.responses import JSONResponse as _JSONResponse

    from turnstone.core.audio import (
        AudioBackendError,
        AudioBusyError,
        AudioUnavailableError,
        resolve_role_alias,
        transcribe_stream,
    )
    from turnstone.core.deadline import DeadlineCancelledError, StreamAbortRef
    from turnstone.core.log import get_logger
    from turnstone.core.model_backend_auth import BackendAuthUnavailableError

    log = get_logger(__name__)
    arrived = time.monotonic()
    ws_id = request.path_params.get("ws_id", "")
    if not ws_id:
        return _JSONResponse({"error": "ws_id is required"}, status_code=400)
    err = await authorize(request, ws_id)
    if err is not None:
        return err

    config_store = getattr(request.app.state, "config_store", None)
    alias = resolve_role_alias(config_store=config_store, registry=registry, role="stt")
    if not alias:
        return _JSONResponse({"error": _STT_NOT_CONFIGURED}, status_code=503)

    got = await read_multipart_file_or_400(request, field="audio", max_bytes=STT_UPLOAD_CAP)
    if isinstance(got, _JSONResponse):
        return got
    _filename, _claimed_mime, data = got
    if not data:
        return _JSONResponse({"error": "Empty audio upload"}, status_code=400)

    # The dictionary hint applies here exactly as on the JSON route.  The
    # correction does not: this route emits raw content deltas and the browser
    # appends each one straight into the composer, so there is no final or
    # complete transcript event to rewrite (and a phrase may span two deltas,
    # which no per-delta rewrite could match).  Raw deltas are left alone.
    dictionary = stt_dictionary(config_store)
    prompt = stt_prompt(config_store, dictionary)
    abort_ref = StreamAbortRef()
    # One budget from arrival: the credential lookup and the call share it.
    # The console's budget is read now, as its admission wait is.
    budget = STT_REQUEST_TIMEOUT_S if request_timeout_s is None else request_timeout_s
    deadline = arrived + budget
    backend_auth_resolver = audio_backend_auth_resolver(request, deadline=deadline)

    # Resolve + transcode + admission + open the stream eagerly (off the event
    # loop) so the common failures map to a clean status before any bytes are
    # sent.
    try:
        async with caller_watched(request, abort_ref, poll_s):
            deltas = await run(
                request,
                arrived,
                transcribe_stream,
                registry=registry,
                alias=alias,
                data=data,
                prompt=prompt,
                config_store=config_store,
                backend_auth_resolver=backend_auth_resolver,
                cancel_ref=abort_ref,
                deadline=deadline,
            )
    except DeadlineCancelledError:
        # Only the watcher aborts during the call, so the caller has gone.
        abort_ref.abort()
        log.debug("speech_to_text_stream.caller_left")
        return _JSONResponse({"error": "Speech transcription was cancelled"}, status_code=499)
    except AudioBusyError:
        abort_ref.abort()
        return audio_busy("Speech transcription is busy; try again shortly")
    except BackendAuthUnavailableError:
        abort_ref.abort()
        log.warning("speech_to_text_stream.backend_auth_unavailable", exc_info=True)
        return _JSONResponse({"error": "Model backend authentication unavailable"}, status_code=503)
    except AudioUnavailableError as exc:
        abort_ref.abort()
        return _JSONResponse({"error": str(exc)}, status_code=503)
    except AudioBackendError:
        abort_ref.abort()
        log.warning("speech_to_text_stream.backend_failed", exc_info=True)
        return _JSONResponse({"error": "Speech transcription backend failed"}, status_code=502)
    except BaseException:
        abort_ref.abort()
        raise

    # Drive the blocking stream from one worker thread that owns (and closes)
    # the upstream connection, handing deltas to the loop via a queue.  A client
    # disconnect sets ``stop`` so the thread releases the connection promptly
    # instead of being pinned mid-``next()`` (which can't be cancelled).
    async def _body() -> AsyncGenerator[bytes]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        stop = threading.Event()
        # Set before the final None when the backend failed mid-stream.  A clean
        # end would read as a whole transcript, so the body fails instead.
        failed = threading.Event()

        def _pump() -> None:
            try:
                for delta in deltas:
                    if stop.is_set():
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, delta.encode("utf-8"))
            except Exception:
                # Mid-stream backend failure (logged); a caller that has gone
                # aborted the stream itself, and that ends quietly.
                if not abort_ref.aborted:
                    log.warning("speech_to_text_stream.mid_stream_failed", exc_info=True)
                    failed.set()
            finally:
                close = getattr(deltas, "close", None)
                if callable(close):
                    close()
                loop.call_soon_threadsafe(queue.put_nowait, None)

        loop.run_in_executor(None, _pump)
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    if failed.is_set():
                        # Escapes the response, so the server drops the
                        # connection before the chunked body's last chunk.
                        raise AudioBackendError("Speech transcription stream failed mid-stream")
                    break
                yield chunk
        finally:
            stop.set()
            abort_ref.abort()

    return AbortOnExitStreamingResponse(
        _body(),
        media_type="text/plain; charset=utf-8",
        abort_ref=abort_ref,
    )


def audit_context(request: Request) -> tuple[str, str]:
    """Extract (user_id, ip_address) from request for audit logging.

    Honors ``X-Forwarded-For`` only when the request appears to come
    through a trusted proxy (``X-Forwarded-Proto`` is set), matching the
    existing ``is_secure_request()`` trust model.  Falls back to
    ``request.client.host`` otherwise.
    """
    from turnstone.core.auth import is_secure_request

    ip = ""
    # Only trust X-Forwarded-For when behind a proxy that sets X-Forwarded-Proto
    if is_secure_request(dict(request.headers), request.url.scheme):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            ip = forwarded.split(",")[0].strip()
    if not ip:
        ip = request.client.host if request.client else ""
    return auth_user_id(request), ip


# The ``code`` on the 503 a node answers when it can't read a workstream's
# row to check access.  The console's route proxy re-routes on it as on a
# 404: a node that doesn't hold the workstream has to read the row, and the
# one that holds it may not need to.  When the re-route finds the same node,
# the 503 passes through.
WORKSTREAM_UNREADABLE_CODE = "workstream_unreadable"


def resolve_workstream_owner(
    request: Request,
    ws_id: str,
    *,
    mgr: Any | None = None,
    not_found_label: str = "Workstream not found",
    resolved_row: dict[str, Any] | None = None,
) -> tuple[str, JSONResponse | None]:
    """Resolve ``ws_id`` to its owner; 404 when the row doesn't exist.

    Turnstone is a trusted-team tool: scope-level auth (e.g.
    ``admin.coordinator``) is the primary gate and row-level OWNERSHIP
    is still not enforced here. The one row-level check this performs
    is PROJECT tenancy: a workstream attached to a *private* project is
    only reachable by the project's owner/members, the workstream's own
    creator, and service-scope callers — everyone else, admins included,
    gets a 403 (see
    :class:`turnstone.core.auth.WorkstreamProjectVisibility`). Returns
    ``(owner_user_id, None)`` on success — the persisted owner id,
    which attachments should be filed under so existing storage shape
    is preserved. Falls back to the caller's own uid when the row has
    no recorded owner.

    When ``mgr`` is provided and the workstream is live in memory,
    trust its cached ``user_id`` / ``project_id`` instead of
    round-tripping storage for the ROW — but note the project gate
    itself may still hit storage (one memoized ``get_project`` +
    membership lookup when the row names a project), and it fails
    CLOSED: a project-attached workstream 403s when that lookup fails
    rather than risking a leak. That is a deliberate trade — a DB
    outage already degrades sends/persistence, and failing open on a
    private-tenancy check is the worse failure. Project-less
    workstreams keep the zero-storage fast path.

    Failure-mode map, precisely: which error a storage outage produces
    depends on where it strikes. A storage-path ROW fetch that fails
    answers 503: an outage is not a missing workstream, and since the
    failed read says nothing about the id, the 503 is no existence
    oracle. Only once a row IS resolved (from the manager or storage)
    does the project gate run, and THAT failure is the fail-closed 403
    above. In-memory workstreams therefore 403 on a project-gate blip;
    not-loaded ones answer 503 at the row fetch first.

    ``not_found_label`` is the message body the 404 carries — the
    interactive surface uses "Workstream not found"; coord uses
    "coordinator not found" so error strings stay readable per kind.
    """
    from starlette.responses import JSONResponse as _JSONResponse

    from turnstone.core.auth import WorkstreamProjectVisibility

    caller = auth_user_id(request)
    owner: str | None = None
    project_id = ""

    if resolved_row is not None:
        if resolved_row.get("state") == "creating":
            return "", _JSONResponse({"error": not_found_label}, status_code=404)
        owner = resolved_row.get("user_id") or ""
        project_id = resolved_row.get("project_id") or ""
    elif mgr is not None:
        ws_mem = mgr.get(ws_id)
        if ws_mem is not None:
            owner = ws_mem.user_id or ""
            raw_pid = getattr(ws_mem, "project_id", "")
            project_id = raw_pid if isinstance(raw_pid, str) else ""

    if owner is None:
        # Not in memory — storage resolves persisted-but-not-loaded rows.
        from turnstone.core.storage import get_storage

        try:
            row = get_storage().get_workstream(ws_id)
        except Exception:
            import structlog

            structlog.get_logger(__name__).warning(
                "resolve_workstream_owner.storage_failed", ws_id=ws_id[:8], exc_info=True
            )
            return "", _JSONResponse(
                {"error": "workstream could not be read", "code": WORKSTREAM_UNREADABLE_CODE},
                status_code=503,
            )
        if row is None or row.get("state") == "creating":
            return "", _JSONResponse({"error": not_found_label}, status_code=404)
        owner = row.get("user_id") or ""
        project_id = row.get("project_id") or ""

    if project_id:
        visibility = WorkstreamProjectVisibility.for_request(request)
        if not visibility.ws_visible(project_id, ws_owner=owner):
            return "", _JSONResponse(
                {"error": "Forbidden: workstream belongs to a private project"},
                status_code=403,
            )

    return owner or caller, None


def parse_cors_origins() -> list[str] | None:
    """Parse ``TURNSTONE_CORS_ORIGINS`` env var into a list of origin strings.

    Returns ``None`` when the variable is unset or empty (meaning: no CORS
    middleware, same-origin only).
    """
    cors_env = os.environ.get("TURNSTONE_CORS_ORIGINS", "").strip()
    if not cors_env:
        return None
    return [o.strip() for o in cors_env.split(",") if o.strip()]


def cors_middleware(origins: list[str]) -> Middleware:
    """Build a Starlette ``CORSMiddleware`` entry for the given *origins*."""
    from starlette.middleware import Middleware as _Middleware
    from starlette.middleware.cors import CORSMiddleware

    return _Middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )


# ---------------------------------------------------------------------------
# Static asset cache-busting
# ---------------------------------------------------------------------------

# Keep this directory definition shared by the HTML rewriter and static response
# policy.  These directories already carry their library version in the URL,
# so their contents can be cached immutably; every other first-party asset must
# revalidate because ES-module imports do not inherit the entry module's ?v=.
_VERSIONED_VENDOR_DIR = r"(?:katex|hljs|hls|mermaid|inter|jetbrains-mono|dejavu-sans)-\d+(?:\.\d+)+"
_VERSIONED_VENDOR_PATH_RE = re.compile(rf"^{_VERSIONED_VENDOR_DIR}/")


def is_safe_static_asset_path(path: str) -> bool:
    """Return whether *path* is a canonical mount-relative asset path.

    Starlette decodes percent escapes before populating ``{path:path}``, and httpx2 normalizes dot
    segments when it builds an outbound URL.  Reject the ambiguous forms before a console proxy can
    leave its static mount.
    """
    return (
        bool(path)
        and "\\" not in path
        and all(segment not in {"", ".", ".."} for segment in path.split("/"))
    )


def static_asset_cache_control(path: str) -> str:
    """Return the cache policy for an asset path relative to its mount."""
    if not is_safe_static_asset_path(path):
        return "no-store"
    if _VERSIONED_VENDOR_PATH_RE.match(path):
        return "public, max-age=31536000, immutable"
    return "no-cache"


class RevalidatingStaticFiles(StaticFiles):
    """StaticFiles with correctness-first caching for first-party assets.

    First-party files use a content-derived ETag and no Last-Modified header.
    ``no-cache`` therefore revalidates by content on reload, even when two
    builds share a package version, byte length, and filesystem timestamp.
    Version-named vendor directories retain immutable caching.
    """

    @cached_property
    def _content_etags(self) -> dict[str, tuple[tuple[int, int, int], str]]:
        """Lazily allocate the bounded per-static-tree validator cache."""
        return {}

    def _content_etag(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
    ) -> str:
        path = os.fspath(full_path)
        signature = (stat_result.st_mtime_ns, stat_result.st_ctime_ns, stat_result.st_size)
        cached = self._content_etags.get(path)
        if cached is not None and cached[0] == signature:
            return cached[1]
        with open(path, "rb") as asset:
            digest = hashlib.file_digest(asset, "sha256").hexdigest()
        etag = f'"sha256-{digest}"'
        self._content_etags[path] = (signature, etag)
        return etag

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        asset_path = self.get_path(scope)
        if static_asset_cache_control(asset_path) != "no-cache":
            return super().file_response(full_path, stat_result, scope, status_code)

        request_headers = Headers(scope=scope)
        response = FileResponse(full_path, status_code=status_code, stat_result=stat_result)
        etag = self._content_etag(full_path, stat_result)
        response.headers["ETag"] = etag
        del response.headers["Last-Modified"]
        if self.is_not_modified(response.headers, request_headers):
            return Response(status_code=304, headers={"ETag": etag})
        return response

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            return PlainTextResponse(
                "Not Found", status_code=404, headers={"Cache-Control": "no-store"}
            )
        cache_control = (
            "no-store" if response.status_code >= 400 else static_asset_cache_control(path)
        )
        response.headers["Cache-Control"] = cache_control
        return response


# Matches src="/static/..." and href="/shared/..." (and vice-versa) but skips
# vendored libraries whose directory names already contain a version number
# (e.g. katex-0.16.44/, hljs-11.12.0/) and URLs that already have a query
# string (prevents double-append if called twice).
_ASSET_RE = re.compile(
    r'(?P<attr>(?:src|href)=")'
    r"(?P<path>/(?:static|shared)/)"
    rf"(?!{_VERSIONED_VENDOR_DIR}/)"
    r'(?P<file>[^"?]+)"'
)


def version_html(html: str) -> str:
    """Inject ``?v=VERSION`` into ``/static/`` and ``/shared/`` asset URLs.

    Vendored libraries with version-bearing directory names are skipped.
    URLs that already contain a query string are left unchanged.
    Safe to call either at startup or while rendering a dynamic template.
    """
    from turnstone import __version__

    def _repl(m: re.Match[str]) -> str:
        return f'{m.group("attr")}{m.group("path")}{m.group("file")}?v={__version__}"'

    return _ASSET_RE.sub(_repl, html)
