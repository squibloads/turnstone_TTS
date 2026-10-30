/* Admin › Voice › Dictionary — the voice dictionary list and its create/edit
   shelf.

   One dictionary serves both directions: read-aloud speaks an entry's
   respelling (or its IPA, on a model that accepts IPA) and dictation turns
   each of its `also_hears` phrases back into the term.  The whole list is a
   single setting (`audio.voice_dictionary`, a JSON list), so every edit PUTs
   the complete list; the server's strict validator
   (turnstone/core/voice_config.py) is the authority on shape, duplicates and
   the 200-entry cap, and its message is shown inline in the shelf.

   The legacy `audio.tts_pronunciations` lines (`word = respelling`) still
   apply on their own, so the list offers a one-click import.  Import is two
   PUTs — the dictionary first, the legacy value only after that succeeds —
   because a failed dictionary write must leave the old lines intact.

   Classic script: exposes window.loadVoiceDictionary(container), called by
   the Voice tab on every show, and window.TurnstoneVoiceDictionary for the
   pure helpers (and the node harness).

   Mounting: the Voice shell adds /static/voice_dictionary.css and this
   script, then calls window.loadVoiceDictionary(container) whenever the
   Dictionary tab is shown.  The loader owns one .voice-dict-panel child of
   that container (its other children are left alone) and builds its shelf
   inside the nearest .hatch-host. */

(function () {
  "use strict";

  const DICTIONARY_KEY = "audio.voice_dictionary";
  const LEGACY_KEY = "audio.tts_pronunciations";
  // Mirrors turnstone/core/voice_config.py.  These cap what the UI offers;
  // every write still goes through the server's validate_dictionary.
  const MAX_ENTRIES = 200;
  const MAX_TEXT = 100;
  const MAX_ALSO_HEARS = 10;

  // The list is one value; a Voice tab is one container.  The shelf is built
  // once and re-used across shows so its field state never needs rebuilding.
  let _entries = [];
  let _legacyRaw = "";
  let _container = null;
  // Our own block inside the tab container: the Voice shell may render a
  // heading around us, so the loader replaces this panel, never the host's
  // other children.  The shelf lives outside it (see ensureShelf).
  let _panel = null;
  let _editIndex = -1;
  let _kind = "respelling";
  let _shelf = null;
  // Guards against an older GET landing after a newer one (tab re-entry).
  let _loadSeq = 0;

  // -------------------------------------------------------------------------
  // Pure helpers
  // -------------------------------------------------------------------------

  function _text(value) {
    return typeof value === "string" ? value.trim() : "";
  }

  function _caseKey(term) {
    return term.toLowerCase();
  }

  /** Tolerant read, mirroring parse_dictionary: bad JSON -> [], bad entries
   *  skipped, a later duplicate wins (and takes the later position). */
  function parseEntries(raw) {
    let items;
    try {
      items = JSON.parse(raw);
    } catch (e) {
      return [];
    }
    if (!Array.isArray(items)) return [];
    const out = new Map();
    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      if (!item || typeof item !== "object" || Array.isArray(item)) continue;

      const term = _text(item.term);
      if (!term || term.length > MAX_TEXT) continue;

      const kind = item.kind == null || item.kind === "" ? "respelling" : item.kind;
      if (kind !== "respelling" && kind !== "ipa") continue;

      const respelling = _text(item.respelling);
      const ipa = kind === "ipa" ? _text(item.ipa) : "";
      if (respelling.length > MAX_TEXT || ipa.length > MAX_TEXT) continue;
      // The strict contract requires an IPA for the IPA kind; a row without
      // one would be rejected on the next save, so do not show it as valid.
      if (kind === "ipa" && !ipa) continue;

      if ("match_case" in item && typeof item.match_case !== "boolean") continue;
      const matchCase = item.match_case === true;

      let hearsRaw = item.also_hears;
      if (hearsRaw == null) hearsRaw = [];
      if (!Array.isArray(hearsRaw)) continue;
      hearsRaw = hearsRaw.slice(0, MAX_ALSO_HEARS);
      const hears = [];
      let badPhrase = false;
      for (let j = 0; j < hearsRaw.length; j++) {
        const phrase = hearsRaw[j];
        if (typeof phrase !== "string" || !phrase.trim()) {
          badPhrase = true;
          break;
        }
        if (phrase.trim().length > MAX_TEXT) {
          badPhrase = true;
          break;
        }
        hears.push(phrase.trim());
      }
      if (badPhrase) continue;
      if (!respelling && !ipa && !hears.length) continue;

      const key = _caseKey(term) + (matchCase ? "\u0000case" : "\u0000fold");
      if (!out.has(key) && out.size >= MAX_ENTRIES) continue;
      out.delete(key);
      out.set(key, {
        term: term,
        kind: kind,
        respelling: respelling,
        ipa: ipa,
        match_case: matchCase,
        also_hears: hears,
      });
    }
    return Array.from(out.values());
  }

  /** Canonical wire shape, field order included — the server canonicalises
   *  too, but a stable body keeps the PUT easy to read in an audit log. */
  function canonicalJson(entries) {
    return JSON.stringify(
      (entries || []).map(function (entry) {
        return {
          term: entry.term,
          kind: entry.kind,
          respelling: entry.respelling,
          ipa: entry.ipa,
          match_case: entry.match_case === true,
          also_hears: entry.also_hears || [],
        };
      }),
    );
  }

  /** Mirrors speech_text.parse_pronunciations: blanks, `#` comments and
   *  lines without a `word = respelling` pair are ignored; an exact repeated
   *  key keeps its first position with the later value. */
  function parseLegacyPronunciations(raw) {
    const out = new Map();
    if (typeof raw !== "string") return [];
    const lines = raw.split(/\r?\n/);
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (!line || line.charAt(0) === "#" || line.indexOf("=") === -1) continue;
      const at = line.indexOf("=");
      const term = line.slice(0, at).trim();
      const respelling = line.slice(at + 1).trim();
      if (!term || !respelling) continue;
      if (term.length > MAX_TEXT || respelling.length > MAX_TEXT) continue;
      if (!out.has(term) && out.size >= MAX_ENTRIES) continue;
      out.set(term, { term: term, respelling: respelling });
    }
    return Array.from(out.values());
  }

  /** Merge legacy lines into the dictionary as case-insensitive respelling
   *  entries.  A term already present (in any case) is skipped.  Legacy
   *  lookup lower-cases keys, so two case variants are one term and the
   *  later line's respelling wins — the same result the old lines had.
   *  When the result would pass the cap nothing is merged: the caller
   *  refuses the import rather than dropping legacy lines it could never
   *  re-derive. */
  function importLegacyEntries(existing, legacy) {
    const merged = existing.slice();
    const taken = new Set();
    for (let i = 0; i < existing.length; i++) taken.add(_caseKey(existing[i].term));
    const imported = new Map();
    let added = 0;
    let skipped = 0;
    for (let i = 0; i < legacy.length; i++) {
      const line = legacy[i];
      const key = _caseKey(line.term);
      if (imported.has(key)) {
        merged[imported.get(key)].respelling = line.respelling;
        continue;
      }
      if (taken.has(key)) {
        skipped++;
        continue;
      }
      if (merged.length >= MAX_ENTRIES) {
        return { entries: existing.slice(), added: 0, skipped: skipped, overflow: true };
      }
      taken.add(key);
      imported.set(key, merged.length);
      merged.push({
        term: line.term,
        kind: "respelling",
        respelling: line.respelling,
        ipa: "",
        match_case: false,
        also_hears: [],
      });
      added++;
    }
    return { entries: merged, added: added, skipped: skipped, overflow: false };
  }

  function splitAlsoHears(text) {
    return String(text || "")
      .split(",")
      .map(function (phrase) {
        return phrase.trim();
      })
      .filter(Boolean);
  }

  function entryFromFields(fields) {
    fields = fields || {};
    const kind = fields.kind === "ipa" ? "ipa" : "respelling";
    return {
      term: _text(fields.term),
      kind: kind,
      respelling: _text(fields.respelling),
      ipa: kind === "ipa" ? _text(fields.ipa) : "",
      match_case: fields.match_case === true,
      also_hears: fields.also_hears || [],
    };
  }

  /** The contract checks worth catching before a round trip; the server
   *  still owns duplicates, lengths and anything else. */
  function validateEntry(entry) {
    if (!_text(entry.term)) return "Term is required.";
    if (entry.kind === "ipa" && !entry.ipa) {
      return "IPA is required for the Phonetic (IPA) type.";
    }
    if (!entry.respelling && !entry.ipa && !entry.also_hears.length) {
      return "Add a respelling, IPA, or at least one \u201calso hears\u201d phrase.";
    }
    if (entry.also_hears.length > MAX_ALSO_HEARS) {
      return "At most " + MAX_ALSO_HEARS + " \u201calso hears\u201d phrases per entry.";
    }
    return "";
  }

  function sayAs(entry) {
    if (entry.kind === "ipa" && entry.ipa) {
      return entry.respelling
        ? entry.ipa + " else \u201c" + entry.respelling + "\u201d"
        : entry.ipa;
    }
    return entry.respelling || "\u2014";
  }

  function countLabel(count) {
    return count + " of " + MAX_ENTRIES + " entries";
  }

  // -------------------------------------------------------------------------
  // Markup
  // -------------------------------------------------------------------------

  function rowsHtml(entries, canWrite) {
    if (!entries.length) {
      return '<div class="dashboard-empty">No dictionary entries yet.</div>';
    }
    let html = "";
    for (let i = 0; i < entries.length; i++) {
      const entry = entries[i];
      const actions = canWrite
        ? '<button type="button" class="admin-btn-action" data-vd-edit="' +
          i +
          '" aria-label="Edit ' +
          escapeHtml(entry.term) +
          '">Edit</button>' +
          '<button type="button" class="admin-btn-danger" data-vd-delete="' +
          i +
          '" aria-label="Delete ' +
          escapeHtml(entry.term) +
          '">Delete</button>'
        : "";
      html +=
        '<div class="admin-row voice-dict-grid" role="listitem">' +
        '<span class="admin-col"><code>' +
        escapeHtml(entry.term) +
        "</code></span>" +
        '<span class="admin-col voice-dict-type">' +
        escapeHtml(entry.kind) +
        "</span>" +
        '<span class="admin-col">' +
        escapeHtml(sayAs(entry)) +
        "</span>" +
        '<span class="admin-col voice-dict-hears">' +
        (entry.also_hears.length ? escapeHtml(entry.also_hears.join(", ")) : "\u2014") +
        "</span>" +
        '<span class="admin-col voice-dict-case">' +
        (entry.match_case ? "Aa" : "\u2014") +
        "</span>" +
        (canWrite
          ? '<span class="admin-col admin-col-actions">' + actions + "</span>"
          : "") +
        "</div>";
    }
    return html;
  }

  function legacyNoticeHtml(count, canWrite) {
    const text =
      count === 1
        ? "1 legacy pronunciation line still applies"
        : count + " legacy pronunciation lines still apply";
    return (
      '<div class="voice-dict-notice">' +
      '<span class="voice-dict-notice-icon" aria-hidden="true">&#9888;</span>' +
      text +
      (canWrite
        ? '<button type="button" class="admin-btn-action" data-vd-import>Import</button>'
        : "") +
      "</div>"
    );
  }

  function renderHtml(entries, legacyCount, canWrite) {
    let html =
      '<div class="admin-toolbar">' +
      '<span class="voice-dict-count">' +
      countLabel(entries.length) +
      "</span>" +
      (canWrite
        ? '<button type="button" class="admin-action-btn" data-vd-new>+ New entry</button>'
        : "") +
      "</div>";
    if (legacyCount > 0) html += legacyNoticeHtml(legacyCount, canWrite);
    html +=
      '<div class="admin-colheaders voice-dict-grid" aria-hidden="true">' +
      '<span class="admin-col">TERM</span>' +
      '<span class="admin-col voice-dict-type">TYPE</span>' +
      '<span class="admin-col">SAY AS</span>' +
      '<span class="admin-col voice-dict-hears">DICTATION ALSO HEARS</span>' +
      '<span class="admin-col voice-dict-case">CASE</span>' +
      (canWrite ? '<span class="admin-col admin-col-actions">ACTIONS</span>' : "") +
      "</div>" +
      '<div class="voice-dict-list" role="list" aria-label="Voice dictionary" aria-live="polite">' +
      rowsHtml(entries, canWrite) +
      "</div>";
    return html;
  }

  // -------------------------------------------------------------------------
  // Settings I/O
  // -------------------------------------------------------------------------

  /** PUT one settings key; a refused write rejects with the server's text. */
  function putSetting(key, value) {
    return authFetch("/v1/api/admin/settings/" + encodeURIComponent(key), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: value }),
    }).then(function (r) {
      if (!r.ok) {
        return r.json().then(function (data) {
          throw new Error((data && data.error) || "Save failed");
        });
      }
      return r.json();
    });
  }

  /** Dictionary first, legacy key second: a refused dictionary write leaves
   *  the legacy lines untouched and the import can be retried. */
  function importEntries(entries, legacyRaw) {
    const legacy = parseLegacyPronunciations(legacyRaw);
    if (!legacy.length) {
      return Promise.resolve({ imported: 0, skipped: 0, entries: entries.slice() });
    }
    const result = importLegacyEntries(entries, legacy);
    if (result.overflow) {
      return Promise.reject(
        new Error(
          "At most " +
            MAX_ENTRIES +
            " dictionary entries; delete some entries before importing.",
        ),
      );
    }
    return putSetting(DICTIONARY_KEY, canonicalJson(result.entries))
      .then(function () {
        return putSetting(LEGACY_KEY, "");
      })
      .then(function () {
        return { imported: result.added, skipped: result.skipped, entries: result.entries };
      });
  }

  // -------------------------------------------------------------------------
  // Permission
  // -------------------------------------------------------------------------

  // Same contract as app.js: prefer admin.js's _consoleHasPermission shim
  // (which itself delegates to auth.js's hasPermission when live), then
  // mirror its storage parse for the node harness / a stale-cached
  // admin.js.  Settings writes need admin.settings; absent means deny.
  function canWrite() {
    if (typeof _consoleHasPermission === "function") {
      return _consoleHasPermission("admin.settings");
    }
    if (typeof window.hasPermission === "function") {
      return window.hasPermission("admin.settings");
    }
    const perms = window.sessionStorage
      ? window.sessionStorage.getItem("turnstone_permissions") || ""
      : "";
    return perms.split(",").indexOf("admin.settings") !== -1;
  }

  // -------------------------------------------------------------------------
  // Shelf
  // -------------------------------------------------------------------------

  const SHELF_HTML = `
<dialog class="hatch hatch--shelf hatch--lg" id="voice-dict-shelf" data-kind="create" aria-labelledby="voice-dict-shelf-title">
  <header class="sh-head">
    <span class="sh-led" aria-hidden="true"></span>
    <h2 class="sh-title" id="voice-dict-shelf-title">New dictionary entry</h2>
    <span class="sh-tag" aria-hidden="true" id="voice-dict-shelf-tag">DCT-NEW</span>
    <button class="sh-x" data-close aria-label="Close">\u2715</button>
  </header>
  <div class="sh-body">
    <div id="voice-dict-shelf-error" class="sh-alert" role="alert" aria-live="assertive" aria-atomic="true"></div>

    <label for="voice-dict-term">Term</label>
    <input id="voice-dict-term" type="text" class="sh-mono" maxlength="100" autocomplete="off" spellcheck="false" autofocus />

    <label id="voice-dict-kind-label">Type</label>
    <div class="seg" role="group" aria-labelledby="voice-dict-kind-label">
      <button type="button" data-vd-kind="respelling" aria-pressed="true">Respelling</button>
      <button type="button" data-vd-kind="ipa" aria-pressed="false">Phonetic (IPA)</button>
    </div>

    <label for="voice-dict-respelling">Respelling <span class="label-hint">used by every model</span></label>
    <input id="voice-dict-respelling" type="text" maxlength="100" autocomplete="off" spellcheck="false" />

    <div id="voice-dict-ipa-field" hidden>
      <label for="voice-dict-ipa">IPA <span class="label-hint">only for models that accept IPA; others use the respelling</span></label>
      <input id="voice-dict-ipa" type="text" class="sh-mono" maxlength="100" autocomplete="off" spellcheck="false" />
    </div>

    <label class="toggle-switch">
      <input id="voice-dict-match-case" type="checkbox" />
      <span class="toggle-track" aria-hidden="true"></span>
      <span class="toggle-label">Match case</span>
    </label>

    <label for="voice-dict-also-hears">Also hears <span class="label-hint">comma-separated</span></label>
    <input id="voice-dict-also-hears" type="text" autocomplete="off" spellcheck="false" />
    <p class="label-hint hint-block" id="voice-dict-hears-hint"></p>
  </div>
  <footer class="sh-foot">
    <div class="sh-foot-meta" id="voice-dict-shelf-count">0 of 200 entries</div>
    <button class="sh-btn" data-close>Cancel</button>
    <button id="voice-dict-shelf-save" class="sh-btn sh-btn--primary">Save</button>
  </footer>
</dialog>`;

  function ensurePanel(container) {
    let panel = container.querySelector(".voice-dict-panel");
    if (!panel) {
      panel = document.createElement("div");
      panel.className = "voice-dict-panel";
      container.appendChild(panel);
    }
    _panel = panel;
    return panel;
  }

  function ensureShelf(container) {
    if (_shelf && _shelf.isConnected) return _shelf;
    // Hatch shelves mount inside the pane that carries .hatch-host; the
    // Voice tab lives in the admin layout, so this normally returns
    // #admin-layout.  A bare container becomes its own host.
    const host = container.closest(".hatch-host") || container;
    if (!host.classList.contains("hatch-host")) host.classList.add("hatch-host");

    let shelf = document.getElementById("voice-dict-shelf");
    if (!shelf) {
      const holder = document.createElement("div");
      setSafeHtml(holder, SHELF_HTML);
      shelf = holder.firstElementChild;
      host.appendChild(shelf);
      wireShelf(shelf);
    } else if (shelf.parentElement !== host) {
      host.appendChild(shelf);
    }
    _shelf = shelf;
    return shelf;
  }

  function wireShelf(shelf) {
    const kinds = shelf.querySelectorAll("[data-vd-kind]");
    for (let i = 0; i < kinds.length; i++) {
      kinds[i].addEventListener("click", function () {
        _kind = this.getAttribute("data-vd-kind") === "ipa" ? "ipa" : "respelling";
        syncKind();
      });
    }
    document.getElementById("voice-dict-term").addEventListener("input", syncTermHint);
    document
      .getElementById("voice-dict-shelf-save")
      .addEventListener("click", saveShelf);
  }

  function syncKind() {
    if (_shelf) {
      const kinds = _shelf.querySelectorAll("[data-vd-kind]");
      for (let i = 0; i < kinds.length; i++) {
        const on = kinds[i].getAttribute("data-vd-kind") === _kind;
        kinds[i].setAttribute("aria-pressed", on ? "true" : "false");
      }
    }
    const field = document.getElementById("voice-dict-ipa-field");
    if (field) field.hidden = _kind !== "ipa";
  }

  function syncTermHint() {
    const input = document.getElementById("voice-dict-term");
    const hint = document.getElementById("voice-dict-hears-hint");
    if (!input || !hint) return;
    const term = input.value.trim() || "the term";
    hint.textContent =
      "When a transcript contains one of these as a whole phrase, it becomes \u201c" +
      term +
      "\u201d.";
  }

  function showShelfError(message) {
    const el = document.getElementById("voice-dict-shelf-error");
    if (!el) return;
    el.textContent = message || "";
    if (message) el.classList.add("is-visible");
    else el.classList.remove("is-visible");
  }

  function openShelf(index) {
    const shelf = _shelf;
    if (!shelf) return;
    const editing = index >= 0 && index < _entries.length;
    _editIndex = editing ? index : -1;
    const entry = editing
      ? _entries[index]
      : {
          term: "",
          kind: "respelling",
          respelling: "",
          ipa: "",
          match_case: false,
          also_hears: [],
        };
    _kind = entry.kind;

    document.getElementById("voice-dict-shelf-title").textContent = editing
      ? "Edit dictionary entry"
      : "New dictionary entry";
    document.getElementById("voice-dict-shelf-tag").textContent = editing
      ? "DCT-EDIT"
      : "DCT-NEW";
    shelf.setAttribute("data-kind", editing ? "edit" : "create");
    document.getElementById("voice-dict-shelf-count").textContent = countLabel(
      _entries.length,
    );
    document.getElementById("voice-dict-term").value = entry.term;
    document.getElementById("voice-dict-respelling").value = entry.respelling;
    document.getElementById("voice-dict-ipa").value = entry.ipa;
    document.getElementById("voice-dict-match-case").checked = entry.match_case;
    document.getElementById("voice-dict-also-hears").value =
      entry.also_hears.join(", ");
    showShelfError("");
    syncKind();
    syncTermHint();
    window.TurnstoneHatch.openShelf(shelf);
    document.getElementById("voice-dict-term").focus();
  }

  function saveShelf() {
    const editing = _editIndex >= 0;
    const entry = entryFromFields({
      term: document.getElementById("voice-dict-term").value,
      kind: _kind,
      respelling: document.getElementById("voice-dict-respelling").value,
      ipa: document.getElementById("voice-dict-ipa").value,
      match_case: document.getElementById("voice-dict-match-case").checked,
      also_hears: splitAlsoHears(
        document.getElementById("voice-dict-also-hears").value,
      ),
    });
    const problem = validateEntry(entry);
    if (problem) {
      showShelfError(problem);
      return;
    }
    if (!editing && _entries.length >= MAX_ENTRIES) {
      showShelfError("The dictionary is limited to " + MAX_ENTRIES + " entries.");
      return;
    }

    const next = _entries.slice();
    if (editing) next[_editIndex] = entry;
    else next.push(entry);

    const shelf = _shelf;
    showShelfError("");
    window.TurnstoneHatch.setBusy(shelf, true);
    putSetting(DICTIONARY_KEY, canonicalJson(next))
      .then(function () {
        window.TurnstoneHatch.setBusy(shelf, false);
        window.TurnstoneHatch.closeShelf(shelf);
        showToast(editing ? "Entry updated" : "Entry added");
        loadVoiceDictionary(_container);
      })
      .catch(function (e) {
        window.TurnstoneHatch.setBusy(shelf, false);
        showShelfError(e.message || "Save failed");
      });
  }

  // -------------------------------------------------------------------------
  // Row actions
  // -------------------------------------------------------------------------

  function deleteEntry(index) {
    const entry = _entries[index];
    if (!entry) return;
    showConfirmModal(
      "Delete dictionary entry",
      "Delete \u201c" + entry.term + "\u201d from the voice dictionary?",
      "Delete",
      function () {
        const next = _entries.slice();
        next.splice(index, 1);
        putSetting(DICTIONARY_KEY, canonicalJson(next))
          .then(function () {
            showToast("Entry deleted");
            loadVoiceDictionary(_container);
          })
          .catch(function (e) {
            showToast("Error: " + (e.message || "Delete failed"), "error");
          });
      },
    );
  }

  function runImport() {
    const button = _panel && _panel.querySelector("[data-vd-import]");
    if (button) {
      button.disabled = true;
      button.textContent = "Importing\u2026";
    }
    importEntries(_entries, _legacyRaw)
      .then(function (result) {
        if (result.imported) {
          showToast(
            "Imported " +
              result.imported +
              (result.imported === 1
                ? " pronunciation entry"
                : " pronunciation entries"),
          );
        } else {
          showToast("Legacy pronunciation lines cleared");
        }
        loadVoiceDictionary(_container);
      })
      .catch(function (e) {
        showToast("Error: " + (e.message || "Import failed"), "error");
        // The first PUT may have landed even when the second (clearing the
        // legacy value) failed — reload so the list tells the truth.
        loadVoiceDictionary(_container);
      });
  }

  // -------------------------------------------------------------------------
  // List
  // -------------------------------------------------------------------------

  function render() {
    if (!_panel) return;
    const legacyCount = parseLegacyPronunciations(_legacyRaw).length;
    setSafeHtml(_panel, renderHtml(_entries, legacyCount, canWrite()));

    const newButton = _panel.querySelector("[data-vd-new]");
    if (newButton) {
      newButton.addEventListener("click", function () {
        openShelf(-1);
      });
    }
    const importButton = _panel.querySelector("[data-vd-import]");
    if (importButton) importButton.addEventListener("click", runImport);

    const list = _panel.querySelector(".voice-dict-list");
    if (!list) return;
    const edits = list.querySelectorAll("[data-vd-edit]");
    for (let i = 0; i < edits.length; i++) {
      edits[i].addEventListener("click", function () {
        openShelf(parseInt(this.getAttribute("data-vd-edit"), 10));
      });
    }
    const deletes = list.querySelectorAll("[data-vd-delete]");
    for (let i = 0; i < deletes.length; i++) {
      deletes[i].addEventListener("click", function () {
        deleteEntry(parseInt(this.getAttribute("data-vd-delete"), 10));
      });
    }
  }

  /** The Voice tab's entry point; safe to call on every show. */
  function loadVoiceDictionary(container) {
    if (!container) return;
    _container = container;
    const seq = ++_loadSeq;
    ensureShelf(container);
    const panel = ensurePanel(container);
    setSafeHtml(
      panel,
      '<div class="dashboard-empty">Loading dictionary\u2026</div>',
    );
    authFetch("/v1/api/admin/settings")
      .then(function (r) {
        if (!r.ok) throw new Error("Failed to load settings");
        return r.json();
      })
      .then(function (data) {
        if (seq !== _loadSeq) return; // an older response lost the race
        const settings = (data && data.settings) || [];
        let dictionary = "[]";
        let legacy = "";
        for (let i = 0; i < settings.length; i++) {
          if (settings[i].key === DICTIONARY_KEY) dictionary = settings[i].value;
          else if (settings[i].key === LEGACY_KEY) legacy = settings[i].value;
        }
        _entries = parseEntries(dictionary);
        _legacyRaw = typeof legacy === "string" ? legacy : "";
        render();
      })
      .catch(function () {
        if (seq !== _loadSeq) return;
        setSafeHtml(
          panel,
          '<div class="dashboard-empty">Failed to load the voice dictionary</div>',
        );
      });
  }

  window.loadVoiceDictionary = loadVoiceDictionary;

  // Pure helpers exposed for the node harness (and any future consumer that
  // needs the same parse/render without a second copy).
  window.TurnstoneVoiceDictionary = {
    MAX_ENTRIES: MAX_ENTRIES,
    parseEntries: parseEntries,
    canonicalJson: canonicalJson,
    parseLegacyPronunciations: parseLegacyPronunciations,
    importLegacyEntries: importLegacyEntries,
    importEntries: importEntries,
    entryFromFields: entryFromFields,
    splitAlsoHears: splitAlsoHears,
    validateEntry: validateEntry,
    sayAs: sayAs,
    countLabel: countLabel,
    rowsHtml: rowsHtml,
    renderHtml: renderHtml,
    putSetting: putSetting,
  };
})();
