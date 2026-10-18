# Voice

Admin › Voice configures speech in both directions: **dictation**
(speech-to-text) turns microphone audio into text, and **read-aloud**
(text-to-speech) synthesizes assistant replies. The page also owns the text
cleanup that runs before synthesis, the per-model pacing that shapes how a
reply is read, the voice dictionary, and the speaking styles offered beside
the speaker button.

The page requires the `admin.settings` permission, the same as the Settings
tab. Every control writes an `audio.*` setting, so a saved change applies to
the next request without a restart. Pacing, playback speed and the dictation
auto-stop settings are the exception: the browser reads them with the rest of
its voice configuration, so they reach recordings and replies started after a
page reload.

## Overview

### The Voice tabs

| Tab | What it edits |
|-----|---------------|
| **Settings** | Dictation and read-aloud settings, and the active TTS alias's pacing, structure and clip profile. |
| **Text cleanup** | The built-in rule groups, custom rewrite rules, and a live preview of the cleaned text. |
| **Dictionary** | Terms read-aloud should say differently and phrases dictation should spell your way, plus the built-in terms. |
| **Styles** | Named delivery instructions (for example "Professional" or "Social") offered beside the speaker button. |

The two model aliases are not on this page: they are **Models → Roles**
fields. Everything else is stored in `audio.*` settings and documented below.

### Read-aloud flow

1. The reply is rendered in the conversation.
2. The player turns the displayed reply into **clips**, using the active TTS
   model's profile: headings, paragraphs, blockquotes and table rows become
   their own clips, a list is one clip by default, fenced code becomes a
   marker, diagrams become a marker, and images, SVG, video and audio are
   skipped. Short inline code is spoken; longer inline code becomes the same
   marker.
3. For each clip, the server cleans the text (rule groups → custom rewrite
   rules → dictionary, see [Order of operations](#order-of-operations)) and
   sends it to the TTS model with the chosen speaking style, if the model
   accepts one.
4. The browser plays the clips in order, pausing between them as the profile
   says, at the configured [playback speed](#playback-speed) with pitch kept.
   The next clip is requested while the current one plays.

### Dictation flow

1. The microphone records a short clip and posts it to the Speech-to-text
   role.
2. The request carries `audio.stt_prompt`, plus the dictionary's terms when
   `audio.stt_dictionary_hint` is on. Whisper-style models use that prompt as
   vocabulary context; models that ignore a prompt are unaffected.
3. The transcript is inserted into the composer. A complete transcript also
   gets the dictionary's "also hears" correction when
   `audio.stt_dictionary_correct` is on; the composer's live streamed
   dictation inserts deltas as they arrive and is not rewritten.
4. Dictation never passes through the read-aloud cleanup.

### Order of operations

Read-aloud cleanup is server-side and model-agnostic. For each clip, in this
order:

1. **Built-in rule groups** ([Rule groups](#rule-groups)) rewrite symbols,
   units, dates, versions, file names and abbreviations. A group the operator
   switched off is skipped.
2. **Custom rewrite rules** ([Custom rewrite rules](#custom-rewrite-rules))
   run top to bottom, each on the output of the previous one.
3. **The pronunciation pass** replaces whole words in one left-to-right pass.
   Later sources win: built-in terms, then the legacy
   `audio.tts_pronunciations` lines, then dictionary entries. The longest term
   is tried first, and a replacement is never rescanned.
4. **The browser** then plans clips and pauses from the active model's
   profile ([Pacing profiles](#pacing-profiles)). Pacing never changes the
   text sent to the model.

Steps 1–3 happen only while `audio.tts_normalize` is on
([the master switch](#text-cleanup-master-switch)). Pacing happens on every
reply.

Two consequences are worth knowing. Rules see **displayed text**, not
Markdown: code and diagrams are already markers, table cells are already
joined, and link URLs and image sources are not present. And rules see **one
clip at a time**, so a `find` that spans two clips never matches.

## Settings

The Settings tab groups the settings by direction. Each row's **?** opens its
help text and a **learn more** link to the matching section of this page.

### Dictation

#### Speech-to-text model role

`audio.stt_model_alias` · default empty (dictation disabled) · set in
**Models → Roles**

The registered model alias that transcribes microphone audio. It is also the
preferred transcript for audio attachments. Point it at a transcription model
(Whisper-style) or an audio-capable omni model; the role picker only offers
audio-capable aliases — one that declares `supports_transcription`, an omni
model with `supports_audio_input`, or a model name it recognises as a
transcription model. Empty disables the microphone; audio attachments then
fall back to the Perception role if one is configured.

Change it when you are enabling dictation or moving transcription to a
different backend. The role is model-agnostic: no voice setting names a
specific backend.

#### Transcription prompt

`audio.stt_prompt` · default empty · free text

Optional text sent as the OpenAI transcription `prompt` parameter. Use it for
domain vocabulary, product names and acronyms that the backend mishears. Some
instruction-style ASR models also expect an instruction here; others ignore
the field.

Set it when a handful of terms are consistently wrong. For a growing word
list, prefer the Dictionary tab: its hint is appended after this prompt and is
capped so it cannot crowd the model's short prompt window. Leave the field
empty for a backend that ignores prompts.

#### Dictionary hint for dictation

`audio.stt_dictionary_hint` · default on · boolean

Appends the dictionary's terms to the transcription prompt as a short
`Vocabulary: …` list. Terms are deduplicated case-insensitively and added only
while the whole hint stays within 400 characters, so a long dictionary does
not push out the operator's prompt.

Keep it on for Whisper-style backends. Turn it off for a backend that ignores
or rejects prompts, or when the vocabulary list is crowding out
`audio.stt_prompt`. Turning it off does not disable
[transcript correction](#transcript-correction).

#### Transcript correction

`audio.stt_dictionary_correct` · default on · boolean

After a complete transcript is returned, replaces every dictionary `also_hears`
phrase with that entry's term. Matching is whole-phrase and case-insensitive,
any run of whitespace inside the phrase matches, the longest phrase is tried
first, and a replacement is not rescanned. It runs on the transcript, so it
works with every STT backend.

Turn it off if a phrase listed under "also hears" is also ordinary speech you
want left alone. Live streamed dictation inserts deltas as they arrive and is
not rewritten by this setting.

### Read aloud

#### Text-to-speech model role

`audio.tts_model_alias` · default empty (read-aloud disabled) · set in
**Models → Roles**

The registered model alias that synthesizes assistant speech. Point it at a
speech-synthesis backend (for example an OpenAI `gpt-4o-mini-tts` alias or a
local vLLM-Omni or Kokoro endpoint). Empty disables the speaker affordance.

Change it when you are enabling read-aloud or moving synthesis to a different
backend. The alias also selects the pacing profile: switching roles switches
profiles automatically.

#### Voice identifier

`audio.tts_voice` · default `alloy` · free text

The voice name passed to the TTS backend. OpenAI voices include `alloy`,
`echo`, `fable`, `onyx`, `nova` and `shimmer`; local backends define their own
names. An empty value falls back to `alloy`.

Change it when the configured voice is wrong, unavailable, or another voice
suits the team better. The right value comes from the model's own
documentation; Turnstone cannot validate it because voice names are
backend-specific.

#### Longest speech request

`audio.tts_max_chars` · default `8000` · range 1–50000 characters

The cap on one speech request. With the default profile, one request is one
clip, so this bounds recitation of a very long paragraph rather than a whole
reply. A request over the cap is refused with an error instead of being read
aloud.

Raise it together with `audio.tts_timeout_seconds`, because the whole clip is
synthesized before playback starts. Lower it if a slow backend cannot finish
long clips in time. The default is generous for normal replies; most operators
never change it.

#### Synthesis timeout

`audio.tts_timeout_seconds` · default `30` · range 1–600 seconds

How long the console waits for a node to return one complete audio clip. Local
speech backends can take far longer than an ordinary chat request; every other
request the console forwards keeps the proxy's shared timeout.

Raise it when long clips time out on a slow or CPU-only backend, and pair the
change with `audio.tts_max_chars`. Lower it to fail fast when a wedged backend
should not hold the player. The console applies this value; a node serving its
own UI uses its own configured timeout.

#### Text cleanup master switch

`audio.tts_normalize` · default on · boolean

When on, every read-aloud request passes through the cleanup pipeline:
built-in rule groups, custom rewrite rules, built-in terms, legacy
pronunciations and the voice dictionary. When off, the clip text is sent to
the model unchanged.

Turn it off for non-English replies or a backend that already normalizes
numbers and symbols itself. It is the single switch for
[Text cleanup](#text-cleanup); the tab still previews with it off so an
operator can see the effect before enabling it.

#### Default speaking style

`audio.tts_style` · default empty (no instruction) · a style name from
`audio.tts_styles`

The style used when a read-aloud request names none. An empty name, an unknown
name, a style with no instruction, or a model without the
[Speaking styles capability](#the-speaking-styles-capability) all mean no
instruction is sent.

Set it when most replies should use one delivery and the model accepts
instructions. Leave it empty to make the per-reply picker the only source of
style, or when the model ignores instructions. Matching is case-insensitive.

#### Legacy pronunciations

`audio.tts_pronunciations` · default empty · one `word = respelling` per line

The original line-based pronunciation editor, still supported. Matching is
whole-word and case-insensitive; the respelling is plain words, which works on
every model. Blank lines and lines beginning with `#` are ignored, at most 200
entries are used, and keys and respellings are capped at 100 characters. A
later duplicate line wins.

The [Dictionary](#dictionary) supersedes this field and applies after it, so a
dictionary entry for the same term wins. Use the pronunciations field only for
an old configuration you have not migrated; the Dictionary tab offers a
one-click import.

### Settings with their own sections

These read-aloud settings are edited on the other tabs or documented in their
own section:

| Setting | Default | What it does | When to change it | Details |
|---------|---------|--------------|-------------------|---------|
| `audio.tts_playback_rate` | `1.0` | Plays every clip faster or slower in the browser, pitch kept. | Even out a model that speaks noticeably slowly or quickly. | [Playback speed](#playback-speed), 0.5–2.0 |
| `audio.tts_profiles` | `{}` | Per-alias clip boundaries, pauses, and table/code handling. | A new TTS model sounds wrong at headings, tables or clip edges. | [Pacing profiles](#pacing-profiles), max 20 |
| `audio.tts_rewrite_rules` | `[]` | Find/replace rules run after the groups. | A term or format the groups do not cover is read badly. | [Custom rewrite rules](#custom-rewrite-rules), max 100 |
| `audio.tts_cleanup_disabled` | `[]` | Built-in groups switched off. | One group misfires on your text. | [Switching a group off](#switching-a-group-off) |
| `audio.tts_builtin_terms_off` | `[]` | Built-in respellings switched off. | A shipped respelling is wrong for your team. | [Built-in terms](#built-in-terms) |
| `audio.voice_dictionary` | `[]` | Read-aloud respellings and dictation corrections. | A term is mispronounced or mistranscribed. | [Dictionary](#dictionary), max 200 |
| `audio.tts_styles` | Professional, Social, Plain | Named delivery instructions. | The model accepts instructions and you want a different delivery. | [Styles](#styles), max 20 |

## Pacing profiles

A pacing profile decides how a reply is cut into clips and how long the player
pauses between them. It applies in the browser, so it changes what the
listener hears without changing what the model synthesizes.

Profiles are **per TTS model alias**. The player asks for the profile of the
alias the TTS role resolves to, and switching the role switches profiles.
Models differ most at exactly these points: one barely pauses at a paragraph,
another adds its own gap, one reads a table as a wall of numbers, another needs
short requests. A profile is the place to absorb those differences.

Settings → Pacing, Structure and Clips edit the active alias's profile.

### How profiles are stored

`audio.tts_profiles` is a JSON object keyed by TTS model alias:

```json
{
  "tts-qwen": {"pause_paragraph_ms": 250, "clip_max_chars": 400},
  "tts-piper": {"pause_heading_ms": 400, "code_marker": ""}
}
```

Only the fields a profile sets are stored; a field it never set reads its
default, so a later change to a default reaches every profile that left it
alone. An alias with no profile behaves exactly like a profile with no fields:
the defaults below. At most 20 profiles are kept; an alias is at most 100
characters. `clip_min_chars` must be smaller than `clip_max_chars`.

### Pacing

Silence is added after the clip that ends with the construct, before the next
one starts.

| Field | Default | Range | What it does |
|-------|---------|-------|--------------|
| `pause_heading_ms` | `600` | 0–5000 ms | Silence after an H1–H6 heading. Raise it when sections run together; lower it when the model already pauses after short lines. 400–800 suits most voices. |
| `pause_paragraph_ms` | `500` | 0–5000 ms | Silence after a paragraph or blockquote. Models differ most here. Lower it for a model that adds its own gap. |
| `pause_table_row_ms` | `150` | 0–5000 ms | Silence between table rows. Short on purpose, so a table sounds like a list rather than a speech. 0–300 is the usual range. |
| `pause_code_ms` | `400` | 0–5000 ms | Silence after a code or diagram marker, so the marker does not run into the next sentence. |
| `pause_list_item_ms` | `0` | 0–5000 ms | `0` reads a whole list as one clip with full stops between items, which sounds natural on most models. Above 0, each item is its own clip followed by this pause; use it when items blur together. |

### Structure

Structure fields decide what text reaches the model for tables, code and
diagrams.

| Field | Default | Choices / range | What it does |
|-------|---------|-----------------|--------------|
| `table_cell_joiner` | `comma` | `comma`, `semicolon`, `pause` | What goes between the cells of a row. `comma` sounds most natural; `semicolon` gives a longer breath; `pause` makes each cell its own clip with the table-row pause between them (slowest). |
| `table_header_mode` | `row` | `row`, `prefix`, `skip` | How the header row is read. `row` reads it like any other row; `prefix` says each cell's header before it ("Name: Ryan, Speed: 1.4"), which makes wide tables understandable; `skip` leaves it out when it only repeats the obvious. |
| `inline_code_max_chars` | `40` | 0–500 chars | Inline code this long or longer is replaced by the code marker; shorter code is spoken. Lower it when commands read aloud become noise. |
| `code_marker` | `Code omitted.` | up to 60 chars | Said in place of a fenced code block or long inline code. Empty skips code silently. |
| `diagram_marker` | `Diagram omitted.` | up to 60 chars | Said in place of a rendered diagram (mermaid or SVG). Empty skips diagrams silently. |

### Clips

Clip fields decide where one speech request ends and the next begins.

| Field | Default | Range | What it does |
|-------|---------|-------|--------------|
| `clip_min_chars` | `20` | 0–200 chars | Lines shorter than this fold into the next clip instead of becoming a request of their own. Some models clip or mispronounce very short inputs; raise it if one-word lines sound cut off. |
| `clip_max_chars` | `600` | 100–4000 chars | Longest text per request; longer text splits at a comma or space. Smaller clips start playing sooner; larger ones keep intonation across sentences. 300–800 suits most models. |
| `max_clips` | `200` | 1–1000 clips | Clips read per reply before reading stops with a notice in the player. Stops a huge reply (a 500-row table) from queuing hundreds of requests. |

### Tuning pacing for a new TTS model

Work through this once per new TTS alias; it takes a few minutes and saves
guessing later.

1. **Point the role at the model.** In Models → Roles, set
   `audio.tts_model_alias` to the alias. Read-aloud now uses that model and its
   profile.
2. **Read a calibration reply.** Paste this into a chat, then press the
   speaker button on the reply. It deliberately contains every construct the
   profile controls:

   ````markdown
   ## Cache warm-up

   The warm-up script primes the cache before the first request. A cold start
   usually takes 10-20 seconds.

   | Step | Command | Expected |
   |------|---------|----------|
   | Prime | `warmup --all` | 200 OK |
   | Verify | `warmup --check` | cache hit |

   1. Run the primer.
   2. Watch the log.
   3. Read the first reply.

   ```bash
   warmup --all --verbose
   ```

   Done.
   ````

3. **Listen for the symptom, change one field.** Use the table below. Change
   one field at a time and replay the same reply so the difference is the field
   and not the text.
4. **Save, then reload.** The player reads the profile with the rest of its
   read-aloud configuration. If a change does not reach the next reply, reload
   the page and play again.
5. **Keep the profile small.** Store only what differs from the defaults. The
   next reader of the setting can then tell what was tuned and why.

| What you hear | Field to change | Direction |
|---------------|-----------------|-----------|
| Sections run together; no breath after a heading | `pause_heading_ms` | Raise (400–800 first) |
| Long silences after short headings | `pause_heading_ms` | Lower |
| Paragraphs run together | `pause_paragraph_ms` | Raise |
| Gaps feel doubled; the model pauses on its own | `pause_paragraph_ms` | Lower |
| A table sounds like a speech | `table_cell_joiner` | `comma`, then `semicolon` |
| Table cells blur together | `table_cell_joiner` | `pause` (slowest) |
| A wide table is impossible to follow | `table_header_mode` | `prefix` |
| The header row repeats obvious labels | `table_header_mode` | `skip` |
| Code reads as punctuation soup | `inline_code_max_chars` | Lower; set `code_marker` |
| "Code omitted" runs into the next sentence | `pause_code_ms` | Raise |
| A diagram marker runs into the next sentence | `pause_code_ms` | Raise |
| List items blur together | `pause_list_item_ms` | 200–400 |
| A list sounds choppy | `pause_list_item_ms` | `0` |
| One-word lines sound cut off or mispronounced | `clip_min_chars` | Raise |
| The first audio takes too long to start | `clip_max_chars` | Lower |
| Intonation breaks between sentences | `clip_max_chars` | Raise |
| A huge reply stops with a notice | `max_clips` | Raise (each clip is one request) |

If a model mangles a particular word rather than the pacing, that is a
Dictionary problem, not a profile problem.

## Text cleanup

Text cleanup runs on the server for every clip, before synthesis, and works
the same for every TTS model. The Text cleanup tab edits it in two layers:

- **Rule groups** — the built-in, model-agnostic rewrites for symbols, units,
  dates, versions and the like.
- **Custom rewrite rules** — your own find-and-replace rules, run after the
  groups.

Both are gated by the [text cleanup master switch](#text-cleanup-master-switch)
(`audio.tts_normalize`). The preview always runs, even when the master switch
is off, so an operator can see the effect before turning it on.

### Rule groups

Every group is on while the master switch is on. The table is display order;
the engine's internal order is fixed so that, for example, phone numbers and
dates are recognized before ranges and units.

| Group id | What it rewrites | Example input |
|----------|------------------|---------------|
| `phone_numbers` | Ten-digit numbers, read digit by digit | `Call 303-555-0142` |
| `dates` | ISO dates and year-months | `Shipped 2026-10-16` |
| `file_names` | The dot before a known file extension | `Edit config.yaml` |
| `versions` | Dotted versions with three or more parts | `Upgrade to v1.8.5` |
| `symbols` | `~`, `→`, `≈`, `±`, `%` and `×` between numbers | `~5 users, a -> b, 50%` |
| `dimensions` | Sizes written NxM | `A 1024x768 screen` |
| `units` | Size, time, rate and frequency units after a number | `120ms, 32GB, 300W` |
| `seconds_decades` | "90s" as seconds unless it reads as a decade | `the 90s vs a 90s timeout` |
| `ranges` | Number ranges | `Takes 10-20 minutes` |
| `abbreviations` | `e.g.`, `i.e.`, `etc.`, `vs.` | `e.g. this, i.e. that` |
| `identifiers` | `snake_case` names read as words, known acronyms spelled out | `Check user_id and aid_kit` |

Groups deliberately leave ambiguous text alone. A two-digit-year dotted date
still reads as a version, `16.10.2026` is not reordered, and an ambiguous token
such as `8k` is untouched. That is a design choice, not a gap in the group:
guessing wrong is worse than reading the raw text.

### Switching a group off

Each group has a switch in the Text cleanup tab. The page stores the switched
off ids in `audio.tts_cleanup_disabled`, a JSON list:

```json
["versions", "seconds_decades"]
```

A switched-off group is skipped; every other group keeps running. Unknown ids
in the stored list are ignored, so a value left over from an older release
cannot break cleanup. Turning the master switch off skips every group at once.
Use a group switch when one rewrite misfires on your text (for example a model
that already reads version numbers well), not as a general off switch.

### Custom rewrite rules

A rule has five fields:

| Field | Meaning |
|-------|---------|
| `find` | Required. The text or pattern to match, at most 500 characters. |
| `regex` | `false` (default) for plain text, `true` for a Python regular expression. |
| `replace` | The replacement, at most 500 characters. An empty replacement **deletes** the match. |
| `test` | The rule editor's test line, kept with the rule as its example, at most 500 characters. |
| `enabled` | A disabled rule stays in the list but is skipped. |

Rules run **top to bottom**, after the built-in groups and before the
dictionary. At most 100 rules are stored. A rule that does not compile is
rejected when you save, with the position of the error, rather than being
stored and failing later.

#### Plain text or regular expression

A **plain-text** rule is what most operators want. It matches the find text
literally, whole-word and ignoring case, and any run of whitespace inside the
find text matches any run of whitespace:

| Find | Matches | Does not match |
|------|---------|----------------|
| `k8s` | `k8s`, `K8s`, `K8S` | `k8sctl`, `my-k8s` |
| `pull request` | `pull request`, `pull  request` | `pull requests` |

The replacement is literal too: `\1`, `$1` and regex syntax have no special
meaning in a plain-text rule. Symbols are literal, so `w/o` is a valid find.

A **regex** rule uses Python's `re` syntax. It is case-sensitive unless the
pattern starts with `(?i)`. Capture groups work in the replacement: `\1` (or
`\g<1>`) inserts the first group and `\g<name>` inserts a named group. A
pattern that can match empty text is rejected, because it would insert the
replacement between every character. A malformed pattern, or a replacement
that references a group the pattern does not have, is rejected on save.

#### What the rules see

Rules see the **displayed text of one clip**, not the Markdown source and not
the whole reply:

- Code blocks and diagrams have already become their markers, and table cells
  have already been joined per the profile.
- Link labels are present; link URLs, image sources, raw HTML and rendered
  math's hidden accessibility text are not spoken and cannot be matched.
- A clip boundary falls between constructs, so a `find` cannot span two
  headings, a heading and the paragraph below it, or two table rows.
- The rule groups have already run. While `identifiers` is on, `user_id` has
  already become `user ID`, so a rule (or dictionary entry) written for the
  snake_case form never matches; match the spoken form, or switch the
  `identifiers` group off.

If a rule must see a construct the player removes, change the profile (for
example the code marker) or handle the text before it reaches the player;
cleanup runs too late for that.

#### Order and limits

- Rules run after every enabled group; disabling a group lets a rule see the
  text that group would have changed.
- Within the list, order is the rule order. Put a broad rule after the narrow
  rule it would otherwise pre-empt.
- A disabled rule keeps its position, so re-enabling it restores the original
  order.
- Limits: 100 rules, 500 characters for `find`, `replace` and `test`.
- Only enabled rules run. Rules never run on dictation.

#### Copy prompt for your AI

**Copy prompt for your AI** copies a ready-made prompt to the clipboard that
describes the rule format — plain text versus regex, the fields, the limits —
and asks an assistant to turn a description of what should be spoken into
rules. Paste the answer back into the editor, check each rule's test line and
the preview, then save. Treat generated rules as drafts: the preview is the
authority on what a rule actually does to your text.

### Worked examples

#### A ticket id

A ticket id such as `DT-1412` is read as "D T one thousand four hundred
twelve" or skipped entirely, depending on the voice. Match it with a regex
rule and spell the letters:

| Field | Value |
|-------|-------|
| `find` | `\bDT-(\d+)\b` |
| `regex` | on |
| `replace` | `D T \1` |
| `test` | `Merged DT-1412 into main.` |

`\b` anchors the id at word boundaries, `(\d+)` captures the number, and `\1`
puts it back. The preview should show `Merged D T 1412 into main.` Use the
same shape for other prefixes (`ABC-123` → `A B C \1`) or add a case flag
(`(?i)\bdt-(\d+)\b`) if ids appear in either case.

#### A plain abbreviation

A team that says "kubernetes" but writes `k8s` needs no regex at all:

| Field | Value |
|-------|-------|
| `find` | `k8s` |
| `regex` | off |
| `replace` | `kubernetes` |
| `test` | `Deploy to k8s, then check K8s again.` |

Plain rules are whole-word and case-insensitive, so both spellings in the test
line are replaced and `k8sctl` is not. If the same abbreviation appears with
different spacing (`k 8 s`), add a second plain rule; the find text's internal
whitespace is flexible, but it must be present.

#### Deleting a phrase

To remove boilerplate before it is spoken, leave the replacement empty:

| Field | Value |
|-------|-------|
| `find` | `As an AI language model,` |
| `regex` | off |
| `replace` | (empty) |
| `test` | `As an AI language model, I cannot browse.` |

The preview should show `I cannot browse.` An empty replacement is a deletion,
not an error. If the phrase leaves a double space or a stranded comma behind,
either include the punctuation in `find` or add a second rule that collapses
it; the preview shows the exact result.

### Previewing cleanup

The tab previews through the server, so the text shown is exactly what the TTS
model would receive for that clip. The preview accepts up to 5000 characters
and can be run with unsaved draft rules, which is how the rule editor's test
line works. It marks each changed stretch with its source (rule group, custom
rule, or dictionary) and reports a change count.

When the master switch is off, the preview still runs and says so; it does not
pretend the cleanup is active. The same preview is the quickest way to check a
group: paste a term the group should rewrite and compare the input and output.

## Dictionary

The Dictionary tab is one list used in both directions:

- **Read aloud** says an entry's `respelling` instead of its `term`.
- **Dictation** turns any of the entry's `also_hears` phrases back into
  `term`, and can hint the terms to the transcription model.

The list is `audio.voice_dictionary`, stored as JSON. At most 200 entries are
kept. An entry has:

| Field | Meaning |
|-------|---------|
| `term` | Required. The word or phrase to match in a reply (and the spelling dictation should produce). At most 100 characters. |
| `kind` | `respelling` (default) or `ipa`. |
| `respelling` | Plain words that sound right read aloud. Works on every model. At most 100 characters. May be empty for a dictation-only entry. |
| `ipa` | Required for `kind: ipa`, ignored otherwise. At most 100 characters. |
| `match_case` | Off: match whole-word, ignoring case. On: match only the exact case. |
| `also_hears` | Up to 10 transcript phrases that should become `term`. Each at most 100 characters. |

An entry with only `also_hears` is dictation-only: read-aloud skips it because
it has nothing to say.

### Respelling or IPA

A **respelling** is plain text chosen for how it sounds: `nginx` →
`engine ex`, `SQL` → `sequel`. It is model-agnostic and is what read-aloud
sends. This is the right choice for almost every entry.

**Phonetic (IPA)** stores a pronunciation in the International Phonetic
Alphabet for models that understand it. The current read-aloud path always
speaks the entry's `respelling`, even for an IPA entry — IPA is never sent to
the model — so give an IPA entry a respelling as well, or it will be skipped
by read-aloud. The IPA is kept with the entry for models and tooling that use
it. The dictation-only fields work the same for either kind.

### Match case

With **Match case** off (the default), a dictionary term matches any casing:
`nginx` also replaces `NGINX`. With it on, only the exact spelling matches.
That is how you distinguish a word from an acronym that looks the same:

| Term | Match case | Respelling | Result |
|------|------------|------------|--------|
| `us` | off | `U S` | Every "us" in a reply becomes "U S" — usually wrong. |
| `US` | on | `U S` | Only the uppercase country abbreviation is replaced. |

The two entries can coexist: they are different keys for duplicate detection.
Read-aloud tries the longest term first and applies the dictionary after the
built-in terms and the legacy pronunciations, so a dictionary entry wins over
both.

### Also hears

`also_hears` is the dictation side of an entry. List the phrases a transcript
might contain — the way people actually say it, or the way the model tends to
spell it — and dictation replaces them with `term`. Matching is whole-phrase,
case-insensitive, and any run of whitespace inside the phrase matches. The
longest phrase is tried first, punctuation around it is left in place, and a
replacement is never rescanned. If two entries claim the same phrase, the later
entry wins, matching the list's later-duplicate-wins rule.

Example: term `BAA`, also hears `Bay A`, `bay area`. A transcript saying "the
bay area badge" becomes "the BAA badge".

Correction applies to a complete transcript. The composer's live dictation
streams deltas and inserts them as they arrive, so it gets the vocabulary hint
but not the rewrite.

## Built-in terms

Turnstone ships five respellings that most voices mangle. They are shown in
the Dictionary tab, below the operator's entries:

| Term | Respelling |
|------|------------|
| `PostgreSQL` | `Postgres` |
| `systemctl` | `system control` |
| `kubectl` | `kube control` |
| `YAML` | `yammel` |
| `JSON` | `jay-son` |

Built-in terms apply after the rule groups and custom rules, and before the
dictionary entries. They are active only while
`audio.tts_normalize` is on.

Two ways to change one:

- **Override it.** Add a dictionary entry for the same term. The dictionary
  applies last, so the entry wins without switching the built-in off.
- **Switch it off.** Toggle it in the Dictionary tab; the term is stored in
  `audio.tts_builtin_terms_off` as a JSON list of term names, for example
  `["YAML"]`. Unknown names are ignored, and switching one off changes nothing
  else.

Switch a built-in off when its respelling is wrong for your team; add a
dictionary entry when you want a different pronunciation rather than none.

## Styles

A speaking style is a named natural-language instruction sent to the TTS model
for one read-aloud request. The Styles tab edits `audio.tts_styles`, a JSON
list of at most 20 styles. Each style has:

| Field | Meaning |
|-------|---------|
| `name` | Required, at most 40 characters, unique case-insensitively. |
| `instruction` | What to tell the model, at most 500 characters. Empty means plain delivery. |
| `in_menu` | Whether the style is offered in the per-reply style picker. |

Turnstone seeds three styles: **Professional** (calm and even), **Social**
(relaxed and warm) and **Plain** (an empty instruction, so the model's default
delivery). Edit, delete or reset them like any other list.

The [default speaking style](#default-speaking-style) is used when a request
names none. The style chosen beside the speaker button applies to that reading
only. An unknown name or a style with an empty instruction resolves to no
instruction; the request still plays.

### The Speaking styles capability

Instructions are only sent to a model whose definition declares
`supports_speech_instructions` — the **Speaking styles** capability in the
Models editor. This is an explicit opt-in, not inferred from the model name:
sending an unknown field to a backend is not harmless if it rejects the
request. On a model without the capability, styles are ignored and the picker
is hidden so it does not offer choices that cannot take effect.

Style names and instructions are model-independent. If two TTS models support
instructions, the same style list works for both; if one does not, it simply
reads without the instruction.

## Playback speed

`audio.tts_playback_rate` — default `1.0`, range 0.5–2.0 — changes how fast the
browser plays every read-aloud clip. Pitch is kept, so a faster reading does
not turn into a chipmunk. It changes playback only; the model still synthesizes
at its normal rate, so no audio is re-requested when the speed changes.

The player reads the value when it loads its read-aloud configuration, so a
saved change reaches replies played after a page reload. A failed read or a
value outside the range plays at 1.0.

Use it to even out a model that speaks noticeably slowly or quickly. It is not
a substitute for a pacing profile: the profile controls the silence between
clips, while playback speed changes the rate inside a clip. Tuning one does not
change the other.

## Voice persona

A voice session is a coordinator created with the `voice` persona. Use it when
the conversation is spoken rather than typed: you talk to it, its replies are
read aloud, and it is expected to answer briefly instead of producing a
document. It is a coordinator persona, so start it from the console's
**Coordinator** launcher with **Voice** selected in the persona picker, or
pass `persona` on `POST /v1/api/workstreams/new`:

```json
{"persona": "voice", "initial_message": "What needs my attention today?"}
```

The persona changes the session's instructions and its memory namespace, not
the speech pipeline. Dictation, read-aloud, cleanup and pacing behave exactly
as described above.

### Spoken replies

The persona asks for replies that make sense out loud: lead with the answer,
keep the whole reply short, and prefer plain prose. A spoken reply has no
scrollback, and the player cannot read back the parts that carry meaning only
on screen — a fenced code block and a diagram become a short marker, images
and video are skipped, and each table row becomes its own clip. Ask for a code
sample or a table when you need one and the player will handle it; the persona
just does not volunteer one.

It also starts background agents only when you ask for work, not when you
think out loud. A voice session is easy to talk to in asides, and an aside
like "I wonder whether the tests pass" should not turn into a fan-out.

### Voice memories

A voice session keeps its memories in the `voice` scope, keyed by the acting
user. Every voice session that user opens shares the namespace; text
coordinators keep using `coordinator`, and the two do not mix. The split is
deliberate: a coordinator's memory accumulates working habits — "always
research first", "read the whole file before editing", "run the narrowed test
file" — that make text work reliable and a spoken conversation exhausting. A
voice session should remember how you like to be talked to and what you asked
it to keep, not a playbook for changing a repository. Separating the scopes
also means a terse correction spoken out loud cannot silently reprogram the
coordinator you use for code review.

The scope is durable and per user, not per session: closing and reopening the
voice session does not lose it. It uses the same body-free metadata index and
`memory(action='get', ...)` read path as any other scope; see
[Structured Memory](memory.md#memory-scopes) for the mechanics.

### Starting agents

Two settings bound the background work a voice session can start. Both apply
only to coordinators created with the voice persona; text coordinators ignore
them.

| Setting | Default | Range | Effect |
|---------|---------|-------|--------|
| `coordinator.voice_max_agents_per_turn` | `1` | 0–10 | Child agents one spoken turn may start. A further spawn call is refused with a note telling the model to ask you first. `0` stops voice sessions starting agents at all. |
| `coordinator.voice_child_model` | `deepseek-flash-high` | any model alias; empty means the normal default | Model an agent starts on when the voice coordinator names none. |

The default cap is one because spoken asides are easy to mistake for
requests: an agent-minded coordinator that researches first will act on "I
wonder whether the tests pass". One agent per turn is enough for a real
request and small enough that a misheard aside does not fan out. Raise it when
you routinely ask the voice session for several agents at once ("start one
researcher and one engineer"); set it to `0` for a conversation-only session,
such as while driving.

`coordinator.voice_child_model` keeps a spoken spawn from waiting on a model
that is not loaded. The default is a fast model suited to background work.
Change it when your deployment uses a different alias or when that work should
run on a stronger model; leave it empty to fall back to the normal default.
An alias the registry does not know fails the spawn the same way it would for
a text coordinator.

Edit both in Admin › Settings, section **Coordinator**. They are read when a
spawn is prepared, so a saved change applies to the next spawn without
reopening the session.


## Agent reports

A text coordinator waits for its children: it calls `wait_for_workstream` and
its turn ends when they finish. A voice session cannot do that. The user has
usually finished speaking long before an agent does, and a turn that blocks
leaves the microphone silent with nothing to say. So a finished agent does not
interrupt anything. Its report is saved, and the next thing you say is where
it comes up.

When an agent you started stops — normally, or with an error — the session
stores its last message as a report. On your next spoken turn the model is
told, in one short internal note, which reports are waiting; it does not
receive their text. It answers you first, then offers them in one sentence,
for example "Your research came back — want the short version?". Only if you
say yes does it read the report aloud, and it can also drop a report you are
not interested in. Nothing is read aloud unasked, and nothing is lost if you
say nothing: a report waits until you ask for it or dismiss it.

Reports live in the database, not in the conversation, so they survive the
console restarting or the session being reopened. An agent that finished while
the console was down is offered the next time you speak.

Each agent gets one report per finished turn. An agent that runs again and
finishes again leaves a second report; state changes that do not finish a turn
produce nothing. A report whose agent never produced any text carries a short
placeholder rather than an empty body, and an agent that failed carries its
recorded error where there is one, so a failure is offered as readily as a
success.

`coordinator.voice_report_chime` (default off) controls the audible nudge for a
report that arrives while you are not speaking: a short chime and a badge
instead of the session starting to talk. The report is offered on your next
turn whether the setting is on or off, and the setting never makes the session
speak on its own. It applies only to voice sessions.

Voice sessions no longer have `wait_for_workstream`. If the model asks for it
the call is refused, and the next turn's report offer is how the result
arrives. Text coordinators are unchanged: they keep the tool and the blocking
wait.

## Auto-stop

Auto-stop ends a recording when you stop talking, so a spoken turn is one
click instead of two. While the microphone is recording, the page watches the
audio it is already capturing and stops the clip once you have been quiet
long enough. The clip is then transcribed exactly as if you had clicked the
button yourself — the dictionary hint, correction and every other dictation
behavior apply unchanged.

The detector runs inside the browser against the microphone stream dictation
already captured. Its model is served by the application itself — not a CDN —
and is loaded once per page, on the first recording made with auto-stop on.
Nothing is sent anywhere until the clip is uploaded for transcription, and no
extra permission prompt appears: it uses the same microphone grant as the
recording itself.

### Turning it on

`audio.stt_auto_stop` · default off · boolean

Off keeps today's behavior: the recording runs until you click the microphone
button again. On, the recording also ends itself once you have been quiet for
[`audio.stt_silence_ms`](#how-long-you-must-be-quiet). The button stays the
manual override in both directions — clicking it still stops a recording
immediately, and it is the only way to stop when the detector is unavailable.

Turn it on for hands-free spoken turns. Leave it off if you dictate long
answers with deliberate pauses (or think out loud between sentences) and would
rather decide when the clip ends.

### How long you must be quiet

`audio.stt_silence_ms` · default `900` · range 300–5000

How long the input must stay quiet before the recording ends. Raise it if
dictation cuts you off mid-thought or while you are looking something up;
lower it for snappier turns. A value below a few hundred milliseconds starts
cutting normal pauses between words.

### What counts as talking

`audio.stt_vad_threshold` · default `0.5` · range 0.1–0.9

For each audio frame the detector reports a speech probability between 0 and
1. A frame at or above this value is you talking; a frame below this value
minus 0.15 is silence. The gap between the two lines keeps a frame that
wobbles around the threshold during speech from starting the silence clock.

The recording can only stop after it has started. If you click the mic and say
nothing, it keeps recording until you click again, so a pause before your
first word never ends a turn early.

Audio between the two lines is neither speech nor silence. It cannot start a
recording, and because a stop needs an *unbroken* run of quiet it also
restarts the silence clock. That protects a pause from being cut short by one
noisy frame, at the cost of a room whose background noise sits in that band
never auto-stopping. Raise the threshold until the room's noise reads as
silence; lower it when quiet speech is missed.

### When the detector is unavailable

The detector ships with the application, so a normal deployment has it. If the
browser cannot load it — an older build without the files, or a browser that
refuses the module — dictation falls back to click-to-stop and
`audio.stt_auto_stop` has no effect. Nothing else about dictation changes.

The mic reads these three settings when it first needs them on a page, so a
saved change reaches recordings started after a page reload.

## Streaming read-aloud

Stub: `audio.tts_stream`.
