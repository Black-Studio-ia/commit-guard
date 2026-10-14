"""Tests for commit-guard. Run with: python -m unittest discover -s tests -v"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import commitguard


def git(*args, cwd):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True)
    assert proc.returncode == 0, (args, proc.stderr)
    return proc.stdout


def make_repo(files):
    root = tempfile.mkdtemp(prefix="commit-guard-test-")
    git("init", cwd=root)
    git("config", "user.email", "test@example.com", cwd=root)
    git("config", "user.name", "Test", cwd=root)
    for rel, content in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(path, mode,
                  **({} if isinstance(content, bytes)
                     else {"encoding": "utf-8", "newline": "\n"})) as handle:
            handle.write(content)
    return root


class UnitTests(unittest.TestCase):
    def test_risky_names(self):
        for bad in [".env", ".env.local", "a/b/.env", "key.pem", "id_rsa",
                    "id_ed25519_backup", "creds/credentials.json", "dump.sql",
                    "x/node_modules/y.js", "a.tfstate", ".npmrc"]:
            self.assertIsNotNone(commitguard.check_risky_name(bad), bad)
        for good in [".env.example", ".env.sample", "src/app.py", "README.md",
                     ".gitignore", "id_rsa.txt"]:
            self.assertIsNone(commitguard.check_risky_name(good), good)

    def test_conflict_markers(self):
        self.assertTrue(commitguard.check_added_line("<<<<<<< HEAD", ".py"))
        self.assertTrue(commitguard.check_added_line("=======", ".js"))
        self.assertTrue(commitguard.check_added_line(">>>>>>> main", ".go"))
        # Decorative banners must not match.
        self.assertEqual(
            commitguard.check_added_line("==================", ".py"), [])
        self.assertEqual(
            commitguard.check_added_line("x = a ====== b", ".py"), [])

    def test_focused_tests(self):
        for code in ["describe.only('x', () => {});",
                     "it.only('x', () => {});",
                     "test.only('x', () => {});",
                     "context.only('x', () => {});",
                     "fdescribe('x', () => {});",
                     "fit('x', () => {});"]:
            hits = commitguard.check_added_line(code, ".ts")
            self.assertTrue(any(h[0] == "focused-test" for h in hits), code)
        for code in ["describe('x', () => {});", "it.skip('x', () => {});",
                     "test('x', () => {});", "xit('x', () => {});",
                     "challenge.only_if_speedy();"]:
            hits = commitguard.check_added_line(code, ".ts")
            self.assertFalse(any(h[0] == "focused-test" for h in hits), code)

    def test_debuggers_error_level(self):
        cases = [("debugger;", ".js"),
                 ("import pdb; pdb.set_trace()", ".py"),
                 ("breakpoint()", ".py"),
                 ("binding.pry", ".rb")]
        for code, ext in cases:
            hits = commitguard.check_added_line(code, ext)
            self.assertTrue(
                any(h[0] == "debugger" and h[1] == "error" for h in hits),
                code)

    def test_debug_prints_warning_level(self):
        cases = [("console.log('x')", ".js"),
                 ("print('x')", ".py"),
                 ('fmt.Println("x")', ".go"),
                 ('println!("x")', ".rs")]
        for code, ext in cases:
            hits = commitguard.check_added_line(code, ext)
            self.assertTrue(
                any(h[0] == "debug-print" and h[1] == "warning"
                    for h in hits), code)
        # Legitimate logging stays quiet.
        self.assertEqual(
            commitguard.check_added_line("console.error('x')", ".js"), [])
        self.assertEqual(
            commitguard.check_added_line("logger.info('x')", ".py"), [])

    def test_suppression_comment(self):
        line = "print('keep me')  # commit-guard-ignore"
        self.assertEqual(commitguard.check_added_line(line, ".py"), [])

    def test_no_line_content_in_findings(self):
        # The privacy contract: findings carry rule/path/message only.
        findings, _ = commitguard.scan_worktree(
            make_repo({"a.py": "API_KEY = 'sk-live-SECRET-123'\nprint(API_KEY)\n"}),
            set(commitguard.DEFAULT_EXCLUDE_DIRS), 500)
        text = json.dumps(findings)
        self.assertNotIn("sk-live-SECRET-123", text)


class StagedTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo({
            "src/app.py": "print('hello')\n",
            "web/app.test.ts": "describe('a', () => {});\n",
        })
        git("add", "-A", cwd=self.root)
        git("commit", "-m", "base", cwd=self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def stage(self, rel, content):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        git("add", rel, cwd=self.root)

    def test_clean_staged_change_passes(self):
        self.stage("src/app.py", "print('hello')\nx = 1\n")
        findings, _ = commitguard.scan_staged(self.root, 500)
        errors = [f for f in findings if f["severity"] == "error"]
        self.assertEqual(errors, [])

    def test_focused_only_fails(self):
        self.stage("web/app.test.ts",
                   "describe('a', () => {});\nit.only('b', () => {});\n")
        findings, _ = commitguard.scan_staged(self.root, 500)
        self.assertTrue(any(f["rule"] == "focused-test" and
                            f["severity"] == "error" for f in findings))

    def test_preexisting_issues_in_untouched_lines_ignored(self):
        # A .only on a line we did NOT touch must not fail the commit.
        with open(os.path.join(self.root, "web", "app.test.ts"), "w",
                  encoding="utf-8", newline="\n") as handle:
            handle.write("it.only('old', () => {});\ndescribe('a', () => {});\n")
        git("add", "-A", cwd=self.root)
        git("commit", "-m", "old only", cwd=self.root)
        self.stage("web/app.test.ts",
                   "it.only('old', () => {});\ndescribe('a', () => {});\n// new comment\n")
        findings, _ = commitguard.scan_staged(self.root, 500)
        self.assertFalse(any(f["rule"] == "focused-test" for f in findings))

    def test_risky_filename_staged(self):
        self.stage(".env", "SECRET=abc\n")
        findings, _ = commitguard.scan_staged(self.root, 500)
        self.assertTrue(any(f["rule"] == "risky-filename" for f in findings))

    def test_conflict_marker_added(self):
        self.stage("src/app.py", "print('hello')\n<<<<<<< HEAD\nx = 1\n")
        findings, _ = commitguard.scan_staged(self.root, 500)
        self.assertTrue(any(f["rule"] == "conflict-marker" for f in findings))

    def test_large_new_file(self):
        big = "x = 1\n" + "#" * (600 * 1024) + "\n"
        self.stage("data/blob.dat", big)
        findings, _ = commitguard.scan_staged(self.root, 500)
        self.assertTrue(any(f["rule"] == "large-file" for f in findings))

    def test_evaluate_strict(self):
        self.stage("src/app.py", "print('hello')\nprint('debug me')\n")
        findings, _ = commitguard.scan_staged(self.root, 500)
        ok, _ = commitguard.evaluate(findings, strict=False)
        self.assertTrue(ok)  # warnings are advisory by default
        ok2, reason = commitguard.evaluate(findings, strict=True)
        self.assertFalse(ok2)
        self.assertIn("warning", reason)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.root = make_repo({"app.py": "x = 1\n"})
        git("add", "-A", cwd=self.root)
        git("commit", "-m", "base", cwd=self.root)
        self.script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "commitguard.py")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, self.script, *args],
                              capture_output=True, text=True, cwd=self.root)

    def test_cli_clean_passes(self):
        proc = self.run_cli("--root", self.root)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PASS", proc.stdout)

    def test_cli_blocks_only(self):
        with open(os.path.join(self.root, "a.test.js"), "w",
                  encoding="utf-8") as handle:
            handle.write("describe.only('x', () => {});\n")
        git("add", "-A", cwd=self.root)
        proc = self.run_cli("--root", self.root)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("focused-test", proc.stdout)

    def test_cli_json_valid(self):
        proc = self.run_cli("--root", self.root, "--format", "json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["mode"], "staged")

    def test_cli_conflict_exit_2(self):
        proc = self.run_cli("--staged", "--worktree")
        self.assertEqual(proc.returncode, 2)

    def test_cli_secret_content_never_printed(self):
        os.makedirs(os.path.join(self.root, "n"), exist_ok=True)
        with open(os.path.join(self.root, "n", "k.py"), "w",
                  encoding="utf-8") as handle:
            handle.write("import pdb; pdb.set_trace()  # token ghp_FAKE123\n")
        git("add", "-A", cwd=self.root)
        proc = self.run_cli("--root", self.root)
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("ghp_FAKE123", proc.stdout)
        self.assertNotIn("ghp_FAKE123", proc.stderr)


if __name__ == "__main__":
    unittest.main()
