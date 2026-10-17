/* Admin › Voice — page shell and the Settings tab.

   The page mirrors the Judge tab: an .admin-subtab-switcher over a set of
   sections, each section a role="tabpanel" host in index.html.  This file owns
   the shell and the Settings section; the Dictionary and Styles editors are
   separate slices and mount through window.TS_VOICE.registerSection(section,
   loader) — see _loadVoiceSection.  Each loader runs once, the first time its
   section is opened, so a large dictionary costs nothing until it is asked for.

   The Settings section edits the audio.* settings the general Settings tab no
   longer shows.  The stt/tts model aliases stay in Models → Roles, and the two
   JSON-list settings have their own tabs here. */

let _voiceSettings = [];
const _voiceSectionLoaders = {};
const _voiceSectionLoaded = {};

// The Settings tab's deliberate order, split by direction.  This is also the
// sort order for _voiceSettingsFrom; a future audio.* setting with no group
// lands in "Other" rather than disappearing from the page.
const VOICE_SETTING_GROUPS = [
  {
    id: "dictation",
    label: "Dictation",
    keys: [
      "audio.stt_prompt",
      "audio.stt_dictionary_hint",
      "audio.stt_dictionary_correct",
    ],
  },
  {
    id: "read-aloud",
    label: "Read aloud",
    keys: [
      "audio.tts_voice",
      "audio.tts_max_chars",
      "audio.tts_timeout_seconds",
      "audio.tts_normalize",
      "audio.tts_style",
      "audio.tts_pronunciations",
    ],
  },
];

// Mirrors MAX_STYLES in turnstone/core/voice_config.py.  The tolerant read
// there ignores a style past the cap, so offering one here would promise a
// delivery the read-aloud path cannot resolve.
const VOICE_MAX_STYLES = 20;

// ---------------------------------------------------------------------------
// Sub-section switcher
// ---------------------------------------------------------------------------

function switchVoiceSection(section) {
  const sections = document.querySelectorAll(".voice-section");
  for (let i = 0; i < sections.length; i++) sections[i].style.display = "none";
  const switcher = document.querySelector(
    "#admin-voice .admin-subtab-switcher",
  );
  const btns = switcher ? switcher.querySelectorAll(".admin-subtab-btn") : [];
  for (let i = 0; i < btns.length; i++) {
    const isActive = btns[i].getAttribute("data-section") === section;
    btns[i].classList.toggle("active", isActive);
    btns[i].setAttribute("aria-selected", isActive ? "true" : "false");
    btns[i].setAttribute("tabindex", isActive ? "0" : "-1");
  }
  const target = document.getElementById(section + "-section");
  if (target) target.style.display = "";
  // Sections other than Settings mount an editor owned by another slice;
  // warm it on first open (and only then).
  if (section !== "voice-settings") _loadVoiceSection(section);
}

// Arrow key navigation for voice sub-section tabs.
(function () {
  const switcher = document.querySelector(
    "#admin-voice .admin-subtab-switcher",
  );
  if (!switcher) return;
  switcher.addEventListener("keydown", function (e) {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const btns = switcher.querySelectorAll(".admin-subtab-btn");
    const secs = [];
    for (let i = 0; i < btns.length; i++)
      secs.push(btns[i].getAttribute("data-section"));
    const current = switcher.querySelector(".admin-subtab-btn.active");
    let idx = secs.indexOf(current ? current.getAttribute("data-section") : "");
    if (e.key === "ArrowRight") idx = (idx + 1) % secs.length;
    else idx = (idx - 1 + secs.length) % secs.length;
    e.preventDefault();
    switchVoiceSection(secs[idx]);
    btns[idx].focus();
  });
})();

// ---------------------------------------------------------------------------
// Section mount seam (Dictionary / Styles slices)
// ---------------------------------------------------------------------------

function _voiceSectionLabel(section) {
  if (section === "voice-dictionary") return "dictionary";
  if (section === "voice-styles") return "speech-style";
  return "section";
}

function _loadVoiceSection(section) {
  if (_voiceSectionLoaded[section]) return;
  const registered = _voiceSectionLoaders[section];
  let loader = typeof registered === "function" ? registered : null;
  // Conventional names let a slice stay a plain classic script without
  // reaching for the registry; the registry wins when both exist.
  if (!loader && section === "voice-dictionary")
    loader =
      typeof window.loadVoiceDictionary === "function"
        ? window.loadVoiceDictionary
        : null;
  if (!loader && section === "voice-styles")
    loader =
      typeof window.loadVoiceStyles === "function"
        ? window.loadVoiceStyles
        : null;
  if (loader) {
    loader();
    _voiceSectionLoaded[section] = true;
    return;
  }
  const container = document.getElementById(section + "-container");
  if (container) {
    setSafeHtml(
      container,
      '<div class="dashboard-empty">The ' +
        _voiceSectionLabel(section) +
        " editor is not loaded.</div>",
    );
  }
}

window.TS_VOICE = window.TS_VOICE || {};
window.TS_VOICE.registerSection = function (section, loader) {
  if (typeof section === "string" && typeof loader === "function") {
    _voiceSectionLoaders[section] = loader;
  }
};
window.TS_VOICE.switchSection = switchVoiceSection;
window.TS_VOICE.reload = loadVoiceTab;

// ---------------------------------------------------------------------------
// Load
// ---------------------------------------------------------------------------

function loadVoiceTab() {
  loadVoiceSettings();
}

function loadVoiceSettings() {
  const c = document.getElementById("voice-settings-container");
  if (!c) return;
  Promise.all([
    authFetch("/v1/api/admin/settings").then(function (r) {
      if (!r.ok) throw new Error("Failed to load settings");
      return r.json();
    }),
    authFetch("/v1/api/admin/settings/schema").then(function (r) {
      if (!r.ok) throw new Error("Failed to load schema");
      return r.json();
    }),
  ])
    .then(function (results) {
      _voiceSettings = _voiceSettingsFrom(
        results[0].settings || [],
        results[1].schema || [],
      );
      renderVoiceSettings();
    })
    .catch(function (err) {
      setSafeHtml(
        c,
        '<div class="dashboard-empty">Failed to load voice settings: ' +
          escapeHtml(err.message || String(err)) +
          "</div>",
      );
    });
}

// ---------------------------------------------------------------------------
// Settings model
// ---------------------------------------------------------------------------

// Tolerant read of audio.tts_styles, matching parse_styles: bad JSON is [],
// non-object entries are skipped, names are unique case-insensitively, the
// later duplicate wins and the cap drops new names rather than the list.
function _voiceStyleNames(raw) {
  let parsed;
  try {
    parsed = JSON.parse(raw || "[]");
  } catch (e) {
    return [];
  }
  if (!Array.isArray(parsed)) return [];
  const names = [];
  const index = {};
  for (let i = 0; i < parsed.length; i++) {
    const item = parsed[i];
    if (!item || typeof item !== "object") continue;
    if (typeof item.name !== "string") continue;
    const name = item.name.trim();
    if (!name) continue;
    const key = name.toLowerCase();
    if (index[key] !== undefined) {
      // Same name in a different case: the later entry is the style.
      names[index[key]] = name;
      continue;
    }
    if (names.length >= VOICE_MAX_STYLES) continue;
    index[key] = names.length;
    names.push(name);
  }
  return names;
}

// Choices for the default-style select.  "" is the no-instruction option.  A
// stored name that no longer exists in the list is kept as an option: dropping
// it would paint the first style while the stored value said something else.
function _voiceStyleChoices(raw, current) {
  const names = _voiceStyleNames(raw);
  const wanted = (current || "").trim();
  if (!wanted) return { choices: [""].concat(names), value: "" };
  for (let i = 0; i < names.length; i++) {
    if (names[i].toLowerCase() === wanted.toLowerCase()) {
      return { choices: [""].concat(names), value: names[i] };
    }
  }
  return { choices: [""].concat(names, wanted), value: wanted };
}

// Merge the values + schema lists into the rows this tab renders.  Mirrors the
// merge in loadSettings, minus everything another surface owns: all audio.* is
// skipped by the general Settings tab now, the stt/tts aliases are pickers in
// Models → Roles (derived from MODEL_ROLES so a future role cannot drift back
// in), and the two JSON lists have Dictionary / Styles tabs.
function _voiceSettingsFrom(valuesArr, schemaArr) {
  const schemaMap = {};
  for (let i = 0; i < schemaArr.length; i++) {
    schemaMap[schemaArr[i].key] = schemaArr[i];
  }

  const hidden = {
    "audio.voice_dictionary": 1,
    "audio.tts_styles": 1,
  };
  for (let r = 0; r < MODEL_ROLES.length; r++) {
    hidden[MODEL_ROLES[r].aliasKey] = 1;
  }

  let stylesRaw = "[]";
  for (let i = 0; i < valuesArr.length; i++) {
    if (valuesArr[i].key === "audio.tts_styles") {
      stylesRaw = valuesArr[i].value || "[]";
      break;
    }
  }

  const items = [];
  for (let i = 0; i < valuesArr.length; i++) {
    const v = valuesArr[i];
    if (!v.key.startsWith("audio.")) continue;
    if (hidden[v.key]) continue;
    const s = schemaMap[v.key] || {};
    const item = {
      key: v.key,
      value: v.value,
      source: v.source,
      type: v.type || s.type || "str",
      default_value: s.default !== undefined ? s.default : "",
      description: v.description || s.description || "",
      section: v.section || s.section || "",
      is_secret: v.is_secret || false,
      min_value: s.min_value,
      max_value: s.max_value,
      choices: s.choices || null,
      restart_required: v.restart_required || false,
      changed_by: v.changed_by || "",
      updated: v.updated || "",
      help: s.help || "",
      reference_url: s.reference_url || "",
    };
    if (item.key === "audio.tts_style") {
      const pick = _voiceStyleChoices(stylesRaw, item.value);
      item.choices = pick.choices;
      item.value = pick.value;
    }
    if (item.key === "audio.tts_pronunciations") item.multiline = true;
    items.push(item);
  }

  const order = [];
  for (let g = 0; g < VOICE_SETTING_GROUPS.length; g++) {
    const keys = VOICE_SETTING_GROUPS[g].keys;
    for (let k = 0; k < keys.length; k++) order.push(keys[k]);
  }
  items.sort(function (a, b) {
    const ai = order.indexOf(a.key);
    const bi = order.indexOf(b.key);
    if (ai === -1 && bi === -1) return a.key < b.key ? -1 : a.key > b.key ? 1 : 0;
    if (ai === -1) return 1;
    if (bi === -1) return -1;
    return ai - bi;
  });
  return items;
}

function _voiceSettingGroups(items) {
  const byKey = {};
  for (let i = 0; i < items.length; i++) byKey[items[i].key] = items[i];
  const used = {};
  const groups = [];
  for (let g = 0; g < VOICE_SETTING_GROUPS.length; g++) {
    const def = VOICE_SETTING_GROUPS[g];
    const group = { id: def.id, label: def.label, items: [] };
    for (let k = 0; k < def.keys.length; k++) {
      const item = byKey[def.keys[k]];
      if (!item) continue;
      group.items.push(item);
      used[item.key] = 1;
    }
    if (group.items.length) groups.push(group);
  }
  const other = [];
  for (let i = 0; i < items.length; i++) {
    if (!used[items[i].key]) other.push(items[i]);
  }
  if (other.length) groups.push({ id: "other", label: "Other", items: other });
  return groups;
}

// ---------------------------------------------------------------------------
// Settings section
// ---------------------------------------------------------------------------

function renderVoiceSettings() {
  const c = document.getElementById("voice-settings-container");
  if (!c) return;
  const groups = _voiceSettingGroups(_voiceSettings);
  if (!groups.length) {
    setSafeHtml(
      c,
      '<div class="dashboard-empty">No voice settings found</div>',
    );
    return;
  }
  let html =
    '<div class="settings-desc voice-settings-note">The speech-to-text and ' +
    "text-to-speech model roles live in Models \u2192 Roles. The dictionary " +
    "and speaking styles have their own tabs.</div>";
  for (let g = 0; g < groups.length; g++) {
    const group = groups[g];
    html +=
      '<div class="settings-section" data-section="voice-' + group.id + '">';
    html +=
      '<div class="settings-section-header" role="button" tabindex="0" ' +
      'aria-expanded="true" aria-controls="voice-settings-body-' +
      group.id +
      '"><span>' +
      escapeHtml(group.label) +
      "</span></div>";
    html +=
      '<div class="settings-section-body" id="voice-settings-body-' +
      group.id +
      '">';
    for (let i = 0; i < group.items.length; i++) {
      html += _renderSettingRow(group.items[i]);
    }
    html += "</div></div>";
  }
  setSafeHtml(c, html);
  _wireVoiceSettings(c);
}

function _wireVoiceSettings(container) {
  // Collapse headers reuse the Settings tab's machinery: a voice section is
  // the same component, just expanded on first paint.
  const headers = container.querySelectorAll(".settings-section-header");
  for (let h = 0; h < headers.length; h++) {
    headers[h].addEventListener("click", function () {
      _toggleSettingsSection(this);
    });
    headers[h].addEventListener("keydown", function (e) {
      _onSettingsHeaderKey(e, this);
    });
  }

  const inputs = container.querySelectorAll("[data-setting-key]");
  for (let i = 0; i < inputs.length; i++) {
    const inp = inputs[i];
    const key = inp.getAttribute("data-setting-key");
    if (inp.type === "checkbox") {
      // Toggles commit on the flip, like the Judge tab: there is no
      // half-typed state to protect.
      _settingsOriginal[key] = inp.checked;
      inp.addEventListener("change", function () {
        saveVoiceSetting(this.getAttribute("data-setting-key"), this.checked);
      });
    } else {
      _settingsOriginal[key] = inp.value;
      const evt = inp.tagName === "SELECT" ? "change" : "input";
      inp.addEventListener(evt, function () {
        _onSettingChange(this);
      });
    }
  }

  const saveBtns = container.querySelectorAll("[data-save-key]");
  for (let sb = 0; sb < saveBtns.length; sb++) {
    saveBtns[sb].addEventListener("click", function () {
      _saveVoiceSettingFromInput(this.getAttribute("data-save-key"));
    });
  }
  const resetBtns = container.querySelectorAll("[data-reset-key]");
  for (let rb = 0; rb < resetBtns.length; rb++) {
    resetBtns[rb].addEventListener("click", function () {
      resetVoiceSetting(this.getAttribute("data-reset-key"));
    });
  }
}

function _saveVoiceSettingFromInput(key) {
  const input = document.querySelector(
    '[data-setting-key="' + cssEscape(key) + '"]',
  );
  if (!input) return;
  if (input.type === "number" && input.value === "") {
    showToast("Value is required");
    return;
  }
  let value;
  if (input.type === "checkbox") value = input.checked;
  else if (input.type === "number") value = Number(input.value);
  else value = input.value;
  saveVoiceSetting(key, value);
}

function saveVoiceSetting(key, value) {
  authFetch("/v1/api/admin/settings/" + encodeURIComponent(key), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: value }),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error(d.error || "Failed");
        });
      return r.json();
    })
    .then(function () {
      showToast("Setting saved");
      loadVoiceSettings();
    })
    .catch(function (e) {
      showToast("Error: " + e.message);
    });
}

function resetVoiceSetting(key) {
  showConfirmModal(
    "Reset Setting",
    "Reset \u2018" +
      key +
      "\u2019 to its default value? The stored override will be removed.",
    "Reset",
    function () {
      authFetch("/v1/api/admin/settings/" + encodeURIComponent(key), {
        method: "DELETE",
      })
        .then(function (r) {
          if (!r.ok)
            return r.json().then(function (d) {
              throw new Error(d.error || "Failed");
            });
          return r.json();
        })
        .then(function () {
          showToast("Reset to default");
          loadVoiceSettings();
        })
        .catch(function (e) {
          showToast("Error: " + e.message);
        });
    },
  );
}
