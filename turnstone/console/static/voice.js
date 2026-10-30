/* Admin › Voice — page shell and the Settings tab.

   The page mirrors the Judge tab: an .admin-subtab-switcher over a set of
   sections, each section a role="tabpanel" host in index.html.  This file owns
   the shell and the Settings section; the Dictionary and Styles editors are
   separate slices and mount through window.TS_VOICE.registerSection(section,
   loader) — see _loadVoiceSection.  A loader runs each time its section is
   opened and is handed its container; nothing is fetched until it is asked for.

   The Settings section edits the audio.* settings the general Settings tab no
   longer shows.  The stt/tts model aliases stay in Models → Roles, and the two
   JSON-list settings have their own tabs here.  Below the setting groups sits
   the per-model pacing profile: the Pacing, Structure and Clips sections edit
   one alias's overrides in `audio.tts_profiles`, with the TTS model select
   choosing which alias. */

let _voiceSettings = [];
const _voiceSectionLoaders = {};
// Guards against an older GET landing after a newer one (a save re-read racing
// a tab re-entry).
let _voiceLoadSeq = 0;

// The Settings tab's deliberate order, split by direction.  This is also the
// sort order for _voiceSettingsFrom; a future audio.* setting with no group
// lands in "Other" rather than disappearing from the page.
const VOICE_SETTING_GROUPS = [
  {
    // The hands-free loop's master switch comes before the two halves it
    // drives, so the page reads "voice mode, then the dictation and read-aloud
    // settings it uses".
    id: "voice-mode",
    label: "Voice mode",
    keys: ["audio.voice_mode_enabled", "audio.voice_announce_reports"],
  },
  {
    id: "dictation",
    label: "Dictation",
    keys: [
      "audio.stt_prompt",
      "audio.stt_dictionary_hint",
      "audio.stt_dictionary_correct",
      "audio.stt_auto_stop",
      "audio.stt_silence_ms",
      "audio.stt_vad_threshold",
    ],
  },
  {
    // Speaker-gate groundwork, kept apart from dictation: these knobs do not
    // change what the transcriber hears, they only shape the frame stream the
    // future gate will score.  Off by default, so the group is easy to skip.
    id: "speaker-gate",
    label: "Speaker gate (preview)",
    keys: [
      "audio.stt_frame_capture",
      "audio.stt_segment_gap_ms",
      "audio.stt_frame_capture_max_seconds",
    ],
  },
  {
    id: "read-aloud",
    label: "Read aloud",
    keys: [
      "audio.tts_voice",
      "audio.tts_playback_rate",
      "audio.tts_style",
      "audio.tts_pronunciations",
      "audio.tts_stream",
    ],
  },
];

// Mirrors MAX_STYLES in turnstone/core/voice_config.py.  The tolerant read
// there ignores a style past the cap, so offering one here would promise a
// delivery the read-aloud path cannot resolve.
const VOICE_MAX_STYLES = 20;

// The per-model profile editor below the two groups.  Every value lives in one
// JSON setting (`audio.tts_profiles`), keyed by TTS model alias; the server's
// validate_profiles (turnstone/core/voice_config.py) is the authority on the
// field table and its bounds.
const VOICE_PROFILE_KEY = "audio.tts_profiles";
const VOICE_PROFILE_MAX_STR = 60; // MAX_MARKER_CHARS

// Section order; the meta endpoint's profile_fields carry one of these ids.
const VOICE_PROFILE_GROUPS = [
  { id: "pacing", label: "Pacing" },
  { id: "structure", label: "Structure" },
  { id: "clips", label: "Clips" },
];

// The loaded meta endpoint (or the message it failed with), the stored
// override map and the alias the bar shows.  _voiceProfilePending counts
// in-flight writes so the re-read happens after the last one, not the first.
let _voiceMeta = null;
let _voiceMetaError = "";
let _voiceProfiles = {};
let _voiceProfileAlias = "";
let _voiceProfilePending = 0;

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
  const container = document.getElementById(section + "-container");
  if (loader) {
    // Editors take their container and are idempotent: re-running on every
    // open re-reads the stored list, so an edit made elsewhere shows up.
    loader(container);
    return;
  }
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
// The name the Styles editor calls after a save.
window.voiceSettingsRefresh = loadVoiceTab;

// ---------------------------------------------------------------------------
// Load
// ---------------------------------------------------------------------------

function loadVoiceTab() {
  loadVoiceSettings();
}

function loadVoiceSettings() {
  const c = document.getElementById("voice-settings-container");
  if (!c) return;
  const seq = ++_voiceLoadSeq;
  // The profile editor is an addition: a failed meta call leaves the ordinary
  // rows on the page and puts one error line where the editor would be.
  const metaPromise = authFetch("/v1/api/admin/voice/meta")
    .then(function (r) {
      if (!r.ok) throw new Error("Failed to load voice metadata");
      return r.json();
    })
    .then(function (meta) {
      return { meta: meta };
    })
    .catch(function (err) {
      return { error: err.message || String(err) };
    });
  Promise.all([
    authFetch("/v1/api/admin/settings").then(function (r) {
      if (!r.ok) throw new Error("Failed to load settings");
      return r.json();
    }),
    authFetch("/v1/api/admin/settings/schema").then(function (r) {
      if (!r.ok) throw new Error("Failed to load schema");
      return r.json();
    }),
    metaPromise,
  ])
    .then(function (results) {
      if (seq !== _voiceLoadSeq) return; // an older response lost the race
      _voiceSettings = _voiceSettingsFrom(
        results[0].settings || [],
        results[1].schema || [],
      );
      const metaResult = results[2] || {};
      _voiceMeta = metaResult.meta || null;
      _voiceMetaError = metaResult.error || "";
      const fields =
        _voiceMeta && Array.isArray(_voiceMeta.profile_fields)
          ? _voiceMeta.profile_fields
          : [];
      _voiceProfiles = _voiceProfilesFrom(
        _voiceSettingValue(results[0].settings || [], VOICE_PROFILE_KEY),
        fields,
        _voiceProfileMaxStr(),
      );
      _voiceProfileAlias = _voiceProfileResolveAlias(
        _voiceProfileModels(_voiceMeta),
        _voiceProfileAlias,
      );
      renderVoiceSettings();
    })
    .catch(function (err) {
      if (seq !== _voiceLoadSeq) return;
      setSafeHtml(
        c,
        '<div class="dashboard-empty">Failed to load voice settings: ' +
          escapeHtml(err.message || String(err)) +
          "</div>",
      );
    });
}

// The stored value for one settings key; "" when a release has not added it.
function _voiceSettingValue(values, key) {
  for (let i = 0; i < values.length; i++) {
    if (values[i].key === key) return values[i].value;
  }
  return "";
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
    // The two JSON-list settings with editors on their own tabs.
    "audio.voice_dictionary": 1,
    "audio.tts_styles": 1,
    // The per-model profile editor owns this one; the Text cleanup tab owns
    // the rewrite rules and group switches, and the Dictionary tab owns the
    // built-in term switches.  None of them is a plain key/value row.
    "audio.tts_profiles": 1,
    "audio.tts_rewrite_rules": 1,
    "audio.tts_cleanup_disabled": 1,
    "audio.tts_builtin_terms_off": 1,
    // The cleanup master switch moved to the Text cleanup tab.
    "audio.tts_normalize": 1,
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
// Pacing profile model (per TTS model)
// ---------------------------------------------------------------------------
//
// One JSON object, keyed by model alias, holds only the fields an operator
// changed.  The helpers below are pure so the node harness can pin them; the
// server's parse_profiles / validate_profiles remain the authority.

// Meta's tts_models: every alias with speech synthesis, the TTS role's one
// marked active.  Active first so the select opens on the model that is
// actually speaking; one option per alias.
function _voiceProfileModels(meta) {
  const items = meta && Array.isArray(meta.tts_models) ? meta.tts_models : [];
  const seen = {};
  const out = [];
  for (let i = 0; i < items.length; i++) {
    const item = items[i];
    if (!item || typeof item.alias !== "string") continue;
    const alias = item.alias.trim();
    if (!alias || seen[alias]) continue;
    seen[alias] = 1;
    out.push({ alias: alias, active: item.active === true });
  }
  out.sort(function (a, b) {
    return (b.active ? 1 : 0) - (a.active ? 1 : 0);
  });
  return out;
}

// Sections in the order the contract states; a group a later release adds
// still renders, after the three known ones.  Empty groups render nothing.
function _voiceProfileGroups(fields) {
  const byId = {};
  const groups = [];
  const add = function (id, label) {
    byId[id] = { id: id, label: label, fields: [] };
    groups.push(byId[id]);
  };
  for (let i = 0; i < VOICE_PROFILE_GROUPS.length; i++) {
    add(VOICE_PROFILE_GROUPS[i].id, VOICE_PROFILE_GROUPS[i].label);
  }
  const list = Array.isArray(fields) ? fields : [];
  for (let i = 0; i < list.length; i++) {
    const field = list[i];
    if (!field || typeof field.key !== "string" || !field.key) continue;
    const id =
      typeof field.group === "string" && field.group ? field.group : "other";
    if (!byId[id]) add(id, id);
    byId[id].fields.push(field);
  }
  return groups.filter(function (group) {
    return group.fields.length > 0;
  });
}

// One field's typed value from an editor input or a stored value; undefined
// means unusable.  Mirrors _check_profile_value in voice_config.py.
function _voiceProfileScalar(field, raw, maxStrChars) {
  if (!field || typeof field.kind !== "string") return undefined;
  if (field.kind === "int") {
    // bool is an int in JS too; true must not read as 1 ms.
    if (typeof raw === "boolean") return undefined;
    const text = String(raw == null ? "" : raw).trim();
    if (!/^-?\d+$/.test(text)) return undefined;
    const value = Number(text);
    if (!isFinite(value) || Math.floor(value) !== value) return undefined;
    if (field.min != null && value < field.min) return undefined;
    if (field.max != null && value > field.max) return undefined;
    return value;
  }
  if (typeof raw !== "string") return undefined;
  if (field.kind === "choice") {
    const choices = Array.isArray(field.choices) ? field.choices : [];
    return choices.indexOf(raw) !== -1 ? raw : undefined;
  }
  const text = raw.trim();
  const cap =
    typeof maxStrChars === "number" && maxStrChars > 0
      ? maxStrChars
      : VOICE_PROFILE_MAX_STR;
  return text.length > cap ? undefined : text;
}

// Tolerant read of audio.tts_profiles, mirroring parse_profiles: bad JSON is
// {}, a profile that is not an object is skipped, and a field that is unknown,
// wrong-typed, out of range or equal to its default does not survive — the
// write path stores overrides only, so the editor should show "default".
function _voiceProfilesFrom(raw, fields, maxStrChars) {
  const byKey = {};
  const list = Array.isArray(fields) ? fields : [];
  for (let i = 0; i < list.length; i++) {
    if (list[i] && typeof list[i].key === "string") byKey[list[i].key] = list[i];
  }
  let data;
  try {
    data = JSON.parse(raw || "{}");
  } catch (e) {
    return {};
  }
  if (!data || typeof data !== "object" || Array.isArray(data)) return {};
  const out = {};
  const aliases = Object.keys(data);
  for (let a = 0; a < aliases.length; a++) {
    const name = aliases[a].trim();
    const item = data[aliases[a]];
    if (!name || !item || typeof item !== "object" || Array.isArray(item)) {
      continue;
    }
    const profile = {};
    const keys = Object.keys(item);
    for (let k = 0; k < keys.length; k++) {
      const field = byKey[keys[k]];
      if (!field) continue;
      const value = _voiceProfileScalar(field, item[keys[k]], maxStrChars);
      if (value === undefined || value === field.default) continue;
      profile[field.key] = value;
    }
    if (Object.keys(profile).length) out[name] = profile;
  }
  return out;
}

function _voiceProfileValue(profile, field) {
  if (profile && Object.prototype.hasOwnProperty.call(profile, field.key)) {
    return profile[field.key];
  }
  return field.default;
}

function _voiceProfileBadge(profile, field) {
  return profile && Object.prototype.hasOwnProperty.call(profile, field.key)
    ? "custom"
    : "default";
}

// A shallow copy of the alias map, enough for the one-level profile objects;
// every helper returns a new map so a failed save leaves the editor as it was.
function _voiceProfileClone(profiles) {
  const next = {};
  const current = profiles || {};
  const names = Object.keys(current);
  for (let i = 0; i < names.length; i++) next[names[i]] = current[names[i]];
  return next;
}

// The write shape: setting a field to its default removes the key, and an
// alias with no keys left is removed.
function _voiceProfileSet(profiles, alias, field, value) {
  const next = _voiceProfileClone(profiles);
  const profile = _voiceProfileClone((profiles || {})[alias]);
  if (value === field.default) delete profile[field.key];
  else profile[field.key] = value;
  if (Object.keys(profile).length) next[alias] = profile;
  else delete next[alias];
  return next;
}

// The target holds the source's overrides, not a merge — the same copy an
// operator gets from a "copy from" control.  Copying a profile with no
// overrides clears the target, which is the truthful result.
function _voiceProfileCopy(profiles, from, to) {
  const next = _voiceProfileClone(profiles);
  const source = (profiles || {})[from];
  if (source && Object.keys(source).length) next[to] = _voiceProfileClone(source);
  else delete next[to];
  return next;
}

function _voiceProfileReset(profiles, alias) {
  const next = _voiceProfileClone(profiles);
  delete next[alias];
  return next;
}

function _voiceProfileHasOverrides(profiles, alias) {
  const profile = profiles && profiles[alias];
  return !!profile && Object.keys(profile).length > 0;
}

function _voiceProfileResolveAlias(models, current) {
  for (let i = 0; i < models.length; i++) {
    if (models[i].alias === current) return current;
  }
  return models.length ? models[0].alias : "";
}

function _voiceProfileFor(alias) {
  return (_voiceProfiles && _voiceProfiles[alias]) || {};
}

function _voiceProfileField(meta, key) {
  const fields = meta && Array.isArray(meta.profile_fields) ? meta.profile_fields : [];
  for (let i = 0; i < fields.length; i++) {
    if (fields[i] && fields[i].key === key) return fields[i];
  }
  return null;
}

// The marker length cap lives on the meta endpoint's limits; the constant is
// the contract's MAX_MARKER_CHARS, used before meta arrives.
function _voiceProfileMaxStr() {
  const limits = _voiceMeta && _voiceMeta.limits;
  const cap = limits && limits.max_marker_chars;
  return typeof cap === "number" && cap > 0 ? cap : VOICE_PROFILE_MAX_STR;
}

// One profile row: the shared .settings-row markup (label column with the
// "?" popover and its learn-more link, input column with the unit, actions
// column with the default/custom badge and a per-field reset).  The input
// carries data-profile-field rather than data-setting-key, so the Settings
// tab's save/reset wiring leaves it to _wireVoiceProfile.
function _voiceProfileRowHtml(field, profile, maxStrChars) {
  const custom = _voiceProfileBadge(profile, field) === "custom";
  const value = _voiceProfileValue(profile, field);
  const key = escapeHtml(field.key);
  const label = escapeHtml(field.label || field.key);
  const helpId = "voice-profile-" + key + "-help";

  let html = '<div class="settings-row" data-profile-row="' + key + '">';
  html += '<div class="settings-label-col">';
  html += '<div class="settings-label">' + label;
  html +=
    ' <button type="button" class="settings-help-btn" data-help-target="' +
    helpId +
    '" aria-label="Help for ' +
    label +
    '" aria-expanded="false" title="More info"></button>';
  html += "</div>";
  html +=
    '<div id="' + helpId + '" class="settings-help-popover" style="display:none">';
  if (field.help) {
    html += '<span class="settings-help-text">' + escapeHtml(field.help) + "</span>";
  }
  if (field.reference_url) {
    html +=
      ' <a href="' +
      escapeHtml(field.reference_url) +
      '" target="_blank" rel="noopener" class="settings-help-ref">learn more</a>';
  }
  html += "</div></div>";

  html += '<div class="settings-input">';
  if (field.kind === "choice") {
    html +=
      '<select data-profile-field="' +
      key +
      '" aria-label="' +
      label +
      '">';
    const choices = Array.isArray(field.choices) ? field.choices : [];
    for (let c = 0; c < choices.length; c++) {
      const selected = choices[c] === String(value) ? " selected" : "";
      html +=
        '<option value="' +
        escapeHtml(choices[c]) +
        '"' +
        selected +
        ">" +
        escapeHtml(choices[c]) +
        "</option>";
    }
    html += "</select>";
  } else if (field.kind === "int") {
    const minAttr = field.min != null ? ' min="' + field.min + '"' : "";
    const maxAttr = field.max != null ? ' max="' + field.max + '"' : "";
    html +=
      '<input type="number" data-profile-field="' +
      key +
      '" aria-label="' +
      label +
      '" value="' +
      escapeHtml(String(value)) +
      '" step="1"' +
      minAttr +
      maxAttr +
      ">";
  } else {
    const cap =
      typeof maxStrChars === "number" && maxStrChars > 0
        ? maxStrChars
        : VOICE_PROFILE_MAX_STR;
    html +=
      '<input type="text" data-profile-field="' +
      key +
      '" aria-label="' +
      label +
      '" value="' +
      escapeHtml(String(value)) +
      '" maxlength="' +
      cap +
      '">';
  }
  if (field.unit) {
    html += '<span class="voice-profile-unit">' + escapeHtml(field.unit) + "</span>";
  }
  html += "</div>";

  html += '<div class="settings-actions">';
  if (custom) {
    html += '<span class="scope-badge scope-write">custom</span>';
    html +=
      '<button class="settings-reset-btn" data-profile-reset="' +
      key +
      '">reset</button>';
  } else {
    html += '<span class="scope-badge settings-badge-default">default</span>';
  }
  html += "</div></div>";
  return html;
}

// The profile bar and its three sections, or the one line that explains why
// there is no bar yet.  "" only before meta has ever loaded.
function _voiceProfileHtml() {
  if (_voiceMetaError) {
    return (
      '<div class="voice-profile-block">' +
      '<div class="dashboard-empty">Voice tuning options could not be loaded: ' +
      escapeHtml(_voiceMetaError) +
      "</div></div>"
    );
  }
  const meta = _voiceMeta;
  if (!meta) return "";
  const models = _voiceProfileModels(meta);
  if (!models.length) {
    return (
      '<div class="voice-profile-block">' +
      '<div class="dashboard-empty">No model with speech synthesis is ' +
      "configured. Add one in Models \u2192 Roles, then return here to tune " +
      "its pacing.</div></div>"
    );
  }
  const selected = _voiceProfileResolveAlias(models, _voiceProfileAlias);
  if (selected) _voiceProfileAlias = selected;
  const fields = Array.isArray(meta.profile_fields) ? meta.profile_fields : [];
  const groups = _voiceProfileGroups(fields);
  const profile = _voiceProfileFor(selected);

  let html = '<div class="voice-profile-block" data-profile-block>';
  html += '<div class="voice-profile-bar">';
  html +=
    '<label class="voice-profile-model-label" for="voice-profile-model">' +
    "Pacing profile for</label>";
  html +=
    '<span class="settings-input voice-profile-model-input">' +
    '<select id="voice-profile-model" data-profile-model ' +
    'aria-label="TTS model to tune">';
  for (let i = 0; i < models.length; i++) {
    html +=
      '<option value="' +
      escapeHtml(models[i].alias) +
      '"' +
      (models[i].alias === selected ? " selected" : "") +
      ">" +
      escapeHtml(models[i].alias) +
      (models[i].active ? " (active TTS model)" : "") +
      "</option>";
  }
  html += "</select></span>";
  if (models.length > 1) {
    html +=
      '<span class="settings-input voice-profile-copy-input">' +
      '<select data-profile-copy aria-label="Copy settings from another ' +
      'TTS model"><option value="">Copy from\u2026</option>';
    for (let i = 0; i < models.length; i++) {
      if (models[i].alias === selected) continue;
      html +=
        '<option value="' +
        escapeHtml(models[i].alias) +
        '">' +
        escapeHtml(models[i].alias) +
        "</option>";
    }
    html += "</select></span>";
  }
  html +=
    '<button type="button" class="settings-reset-btn" data-profile-clear' +
    (_voiceProfileHasOverrides(_voiceProfiles, selected) ? "" : " disabled") +
    ">Reset profile</button>";
  html += "</div>";
  html +=
    '<div class="settings-desc voice-profile-note">Each TTS model keeps its ' +
    "own pacing, structure and clip settings. Switching the TTS model role " +
    "switches the profile.</div>";
  html +=
    '<div class="voice-profile-error" role="alert" data-profile-error></div>';

  for (let g = 0; g < groups.length; g++) {
    const group = groups[g];
    const groupId = escapeHtml(group.id);
    html +=
      '<div class="settings-section" data-section="voice-profile-' + groupId + '">';
    html +=
      '<div class="settings-section-header" role="button" tabindex="0" ' +
      'aria-expanded="true" aria-controls="voice-profile-body-' +
      groupId +
      '"><span>' +
      escapeHtml(group.label) +
      "</span></div>";
    html +=
      '<div class="settings-section-body" id="voice-profile-body-' + groupId + '">';
    for (let f = 0; f < group.fields.length; f++) {
      html += _voiceProfileRowHtml(group.fields[f], profile, _voiceProfileMaxStr());
    }
    html += "</div></div>";
  }
  html += "</div>";
  return html;
}

// ---------------------------------------------------------------------------
// Settings section
// ---------------------------------------------------------------------------

function renderVoiceSettings() {
  const c = document.getElementById("voice-settings-container");
  if (!c) return;
  const groups = _voiceSettingGroups(_voiceSettings);
  const profileHtml = _voiceProfileHtml();
  if (!groups.length && !profileHtml) {
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
  html += profileHtml;
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

  _wireVoiceProfile(container);
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

// ---------------------------------------------------------------------------
// Pacing profile editor
// ---------------------------------------------------------------------------

function _wireVoiceProfile(container) {
  const modelSelect = container.querySelector("[data-profile-model]");
  if (modelSelect) {
    modelSelect.addEventListener("change", function () {
      _voiceProfileAlias = this.value;
      renderVoiceSettings();
    });
  }

  const copySelect = container.querySelector("[data-profile-copy]");
  if (copySelect) {
    copySelect.addEventListener("change", function () {
      const source = this.value;
      this.value = "";
      if (source) _copyVoiceProfile(source);
    });
  }

  const clearBtn = container.querySelector("[data-profile-clear]");
  if (clearBtn) clearBtn.addEventListener("click", _resetVoiceProfile);

  const inputs = container.querySelectorAll("[data-profile-field]");
  for (let i = 0; i < inputs.length; i++) {
    inputs[i].addEventListener("change", function () {
      _onVoiceProfileField(this);
    });
  }

  const resetBtns = container.querySelectorAll("[data-profile-reset]");
  for (let i = 0; i < resetBtns.length; i++) {
    resetBtns[i].addEventListener("click", function () {
      _resetVoiceProfileField(this.getAttribute("data-profile-reset"));
    });
  }
}

// Number and text commit on change (blur/Enter), select on its pick: there is
// no half-typed value to protect, and one PUT carries the whole profile.
function _onVoiceProfileField(input) {
  const key = input.getAttribute("data-profile-field");
  const field = _voiceProfileField(_voiceMeta, key);
  if (!field) return;
  const value = _voiceProfileScalar(field, input.value, _voiceProfileMaxStr());
  if (value === undefined) {
    _showVoiceProfileError(_voiceProfileInvalidMessage(field));
    input.value = String(
      _voiceProfileValue(_voiceProfileFor(_voiceProfileAlias), field),
    );
    return;
  }
  _voiceProfiles = _voiceProfileSet(
    _voiceProfiles,
    _voiceProfileAlias,
    field,
    value,
  );
  renderVoiceSettings();
  _saveVoiceProfiles();
}

function _voiceProfileInvalidMessage(field) {
  if (field.kind === "int") {
    if (field.min != null && field.max != null) {
      return (
        field.label +
        " must be a whole number between " +
        field.min +
        " and " +
        field.max
      );
    }
    return field.label + " must be a whole number";
  }
  if (field.kind === "choice") {
    const choices = Array.isArray(field.choices) ? field.choices : [];
    return "Choose one of: " + choices.join(", ");
  }
  return field.label + " is longer than " + _voiceProfileMaxStr() + " characters";
}

// Inline, beside the editor, rather than a toast: the server's rejection is
// about the profile the operator is looking at.
function _showVoiceProfileError(message) {
  const el = document.querySelector("[data-profile-error]");
  if (!el) return;
  el.textContent = message || "";
}

// The whole override map is one setting, so every edit PUTs all of it.  The
// re-read happens once the last in-flight write lands: reloading per write
// could land an older GET on top of a newer edit.
function _saveVoiceProfiles() {
  const payload = JSON.stringify(_voiceProfiles);
  _voiceProfilePending++;
  authFetch("/v1/api/admin/settings/" + encodeURIComponent(VOICE_PROFILE_KEY), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value: payload }),
  })
    .then(function (r) {
      if (!r.ok)
        return r.json().then(function (d) {
          throw new Error((d && d.error) || "Save failed");
        });
      return r.json();
    })
    .then(function () {
      _voiceProfilePending--;
      if (_voiceProfilePending === 0) loadVoiceSettings();
    })
    .catch(function (e) {
      _voiceProfilePending--;
      _showVoiceProfileError(e.message || "Save failed");
    });
}

function _copyVoiceProfile(from) {
  const to = _voiceProfileAlias;
  if (!from || !to || from === to) return;
  const apply = function () {
    _voiceProfiles = _voiceProfileCopy(_voiceProfiles, from, to);
    renderVoiceSettings();
    _saveVoiceProfiles();
  };
  if (_voiceProfileHasOverrides(_voiceProfiles, to)) {
    showConfirmModal(
      "Copy pacing profile",
      "Replace \u2018" +
        to +
        "\u2019 with the settings stored for \u2018" +
        from +
        "\u2019?",
      "Copy",
      apply,
    );
    return;
  }
  apply();
}

function _resetVoiceProfile() {
  const alias = _voiceProfileAlias;
  if (!alias || !_voiceProfileHasOverrides(_voiceProfiles, alias)) return;
  showConfirmModal(
    "Reset profile",
    "Reset \u2018" +
      alias +
      "\u2019 to the default pacing, structure and clip settings? Its stored " +
      "overrides will be removed.",
    "Reset",
    function () {
      _voiceProfiles = _voiceProfileReset(_voiceProfiles, alias);
      renderVoiceSettings();
      _saveVoiceProfiles();
    },
  );
}

function _resetVoiceProfileField(key) {
  const field = _voiceProfileField(_voiceMeta, key);
  if (!field) return;
  _voiceProfiles = _voiceProfileSet(
    _voiceProfiles,
    _voiceProfileAlias,
    field,
    field.default,
  );
  renderVoiceSettings();
  _saveVoiceProfiles();
}
