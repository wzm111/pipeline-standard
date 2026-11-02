"""Managed adoption gates use real CLI/API calls and system-temp fixtures only."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
sys.path.insert(0, str(SCRIPTS))
import check_plan as P
import observed_checks as H
import operation_metrics as O
import role_dispatch as D
import run_audit as A
import run_checks as C
import run_policy as V
import run_state as R
import run_tasks as T


class PolicyTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "PIPELINE.md").write_text("fixture private rule text")
        (self.root / "input").write_text("stable input")
        self.argv = [sys.executable, "-B", "-c", "from pathlib import Path; Path('marker').touch()"]
        self.check = {"argv": self.argv, "scope": ["input"], "coverage": ["unit"], "write_paths": []}
        self.host = {"command": "fixture.host", "scope": ["input"], "environment": {"host": "fixture"}}

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "pipeline_cli.py"), *args],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertFalse(result.stderr)
        self.assertNotIn("fixture private rule text", result.stdout)
        value = json.loads(result.stdout)
        if hasattr(self, "args"):
            self.assertNotIn(self.args[2], result.stdout)
        return value

    def start(self, acknowledge=False):
        self.started = self.cli("start")
        self.receipt = self.started["receipt"]
        private = json.loads(Path(self.receipt).read_text())
        self.args = self.root, private["run_id"], private["owner_token"]
        if acknowledge:
            self.ack()
        return self.started

    def ack(self):
        revision = self.cli("policy")["revision"]
        return self.cli("policy-ack", "--receipt", self.receipt, "--revision", revision)

    def state(self):
        return R.status(self.root)["state"]

    def snapshot(self):
        return {p.relative_to(self.root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in self.root.rglob("*") if p.is_file()}

    def test_unacknowledged_gates_shared_entrypoints_without_execution_or_pass(self):
        started = self.start()
        self.assertEqual(started["policy"]["mode"], "managed")
        self.assertFalse(started["policy"]["caller_acknowledged"])
        before = self.snapshot()
        actions = [lambda: C.execute_check(*self.args, "blocked", argv=self.argv, scope=["input"]),
                   lambda: H.begin(*self.args, "host", **self.host),
                   lambda: P.save_plan(*self.args, "plan", {"unit": self.check}),
                   lambda: D.begin(*self.args, "developer"),
                   lambda: T.start(*self.args, "task", "goal", ["acceptance"]),
                   lambda: R.finish(*self.args, "completed", executors_stopped=True, stop_evidence="fixture stopped")]
        for action in actions:
            with self.assertRaisesRegex(ValueError, "policy"):
                action()
            self.assertEqual(self.snapshot(), before)
        self.cli("check", "--receipt", self.receipt, "--name", "blocked", "--scope", "input",
                 "--", *self.argv, expected=2)
        self.assertFalse((self.root / "marker").exists())
        self.assertEqual(C.check_summary(self.root)["executed"], 0)
        self.assertFalse(self.state().get("operation_spans"))

    def test_wrong_revision_owner_and_explicit_ack(self):
        self.start()
        before = self.snapshot()
        self.cli("policy-ack", "--receipt", self.receipt, "--revision", "wrong", expected=2)
        with self.assertRaises(ValueError):
            V.acknowledge(self.root, self.args[1], "wrong-owner", self.started["policy"]["revision"])
        self.assertEqual(before, self.snapshot())
        self.assertTrue(self.ack()["caller_acknowledged"])
        self.assertEqual(C.execute_check(*self.args, "allowed", argv=self.argv, scope=["input"])["exit_code"], 0)

    def test_rule_add_remove_edit_refresh_preserves_valid_evidence(self):
        self.start(acknowledge=True)
        first = C.execute_check(*self.args, "first", argv=self.argv, scope=["input"])
        (self.root / "marker").unlink()
        for name in ("AGENTS.md", "CLAUDE.md", "PIPELINE.md"):
            path = self.root / name
            old = path.read_bytes() if path.exists() else None
            path.write_text("new fixture private rule text")
            self.assertTrue(self.cli("policy")["stale"])
            with self.assertRaisesRegex(ValueError, "policy"):
                C.execute_check(*self.args, "blocked", argv=self.argv, scope=["input"], impact_covered=True)
            self.ack()
            if old is None:
                path.unlink()
            else:
                path.write_bytes(old)
            self.assertTrue(self.cli("policy")["stale"])
            self.ack()
        reused = C.execute_check(*self.args, "reused", argv=self.argv, scope=["input"],
                                 reuse_from=first["report"], impact_covered=True)
        self.assertTrue(reused["reused"])
        self.assertFalse((self.root / "marker").exists())
        self.assertEqual(self.state()["run_id"], self.args[1])

    def test_stale_policy_allows_existing_results_wait_pause_resume_and_abort(self):
        self.start(acknowledge=True)
        H.begin(*self.args, "host", **self.host)
        span = O.begin(*self.args, "verify", "observed-operation")
        (self.root / "AGENTS.md").write_text("changed rules")
        (self.root / "host.log").write_text("actual fixture return")
        H.finish(*self.args, "host", exit_code=1, log_path="host.log", host_stopped=True,
                 stop_evidence="fixture returned")
        O.end(*self.args, span, status="failed")
        self.cli("wait", "--receipt", self.receipt, "--reason", "input needed", "--action", "choose", "--verify", "resume")
        self.cli("continue", "--receipt", self.receipt, "--evidence", "fixture supplied input")
        R.finish(*self.args, "paused", executors_stopped=True, stop_evidence="fixture stopped")
        R.resume(*self.args, executors_stopped=True, stop_evidence="fixture still stopped")
        self.assertTrue(self.cli("policy")["stale"])
        R.finish(*self.args, "aborted", executors_stopped=True, stop_evidence="fixture stopped")
        self.assertEqual(R.status(self.root, self.args[1])["state"]["status"], "aborted")

    def test_legacy_upgrade_does_not_rewrite_history(self):
        state = R.start(self.root)
        args = self.root, state["run_id"], state["owner_token"]
        C.execute_check(*args, "legacy", argv=self.argv, scope=["input"])
        before = R.status(self.root)["state"]
        self.assertEqual(V.inspect(self.root, before)["mode"], "legacy_untracked")
        self.assertIsNone(V.inspect(self.root, before)["caller_acknowledged"])
        self.assertNotIn("run_policy", before)
        V.acknowledge(*args, V.snapshot(self.root)["revision"])
        after = R.status(self.root)["state"]
        self.assertEqual(after["history"], before["history"])
        self.assertEqual(after["artifacts"], before["artifacts"])
        self.assertGreaterEqual(after["run_policy"]["managed_since"], state["created_at"])

    def test_policy_status_are_readonly_and_audit_safe_old_snapshot_compatible(self):
        self.start(acknowledge=True)
        before = self.snapshot()
        self.cli("policy")
        self.cli("status")
        self.assertEqual(self.snapshot(), before)
        view = A.audit(self.root, self.args[1], live=True)
        self.assertNotIn("fixture private rule text", json.dumps(view))
        self.assertNotIn(self.args[2], json.dumps(view))
        document = A._build(self.root, self.state())
        self.assertTrue(document["adoption"]["caller_acknowledged"])
        A._validate_document(document)
        document.pop("adoption")
        A._validate_document(document)

    def test_fixed_skill_sources_and_missing_unsafe_rules_fail_closed(self):
        skill = self.root / "fixture-skill"
        for relative in V.RULES:
            dest = skill / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(V.SKILL_ROOT / relative, dest)
        alias = self.root / "installed-skill"
        alias.symlink_to(skill, target_is_directory=True)
        with patch.object(V, "SKILL_ROOT", alias):
            state = R.start(self.root, managed=True)
            args = self.root, state["run_id"], state["owner_token"]
            V.acknowledge(*args, V.snapshot(self.root)["revision"])
            rule = skill / "references/evidence.md"
            original = rule.read_bytes()
            rule.write_bytes(original + b"\nfixture rule change")
            with self.assertRaisesRegex(ValueError, "policy"):
                C.execute_check(*args, "blocked", argv=self.argv, scope=["input"])
            rule.unlink()
            with self.assertRaisesRegex(ValueError, "policy"):
                V.acknowledge(*args, state["run_policy"]["revision"])
            rule.symlink_to(skill / "SKILL.md")
            with self.assertRaisesRegex(ValueError, "policy"):
                V.snapshot(self.root)
        self.assertFalse((self.root / "marker").exists())

    def test_missing_required_project_rule_and_managed_completed_guard(self):
        (self.root / "PIPELINE.md").unlink()
        self.cli("start", expected=2)
        self.assertIsNone(R.status(self.root)["state"])
        (self.root / "PIPELINE.md").write_text("fixture contract")
        self.start(acknowledge=True)
        (self.root / "PIPELINE.md").unlink()
        with self.assertRaisesRegex(ValueError, "policy"):
            R.finish(*self.args, "completed", executors_stopped=True, stop_evidence="fixture stopped")
        (self.root / "PIPELINE.md").write_text("fixture contract")
        self.ack()
        final = R.finish(*self.args, "completed", executors_stopped=True, stop_evidence="fixture stopped")
        self.assertEqual(final["status"], "completed")


if __name__ == "__main__":
    unittest.main()
