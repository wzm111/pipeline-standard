"""Managed acceptance behavior with real fixture checks and no external work."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
sys.path.insert(0, str(SCRIPTS))
import check_plan as P
import run_checks as C
import run_evidence as E
import run_policy as V
import run_state as R
import run_tasks as T


class ManagedQaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "PIPELINE.md").write_text("fixture contract")
        (self.root / "input").write_text("fixture source")
        state = R.start(self.root, managed=True)
        self.args = self.root, state["run_id"], state["owner_token"]
        V.acknowledge(*self.args, V.snapshot(self.root)["revision"])
        self.receipt = self.root / "receipt.json"
        R._atomic(self.receipt, {"project_root": str(self.root), **{k: state[k] for k in ("schema_version", "run_id", "owner_token")}})
        self.spec = {"argv": [sys.executable, "-B", "-c", "print('actual fixture check')"],
                     "scope": ["input"], "coverage": ["unit"], "write_paths": []}

    def check(self, name="one", **changes):
        return C.execute_check(*self.args, name, **(self.spec | changes))

    def qa(self, name, payload):
        return E.write_report(*self.args, "qa", name, payload)

    def single(self, result, name="qa"):
        return self.qa(name, {"gate": "PASS", "checks": [result["report"]], "required_checks": ["unit"]})

    def finish(self):
        return R.finish(*self.args, "completed", executors_stopped=True, stop_evidence="fixture stopped")

    def payload(self, ref):
        return E.read_report(self.root, self.args[1], ref)["payload"]

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "pipeline_cli.py"), *args],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        self.assertFalse(result.stderr)
        self.assertNotIn(self.args[2], result.stdout)
        return json.loads(result.stdout)

    def test_plain_pass_names_missing_and_wrong_refs_never_create_acceptance(self):
        checked = self.check()
        before = R.status(self.root)["state"]
        for index, payload in enumerate([
                {"gate": "PASS"}, {"gate": "PASS", "required_checks": ["unit"]},
                {"gate": "PASS", "checks": ["one"], "required_checks": ["unit"]},
                {"gate": "PASS", "checks": [checked["report"]], "required_checks": ["fake"]},
                {"gate": "PASS", "plan_ref": "fake"}]):
            with self.assertRaises(ValueError):
                self.qa(f"bad{index}", payload)
            self.assertEqual(R.status(self.root)["state"], before)
        self.cli("report", "--receipt", str(self.receipt), "--kind", "qa", "--name", "fake",
                 "--json", '{"gate":"PASS"}', expected=2)
        with self.assertRaises(ValueError):
            self.finish()
        self.assertIsNotNone(R.status(self.root)["active"])

    def test_single_actual_ref_shared_finish_and_exploration_ignored(self):
        self.check("search", argv=[sys.executable, "-B", "-c", "raise SystemExit(1)"], intent="search", coverage=["explore"])
        checked = self.check()
        ref = self.single(checked)
        self.assertEqual(self.payload(ref)["machine_validation"]["mode"], "single")
        final = self.finish()
        self.assertEqual(final["status"], "completed")
        self.assertTrue(final["delivery_status"]["sealed"])

    def test_later_failure_cannot_revive_old_pass_via_report_or_shared_finish(self):
        argv = [sys.executable, "-B", "-c", "import os; raise SystemExit(int(os.environ['QA_FIXTURE_EXIT']))"]
        with patch.dict(os.environ, {"QA_FIXTURE_EXIT": "0"}):
            checked = self.check(argv=argv)
            self.single(checked)
            with patch.dict(os.environ, {"QA_FIXTURE_EXIT": "1"}):
                self.assertEqual(self.check("failed", argv=argv)["exit_code"], 1)
            with self.assertRaises(ValueError):
                self.single(checked, "revive")
            with self.assertRaises(ValueError):
                self.finish()
        self.assertIsNotNone(R.status(self.root)["active"])

    def test_log_inputs_and_current_environment_drift_block_completion(self):
        checked = self.check()
        self.single(checked)
        proof = self.payload(checked["report"])
        log = self.root / proof["log_path"]
        original = log.read_bytes()
        log.write_text("tampered fixture log")
        with self.assertRaises(ValueError):
            self.finish()
        log.write_bytes(original)
        (self.root / "input").write_text("changed fixture input")
        with self.assertRaises(ValueError):
            self.finish()
        (self.root / "input").write_text("fixture source")
        with patch.dict(os.environ, {"NODE_ENV": "changed-fixture-environment"}):
            with self.assertRaises(ValueError):
                self.finish()
        self.assertEqual(self.finish()["status"], "completed")

    def test_plan_canonicalizes_forged_pass_and_keeps_required_union(self):
        other = self.spec | {"coverage": ["other"], "argv": [sys.executable, "-B", "-c", "print('other')"]}
        plan = P.save_plan(*self.args, "both", {"unit": self.spec, "other": other})
        P.run_planned(*self.args, plan, "unit", "unit")
        fake = self.qa("fake", {"gate": "PASS", "plan_ref": plan, "required_checks": ["unit"]})
        self.assertEqual(self.payload(fake)["gate"], "CONCERNS")
        self.assertIn("other", self.payload(fake)["missing_checks"])
        actual = self.cli("report", "--receipt", str(self.receipt), "--kind", "qa", "--name", "cli-fake",
                          "--json", json.dumps({"gate": "PASS", "plan_ref": plan}), expected=1)
        self.assertEqual(actual["gate"], "CONCERNS")
        self.assertIn("other", actual["missing_checks"])
        small = P.save_plan(*self.args, "smaller", {"unit": self.spec})
        P.run_planned(*self.args, small, "unit", "unit-again", impact_covered=True)
        narrowed = P.qa_from_plan(*self.args, small, "narrowed")
        self.assertEqual(self.payload(narrowed)["gate"], "CONCERNS")
        self.assertEqual(self.payload(narrowed)["required_checks"], ["other", "unit"])
        P.run_planned(*self.args, plan, "other", "other")
        final = P.qa_from_plan(*self.args, plan, "final")
        self.assertEqual(self.payload(final)["gate"], "PASS")
        diagnostic = self.cli("report", "--receipt", str(self.receipt), "--kind", "qa", "--name", "business-failed",
                              "--json", json.dumps({"gate": "FAIL", "plan_ref": plan}))
        self.assertEqual(diagnostic["gate"], "FAIL")
        with self.assertRaises(ValueError):
            self.finish()
        P.qa_from_plan(*self.args, plan, "revalidated")
        self.assertEqual(self.finish()["status"], "completed")

    def test_multiple_identities_or_categories_require_plan(self):
        one = self.check()
        self.check("two", argv=[sys.executable, "-B", "-c", "print('distinct check')"])
        with self.assertRaisesRegex(ValueError, "plan"):
            self.single(one)
        T.start(*self.args, "next", "separate task", ["observed"])
        two = self.check("multi-label", coverage=["unit", "other"])
        with self.assertRaisesRegex(ValueError, "plan"):
            self.single(two)

    def test_exploration_only_has_no_qa_or_delivered_product(self):
        self.check("search", intent="search", coverage=["explore"])
        final = self.finish()
        self.assertEqual(final["status"], "completed")
        self.assertFalse(final["delivery_status"]["sealed"])

    def test_task_end_is_not_acceptance_and_old_qa_does_not_accept_new_task(self):
        T.start(*self.args, "first", "first goal", ["first observed"])
        checked = self.check()
        self.single(checked)
        T.end(*self.args, "first")
        T.start(*self.args, "next", "next goal", ["next observed"])
        T.end(*self.args, "next")
        with self.assertRaises(ValueError):
            self.finish()
        with self.assertRaises(ValueError):
            self.single(checked, "old-ref")
        reused = self.check("explicit-reuse", impact_covered=True)
        self.assertTrue(reused["reused"])
        self.single(reused, "current")
        self.assertEqual(self.finish()["status"], "completed")

    def test_old_task_plan_requirements_do_not_pollute_new_task(self):
        T.start(*self.args, "old", "old goal", ["old observed"])
        plan = P.save_plan(*self.args, "old-plan", {"old": self.spec | {"coverage": ["old"]}})
        P.run_planned(*self.args, plan, "old", "old-run")
        P.qa_from_plan(*self.args, plan, "old-qa")
        T.end(*self.args, "old")
        T.start(*self.args, "new", "new goal", ["new observed"])
        with self.assertRaises(ValueError):
            self.qa("old-plan-reused", {"gate": "PASS", "plan_ref": plan})
        checked = self.check("new-check")
        self.single(checked, "new-qa")
        T.end(*self.args, "new")
        self.assertEqual(self.finish()["status"], "completed")

    def test_legacy_cli_new_task_needs_explicit_adoption_but_old_retry_is_readonly(self):
        R.finish(*self.args, "aborted", executors_stopped=True, stop_evidence="fixture stopped")
        state = R.start(self.root)
        self.args = self.root, state["run_id"], state["owner_token"]
        R._atomic(self.receipt, {"project_root": str(self.root), **{k: state[k] for k in ("schema_version", "run_id", "owner_token")}})
        T.start(*self.args, "old", "old goal", ["old observed"])
        args = ("task-start", "--receipt", str(self.receipt), "--task-id", "old", "--goal", "old goal", "--accept", "old observed")
        before = R.status(self.root)["state"]
        self.cli(*args)
        self.assertEqual(R.status(self.root)["state"], before)
        T.end(*self.args, "old")
        args = ("task-start", "--receipt", str(self.receipt), "--task-id", "new", "--goal", "new goal", "--accept", "new observed")
        self.cli(*args, expected=2)
        revision = self.cli("policy")["revision"]
        self.cli("policy-ack", "--receipt", str(self.receipt), "--revision", revision)
        self.cli(*args)
        self.assertEqual(T.status(self.root)["current_task_id"], "new")

    def test_full_per_check_dependencies_only_reexecute_impacted_checks(self):
        for name in ("a-source", "a-fixture", "b-source", "b-guide", "shared-config", "lockfile"):
            (self.root / name).write_text("v1")
        checks = {}
        for key, unique in (("a", ["a-source", "a-fixture"]), ("b", ["b-source", "b-guide"])):
            dependencies = unique + ["shared-config", "lockfile"]
            checks[key] = self.spec | {"scope": unique + ["shared-config", "lockfile"], "coverage": [key],
                "argv": [sys.executable, "-B", "-c", f"from pathlib import Path; print('|'.join(Path(name).read_text() for name in {dependencies!r})); p=Path('{key}-counter'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"]}
        plan = P.save_plan(*self.args, "deps", checks)
        P.verify_plan(*self.args, plan, "first")
        (self.root / "b-guide").write_text("v2")
        second = P.verify_plan(*self.args, plan, "unique-change")
        self.assertEqual([item["reused"] for item in second["results"]], [True, False])
        self.assertIn("v2", (self.root / self.payload(second["results"][1]["report"])["log_path"]).read_text())
        for index, shared in enumerate(("shared-config", "lockfile")):
            (self.root / shared).write_text("v2")
            result = P.verify_plan(*self.args, plan, f"shared{index}")
            self.assertEqual([item["reused"] for item in result["results"]], [False, False])
        self.assertEqual((self.root / "a-counter").read_text(), "3")
        self.assertEqual((self.root / "b-counter").read_text(), "4")


if __name__ == "__main__":
    unittest.main()
