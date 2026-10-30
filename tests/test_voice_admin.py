"""Tests for the Admin › Voice tuning endpoints: meta and preview.

``GET /v1/api/admin/voice/meta`` describes the settings the Voice page edits
(cleanup groups, built-in terms, profile fields, TTS models, limits);
``POST /v1/api/admin/voice/preview`` runs the text an operator typed through
the same cleanup the read-aloud path will run, with draft rules replacing the
stored ones.  ``preview_for_speech`` is the cleanup-engine slice's function and
raises ``NotImplementedError`` until it lands, so every test that reaches it
patches it and asserts the arguments — especially the ``only_rules`` options.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware, _FakeConfigStore
from turnstone.console.voice_admin import admin_voice_meta, admin_voice_preview
from turnstone.core.voice_config import (
    BUILTIN_TERMS,
    CLEANUP_GROUP_IDS,
    CLEANUP_GROUPS,
    DEFAULT_PROFILE,
    MAX_MARKER_CHARS,
    MAX_PROFILES,
    MAX_REWRITE_RULES,
    MAX_RULE_FIND_CHARS,
    MAX_RULE_REPLACE_CHARS,
    MAX_RULE_TEST_CHARS,
    PROFILE_FIELDS,
    VOICE_DOCS_URL,
    PreviewSpan,
    SpeechPreview,
)

# ---------------------------------------------------------------------------
# Fakes and app wiring
# ---------------------------------------------------------------------------


class _Registry:
    """Just the surface ``model_supports_role`` + ``resolve_role_alias`` read."""

    def __init__(self, models: dict[str, SimpleNamespace]) -> None:
        self._models = models

    def has_alias(self, alias: str) -> bool:
        return alias in self._models

    def get_config(self, alias: str) -> SimpleNamespace:
        return self._models[alias]

    def list_aliases(self) -> list[str]:
        return list(self._models)


def _cfg(model: str, capabilities: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(provider="openai", model=model, capabilities=capabilities or {})


def _registry() -> _Registry:
    return _Registry(
        {
            "tts-qwen": _cfg("qwen-tts", {"supports_speech_synthesis": True}),
            "tts-other": _cfg("other-tts", {"supports_speech_synthesis": True}),
            "chat": _cfg("gpt-4o"),
        }
    )


def _client(
    *,
    settings: dict[str, Any] | None = None,
    registry: Any = "default",
) -> TestClient:
    app = Starlette(
        routes=[
            Route("/v1/api/admin/voice/meta", admin_voice_meta),
            Route("/v1/api/admin/voice/preview", admin_voice_preview, methods=["POST"]),
        ],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.config_store = _FakeConfigStore(dict(settings or {}))
    app.state.coord_registry = _registry() if registry == "default" else registry
    client = TestClient(app)
    client.headers.update({"X-Test-User": "admin", "X-Test-Perms": "admin.settings"})
    return client


class _PreviewSpy:
    """Stand-in for ``preview_for_speech``: records calls, returns a fixed result."""

    def __init__(self, spans: tuple[PreviewSpan, ...] = ()) -> None:
        self.spans = spans
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        text: str,
        pronunciations: Any = None,
        dictionary: Any = None,
        options: Any = None,
    ) -> SpeechPreview:
        self.calls.append(
            {
                "text": text,
                "pronunciations": pronunciations,
                "dictionary": dictionary,
                "options": options,
            }
        )
        return SpeechPreview(text=f"<{text}>", spans=self.spans)


def _install_preview(monkeypatch: pytest.MonkeyPatch, spy: _PreviewSpy) -> _PreviewSpy:
    # The handler imports the name at call time, so patching the module
    # attribute is what reaches it (and what the cleanup-engine merge keeps).
    import turnstone.core.speech_text as speech_text

    monkeypatch.setattr(speech_text, "preview_for_speech", spy)
    return spy


_ALL_TERMS = frozenset(term for term, _ in BUILTIN_TERMS)

_STORED_RULES = [
    {
        "find": "ACME",
        "regex": False,
        "replace": "Acme Corp",
        "test": "ACME builds",
        "enabled": True,
    },
    {"find": "k8s", "regex": False, "replace": "kubernetes", "test": "", "enabled": False},
]

_STORED_SETTINGS: dict[str, Any] = {
    "audio.tts_normalize": True,
    "audio.tts_cleanup_disabled": json.dumps(["versions", "dates"]),
    "audio.tts_rewrite_rules": json.dumps(_STORED_RULES),
    "audio.tts_builtin_terms_off": json.dumps(["YAML", "JSON"]),
    "audio.tts_pronunciations": "nginx = engine ex\n# comment\nsql = sequel",
    "audio.voice_dictionary": json.dumps(
        [{"term": "SQL", "kind": "respelling", "respelling": "ess cue ell"}]
    ),
}

_SPANS = (
    PreviewSpan(0, 4, "group", "units"),
    PreviewSpan(5, 11, "custom", "custom 2"),
    PreviewSpan(12, 20, "dictionary", "dictionary"),
)


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


def test_meta_requires_admin_settings() -> None:
    client = _client()
    client.headers.update({"X-Test-Perms": ""})
    resp = client.get("/v1/api/admin/voice/meta")
    assert resp.status_code == 403
    assert resp.json() == {"error": "Forbidden: missing 'admin.settings' permission"}


def test_preview_requires_admin_settings() -> None:
    client = _client()
    client.headers.update({"X-Test-Perms": ""})
    resp = client.post("/v1/api/admin/voice/preview", json={"text": "hi"})
    assert resp.status_code == 403
    assert resp.json() == {"error": "Forbidden: missing 'admin.settings' permission"}


def test_meta_without_any_auth_is_401() -> None:
    client = _client()
    client.headers.clear()
    resp = client.get("/v1/api/admin/voice/meta")
    assert resp.status_code == 401
    assert resp.json() == {"error": "Unauthorized"}


# ---------------------------------------------------------------------------
# GET /v1/api/admin/voice/meta
# ---------------------------------------------------------------------------


def test_meta_groups_run_each_group_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _install_preview(monkeypatch, _PreviewSpy())
    body = _client().get("/v1/api/admin/voice/meta").json()

    assert [g["id"] for g in body["groups"]] == [g.id for g in CLEANUP_GROUPS]
    assert len(spy.calls) == len(CLEANUP_GROUPS)
    for group, call in zip(CLEANUP_GROUPS, spy.calls, strict=True):
        assert call["text"] == group.example
        assert call["options"].disabled_groups == CLEANUP_GROUP_IDS - {group.id}
        assert call["options"].rules == ()
        assert call["options"].builtin_terms_off == _ALL_TERMS
        assert call["pronunciations"] is None
        assert call["dictionary"] is None
    first = body["groups"][0]
    assert first == {
        "id": CLEANUP_GROUPS[0].id,
        "label": CLEANUP_GROUPS[0].label,
        "description": CLEANUP_GROUPS[0].description,
        "example": CLEANUP_GROUPS[0].example,
        "result": f"<{CLEANUP_GROUPS[0].example}>",
    }


def test_meta_lists_builtin_terms_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_preview(monkeypatch, _PreviewSpy())
    body = _client().get("/v1/api/admin/voice/meta").json()
    assert body["builtin_terms"] == [
        {"term": term, "respelling": respelling} for term, respelling in BUILTIN_TERMS
    ]


def test_meta_profile_fields_document_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_preview(monkeypatch, _PreviewSpy())
    body = _client().get("/v1/api/admin/voice/meta").json()
    fields = body["profile_fields"]
    assert [f["key"] for f in fields] == [f.key for f in PROFILE_FIELDS]
    by_key = {f["key"]: f for f in fields}
    assert by_key["pause_heading_ms"] == {
        "key": "pause_heading_ms",
        "group": "pacing",
        "kind": "int",
        "default": 600,
        "label": PROFILE_FIELDS[0].label,
        "help": PROFILE_FIELDS[0].help,
        "reference_url": VOICE_DOCS_URL + "#pacing",
        "min": 0,
        "max": 5000,
        "choices": [],
        "unit": "ms",
    }
    assert by_key["table_cell_joiner"]["choices"] == ["comma", "semicolon", "pause"]
    assert by_key["table_cell_joiner"]["min"] is None
    assert by_key["code_marker"]["kind"] == "str"
    assert by_key["code_marker"]["unit"] == ""
    assert by_key["code_marker"]["reference_url"] == VOICE_DOCS_URL + "#structure"
    assert by_key["max_clips"]["reference_url"] == VOICE_DOCS_URL + "#clips"


def test_meta_tts_models_lists_speech_models_and_marks_the_active_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_preview(monkeypatch, _PreviewSpy())
    body = (
        _client(settings={"audio.tts_model_alias": "tts-qwen"})
        .get("/v1/api/admin/voice/meta")
        .json()
    )
    assert body["tts_models"] == [
        {"alias": "tts-qwen", "active": True},
        {"alias": "tts-other", "active": False},
    ]


def test_meta_tts_models_without_a_resolved_role(monkeypatch: pytest.MonkeyPatch) -> None:
    # The configured alias is not speech-capable: every row is inactive.
    _install_preview(monkeypatch, _PreviewSpy())
    body = (
        _client(settings={"audio.tts_model_alias": "chat"}).get("/v1/api/admin/voice/meta").json()
    )
    assert [m["alias"] for m in body["tts_models"]] == ["tts-qwen", "tts-other"]
    assert all(m["active"] is False for m in body["tts_models"])


def test_meta_tts_models_is_empty_without_a_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_preview(monkeypatch, _PreviewSpy())
    body = _client(registry=None).get("/v1/api/admin/voice/meta").json()
    assert body["tts_models"] == []


def test_meta_limits_come_from_the_contract_constants(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_preview(monkeypatch, _PreviewSpy())
    body = _client().get("/v1/api/admin/voice/meta").json()
    assert body["limits"] == {
        "max_rules": MAX_REWRITE_RULES,
        "max_find_chars": MAX_RULE_FIND_CHARS,
        "max_replace_chars": MAX_RULE_REPLACE_CHARS,
        "max_test_chars": MAX_RULE_TEST_CHARS,
        "max_profiles": MAX_PROFILES,
        "max_marker_chars": MAX_MARKER_CHARS,
    }


# ---------------------------------------------------------------------------
# POST /v1/api/admin/voice/preview — body validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [{}, {"text": None}, {"text": 5}, {"text": ["hi"]}],
)
def test_preview_requires_a_text_string(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
) -> None:
    spy = _install_preview(monkeypatch, _PreviewSpy())
    resp = _client().post("/v1/api/admin/voice/preview", json=body)
    assert resp.status_code == 400
    assert resp.json() == {"error": "text is required"}
    assert spy.calls == []


def test_preview_rejects_text_over_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _install_preview(monkeypatch, _PreviewSpy())
    resp = _client().post("/v1/api/admin/voice/preview", json={"text": "x" * 5001})
    assert resp.status_code == 400
    assert resp.json() == {"error": "text is too long (max 5000 characters)"}
    assert spy.calls == []


def test_preview_accepts_text_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_preview(monkeypatch, _PreviewSpy())
    resp = _client().post("/v1/api/admin/voice/preview", json={"text": "x" * 5000})
    assert resp.status_code == 200


def test_preview_accepts_empty_text(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _install_preview(monkeypatch, _PreviewSpy())
    resp = _client().post("/v1/api/admin/voice/preview", json={"text": ""})
    assert resp.status_code == 200
    assert spy.calls[0]["text"] == ""


@pytest.mark.parametrize("rules", ["nope", None, {}, 3])
def test_preview_rejects_rules_that_are_not_a_list(
    monkeypatch: pytest.MonkeyPatch, rules: Any
) -> None:
    spy = _install_preview(monkeypatch, _PreviewSpy())
    resp = _client().post("/v1/api/admin/voice/preview", json={"text": "hi", "rules": rules})
    assert resp.status_code == 400
    assert resp.json() == {"error": "rules must be a list"}
    assert spy.calls == []


@pytest.mark.parametrize("only_rules", ["yes", 0, 1, [], {}])
def test_preview_rejects_only_rules_that_is_not_a_bool(
    monkeypatch: pytest.MonkeyPatch, only_rules: Any
) -> None:
    spy = _install_preview(monkeypatch, _PreviewSpy())
    resp = _client().post(
        "/v1/api/admin/voice/preview", json={"text": "hi", "only_rules": only_rules}
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "only_rules must be true or false"}
    assert spy.calls == []


def test_preview_rejects_a_non_json_body() -> None:
    resp = _client().post(
        "/v1/api/admin/voice/preview",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "Invalid JSON body"}


def test_preview_returns_the_validation_message_for_a_bad_draft_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _install_preview(monkeypatch, _PreviewSpy())
    resp = _client().post(
        "/v1/api/admin/voice/preview",
        json={"text": "hi", "rules": [{"find": "", "replace": "x"}]},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": "rule 1: find is required"}
    assert spy.calls == []


def test_preview_returns_the_compile_message_for_a_bad_regex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _install_preview(monkeypatch, _PreviewSpy())
    resp = _client().post(
        "/v1/api/admin/voice/preview",
        json={"text": "hi", "rules": [{"find": "a*", "regex": True, "replace": "x"}]},
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error.startswith("rule 1: this pattern can match empty text")
    assert spy.calls == []


# ---------------------------------------------------------------------------
# POST /v1/api/admin/voice/preview — stored settings and options
# ---------------------------------------------------------------------------


def test_preview_uses_the_stored_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _install_preview(monkeypatch, _PreviewSpy(spans=_SPANS))
    body = (
        _client(settings=_STORED_SETTINGS)
        .post("/v1/api/admin/voice/preview", json={"text": "Call 120ms"})
        .json()
    )

    call = spy.calls[0]
    assert call["text"] == "Call 120ms"
    assert call["pronunciations"] == {"nginx": "engine ex", "sql": "sequel"}
    assert [(e.term, e.respelling) for e in call["dictionary"]] == [("SQL", "ess cue ell")]
    options = call["options"]
    assert options.disabled_groups == frozenset({"versions", "dates"})
    assert [r.find for r in options.rules] == ["ACME", "k8s"]
    assert [r.enabled for r in options.rules] == [True, False]
    assert options.builtin_terms_off == frozenset({"YAML", "JSON"})

    assert body == {
        "text": "<Call 120ms>",
        "spans": [
            {"start": 0, "end": 4, "source": "group", "label": "units"},
            {"start": 5, "end": 11, "source": "custom", "label": "custom 2"},
            {"start": 12, "end": 20, "source": "dictionary", "label": "dictionary"},
        ],
        "changes": 3,
        "normalize_enabled": True,
    }


def test_preview_without_a_config_store_uses_the_contract_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _install_preview(monkeypatch, _PreviewSpy())
    client = _client()
    client.app.state.config_store = None  # type: ignore[attr-defined]
    body = client.post("/v1/api/admin/voice/preview", json={"text": "hi"}).json()
    assert spy.calls[0]["options"].disabled_groups == frozenset()
    assert spy.calls[0]["options"].rules == ()
    assert spy.calls[0]["options"].builtin_terms_off == frozenset()
    assert spy.calls[0]["pronunciations"] == {}
    assert spy.calls[0]["dictionary"] == []
    # The registry default for audio.tts_normalize is on.
    assert body["normalize_enabled"] is True


def test_preview_reports_normalize_off_but_still_previews(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _install_preview(monkeypatch, _PreviewSpy())
    body = (
        _client(settings={**_STORED_SETTINGS, "audio.tts_normalize": False})
        .post("/v1/api/admin/voice/preview", json={"text": "hi"})
        .json()
    )
    assert body["normalize_enabled"] is False
    assert len(spy.calls) == 1


def test_preview_draft_rules_replace_the_stored_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _install_preview(monkeypatch, _PreviewSpy())
    draft = [
        {"find": "R9700", "replace": "R nine seven hundred", "test": "R9700", "enabled": True},
        {"find": "B70", "replace": "", "enabled": False},
    ]
    _client(settings=_STORED_SETTINGS).post(
        "/v1/api/admin/voice/preview", json={"text": "hi", "rules": draft}
    )
    options = spy.calls[0]["options"]
    assert [r.find for r in options.rules] == ["R9700", "B70"]
    assert [r.replace for r in options.rules] == ["R nine seven hundred", ""]
    assert [r.enabled for r in options.rules] == [True, False]
    # Everything not overridden still comes from storage.
    assert options.disabled_groups == frozenset({"versions", "dates"})
    assert options.builtin_terms_off == frozenset({"YAML", "JSON"})


def test_preview_only_rules_skips_groups_terms_and_pronunciations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _install_preview(monkeypatch, _PreviewSpy())
    draft = [{"find": "R9700", "replace": "red"}]
    _client(settings=_STORED_SETTINGS).post(
        "/v1/api/admin/voice/preview",
        json={"text": "R9700", "rules": draft, "only_rules": True},
    )
    call = spy.calls[0]
    assert [r.find for r in call["options"].rules] == ["R9700"]
    assert call["options"].disabled_groups == CLEANUP_GROUP_IDS
    assert call["options"].builtin_terms_off == _ALL_TERMS
    assert call["pronunciations"] == {}
    assert call["dictionary"] == []


def test_preview_only_rules_without_draft_uses_the_stored_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spy = _install_preview(monkeypatch, _PreviewSpy())
    _client(settings=_STORED_SETTINGS).post(
        "/v1/api/admin/voice/preview", json={"text": "hi", "only_rules": True}
    )
    call = spy.calls[0]
    assert [r.find for r in call["options"].rules] == ["ACME", "k8s"]
    assert call["options"].disabled_groups == CLEANUP_GROUP_IDS
    assert call["options"].builtin_terms_off == _ALL_TERMS
    assert call["pronunciations"] == {}
    assert call["dictionary"] == []


def test_preview_changes_is_the_span_count_and_none_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_preview(monkeypatch, _PreviewSpy(spans=_SPANS))
    body = _client().post("/v1/api/admin/voice/preview", json={"text": "hi"}).json()
    assert body["changes"] == len(body["spans"]) == 3

    _install_preview(monkeypatch, _PreviewSpy())
    body = _client().post("/v1/api/admin/voice/preview", json={"text": "hi"}).json()
    assert body["spans"] == []
    assert body["changes"] == 0


def test_preview_ignores_unknown_body_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    spy = _install_preview(monkeypatch, _PreviewSpy())
    resp = _client().post(
        "/v1/api/admin/voice/preview", json={"text": "hi", "unexpected": "ignored"}
    )
    assert resp.status_code == 200
    assert len(spy.calls) == 1


def test_default_profile_json_is_complete() -> None:
    # The profile the meta page edits and the player reads are the same shape;
    # this pins the field list another worker's UI renders against.
    from turnstone.core.voice_config import profile_json

    assert list(profile_json(DEFAULT_PROFILE)) == [f.key for f in PROFILE_FIELDS]
