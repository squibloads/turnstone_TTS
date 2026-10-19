# Voice work on Turnstone (a personal fork)

This is a personal fork of [turnstonelabs/turnstone](https://github.com/turnstonelabs/turnstone). It adds voice to Turnstone: read-aloud (TTS), dictation (STT), a Voice admin page, and a hands-free voice mode on the coordinator. It is not an official Turnstone project. Upstream has not reviewed most of this code. A few small, self-contained pieces are offered upstream one PR at a time, starting with the branches at the top of the table. It runs on one home server, built from upstream `dev` with the branches below merged on top.

**The code is LLM-generated** (Claude Code, plus Turnstone working on itself), with tests and a human checking that it behaves correctly. No experienced Turnstone developer has reviewed it. Read it as a set of ideas and working references, not as code ready to merge.

You're on the `fork-guide` branch, which holds only this page. The code is on the `feat/*` branches. `dev` mirrors upstream.

## Branches

Each branch is stacked on the one above it and merged in this order. "Own diff" is what that branch adds over its parent, including tests and docs, excluding vendored binaries (measured 2026-10-19 against upstream `dev`).

| Branch | Adds | Own diff | Stands alone? |
|---|---|---|---|
| `feat/tts-configurable-limits` | Speech-synthesis text cap and timeout become `audio.*` settings | +212 / 7 files | **Yes**, applies cleanly to current `dev` |
| `feat/tts-speakable-text` | Text normalisation before TTS: units, numbers, symbols, code, Markdown | +761 / 7 | Nearly: shares the settings location with the branch above |
| `feat/tts-segmented-playback` | Long replies read aloud in segments that start playing sooner; diagrams and media skipped | +1,567 / 4 | Needs the two above |
| `feat/tts-every-message` | A read-aloud button on every finished reply | +279 / 4 | Small, but stacked |
| `feat/tts-coordinator` | Shared `tts_player` module; read-aloud in coordinator sessions | +2,793 / 23 | Stacked |
| `feat/stt-coordinator` | Shared `stt_mic` module; dictation into the coordinator composer | +1,903 / 14 | Stacked |
| `feat/voice-admin-page` | Voice admin page: voices, speaking styles, playback speed | +6,417 / 39 | Stacked |
| `feat/voice-tuning` | Voice profiles per TTS model, text-cleanup rules, docs | +9,778 / 33 | Stacked |
| `feat/voice-persona` | Voice coordinator persona; per-turn spawn cap; default child model | +1,846 / 23 | Stacked |
| `feat/voice-reports` | Agent reports in voice; voice-activity detection (VAD) auto-stop, Silero VAD vendored; streamed read-aloud | +4,329 / 56 | Stacked |
| `feat/voice-loop` | Hands-free loop (listen, reply, speak, listen) and a voice-session launcher | +3,307 / 23 | Stacked |
| `feat/voice-mic-mute` | Mic on/off switch inside voice mode | +395 / 9 | Stacked |
| `feat/voice-report-announce` | Announce finished agents in an idle voice session | +272 / 5 | Stacked |
| `feat/voice-r6` | Voice-frame segmenter; host-path facts for agents | +2,351 / 18 | Stacked |
| `feat/voice-r7` | Frame capture; slash commands in coordinator and voice sessions. **Top of the stack, what is deployed** | +9,088 / 70 | Stacked |

`docs/voice.md` on `feat/voice-r7` describes voice mode as shipped, including every voice setting.

## Taking pieces from here

Any of it, in any form, is fine to take, rewrite or ignore. The lower branches are the most self-contained. From `feat/voice-tuning` up, the work assumes everything below it.

## License

Same as upstream: Apache License 2.0. The `LICENSE` file is on every code branch.
