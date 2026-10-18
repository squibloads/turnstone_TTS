"""governance.js: the Admin › Memories scope target cell.

The cell shows the server-supplied human name when there is one.  An unknown
or deleted user makes the server fall back to the raw ``scope_id``, and a full
UUID would swamp the column — so the cell shortens it while the full id stays
in ``title`` for hover.
"""

from __future__ import annotations

from pathlib import Path

from tests._js_harness_helpers import extract_braced, node_skip, run_node_source

pytestmark = node_skip

_GOVERNANCE = (
    Path(__file__).resolve().parents[1] / "turnstone" / "console" / "static" / "governance.js"
)

_UUID = "5f0c2a9e-1b3d-4c5e-8f90-abcdef012345"


def _run(signatures: list[str], body: str) -> None:
    source = _GOVERNANCE.read_text(encoding="utf-8")
    sliced = "\n".join(extract_braced(source, signature) for signature in signatures)
    result = run_node_source("import assert from 'node:assert/strict';\n" + sliced + "\n" + body)
    assert result.returncode == 0, result.stderr


def test_scope_target_uses_the_name_and_shortens_a_fallback_id() -> None:
    _run(
        ["function _memoryScopeTarget(m) {"],
        f"""
const uuid = "{_UUID}";
// The server resolved the owner: the name is shown and the full id is the title.
assert.deepEqual(
  _memoryScopeTarget({{scope: "user", scope_id: "u1", scope_label: "alice"}}),
  {{text: "alice", title: "u1"}},
);
// Voice memories are user-keyed exactly like user / coordinator ones.
assert.deepEqual(
  _memoryScopeTarget({{scope: "voice", scope_id: "u1", scope_label: "alice"}}),
  {{text: "alice", title: "u1"}},
);
// Unknown / deleted user: the server falls back to the id, so the cell keeps
// only its first 8 characters but the title still carries the whole id.
assert.deepEqual(
  _memoryScopeTarget({{scope: "user", scope_id: uuid, scope_label: uuid}}),
  {{text: uuid.slice(0, 8), title: uuid}},
);
assert.deepEqual(
  _memoryScopeTarget({{scope: "user", scope_id: uuid}}),
  {{text: uuid.slice(0, 8), title: uuid}},
);
// Global scope has no target, so the renderer draws no cell at all.
assert.deepEqual(_memoryScopeTarget({{scope: "global", scope_id: ""}}), {{text: "", title: ""}});
""",
    )


def test_the_scope_cell_shows_the_short_id_and_titles_the_full_one() -> None:
    _run(
        ["function _memoryScopeTarget(m) {", "function _renderAdminMemories(items, total) {"],
        f"""
const uuid = "{_UUID}";
const rendered = [];
const escapeHtml = (s) =>
  String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
const setSafeHtml = (el, html) => rendered.push(html);
const _relativeTime = () => "now";
const _kebabMenu = () => "";
globalThis.document = {{
  getElementById: () => ({{ querySelectorAll: () => [] }}),
}};

_renderAdminMemories(
  [
    {{memory_id: "m1", type: "user", name: "named", scope: "user",
      scope_id: "u1", scope_label: "alice", description: "", updated: ""}},
    {{memory_id: "m2", type: "user", name: "unknown", scope: "voice",
      scope_id: uuid, scope_label: uuid, description: "", updated: ""}},
    {{memory_id: "m3", type: "user", name: "global", scope: "global",
      scope_id: "", scope_label: "", description: "", updated: ""}},
  ],
  3,
);
const html = rendered[0];
assert.ok(html.includes('title="u1">alice</span>'), "a resolved name is the cell text");
assert.ok(
  html.includes('title="' + uuid + '">' + uuid.slice(0, 8) + "</span>"),
  "an unresolved id is shortened but the title keeps all of it",
);
assert.equal(
  (html.match(/class="mem-scope-target"/g) || []).length,
  2,
  "global scope renders no target cell",
);
""",
    )
