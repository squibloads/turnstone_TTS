"""Pins the names and settings the voice run-4 slices build on.

The report queue, auto-stop, and streaming read-aloud are built in parallel;
fixing their shared vocabulary first keeps the slices agreeing at merge.
"""

from __future__ import annotations

import pytest

from turnstone.core.settings_registry import SETTINGS
from turnstone.core.voice_config import VOICE_DOCS_URL, VOICE_REPORT_STATES


def test_report_states() -> None:
    assert VOICE_REPORT_STATES == ("pending", "offered", "read", "dismissed")


@pytest.mark.parametrize(
    ("key", "default", "anchor"),
    [
        ("coordinator.voice_report_chime", False, "#agent-reports"),
        ("audio.stt_auto_stop", False, "#auto-stop"),
        ("audio.stt_silence_ms", 900, "#auto-stop"),
        ("audio.stt_vad_threshold", 0.5, "#auto-stop"),
        ("audio.tts_stream", False, "#streaming-read-aloud"),
        ("audio.voice_mode_enabled", True, "#voice-mode"),
    ],
)
def test_settings(key: str, default: object, anchor: str) -> None:
    sd = SETTINGS[key]
    assert sd.default == default
    assert sd.help
    assert sd.reference_url == VOICE_DOCS_URL + anchor
