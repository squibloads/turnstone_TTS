"""The spectrum strip's stylesheet contract (``coordinator.css``).

The strip is a fixed-height band above the CHILDREN list.  These pin the rules
the layout depends on: the band is a fixed 48px so showing it cannot reflow the
sidebar, its colours are existing console tokens and not a new palette, and it
carries no emoji.  The coordinator page must load the stylesheet that holds it.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_COORD_CSS = _ROOT / "turnstone/console/static/coordinator/coordinator.css"
_BASE_CSS = _ROOT / "turnstone/shared_static/base.css"
_COORD_INDEX = _ROOT / "turnstone/console/static/coordinator/index.html"


def _rule(css: str, selector: str) -> str:
    """The body of the first rule whose selector list contains ``selector``."""
    m = re.search(r"(^|\n)\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"no rule for {selector} in coordinator.css"
    return m.group(2)


def test_the_strip_is_a_fixed_48px_band_so_showing_it_cannot_reflow_the_sidebar() -> None:
    css = _COORD_CSS.read_text(encoding="utf-8")
    body = _rule(css, ".voice-viz")
    assert re.search(r"height:\s*48px\s*;", body), body
    # A flex-shrink-proof height: the strip must keep its band even when the
    # sidebar is tight, or it would squeeze the children list instead.
    assert "flex-shrink: 0" in body or "flex-shrink:0" in body, body


def test_the_strip_colours_are_console_tokens_and_never_a_new_palette() -> None:
    css = _COORD_CSS.read_text(encoding="utf-8")
    # The canvas is coloured per frame from the speaker tokens, so the stylesheet
    # only needs the border and background from the existing set.
    body = _rule(css, ".voice-viz")
    for colour in re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", body):
        raise AssertionError(f"hard-coded colour {colour} in the strip rule")
    assert "var(--hair)" in body or "var(--panel)" in body, body


def test_the_strip_carries_no_emoji() -> None:
    css = _COORD_CSS.read_text(encoding="utf-8")
    emoji = re.compile("[\U0001f300-\U0001faff\u2600-\u27bf]")
    assert not emoji.search(css), "emoji in coordinator.css"


def test_the_coordinator_page_loads_the_stylesheet_that_holds_the_strip() -> None:
    html = _COORD_INDEX.read_text(encoding="utf-8")
    assert 'href="/static/coordinator/coordinator.css"' in html
