"""Real CLI verify pipeline: strict environment, evidence and original closeout."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"
sys.path.insert(0, str(SCRIPTS))
import role_dispatch as D
import run_evidence as E
import run_state as R
import run_tasks as T
import check_plan as P
import pipeline_cli as CLI
import work_allocation as W


class VerifyCliTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "PIPELINE.md").write_text("isolated authorized fixture")
        (self.root / "input").write_text("stable")
        self.env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        self.started = self.cli("start", "--review-risk", "low", "--review-reason", "fixture scoped change")
        self.receipt = self.started["receipt"]
        self.cli("policy-ack", "--receipt", self.receipt, "--revision", self.started["policy"]["revision"])
        state = R.status(self.root)["state"]
        self.run, self.owner = state["run_id"], state["owner_token"]
        self.check = {"argv": [sys.executable, "-B", "-c",
                     "from pathlib import Path; p=Path('counter'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"],
                      "scope": ["input"], "coverage": ["unit"], "write_paths": []}
        self._plan = None

    @property
    def plan(self):
        if self._plan is None:
            self._plan = self.cli("plan", "--receipt", self.receipt, "--name", "plan",
                                  "--json", json.dumps({"checks": {"unit": self.check}}))["plan"]
        return self._plan

    @plan.setter
    def plan(self, value):
        self._plan = value

    def cli(self, *args, expected=0, env=None):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "pipeline_cli.py"), *args],
                                 cwd=self.root, env=self.env if env is None else env,
                                 capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def verify(self, name, *args, **kwargs):
        return self.cli("verify", "--receipt", self.receipt, "--plan", self.plan,
                        "--name", name, *args, **kwargs)

    def inline(self, name, *args, check=None, payload=None, **kwargs):
        payload = {"checks": {"unit": check or self.check}} if payload is None else payload
        return self.cli("verify", "--receipt", self.receipt, "--json", json.dumps(payload),
                        "--name", name, *args, **kwargs)

    def finish_args(self):
        return ("--finish", "--direct", "--stopped", "--stop-evidence", "fixture host executors observed stopped")

    def test_execution_qa_and_seal_one_process_without_environment_replay(self):
        result = self.verify("final", *self.finish_args())
        self.assertEqual(result["gate"], "PASS")
        self.assertFalse(result["results"][0]["reused"])
        self.assertTrue(result["completion"]["delivery_sealed"])
        self.assertTrue(result["completion"]["coverage_complete"])
        self.assertEqual(result["completion"]["status"], "completed")
        self.assertIsNone(R.status(self.root)["active"])
        self.assertNotIn(self.owner, json.dumps(result))
        self.assertEqual((self.root / "counter").read_text(), "1")

    def test_inline_baseline_then_changed_source_executes_final_once_and_seals(self):
        argv = [sys.executable, "-B", "-c",
                "from pathlib import Path; p=Path('counter'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1'); "
                "raise SystemExit(int(Path('input').read_text() != 'fixed'))"]
        baseline = self.cli("check", "--receipt", self.receipt, "--name", "before", "--scope", "input",
                            "--coverage", "before-change", "--intent", "baseline", "--", *argv, expected=1)
        original = (self.root / baseline["report"]).read_bytes()
        (self.root / "input").write_text("fixed")
        result = self.inline("after", *self.finish_args(), "--summary", "fixture source fixed; final exit checked",
                             check={**self.check, "argv": argv})
        self.assertFalse(result["results"][0]["reused"])
        self.assertEqual(result["gate"], "PASS")
        self.assertTrue(result["completion"]["coverage_complete"])
        self.assertTrue(result["completion"]["delivery_sealed"])
        self.assertEqual((self.root / "counter").read_text(), "2")
        self.assertEqual(original, (self.root / baseline["report"]).read_bytes())
        self.assertEqual(E.read_report(self.root, self.run, result["plan_ref"], kind="check_plan")["payload"]["required_checks"], ["unit"])
        self.assertNotIn(self.owner, json.dumps(result))

    def test_inline_reuses_only_still_valid_final_evidence(self):
        first = self.verify("original")
        second = self.inline("inline")
        self.assertTrue(second["results"][0]["reused"])
        self.assertEqual(second["results"][0]["report"], first["results"][0]["report"])
        self.assertEqual((self.root / "counter").read_text(), "1")
        (self.root / "input").write_text("changed")
        third = self.inline("changed")
        self.assertFalse(third["results"][0]["reused"])
        self.assertEqual((self.root / "counter").read_text(), "2")

    def test_preflight_blocks_omitted_observation_before_any_new_process(self):
        # Same coverage is insufficient: a different executed command remains
        # a current-task obligation even when all coverage labels are planned.
        self.cli("check", "--receipt", self.receipt, "--name", "extra",
                 "--scope", "input", "--coverage", "unit", "--",
                 sys.executable, "-B", "-c", "print('existing obligation')")
        before = R.status(self.root)["state"]
        preview = self.cli("plan-preflight", "--receipt", self.receipt,
                           "--plan", self.plan, "--name", "readiness", expected=1)
        self.assertFalse(preview["ready"])
        self.assertEqual(len(preview["unplanned_checks"]), 1)
        self.assertEqual(R.status(self.root)["state"]["artifacts"],
                         before["artifacts"] + [self.plan])
        result = self.verify("blocked", expected=1)
        self.assertEqual(result["results"], [])
        self.assertEqual(result["gate"], "CONCERNS")
        self.assertFalse((self.root / "counter").exists())

    def test_preflight_excludes_old_task_observations_without_weakening_current_ones(self):
        self.cli("check", "--receipt", self.receipt, "--name", "old",
                 "--scope", "input", "--coverage", "unit", "--",
                 sys.executable, "-B", "-c", "print('old task only')")
        T.start(self.root, self.run, self.owner, "next", "next goal", ["unit"])
        preview = self.cli("plan-preflight", "--receipt", self.receipt,
                           "--plan", self.plan, "--name", "readiness")
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["unplanned_checks"], [])

    def test_inline_invalid_payloads_stopping_flags_and_names_never_execute(self):
        for index, payload in enumerate(([], {}, {"checks": {}}, {"checks": {"unit": self.check}, "gate": "PASS"},
                {"checks": {"unit": self.check}, "required_checks": ["unknown"]},
                {"checks": {"unit": {**self.check, "intent": "baseline"}}})):
            with self.subTest(payload=payload):
                self.inline(f"bad-json-{index}", payload=payload, expected=2)
        for index, options in enumerate((("--finish",), ("--finish", "--stopped"),
                ("--direct",), ("--stopped",), ("--stop-evidence", "observed"))):
            before = R.status(self.root)["state"]["artifacts"]
            self.inline(f"bad-flags-{index}", *options, expected=2)
            self.assertEqual(R.status(self.root)["state"]["artifacts"], before)
        self.inline("bad/name", expected=2)
        self.inline("a" * 64, expected=2)  # Derived check attempt exceeds the existing label limit.
        invalid = self.cli("verify", "--receipt", self.receipt, "--name", "malformed", "--json", "{", expected=2)
        self.assertNotIn(self.owner, json.dumps(invalid))
        self.assertFalse((self.root / "counter").exists())

    def test_verify_sources_are_required_and_mutually_exclusive(self):
        for sources in ([], ["--plan", self.plan, "--json", "{}"]):
            result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "pipeline_cli.py"), "verify",
                "--receipt", self.receipt, "--name", "invalid", *sources], cwd=self.root,
                env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn(self.owner, result.stdout + result.stderr)
        self.assertFalse((self.root / "counter").exists())

    def test_inline_name_collisions_never_overwrite_or_execute(self):
        before_plan = (self.root / self.plan).read_bytes()
        self.inline("plan", expected=2)
        self.assertEqual(before_plan, (self.root / self.plan).read_bytes())
        qa = E.write_report(self.root, self.run, self.owner, "qa", "occupied", {"gate": "CONCERNS"})
        original = (self.root / qa).read_bytes()
        result = self.inline("occupied", expected=1)
        self.assertEqual(result["results"], [])
        self.assertIsNone(result["report"])
        self.assertEqual(original, (self.root / qa).read_bytes())
        self.assertFalse((self.root / "counter").exists())
        first = self.inline("first")
        attempt = E.write_report(self.root, self.run, self.owner, "check_reuse", "attempt-c1",
                                 {"source": first["results"][0]["report"]})
        original = (self.root / attempt).read_bytes()
        result = self.inline("attempt", expected=1)
        self.assertEqual(result["results"], [])
        self.assertEqual(original, (self.root / attempt).read_bytes())
        self.assertEqual((self.root / "counter").read_text(), "1")

    def test_inline_late_runner_and_resource_preflight_block_first_command(self):
        for name, check in (("missing-runner", {**self.check, "argv": ["pipeline-fixture-missing-runner"]}),
                            ("resources", {**self.check, "write_paths": None, "protected_paths": ["dist"]})):
            result = self.inline(name, payload={"checks": {"unit": self.check, "late": check}}, expected=1)
            self.assertEqual(result["results"], [])
            self.assertEqual(result["gate"], "CONCERNS")
            self.assertFalse((self.root / "counter").exists())

    def test_inline_missing_registered_requirement_and_nonzero_keep_run_active(self):
        self.cli("plan", "--receipt", self.receipt, "--name", "required-other", "--json",
                 json.dumps({"checks": {"other": {**self.check, "coverage": ["other"]}}}))
        missing = self.inline("narrow", *self.finish_args(), expected=1)
        self.assertEqual(missing["preflight"]["missing_checks"], ["other"])
        self.assertEqual(missing["results"], [])
        self.assertFalse((self.root / "counter").exists())
        failed = {**self.check, "coverage": ["unit", "other"],
                  "argv": [sys.executable, "-B", "-c", "raise SystemExit(3)"]}
        result = self.inline("failed", *self.finish_args(), check=failed, expected=1)
        self.assertEqual(result["gate"], "CONCERNS")
        self.assertEqual(result["results"][0]["exit_code"], 3)
        self.assertNotIn("completion", result)
        self.assertIsNotNone(R.status(self.root)["active"])

    def test_inline_stopping_preflight_happens_before_plan_registration(self):
        T.start(self.root, self.run, self.owner, "active", "goal", ["observe"])
        before = R.status(self.root)["state"]["artifacts"]
        self.inline("blocked", *self.finish_args(), expected=2)
        self.assertEqual(before, R.status(self.root)["state"]["artifacts"])
        self.assertFalse((self.root / "counter").exists())

    def test_inline_finish_revalidates_input_log_environment_and_required_coverage(self):
        original = P.verify_plan
        for kind in ("input", "log", "environment", "required"):
            with self.subTest(kind=kind), mock.patch.dict(os.environ, self.env, clear=True):
                def drift(*args, **kwargs):
                    result = original(*args, **kwargs)
                    self.assertEqual(result["gate"], "PASS")
                    if kind == "input":
                        (self.root / "input").write_text("changed after QA")
                    elif kind == "log":
                        proof = E.read_report(self.root, self.run, result["results"][0]["report"])["payload"]
                        (self.root / proof["log_path"]).write_text("changed after QA")
                    elif kind == "environment":
                        os.environ["INLINE_VERIFY_CONTEXT"] = "changed after QA"
                    else:
                        P.save_plan(self.root, self.run, self.owner, "late-required",
                                    {"other": {**self.check, "coverage": ["other"]}})
                    return result
                args = CLI.parser().parse_args(["verify", "--project", str(self.root), "--receipt", self.receipt,
                    "--json", json.dumps({"checks": {"unit": self.check}}), "--name", "drift-" + kind,
                    *self.finish_args()])
                with mock.patch.object(P, "verify_plan", side_effect=drift):
                    with self.assertRaisesRegex(ValueError, "QA evidence changed"):
                        CLI.dispatch(args)
                self.assertIsNotNone(R.status(self.root)["active"])

    def test_valid_existing_evidence_seals_without_another_execution(self):
        self.verify("first")
        result = self.verify("seal", *self.finish_args())
        self.assertTrue(result["results"][0]["reused"])
        self.assertTrue(result["completion"]["delivery_sealed"])
        self.assertEqual((self.root / "counter").read_text(), "1")

    def test_drift_after_same_process_qa_still_blocks_finish(self):
        original = P.verify_plan
        for kind in ("environment", "input"):
            with self.subTest(kind=kind), mock.patch.dict(os.environ, self.env, clear=True):
                def drift(*args, **kwargs):
                    result = original(*args, **kwargs)
                    self.assertEqual(result["gate"], "PASS")
                    if kind == "environment":
                        os.environ["SHLVL"] = str(int(os.environ.get("SHLVL", "0")) + 1)
                    else:
                        (self.root / "input").write_text("changed after QA")
                    return result

                args = CLI.parser().parse_args(["verify", "--project", str(self.root),
                       "--receipt", self.receipt, "--plan", self.plan, "--name", "drift-" + kind,
                       *self.finish_args()])
                with mock.patch.object(P, "verify_plan", side_effect=drift):
                    with self.assertRaisesRegex(ValueError, "QA evidence changed"):
                        CLI.dispatch(args)
                self.assertEqual(R.status(self.root)["state"]["status"], "running")
                self.assertIsNotNone(R.status(self.root)["active"])
                self.assertFalse((self.root / "tmp/pipeline/delivery/latest.json").exists())

    def test_default_keeps_run_active_and_valid_evidence_is_not_executed_again(self):
        first = self.verify("first")
        second = self.verify("second")
        self.assertEqual(first["gate"], "PASS")
        self.assertTrue(second["results"][0]["reused"])
        self.assertEqual((self.root / "counter").read_text(), "1")
        self.assertEqual(R.status(self.root)["state"]["status"], "running")
        self.assertNotIn("completion", second)
        self.assertFalse((self.root / "tmp/pipeline/delivery/latest.json").exists())

    def test_plan_preflight_cli_is_read_only_and_valid_plan_still_executes(self):
        plan = self.plan
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = self.cli("plan-preflight", "--receipt", self.receipt, "--plan", plan, "--name", "ready")
        self.assertTrue(result["ready"])
        self.assertNotIn("gate", result)
        self.assertNotIn("argv", json.dumps(result))
        self.assertNotIn(self.owner, json.dumps(result))
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes()
                                  for p in self.root.rglob("*") if p.is_file()})
        self.assertFalse((self.root / "counter").exists())
        self.assertFalse(result["reuse"]["unit"]["reusable"])
        self.assertEqual(self.verify("ready")["gate"], "PASS")
        args = ("plan-preflight", "--receipt", self.receipt, "--plan", plan, "--name", "preview")
        self.assertTrue(self.cli(*args)["reuse"]["unit"]["reusable"])
        changed = {**self.env, "SHLVL": str(int(self.env.get("SHLVL", "0")) + 1)}
        prediction = self.cli(*args, env=changed)["reuse"]["unit"]
        self.assertFalse(prediction["reusable"])
        self.assertEqual(prediction["reason"], "command_or_environment_changed")
        self.assertEqual((self.root / "counter").read_text(), "1")

    def test_verify_managed_preflight_rejects_missing_coverage_and_late_runner(self):
        missing = {**self.check, "argv": ["pipeline-fixture-no-such-runner"], "coverage": ["late"]}
        self.plan = self.cli("plan", "--receipt", self.receipt, "--name", "late-plan",
                             "--json", json.dumps({"checks": {"unit": self.check, "late": missing}}))["plan"]
        result = self.verify("late", expected=1)
        self.assertEqual(result["gate"], "CONCERNS")
        self.assertEqual(result["results"], [])
        self.assertFalse((self.root / "counter").exists())
        self.plan = self.cli("plan", "--receipt", self.receipt, "--name", "narrow-plan",
                             "--json", json.dumps({"checks": {"unit": self.check}}))["plan"]
        static = self.cli("plan-preflight", "--receipt", self.receipt, "--plan", self.plan,
                          "--name", "narrow", expected=1)
        self.assertEqual(static["missing_checks"], ["late"])
        result = self.verify("narrow", expected=1)
        self.assertEqual(result["gate"], "CONCERNS")
        self.assertEqual(result["preflight"]["missing_checks"], ["late"])
        self.assertEqual(result["missing_checks"], ["late", "unit"])
        self.assertEqual(result["results"], [])
        self.assertFalse((self.root / "counter").exists())
        self.assertEqual(R.status(self.root)["state"]["status"], "running")

    def test_preflight_concerns_and_reason_survive_managed_qa_with_valid_prior_pass(self):
        first = self.verify("first")
        E.write_report(self.root, self.run, self.owner, "check_reuse", "blocked-c1",
                       {"source": first["results"][0]["report"], "coverage": ["unit"]})
        result = self.verify("blocked", expected=1)
        self.assertEqual(result["gate"], "CONCERNS")
        self.assertEqual(result["results"], [])
        self.assertEqual(result["missing_checks"], [])
        qa = E.read_report(self.root, self.run, result["report"], kind="qa")["payload"]
        self.assertEqual(qa["gate"], "CONCERNS")
        self.assertIn("attempt_name_already_used", qa["summary"])
        self.assertEqual((self.root / "counter").read_text(), "1")

    def test_repeated_verify_name_returns_concerns_without_writing_or_executing(self):
        first = self.verify("same-round")
        self.assertEqual(first["gate"], "PASS")
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = self.verify("same-round", expected=1)
        self.assertEqual(result["gate"], "CONCERNS")
        self.assertIsNone(result["report"])
        self.assertEqual(result["results"], [])
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes()
                                  for p in self.root.rglob("*") if p.is_file()})
        self.assertEqual((self.root / "counter").read_text(), "1")

    def test_real_env_including_shlvl_input_and_log_changes_force_execution(self):
        self.verify("first")
        changed = {**self.env, "SHLVL": str(int(self.env.get("SHLVL", "0")) + 1)}
        qa = self.cli("qa", "--receipt", self.receipt, "--plan", self.plan,
                      "--name", "stale-env", env=changed, expected=1)
        self.assertEqual(qa["check_failures"]["unit"], "command_or_environment_changed")
        second = self.verify("new-env", env=changed)
        self.assertFalse(second["results"][0]["reused"])
        self.assertEqual(second["gate"], "PASS")
        (self.root / "input").write_text("changed")
        stale = self.cli("qa", "--receipt", self.receipt, "--plan", self.plan,
                         "--name", "stale-input", env=changed, expected=1)
        self.assertEqual(stale["gate"], "CONCERNS")
        third = self.verify("new-input", env=changed)
        self.assertFalse(third["results"][0]["reused"])
        proof = E.read_report(self.root, self.run, third["results"][0]["report"], kind="check")["payload"]
        (self.root / proof["log_path"]).write_text("tampered")
        fourth = self.verify("new-log", env=changed)
        self.assertFalse(fourth["results"][0]["reused"])
        self.assertEqual((self.root / "counter").read_text(), "4")

    def test_later_failure_prevents_old_pass_reuse_and_stops_heavy_followup(self):
        self.verify("pass")
        proof = E.read_report(self.root, self.run, R.status(self.root)["state"]["artifacts"][1], kind="check")["payload"]
        (self.root / "failed.log").write_text("observed failure")
        E.record_check(self.root, self.run, self.owner, "newer-failure", command=proof["command"],
                       scope=proof["scope"], environment=proof["environment"], exit_code=1,
                       log_path="failed.log", coverage=["other"],
                       started_fingerprint=E.fingerprint(self.root, proof["scope"]))
        stale = self.cli("qa", "--receipt", self.receipt, "--plan", self.plan,
                         "--name", "failure-qa", expected=1)
        self.assertEqual(stale["gate"], "CONCERNS")
        result = self.verify("repair")
        self.assertFalse(result["results"][0]["reused"])
        self.assertEqual((self.root / "counter").read_text(), "2")
        failed = {**self.check, "argv": [sys.executable, "-B", "-c", "raise SystemExit(1)"]}
        heavy = {**self.check, "coverage": ["heavy"], "argv": [sys.executable, "-B", "-c",
                 "from pathlib import Path; Path('heavy-ran').touch()"]}
        self.plan = self.cli("plan", "--receipt", self.receipt, "--name", "failure-plan",
                             "--json", json.dumps({"checks": {"existing": self.check, "first": failed, "heavy": heavy}}))["plan"]
        stopped = self.verify("fail-fast", *self.finish_args(), expected=1)
        self.assertEqual(stopped["skipped_checks"], ["heavy"])
        self.assertEqual(stopped["check_failures"]["heavy"], "not_executed")
        self.assertFalse((self.root / "heavy-ran").exists())
        self.assertEqual(R.status(self.root)["state"]["status"], "running")

    def test_missing_stopping_evidence_is_rejected_before_commands(self):
        for index, args in enumerate((("--finish",), ("--finish", "--stopped"),
                                      ("--direct",), ("--stopped",), ("--stop-evidence", "observed"))):
            with self.subTest(args=args):
                self.verify("invalid" + str(index), *args, expected=2)
                self.assertFalse((self.root / "counter").exists())

    def test_existing_task_role_wait_and_release_guards_remain(self):
        for kind in ("task", "role", "wait", "release", "check", "host"):
            with self.subTest(kind=kind):
                state = R.status(self.root)["state"]
                path = self.root / "tmp/pipeline/runs" / (self.run + ".json")
                original = dict(state)
                if kind == "task":
                    T.start(self.root, self.run, self.owner, "active", "goal", ["observe"])
                elif kind == "role":
                    self.developer_request()
                elif kind == "wait":
                    state["continuity_wait"] = {"status": "waiting"}
                    R._atomic(path, state)
                elif kind == "release":
                    state["release_plan"] = {"closeout": None, "phase": "pending"}
                    R._atomic(path, state)
                elif kind == "check":
                    state["running_checks"] = [{"name": "active", "write_paths": []}]
                    R._atomic(path, state)
                else:
                    state["observed_checks"] = {"host": {"status": "running"}}
                    R._atomic(path, state)
                args = self.finish_args()
                if kind == "role":
                    args = tuple(item for item in args if item != "--direct")
                self.verify("blocked-" + kind, *args, expected=2)
                self.assertFalse((self.root / "counter").exists())
                self.assertEqual(R.status(self.root)["state"]["status"], "running")
                self.assertIsNotNone(R.status(self.root)["active"])
                current = R.status(self.root)["state"]
                for key in ("tasks", "role_dispatches", "role_events", "role_execution", "continuity_wait", "release_plan",
                            "running_checks", "observed_checks", "work_allocation"):
                    if key in original:
                        current[key] = original[key]
                    else:
                        current.pop(key, None)
                R._atomic(path, current)

    def test_observed_delegation_cannot_use_direct_finish(self):
        item = self.developer_request()
        D.complete(self.root, self.run, self.owner, item["id"], executors_stopped=True,
                   stop_evidence="host rejected creation", outcome="failed")
        result = self.verify("delegated", *self.finish_args(), expected=2)
        self.assertIn("cannot be relabeled direct", result["message"])
        self.assertEqual(R.status(self.root)["state"]["role_execution"], "delegated")
        self.assertFalse((self.root / "counter").exists())

    def developer_request(self):
        W.decide(self.root, self.run, self.owner, "serial", "shared_write_paths",
                 [{"id": "fixture-work", "executor_id": "fixture-dev", "write_paths": [], "depends_on": []}])
        return D.begin(self.root, self.run, self.owner, "developer", work_item="fixture-work")


if __name__ == "__main__":
    unittest.main()
