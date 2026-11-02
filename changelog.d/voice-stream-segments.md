### Added

- **Read-aloud streams resume exactly after a break** (`POST /runs/{run_id}/voice/tts/stream` and the entity route).
  With AbstractVoice's segment fields (AbstractVoice ADR 0009), every `audio` line carries `segment`, `text_start` and
  `text_end` (code-point offsets into the request `text`), a `segment_end` line follows each text segment's last
  audio, and `done` counts `segments`. After a break, request the text from `text_end` of the last `segment_end`
  received and discard the audio of the segment that had no `segment_end`: no word is repeated or missed. The
  gateway forwards the fields unchanged and the audit log's `voice_stream` counts `segments`.

### Changed

- **The streamed route hands the engine the request text exactly as sent** (no leading or trailing whitespace
  stripped), so segment offsets index the client's own text. A blank text is still refused (400).
