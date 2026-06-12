# Changelog

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
