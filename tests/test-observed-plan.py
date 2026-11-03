"""Real host tickets join the original multi-check plan and managed QA gates."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
sys.path.insert(0, str(SCRIPTS))
import check_plan as P
import observed_checks as O
import run_evidence as E
import run_policy as V
import run_state as R
import run_tasks as T
import release_checks as F


class ObservedPlanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "PIPELINE.md").write_text("isolated host fixture")
        (self.root / "input").write_text("source")
        state = R.start(self.root, managed=True)
        self.args = self.root, state["run_id"], state["owner_token"]
        V.acknowledge(*self.args, V.snapshot(self.root)["revision"])

    def observed(self, name, *, command=None, code=0, intent="test", environment=None, coverage=None):
        coverage = coverage or [name]
        ticket = O.begin(*self.args, name, command=command or f"browser.fixture.{name}", scope=["input"],
                         environment=environment or {"host": "fixture-v1"}, coverage=coverage, write_paths=[], intent=intent)
        # A real local host action supplies the observed result; no model/network.
        content = (self.root / "input").read_text()
        (self.root / f"{name}.log").write_text(content)
        ref = O.finish(*self.args, name, exit_code=code, log_path=f"{name}.log",
                       host_stopped=True, stop_evidence="fixture host read returned")
        return {"observed_ref": ref, "scope": ["input"], "coverage": coverage,
                "environment": ticket["declaration"]["environment"], "intent": intent}

    def plan(self, checks, name="plan"):
        return P.save_plan(*self.args, name, checks)

    def test_native_multi_check_plan_qa_run_and_verify_do_not_launch_or_invent_time(self):
        checks = {"first": self.observed("first"), "second": self.observed("second")}
        plan = self.plan(checks)
        self.assertEqual(P.inspect_plan(*self.args, plan)["gate"], "PASS")
        before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()}
        self.assertTrue(P.preflight_plan(*self.args, plan, "verify")["ready"])
        self.assertEqual(before, {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.rglob("*") if p.is_file()})
        with patch.object(P.C, "execute_check", side_effect=AssertionError("must not launch")):
            result = P.verify_plan(*self.args, plan, "verify")
        self.assertEqual(result["gate"], "PASS")
        self.assertEqual(len(result["results"]), 2)
        for item in result["results"]:
            self.assertTrue(item["reused"])
            self.assertEqual(item["report"], checks[item["check_id"]]["observed_ref"])
        state = R.status(self.root)["state"]
        self.assertEqual(len(state["observed_checks"]), 2)
        self.assertEqual(len([p for p in state["artifacts"] if "/artifacts/check/" in p]), 2)
        R.finish(*self.args, "completed", executors_stopped=True, stop_evidence="fixture stopped")

    def test_mixed_plan_preserves_native_and_argv_coverage(self):
        host = self.observed("host")
        plan = self.plan({"host": host, "unit": {"argv": [sys.executable, "-c", "print('unit')"],
                          "scope": ["input"], "coverage": ["unit"], "write_paths": []}})
        result = P.verify_plan(*self.args, plan, "mixed")
        self.assertEqual(result["gate"], "PASS")
        self.assertEqual(result["required_checks"], ["host", "unit"])
        self.assertEqual([r["reused"] for r in result["results"]], [True, False])

    def test_failed_and_later_failed_host_sources_block_heavy_execution(self):
        host = self.observed("host")
        plan = self.plan({"host": host, "heavy": {"argv": [sys.executable, "-c", "raise Exception('never')"],
                          "scope": ["input"], "coverage": ["heavy"]}})
        self.observed("later", command="browser.fixture.host", code=1)
        with patch.object(P.C, "execute_check", side_effect=AssertionError("must not launch")):
            result = P.verify_plan(*self.args, plan, "failed")
        self.assertEqual(result["gate"], "CONCERNS")
        self.assertIn("host", result["missing_checks"])
        self.assertIn("heavy", result["missing_checks"])
        self.assertFalse(result["preflight"]["ready"])
        failed = self.observed("failed", code=1)
        failed_plan = self.plan({"failed": failed}, name="failed-plan")
        with self.assertRaises(ValueError):
            P.run_planned(*self.args, failed_plan, "failed", "no-bind")

    def test_drift_scope_coverage_environment_and_log_are_not_accepted(self):
        host = self.observed("host")
        for name, change in (("scope", {"scope": ["different"]}), ("coverage", {"coverage": ["different"]}),
                             ("environment", {"environment": {"host_identity_sha256": "0" * 64}})):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.plan({"host": {**host, **change}}, name=name)
        plan = self.plan({"host": host})
        (self.root / "host.log").write_text("changed")
        self.assertNotEqual(P.inspect_plan(*self.args, plan)["gate"], "PASS")
        (self.root / "host.log").write_text("source")
        (self.root / "input").write_text("changed source")
        self.assertNotEqual(P.inspect_plan(*self.args, plan)["gate"], "PASS")

    def test_old_task_requires_explicit_validated_reuse_and_binding_cannot_launder_it(self):
        host = self.observed("host")
        T.start(*self.args, "new-task", "new goal", ["current acceptance"])
        plan = self.plan({"host": host})
        E.write_report(*self.args, "planned_check", "metadata-binding",
                       {"plan_ref": plan, "plan_sha256": P._digest(self.root, plan), "check_id": "host",
                        "result": {"report": host["observed_ref"], "exit_code": 0, "reused": True}})
        self.assertFalse(P.preflight_plan(*self.args, plan, "before")["ready"])
        with self.assertRaises(ValueError):
            P.run_planned(*self.args, plan, "host", "old")
        result = P.run_planned(*self.args, plan, "host", "explicit", impact_covered=True)
        self.assertTrue(result["reused"])
        self.assertEqual(P.verify_plan(*self.args, plan, "after")["gate"], "PASS")

    def test_real_corrected_observation_keeps_failed_source_and_stable_logical_identity(self):
        failed = self.observed("failed", command="browser.confirm", code=1, coverage=["confirm"])
        self.plan({"confirm": failed}, name="failed-plan")
        corrected = self.observed("corrected", command="browser.confirm", coverage=["confirm"])
        plan = self.plan({"confirm": corrected}, name="corrected-plan")
        self.assertEqual(P.verify_plan(*self.args, plan, "corrected-qa")["gate"], "PASS")
        original = E.read_report(self.root, self.args[1], failed["observed_ref"], kind="check")["payload"]
        self.assertEqual(original["exit_code"], 1)

    def test_metadata_baseline_visual_and_execution_fields_cannot_supply_native_checks(self):
        for intent in ("baseline", "visual"):
            source = self.observed(intent, intent=intent)
            with self.assertRaises(ValueError):
                self.plan({"host": {**source, "intent": "test"}}, name=intent)
        host = self.observed("host")
        for extra in ({"argv": ["fake"]}, {"timeout": 5}, {"cwd": "."}):
            with self.assertRaises(ValueError):
                self.plan({"host": {**host, **extra}})
        metadata = E.write_report(*self.args, "metadata", "fake", {"observation_kind": "host_tool"})
        with self.assertRaises(ValueError):
            self.plan({"host": {**host, "observed_ref": metadata}})

    def test_actual_cli_native_only_plan_qa_and_verify_use_original_sources(self):
        checks = {"first": self.observed("first"), "second": self.observed("second")}
        receipt = self.root / "receipt.json"
        R._atomic(receipt, {"schema_version": R.status(self.root)["state"]["schema_version"],
                           "project_root": str(self.root), "run_id": self.args[1], "owner_token": self.args[2]})

        def cli(*args):
            result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "pipeline_cli.py"), *args,
                                     "--receipt", str(receipt)], cwd=self.root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)

        plan = cli("plan", "--name", "cli-plan", "--json", json.dumps({"checks": checks}))["plan"]
        self.assertTrue(cli("plan-preflight", "--plan", plan, "--name", "preflight")["ready"])
        cli("qa", "--plan", plan, "--name", "direct-qa")
        self.assertEqual(cli("verify", "--plan", plan, "--name", "verify")["gate"], "PASS")

    def recovered_source(self, **changes):
        failed = self.observed("failed", command="browser.confirm", code=1, coverage=["confirm"])
        before = self.plan({"confirm": failed}, name="failed-plan")
        corrected = self.observed("corrected", command="browser.confirm", coverage=["confirm"], **changes)
        after = self.plan({"confirm": corrected}, name="corrected-plan")
        return before, after

    def release(self):
        values = {"argv": [sys.executable, "-c", "print('release fixture')"], "scope": ["input"], "write_paths": []}
        for name, phase in (("preflight", "preflight"), ("compat", "compatibility")):
            F.execute(*self.args, name, phase=phase, **values)
        F.execute(*self.args, "build", phase="build", scope=["input"], artifact_paths=["artifact"],
                  argv=[sys.executable, "-c", "from pathlib import Path; Path('artifact').write_text('v1')"])
        F.execute(*self.args, "activate", phase="activate", **values)
        F.execute(*self.args, "smoke", phase="smoke", coverage=["release_identity"], **values)
        return F.closeout(*self.args)

    def test_equivalent_corrected_plan_allows_real_release_and_keeps_old_failed_ref(self):
        before, after = self.recovered_source()
        self.assertEqual(P.verify_plan(*self.args, after, "source-qa")["gate"], "PASS")
        result = self.release()
        self.assertTrue(result["report"])
        self.assertEqual(P.inspect_plan(*self.args, before)["gate"], "CONCERNS")
        self.assertIn(before, R.status(self.root)["state"]["check_plans"])

    def test_recovery_different_host_environment_cannot_replace_old_plan_for_release(self):
        self.recovered_source(environment={"host": "different"})
        with self.assertRaisesRegex(ValueError, "source plan"):
            self.release()

    def test_recovery_cannot_drop_an_old_check_for_release(self):
        failed = self.observed("failed", command="browser.confirm", code=1, coverage=["confirm"])
        ready = self.observed("ready")
        self.plan({"ready": ready, "confirm": {**failed, "depends_on": ["ready"]}}, name="old")
        corrected = self.observed("corrected", command="browser.confirm", coverage=["confirm"])
        self.plan({"confirm": corrected}, name="narrow")
        with self.assertRaisesRegex(ValueError, "source plan"):
            self.release()

    def test_recovery_cannot_remove_dependency_even_when_all_checks_pass(self):
        ready = self.observed("ready")
        failed = self.observed("failed", command="browser.confirm", code=1, coverage=["confirm"])
        self.plan({"ready": ready, "confirm": {**failed, "depends_on": ["ready"]}}, name="old")
        corrected = self.observed("corrected", command="browser.confirm", coverage=["confirm"])
        after = self.plan({"ready": ready, "confirm": corrected}, name="dependency-removed")
        self.assertEqual(P.verify_plan(*self.args, after, "source-qa")["gate"], "PASS")
        with self.assertRaisesRegex(ValueError, "source plan"):
            self.release()

    def test_recovery_narrower_scope_is_not_equivalent_for_release(self):
        (self.root / "extra").write_text("required dependency")
        failed = self.observed("failed", command="browser.confirm", code=1, coverage=["confirm"])
        # A complete old declaration must retain its dependency scope; use a
        # real second observation rather than editing the old evidence.
        ticket = O.begin(*self.args, "wide", command="browser.confirm", scope=["input", "extra"],
                         environment={"host": "fixture-v1"}, coverage=["confirm"], write_paths=[])
        (self.root / "wide.log").write_text("failed actual wide check")
        ref = O.finish(*self.args, "wide", exit_code=1, log_path="wide.log", host_stopped=True, stop_evidence="fixture returned")
        self.plan({"confirm": {**failed, "observed_ref": ref, "scope": ["input", "extra"],
                              "environment": ticket["declaration"]["environment"]}}, name="wide-plan")
        corrected = self.observed("corrected", command="browser.confirm", coverage=["confirm"])
        self.plan({"confirm": corrected}, name="narrow-plan")
        with self.assertRaisesRegex(ValueError, "source plan"):
            self.release()

    def test_recovery_new_failure_still_blocks_release(self):
        self.recovered_source()
        self.observed("later-fail", command="browser.confirm", coverage=["confirm"], code=1)
        with self.assertRaisesRegex(ValueError, "source plan"):
            self.release()

    def test_recovery_does_not_erase_unresolved_business_concerns_on_old_plan(self):
        before, after = self.recovered_source()
        E.write_report(*self.args, "qa", "business-concerns", {"plan_ref": before, "gate": "CONCERNS",
                       "summary": "business confirmation still unaccepted"})
        self.assertEqual(P.verify_plan(*self.args, after, "new-source-qa")["gate"], "PASS")
        with self.assertRaisesRegex(ValueError, "source QA"):
            self.release()

    def test_old_machine_concerns_are_conservatively_retained_without_an_explicit_resolution(self):
        failed = self.observed("failed", command="browser.confirm", code=1, coverage=["confirm"])
        before = self.plan({"confirm": failed}, name="failed-plan")
        self.assertEqual(P.verify_plan(*self.args, before, "old-machine-qa")["gate"], "CONCERNS")
        corrected = self.observed("corrected", command="browser.confirm", coverage=["confirm"])
        after = self.plan({"confirm": corrected}, name="corrected-plan")
        self.assertEqual(P.verify_plan(*self.args, after, "new-machine-qa")["gate"], "PASS")
        with self.assertRaisesRegex(ValueError, "source QA"):
            self.release()


if __name__ == "__main__":
    unittest.main()
