#!/usr/bin/env python3
"""commit-guard: stop dangerous staged changes before they become commits.

Scans staged git changes (or a worktree) for the mistakes that turn into
incidents and embarrassment:

* secret-bearing filenames staged by accident (``.env``, ``*.pem`` …),
* merge-conflict markers left in added lines,
* focused tests (``describe.only``, ``it.only``, ``fdescribe`` …) that make
  CI green while most tests never run,
* leftover debuggers and debug statements across five languages
  (Python, JS/TS, Go, Rust, Ruby; shell files get filename/marker/size
  rules),
* giant newly added files that bloat every future clone.

Zero dependencies, offline. Reports never echo matched line *content* --
only file, line, rule, and message -- so findings are safe to paste into
CI logs and issues.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys

__version__ = "0.1.0"

DEFAULT_MAX_KB = 500
DEFAULT_EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", ".tox",
    "__pycache__", "dist", "build", "vendor", "target", ".idea", ".vscode",
}
_MAX_FILE_BYTES = 1024 * 1024
_BINARY_PROBE_BYTES = 8192

# Basename (or path-segment) patterns that should basically never be
# committed. Kept conservative: template files (.env.example, …) are fine.
RISKY_BASENAMES = {
    ".env", ".env.local", ".env.production", ".env.staging",
    ".env.development", ".env.test", "credentials.json", "dump.sql",
    ".npmrc", ".pypirc", ".DS_Store",
}
RISKY_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".tfstate")
RISKY_PREFIXES = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")
RISKY_SEGMENTS = {"node_modules", "__pycache__"}
SAFE_ENV_TEMPLATES = {
    ".env.example", ".env.sample", ".env.template", ".env.dist",
}

JS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}
PY_EXTS = {".py"}
GO_EXTS = {".go"}
RUST_EXTS = {".rs"}
RUBY_EXTS = {".rb"}
SHELL_EXTS = {".sh", ".bash"}

# -- content rules apply to ADDED lines only; each entry is
# (rule_id, severity, extensions|None for all, compiled_pattern, message).
CONTENT_RULES: list[tuple[str, str, set[str] | None, re.Pattern[str], str]] = [
    ("focused-test", "error", JS_EXTS,
     re.compile(r"""(?<!\w)(?:describe|it|test|context)\s*\.\s*only\s*\("""),
     "focused test (.only) runs a subset of the suite"),
    ("focused-test", "error", JS_EXTS,
     re.compile(r"""(?<!\w)f(?:describe|it)\s*\("""),
     "focused test (fdescribe/fit) runs a subset of the suite"),
    ("debugger", "error", JS_EXTS,
     re.compile(r"""(?<!\w)debugger\s*;"""),
     "debugger statement left in code"),
    ("debugger", "error", PY_EXTS,
     re.compile(r"""(?<!\w)(?:pdb\.set_trace|breakpoint)\s*\("""),
     "python debugger breakpoint left in code"),
    ("debugger", "error", RUBY_EXTS,
     re.compile(r"""(?<!\w)(?:binding\.pry|byebug)\b"""),
     "ruby debugger left in code"),
    ("debug-print", "warning", JS_EXTS,
     re.compile(r"""(?<!\w)console\s*\.\s*(?:log|info|debug|trace)\s*\("""),
     "console.* debug output (use a logger for intentional output)"),
    ("debug-print", "warning", PY_EXTS,
     re.compile(r"""(?<!\w)(?:print|pprint)\s*\("""),
     "print() debug output"),
    ("debug-print", "warning", GO_EXTS,
     re.compile(r"""(?<!\w)fmt\s*\.\s*Print(?:ln|f)?\s*\("""),
     "fmt.Print* debug output"),
    ("debug-print", "warning", RUST_EXTS,
     re.compile(r"""(?<!\w)(?:println|eprintln|dbg)!\s*\("""),
     "println!/dbg! debug output"),
    ("debug-print", "warning", RUBY_EXTS,
     re.compile(r"""(?<!\w)(?:puts|print|pp)\s+["'(]"""),
     "puts/print debug output"),
]

SUPPRESS_RE = re.compile(r"commit-guard-ignore")


def _run_git(args: list[str], cwd: str) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _in_git_repo(root: str) -> bool:
    code, _, _ = _run_git(["rev-parse", "--is-inside-work-tree"], root)
    return code == 0


def _is_binary_bytes(chunk: bytes) -> bool:
    return b"\0" in chunk


def parse_staged_diff(diff_text: str) -> tuple[dict[str, list[str]], set[str]]:
    """Parse ``git diff --cached -U0`` output.

    Returns ``(added_lines, new_files)`` where ``added_lines`` maps
    repo-relative path -> list of added line *contents* (without the
    leading ``+``) and ``new_files`` holds paths that are newly added.
    Binary files are skipped.
    """
    added: dict[str, list[str]] = {}
    new_files: set[str] = set()
    path: str | None = None
    is_new = False
    is_binary = False
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            path = None
            is_new = False
            is_binary = False
            continue
        if raw.startswith("Binary files "):
            is_binary = True
            continue
        if raw == "--- /dev/null":
            is_new = True
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                path = None
                continue
            if target.startswith('"') and target.endswith('"'):
                target = target[1:-1]
            if target.startswith("b/"):
                target = target[2:]
            # Keep git-style forward slashes: they display fine everywhere
            # and remain valid for follow-up git commands on Windows too.
            path = target
            if is_new and path is not None:
                new_files.add(path)
            added.setdefault(path or "", [])
            continue
        if raw.startswith("@@ ") or raw.startswith("--- "):
            continue
        if path is None or is_binary:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            added.setdefault(path, []).append(raw[1:])
    return ({k: v for k, v in added.items() if k}, new_files)


def check_risky_name(path: str) -> str | None:
    """Return a message if a staged path looks dangerous, else None."""
    segments = path.replace("\\", "/").split("/")
    base = segments[-1]
    if base in SAFE_ENV_TEMPLATES:
        return None
    if base in RISKY_BASENAMES:
        return f"risky filename staged ({base} often holds secrets)"
    lowered = base.lower()
    if lowered.endswith(RISKY_SUFFIXES):
        return f"risky file type staged ({base} often holds secrets)"
    _, dot, ext = lowered.rpartition(".")
    # id_rsa.txt / id_rsa.md are usually documentation; extensionless
    # id_rsa (or key-like extensions) is treated as a real key.
    if lowered.startswith(RISKY_PREFIXES) and (
            not dot or f".{ext}" in {".pem", ".key", ".ppk"}):
        return f"possible private key staged ({base})"
    if any(seg in RISKY_SEGMENTS for seg in segments):
        return f"generated directory staged ({path})"
    return None


def check_added_line(line: str, ext: str) -> list[tuple[str, str, str]]:
    """Return ``[(rule_id, severity, message)]`` for one added line."""
    findings: list[tuple[str, str, str]] = []
    if SUPPRESS_RE.search(line):
        return findings
    stripped = line.strip()
    if (stripped.startswith("<<<<<<< ") or stripped.startswith(">>>>>>> ")
            or stripped == "======="):
        findings.append(("conflict-marker", "error",
                         "merge-conflict marker left in added lines"))
    for rule_id, severity, exts, pattern, message in CONTENT_RULES:
        if exts is not None and ext not in exts:
            continue
        if pattern.search(line):
            findings.append((rule_id, severity, message))
    return findings


def iter_worktree_files(root: str, exclude_dirs: set[str]):
    found: list[str] = []
    skipped = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in exclude_dirs)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                if os.path.getsize(full) > _MAX_FILE_BYTES:
                    skipped += 1
                    continue
            except OSError:
                skipped += 1
                continue
            try:
                with open(full, "rb") as handle:
                    if _is_binary_bytes(handle.read(_BINARY_PROBE_BYTES)):
                        skipped += 1
                        continue
            except OSError:
                skipped += 1
                continue
            found.append(full)
    return found, skipped


def scan_staged(root: str, max_kb: int) -> tuple[list[dict], dict]:
    """Scan staged changes. Returns ``(findings, stats)``."""
    findings: list[dict] = []
    code, diff_text, err = _run_git(
        ["diff", "--cached", "--no-color", "--no-ext-diff", "-U0",
         "--no-renames", "--", "."], root)
    if code != 0:
        return findings, {"error": f"git diff --cached failed: {err.strip()}"}
    added_lines, new_files = parse_staged_diff(diff_text)
    # Oversized newly added files: measure from the index version.
    for path in sorted(new_files):
        code, content, _ = _run_git(["show", f":{path}"], root)
        if code == 0 and len(content.encode("utf-8", "replace")) > max_kb * 1024:
            findings.append({"path": path, "line": None, "rule": "large-file",
                             "severity": "error",
                             "message": f"new file exceeds {max_kb} KB"})
    for path in sorted(added_lines):
        risky = check_risky_name(path)
        if risky:
            findings.append({"path": path, "line": None,
                             "rule": "risky-filename", "severity": "error",
                             "message": risky})
        ext = os.path.splitext(path)[1].lower()
        lineno = 0
        for line in added_lines[path]:
            lineno += 1  # position within added lines, not file line
            for rule_id, severity, message in check_added_line(line, ext):
                findings.append({"path": path, "line": None, "rule": rule_id,
                                 "severity": severity, "message": message})
                break  # one finding per added line keeps output readable
    return findings, {"files": len(added_lines), "new_files": len(new_files)}


def scan_worktree(root: str, exclude_dirs: set[str],
                  max_kb: int) -> tuple[list[dict], dict]:
    """Scan worktree files fully (used with --worktree or outside git)."""
    findings: list[dict] = []
    files, skipped = iter_worktree_files(root, exclude_dirs)
    in_repo = _in_git_repo(root)
    untracked: set[str] = set()
    if in_repo:
        code, out, _ = _run_git(
            ["ls-files", "--others", "--exclude-standard", "-z"], root)
        if code == 0:
            untracked = {p for p in out.split("\0") if p}
    for full in files:
        rel = os.path.relpath(full, root)
        risky = check_risky_name(rel)
        if risky:
            findings.append({"path": rel, "line": None,
                             "rule": "risky-filename", "severity": "error",
                             "message": risky})
        applies_size = (rel in untracked) if in_repo else True
        if applies_size:
            try:
                if os.path.getsize(full) > max_kb * 1024:
                    findings.append({"path": rel, "line": None,
                                     "rule": "large-file",
                                     "severity": "error",
                                     "message": f"file exceeds {max_kb} KB"})
            except OSError:
                pass
        ext = os.path.splitext(full)[1].lower()
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as handle:
                for lineno, line in enumerate(handle, start=1):
                    for rule_id, severity, message in check_added_line(
                            line.rstrip("\n"), ext):
                        findings.append({"path": rel, "line": lineno,
                                         "rule": rule_id, "severity": severity,
                                         "message": message})
                        break
        except OSError:
            continue
    return findings, {"files": len(files), "skipped": skipped}


def evaluate(findings: list[dict], strict: bool) -> tuple[bool, str]:
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = sum(1 for f in findings if f["severity"] == "warning")
    failing = errors + (warnings if strict else 0)
    if failing == 0:
        return True, ""
    parts = [f"{errors} error(s)"]
    if warnings:
        parts.append(f"{warnings} warning(s){' (failing under --strict)' if strict else ' (advisory)'}")
    return False, "; ".join(parts)


def format_text(findings: list[dict], stats: dict, reason: str) -> str:
    out = [f"commit-guard: {len(findings)} finding(s)"]
    for finding in findings:
        loc = finding["path"]
        if finding["line"] is not None:
            loc += f":{finding['line']}"
        out.append(f"  [{finding['severity']}] {loc} "
                   f"({finding['rule']}) {finding['message']}")
    out.append("FAIL: " + reason if reason else "PASS: no blocking findings")
    return "\n".join(out) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="commit-guard",
        description="Fail the commit/CI when staged changes contain "
                    "focused tests, debuggers, conflict markers, risky "
                    "filenames, or oversized new files. Never prints "
                    "matched line content.",
    )
    parser.add_argument("--root", default=".",
                        help="repository or directory to scan (default: .)")
    parser.add_argument("--staged", action="store_true",
                        help="scan staged git changes (default inside a "
                             "git work tree)")
    parser.add_argument("--worktree", action="store_true",
                        help="scan worktree files instead of staged changes")
    parser.add_argument("--strict", action="store_true",
                        help="also fail on warnings (debug prints, …)")
    parser.add_argument("--max-kb", type=int, default=DEFAULT_MAX_KB,
                        help=f"max size in KB for newly added files "
                             f"(default: {DEFAULT_MAX_KB})")
    parser.add_argument("--exclude", action="append", default=[],
                        help="extra directory name to skip (repeatable)")
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="output format (default: text)")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.staged and args.worktree:
        parser.error("--staged and --worktree are mutually exclusive")
    if args.max_kb < 1:
        parser.error("--max-kb must be at least 1")

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print("commit-guard: error: --root is not a directory: "
              f"{args.root}", file=sys.stderr)
        return 2
    exclude_dirs = set(DEFAULT_EXCLUDE_DIRS) | set(args.exclude)

    use_staged = args.staged or (not args.worktree and _in_git_repo(root))
    if use_staged:
        findings, stats = scan_staged(root, args.max_kb)
        mode = "staged"
    else:
        findings, stats = scan_worktree(root, exclude_dirs, args.max_kb)
        mode = "worktree"
    if "error" in stats:
        print(f"commit-guard: error: {stats['error']}", file=sys.stderr)
        return 2
    ok, reason = evaluate(findings, args.strict)

    if args.format == "json":
        print(json.dumps({"ok": ok, "mode": mode, "reason": reason,
                          "stats": stats, "findings": findings}, indent=2))
    else:
        print(format_text(findings, stats, reason), end="")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
