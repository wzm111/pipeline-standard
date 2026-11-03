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

    def test_verify_api_reuses_valid_results_and_does_not_finish(self):
        plan = self.save()
        first = P.verify_plan(self.root, self.run, self.owner, plan, "first")
        second = P.verify_plan(self.root, self.run, self.owner, plan, "second")
        self.assertEqual(first["gate"], "PASS")
        self.assertFalse(first["results"][0]["reused"])
        self.assertTrue(second["results"][0]["reused"])
        self.assertEqual(second["skipped_checks"], [])
        self.assertEqual(R.status(self.root)["state"]["status"], "running")

    def test_preflight_is_read_only_and_does_not_launch_or_claim(self):
        plan = self.save()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch.object(R, "_write_guard", side_effect=AssertionError("write lock")), \
                patch.object(C, "execute_check", side_effect=AssertionError("execute")):
            result = P.preflight_plan(self.root, self.run, self.owner, plan, "ready")
        self.assertTrue(result["ready"])
        self.assertNotIn("gate", result)
        self.assertNotIn(self.owner, json.dumps(result))
        self.assertNotIn("print", json.dumps(result))
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_whole_plan_missing_last_runner_blocks_first_process_and_records_concerns(self):
        first = {**self.check, "argv": [sys.executable, "-B", "-c",
                 "from pathlib import Path; Path('counter').touch()"]}
        missing = {**self.check, "argv": ["pipeline-fixture-missing-runner"], "coverage": ["heavy"]}
        plan = self.save({"first": first, "last": missing})
        preflight = P.preflight_plan(self.root, self.run, self.owner, plan, "blocked")
        self.assertFalse(preflight["ready"])
        self.assertIn({"check_id": "last", "category": "runner", "reason": "executable_unavailable"},
                      preflight["failures"])
        result = P.verify_plan(self.root, self.run, self.owner, plan, "blocked")
        self.assertEqual(result["gate"], "CONCERNS")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["skipped_checks"], ["first", "last"])
        self.assertEqual(E.read_report(self.root, self.run, result["report"], kind="qa")["payload"]["gate"],
                         "CONCERNS")
        self.assertFalse((self.root / "counter").exists())

    def test_preflight_required_coverage_uses_registered_and_valid_inherited_requirements(self):
        self.save({"original": {**self.check, "coverage": ["original"]}}, name="original")
        narrow = self.save({"narrow": {**self.check, "coverage": ["narrow"]}}, name="narrow")
        result = P.verify_plan(self.root, self.run, self.owner, narrow, "blocked")
        self.assertEqual(result["gate"], "CONCERNS")
        self.assertEqual(result["preflight"]["missing_checks"], ["original"])
        self.assertEqual(result["missing_checks"], ["narrow", "original"])
        self.assertEqual(result["results"], [])
        state = R.status(self.root)["state"]
        state["inherited_required_checks"] = ["inherited"]
        R._atomic(self.root / "tmp/pipeline/runs" / (self.run + ".json"), state)
        inherited = self.save({"narrow": {**self.check, "coverage": ["narrow"]}}, name="inherited")
        self.assertEqual(P.preflight_plan(self.root, self.run, self.owner, inherited, "inherited")["missing_checks"],
                         ["inherited", "original"])

    def test_preflight_checks_late_cwd_resources_names_and_active_claims(self):
        package = self.root / "package"
        package.mkdir()
        plan = self.save({"first": self.check, "last": {**self.check, "cwd": "package",
                          "write_paths": ["output"], "protected_paths": ["output"]}})
        result = P.preflight_plan(self.root, self.run, self.owner, plan, "resources")
        self.assertIn({"check_id": "last", "category": "resources", "reason": "conflict"}, result["failures"])
        package.rmdir()
        result = P.verify_plan(self.root, self.run, self.owner, plan, "cwd")
        self.assertEqual(result["results"], [])
        self.assertIn({"check_id": "last", "category": "cwd", "reason": "working_directory_unavailable"},
                      result["preflight"]["failures"])
        valid = self.save(name="valid")
        self.assertEqual(P.preflight_plan(self.root, self.run, self.owner, valid, "a" * 64)["failures"],
                         [{"check_id": "unit", "category": "attempt", "reason": "invalid_attempt_name"}])
        self.execute(valid, "used-c1")
        self.assertFalse(P.preflight_plan(self.root, self.run, self.owner, valid, "used")["ready"])
        (self.root / "input").write_text("changed")
        state = R.status(self.root)["state"]
        state["running_checks"] = [{"name": "running", "write_paths": None}]
        R._atomic(self.root / "tmp/pipeline/runs" / (self.run + ".json"), state)
        self.assertIn({"check_id": "unit", "category": "resources", "reason": "active_check_conflict"},
                      P.preflight_plan(self.root, self.run, self.owner, valid, "claims")["failures"])

    def test_preflight_resource_claim_does_not_block_current_cached_reuse(self):
        plan = self.save()
        self.execute(plan)
        state = R.status(self.root)["state"]
        state["running_checks"] = [{"name": "running", "write_paths": None}]
        R._atomic(self.root / "tmp/pipeline/runs" / (self.run + ".json"), state)
        self.assertTrue(P.preflight_plan(self.root, self.run, self.owner, plan, "cached")["ready"])
        result = P.verify_plan(self.root, self.run, self.owner, plan, "cached")
        self.assertEqual(result["gate"], "PASS")
        self.assertTrue(result["results"][0]["reused"])
        self.assertEqual(R.status(self.root)["state"]["running_checks"], state["running_checks"])

    def test_repeated_successful_verify_name_refuses_without_execution_or_qa_overwrite(self):
        plan = self.save()
        first = P.verify_plan(self.root, self.run, self.owner, plan, "same-round")
        self.assertEqual(first["gate"], "PASS")
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        with patch.object(C, "execute_check", side_effect=AssertionError("must not execute")):
            result = P.verify_plan(self.root, self.run, self.owner, plan, "same-round")
        self.assertEqual(result["gate"], "CONCERNS")
        self.assertIsNone(result["report"])
        self.assertEqual(result["results"], [])
        self.assertIn("qa_name_already_used", [item["reason"] for item in result["preflight"]["failures"]])
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes()
                                  for p in self.root.rglob("*") if p.is_file()})
        self.assertEqual(E.read_report(self.root, self.run, first["report"], kind="qa")["payload"]["gate"], "PASS")

    def quick_plan(self):
        (self.root / "quick.py").write_text(
            "from pathlib import Path\n"
            "p=Path('quick-count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\n"
            "raise SystemExit(int(Path('fail-quick').exists()))\n")
        (self.root / "final-input").write_text("stable final input")
        quick = {**self.check, "argv": [sys.executable, "-B", "quick.py"],
                 "scope": ["quick.py", "input"], "coverage": ["unit"], "layer": "quick"}
        final = {**self.check, "argv": [sys.executable, "-B", "-c",
                 "from pathlib import Path; Path('heavy-ran').touch()"],
                 "scope": ["final-input"], "coverage": ["regression"], "layer": "final"}
        return self.save({"final": final, "unit": quick}), quick, final

    def assert_final_blocked(self, plan, name="blocked"):
        with self.assertRaisesRegex(ValueError, "valid quick evidence: unit"):
            self.execute(plan, name, key="final", impact_covered=True)
        self.assertFalse((self.root / "heavy-ran").exists())

    def test_direct_final_requires_quick_and_verify_orders_it_first(self):
        plan, _, _ = self.quick_plan()
        self.assert_final_blocked(plan)
        self.assertFalse((self.root / "quick-count").exists())
        result = P.verify_plan(self.root, self.run, self.owner, plan, "verified")
        self.assertEqual([item["check_id"] for item in result["results"]], ["unit", "final"])
        self.assertEqual(result["gate"], "PASS")

    def test_final_rejects_quick_code_input_environment_and_log_drift(self):
        plan, _, _ = self.quick_plan()
        result = self.execute(plan)
        for relative in ("quick.py", "input"):
            path = self.root / relative
            before = path.read_text()
            path.write_text(before + "\n# changed")
            self.assert_final_blocked(plan)
            path.write_text(before)
        with patch.dict(os.environ, {"QUICK_BOUNDARY_CONTEXT": "changed"}):
            self.assert_final_blocked(plan)
        proof = E.read_report(self.root, self.run, result["report"])["payload"]
        (self.root / proof["log_path"]).write_text("edited")
        self.assert_final_blocked(plan)

    def test_later_quick_failure_blocks_final_and_preserves_reproduction_class(self):
        plan, quick, final = self.quick_plan()
        self.execute(plan)
        (self.root / "fail-quick").touch()
        failure = C.execute_check(self.root, self.run, self.owner, "later-failure",
                                  argv=quick["argv"], scope=quick["scope"], coverage=["other"])
        self.assertEqual(failure["exit_code"], 1)
        self.assert_final_blocked(plan)
        negative = self.save({"unit": {**quick, "intent": "reproduce"}, "final": final}, name="negative")
        reproduced = self.execute(negative, "reproduced")
        proof = E.read_report(self.root, self.run, reproduced["report"])["payload"]
        self.assertEqual(proof["result_class"], "reproduction_nonzero")
        self.assert_final_blocked(negative)

    def test_additive_quick_carry_and_disjoint_final_input_change_do_not_rerun_quick(self):
        plan, quick, final = self.quick_plan()
        self.execute(plan)
        additive = self.save({"unit": quick, "final": final,
                              "metadata": {**self.check, "coverage": ["metadata"]}}, name="additive")
        self.assert_final_blocked(additive)
        carried = self.execute(additive, "carried", impact_covered=True)
        self.assertTrue(carried["reused"])
        self.execute(additive, "final-first", key="final")
        (self.root / "final-input").write_text("final changed only")
        self.assertFalse(self.execute(additive, "final-retest", key="final", impact_covered=True)["reused"])
        self.assertEqual((self.root / "quick-count").read_text(), "1")
        self.assertEqual(self.qa(additive)["check_failures"], {"metadata": "not_executed"})

    def test_layer_only_plan_change_keeps_exact_quick_evidence_reusable(self):
        plan, quick, _ = self.quick_plan()
        self.execute(plan)
        changed = self.save({"unit": {**quick, "layer": "final"}}, name="relayered")
        self.assertTrue(self.execute(changed, "relayered", impact_covered=True)["reused"])
        self.assertEqual((self.root / "quick-count").read_text(), "1")

    def dependency_plan(self):
        (self.root / "ready-input").write_text("ready")
        ready = {**self.check, "layer": "quick", "scope": ["ready-input"],
                 "coverage": ["ready"], "argv": [sys.executable, "-B", "-c", "print('ready')"]}
        dependent = {**self.check, "layer": "quick", "depends_on": ["ready"],
                     "argv": [sys.executable, "-B", "-c",
                              "from pathlib import Path; Path('dependent-ran').touch()"]}
        return self.save({"dependent": dependent, "ready": ready}), ready, dependent

    def test_dependencies_reject_invalid_references_types_and_cycles_before_execution(self):
        for dependencies in ("unit", [1], ["missing"], ["unit"]):
            with self.subTest(dependencies=dependencies), self.assertRaisesRegex(ValueError, "depends_on"):
                self.save({"unit": {**self.check, "depends_on": dependencies}})
        for checks in (
            {"a": {**self.check, "depends_on": ["b"]}, "b": {**self.check, "depends_on": ["a"]}},
            {"a": {**self.check, "layer": "quick", "depends_on": ["b"]}, "b": self.check},
        ):
            with self.subTest(checks=checks), self.assertRaisesRegex(ValueError, "cycle"):
                self.save(checks)
        self.assertFalse(R.status(self.root)["state"].get("running_checks"))

    def test_read_revalidates_dependency_graph(self):
        plan = self.save()
        payload = P.read_plan(self.root, self.run, self.owner, plan)
        payload["checks"]["unit"]["depends_on"] = ["missing"]
        with patch.object(P, "_report", return_value=payload):
            with self.assertRaisesRegex(ValueError, "depends_on"):
                P.read_plan(self.root, self.run, self.owner, plan)

    def test_verify_stable_topological_order_keeps_quick_priority(self):
        plan, ready, dependent = self.dependency_plan()
        final = {**self.check, "coverage": ["final"], "depends_on": ["other-final"]}
        plan = self.save({"final": final, "dependent": dependent,
                          "independent": {**ready, "coverage": ["independent"]}, "ready": ready,
                          "other-final": {**self.check, "coverage": ["other-final"]}}, name="ordered")
        result = P.verify_plan(self.root, self.run, self.owner, plan, "ordered")
        self.assertEqual([item["check_id"] for item in result["results"]],
                         ["independent", "ready", "dependent", "other-final", "final"])
        self.assertEqual(result["gate"], "PASS")

    def test_direct_dependency_checks_current_input_environment_logs_and_latest_failure(self):
        plan, ready, _ = self.dependency_plan()
        def blocked(name):
            with self.assertRaisesRegex(ValueError, "valid dependency evidence: ready"):
                self.execute(plan, name, key="dependent", impact_covered=True)
            self.assertFalse((self.root / "dependent-ran").exists())
        blocked("missing")
        result = self.execute(plan, "ready", key="ready")
        (self.root / "ready-input").write_text("drift")
        blocked("input-drift")
        (self.root / "ready-input").write_text("ready")
        with patch.dict(os.environ, {"READINESS_CONTEXT": "drift"}):
            blocked("environment-drift")
        proof = E.read_report(self.root, self.run, result["report"])["payload"]
        log = self.root / proof["log_path"]
        content = log.read_bytes()
        log.write_text("tampered")
        blocked("log-drift")
        log.write_bytes(content)
        (self.root / "failure.log").write_text("later observed failure")
        E.record_check(self.root, self.run, self.owner, "later-failure", command=proof["command"],
                       scope=proof["scope"], environment=proof["environment"], exit_code=1,
                       log_path="failure.log", coverage=["other"],
                       started_fingerprint=E.fingerprint(self.root, ready["scope"]))
        blocked("latest-failure")

    def test_direct_dependency_checks_transitive_prerequisites(self):
        plan, ready, dependent = self.dependency_plan()
        leaf = {**self.check, "layer": "quick", "depends_on": ["dependent"]}
        plan = self.save({"ready": ready, "dependent": dependent, "leaf": leaf}, name="chain")
        self.execute(plan, "ready", key="ready")
        self.execute(plan, "dependent", key="dependent")
        (self.root / "ready-input").write_text("drift")
        with self.assertRaisesRegex(ValueError, "valid dependency evidence: ready"):
            self.execute(plan, "leaf", key="leaf")

    def test_dependency_only_change_reuses_identity_after_current_prerequisite(self):
        plan, ready, dependent = self.dependency_plan()
        original = self.save({"dependent": {k: v for k, v in dependent.items() if k != "depends_on"}},
                             name="original")
        self.execute(original, "original", key="dependent")
        (self.root / "dependent-ran").unlink()
        with self.assertRaisesRegex(ValueError, "valid dependency evidence"):
            self.execute(plan, "blocked", key="dependent", impact_covered=True)
        self.execute(plan, "ready", key="ready")
        reused = self.execute(plan, "carried", key="dependent", impact_covered=True)
        self.assertTrue(reused["reused"])
        self.assertFalse((self.root / "dependent-ran").exists())
        self.assertEqual(self.qa(plan)["gate"], "PASS")

    def test_failed_readiness_stops_dependent_quick_before_heavy_check(self):
        plan, ready, dependent = self.dependency_plan()
        ready["argv"][-1] = "raise SystemExit(1)"
        final = {**self.check, "coverage": ["final"], "argv": [sys.executable, "-B", "-c",
                 "from pathlib import Path; Path('heavy-ran').touch()"]}
        plan = self.save({"dependent": dependent, "ready": ready, "final": final}, name="failed-ready")
        result = P.verify_plan(self.root, self.run, self.owner, plan, "failed-ready")
        self.assertEqual([item["check_id"] for item in result["results"]], ["ready"])
        self.assertEqual(result["skipped_checks"], ["dependent", "final"])
        self.assertEqual(result["gate"], "CONCERNS")
        self.assertFalse((self.root / "dependent-ran").exists())
        self.assertFalse((self.root / "heavy-ran").exists())

    def test_quick_prerequisite_error_bounds_ids_without_command_details(self):
        plan = self.save({**{f"boundary{i}": {**self.check, "layer": "quick"} for i in range(10)},
                          "final": {**self.check, "layer": "final"}})
        with self.assertRaises(ValueError) as error:
            self.execute(plan, key="final")
        self.assertIn("boundary7 (+2 more)", str(error.exception))
        self.assertNotIn("boundary8", str(error.exception))
        self.assertNotIn("print", str(error.exception))
        self.assertFalse(R.status(self.root)["state"].get("running_checks"))

    def test_verify_api_failure_stops_later_commands_but_records_missing_qa(self):
        failed = copy.deepcopy(self.check)
        failed["argv"][-1] = "raise SystemExit(1)"
        other = copy.deepcopy(self.check)
        other["argv"][-1] = "from pathlib import Path; Path('heavy-ran').touch()"
        other["coverage"] = ["heavy"]
        plan = self.save({"first": failed, "heavy": other})
        result = P.verify_plan(self.root, self.run, self.owner, plan, "verify")
        self.assertEqual(result["gate"], "CONCERNS")
        self.assertEqual(result["skipped_checks"], ["heavy"])
        self.assertEqual(result["check_failures"]["heavy"], "not_executed")
        self.assertFalse((self.root / "heavy-ran").exists())
        self.assertIn(result["report"], R.status(self.root)["state"]["artifacts"])

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

    def test_first_binding_reuses_direct_check_only_with_explicit_impact_confirmation(self):
        self.check["argv"][-1] = "from pathlib import Path; p=Path('count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"
        direct = C.execute_check(self.root, self.run, self.owner, "direct", **self.check)
        plan = self.save()
        with patch.object(C.subprocess, "Popen", side_effect=AssertionError("must reuse direct check")):
            bound = self.execute(plan, "bound", impact_covered=True)
        self.assertTrue(bound["reused"])
        self.assertEqual(bound["report"], direct["report"])
        self.assertEqual((self.root / "count").read_text(), "1")
        binding = E.read_report(self.root, self.run, bound["planned_report"])["payload"]
        self.assertNotIn("carried_from_plan", binding)
        self.assertEqual(self.qa(plan)["gate"], "PASS")
        for index, options in enumerate(({}, {"impact_covered": False})):
            other = self.save(name=f"unconfirmed-{index}")
            self.assertFalse(self.execute(other, f"fresh-{index}", **options)["reused"])
        self.assertEqual((self.root / "count").read_text(), "3")

    def test_first_binding_keeps_strict_direct_evidence_guards(self):
        for case in ("input", "environment", "log", "coverage", "command", "cwd", "failure"):
            with self.subTest(case=case):
                (self.root / "input").write_text("stable")
                check = {**self.check, "argv": [sys.executable, "-B", "-c", f"print({case!r})"]}
                if case == "failure":
                    check["argv"][-1] = "from pathlib import Path; raise SystemExit(int(Path('fail-direct').exists()))"
                direct = C.execute_check(self.root, self.run, self.owner, f"direct-{case}", **check)
                if case == "input":
                    (self.root / "input").write_text("changed")
                elif case == "environment":
                    check["environment"] = {"fixture": "changed"}
                elif case == "log":
                    proof = E.read_report(self.root, self.run, direct["report"])["payload"]
                    (self.root / proof["log_path"]).write_text("changed log")
                elif case == "coverage":
                    check["coverage"] = ["different"]
                elif case == "command":
                    check["argv"] = [*check["argv"][:-1], "print('changed command')"]
                elif case == "cwd":
                    (self.root / "package").mkdir()
                    check["cwd"] = "package"
                elif case == "failure":
                    (self.root / "fail-direct").touch()
                    failed = C.execute_check(self.root, self.run, self.owner, "later-failure", **check)
                    self.assertEqual(failed["exit_code"], 1)
                plan = self.save({"unit": check}, name=f"plan-{case}")
                result = self.execute(plan, f"bound-{case}", impact_covered=True)
                self.assertFalse(result["reused"])
                self.assertEqual(result["exit_code"], 1 if case == "failure" else 0)

    def test_first_binding_preflight_allows_only_current_direct_reuse_under_resource_conflict(self):
        C.execute_check(self.root, self.run, self.owner, "direct", **self.check)
        plan = self.save()
        state = R.status(self.root)["state"]
        state["running_checks"] = [{"name": "running", "write_paths": None}]
        R._atomic(self.root / "tmp/pipeline/runs" / (self.run + ".json"), state)
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertTrue(P.preflight_plan(self.root, self.run, self.owner, plan, "cached")["ready"])
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes()
                                  for p in self.root.rglob("*") if p.is_file()})
        result = P.verify_plan(self.root, self.run, self.owner, plan, "cached")
        self.assertEqual(result["gate"], "PASS")
        self.assertTrue(result["results"][0]["reused"])
        self.assertEqual(R.status(self.root)["state"]["running_checks"], state["running_checks"])
        (self.root / "input").write_text("changed")
        self.assertFalse(P.preflight_plan(self.root, self.run, self.owner, plan, "stale")["ready"])
        with self.assertRaisesRegex(ValueError, "conflict"):
            self.execute(plan, "stale", impact_covered=True)

    def test_direct_final_evidence_cannot_bypass_missing_dependency_binding(self):
        plan, _, final = self.quick_plan()
        C.execute_check(self.root, self.run, self.owner, "direct-final", **P._execution_spec(final))
        with self.assertRaisesRegex(ValueError, "valid quick evidence: unit"):
            self.execute(plan, "bound-final", key="final", impact_covered=True)
        self.assertEqual(self.qa(plan)["gate"], "CONCERNS")

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
