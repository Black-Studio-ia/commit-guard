# Security policy

## Supported versions

| Version | Supported |
|---|---|
| 0.1.x | Yes |

## Design guarantees

- **Read-only.** commit-guard reads the git index and worktree files; it
  never writes, stages, deletes, executes, or transmits anything.
- **No finding ever includes matched line content.** Reports contain file,
  rule, severity, and a fixed message only — enforced by
  `test_no_line_content_in_findings` and
  `test_cli_secret_content_never_printed`. This keeps output safe to paste
  into CI logs and issues.
- **No network access and no dependencies.**

## What this tool is not

- Not a secret-content scanner. Filename rules catch likely accidents
  (`.env`, `*.pem`, …), not every leak pattern. Use `gitleaks` or GitHub
  secret scanning for content-level protection.
- Static patterns cannot see aliased calls or dynamically built code.

## Reporting a vulnerability

Open a GitHub issue with the `security` label. Never include real secrets —
redact values and share key names and reproduction steps only.
