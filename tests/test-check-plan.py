"""Check plans bind real execution and current QA evidence in isolated runs."""
import copy
import json
import os
import shlex
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import check_plan as P
import run_checks as C
import run_evidence as E
import run_state as R


class PlanTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.state = R.start(self.root, {"stage": "implementation"})
        self.run, self.owner = self.state["run_id"], self.state["owner_token"]
        (self.root / "input").write_text("stable")
        self.check = {"argv": [sys.executable, "-B", "-c", "print('checked')"],
                      "scope": ["input"], "coverage": ["unit", "regression"], "write_paths": []}

    def save(self, checks=None, required=None, name="plan"):
        return P.save_plan(self.root, self.run, self.owner, name,
                           checks or {"unit": self.check}, required)

    def execute(self, plan, name="first", key="unit", **kwargs):
        return P.run_planned(self.root, self.run, self.owner, plan, key, name, **kwargs)

    def qa(self, plan, name="qa"):
        ref = P.qa_from_plan(self.root, self.run, self.owner, plan, name)
        return E.read_report(self.root, self.run, ref, kind="qa")["payload"]

    def test_one_plan_defines_real_coverage_required_and_reuse(self):
        self.check["argv"][-1] = "from pathlib import Path; p=Path('count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"
        plan = self.save(required=["unit"])
        first = self.execute(plan)
        second = self.execute(plan, "second", impact_covered=True)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual((self.root / "count").read_text(), "1")
        qa = self.qa(plan)
        self.assertEqual(qa["gate"], "PASS")
        self.assertEqual(qa["required_checks"], ["regression", "unit"])
        proof = E.read_report(self.root, self.run, first["report"])["payload"]
        self.assertEqual(proof["coverage"], qa["required_checks"])

    def test_unexecuted_and_partial_shared_coverage_are_concerns(self):
        plan = self.save({"unit": self.check, "other": copy.deepcopy(self.check)})
        self.assertEqual(self.qa(plan)["gate"], "CONCERNS")
        self.execute(plan)
        qa = self.qa(plan, "partial")
        self.assertEqual(qa["gate"], "CONCERNS")
        self.assertEqual(qa["check_failures"], {"other": "not_executed"})
        self.execute(plan, "other", key="other")
        self.assertEqual(self.qa(plan, "complete")["gate"], "PASS")

    def test_latest_failure_never_revives_pass(self):
        self.check["argv"][-1] = "from pathlib import Path; raise SystemExit(int(Path('failure').exists()))"
        plan = self.save()
        self.execute(plan)
        (self.root / "failure").touch()
        failed = self.execute(plan, "failed")
        self.assertEqual(failed["exit_code"], 1)
        self.assertEqual(self.qa(plan)["gate"], "CONCERNS")
        retry = self.execute(plan, "retry", impact_covered=True)
        self.assertFalse(retry["reused"])
        self.assertEqual(retry["exit_code"], 1)

    def test_later_unplanned_failure_blocks_old_bound_pass(self):
        self.check["argv"][-1] = "from pathlib import Path; raise SystemExit(int(Path('failure').exists()))"
        plan = self.save()
        self.execute(plan)
        (self.root / "failure").touch()
        C.execute_check(self.root, self.run, self.owner, "later", argv=self.check["argv"],
                        scope=self.check["scope"], coverage=["other"])
        self.assertEqual(self.qa(plan)["gate"], "CONCERNS")
        retried = self.execute(plan, "retry", impact_covered=True)
        self.assertFalse(retried["reused"])
        self.assertEqual(retried["exit_code"], 1)

    def test_new_legacy_manual_failure_blocks_planned_and_automatic_reuse(self):
        plan = self.save()
        self.execute(plan)
        (self.root / "manual-failure.log").write_text("observed failure")
        E.record_check(self.root, self.run, self.owner, "manual-failure",
                       command=shlex.join(self.check["argv"]), scope=self.check["scope"],
                       environment={"runtime": "dedicated-tool"}, exit_code=1,
                       log_path="manual-failure.log", coverage=self.check["coverage"],
                       started_fingerprint=E.fingerprint(self.root, self.check["scope"]))
        self.assertEqual(self.qa(plan)["gate"], "CONCERNS")
        retried = C.execute_check(self.root, self.run, self.owner, "automatic-retry",
                                  argv=self.check["argv"], scope=self.check["scope"],
                                  coverage=self.check["coverage"], impact_covered=True)
        self.assertFalse(retried["reused"])
        self.assertEqual(retried["reuse_decision"]["reason"], "check_not_passed")
        E.record_check(self.root, self.run, self.owner, "manual-failure-again",
                       command=shlex.join(self.check["argv"]), scope=self.check["scope"],
                       environment={"runtime": "dedicated-tool"}, exit_code=1,
                       log_path="manual-failure.log", coverage=self.check["coverage"],
                       started_fingerprint=E.fingerprint(self.root, self.check["scope"]))
        self.assertFalse(self.execute(plan, "planned-retry", impact_covered=True)["reused"])

    def test_changed_input_log_or_environment_blocks_pass(self):
        plan = self.save()
        result = self.execute(plan)
        (self.root / "input").write_text("changed")
        self.assertEqual(self.qa(plan, "inputchanged")["gate"], "CONCERNS")
        (self.root / "input").write_text("stable")
        with patch.dict(os.environ, {"PLAN_TEST_CONTEXT": "changed"}):
            changed = self.qa(plan, "envchanged")
            self.assertEqual(changed["gate"], "CONCERNS")
            self.assertEqual(changed["environment_diagnostics"]["unit"]["status"], "known")
            self.assertTrue(changed["environment_diagnostics"]["unit"]["other_environment_changed"])
            self.assertNotIn("PLAN_TEST_CONTEXT", json.dumps(changed["environment_diagnostics"]))
        proof = E.read_report(self.root, self.run, result["report"])["payload"]
        (self.root / proof["log_path"]).write_text("edited")
        self.assertEqual(self.qa(plan, "logchanged")["gate"], "CONCERNS")

    def test_changed_unregistered_cross_run_and_wrong_owner_plans_rejected(self):
        plan = self.save()
        with self.assertRaises(ValueError):
            P.read_plan(self.root, self.run, "wrong", plan)
        unregistered = E.write_report(self.root, self.run, self.owner, "check_plan", "forged",
                                      {"checks": {"unit": self.check}, "required_checks": ["unit"]})
        with self.assertRaises(ValueError):
            P.read_plan(self.root, self.run, self.owner, unregistered)
        with self.assertRaises((ValueError, FileNotFoundError)):
            P.read_plan(self.root, "a" * 32, self.owner, plan)
        data = json.loads((self.root / plan).read_text())
        data["payload"]["required_checks"] = ["unit"]
        (self.root / plan).write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.qa(plan)

    def test_invalid_fields_paths_coverage_required_credentials_rejected(self):
        changes = [{"unknown": True}, {"coverage": []}, {"scope": ["../outside"]},
                   {"cwd": "../outside"}, {"environment": {"owner_token": "credential"}},
                   {"argv": ["echo", self.owner]}, {"timeout": 0}]
        for change in changes:
            with self.subTest(fields=list(change)):
                with self.assertRaises(ValueError):
                    self.save({"unit": {**self.check, **change}})
        with self.assertRaises(ValueError):
            self.save(required=["uncovered"])
        with self.assertRaises(ValueError):
            P.save_plan(self.root, self.run, self.owner, "empty", {})

    def test_inherited_required_is_not_weakened_or_silently_passed(self):
        with R._write_guard(self.root) as base:
            path, state = R._load_owned(base, self.run, self.owner)
            state["inherited_required_checks"] = ["integration"]
            R._atomic(path, state)
        plan = self.save(required=[])
        self.execute(plan)
        qa = self.qa(plan)
        self.assertEqual(qa["gate"], "CONCERNS")
        self.assertEqual(qa["required_checks"], ["integration", "regression", "unit"])
        self.assertEqual(qa["missing_checks"], ["integration"])

    def test_declared_cwd_executes_and_qa_validates_same_directory(self):
        (self.root / "sub").mkdir()
        self.check.update(cwd="sub")
        self.check["argv"][-1] = "from pathlib import Path; Path('observed').write_text(Path.cwd().name)"
        plan = self.save()
        self.execute(plan)
        self.assertEqual((self.root / "sub/observed").read_text(), "sub")
        self.assertEqual(self.qa(plan)["gate"], "PASS")

    def test_equivalent_cwd_spellings_are_normalized_before_execution(self):
        (self.root / "sub").mkdir()
        for index, cwd in enumerate(("./sub", "sub/")):
            self.check["cwd"] = cwd
            plan = self.save(name=f"plan{index}")
            payload = P.read_plan(self.root, self.run, self.owner, plan)
            self.assertEqual(payload["checks"]["unit"]["cwd"], "sub")
            self.execute(plan, name=f"check{index}")
            self.assertEqual(self.qa(plan, name=f"qa{index}")["gate"], "PASS")

    def adopt_manual(self, required=None):
        (self.root / "PIPELINE.md").write_text("fixture contract")
        log = "manual.log"
        (self.root / log).write_text("observed success")
        E.record_check(self.root, self.run, self.owner, "manual", command="dedicated-tool unit",
                       scope=["input"], environment={"runtime": "caller-confirmed"}, exit_code=0,
                       log_path=log, started_fingerprint=E.fingerprint(self.root, ["input"]), coverage=["unit"])
        qa = {"gate": "PASS"}
        if required is not None:
            qa["required_checks"] = required
        E.write_report(self.root, self.run, self.owner, "qa", "approved", qa)
        finished = R.finish(self.root, self.run, self.owner, "completed", executors_stopped=True,
                            stop_evidence="fixture tool finished")
        self.assertTrue(finished["delivery_status"]["sealed"])
        self.state = R.start(self.root, {"delivery_from": "latest"})
        self.assertTrue(self.state["delivery_status"]["adopted"])
        self.run, self.owner = self.state["run_id"], self.state["owner_token"]

    def test_manual_evidence_without_argv_remains_valid_after_real_delivery(self):
        self.adopt_manual(required=["unit"])
        self.assertIn("unit", self.state["delivery_status"]["coverage"]["covered"])
        self.check["coverage"] = ["regression"]
        plan = self.save()
        self.execute(plan)
        self.assertEqual(self.qa(plan)["gate"], "PASS")
        (self.root / "manual.log").write_text("changed log")
        self.assertEqual(self.qa(plan, "changedlog")["gate"], "CONCERNS")

    def test_legacy_qa_without_required_checks_adopts_and_plans_without_crashing(self):
        self.adopt_manual()
        self.assertIsNone(self.state.get("inherited_required_checks"))
        plan = self.save()
        self.execute(plan)
        self.assertEqual(self.qa(plan)["gate"], "PASS")

    def test_new_failure_blocks_inherited_manual_pass_with_legacy_identity(self):
        self.adopt_manual(required=["unit"])
        self.check["coverage"] = ["regression"]
        plan = self.save()
        self.execute(plan)
        self.assertEqual(self.qa(plan)["gate"], "PASS")
        C.execute_check(self.root, self.run, self.owner, "missing-tool",
                        argv=["dedicated-tool", "unit"], scope=["input"], coverage=["unit"])
        self.assertEqual(self.qa(plan, "afterfailure")["missing_checks"], ["unit"])

    def test_new_plan_and_generic_qa_cannot_erase_declared_requirements(self):
        (self.root / "PIPELINE.md").write_text("fixture contract")
        plan = self.save()
        narrower = copy.deepcopy(self.check)
        narrower["coverage"] = ["metadata"]
        replacement = self.save({"metadata": narrower}, name="narrower")
        self.execute(replacement, key="metadata")
        qa = self.qa(replacement)
        self.assertEqual(qa["gate"], "CONCERNS")
        self.assertEqual(qa["missing_checks"], ["regression", "unit"])
        E.write_report(self.root, self.run, self.owner, "qa", "manualoverride", {"gate": "PASS"})
        finished = R.finish(self.root, self.run, self.owner, "completed", executors_stopped=True,
                            stop_evidence="fixture processes stopped")
        self.assertFalse(finished["delivery_status"]["coverage"]["complete"])
        self.assertEqual(finished["delivery_status"]["coverage"]["missing"], ["regression", "unit"])

    def test_legitimate_secret_names_and_relative_command_arguments_are_preserved(self):
        argv = [sys.executable, "-B", "tests/test_secret_validation.py",
                "../../node_modules/tool", "--authorization-fixture"]
        self.check["argv"] = argv
        with patch.object(C, "execute_check", side_effect=AssertionError("planning must not execute")):
            plan = self.save()
            payload = P.read_plan(self.root, self.run, self.owner, plan)
            self.assertEqual(payload["checks"]["unit"]["argv"], argv)
            self.assertEqual(self.qa(plan)["gate"], "CONCERNS")

    def test_read_plan_does_not_rehash_inactive_check_inputs(self):
        plan = self.save({f"case{i}": copy.deepcopy(self.check) for i in range(12)})
        with patch.object(E, "fingerprint", side_effect=AssertionError("plan reads must not scan input contents")):
            for _ in range(12):
                self.assertEqual(len(P.read_plan(self.root, self.run, self.owner, plan)["checks"]), 12)

    def test_qa_report_reads_grow_linearly_for_one_operation(self):
        checks = {f"case{i}": {**self.check, "argv": [sys.executable, "-B", "-c", f"print({i})"]}
                  for i in range(10)}
        plan = self.save(checks)
        for key in checks:
            self.execute(plan, name=key, key=key)
        with patch.object(E, "read_report", wraps=E.read_report) as reads:
            result = self.qa(plan)
        self.assertEqual(result["gate"], "PASS")
        self.assertLessEqual(reads.call_count, 4 * len(checks) + 3)

    def test_additive_plan_reuses_identical_check_and_preserves_origin(self):
        self.check["argv"][-1] = "from pathlib import Path; p=Path('count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"
        first_plan = self.save(name="first-plan")
        first = self.execute(first_plan)
        metadata = {**self.check, "argv": [sys.executable, "-B", "-c", "print('metadata')"], "coverage": ["metadata"]}
        second_plan = self.save({"unit": self.check, "metadata": metadata}, name="second-plan")
        carried = self.execute(second_plan, "carried", impact_covered=True)
        self.assertTrue(carried["reused"])
        self.assertEqual(carried["report"], first["report"])
        self.assertEqual((self.root / "count").read_text(), "1")
        self.assertEqual(self.qa(second_plan)["check_failures"], {"metadata": "not_executed"})
        self.execute(second_plan, "metadata", key="metadata")
        again = self.execute(second_plan, "carried-again", impact_covered=True)
        binding = E.read_report(self.root, self.run, again["planned_report"])["payload"]
        self.assertEqual(binding["carried_from_plan"], first_plan)
        self.assertEqual(self.qa(second_plan, "complete")["gate"], "PASS")
        data = json.loads((self.root / first_plan).read_text())
        data["payload"]["checks"]["unit"]["coverage"] = ["altered"]
        (self.root / first_plan).write_text(json.dumps(data))
        self.assertEqual(self.qa(second_plan, "originchanged")["gate"], "CONCERNS")

    def test_plan_carry_forward_cannot_ignore_new_failure_or_relabel_coverage(self):
        self.check["argv"][-1] = "from pathlib import Path; p=Path('count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"
        first_plan = self.save(name="first-plan")
        first = self.execute(first_plan)
        proof = E.read_report(self.root, self.run, first["report"])["payload"]
        (self.root / "failed.log").write_text("observed failure")
        E.record_check(self.root, self.run, self.owner, "newfailure", command=proof["command"],
                       scope=proof["scope"], environment=proof["environment"], exit_code=1,
                       log_path="failed.log", started_fingerprint=E.fingerprint(self.root, proof["scope"]),
                       coverage=proof["coverage"])
        next_plan = self.save(name="next-plan")
        self.assertFalse(self.execute(next_plan, "retry", impact_covered=True)["reused"])
        renamed = self.save({"unit": {**self.check, "coverage": ["other"]}}, name="relabeled")
        self.assertFalse(self.execute(renamed, "differentcoverage", impact_covered=True)["reused"])
        self.assertEqual((self.root / "count").read_text(), "3")

    def test_input_path_safety_remains_at_execution_after_plan_read(self):
        (self.root / "directory").mkdir()
        (self.root / "directory/file").write_text("safe")
        self.check["scope"] = ["directory"]
        plan = self.save()
        (self.root / "directory/file").unlink()
        (self.root / "directory/file").symlink_to(self.root / "input")
        P.read_plan(self.root, self.run, self.owner, plan)
        with self.assertRaises(ValueError):
            self.execute(plan)
        self.assertEqual(self.qa(plan)["gate"], "CONCERNS")

    def test_qa_rejects_evidence_appended_during_snapshot_validation(self):
        plan = self.save()
        self.execute(plan)
        original = E.reusable_check
        def observe(*args, **kwargs):
            result = original(*args, **kwargs)
            E.write_report(self.root, self.run, self.owner, "review", "concurrent", {"observed": True})
            return result
        with patch.object(E, "reusable_check", side_effect=observe):
            self.assertEqual(self.qa(plan)["check_failures"]["snapshot"], "evidence_changed_during_qa")


if __name__ == "__main__":
    unittest.main()
