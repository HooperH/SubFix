# Changelog

## Latest working copy - 2026-07-03

This is the version intended for the GitHub sharing project. It uses the root
`SubFix.lua`, not the packaged `dist/SubFix_v2.0_macOS/SubFix.lua` copy.

Changes after the 2026-05-15 working copy:

- Add a separate `SubFix_GenerateSelectionSubtitles.lua` DaVinci child plugin
  for generating subtitles from the current In/Out selection.
- Generate selection subtitles from source audio clips directly, including
  audio track selection, linked-channel detection, progress UI, cancellation,
  and selected-range SRT writeback.
- Rebuild the target subtitle track from generated rows while preserving
  subtitles outside the selection and restoring the original playhead.
- Add `generate_subtitles` mode to `subfix_asr_transcribe.py`, including
  readable Chinese/mixed-language subtitle splitting, weighted timing
  distribution, `subtitle_rows` output, and optional SRT export.
- Update `sync_to_plugin.sh` to install the child plugin alongside `SubFix.lua`
  and the ASR helper files.
- Allow pre-delivery timeline-audio speech checks to override export padding
  through `options.padding_seconds`.

## Latest working copy - 2026-05-15

This is the version intended for the GitHub sharing project. It uses the root
`SubFix.lua`, not the packaged `dist/SubFix_v2.0_macOS/SubFix.lua` copy.

Changes after the packaged 2.0 copy:

- Skip the `temperature` request field for newer model families that reject it,
  including Claude 4/5 style names, OpenAI o-series names, and GPT-5 style names.
- For single-pass AI translation or correction tasks, keep original subtitle text
  for missing AI response lines instead of discarding the whole batch.

## 2.0 package - 2026-03-18

- Native TabBar and Stack UI architecture.
- Timecode and update engine.
- Backup and restore workflow.
- AI correction workflow.
