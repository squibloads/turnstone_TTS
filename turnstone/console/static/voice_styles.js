/* Voice styles — Admin › Voice › Styles tab.

   The list is the `audio.tts_styles` JSON setting (name + instruction +
   in_menu); the ● marks the style named by `audio.tts_style`.  Create/edit
   goes through the pane-scoped Hatch shelf, delete/reset through the Hatch
   danger dialog.  Every write PUTs the whole styles list — the setting is a
   list, not a table — and the default choice is a second, separate setting.

   The caps mirror turnstone/core/voice_config.py (MAX_STYLES,
   MAX_STYLE_NAME_CHARS, MAX_INSTRUCTION_CHARS); the strict server validator
   remains the authority, so a mismatch surfaces as its inline error rather
   than as silent data loss.  Parsing here is deliberately tolerant, like
   voice_config.parse_styles: a stored list that predates a rule change must
   still render.

   Classic script: exposes window.loadVoiceStyles (the Voice page's entry
   point, idempotent) and the pure helpers under window.TurnstoneVoiceStyles
   for the node tests.  Pairs with voice_styles.css. */
(function () {
  "use strict";

  const MAX_STYLES = 20;
  const MAX_NAME_CHARS = 40;
  const MAX_INSTRUCTION_CHARS = 500;
  const SETTINGS_URL = "/v1/api/admin/settings";
  const STYLES_KEY = "audio.tts_styles";
  const DEFAULT_KEY = "audio.tts_style";

  // ---------------------------------------------------------------------
  // Pure helpers (node-tested under tests/test_voice_styles_js.py)
  // ---------------------------------------------------------------------

  function _isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  // Tolerant read: bad JSON -> [], bad entries skipped, a later duplicate
  // name (case-insensitive) wins and takes the later position — the same
  // contract as voice_config.parse_styles.
  function parseStyles(raw) {
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
      if (!_isObject(item)) continue;
      const name = typeof item.name === "string" ? item.name.trim() : "";
      if (!name || name.length > MAX_NAME_CHARS) continue;
      let instruction = item.instruction == null ? "" : item.instruction;
      if (typeof instruction !== "string") continue;
      instruction = instruction.trim();
      if (instruction.length > MAX_INSTRUCTION_CHARS) continue;
      const inMenu = item.in_menu === undefined ? true : item.in_menu;
      if (typeof inMenu !== "boolean") continue;
      const key = name.toLowerCase();
      if (!out.has(key) && out.size >= MAX_STYLES) continue;
      out.delete(key); // re-insert so a later duplicate also takes the later row
      out.set(key, { name: name, instruction: instruction, in_menu: inMenu });
    }
    return Array.from(out.values());
  }

  function serializeStyles(styles) {
    return JSON.stringify(
      styles.map(function (style) {
        return {
          name: style.name,
          instruction: style.instruction,
          in_menu: style.in_menu,
        };
      }),
    );
  }

  // Display state for one row.  The default match is case-insensitive, as
  // resolve_style is on the server.
  function styleRows(styles, defaultName) {
    const wanted = (defaultName || "").trim().toLowerCase();
    return (styles || []).map(function (style) {
      return {
        name: style.name,
        instruction: style.instruction,
        instructionText: style.instruction || "(no instruction)",
        inMenu: !!style.in_menu,
        inMenuText: style.in_menu ? "yes" : "\u2014",
        isDefault: !!wanted && style.name.trim().toLowerCase() === wanted,
      };
    });
  }

  // The strict client-side mirror of the server rules the shelf can check
  // itself: required/unique name, the two caps.  editIndex is null on
  // create and the row being replaced on edit (so keeping its own name is
  // not a duplicate).
  function validateDraft(draft, styles, editIndex) {
    const name = ((draft && draft.name) || "").trim();
    if (!name) return "Name is required";
    if (name.length > MAX_NAME_CHARS) {
      return "Name is longer than " + MAX_NAME_CHARS + " characters";
    }
    const instruction = (draft && draft.instruction) || "";
    if (instruction.length > MAX_INSTRUCTION_CHARS) {
      return "Instruction is longer than " + MAX_INSTRUCTION_CHARS + " characters";
    }
    const key = name.toLowerCase();
    for (let i = 0; i < (styles || []).length; i++) {
      if (i === editIndex) continue;
      if (styles[i].name.trim().toLowerCase() === key) {
        return 'A style named "' + name + '" already exists';
      }
    }
    if (editIndex == null && (styles || []).length >= MAX_STYLES) {
      return "At most " + MAX_STYLES + " speaking styles";
    }
    return null;
  }

  // The next whole list: the draft normalized the way the server will
  // normalize it, replacing editIndex or appended on create.
  function applyDraft(styles, editIndex, draft) {
    const style = {
      name: ((draft && draft.name) || "").trim(),
      instruction: ((draft && draft.instruction) || "").trim(),
      in_menu: draft && draft.in_menu !== undefined ? !!draft.in_menu : true,
    };
    const out = (styles || []).map(function (current) {
      return {
        name: current.name,
        instruction: current.instruction,
        in_menu: !!current.in_menu,
      };
    });
    if (editIndex == null || editIndex < 0 || editIndex >= out.length) out.push(style);
    else out[editIndex] = style;
    return out;
  }

  // Which `audio.tts_style` write follows this save, if any.
  //   null   -> leave the default setting alone
  //   ""     -> clear it (the edited row was the default and is no longer)
  //   name   -> make the saved/renamed style the default
  function defaultUpdateFor(currentDefault, oldName, newName, makeDefault) {
    const current = (currentDefault || "").trim().toLowerCase();
    const wasDefault =
      !!oldName && current !== "" && current === oldName.trim().toLowerCase();
    if (makeDefault) return newName;
    if (wasDefault) return "";
    return null;
  }

  // Deleting the default style also clears the default setting.
  function deleteUpdate(styles, defaultName, index) {
    const target = (styles || [])[index];
    if (!target) return null;
    const remaining = styles.filter(function (_style, i) {
      return i !== index;
    });
    const wanted = (defaultName || "").trim().toLowerCase();
    return {
      styles: remaining,
      clearDefault: wanted !== "" && wanted === target.name.trim().toLowerCase(),
    };
  }

  // The two writes a save produces, in order: the whole list, then (when
  // the default choice moves) audio.tts_style.  Pure so the request bodies
  // are pinned by the node tests.
  function savePlan(styles, editIndex, draft, currentDefault, makeDefault) {
    const oldName =
      editIndex == null || !styles[editIndex] ? null : styles[editIndex].name;
    const next = applyDraft(styles, editIndex, draft);
    return {
      styles: next,
      stylesValue: serializeStyles(next),
      defaultStyle: defaultUpdateFor(
        currentDefault,
        oldName,
        ((draft && draft.name) || "").trim(),
        !!makeDefault,
      ),
    };
  }

  // ---------------------------------------------------------------------
  // DOM: mount, render, shelf, confirm
  // ---------------------------------------------------------------------

  let _mountSeq = 0;

  function _canEdit() {
    if (typeof window.hasPermission === "function") {
      return window.hasPermission("admin.settings");
    }
    // Deliberate copy of auth.js's storage parse, via window like admin.js's
    // cache-skew shim: absent storage means "not granted", never "unknown".
    try {
      const perms = window.sessionStorage
        ? window.sessionStorage.getItem("turnstone_permissions") || ""
        : "";
      return perms.split(",").indexOf("admin.settings") !== -1;
    } catch (e) {
      return false;
    }
  }

  function _refreshSettings() {
    if (typeof window.voiceSettingsRefresh === "function") {
      try {
        window.voiceSettingsRefresh();
      } catch (e) {
        /* the Settings tab's refresh must not break the Styles tab */
      }
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

  function _putSetting(key, value) {
    return authFetch(SETTINGS_URL + "/" + encodeURIComponent(key), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: value }),
    }).then(function (r) {
      if (!r.ok) return _failFrom(r, "Save failed");
      return r.json();
    });
  }

  function _deleteSetting(key) {
    return authFetch(SETTINGS_URL + "/" + encodeURIComponent(key), {
      method: "DELETE",
    }).then(function (r) {
      if (!r.ok) return _failFrom(r, "Reset failed");
      return r.json();
    });
  }

  function _storedValue(data, fallback) {
    return data && typeof data.value === "string" ? data.value : fallback;
  }

  function _showError(root, message) {
    const errorEl = root._vs.els.error;
    errorEl.textContent = message;
    errorEl.classList.add("is-visible");
  }

  function _clearError(root) {
    const errorEl = root._vs.els.error;
    errorEl.textContent = "";
    errorEl.classList.remove("is-visible");
  }

  function _syncCounter(root) {
    root._vs.els.count.textContent = String(root._vs.els.instruction.value.length);
  }

  function _rowHtml(row, index, writable) {
    const name = escapeHtml(row.name);
    let html =
      '<div class="admin-row voice-styles-grid" role="listitem">' +
      '<span class="admin-col admin-col-vs-name">' +
      name +
      "</span>" +
      '<span class="admin-col admin-col-vs-instruction' +
      (row.instruction ? "" : " is-empty") +
      '">' +
      escapeHtml(row.instructionText) +
      "</span>" +
      '<span class="admin-col admin-col-vs-menu">' +
      row.inMenuText +
      "</span>" +
      '<span class="admin-col admin-col-vs-default">';
    if (row.isDefault) {
      html +=
        '<span class="voice-styles-default" title="Default style">' +
        "\u25cf" +
        '<span class="sr-only"> default</span></span>';
    }
    html += "</span>";
    if (writable) {
      let actions =
        '<span class="voice-styles-actions">' +
        '<button type="button" class="admin-btn-action" data-vs-action="edit" ' +
        'data-vs-index="' +
        index +
        '" aria-label="Edit ' +
        name +
        '">Edit</button>';
      if (!row.isDefault) {
        actions +=
          '<button type="button" class="admin-btn-action" data-vs-action="default" ' +
          'data-vs-index="' +
          index +
          '" aria-label="Make ' +
          name +
          ' the default style">Make default</button>';
      }
      actions +=
        '<button type="button" class="admin-btn-danger" data-vs-action="delete" ' +
        'data-vs-index="' +
        index +
        '" aria-label="Delete ' +
        name +
        '">Delete</button></span>';
      html += '<span class="admin-col admin-col-actions">' + actions + "</span>";
    }
    return html + "</div>";
  }

  // The permission-dependent chrome only: toolbar + ACTIONS column.  Kept
  // separate from _render so mounting can gate the static markup without
  // replacing the "Loading..." placeholder before the first fetch lands.
  function _syncChrome(root) {
    const writable = _canEdit();
    root.classList.toggle("is-readonly", !writable);
    const toolbar = root.querySelector("[data-vs-toolbar-actions]");
    if (toolbar) toolbar.hidden = !writable;
    const actionsHeader = root.querySelector("[data-vs-actions-header]");
    if (actionsHeader) actionsHeader.hidden = !writable;
    return writable;
  }

  function _render(root) {
    const state = root._vs;
    const writable = _syncChrome(root);

    const listEl = root.querySelector("[data-vs-list]");
    if (!listEl) return;
    if (!state.styles.length) {
      setSafeHtml(
        listEl,
        '<div class="dashboard-empty">' +
          (writable
            ? "No speaking styles. Use + New style to add one."
            : "No speaking styles are defined.") +
          "</div>",
      );
      return;
    }
    const rows = styleRows(state.styles, state.defaultName);
    let html = "";
    for (let i = 0; i < rows.length; i++) html += _rowHtml(rows[i], i, writable);
    setSafeHtml(listEl, html);
  }

  function _load(root) {
    const state = root._vs;
    const seq = ++state.seq;
    return authFetch(SETTINGS_URL)
      .then(function (r) {
        if (!r.ok) throw new Error("Failed to load settings");
        return r.json();
      })
      .then(function (data) {
        if (seq !== state.seq) return; // a newer load already answered
        const items = (data && data.settings) || [];
        let stylesRaw = "[]";
        let defaultName = "";
        let source = "default";
        for (let i = 0; i < items.length; i++) {
          const item = items[i];
          if (item.key === STYLES_KEY) {
            stylesRaw = typeof item.value === "string" ? item.value : "[]";
            source = item.source === "storage" ? "storage" : "default";
          } else if (item.key === DEFAULT_KEY) {
            defaultName = typeof item.value === "string" ? item.value : "";
          }
        }
        state.styles = parseStyles(stylesRaw);
        state.defaultName = defaultName;
        state.stylesSource = source;
        state.loadedOnce = true;
        _render(root);
      })
      .catch(function () {
        if (seq !== state.seq) return;
        state.loadedOnce = true;
        const listEl = root.querySelector("[data-vs-list]");
        if (!listEl) return;
        setSafeHtml(
          listEl,
          '<div class="dashboard-empty">Failed to load speaking styles</div>',
        );
      });
  }

  function _openShelf(root, index) {
    const state = root._vs;
    const els = state.els;
    const editing = index != null;
    const style = editing ? state.styles[index] : null;
    if (editing && !style) return;
    if (!_canEdit()) return;
    state.editingIndex = editing ? index : null;
    _clearError(root);
    els.name.value = style ? style.name : "";
    els.instruction.value = style ? style.instruction : "";
    els.inMenu.checked = style ? !!style.in_menu : true;
    els.default.checked = style
      ? styleRows([style], state.defaultName)[0].isDefault
      : false;
    els.title.textContent = editing
      ? "Edit speaking style \u2014 " + style.name
      : "New speaking style";
    els.tag.textContent = editing ? "STY-EDIT" : "STY-NEW";
    els.shelf.setAttribute("data-kind", editing ? "edit" : "create");
    els.submit.textContent = editing ? "Save" : "Create";
    _syncCounter(root);
    window.TurnstoneHatch.setBusy(els.shelf, false);
    window.TurnstoneHatch.openShelf(els.shelf);
    els.name.focus();
  }

  function _submit(root) {
    const state = root._vs;
    const els = state.els;
    if (els.shelf.hasAttribute("data-busy")) return;
    const draft = {
      name: els.name.value,
      instruction: els.instruction.value,
      in_menu: els.inMenu.checked,
    };
    const problem = validateDraft(draft, state.styles, state.editingIndex);
    if (problem) {
      _showError(root, problem);
      return;
    }
    const editing = state.editingIndex != null;
    const plan = savePlan(
      state.styles,
      state.editingIndex,
      draft,
      state.defaultName,
      els.default.checked,
    );
    let stylesSaved = false;

    _clearError(root);
    window.TurnstoneHatch.setBusy(els.shelf, true);
    _putSetting(STYLES_KEY, plan.stylesValue)
      .then(function (data) {
        stylesSaved = true;
        state.styles = parseStyles(_storedValue(data, plan.stylesValue));
        if (plan.defaultStyle === null) return null;
        return _putSetting(DEFAULT_KEY, plan.defaultStyle).then(function () {
          state.defaultName = plan.defaultStyle;
        });
      })
      .then(function () {
        window.TurnstoneHatch.setBusy(els.shelf, false);
        window.TurnstoneHatch.closeShelf(els.shelf);
        _render(root);
        showToast(
          editing ? "Speaking style updated" : "Speaking style created",
        );
        _refreshSettings();
      })
      .catch(function (err) {
        window.TurnstoneHatch.setBusy(els.shelf, false);
        if (stylesSaved) {
          // Half-saved: the list is honest, the default choice is not.  Close
          // the shelf (a retry would collide with the row just created and
          // read as a duplicate) and point at the failure.
          window.TurnstoneHatch.closeShelf(els.shelf);
          _render(root);
          _refreshSettings();
          showToast(
            "Style saved, but the default could not be updated: " + err.message,
            "warn",
          );
        } else {
          _showError(root, err.message);
        }
      });
  }

  function _makeDefault(root, index) {
    const state = root._vs;
    const style = state.styles[index];
    if (!style || !_canEdit()) return;
    _putSetting(DEFAULT_KEY, style.name)
      .then(function () {
        state.defaultName = style.name;
        _render(root);
        showToast("\u201c" + style.name + "\u201d is now the default style");
        _refreshSettings();
      })
      .catch(function (err) {
        showToast("Error: " + err.message, "error");
      });
  }

  function _deleteStyle(root, index) {
    const state = root._vs;
    const update = deleteUpdate(state.styles, state.defaultName, index);
    if (!update || !_canEdit()) return;
    const localValue = serializeStyles(update.styles);
    let stylesSaved = false;
    _putSetting(STYLES_KEY, localValue)
      .then(function (data) {
        stylesSaved = true;
        state.styles = parseStyles(_storedValue(data, localValue));
        if (!update.clearDefault) return null;
        return _putSetting(DEFAULT_KEY, "").then(function () {
          state.defaultName = "";
        });
      })
      .then(function () {
        _render(root);
        showToast("Speaking style deleted");
        _refreshSettings();
      })
      .catch(function (err) {
        if (stylesSaved) {
          _render(root);
          _refreshSettings();
          showToast(
            "Speaking style deleted, but the default could not be cleared: " +
              err.message,
            "warn",
          );
        } else {
          showToast("Error: " + err.message, "error");
        }
      });
  }

  function _confirm(root, title, prose, actionLabel, onConfirm) {
    const els = root._vs.els;
    els.confirmTitle.textContent = title;
    els.confirmProse.textContent = prose;
    els.confirmAction.textContent = actionLabel;
    root._vs.confirmAction = onConfirm;
    window.TurnstoneHatch.openDialog(els.confirm);
  }

  function _confirmDelete(root, index) {
    const state = root._vs;
    const style = state.styles[index];
    if (!style || !_canEdit()) return;
    const isDefault = styleRows([style], state.defaultName)[0].isDefault;
    _confirm(
      root,
      "Delete speaking style",
      "Delete \u201c" +
        style.name +
        "\u201d?" +
        (isDefault
          ? " It is the default style, so the default will be cleared."
          : ""),
      "Delete",
      function () {
        _deleteStyle(root, index);
      },
    );
  }

  function _resetStyles(root) {
    _deleteSetting(STYLES_KEY)
      .then(function () {
        showToast("Speaking styles reset to defaults");
        _refreshSettings();
        return _load(root);
      })
      .catch(function (err) {
        showToast("Error: " + err.message, "error");
      });
  }

  function _confirmReset(root) {
    if (!_canEdit()) return;
    if (root._vs.stylesSource !== "storage") {
      showToast("Speaking styles are already the defaults");
      return;
    }
    _confirm(
      root,
      "Reset speaking styles",
      "Reset the speaking styles to the built-in defaults? Custom styles are " +
        "removed. The default style setting is unchanged.",
      "Reset",
      function () {
        _resetStyles(root);
      },
    );
  }

  function _rootMarkup() {
    return (
      '<div class="admin-toolbar voice-styles-toolbar">' +
      '<p class="voice-styles-explainer">Styles offered beside the read-aloud ' +
      "button. The instruction is sent only to TTS models that accept " +
      "instructions.</p>" +
      '<div class="voice-styles-toolbar-actions" data-vs-toolbar-actions>' +
      '<button type="button" class="admin-btn-action" data-vs-action="reset">' +
      "Reset to defaults</button>" +
      '<button type="button" class="admin-action-btn" data-vs-action="new">' +
      "+ New style</button>" +
      "</div></div>" +
      '<div class="admin-colheaders voice-styles-grid" aria-hidden="true">' +
      '<span class="admin-col admin-col-vs-name">NAME</span>' +
      '<span class="admin-col admin-col-vs-instruction">INSTRUCTION</span>' +
      '<span class="admin-col admin-col-vs-menu">IN MENU</span>' +
      '<span class="admin-col admin-col-vs-default">DEFAULT</span>' +
      '<span class="admin-col admin-col-actions" data-vs-actions-header>' +
      "ACTIONS</span></div>" +
      '<div class="voice-styles-list" data-vs-list role="list" ' +
      'aria-label="Speaking styles" aria-live="polite">' +
      '<div class="dashboard-empty">Loading speaking styles...</div></div>'
    );
  }

  function _shelfMarkup(prefix) {
    return (
      '<dialog class="hatch hatch--shelf" id="' +
      prefix +
      '-shelf" data-kind="create" aria-labelledby="' +
      prefix +
      '-shelf-title">' +
      '<header class="sh-head">' +
      '<span class="sh-led" aria-hidden="true"></span>' +
      '<h2 class="sh-title" id="' +
      prefix +
      '-shelf-title">New speaking style</h2>' +
      '<span class="sh-tag" aria-hidden="true" id="' +
      prefix +
      '-shelf-tag">STY-NEW</span>' +
      '<button class="sh-x" data-close aria-label="Close">\u2715</button>' +
      "</header>" +
      '<div class="sh-body">' +
      '<div class="sh-alert" id="' +
      prefix +
      '-shelf-error" role="alert" aria-live="assertive" ' +
      'aria-atomic="true"></div>' +
      '<label for="' +
      prefix +
      '-name">Name <span class="label-hint">max ' +
      MAX_NAME_CHARS +
      " characters</span></label>" +
      '<input id="' +
      prefix +
      '-name" type="text" maxlength="' +
      MAX_NAME_CHARS +
      '" autocomplete="off" spellcheck="false" />' +
      '<label for="' +
      prefix +
      '-instruction">Instruction <span class="label-hint">' +
      '<span id="' +
      prefix +
      '-count" class="voice-styles-count">0</span> / ' +
      MAX_INSTRUCTION_CHARS +
      "</span></label>" +
      '<textarea id="' +
      prefix +
      '-instruction" maxlength="' +
      MAX_INSTRUCTION_CHARS +
      '" rows="5" ' +
      'placeholder="How the voice should read. Empty means plain delivery.">' +
      "</textarea>" +
      '<div class="toggle-stack">' +
      '<label class="toggle-switch"><input type="checkbox" id="' +
      prefix +
      '-in-menu" checked /><span class="toggle-track" aria-hidden="true"></span>' +
      '<span class="toggle-label">In menu</span></label>' +
      '<label class="toggle-switch"><input type="checkbox" id="' +
      prefix +
      '-default" /><span class="toggle-track" aria-hidden="true"></span>' +
      '<span class="toggle-label">Default style</span></label>' +
      "</div></div>" +
      '<footer class="sh-foot"><div class="sh-foot-meta"></div>' +
      '<button class="sh-btn" data-close>Cancel</button>' +
      '<button class="sh-btn sh-btn--primary" id="' +
      prefix +
      '-submit">Create</button></footer></dialog>'
    );
  }

  function _confirmMarkup(prefix) {
    return (
      '<dialog class="hatch hatch--dialog" data-kind="danger" ' +
      'aria-labelledby="' +
      prefix +
      '-confirm-title" aria-describedby="' +
      prefix +
      '-confirm-prose">' +
      '<header class="sh-head">' +
      '<span class="sh-led" aria-hidden="true"></span>' +
      '<h2 class="sh-title" id="' +
      prefix +
      '-confirm-title">Confirm</h2>' +
      '<button class="sh-x" data-close aria-label="Close">\u2715</button>' +
      "</header>" +
      '<div class="sh-body"><p class="sh-prose" id="' +
      prefix +
      '-confirm-prose"></p></div>' +
      '<footer class="sh-foot"><div class="sh-foot-meta"></div>' +
      '<button class="sh-btn" data-close>Cancel</button>' +
      '<button class="sh-btn sh-btn--danger" id="' +
      prefix +
      '-confirm-action">Delete</button></footer></dialog>'
    );
  }

  function _dialogFromMarkup(html) {
    const holder = document.createElement("div");
    setSafeHtml(holder, html);
    return holder.querySelector("dialog");
  }

  function _mount(container) {
    const host = container.closest ? container.closest(".hatch-host") : null;
    const dialogHost = host || container;
    if (!host && dialogHost.style) dialogHost.style.position = "relative";

    const prefix = "voice-styles-" + ++_mountSeq;
    const root = document.createElement("div");
    root.className = "voice-styles-root";
    setSafeHtml(root, _rootMarkup());
    container.appendChild(root);

    // The shelf/dialog are siblings of the pane content (direct children of
    // the .hatch-host), exactly like the static admin shelves: openShelf
    // inerts the host's other children, so a dialog INSIDE the content
    // would inert itself.
    const shelf = _dialogFromMarkup(_shelfMarkup(prefix));
    dialogHost.appendChild(shelf);
    const confirm = _dialogFromMarkup(_confirmMarkup(prefix));
    dialogHost.appendChild(confirm);

    const state = {
      seq: 0,
      loadedOnce: false,
      styles: [],
      defaultName: "",
      stylesSource: "default",
      editingIndex: null,
      confirmAction: null,
      dialogs: [shelf, confirm],
      els: {
        error: shelf.querySelector("#" + prefix + "-shelf-error"),
        name: shelf.querySelector("#" + prefix + "-name"),
        instruction: shelf.querySelector("#" + prefix + "-instruction"),
        count: shelf.querySelector("#" + prefix + "-count"),
        inMenu: shelf.querySelector("#" + prefix + "-in-menu"),
        default: shelf.querySelector("#" + prefix + "-default"),
        submit: shelf.querySelector("#" + prefix + "-submit"),
        title: shelf.querySelector("#" + prefix + "-shelf-title"),
        tag: shelf.querySelector("#" + prefix + "-shelf-tag"),
        shelf: shelf,
        confirm: confirm,
        confirmTitle: confirm.querySelector("#" + prefix + "-confirm-title"),
        confirmProse: confirm.querySelector("#" + prefix + "-confirm-prose"),
        confirmAction: confirm.querySelector("#" + prefix + "-confirm-action"),
      },
    };
    root._vs = state;
    _syncChrome(root);

    root.addEventListener("click", function (ev) {
      const button = ev.target.closest("[data-vs-action]");
      if (!button) return;
      const action = button.getAttribute("data-vs-action");
      const index = parseInt(button.getAttribute("data-vs-index"), 10);
      if (action === "new") _openShelf(root, null);
      else if (action === "reset") _confirmReset(root);
      else if (action === "edit") _openShelf(root, index);
      else if (action === "default") _makeDefault(root, index);
      else if (action === "delete") _confirmDelete(root, index);
    });
    state.els.submit.addEventListener("click", function () {
      _submit(root);
    });
    state.els.instruction.addEventListener("input", function () {
      _syncCounter(root);
    });
    state.els.confirmAction.addEventListener("click", function () {
      const action = state.confirmAction;
      state.confirmAction = null;
      if (confirm.open) confirm.close();
      if (action) action();
    });

    // A first render can beat whoami's permission list; once the data is in,
    // re-render when the scopes settle so an authorized operator does not
    // keep a read-only list until the next tab switch.
    if (typeof window.whenPermissionsReady === "function") {
      window.whenPermissionsReady(function () {
        if (state.loadedOnce) _render(root);
      });
    }

    container._voiceStylesRoot = root;
    return root;
  }

  function _ensureMounted(container) {
    const existing = container._voiceStylesRoot;
    if (existing && existing._vs && container.contains(existing)) return existing;
    if (existing) {
      if (existing._vs) {
        for (let i = 0; i < existing._vs.dialogs.length; i++) {
          const dialog = existing._vs.dialogs[i];
          if (dialog.parentNode) dialog.parentNode.removeChild(dialog);
        }
      }
      if (existing.parentNode) existing.parentNode.removeChild(existing);
    }
    return _mount(container);
  }

  window.loadVoiceStyles = function (container) {
    if (!container) return Promise.resolve();
    return _load(_ensureMounted(container));
  };

  window.TurnstoneVoiceStyles = {
    MAX_STYLES: MAX_STYLES,
    MAX_NAME_CHARS: MAX_NAME_CHARS,
    MAX_INSTRUCTION_CHARS: MAX_INSTRUCTION_CHARS,
    parseStyles: parseStyles,
    serializeStyles: serializeStyles,
    styleRows: styleRows,
    validateDraft: validateDraft,
    applyDraft: applyDraft,
    defaultUpdateFor: defaultUpdateFor,
    deleteUpdate: deleteUpdate,
    savePlan: savePlan,
  };
})();
