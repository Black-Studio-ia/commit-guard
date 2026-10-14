# Changelog

## 0.1.0 — 2026-10-14

Initial release.

- Staged-diff and worktree scanning for six languages (stdlib only).
- Rules: risky-filename, conflict-marker (added lines only), focused-test
  (JS/TS), debugger, debug-print (warning), large-file.
- `--strict`, `--max-kb`, `--format json`, `--staged`/`--worktree`;
  exit codes 0/1/2.
- Findings never include matched line content (privacy, tested).
- 19-test stdlib `unittest` suite; CI workflow with self-check.
