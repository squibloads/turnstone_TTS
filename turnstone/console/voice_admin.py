"""Admin › Voice tuning endpoints: the meta table and the text-cleanup preview.

``GET /v1/api/admin/voice/meta`` serves the static half of the page — the
cleanup groups, the built-in terms, the profile field table and the model
aliases that can hold a profile — so the UI never keeps a second copy of the
contract.  ``POST /v1/api/admin/voice/preview`` runs the server's cleanup over
draft text and returns exactly what the TTS model would receive, which is the
only way the Text cleanup tab can show the effect the read-aloud path will
have: the rules run on the server, so a browser-side imitation would drift.

Both routes are console-only: Admin › Voice is a console panel and the
``audio.*`` settings it edits are console-scoped.  Both take the settings
permission, the same gate as the settings endpoints the page already writes
through.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.requests import Request

# The preview box is a test line, not a document.  The cap bounds the work one
# request can ask for (every cleanup rule is linear in the text, and the
# preview runs all of them); it is fixed by the HTTP contract.
MAX_PREVIEW_CHARS = 5000


def _raw_setting(config_store: Any, key: str) -> str:
    """One stored string setting, or ``""`` when absent or unusable.

    The voice parsers all take a string and degrade bad input to "not
    configured".  A missing config store (a bare console) must read the same
    way, and a value of the wrong type must not raise into a 500.
    """
    value = config_store.get(key) if config_store is not None else None
    return value if isinstance(value, str) else ""


async def admin_voice_meta(request: Request) -> JSONResponse:
    """GET /v1/api/admin/voice/meta — the tables the Voice page renders."""
    from turnstone.core.audio import model_supports_role, resolve_role_alias
    from turnstone.core.auth import require_permission
    from turnstone.core.speech_text import preview_for_speech
    from turnstone.core.voice_config import (
        BUILTIN_TERMS,
        CLEANUP_GROUP_IDS,
        CLEANUP_GROUPS,
        MAX_MARKER_CHARS,
        MAX_PROFILES,
        MAX_REWRITE_RULES,
        MAX_RULE_FIND_CHARS,
        MAX_RULE_REPLACE_CHARS,
        MAX_RULE_TEST_CHARS,
        PROFILE_FIELDS,
        VOICE_DOCS_URL,
        CleanupOptions,
    )

    err = require_permission(request, "admin.settings")
    if err:
        return err

    config_store = getattr(request.app.state, "config_store", None)
    registry = getattr(request.app.state, "coord_registry", None)

    # Each group's example is shown with every other group and every built-in
    # term off, so the sentence demonstrates that group alone.
    terms_off = frozenset(term for term, _ in BUILTIN_TERMS)
    groups: list[dict[str, Any]] = []
    for group in CLEANUP_GROUPS:
        options = CleanupOptions(
            disabled_groups=CLEANUP_GROUP_IDS - {group.id},
            rules=(),
            builtin_terms_off=terms_off,
        )
        groups.append(
            {
                "id": group.id,
                "label": group.label,
                "description": group.description,
                "example": group.example,
                "result": preview_for_speech(group.example, options=options).text,
            }
        )

    # The aliases the TTS role could resolve to, in registry order, with the
    # one it does resolve to now marked.  The same gate the role uses, so a
    # profile can only be edited for a model read-aloud would actually pick.
    active_alias = resolve_role_alias(config_store=config_store, registry=registry, role="tts")
    tts_models: list[dict[str, Any]] = []
    if registry is not None:
        for alias in registry.list_aliases():
            if model_supports_role(registry.get_config(alias), "tts"):
                tts_models.append({"alias": alias, "active": alias == active_alias})

    return JSONResponse(
        {
            "groups": groups,
            "builtin_terms": [
                {"term": term, "respelling": respelling} for term, respelling in BUILTIN_TERMS
            ],
            "profile_fields": [
                {
                    "key": field.key,
                    "group": field.group,
                    "kind": field.kind,
                    "default": field.default,
                    "label": field.label,
                    "help": field.help,
                    "reference_url": f"{VOICE_DOCS_URL}#{field.anchor}",
                    "min": field.min_value,
                    "max": field.max_value,
                    "choices": list(field.choices),
                    "unit": field.unit,
                }
                for field in PROFILE_FIELDS
            ],
            "tts_models": tts_models,
            "limits": {
                "max_rules": MAX_REWRITE_RULES,
                "max_find_chars": MAX_RULE_FIND_CHARS,
                "max_replace_chars": MAX_RULE_REPLACE_CHARS,
                "max_test_chars": MAX_RULE_TEST_CHARS,
                "max_profiles": MAX_PROFILES,
                "max_marker_chars": MAX_MARKER_CHARS,
            },
        }
    )


async def admin_voice_preview(request: Request) -> JSONResponse:
    """POST /v1/api/admin/voice/preview — clean draft text for the editor.

    ``rules``, when present, is a draft list in the stored shape and replaces
    the stored rules for this call only.  ``only_rules`` is the rule editor's
    test line: the draft (or stored) rules alone, with the groups, built-in
    terms, legacy pronunciations and dictionary out of the way.
    """
    from turnstone.core.auth import require_permission
    from turnstone.core.speech_text import parse_pronunciations, preview_for_speech
    from turnstone.core.voice_config import (
        BUILTIN_TERMS,
        CLEANUP_GROUP_IDS,
        CleanupOptions,
        cleanup_options,
        parse_dictionary,
        parse_rewrite_rules,
        validate_rewrite_rules,
    )
    from turnstone.core.web_helpers import read_json_or_400

    err = require_permission(request, "admin.settings")
    if err:
        return err

    body = await read_json_or_400(request)
    if isinstance(body, JSONResponse):
        return body

    text = body.get("text")
    if not isinstance(text, str):
        return JSONResponse({"error": "text is required"}, status_code=400)
    if len(text) > MAX_PREVIEW_CHARS:
        return JSONResponse(
            {"error": f"text is too long (max {MAX_PREVIEW_CHARS} characters)"},
            status_code=400,
        )

    draft: list[Any] | None = None
    if "rules" in body:
        if not isinstance(body["rules"], list):
            return JSONResponse({"error": "rules must be a list"}, status_code=400)
        draft = body["rules"]

    only_rules = False
    if "only_rules" in body:
        if not isinstance(body["only_rules"], bool):
            return JSONResponse({"error": "only_rules must be true or false"}, status_code=400)
        only_rules = body["only_rules"]

    config_store = getattr(request.app.state, "config_store", None)

    def read(key: str) -> str:
        return _raw_setting(config_store, key)

    # The tolerant parsers, not the validators: a stored value that predates a
    # rule change must preview as "that entry is ignored", exactly as it will
    # sound.
    stored = cleanup_options(read)
    rules = stored.rules
    if draft is not None:
        try:
            canonical = validate_rewrite_rules(json.dumps(draft))
        except ValueError as exc:
            # The editor shows this message inline beside the offending rule.
            return JSONResponse({"error": str(exc)}, status_code=400)
        rules = tuple(parse_rewrite_rules(canonical))

    pronunciations = parse_pronunciations(read("audio.tts_pronunciations"))
    dictionary = parse_dictionary(read("audio.voice_dictionary"))

    if only_rules:
        options = CleanupOptions(
            disabled_groups=CLEANUP_GROUP_IDS,
            rules=rules,
            builtin_terms_off=frozenset(term for term, _ in BUILTIN_TERMS),
        )
        pronunciations = {}
        dictionary = []
    else:
        options = CleanupOptions(
            disabled_groups=stored.disabled_groups,
            rules=rules,
            builtin_terms_off=stored.builtin_terms_off,
        )

    # The preview runs whatever the master switch says: an admin needs to see
    # the effect before switching audio.tts_normalize on.
    preview = preview_for_speech(
        text,
        pronunciations=pronunciations,
        dictionary=dictionary,
        options=options,
    )
    spans = [
        {"start": span.start, "end": span.end, "source": span.source, "label": span.label}
        for span in preview.spans
    ]
    normalize_enabled = config_store is None or bool(config_store.get("audio.tts_normalize"))
    return JSONResponse(
        {
            "text": preview.text,
            "spans": spans,
            "changes": len(spans),
            "normalize_enabled": normalize_enabled,
        }
    )
