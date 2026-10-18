"""Documentation completeness for Admin › Voice.

``docs/voice.md`` is the landing page for every ``audio.*`` setting's "learn
more" link and for the pacing profile fields' anchors. A setting that names an
anchor this file does not publish is a dead link in the admin UI, so the tests
here derive GitHub's heading slugs from the document and check every link and
every named group/field against them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from turnstone.core.settings_registry import SETTINGS
from turnstone.core.voice_config import CLEANUP_GROUPS, PROFILE_FIELDS, VOICE_DOCS_URL

_ROOT = Path(__file__).resolve().parents[1]
_VOICE_DOC = _ROOT / "docs" / "voice.md"
_SETTINGS_DOC = _ROOT / "docs" / "settings.md"

# An opening or closing fence: three or more backticks or tildes.
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_ATX = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_CODE = re.compile(r"`([^`]*)`")
_TAG = re.compile(r"<[^>]*>")
# GitHub keeps word characters, hyphens and spaces; everything else is dropped.
_PUNCT = re.compile(r"[^\w\s-]", re.UNICODE)


def github_slug(heading: str) -> str:
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces -> hyphens."""
    text = _LINK.sub(r"\1", heading)  # a link contributes its label
    text = _CODE.sub(r"\1", text)  # a code span contributes its text
    text = _TAG.sub("", text)
    text = text.strip().lower()
    text = _PUNCT.sub("", text)
    return re.sub(r"\s", "-", text)


def heading_slugs(markdown: str) -> list[str]:
    """Every ATX heading slug, ignoring headings inside fenced code blocks."""
    slugs: list[str] = []
    fence: str | None = None
    for line in markdown.splitlines():
        opener = _FENCE.match(line)
        if opener:
            marker = opener.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        match = _ATX.match(line)
        if match:
            slugs.append(github_slug(match.group(2)))
    return slugs


def _doc_text() -> str:
    assert _VOICE_DOC.is_file(), f"missing {_VOICE_DOC}"
    return _VOICE_DOC.read_text(encoding="utf-8")


def _doc_slugs() -> set[str]:
    return set(heading_slugs(_doc_text()))


# -- the slug function --------------------------------------------------------


@pytest.mark.parametrize(
    ("heading", "slug"),
    [
        ("Pacing", "pacing"),
        ("Tuning pacing for a new TTS model", "tuning-pacing-for-a-new-tts-model"),
        ("Text cleanup: rules & groups", "text-cleanup-rules--groups"),
    ],
)
def test_slug_function_matches_github(heading: str, slug: str) -> None:
    assert github_slug(heading) == slug


# -- docs links ---------------------------------------------------------------


def test_every_audio_setting_has_help_and_a_resolving_docs_anchor() -> None:
    """Every audio.* SettingDef explains itself and links to a real heading."""
    slugs = _doc_slugs()
    prefix = VOICE_DOCS_URL + "#"
    audio = {key: d for key, d in SETTINGS.items() if key.startswith("audio.")}
    assert audio, "positive control: the registry has audio settings"
    problems: list[str] = []
    for key, defn in audio.items():
        if not defn.help.strip():
            problems.append(f"{key}: empty help")
        if not defn.reference_url.startswith(prefix):
            problems.append(f"{key}: reference_url {defn.reference_url!r} lacks {prefix!r}")
            continue
        anchor = defn.reference_url[len(prefix) :]
        if anchor not in slugs:
            problems.append(f"{key}: anchor #{anchor} is not a heading in docs/voice.md")
    assert not problems, "\n".join(problems)


def test_every_profile_field_anchor_is_a_heading() -> None:
    slugs = _doc_slugs()
    assert PROFILE_FIELDS, "positive control: the profile has fields"
    missing = [f"{f.key} -> #{f.anchor}" for f in PROFILE_FIELDS if f.anchor not in slugs]
    assert not missing, f"profile anchors with no heading: {missing}"


def test_every_profile_field_is_documented() -> None:
    text = _doc_text()
    missing = [f.key for f in PROFILE_FIELDS if f.key not in text]
    assert not missing, f"profile fields not documented: {missing}"


def test_every_audio_setting_and_cleanup_group_is_named() -> None:
    text = _doc_text()
    missing = [
        key.rsplit(".", 1)[-1]
        for key in SETTINGS
        if key.startswith("audio.") and key.rsplit(".", 1)[-1] not in text
    ]
    missing += [group.id for group in CLEANUP_GROUPS if group.id not in text]
    assert not missing, f"settings/groups missing from docs/voice.md: {missing}"


def test_settings_doc_links_to_voice_doc() -> None:
    text = _SETTINGS_DOC.read_text(encoding="utf-8")
    assert re.search(r"\]\(voice\.md(?:#[^)]+)?\)", text), "docs/settings.md must link to voice.md"


def test_voice_doc_headings_are_unique() -> None:
    slugs = heading_slugs(_doc_text())
    duplicates = sorted({slug for slug in slugs if slugs.count(slug) > 1})
    assert not duplicates, f"duplicate heading slugs make anchors ambiguous: {duplicates}"
