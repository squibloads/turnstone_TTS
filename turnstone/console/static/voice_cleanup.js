/* Admin › Voice › Text cleanup — the Text cleanup tab.

   The reply read aloud passes through three stages, in order: the built-in
   cleanup groups (`audio.tts_cleanup_disabled` switches them off), the custom
   rewrite rules (`audio.tts_rewrite_rules`, an ordered JSON list), then the
   voice dictionary (its own tab).  Later stages win: a dictionary entry
   overrides a custom rule, and a custom rule overrides a group.

   `audio.tts_normalize` is the master switch.  With it off the TTS model gets
   the reply as written; the preview still runs, so an admin can see the
   difference before switching it on.  The preview and the per-rule examples
   call the server (`POST /v1/api/admin/voice/preview`) because the rules are
   Python `re` on the server: what this tab shows is what is spoken.

   Classic script: exposes window.loadVoiceCleanup(container), called by the
   Voice tab on every show, and window.TurnstoneVoiceCleanup for the pure
   helpers (and the node harness).  Pairs with voice_cleanup.css.

   Mounting: the Voice shell adds voice_cleanup.css and this script, then
   calls loadVoiceCleanup(container) whenever the Text cleanup tab is shown.
   The loader owns one .voice-cleanup-root child of that container and builds
   its shelf inside the nearest .hatch-host, exactly like the Dictionary and
   Styles editors. */

(function () {
  "use strict";

  const SETTINGS_URL = "/v1/api/admin/settings";
  const SCHEMA_URL = "/v1/api/admin/settings/schema";
  const META_URL = "/v1/api/admin/voice/meta";
  const PREVIEW_URL = "/v1/api/admin/voice/preview";
  const NORMALIZE_KEY = "audio.tts_normalize";
  const RULES_KEY = "audio.tts_rewrite_rules";
  const DISABLED_KEY = "audio.tts_cleanup_disabled";

  // Mirrors turnstone/core/voice_config.py: voice_config.VOICE_DOCS_URL is
  // the same URL, and MAX_REWRITE_RULES / MAX_RULE_*_CHARS are the defaults
  // below.  The server's strict validators remain the authority; the meta
  // endpoint's `limits` block overrides these when it loads.
  const VOICE_DOCS_URL =
    "https://github.com/turnstonelabs/turnstone/blob/main/docs/voice.md";
  const DEFAULT_LIMITS = {
    max_rules: 100,
    max_find_chars: 500,
    max_replace_chars: 500,
    max_test_chars: 500,
  };

  // A sample that exercises several groups, the dictionary (built-in terms
  // included) and a ticket id a custom rule commonly targets.  It runs through
  // the real endpoint, so it reflects the operator's own settings.
  const SAMPLE_TEXT =
    "Deploy v1.8.5 to the 32GB box by 2026-10-16, then call 303-555-0142 " +
    "and edit config.yaml.\n\n" +
    "Ping YAML and kubectl about ticket DT-1412; ~5 users are affected, " +
    "e.g. a 90s timeout.";

  const PLAIN_FIND_HINT =
    "Type the word or phrase exactly as it appears. Matches whole words and ignores case: \u201ck8s\u201d also matches \u201cK8s\u201d, but not \u201ck8sctl\u201d.";
  const REGEX_FIND_HINT =
    "Find is now a regular expression (Python flavor), case-sensitive; start with (?i) to ignore case. \\1, \\2 in Replace with insert captured groups. It must compile and must not match empty text.";

  const MASTER_FALLBACK_HELP =
    "When on, the built-in rule groups, your custom rewrite rules and the " +
    "voice dictionary rewrite the reply into speakable words before it reaches " +
    "the TTS model. Turn it off for non-English replies or a model that " +
    "already handles this itself.";

  // The pure helpers are exposed for the node harness.  They take every
  // variable as an argument (limits, groups, rules) so a test never has to
  // mount the DOM.
  function _text(value) {
    return typeof value === "string" ? value.trim() : "";
  }

  function _isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function normalizeLimits(limits) {
    const source = _isObject(limits) ? limits : {};
    const out = {};
    const keys = Object.keys(DEFAULT_LIMITS);
    for (let i = 0; i < keys.length; i++) {
      const key = keys[i];
      const value = source[key];
      out[key] =
        typeof value === "number" &&
        isFinite(value) &&
        value > 0 &&
        Math.floor(value) === value
          ? value
          : DEFAULT_LIMITS[key];
    }
    return out;
  }

  /** Tolerant read, mirroring parse_rewrite_rules: bad JSON -> [], bad rules
   *  skipped, the cap keeps the first N.  A regex is not compiled here —
   *  JavaScript cannot compile Python `re`; the preview endpoint does that,
   *  and a stored rule it rejects is still shown so an admin can fix it. */
  function parseRules(raw, limits) {
    const lim = normalizeLimits(limits);
    let items;
    try {
      items = JSON.parse(raw);
    } catch (e) {
      return [];
    }
    if (!Array.isArray(items)) return [];
    const out = [];
    for (let i = 0; i < items.length && i < lim.max_rules; i++) {
      const item = items[i];
      if (!_isObject(item)) continue;

      const find = _text(item.find);
      if (!find || find.length > lim.max_find_chars) continue;

      let regex = false;
      if (item.regex !== undefined) {
        if (typeof item.regex !== "boolean") continue;
        regex = item.regex;
      }

      let replace = item.replace;
      if (replace === undefined || replace === null) replace = "";
      if (typeof replace !== "string" || replace.length > lim.max_replace_chars) {
        continue;
      }

      const test = _text(item.test);
      if (test.length > lim.max_test_chars) continue;

      let enabled = true;
      if (item.enabled !== undefined) {
        if (typeof item.enabled !== "boolean") continue;
        enabled = item.enabled;
      }

      out.push({
        find: find,
        regex: regex,
        replace: replace,
        test: test,
        enabled: enabled,
      });
    }
    return out;
  }

  /** Canonical wire shape, field order included — the server canonicalises
   *  too, but a stable body keeps the PUT easy to read in an audit log. */
  function serializeRules(rules) {
    return JSON.stringify(
      (rules || []).map(function (rule) {
        return {
          find: rule.find,
          regex: rule.regex === true,
          replace: rule.replace || "",
          test: rule.test || "",
          enabled: rule.enabled !== false,
        };
      }),
    );
  }

  /** A rewrite rule as the preview endpoint's draft item (enabled forced on:
   *  the test line demonstrates what the pattern does, whether or not the
   *  rule is switched on). */
  function _previewRule(rule) {
    return {
      find: rule.find,
      regex: rule.regex === true,
      replace: rule.replace || "",
      test: rule.test || "",
      enabled: true,
    };
  }

  function previewBody(text, rules, onlyRules) {
    const body = { text: text };
    if (rules) {
      body.rules = rules;
      body.only_rules = !!onlyRules;
    }
    return body;
  }

  /** The editor's live test: one draft rule, nothing else.  A rule with no
   *  test line still needs a text to validate against, so use a neutral
   *  sample and let the caller skip painting the result. */
  function draftPreviewBody(rule) {
    const test = _text(rule.test);
    return {
      text: test || "sample",
      rules: [_previewRule(rule)],
      only_rules: true,
    };
  }

  function validateRuleDraft(draft, limits) {
    const lim = normalizeLimits(limits);
    const source = draft || {};
    const find = _text(source.find);
    if (!find) return "Find is required.";
    if (find.length > lim.max_find_chars) {
      return "Find is longer than " + lim.max_find_chars + " characters.";
    }
    const replace =
      source.replace === undefined || source.replace === null
        ? ""
        : String(source.replace);
    if (replace.length > lim.max_replace_chars) {
      return (
        "Replace with is longer than " + lim.max_replace_chars + " characters."
      );
    }
    const test = _text(source.test);
    if (test.length > lim.max_test_chars) {
      return "The test line is longer than " + lim.max_test_chars + " characters.";
    }
    return "";
  }

  /** Mirrors voice_config.parse_disabled_groups: a list of known group ids. */
  function parseDisabled(raw, knownIds) {
    let items;
    try {
      items = JSON.parse(raw);
    } catch (e) {
      return [];
    }
    if (!Array.isArray(items)) return [];
    const allowed = Array.isArray(knownIds) ? knownIds : null;
    const seen = Object.create(null);
    const out = [];
    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      if (typeof item !== "string" || !item) continue;
      if (seen[item]) continue;
      if (allowed && allowed.indexOf(item) === -1) continue;
      seen[item] = true;
      out.push(item);
    }
    return out;
  }

  /** The whole `audio.tts_cleanup_disabled` list after one switch flips. */
  function toggleDisabled(disabled, id, enabled) {
    const out = (disabled || []).slice();
    const at = out.indexOf(id);
    if (enabled) {
      if (at !== -1) out.splice(at, 1);
    } else if (at === -1) {
      out.push(id);
    }
    return out;
  }

  function matchCountLabel(count, removed) {
    if (!count) return "No match in the test line";
    const label = count === 1 ? "1 match" : count + " matches";
    return removed ? label + " \u00b7 removed" : label;
  }

  function changesLabel(count) {
    return count === 1 ? "1 change" : count + " changes";
  }

  function ruleTypeLabel(rule) {
    return rule && rule.regex === true ? "regex" : "text";
  }

  function exampleText(rule, result) {
    if (!rule || !_text(rule.test)) return "\u2014";
    const prefix = _text(rule.test) + " \u2192 ";
    return prefix + (result === undefined || result === null ? "\u2026" : result);
  }

  /** The prefilled sample, extended with one enabled rule's test line when
   *  the operator has a custom rule: the brief's sample must exercise the
   *  three stages, and only the stored rule knows what its own example is. */
  function sampleFor(rules) {
    const text = SAMPLE_TEXT;
    const list = rules || [];
    for (let i = 0; i < list.length; i++) {
      const rule = list[i];
      if (!rule || rule.enabled === false) continue;
      const test = _text(rule.test);
      if (!test) continue;
      if (text.indexOf(test) !== -1) return text;
      return text + "\n\n" + test;
    }
    return text;
  }

  /** Position choices for the rule shelf.  `editIndex` is null on create;
   *  on edit the rule itself is excluded, so "After N" never means "after
   *  itself".  The value is the rule's final index in the saved list. */
  function positionOptions(rules, editIndex) {
    const current = rules || [];
    const editing =
      typeof editIndex === "number" &&
      editIndex >= 0 &&
      editIndex < current.length;
    const others = editing
      ? current.filter(function (_rule, i) {
          return i !== editIndex;
        })
      : current.slice();
    const out = [];
    for (let i = 0; i <= others.length; i++) {
      let label;
      if (i === 0) {
        label = others.length ? "First, before rule 1" : "First";
      } else {
        const before = others[i - 1];
        label = "After " + i + " \u00b7 " + (before.find || "");
        if (i === others.length) label += " (last)";
      }
      out.push({ value: i, label: label });
    }
    return out;
  }

  /** The next whole list: remove the edited rule, then insert at `position`. */
  function applyRuleAt(rules, editIndex, position, rule) {
    const out = (rules || []).slice();
    const editing =
      typeof editIndex === "number" &&
      editIndex >= 0 &&
      editIndex < out.length;
    if (editing) out.splice(editIndex, 1);
    let at = parseInt(position, 10);
    if (!isFinite(at) || at < 0) at = 0;
    if (at > out.length) at = out.length;
    out.splice(at, 0, rule);
    return out;
  }

  /** The AI prompt: every Turnstone-specific rule the model cannot know. */
  function buildAiPrompt(draft, limits) {
    const lim = normalizeLimits(limits);
    const source = draft || {};
    const find = _text(source.find);
    const replace =
      source.replace === undefined || source.replace === null
        ? ""
        : String(source.replace);
    const test = _text(source.test);
    const lines = [
      "Write one custom rewrite rule for Turnstone read-aloud (text-to-speech): a regular expression and its replacement.",
      "",
      "How Turnstone applies the rule:",
      "- Engine: Python `re`, applied with re.sub. In the replacement use \\1, \\2 or \\g<name>; named groups are (?P<name>...). Lookbehind must be fixed width.",
      "- Case-sensitive. Start the pattern with (?i) to ignore case.",
      "- It sees the reply as displayed, not the markdown source: no **, backticks or # heading marks. The built-in Text cleanup groups have already run.",
      "- It runs on one clip at a time (a paragraph, list item, heading or table row). ^ and $ mean the start and end of that clip; a match cannot span two clips.",
      "- Custom rules run top to bottom, each on the previous rule's output. Dictionary respellings run after all rules and override them.",
      "- The replacement is what the voice should SAY: write words, not symbols or markup. An empty replacement deletes the match.",
      "- The pattern must not be able to match empty text, and is at most " +
        lim.max_find_chars +
        " characters.",
      "",
      'What I want: <describe it, e.g. "read ticket ids like DT-1412 as D T 1412">',
      "Example line: " + (test || "<paste a line from a reply>"),
      "It should be read as: <how it should sound>",
    ];
    if (find) {
      lines.push(
        "My attempt so far: " + find + "  ->  " + (replace || "(delete)"),
      );
    }
    lines.push("");
    lines.push(
      "Reply with the pattern and the replacement on separate lines, then one sentence on what it matches and anything it might catch by mistake.",
    );
    return lines.join("\n");
  }

  function _safeUrl(url) {
    if (typeof url !== "string") return "";
    return /^https?:\/\//i.test(url) ? url : "";
  }

  /** Render output text with spans as <mark>s, escaping every slice first.
   *  `labelFor(span)` supplies the tooltip label.  Overlapping spans keep the
   *  first, and out-of-range spans are clipped: a buggy endpoint can make the
   *  preview ugly, never inject markup or duplicate text. */
  function highlightSegments(text, spans, labelFor) {
    const source = String(text === undefined || text === null ? "" : text);
    const list = (spans || [])
      .filter(function (span) {
        return (
          span &&
          typeof span.start === "number" &&
          typeof span.end === "number" &&
          isFinite(span.start) &&
          isFinite(span.end)
        );
      })
      .slice()
      .sort(function (a, b) {
        return a.start - b.start || a.end - b.end;
      });
    let out = "";
    let cursor = 0;
    for (let i = 0; i < list.length; i++) {
      const span = list[i];
      let start = Math.floor(span.start);
      let end = Math.floor(span.end);
      if (start < 0) start = 0;
      if (end > source.length) end = source.length;
      if (start >= source.length || end <= start || start < cursor) continue;
      const kind =
        span.source === "group" ||
        span.source === "custom" ||
        span.source === "dictionary"
          ? span.source
          : "group";
      const label =
        typeof labelFor === "function" ? labelFor(span) : span.label || kind;
      out += escapeHtml(source.slice(cursor, start));
      out +=
        '<mark class="voice-cleanup-hl voice-cleanup-hl--' +
        kind +
        '" title="' +
        escapeHtml(label) +
        '">' +
        escapeHtml(source.slice(start, end)) +
        "</mark>";
      cursor = end;
    }
    out += escapeHtml(source.slice(cursor));
    return out;
  }

  // -------------------------------------------------------------------------
  // Markup (pure)
  // -------------------------------------------------------------------------

  function _checked(value) {
    return value === true || value === "true" ? " checked" : "";
  }

  /** The same .settings-row + "?" popover structure admin.js's
   *  _renderSettingRow emits, without the dirty/save/reset column: this
   *  switch commits the moment it flips.  The document-delegated help
   *  handler in admin.js opens the popover. */
  function settingRowHtml(item) {
    const key = item && item.key ? String(item.key) : "";
    const shortKey = key.indexOf(".") !== -1 ? key.substring(key.indexOf(".") + 1) : key;
    const description = item && item.description ? String(item.description) : "";
    const help = item && item.help ? String(item.help) : "";
    const reference = _safeUrl(item && item.reference_url);
    const hasHelp = !!(help || reference);
    const helpId = escapeHtml(key) + "-help";

    let html = '<div class="settings-row" data-row-key="' + escapeHtml(key) + '">';
    html += '<div class="settings-label-col">';
    html += '<div class="settings-label">' + escapeHtml(shortKey);
    if (hasHelp) {
      html +=
        ' <button type="button" class="settings-help-btn" data-help-target="' +
        helpId +
        '" aria-label="Help for ' +
        escapeHtml(shortKey) +
        '" aria-expanded="false" title="More info"></button>';
    }
    html += "</div>";
    if (description) {
      html += '<div class="settings-desc">' + escapeHtml(description) + "</div>";
    }
    if (hasHelp) {
      html +=
        '<div id="' +
        helpId +
        '" class="settings-help-popover" style="display:none">';
      if (help) {
        html += '<span class="settings-help-text">' + escapeHtml(help) + "</span>";
      }
      if (reference) {
        html +=
          ' <a href="' +
          escapeHtml(reference) +
          '" target="_blank" rel="noopener" class="settings-help-ref">learn more</a>';
      }
      html += "</div>";
    }
    html += "</div>";
    html +=
      '<div class="settings-input"><label class="settings-toggle">' +
      '<input type="checkbox" data-setting-key="' +
      escapeHtml(key) +
      '" aria-label="' +
      escapeHtml(shortKey) +
      '"' +
      _checked(item && item.value) +
      '><span class="settings-toggle-slider"></span></label></div>';
    html += "</div>";
    return html;
  }

  function groupsTableHtml(groups, disabled, writable) {
    if (!groups || !groups.length) {
      return '<div class="dashboard-empty">No cleanup groups are available.</div>';
    }
    const off = Object.create(null);
    for (let i = 0; i < (disabled || []).length; i++) off[disabled[i]] = true;
    let html =
      '<div class="admin-colheaders voice-cleanup-groups-grid" aria-hidden="true">' +
      '<span class="admin-col">GROUP</span>' +
      '<span class="admin-col voice-cleanup-groups-desc">WHAT IT REWRITES</span>' +
      '<span class="admin-col">EXAMPLE</span>' +
      '<span class="admin-col voice-cleanup-group-on">ON</span>' +
      "</div>" +
      '<div role="list" aria-label="Cleanup rule groups" aria-live="polite">';
    for (let i = 0; i < groups.length; i++) {
      const group = groups[i];
      const id = String(group.id || "");
      const label = group.label || id;
      html +=
        '<div class="admin-row voice-cleanup-groups-grid" role="listitem">' +
        '<span class="admin-col"><code>' +
        escapeHtml(label) +
        "</code></span>" +
        '<span class="admin-col voice-cleanup-groups-desc">' +
        escapeHtml(group.description || "") +
        "</span>" +
        '<span class="admin-col voice-cleanup-example-cell"><code>' +
        escapeHtml(group.example || "") +
        "</code> \u2192 <code>" +
        escapeHtml(group.result || "") +
        "</code></span>" +
        '<span class="admin-col voice-cleanup-group-on">' +
        '<label class="settings-toggle"><input type="checkbox" data-vc-group="' +
        escapeHtml(id) +
        '" aria-label="' +
        escapeHtml(label + " cleanup group") +
        '"' +
        (off[id] ? "" : " checked") +
        (writable ? "" : " disabled") +
        '><span class="settings-toggle-slider"></span></label>' +
        "</span></div>";
    }
    return html + "</div>";
  }

  function rulesTableHtml(rules, writable, justAddedIndex, limits) {
    const lim = normalizeLimits(limits);
    const list = rules || [];
    let html =
      '<div class="admin-colheaders voice-cleanup-rules-grid" aria-hidden="true">' +
      '<span class="admin-col voice-cleanup-rule-num">#</span>' +
      '<span class="admin-col">FIND</span>' +
      '<span class="admin-col voice-cleanup-rule-type">TYPE</span>' +
      '<span class="admin-col voice-cleanup-rule-replace">REPLACE WITH</span>' +
      '<span class="admin-col voice-cleanup-rule-example">EXAMPLE</span>' +
      '<span class="admin-col voice-cleanup-rule-on">ON</span>' +
      (writable
        ? '<span class="admin-col admin-col-actions">ACTIONS</span>'
        : "") +
      "</div>";
    if (!list.length) {
      html +=
        '<div class="dashboard-empty">No custom rewrite rules yet.' +
        (writable
          ? " Use + New rule for anything the groups don\u2019t cover."
          : "") +
        "</div>";
      return html;
    }
    html +=
      '<div role="list" aria-label="Custom rewrite rules" aria-live="polite">';
    for (let i = 0; i < list.length; i++) {
      const rule = list[i];
      const type = ruleTypeLabel(rule);
      const isNew = i === justAddedIndex;
      const initial = rule.test
        ? rule.test + " \u2192 \u2026"
        : "\u2014";
      html +=
        '<div class="admin-row voice-cleanup-rules-grid' +
        (isNew ? " is-just-added" : "") +
        '" role="listitem">' +
        '<span class="admin-col voice-cleanup-rule-num">' +
        (i + 1) +
        "</span>" +
        '<span class="admin-col voice-cleanup-rule-find"><code>' +
        escapeHtml(rule.find) +
        "</code>" +
        (isNew ? '<span class="voice-cleanup-new-pill">just added</span>' : "") +
        "</span>" +
        '<span class="admin-col voice-cleanup-rule-type"><span class="voice-cleanup-pill voice-cleanup-pill--' +
        type +
        '">' +
        type +
        "</span></span>" +
        '<span class="admin-col voice-cleanup-rule-replace">' +
        (rule.replace
          ? escapeHtml(rule.replace)
          : '<span class="voice-cleanup-empty">\u2014</span>') +
        "</span>" +
        '<span class="admin-col voice-cleanup-rule-example" data-vc-example="' +
        i +
        '">' +
        escapeHtml(initial) +
        "</span>" +
        '<span class="admin-col voice-cleanup-rule-on">' +
        '<label class="settings-toggle"><input type="checkbox" data-vc-rule-toggle="' +
        i +
        '" aria-label="Rule ' +
        (i + 1) +
        ' enabled"' +
        (rule.enabled === false ? "" : " checked") +
        (writable ? "" : " disabled") +
        '><span class="settings-toggle-slider"></span></label>' +
        "</span>";
      if (writable) {
        html +=
          '<span class="admin-col admin-col-actions voice-cleanup-rule-actions">' +
          '<button type="button" class="admin-btn-action" data-vc-rule-edit="' +
          i +
          '" aria-label="Edit rule ' +
          (i + 1) +
          '">Edit</button>' +
          '<button type="button" class="admin-btn-danger" data-vc-rule-delete="' +
          i +
          '" aria-label="Delete rule ' +
          (i + 1) +
          '">Delete</button></span>';
      }
      html += "</div>";
    }
    html += "</div>";
    // A rule count is repeated in the footer for very long lists; the header
    // count lives beside the title, so this only pins the cap.
    if (list.length >= lim.max_rules) {
      html +=
        '<p class="label-hint hint-block">The list is at its ' +
        lim.max_rules +
        "-rule limit.</p>";
    }
    return html;
  }

  function _helpButton(id, label) {
    return (
      '<button type="button" class="settings-help-btn" data-help-target="' +
      id +
      '" aria-label="' +
      escapeHtml(label) +
      '" aria-expanded="false" title="More info"></button>'
    );
  }

  function _helpPopover(id, text, url) {
    let html =
      '<div id="' +
      id +
      '" class="settings-help-popover" style="display:none">' +
      '<span class="settings-help-text">' +
      escapeHtml(text) +
      "</span>";
    if (url) {
      html +=
        ' <a href="' +
        escapeHtml(url) +
        '" target="_blank" rel="noopener" class="settings-help-ref">learn more</a>';
    }
    return html + "</div>";
  }

  // -------------------------------------------------------------------------
  // DOM state
  // -------------------------------------------------------------------------

  let _mountSeq = 0;
  const _exampleCache = {};

  function _canWrite() {
    // Same contract as voice_dictionary.js: prefer admin.js's shim, then
    // auth.js's live check, then a mirror of its storage parse.  Absent means
    // deny.
    if (typeof _consoleHasPermission === "function") {
      return _consoleHasPermission("admin.settings");
    }
    if (typeof window.hasPermission === "function") {
      return window.hasPermission("admin.settings");
    }
    try {
      const perms = window.sessionStorage
        ? window.sessionStorage.getItem("turnstone_permissions") || ""
        : "";
      return perms.split(",").indexOf("admin.settings") !== -1;
    } catch (e) {
      return false;
    }
  }

  function _failFrom(response, fallback) {
    return response.json().then(
      function (data) {
        throw new Error((data && data.error) || fallback);
      },
      function () {
        throw new Error(fallback);
      },
    );
  }

  function putSetting(key, value) {
    return authFetch(SETTINGS_URL + "/" + encodeURIComponent(key), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: value }),
    }).then(function (r) {
      if (!r.ok) return _failFrom(r, "Save failed");
      return r.json();
    });
  }

  function _getJson(url, fallback) {
    return authFetch(url).then(function (r) {
      if (!r.ok) return _failFrom(r, fallback);
      return r.json();
    });
  }

  function _previewRequest(body) {
    return authFetch(PREVIEW_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (r) {
      return r.json().then(
        function (data) {
          if (!r.ok) {
            const err = new Error((data && data.error) || "Preview failed");
            err.status = r.status;
            throw err;
          }
          return data || {};
        },
        function () {
          if (!r.ok) {
            const err = new Error("Preview failed");
            err.status = r.status;
            throw err;
          }
          return {};
        },
      );
    });
  }

  function _limits(state) {
    return normalizeLimits(state.meta && state.meta.limits);
  }

  function _groups(state) {
    return (state.meta && state.meta.groups) || [];
  }

  function _groupIds(state) {
    const groups = _groups(state);
    const ids = [];
    for (let i = 0; i < groups.length; i++) ids.push(String(groups[i].id));
    return ids;
  }

  function _spanLabel(state, span) {
    if (!span) return "";
    if (span.source === "group") {
      const groups = _groups(state);
      for (let i = 0; i < groups.length; i++) {
        if (groups[i].id === span.label) return groups[i].label || span.label;
      }
      return span.label || "group";
    }
    if (span.source === "dictionary") return "dictionary";
    return span.label || "custom rule";
  }

  // -------------------------------------------------------------------------
  // Root layout
  // -------------------------------------------------------------------------

  function _rootMarkup() {
    return (
      '<div class="voice-cleanup-order">Runs in order, left to right like the tabs: ' +
      "<strong>rule groups</strong> \u2192 <strong>custom rules</strong> " +
      "\u2192 <strong>dictionary</strong> \u00b7 later steps win; pacing and " +
      "structure are set per model on the Settings tab.</div>" +
      '<div id="voice-cleanup-load-error" class="voice-cleanup-load-error" role="alert" hidden></div>' +
      '<section class="voice-cleanup-try" aria-labelledby="voice-cleanup-try-title">' +
      '<div class="voice-cleanup-try-box">' +
      '<label class="voice-cleanup-field-label" for="voice-cleanup-sample">' +
      "Try it \u00b7 paste a reply</label>" +
      '<textarea id="voice-cleanup-sample" rows="8" maxlength="5000" spellcheck="false" ' +
      'aria-describedby="voice-cleanup-sample-hint"></textarea>' +
      '<p class="label-hint hint-block" id="voice-cleanup-sample-hint">' +
      "The preview runs even while cleanup is off, so you can see the " +
      "difference.</p>" +
      '<div class="voice-cleanup-try-actions">' +
      '<button type="button" class="admin-btn-action" id="voice-cleanup-play">' +
      "\u25b6 Play</button>" +
      '<span class="voice-cleanup-play-hint">plays the original text, before ' +
      "cleanup</span></div></div>" +
      '<div class="voice-cleanup-try-box">' +
      '<div class="voice-cleanup-preview-head">' +
      '<span class="voice-cleanup-field-label" id="voice-cleanup-try-title">' +
      "What the TTS model receives</span>" +
      '<span class="voice-cleanup-changes" id="voice-cleanup-changes"></span>' +
      "</div>" +
      '<div class="voice-cleanup-preview" id="voice-cleanup-preview" ' +
      'aria-live="polite"></div>' +
      '<div class="voice-cleanup-legend" aria-label="Highlight legend">' +
      '<span><span class="voice-cleanup-swatch voice-cleanup-swatch--group"></span>group</span>' +
      '<span><span class="voice-cleanup-swatch voice-cleanup-swatch--custom"></span>custom rule</span>' +
      '<span><span class="voice-cleanup-swatch voice-cleanup-swatch--dictionary"></span>dictionary</span>' +
      "</div></div></section>" +
      '<div class="voice-cleanup-notice" id="voice-cleanup-notice" hidden></div>' +
      '<section class="voice-cleanup-section" aria-labelledby="voice-cleanup-master-title">' +
      '<div class="voice-cleanup-section-head">' +
      '<span class="section-header" id="voice-cleanup-master-title" ' +
      'style="margin:0">MASTER SWITCH</span></div>' +
      '<p class="voice-cleanup-lede">One switch for everything below. Off ' +
      "means the TTS model gets the reply exactly as written.</p>" +
      '<div id="voice-cleanup-master"></div></section>' +
      '<section class="voice-cleanup-section" aria-labelledby="voice-cleanup-groups-title">' +
      '<div class="voice-cleanup-section-head">' +
      '<span class="section-header" id="voice-cleanup-groups-title" ' +
      'style="margin:0">RULE GROUPS</span>' +
      _helpButton("voice-cleanup-groups-help", "How rule groups work") +
      "</div>" +
      '<p class="voice-cleanup-lede">Built in; turn off any your model already reads well.</p>' +
      _helpPopover(
        "voice-cleanup-groups-help",
        "Each group is a small set of built-in rules for one kind of technical " +
          "text. A group you switch off stops running, while every other group " +
          "keeps running. Turn a group off when your TTS model already reads " +
          "that kind of text well, or when a custom rule should own it.",
        VOICE_DOCS_URL + "#rule-groups",
      ) +
      '<div class="voice-cleanup-rules-wrap" id="voice-cleanup-groups"></div></section>' +
      '<section class="voice-cleanup-section" aria-labelledby="voice-cleanup-rules-title">' +
      '<div class="voice-cleanup-section-head">' +
      '<span class="section-header" id="voice-cleanup-rules-title" ' +
      'style="margin:0">CUSTOM REWRITE RULES</span>' +
      _helpButton("voice-cleanup-rules-help", "How custom rewrite rules work") +
      '<button type="button" class="admin-action-btn" id="voice-cleanup-new" ' +
      "hidden>+ New rule</button></div>" +
      '<p class="voice-cleanup-lede" id="voice-cleanup-rules-lede">Custom ' +
      'rewrite rules \u00b7 <span id="voice-cleanup-rule-count">0 of 100</span> ' +
      "\u00b7 for anything the groups don't cover</p>" +
      _helpPopover(
        "voice-cleanup-rules-help",
        "A custom rule replaces Find with Replace with. Text rules match whole " +
          "words and ignore case; regex rules use Python regular expressions " +
          "and can capture groups. Rules run top to bottom after the built-in " +
          "groups and before the dictionary, so a dictionary entry still wins.",
        VOICE_DOCS_URL + "#custom-rewrite-rules",
      ) +
      '<div class="voice-cleanup-rules-wrap" id="voice-cleanup-rules"></div>' +
      '<p class="label-hint hint-block voice-cleanup-footnote">' +
      "text = the words you typed, matched whole-word and ignoring case. " +
      "regex = an advanced rule written as a regular expression (Python " +
      "flavor). Read-aloud only: dictation uses the dictionary\u2019s " +
      "\u201calso hears\u201d.</p></section>"
    );
  }

  function _shelfMarkup(prefix) {
    return (
      '<dialog class="hatch hatch--shelf hatch--lg" id="' +
      prefix +
      '-shelf" data-kind="create" aria-labelledby="' +
      prefix +
      '-shelf-title">' +
      '<header class="sh-head">' +
      '<span class="sh-led" aria-hidden="true"></span>' +
      '<h2 class="sh-title" id="' +
      prefix +
      '-shelf-title">New rewrite rule</h2>' +
      '<span class="sh-tag" aria-hidden="true" id="' +
      prefix +
      '-shelf-tag">RWR-NEW</span>' +
      '<button class="sh-x" data-close aria-label="Close">\u2715</button>' +
      "</header>" +
      '<div class="sh-body">' +
      '<div class="sh-alert" id="' +
      prefix +
      '-shelf-error" role="alert" aria-live="assertive" aria-atomic="true"></div>' +
      '<label for="' +
      prefix +
      '-find" id="' +
      prefix +
      '-find-label">Find</label>' +
      '<input id="' +
      prefix +
      '-find" type="text" class="sh-mono voice-cleanup-find" autocomplete="off" spellcheck="false" autofocus />' +
      '<p class="label-hint hint-block" id="' +
      prefix +
      '-find-hint"></p>' +
      '<div class="voice-cleanup-field-error" id="' +
      prefix +
      '-find-error" role="alert"></div>' +
      '<label class="toggle-switch"><input type="checkbox" id="' +
      prefix +
      '-regex" /><span class="toggle-track" aria-hidden="true"></span>' +
      '<span class="toggle-label">Advanced: use a regex pattern</span></label>' +
      '<div class="voice-cleanup-regex-help" id="' +
      prefix +
      '-regex-help" hidden>' +
      "<p class=\"voice-cleanup-regex-title\"><strong>Don't write regex?</strong> " +
      "Describe what you want to your AI assistant. This prompt tells it how " +
      "Turnstone applies rules, which it can't know on its own, and includes " +
      "your test line.</p>" +
      '<div class="voice-cleanup-regex-actions">' +
      '<button type="button" class="sh-btn" data-vc-copy-prompt>Copy prompt for your AI</button>' +
      '<button type="button" class="admin-btn-action" data-vc-toggle-prompt>Show prompt</button>' +
      '<a class="voice-cleanup-regex-link" href="https://regex101.com/?flavor=python" ' +
      'target="_blank" rel="noopener">Test it on regex101 (Python) \u2197</a>' +
      "</div>" +
      '<textarea id="' +
      prefix +
      '-prompt" class="voice-cleanup-prompt" rows="10" readonly hidden></textarea>' +
      "</div>" +
      '<label for="' +
      prefix +
      '-replace">Replace with <span class="label-hint" id="' +
      prefix +
      '-replace-hint">said instead</span></label>' +
      '<input id="' +
      prefix +
      '-replace" type="text" autocomplete="off" spellcheck="false" ' +
      'placeholder="what the TTS model should hear" />' +
      '<label for="' +
      prefix +
      '-test">Test line <span class="label-hint">kept with the rule as its example</span></label>' +
      '<textarea id="' +
      prefix +
      '-test" rows="3" spellcheck="false"></textarea>' +
      '<div class="voice-cleanup-draft-preview" id="' +
      prefix +
      '-draft-preview" aria-live="polite"></div>' +
      '<div class="voice-cleanup-draft-meta"><span id="' +
      prefix +
      '-match"></span> <span class="voice-cleanup-draft-where">after rule groups, before the dictionary</span></div>' +
      '<label for="' +
      prefix +
      '-position">Runs</label>' +
      '<select id="' +
      prefix +
      '-position"></select>' +
      '<p class="label-hint hint-block">Custom rules run top to bottom; each sees the previous rule\u2019s output. <strong>Dictionary entries still win</strong>, since they run last.</p>' +
      '<label class="toggle-switch"><input type="checkbox" id="' +
      prefix +
      '-enabled" checked /><span class="toggle-track" aria-hidden="true"></span>' +
      '<span class="toggle-label">Enabled</span></label>' +
      "</div>" +
      '<footer class="sh-foot">' +
      '<div class="sh-foot-meta" id="' +
      prefix +
      '-shelf-count">0 of 100 rules</div>' +
      '<button class="sh-btn" data-close>Cancel</button>' +
      '<button class="sh-btn sh-btn--primary" id="' +
      prefix +
      '-shelf-save">Add rule</button>' +
      "</footer></dialog>"
    );
  }

  function _dialogFromMarkup(html) {
    const holder = document.createElement("div");
    setSafeHtml(holder, html);
    return holder.querySelector("dialog");
  }

  // -------------------------------------------------------------------------
  // Rendering
  // -------------------------------------------------------------------------

  function _setLoadError(root, message) {
    const el = root.querySelector("#voice-cleanup-load-error");
    if (!el) return;
    el.hidden = !message;
    el.textContent = message || "";
  }

  function _renderMaster(root) {
    const state = root._vc;
    const el = root.querySelector("#voice-cleanup-master");
    if (!el) return;
    const schema = state.schema[NORMALIZE_KEY] || {};
    setSafeHtml(
      el,
      settingRowHtml({
        key: NORMALIZE_KEY,
        description: schema.description || "",
        help: schema.help || MASTER_FALLBACK_HELP,
        reference_url: VOICE_DOCS_URL + "#text-cleanup",
        value: state.normalize,
        type: "bool",
      }),
    );
    const input = el.querySelector('[data-setting-key="' + NORMALIZE_KEY + '"]');
    if (input) input.disabled = !_canWrite();
  }

  function _renderGroups(root) {
    const state = root._vc;
    const el = root.querySelector("#voice-cleanup-groups");
    if (!el) return;
    setSafeHtml(
      el,
      groupsTableHtml(_groups(state), state.disabled, _canWrite()),
    );
  }

  function _renderRules(root) {
    const state = root._vc;
    const el = root.querySelector("#voice-cleanup-rules");
    if (!el) return;
    const limits = _limits(state);
    setSafeHtml(
      el,
      rulesTableHtml(
        state.rules,
        _canWrite(),
        state.justAddedIndex,
        limits,
      ),
    );
    const count = root.querySelector("#voice-cleanup-rule-count");
    if (count) {
      count.textContent = state.rules.length + " of " + limits.max_rules;
    }
    const newButton = root.querySelector("#voice-cleanup-new");
    if (newButton) {
      newButton.hidden = !_canWrite() || state.rules.length >= limits.max_rules;
    }
  }

  function _render(root) {
    const state = root._vc;
    _setLoadError(root, "");
    _renderMaster(root);
    _renderGroups(root);
    _renderRules(root);
    const sample = root.querySelector("#voice-cleanup-sample");
    if (sample && !sample.value) sample.value = sampleFor(state.rules);
    _schedulePreview(root, 0);
    _loadExamples(root);
    state.loadedOnce = true;
  }

  // -------------------------------------------------------------------------
  // Preview
  // -------------------------------------------------------------------------

  function _paintPreview(root, data) {
    const state = root._vc;
    const out = root.querySelector("#voice-cleanup-preview");
    const changes = root.querySelector("#voice-cleanup-changes");
    const notice = root.querySelector("#voice-cleanup-notice");
    if (!out) return;
    out.classList.remove("is-error");
    setSafeHtml(
      out,
      highlightSegments(data.text || "", data.spans || [], function (span) {
        return _spanLabel(state, span);
      }),
    );
    const count =
      typeof data.changes === "number"
        ? data.changes
        : (data.spans || []).length;
    if (changes) changes.textContent = changesLabel(count);
    if (notice) {
      const enabled = data.normalize_enabled !== false;
      notice.hidden = enabled;
      notice.textContent = enabled
        ? ""
        : "Cleanup is off, so the TTS model receives the text as written. " +
          "The preview above still shows what would change; switch it on below " +
          "to apply it.";
    }
  }

  function _runPreview(root, seq) {
    const state = root._vc;
    const ta = root.querySelector("#voice-cleanup-sample");
    const out = root.querySelector("#voice-cleanup-preview");
    const changes = root.querySelector("#voice-cleanup-changes");
    const text = ta ? ta.value : "";
    if (!text.trim()) {
      if (out) {
        setSafeHtml(
          out,
          '<span class="voice-cleanup-placeholder">Paste a reply to see what ' +
            "the TTS model receives.</span>",
        );
      }
      if (changes) changes.textContent = "";
      return;
    }
    _previewRequest(previewBody(text))
      .then(function (data) {
        if (seq !== state.previewSeq) return;
        _paintPreview(root, data);
      })
      .catch(function (err) {
        if (seq !== state.previewSeq) return;
        if (out) {
          out.classList.add("is-error");
          out.textContent =
            "Preview failed: " + (err && err.message ? err.message : "unknown error");
        }
        if (changes) changes.textContent = "";
      });
  }

  function _schedulePreview(root, delay) {
    const state = root._vc;
    clearTimeout(state.previewTimer);
    const seq = ++state.previewSeq;
    state.previewTimer = setTimeout(function () {
      _runPreview(root, seq);
    }, delay === undefined ? 300 : delay);
  }

  // -------------------------------------------------------------------------
  // Per-rule examples (table column)
  // -------------------------------------------------------------------------

  function _loadExamples(root) {
    const state = root._vc;
    const seq = ++state.examplesSeq;
    state.exampleQueue = [];
    const cells = root.querySelectorAll("[data-vc-example]");
    for (let i = 0; i < cells.length; i++) {
      const index = parseInt(cells[i].getAttribute("data-vc-example"), 10);
      const rule = state.rules[index];
      if (!rule || !rule.test) {
        cells[i].textContent = "\u2014";
        continue;
      }
      const key = serializeRules([_previewRule(rule)]);
      if (Object.prototype.hasOwnProperty.call(_exampleCache, key)) {
        cells[i].textContent = exampleText(rule, _exampleCache[key]);
        continue;
      }
      cells[i].textContent = rule.test + " \u2192 \u2026";
      state.exampleQueue.push({
        seq: seq,
        rule: rule,
        cell: cells[i],
        key: key,
      });
    }
    _drainExamples(root);
  }

  function _drainExamples(root) {
    const state = root._vc;
    while (state.exampleActive < 4 && state.exampleQueue.length) {
      const job = state.exampleQueue.shift();
      state.exampleActive++;
      _previewRequest(draftPreviewBody(job.rule)).then(
        function (data) {
          _exampleCache[job.key] = data.text;
          if (job.seq === state.examplesSeq) {
            job.cell.textContent = exampleText(job.rule, data.text);
          }
        },
        function () {
          if (job.seq === state.examplesSeq) {
            job.cell.textContent = _text(job.rule.test) + " \u2192 \u2014";
          }
        },
      ).then(function () {
        state.exampleActive--;
        _drainExamples(root);
      });
    }
  }

  // -------------------------------------------------------------------------
  // Settings writes
  // -------------------------------------------------------------------------

  function _saveNormalize(root, input) {
    const state = root._vc;
    const wanted = input.checked;
    input.disabled = true;
    putSetting(NORMALIZE_KEY, wanted)
      .then(function (data) {
        state.normalize =
          data && typeof data.value === "boolean" ? data.value : wanted;
        _renderMaster(root);
        _schedulePreview(root, 0);
        showToast(wanted ? "Text cleanup on" : "Text cleanup off");
      })
      .catch(function (err) {
        input.checked = !wanted;
        input.disabled = false;
        showToast("Error: " + (err.message || "Save failed"), "error");
      });
  }

  function _saveGroup(root, input) {
    const state = root._vc;
    const id = input.getAttribute("data-vc-group");
    const wanted = input.checked;
    const next = toggleDisabled(state.disabled, id, wanted);
    input.disabled = true;
    putSetting(DISABLED_KEY, JSON.stringify(next))
      .then(function (data) {
        state.disabled = parseDisabled(
          data && typeof data.value === "string"
            ? data.value
            : JSON.stringify(next),
          _groupIds(state),
        );
        _renderGroups(root);
        _schedulePreview(root, 0);
        showToast(
          (wanted ? "Group on: " : "Group off: ") + id,
        );
      })
      .catch(function (err) {
        input.checked = !wanted;
        input.disabled = false;
        showToast("Error: " + (err.message || "Save failed"), "error");
      });
  }

  function _saveRuleEnabled(root, index, input) {
    const state = root._vc;
    const rule = state.rules[index];
    if (!rule) return;
    const wanted = input.checked;
    const next = state.rules.map(function (item, i) {
      if (i !== index) return item;
      return {
        find: item.find,
        regex: item.regex,
        replace: item.replace,
        test: item.test,
        enabled: wanted,
      };
    });
    const value = serializeRules(next);
    input.disabled = true;
    putSetting(RULES_KEY, value)
      .then(function (data) {
        state.rules = parseRules(
          data && typeof data.value === "string" ? data.value : value,
          _limits(state),
        );
        _renderRules(root);
        _loadExamples(root);
        _schedulePreview(root, 0);
        showToast(wanted ? "Rule on" : "Rule off");
      })
      .catch(function (err) {
        input.checked = !wanted;
        input.disabled = false;
        showToast("Error: " + (err.message || "Save failed"), "error");
      });
  }

  function _deleteRule(root, index) {
    const state = root._vc;
    const rule = state.rules[index];
    if (!rule) return;
    showConfirmModal(
      "Delete rewrite rule",
      "Delete rule " +
        (index + 1) +
        " (\u201c" +
        rule.find +
        "\u201d \u2192 \u201c" +
        (rule.replace || "removes the match") +
        "\u201d)?",
      "Delete",
      function () {
        const next = state.rules.filter(function (_item, i) {
          return i !== index;
        });
        const value = serializeRules(next);
        putSetting(RULES_KEY, value)
          .then(function (data) {
            state.rules = parseRules(
              data && typeof data.value === "string" ? data.value : value,
              _limits(state),
            );
            state.justAddedIndex = -1;
            _renderRules(root);
            _loadExamples(root);
            _schedulePreview(root, 0);
            showToast("Rewrite rule deleted");
          })
          .catch(function (err) {
            showToast("Error: " + (err.message || "Delete failed"), "error");
          });
      },
    );
  }

  // -------------------------------------------------------------------------
  // Rule shelf
  // -------------------------------------------------------------------------

  function _draftFromFields(root) {
    const els = root._vc.els;
    return {
      find: els.find.value,
      regex: els.regex.checked,
      replace: els.replace.value,
      test: els.test.value,
      enabled: els.enabled.checked,
    };
  }

  function _setFindMode(root) {
    const els = root._vc.els;
    const regex = els.regex.checked;
    els.findLabel.textContent = regex ? "Find (regex)" : "Find";
    els.findHint.textContent = regex ? REGEX_FIND_HINT : PLAIN_FIND_HINT;
    els.replaceHint.textContent = regex ? "\\1 = first captured group" : "said instead";
    els.regexHelp.hidden = !regex;
  }

  function _showShelfError(root, message) {
    const el = root._vc.els.error;
    if (!el) return;
    el.textContent = message || "";
    if (message) el.classList.add("is-visible");
    else el.classList.remove("is-visible");
  }

  function _showFindError(root, message) {
    const els = root._vc.els;
    els.fieldError.textContent = message ? "\u2715 " + message : "";
    els.fieldError.classList.toggle("is-visible", !!message);
    els.find.classList.toggle("is-invalid", !!message);
  }

  function _setSubmitState(root) {
    const state = root._vc;
    const els = state.els;
    const draft = state.draft || _draftFromFields(root);
    els.submit.disabled =
      !_canWrite() ||
      !_text(draft.find) ||
      !!state.draftClientError ||
      !!state.draftServerError;
  }

  function _syncDraft(root) {
    const state = root._vc;
    const els = state.els;
    const draft = _draftFromFields(root);
    state.draft = draft;
    _setFindMode(root);
    const problem = validateRuleDraft(draft, _limits(state));
    state.draftClientError = problem;
    els.prompt.value = buildAiPrompt(draft, _limits(state));
    if (problem) {
      state.draftServerError = "";
      if (_text(draft.find) || state.saveAttempted) {
        _showFindError(root, problem);
      } else {
        _showFindError(root, "");
      }
      _setSubmitState(root);
      return;
    }
    state.draftServerError = "";
    if (!state.saveAttempted) _showFindError(root, "");
    _setSubmitState(root);
    clearTimeout(state.draftTimer);
    const seq = ++state.draftSeq;
    state.draftTimer = setTimeout(function () {
      _runDraftPreview(root, seq);
    }, 300);
  }

  function _runDraftPreview(root, seq) {
    const state = root._vc;
    const els = state.els;
    const draft = state.draft;
    if (!draft || !_text(draft.find)) return;
    _previewRequest(draftPreviewBody(draft))
      .then(function (data) {
        if (seq !== state.draftSeq) return;
        state.draftServerError = "";
        _showFindError(root, "");
        if (_text(draft.test)) {
          setSafeHtml(
            els.draftPreview,
            highlightSegments(data.text || "", data.spans || [], function (span) {
              return span.label || "custom rule";
            }),
          );
        } else {
          setSafeHtml(
            els.draftPreview,
            '<span class="voice-cleanup-placeholder">Add a test line to ' +
              "preview the rule.</span>",
          );
        }
        els.match.textContent = _text(draft.test)
          ? matchCountLabel(
              typeof data.changes === "number"
                ? data.changes
                : (data.spans || []).length,
              !draft.replace,
            )
          : "";
        _setSubmitState(root);
      })
      .catch(function (err) {
        if (seq !== state.draftSeq) return;
        if (err && err.status === 400) {
          state.draftServerError = err.message || "Invalid rule";
          _showFindError(root, state.draftServerError);
          setSafeHtml(
            els.draftPreview,
            '<span class="voice-cleanup-placeholder">The rule cannot run ' +
              "until this is fixed.</span>",
          );
          els.match.textContent = "";
        } else {
          els.match.textContent =
            "Could not check the rule: " +
            (err && err.message ? err.message : "unknown error");
        }
        _setSubmitState(root);
      });
  }

  function _openShelf(root, index) {
    const state = root._vc;
    if (!_canWrite()) return;
    const els = state.els;
    const limits = _limits(state);
    const editing =
      typeof index === "number" && index >= 0 && index < state.rules.length;
    if (!editing && state.rules.length >= limits.max_rules) {
      showToast(
        "At most " + limits.max_rules + " custom rewrite rules",
        "warn",
      );
      return;
    }
    state.editingIndex = editing ? index : null;
    state.saveAttempted = false;
    state.draftClientError = "";
    state.draftServerError = "";
    state.justAddedIndex = -1;
    const rule = editing
      ? state.rules[index]
      : { find: "", regex: false, replace: "", test: "", enabled: true };

    els.title.textContent = editing ? "Edit rewrite rule" : "New rewrite rule";
    els.tag.textContent = editing ? "RWR-EDIT" : "RWR-NEW";
    els.shelf.setAttribute("data-kind", editing ? "edit" : "create");
    els.submit.textContent = editing ? "Save" : "Add rule";
    els.find.value = rule.find;
    els.regex.checked = rule.regex === true;
    els.replace.value = rule.replace || "";
    els.test.value = rule.test || "";
    els.enabled.checked = rule.enabled !== false;
    els.find.maxLength = limits.max_find_chars;
    els.replace.maxLength = limits.max_replace_chars;
    els.test.maxLength = limits.max_test_chars;

    els.position.innerHTML = "";
    const options = positionOptions(state.rules, state.editingIndex);
    for (let i = 0; i < options.length; i++) {
      const option = document.createElement("option");
      option.value = String(options[i].value);
      option.textContent = options[i].label;
      els.position.appendChild(option);
    }
    els.position.value = String(editing ? index : state.rules.length);

    els.count.textContent =
      state.rules.length + " of " + limits.max_rules + " rules";
    els.prompt.hidden = true;
    els.promptToggle.textContent = "Show prompt";
    els.copyPrompt.textContent = "Copy prompt for your AI";
    _showShelfError(root, "");
    _showFindError(root, "");
    setSafeHtml(
      els.draftPreview,
      '<span class="voice-cleanup-placeholder">Add a test line to preview ' +
        "the rule.</span>",
    );
    els.match.textContent = "";
    _setFindMode(root);
    _syncDraft(root);
    window.TurnstoneHatch.setBusy(els.shelf, false);
    window.TurnstoneHatch.openShelf(els.shelf);
    els.find.focus();
  }

  function _saveDraft(root) {
    const state = root._vc;
    const els = state.els;
    if (els.shelf.hasAttribute("data-busy") || els.submit.disabled) return;
    state.saveAttempted = true;
    const draft = _draftFromFields(root);
    state.draft = draft;
    const problem = validateRuleDraft(draft, _limits(state));
    state.draftClientError = problem;
    if (problem) {
      _showFindError(root, problem);
      _setSubmitState(root);
      return;
    }
    if (state.draftServerError) {
      _showFindError(root, state.draftServerError);
      return;
    }
    const editing = state.editingIndex !== null;
    const rule = {
      find: _text(draft.find),
      regex: !!draft.regex,
      replace: draft.replace,
      test: _text(draft.test),
      enabled: !!draft.enabled,
    };
    const position = parseInt(els.position.value, 10);
    const next = applyRuleAt(state.rules, state.editingIndex, position, rule);
    const value = serializeRules(next);
    _showShelfError(root, "");
    window.TurnstoneHatch.setBusy(els.shelf, true);
    putSetting(RULES_KEY, value)
      .then(function (data) {
        state.rules = parseRules(
          data && typeof data.value === "string" ? data.value : value,
          _limits(state),
        );
        state.editingIndex = null;
        state.justAddedIndex = editing ? -1 : next.indexOf(rule);
        window.TurnstoneHatch.setBusy(els.shelf, false);
        window.TurnstoneHatch.closeShelf(els.shelf);
        _renderRules(root);
        _loadExamples(root);
        _schedulePreview(root, 0);
        showToast(editing ? "Rewrite rule updated" : "Rewrite rule added");
      })
      .catch(function (err) {
        window.TurnstoneHatch.setBusy(els.shelf, false);
        _showShelfError(root, err.message || "Save failed");
      });
  }

  function _copyPrompt(root) {
    const state = root._vc;
    const els = state.els;
    const text = buildAiPrompt(_draftFromFields(root), _limits(state));
    const done = function () {
      els.copyPrompt.textContent = "\u2713 Copied";
      setTimeout(function () {
        if (els.copyPrompt) els.copyPrompt.textContent = "Copy prompt for your AI";
      }, 1500);
    };
    const fallback = function () {
      els.prompt.hidden = false;
      els.promptToggle.textContent = "Hide prompt";
      els.prompt.value = text;
      showToast("Clipboard unavailable \u2014 the prompt is shown below", "warn");
    };
    if (
      typeof navigator !== "undefined" &&
      navigator.clipboard &&
      typeof navigator.clipboard.writeText === "function"
    ) {
      navigator.clipboard.writeText(text).then(done, fallback);
    } else {
      fallback();
    }
  }

  function _togglePrompt(root) {
    const els = root._vc.els;
    const showing = !els.prompt.hidden;
    els.prompt.hidden = showing;
    els.promptToggle.textContent = showing ? "Show prompt" : "Hide prompt";
    if (!showing) {
      els.prompt.value = buildAiPrompt(_draftFromFields(root), _limits(root._vc));
    }
  }

  // -------------------------------------------------------------------------
  // Read-aloud (the original text)
  // -------------------------------------------------------------------------

  function _playOriginal(root) {
    const state = root._vc;
    const ta = root.querySelector("#voice-cleanup-sample");
    const btn = root.querySelector("#voice-cleanup-play");
    const text = ta ? ta.value : "";
    if (!_text(text)) {
      showToast("Type a reply to read aloud", "warn");
      return;
    }
    // tts_player.js is an ES module; a classic script reaches it with a
    // dynamic import.  This is the same player the reply bubbles use, so the
    // normal read-aloud path (styles, playback rate, pauses) applies.
    if (!state.playerPromise) {
      state.playerPromise = import("/shared/tts_player.js").then(function (mod) {
        return mod.createTtsPlayer({ base: "" });
      });
    }
    state.playerPromise
      .then(function (player) {
        const holder = document.createElement("div");
        holder.className = "msg assistant";
        const body = document.createElement("div");
        body.className = "msg-body";
        const parts = text.split(/\n{2,}/);
        for (let i = 0; i < parts.length; i++) {
          if (!_text(parts[i])) continue;
          const p = document.createElement("p");
          p.textContent = parts[i];
          body.appendChild(p);
        }
        holder.appendChild(body);
        return player.play(holder, btn);
      })
      .catch(function (err) {
        showToast(
          err && err.message ? err.message : "Read-aloud failed",
          "error",
        );
      });
  }

  // -------------------------------------------------------------------------
  // Mount and load
  // -------------------------------------------------------------------------

  function _mountShelf(root, shelf) {
    const state = root._vc;
    const prefix = state.prefix;
    const byId = function (suffix) {
      return shelf.querySelector("#" + prefix + "-" + suffix);
    };
    state.els = {
      shelf: shelf,
      title: byId("shelf-title"),
      tag: byId("shelf-tag"),
      error: byId("shelf-error"),
      find: byId("find"),
      findLabel: byId("find-label"),
      findHint: byId("find-hint"),
      fieldError: byId("find-error"),
      regex: byId("regex"),
      regexHelp: byId("regex-help"),
      prompt: byId("prompt"),
      copyPrompt: shelf.querySelector("[data-vc-copy-prompt]"),
      promptToggle: shelf.querySelector("[data-vc-toggle-prompt]"),
      replace: byId("replace"),
      replaceHint: byId("replace-hint"),
      test: byId("test"),
      draftPreview: byId("draft-preview"),
      match: byId("match"),
      position: byId("position"),
      enabled: byId("enabled"),
      count: byId("shelf-count"),
      submit: byId("shelf-save"),
    };

    state.els.find.addEventListener("input", function () {
      _syncDraft(root);
    });
    state.els.regex.addEventListener("change", function () {
      _syncDraft(root);
    });
    state.els.replace.addEventListener("input", function () {
      _syncDraft(root);
    });
    state.els.test.addEventListener("input", function () {
      _syncDraft(root);
    });
    state.els.enabled.addEventListener("change", function () {
      _syncDraft(root);
    });
    state.els.submit.addEventListener("click", function () {
      _saveDraft(root);
    });
    state.els.copyPrompt.addEventListener("click", function () {
      _copyPrompt(root);
    });
    state.els.promptToggle.addEventListener("click", function () {
      _togglePrompt(root);
    });
  }

  function _wireRoot(root) {
    root.addEventListener("input", function (e) {
      if (e.target && e.target.id === "voice-cleanup-sample") {
        _schedulePreview(root, 300);
      }
    });
    root.addEventListener("change", function (e) {
      const target = e.target;
      if (!target) return;
      if (target.getAttribute("data-setting-key") === NORMALIZE_KEY) {
        _saveNormalize(root, target);
      } else if (target.hasAttribute("data-vc-group")) {
        _saveGroup(root, target);
      } else if (target.hasAttribute("data-vc-rule-toggle")) {
        _saveRuleEnabled(
          root,
          parseInt(target.getAttribute("data-vc-rule-toggle"), 10),
          target,
        );
      }
    });
    root.addEventListener("click", function (e) {
      const edit = e.target.closest("[data-vc-rule-edit]");
      if (edit) {
        _openShelf(root, parseInt(edit.getAttribute("data-vc-rule-edit"), 10));
        return;
      }
      const del = e.target.closest("[data-vc-rule-delete]");
      if (del) {
        _deleteRule(root, parseInt(del.getAttribute("data-vc-rule-delete"), 10));
        return;
      }
      if (e.target.closest("#voice-cleanup-new")) {
        _openShelf(root, null);
        return;
      }
      if (e.target.closest("#voice-cleanup-play")) _playOriginal(root);
    });
  }

  function _mount(container) {
    const host = container.closest ? container.closest(".hatch-host") : null;
    const dialogHost = host || container;
    if (!host && dialogHost.style) dialogHost.style.position = "relative";

    const prefix = "voice-cleanup-" + ++_mountSeq;
    const root = document.createElement("div");
    root.className = "voice-cleanup-root";
    setSafeHtml(root, _rootMarkup());
    container.appendChild(root);

    // The shelf lives outside the tab content, as a direct child of the
    // .hatch-host: openShelf inerts the host's other children, so a dialog
    // inside the content would inert itself.
    const shelf = _dialogFromMarkup(_shelfMarkup(prefix));
    dialogHost.appendChild(shelf);

    const state = {
      prefix: prefix,
      seq: 0,
      loadedOnce: false,
      meta: null,
      schema: {},
      normalize: true,
      disabled: [],
      rules: [],
      justAddedIndex: -1,
      editingIndex: null,
      saveAttempted: false,
      draft: null,
      draftClientError: "",
      draftServerError: "",
      draftSeq: 0,
      draftTimer: null,
      previewSeq: 0,
      previewTimer: null,
      examplesSeq: 0,
      exampleQueue: [],
      exampleActive: 0,
      playerPromise: null,
      els: null,
    };
    root._vc = state;
    _mountShelf(root, shelf);
    _wireRoot(root);

    // A first render can beat whoami's permission list; once the data is in,
    // re-render when the scopes settle so an authorized operator does not keep
    // a read-only page until the next tab switch.
    if (typeof window.whenPermissionsReady === "function") {
      window.whenPermissionsReady(function () {
        if (state.loadedOnce) _render(root);
      });
    }

    container._voiceCleanupRoot = root;
    return root;
  }

  function _ensureMounted(container) {
    const existing = container._voiceCleanupRoot;
    if (existing && existing._vc && container.contains(existing)) return existing;
    if (existing) {
      if (existing._vc && existing._vc.els && existing._vc.els.shelf) {
        const shelf = existing._vc.els.shelf;
        if (shelf.parentNode) shelf.parentNode.removeChild(shelf);
      }
      if (existing.parentNode) existing.parentNode.removeChild(existing);
    }
    return _mount(container);
  }

  function _load(root) {
    const state = root._vc;
    const seq = ++state.seq;
    // A reload is a fresh look at the stored list: a "just added" pill from
    // the previous visit no longer means anything.
    state.justAddedIndex = -1;
    _setLoadError(root, "");
    return Promise.all([
      _getJson(SETTINGS_URL, "Failed to load settings"),
      _getJson(SCHEMA_URL, "Failed to load the settings schema"),
      _getJson(META_URL, "Failed to load the voice metadata"),
    ])
      .then(function (results) {
        if (seq !== state.seq) return;
        const settings = (results[0] && results[0].settings) || [];
        const schema = (results[1] && results[1].schema) || [];
        state.meta = results[2] || {};
        state.schema = {};
        for (let i = 0; i < schema.length; i++) {
          if (schema[i] && schema[i].key) state.schema[schema[i].key] = schema[i];
        }

        let normalize = true;
        let disabledRaw = "[]";
        let rulesRaw = "[]";
        for (let i = 0; i < settings.length; i++) {
          const item = settings[i];
          if (!item) continue;
          if (item.key === NORMALIZE_KEY) {
            normalize = item.value === true || item.value === "true";
          } else if (item.key === DISABLED_KEY) {
            disabledRaw = typeof item.value === "string" ? item.value : "[]";
          } else if (item.key === RULES_KEY) {
            rulesRaw = typeof item.value === "string" ? item.value : "[]";
          }
        }
        state.normalize = normalize;
        state.disabled = parseDisabled(disabledRaw, _groupIds(state));
        state.rules = parseRules(rulesRaw, _limits(state));
        _render(root);
      })
      .catch(function (err) {
        if (seq !== state.seq) return;
        _setLoadError(
          root,
          "Failed to load text cleanup: " +
            (err && err.message ? err.message : "unknown error"),
        );
      });
  }

  /** The Voice tab's entry point; safe to call on every show. */
  function loadVoiceCleanup(container) {
    if (!container) return Promise.resolve();
    const root = _ensureMounted(container);
    return _load(root);
  }

  window.loadVoiceCleanup = loadVoiceCleanup;
  if (window.TS_VOICE && typeof window.TS_VOICE.registerSection === "function") {
    window.TS_VOICE.registerSection("voice-cleanup", loadVoiceCleanup);
  }

  // Pure helpers exposed for the node harness (and any future consumer that
  // needs the same parse/render without a second copy).
  window.TurnstoneVoiceCleanup = {
    VOICE_DOCS_URL: VOICE_DOCS_URL,
    DEFAULT_LIMITS: DEFAULT_LIMITS,
    SAMPLE_TEXT: SAMPLE_TEXT,
    normalizeLimits: normalizeLimits,
    parseRules: parseRules,
    serializeRules: serializeRules,
    parseDisabled: parseDisabled,
    toggleDisabled: toggleDisabled,
    matchCountLabel: matchCountLabel,
    changesLabel: changesLabel,
    highlightSegments: highlightSegments,
    positionOptions: positionOptions,
    applyRuleAt: applyRuleAt,
    buildAiPrompt: buildAiPrompt,
    previewBody: previewBody,
    draftPreviewBody: draftPreviewBody,
    validateRuleDraft: validateRuleDraft,
    settingRowHtml: settingRowHtml,
    groupsTableHtml: groupsTableHtml,
    rulesTableHtml: rulesTableHtml,
    ruleTypeLabel: ruleTypeLabel,
    exampleText: exampleText,
    sampleFor: sampleFor,
    putSetting: putSetting,
  };
})();
