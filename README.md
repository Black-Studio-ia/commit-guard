# commit-guard

One sentence: **commit-guard inspects your staged git changes and fails the commit (or CI job) when they contain focused tests, leftover debuggers, merge-conflict markers, secret-bearing filenames, or oversized new files — with zero dependencies and no pre-commit framework required.**

## The problem

The most embarrassing defects are committed, not written: `.env` staged with `git add .`, a `<<<<<<< HEAD` left after resolving a merge, an `it.only(` that makes CI green while 400 tests never run, a `pdb.set_trace()` that freezes production. Nothing looks at the git index before the commit happens — unless your repo already has the pre-commit framework configured, which most repos you touch in a week do not.

## Why commit-guard instead of …

| Alternative | What commit-guard does differently |
|---|---|
| `pre-commit-hooks` (`check-merge-conflict`, `debug-statements`, `check-added-large-files`) | Excellent — but requires adopting the pre-commit framework, its `debug-statements` hook is Python-only, and it has no focused-test check. commit-guard is one stdlib file with no setup. |
| ESLint rules (`no-console`, `no-only-tests`, …) | JS-only and need an ESLint setup. commit-guard covers Python, JS/TS, Go, Rust, and Ruby with nothing to install. |
| `gitleaks` / secret scanners | Different job: they scan file *content* for secret shapes. commit-guard blocks risky *filenames* and hygiene issues; use both. |
| Editor extensions (e.g. staged-change checkers for VS Code) | Editor-only. commit-guard runs in any terminal, hook, or CI job. |

> **Honest positioning.** If your repo already runs the pre-commit framework with the relevant hooks plus ESLint, you largely have this covered. commit-guard is for everywhere else: quick `curl` into any repo, works today, polyglot, offline.

## Features (all implemented and tested)

- **risky-filename** (error): `.env` (except `.env.example`/`.sample`/`.template`/`.dist`), `*.pem`/`*.key`, private-key names, `credentials.json`, `*.tfstate`, `.npmrc`/`.pypirc`, `dump.sql`, `node_modules/`, …
- **conflict-marker** (error): `<<<<<<<` / `=======` / `>>>>>>>` in *added* lines only (untouched lines never fail your commit)
- **focused-test** (error, JS/TS): `describe.only`, `it.only`, `test.only`, `context.only`, `fdescribe`, `fit`
- **debugger** (error): `debugger;`, `pdb.set_trace`, `breakpoint()`, `binding.pry`, `byebug`
- **debug-print** (warning): `console.log`, `print()`, `fmt.Println`, `println!`/`dbg!`, `puts` — advisory by default, failing with `--strict`
- **large-file** (error): newly added files over `--max-kb` (default 500)
- Staged-diff mode by default; `--worktree` for full-tree scans; `--format json`; exit codes 0/1/2
- **Privacy:** findings report file, line, rule, message — never the matched line content
- Inline suppression: append `commit-guard-ignore` to a line to silence it

## Requirements

- Python 3.9+ (standard library only), git (for staged mode), Windows/macOS/Linux

## Installation

```sh
curl -O https://raw.githubusercontent.com/Black-Studio-ia/commit-guard/main/commitguard.py
```

Verify: `python commitguard.py --version`

## Quick start

```sh
cd your-repo
git add -A
python /path/to/commitguard.py
```

Example — a staged spec contains `it.only`:

```
commit-guard: 1 finding(s)
  [error] web/app.test.ts (focused-test) focused test (.only) runs a subset of the suite
FAIL: 1 error(s)
```

Remove the `.only`, re-stage, and it passes with exit code 0.

### Pre-commit hook (no framework needed)

```sh
# .git/hooks/pre-commit
#!/bin/sh
python /path/to/commitguard.py --staged || exit 1
```

### GitHub Actions

See [`.github/workflows/ci.yml`](.github/workflows/ci.yml) — test suite plus a self-check on every push. To gate your own repo:

```yaml
- name: Guard staged hygiene
  run: python commitguard.py --worktree --strict
```

## Usage

```
usage: commit-guard [--root DIR] [--staged] [--worktree] [--strict]
                    [--max-kb N] [--exclude DIR] [--format text|json]

  --staged     scan staged git changes (default inside a git work tree)
  --worktree   scan worktree files instead
  --strict     also fail on warnings
  --max-kb     max size in KB for newly added files (default: 500)
```

Exit codes: `0` clean, `1` blocking findings, `2` usage error.

## Common errors

| Message | Fix |
|---|---|
| `git diff --cached failed` | Run inside a git repository, or use `--worktree` |
| `--staged and --worktree are mutually exclusive` | Pick one mode |
| `print()` flagged in a script | It's a warning (advisory); use `--strict` only where you mean it, or append `commit-guard-ignore` |

## Security and privacy

- **Read-only** except reading the git index/worktree; never writes, deletes, executes, or transmits anything. No network access, no dependencies.
- **Never prints matched content** — enforced by dedicated tests — so pasting output can't leak the secret it just caught.
- Not a secret scanner: filename rules catch likely accidents, not every leak. Pair with `gitleaks` for content scanning. See [SECURITY.md](SECURITY.md).

## Known limitations

- Focused-test detection covers JS/TS only (Python/Go/Rust have no `.only` equivalent; skipped-test auditing is out of scope).
- `print()`/`console.log` are warnings by design — legitimate output exists; `--strict` is opt-in.
- `git diff` filenames with exotic quoting edge cases may mis-resolve; paths are reported git-style (`/` separators).
- New-file size is measured from the indexed blob; worktree mode applies size checks to untracked files (tracked files assumed reviewed).
- Aliased/renamed test functions (`const only = describe.only`) are invisible to static patterns.

## Running the tests

```sh
python -m unittest discover -s tests -v
```

19 tests, stdlib only (git binary required for staged-mode fixtures).

## License

MIT — see [LICENSE](LICENSE).

## Support this project

If this tool saves you a bad commit, you may optionally support its continued development with USDT.

Network: TRC20 (Tron)

USDT address: TXQCrX61CceFxX5gnFg9N3ssZEC7MavwBQ

Please verify the network and address carefully before sending — USDT on the wrong network can be lost permanently. Contributions are voluntary and do not guarantee specific features, services, or financial returns. The software remains fully usable under its MIT license regardless.

Contact: ia.creative.tn@gmail.com
