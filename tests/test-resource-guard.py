"""Declared resource checks use only self-cleaning temporary project fixtures."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
sys.path.insert(0, str(SCRIPTS))
import check_plan as P
import run_checks as C
import run_state as R


class ResourceGuardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "input").write_text("fixture")
        self.argv = [sys.executable, "-B", "-c", "from pathlib import Path; Path('ran').touch()"]

    def start(self):
        state = R.start(self.root)
        self.args = self.root, state["run_id"], state["owner_token"]

    def execute(self, name="test", **changes):
        values = dict(argv=self.argv, scope=["input"])
        values.update(changes)
        return C.execute_check(*self.args, name, **values)

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "pipeline_cli.py"),
                                 *args], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_preflight_read_only_nested_sibling_unknown_and_default(self):
        before = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        for writes, protected, status in [(["dist"], ["dist"], "conflict"),
                (["dist/a"], ["dist"], "conflict"), (["dist"], ["dist/a"], "conflict"),
                (["dist-two"], ["dist"], "disjoint"), ([], ["dist"], "disjoint"),
                (None, ["dist"], "unknown_writes"), (None, None, "not_declared"),
                (["dist"], [], "not_declared")]:
            with self.subTest(writes=writes, protected=protected):
                info = C.inspect_runner(self.root, self.argv, write_paths=writes, protected_paths=protected)
                self.assertEqual(info["resources"]["status"], status)
                self.assertEqual(info["resources"]["basis"], "declared resources only")
                self.assertEqual(info["resources"]["safe_to_execute"], status not in {"conflict", "unknown_writes"})
        after = {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["input"])

    def test_all_declarations_validate_even_with_unknown_other_set(self):
        (self.root / "link").symlink_to(self.root / "input")
        for invalid in (["../outside"], [str(self.root / "absolute")], ["link/a"], "dist", [1]):
            for key in ("write_paths", "protected_paths"):
                with self.subTest(key=key, invalid=invalid), self.assertRaises(ValueError):
                    C.inspect_runner(self.root, self.argv, **{key: invalid})

    def test_execution_rejects_conflict_unknown_and_invalid_before_command(self):
        self.start()
        for index, changes in enumerate([
                dict(protected_paths=["dist"]),
                dict(write_paths=["dist/assets"], protected_paths=["dist"]),
                dict(write_paths=["dist"], protected_paths=["dist/assets"]),
                dict(write_paths=[], protected_paths=["../bad"])]):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.execute(f"blocked{index}", **changes)
            self.assertFalse((self.root / "ran").exists())
        self.assertFalse(R.status(self.root)["state"].get("running_checks"))
        self.assertEqual(self.execute("sibling", write_paths=["other"], protected_paths=["dist"])["exit_code"], 0)

    def test_old_default_and_valid_cache_do_not_execute_protected_writes(self):
        self.start()
        first = self.execute("first")
        (self.root / "ran").unlink()
        # Valid evidence skips the command even though execution would be unsafe.
        cached = self.execute("cache", protected_paths=["dist"], impact_covered=True,
                              reuse_from=first["report"])
        self.assertTrue(cached["reused"])
        self.assertFalse((self.root / "ran").exists())
        self.assertFalse((self.root / "dist").exists())
        (self.root / "input").write_text("changed")
        with self.assertRaises(ValueError):
            self.execute("cache-miss", protected_paths=["dist"], impact_covered=True,
                         reuse_from=first["report"])
        self.assertFalse((self.root / "ran").exists())

    def test_plan_passes_protected_declaration_to_real_executor(self):
        self.start()
        spec = {"argv": self.argv, "scope": ["input"], "coverage": ["unit"],
                "write_paths": ["dist"], "protected_paths": ["dist/preview"]}
        plan = P.save_plan(*self.args, "plan", {"check": spec})
        with self.assertRaises(ValueError):
            P.run_planned(*self.args, plan, "check", "blocked")
        self.assertFalse((self.root / "ran").exists())
        spec["write_paths"] = ["isolated"]
        safe = P.save_plan(*self.args, "safe", {"check": spec})
        self.assertEqual(P.run_planned(*self.args, safe, "check", "safe")["exit_code"], 0)

    def test_cli_preflight_and_check_use_same_guard(self):
        initial = sorted(self.root.iterdir())
        result = self.cli("preflight", "--write-path", "dist/assets", "--protected-path", "dist",
                          "--", *self.argv, expected=1)
        self.assertEqual(result["resources"]["status"], "conflict")
        self.assertEqual(initial, sorted(self.root.iterdir()))
        self.assertEqual(self.cli("preflight", "--protected-path", "dist", "--", *self.argv,
                                 expected=1)["resources"]["status"], "unknown_writes")
        self.assertEqual(self.cli("preflight", "--write-path", "other", "--protected-path", "dist",
                                 "--protected-path", "preview", "--", *self.argv)["resources"]["status"], "disjoint")
        started = self.cli("start")
        receipt = started["receipt"]
        self.cli("check", "--receipt", receipt, "--name", "blocked", "--scope", "input",
                 "--write-path", "dist", "--protected-path", "dist/assets", "--", *self.argv, expected=2)
        self.assertFalse((self.root / "ran").exists())
        self.cli("check", "--receipt", receipt, "--name", "safe", "--scope", "input",
                 "--write-path", "isolated", "--protected-path", "dist", "--", *self.argv)
        self.assertTrue((self.root / "ran").exists())

    def test_cli_plan_run_rejects_declared_protected_output(self):
        receipt = self.cli("start")["receipt"]
        payload = {"checks": {"build": {"argv": self.argv, "scope": ["input"],
                   "coverage": ["build"], "write_paths": ["dist"],
                   "protected_paths": ["dist/preview"]}}}
        plan = self.cli("plan", "--receipt", receipt, "--name", "plan", "--json", json.dumps(payload))
        self.cli("run", "--receipt", receipt, "--plan", plan["plan"], "--id", "build",
                 "--name", "blocked", expected=2)
        self.assertFalse((self.root / "ran").exists())


if __name__ == "__main__":
    unittest.main()
