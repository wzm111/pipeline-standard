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
import managed_qa as Q
import process_compliance as PC
import review_control as RC
import release_checks as F
import delivery as Delivery
import role_dispatch as D
import run_checks as C
import run_evidence as E
import run_policy as V
import run_state as R
import run_tasks as T
import work_allocation as W


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

    def git(self, *args):
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", *args],
                       cwd=self.root, capture_output=True, check=True)

    def initialize_source_baseline(self):
        self.git("init")
        self.git("add", "input", "PIPELINE.md")
        self.git("commit", "-m", "fixture input")

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

    def test_baseline_failure_then_changed_source_accepts_only_final_check(self):
        argv = [sys.executable, "-B", "-c",
                "from pathlib import Path; raise SystemExit(int(Path('input').read_text() != 'fixed'))"]
        baseline = self.check("before", argv=argv, intent="baseline", coverage=["before-change"])
        original = (self.root / baseline["report"]).read_bytes()
        proof = self.payload(baseline["report"])
        self.assertEqual((proof["exit_code"], proof["result_class"]), (1, "test_failure"))
        self.assertGreater(proof["seconds"], 0)
        self.assertNotIn("gate", proof)
        (self.root / "input").write_text("fixed")
        final_check = self.check("after", argv=argv, impact_covered=True)
        self.assertFalse(final_check["reused"])
        self.single(final_check)
        final = self.finish()
        self.assertTrue(final["delivery_status"]["sealed"])
        self.assertEqual(final["delivery_status"]["coverage"]["required"], ["unit"])
        self.assertEqual(final["delivery_status"]["coverage"]["covered"], ["unit"])
        self.assertEqual(original, (self.root / baseline["report"]).read_bytes())
        self.assertIn(baseline["report"], final["artifacts"])
        self.assertEqual(C.check_summary(self.root, self.args[1])["by_raw_result_class"]["test_failure"], 1)

    def test_baseline_pass_cannot_masquerade_as_acceptance_or_reusable_check(self):
        baseline = self.check("before", intent="baseline")
        proof = self.payload(baseline["report"])
        with self.assertRaises(ValueError):
            self.single(baseline)
        with self.assertRaisesRegex(ValueError, "baseline"):
            P.save_plan(*self.args, "bad-plan", {"unit": self.spec | {"intent": "baseline"}})
        verdict = E.reusable_check(self.root, self.args[1], baseline["report"], command=proof["command"],
                                  environment=proof["environment"], coverage=proof["coverage"], impact_covered=True)
        self.assertEqual(verdict, {"reusable": False, "reason": "baseline_not_acceptance"})
        formal = self.check("formal", reuse_from=baseline["report"], impact_covered=True)
        self.assertFalse(formal["reused"])
        self.single(formal)
        self.assertTrue(self.finish()["delivery_status"]["sealed"])

    def test_baseline_only_completion_has_no_delivered_acceptance(self):
        self.check("before", intent="baseline")
        final = self.finish()
        self.assertEqual(final["status"], "completed")
        self.assertFalse(final["delivery_status"]["sealed"])

    def test_baseline_does_not_satisfy_or_erase_existing_plan_requirements(self):
        plan = P.save_plan(*self.args, "required", {"unit": self.spec})
        self.check("before", intent="baseline")
        qa = P.qa_from_plan(*self.args, plan, "missing")
        self.assertEqual(self.payload(qa)["gate"], "CONCERNS")
        self.assertEqual(self.payload(qa)["required_checks"], ["unit"])
        self.assertEqual(self.payload(qa)["missing_checks"], ["unit"])
        with self.assertRaises(ValueError):
            self.finish()
        P.run_planned(*self.args, plan, "unit", "actual", impact_covered=True)
        qa = P.qa_from_plan(*self.args, plan, "actual-qa")
        self.assertEqual(self.payload(qa)["gate"], "PASS")
        self.assertTrue(self.finish()["delivery_status"]["sealed"])

    def test_coverage_named_baseline_remains_formal_evidence(self):
        old = self.check("legacy", coverage=["baseline"])
        before = (self.root / old["report"]).read_bytes()
        checked = self.check("new")
        with self.assertRaisesRegex(ValueError, "plan"):
            self.single(checked)
        self.assertEqual(before, (self.root / old["report"]).read_bytes())

    def test_later_baseline_pass_cannot_erase_formal_failure(self):
        argv = [sys.executable, "-B", "-c",
                "from pathlib import Path; raise SystemExit(int(Path('fail').exists()))"]
        passed = self.check("old-pass", argv=argv)
        (self.root / "fail").touch()
        failure = self.check("formal-failure", argv=argv)
        original = (self.root / failure["report"]).read_bytes()
        (self.root / "fail").unlink()
        self.assertEqual(self.check("baseline-pass", argv=argv, intent="baseline")["exit_code"], 0)
        with self.assertRaises(ValueError):
            self.single(passed)
        self.assertEqual(original, (self.root / failure["report"]).read_bytes())

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

    def test_high_risk_omitted_declaration_blocks_development_and_acceptance(self):
        T.start(*self.args, "risk", "shared logic", ["cross entry"], require_managed=True,
                review_risk="high", review_reason="shared authorization")
        self.start_feedback()
        with self.assertRaisesRegex(ValueError, "solution review"):
            D.begin(*self.args, "developer")
        with self.assertRaisesRegex(ValueError, "solution review"):
            R.update(*self.args, {"stage": "implementation"})
        check = self.check()
        self.assertEqual(self.payload(self.single(check))["gate"], "CONCERNS")
        with self.assertRaises(ValueError):
            self.finish()

    def test_review_obligation_survives_task_continuation_but_not_unrelated_work(self):
        first = T.start(*self.args, "risk", "shared logic", ["cross entry"],
                        review_risk="high", review_reason="shared authorization")
        T.end(*self.args, first["task_id"])
        T.start(*self.args, "continued", "same delivery", ["same criteria"],
                review_risk="low", review_reason="delivery only", continuation_of="risk")
        self.assertTrue(RC.required(R.status(self.root)["state"]))
        T.end(*self.args, "continued")
        T.start(*self.args, "unrelated", "new task", ["new criteria"],
                review_risk="low", review_reason="independent documentation change", independent_goal=True)
        self.assertFalse(RC.required(R.status(self.root)["state"]))

    def test_review_decision_is_required_for_managed_task_and_immutable(self):
        with self.assertRaises(ValueError):
            T.start(*self.args, "missing", "goal", ["criteria"], require_managed=True)
        T.start(*self.args, "risk", "goal", ["criteria"], review_risk="high", review_reason="payments")
        with self.assertRaisesRegex(ValueError, "immutable"):
            T.start(*self.args, "risk", "goal", ["criteria"], review_risk="low", review_reason="changed")

    def test_actual_feedback_is_checked_and_only_metadata_is_saved(self):
        ref = E.write_report(*self.args, "progress_feedback", "bad", {"messages": [{"text": "PRIVATE body"}]})
        payload = self.payload(ref)
        self.assertFalse(payload["format_valid"])
        self.assertNotIn("PRIVATE", json.dumps(payload))
        with self.assertRaises(ValueError):
            E.write_report(*self.args, "progress_feedback", "forged", {"format_valid": True})

    def test_activation_missing_experts_does_not_register_or_execute_mutation(self):
        E.write_report(*self.args, "dual_review", "required", {})
        # A real release build is unnecessary to prove the early fail-closed boundary.
        with self.assertRaisesRegex(ValueError, "verification"):
            F.execute(*self.args, "blocked", phase="activate",
                      argv=[sys.executable, "-c", "from pathlib import Path; Path('mutated').touch()"],
                      scope=["input"])
        self.assertFalse((self.root / "mutated").exists())
        state = R.status(self.root)["state"]
        self.assertEqual(state["release_plan"]["attempts"], [])

    def review_report(self, kind, name, payload):
        return E.write_report(*self.args, kind, name, payload)

    def review_dispatch(self, role, executor, host=None):
        dispatch = D.begin(*self.args, role)["id"]
        D.accepted(*self.args, dispatch, executor, host_executor_id=host or "host-" + executor)
        D.complete(*self.args, dispatch, executors_stopped=True, stop_evidence="fixture stopped")
        return dispatch

    def reviewed_plan(self, checks):
        self.review_report("progress_feedback", "visible", {"messages": [
            {"text": "🔵 **[进行中]** 已完成范围核对，下一步验证。"}]})
        requirement = self.review_report("dual_review", "required", {})
        plan = P.save_plan(*self.args, "reviewed", checks)
        solution = self.review_report("solution_review", "solution", {
            "gate": "PASS", "dispatch_id": self.review_dispatch("scope_guardian", "planner"),
            "requirement_ref": requirement, "plan_ref": plan})
        return plan, solution

    def verified_refs(self, solution, refs, name="verification"):
        return self.review_report("verification_review", name, {
            "gate": "PASS", "dispatch_id": self.review_dispatch("tester", name),
            "solution_ref": solution, "checks": refs})

    def test_manual_run_obligation_survives_first_classified_task(self):
        self.review_report("dual_review", "required", {})
        T.start(*self.args, "first", "same delivery", ["same criteria"],
                review_risk="low", review_reason="delivery continuation")
        self.assertTrue(RC.required(R.status(self.root)["state"]))
        with self.assertRaises(ValueError):
            D.begin(*self.args, "developer")

    def test_cross_run_delivery_preserves_obligation_and_valid_checks(self):
        plan, solution = self.reviewed_plan({"unit": self.spec})
        checked = P.run_planned(*self.args, plan, "unit", "unit")
        refs = P.inspect_plan(*self.args, plan)["checks"]
        self.verified_refs(solution, refs)
        P.qa_from_plan(*self.args, plan, "final")
        finished = self.finish()
        reference = finished["delivery_status"]["reference"]
        packet_path = self.root / reference["path"]
        envelope = json.loads(packet_path.read_text())
        self.assertTrue(envelope["payload"].pop("dual_review_required"))
        # Simulate the prior receipt schema, whose QA still binds expert reviews.
        R._atomic(packet_path, envelope)
        legacy = Delivery._reference(self.root, self.args[1], reference["path"])
        adopted = R.start(self.root, {"delivery_from": legacy}, managed=True)
        self.assertTrue(adopted["delivery_status"]["adopted"])
        self.assertTrue(RC.required(adopted))
        self.assertEqual(len(adopted["delivery_status"]["checks"]), 1)

    def test_reviewed_prechecks_cannot_be_replaced_by_same_label_pass(self):
        failing = self.spec | {"argv": [sys.executable, "-B", "-c", "raise SystemExit(1)"]}
        plan, solution = self.reviewed_plan({"unit": failing})
        P.run_planned(*self.args, plan, "unit", "failed")
        unrelated = self.check("unrelated")
        self.verified_refs(solution, [unrelated["report"]])
        F.prepare(*self.args)
        with self.assertRaisesRegex(ValueError, "reviewed pre-activation check"):
            RC.validate_pre_activation(self.root, R.status(self.root)["state"])

    def test_pre_activation_allows_pending_post_smoke_but_closeout_does_not(self):
        post = self.spec | {"coverage": ["release_identity"],
                             "argv": [sys.executable, "-B", "-c", "print('post-release identity')"]}
        plan, solution = self.reviewed_plan({"unit": self.spec, "post": post})
        P.run_planned(*self.args, plan, "unit", "unit")
        for phase in ("preflight", "compatibility"):
            F.execute(*self.args, phase, phase=phase, argv=self.spec["argv"], scope=["input"], write_paths=[])
        state = R.status(self.root)["state"]
        refs = sorted(Q._checks(self.root, state)[0])
        self.verified_refs(solution, refs)
        RC.validate_pre_activation(self.root, R.status(self.root)["state"])
        # A caller's unresolved business concern remains a blocker.
        self.review_report("qa", "business-concern", {"gate": "CONCERNS"})
        with self.assertRaisesRegex(ValueError, "unresolved"):
            RC.validate_pre_activation(self.root, R.status(self.root)["state"])
        with self.assertRaises(ValueError):
            F.closeout(*self.args)

    def test_late_solution_cannot_certify_earlier_implementation(self):
        plan, solution = self.reviewed_plan({"unit": self.spec})
        state = R.status(self.root)["state"]
        state["history"].append({"event": "transition", "at": state["created_at"],
                                 "stage": "implementation", "activity": "execution"})
        R._atomic(self.root / "tmp/pipeline/runs" / (self.args[1] + ".json"), state)
        P.run_planned(*self.args, plan, "unit", "unit")
        refs = P.inspect_plan(*self.args, plan)["checks"]
        self.verified_refs(solution, refs)
        result = self.payload(P.qa_from_plan(*self.args, plan, "late"))
        self.assertEqual(result["gate"], "CONCERNS")
        self.assertIn("dual_review", result["check_failures"])

    def test_amendment_preserves_timely_root_and_independent_current_check(self):
        _, solution = self.reviewed_plan({"unit": self.spec})
        R.update(*self.args, {"stage": "implementation"})
        (self.root / "extra").write_text("also checked")
        new = P.save_plan(*self.args, "expanded", {"unit": self.spec | {"scope": ["input", "extra"]}})
        pending = P.qa_from_plan(*self.args, new, "missing")
        self.assertEqual(self.payload(pending)["gate"], "CONCERNS")
        leaf = self.review_report("solution_review", "amendment", {
            "gate": "PASS", "dispatch_id": self.review_dispatch("scope_guardian", "amender"),
            "requirement_ref": self.payload(solution)["requirement_ref"], "amends": solution, "plan_ref": new})
        P.run_planned(*self.args, new, "unit", "expanded-test")
        refs = P.inspect_plan(*self.args, new)["checks"]
        self.review_report("verification_review", "same-root-planner", {
            "gate": "PASS", "dispatch_id": self.review_dispatch("tester", "alias", "host-planner"),
            "solution_ref": leaf, "checks": refs})
        self.assertEqual(self.payload(P.qa_from_plan(*self.args, new, "shared-root-reviewer"))["gate"], "CONCERNS")
        self.verified_refs(leaf, refs)
        self.assertEqual(self.payload(P.qa_from_plan(*self.args, new, "amended"))["gate"], "PASS")
        self.assertEqual(self.payload(pending)["gate"], "CONCERNS")
        self.assertEqual(self.finish()["status"], "completed")

    def test_amendment_cannot_drop_a_check_even_when_coverage_labels_match(self):
        _, solution = self.reviewed_plan({"unit": self.spec, "independent": self.spec})
        new = P.save_plan(*self.args, "reduced", {"unit": self.spec})
        leaf = self.review_report("solution_review", "amend", {"gate": "PASS", "amends": solution,
            "dispatch_id": self.review_dispatch("scope_guardian", "new"),
            "requirement_ref": self.payload(solution)["requirement_ref"], "plan_ref": new})
        P.run_planned(*self.args, new, "unit", "current")
        self.verified_refs(leaf, P.inspect_plan(*self.args, new)["checks"])
        self.assertEqual(self.payload(P.qa_from_plan(*self.args, new, "dropped"))["gate"], "CONCERNS")

    def test_amendment_cannot_skip_a_failed_verdict_or_reuse_a_dispatch(self):
        _, solution = self.reviewed_plan({"unit": self.spec})
        new = P.save_plan(*self.args, "expanded", {"unit": self.spec})
        original = self.payload(solution)
        stale = self.review_report("solution_review", "stale", original | {"amends": solution, "plan_ref": new})
        P.run_planned(*self.args, new, "unit", "current")
        refs = P.inspect_plan(*self.args, new)["checks"]
        self.verified_refs(stale, refs)
        self.assertEqual(self.payload(P.qa_from_plan(*self.args, new, "replayed"))["gate"], "CONCERNS")
        self.review_report("solution_review", "fail", {"gate": "FAIL"})
        skipped = self.review_report("solution_review", "skip", original | {"amends": solution, "plan_ref": new,
            "dispatch_id": self.review_dispatch("scope_guardian", "fresh")})
        self.verified_refs(skipped, refs, "new-verification")
        self.assertEqual(self.payload(P.qa_from_plan(*self.args, new, "skipped"))["gate"], "CONCERNS")

    def test_declared_retrospective_cannot_start_development(self):
        T.start(*self.args, "retro", "review existing implementation", ["evidence"],
                review_risk="high", review_reason="post implementation pilot", retrospective=True)
        with self.assertRaisesRegex(ValueError, "read-only"):
            R.update(*self.args, {"stage": "implementation"})
        with self.assertRaisesRegex(ValueError, "read-only"):
            D.begin(*self.args, "tester", workflow_phase="implementation")

    def test_declared_dual_review_needs_independent_current_returns(self):
        def report(kind, name, payload):
            return E.write_report(*self.args, kind, name, payload)

        def complete(role, executor, host_id):
            dispatch = D.begin(*self.args, role)["id"]
            D.accepted(*self.args, dispatch, executor, host_executor_id=host_id)
            D.complete(*self.args, dispatch, executors_stopped=True, stop_evidence="fixture returned")
            return dispatch

        report("progress_feedback", "visible", {"messages": [
            {"text": "🔵 **[进行中]** 已核对候选，下一步独立评审。"}]})
        requirement = report("dual_review", "required", {})
        with self.assertRaises(ValueError):
            self.finish()
        plan = P.save_plan(*self.args, "plan", {"unit": self.spec})
        solution = report("solution_review", "scope", {
            "gate": "PASS", "dispatch_id": complete("scope_guardian", "reviewer", "host-reviewer"),
            "requirement_ref": requirement, "plan_ref": plan,
        })
        P.run_planned(*self.args, plan, "unit", "unit")
        pending = self.payload(P.qa_from_plan(*self.args, plan, "pending"))
        self.assertEqual(pending["gate"], "CONCERNS")
        verification = {"gate": "PASS", "solution_ref": solution, "checks": pending["checks"]}
        report("verification_review", "shared-host", verification | {
            "dispatch_id": complete("tester", "alias", "host-reviewer"),
        })
        self.assertEqual(self.payload(P.qa_from_plan(*self.args, plan, "alias"))["gate"], "CONCERNS")
        report("verification_review", "independent", verification | {
            "dispatch_id": complete("tester", "verifier", "host-verifier"),
        })
        final = self.payload(P.qa_from_plan(*self.args, plan, "verified"))
        self.assertEqual(final["gate"], "PASS")
        self.assertEqual(len(final["expert_reviews"]), 3)
        report("verification_review", "new-failure", {"gate": "FAIL"})
        with self.assertRaises(ValueError):
            self.finish()

    def start_feedback(self, name="opening"):
        return E.write_report(*self.args, "progress_feedback", name, {"milestone": "start",
                              "messages": [{"text": "🔵 **[进行中]** 开始核对任务并安排评审。"}]})

    def test_first_dispatch_requires_valid_start_feedback_in_current_task(self):
        def start(task):
            self.cli("task-start", "--receipt", str(self.receipt), "--task-id", task,
                     "--goal", "review", "--accept", "checked", "--review-risk", "low",
                     "--review-reason", "fixture")

        start("first")
        with self.assertRaisesRegex(ValueError, "progress_feedback for start"):
            D.begin(*self.args, "planner")
        for name, message in (("malformed", {"text": "[Pipeline] 开始"}),
                              ("stale", {"text": "🔵 **[进行中]** 开始。", "sent_at": "2000-01-01T00:00:00Z"})):
            E.write_report(*self.args, "progress_feedback", name, {"milestone": "start", "messages": [message]})
            with self.assertRaisesRegex(ValueError, "progress_feedback for start"):
                D.begin(*self.args, "planner")
        self.assertFalse(R.status(self.root)["state"].get("role_dispatches"))
        self.start_feedback()
        first = D.begin(*self.args, "planner")
        D.complete(*self.args, first["id"], executors_stopped=True, stop_evidence="fixture stopped", outcome="failed")
        second = D.begin(*self.args, "planner")
        D.complete(*self.args, second["id"], executors_stopped=True, stop_evidence="fixture stopped", outcome="failed")
        T.end(*self.args, "first")
        start("second")
        with self.assertRaisesRegex(ValueError, "progress_feedback for start"):
            D.begin(*self.args, "planner")
        self.start_feedback("second-opening")
        self.assertEqual(D.begin(*self.args, "planner")["status"], "dispatching")

    def test_high_risk_project_write_requires_review_and_current_implementation(self):
        (self.root / "PIPELINE.md").write_text("<!-- pipeline-guard:allow-write\ninput\ntmp/\n-->")
        self.initialize_source_baseline()
        V.acknowledge(*self.args, V.snapshot(self.root)["revision"])
        self.cli("task-start", "--receipt", str(self.receipt), "--task-id", "repair",
                 "--goal", "repair", "--accept", "checks", "--review-risk", "high",
                 "--review-reason", "sensitive behavior")

        def hook(path, cwd=None):
            payload = {"cwd": str(cwd or self.root), "tool_name": "Edit", "tool_input": {"file_path": path}}
            return subprocess.run(["bash", str(SCRIPTS.parents[2] / "hooks/pipeline-guard.sh")],
                                  input=json.dumps(payload), capture_output=True, text=True)

        args = ("write-preflight", "--receipt", str(self.receipt), "--path")
        self.assertIn("solution review", self.cli(*args, "input", expected=2)["message"])
        self.assertEqual(hook("input").returncode, 2)
        subdirectory = self.root / "src"
        subdirectory.mkdir()
        self.assertEqual(hook("../input", cwd=subdirectory).returncode, 2)
        with self.assertRaisesRegex(ValueError, "solution review"):
            self.check(write_paths=["input"])
        self.assertEqual(hook("tmp/pipeline/context.md").returncode, 0)
        self.assertTrue(self.cli(*args, "tmp/pipeline/context.md")["ready"])
        self.start_feedback()
        requirement = E.write_report(*self.args, "dual_review", "required", {})
        plan = P.save_plan(*self.args, "plan", {"unit": self.spec})
        ticket = D.begin(*self.args, "scope_guardian")
        D.accepted(*self.args, ticket["id"], "reviewer", host_executor_id="native-reviewer")
        D.complete(*self.args, ticket["id"], executors_stopped=True, stop_evidence="fixture returned")
        good_result = {"milestone": "solution_returned",
                       "messages": [{"text": "✅ **[已完成｜方案评审]** 方案通过，下一步按范围开发。"}]}
        E.write_report(*self.args, "progress_feedback", "too-early", good_result)
        E.write_report(*self.args, "solution_review", "solution",
                       {"gate": "PASS", "requirement_ref": requirement, "plan_ref": plan,
                        "dispatch_id": ticket["id"]})
        with self.assertRaisesRegex(ValueError, "progress_feedback for solution_returned"):
            R.update(*self.args, {"stage": "implementation"})
        with self.assertRaisesRegex(ValueError, "progress_feedback for solution_returned"):
            D.begin(*self.args, "developer", workflow_phase="implementation")
        E.write_report(*self.args, "progress_feedback", "wrong-style", {"milestone": "solution_returned",
                       "messages": [{"text": "[Pipeline] 方案评审通过。"}]})
        self.assertIn("solution_returned", self.cli(*args, "input", expected=2)["message"])
        E.write_report(*self.args, "progress_feedback", "actual-solution-return", good_result)
        self.assertIn("enter implementation", self.cli(*args, "input", expected=2)["message"])
        (self.root / "input").write_text("direct edit before entry")
        with self.assertRaisesRegex(ValueError, "changed before"):
            R.update(*self.args, {"stage": "implementation"})
        (self.root / "input").write_text("fixture source")
        R.update(*self.args, {"stage": "implementation"})
        self.assertIn("native developer", self.cli(*args, "input", expected=2)["message"])
        W.decide(*self.args, mode="serial", reason="dependency_chain",
                 assignments=[{"id": "repair", "executor_id": "dev", "write_paths": ["input"], "depends_on": []}])
        (self.root / "input").write_text("direct edit after entry")
        with self.assertRaisesRegex(ValueError, "changed before"):
            D.begin(*self.args, "developer", work_item="repair")
        (self.root / "input").write_text("fixture source")
        developer = D.begin(*self.args, "developer", work_item="repair")
        D.accepted(*self.args, developer["id"], "dev", host_executor_id="native-dev")
        self.assertIn("assigned paths", self.cli(*args, "elsewhere", expected=2)["message"])
        self.assertTrue(self.cli(*args, "input")["ready"])
        self.assertEqual(hook("input").returncode, 0)

    def test_closeout_detects_unregistered_source_changes_even_after_commit(self):
        self.initialize_source_baseline()
        self.cli("task-start", "--receipt", str(self.receipt), "--task-id", "repair",
                 "--goal", "repair", "--accept", "checks", "--review-risk", "high",
                 "--review-reason", "sensitive behavior")
        state = R.status(self.root)["state"]
        (self.root / "tmp/pipeline/technical-note.md").write_text("normal work record")
        self.assertFalse(RC.implementation_changed(self.root, state))
        self.assertNotIn("developer", PC.assess(self.root, state)["dispatch_issues"])
        (self.root / "input").write_text("direct edit")
        self.assertTrue(RC.implementation_changed(self.root, state))
        self.git("add", "input")
        self.git("commit", "-m", "bypassed entry")
        compliance = PC.assess(self.root, R.status(self.root)["state"])
        self.assertEqual(compliance["dispatch_issues"]["developer"], "missing_completed_host_dispatch")
        self.assertEqual(compliance["workflow_issues"]["implementation"], "project_changes_without_implementation_entry")
        with self.assertRaisesRegex(ValueError, "project_changes_without_implementation_entry"):
            T.end(*self.args, "repair")

    def test_supervised_high_risk_requires_current_phase_and_observed_routing_at_closeout(self):
        configured = {"schema_version": 1, "runtimes": {"codex": {
            "scope_guardian": {"model": "test-expert", "effort": "high"},
            "tester": {"model": "test-default", "task_routes": {
                "high_risk_verification": {"model": "test-verifier", "effort": "high"}}},
        }}}
        (self.root / "pipeline.models.json").write_text(json.dumps(configured))
        self.initialize_source_baseline()
        self.cli("task-start", "--receipt", str(self.receipt), "--task-id", "audit",
                 "--goal", "audit", "--accept", "checks", "--review-risk", "high",
                 "--review-reason", "sensitive evidence", "--retrospective")

        def report(kind, name, payload):
            return E.write_report(*self.args, kind, name, payload)

        def dispatch(role, host, request, actual, *, returned=None):
            ticket = D.begin(*self.args, role, routing_runtime="codex", requested_model=request,
                             requested_effort="high",
                             native_parameters={"model": request, "reasoning_effort": "high"})
            D.accepted(*self.args, ticket["id"], host, host_executor_id="native-" + host,
                       actual_model=actual, actual_effort="high")
            D.complete(*self.args, ticket["id"], executors_stopped=True, stop_evidence="fixture returned",
                       actual_model=returned)
            return ticket["id"]

        self.start_feedback()
        requirement = report("dual_review", "required", {})
        plan = P.save_plan(*self.args, "plan", {"unit": self.spec})
        solution = report("solution_review", "solution", {
            "gate": "PASS", "requirement_ref": requirement, "plan_ref": plan,
            "dispatch_id": dispatch("scope_guardian", "expert", "test-expert", "test-expert"),
        })
        self.assertEqual(PC.assess(self.root, R.status(self.root)["state"])["workflow_issues"]["verification"],
                         "missing_current_verification_stage")
        report("progress_feedback", "verification", {"milestone": "verification_started",
               "messages": [{"text": "🔵 **[进行中]** 开始验证，下一步检查行为。"}]})
        R.update(*self.args, {"stage": "verification"})
        P.run_planned(*self.args, plan, "unit", "unit")
        args = ("qa", "--receipt", str(self.receipt), "--plan", plan, "--name")
        pending = self.cli(*args, "pending", expected=1)
        self.assertEqual(pending["engineering_gate"], "PASS")
        self.assertEqual(pending["gate"], "CONCERNS")
        verification = {"gate": "PASS", "solution_ref": solution,
                        "checks": self.payload(pending["report"])["checks"]}
        for name, actual, issue in [
            ("unknown", None, "missing_actual_model_or_effort"),
            ("wrong", "test-default", "model_route_mismatch"),
        ]:
            report("verification_review", name, verification | {
                "dispatch_id": dispatch("tester", name, "test-verifier", actual)})
            result = self.cli(*args, "qa-" + name)
            self.assertEqual(result["engineering_gate"], "PASS")
            self.assertEqual(result["supervision"]["process_compliance"]["dispatch_issues"], {"tester": issue})
            with self.assertRaisesRegex(ValueError, issue):
                T.end(*self.args, "audit")
        report("verification_review", "correct", verification | {
            "dispatch_id": dispatch("tester", "correct", "test-verifier", None, returned="test-verifier")})
        result = self.cli(*args, "qa-correct")
        self.assertEqual(result["supervision"]["process_compliance"]["gate"], "PASS")
        self.assertEqual(PC.assess(self.root, R.status(self.root)["state"])["gate"], "PASS")
        self.assertEqual(T.end(*self.args, "audit")["status"], "execution_ended")
        route = configured["runtimes"]["codex"]["tester"]["task_routes"]["high_risk_verification"]
        route["model"] = "new-verifier"
        (self.root / "pipeline.models.json").write_text(json.dumps(configured))
        with self.assertRaisesRegex(ValueError, "tester: model_route_mismatch"):
            self.finish()
        route["model"] = "test-verifier"
        (self.root / "pipeline.models.json").write_text(json.dumps(configured))
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
        args = ("task-start", "--review-risk", "low", "--review-reason", "fixture scoped change", "--receipt", str(self.receipt), "--task-id", "old", "--goal", "old goal", "--accept", "old observed")
        before = R.status(self.root)["state"]
        self.cli(*args)
        self.assertEqual(R.status(self.root)["state"], before)
        T.end(*self.args, "old")
        args = ("task-start", "--review-risk", "low", "--review-reason", "fixture scoped change", "--receipt", str(self.receipt), "--task-id", "new", "--goal", "new goal", "--accept", "new observed")
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
