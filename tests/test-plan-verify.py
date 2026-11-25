"""Real CLI verify pipeline: strict environment, evidence and original closeout."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
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

    def review_dispatch(self, role, name, host=None):
        args = (self.root, self.run, self.owner)
        dispatch = D.begin(*args, role)["id"]
        D.accepted(*args, dispatch, name, host_executor_id=host or "host-" + name)
        D.complete(*args, dispatch, executors_stopped=True, stop_evidence="fixture review returned")
        return dispatch

    def amendment_retry_fixture(self, *, early=True, narrower=False):
        args = self.root, self.run, self.owner
        E.write_report(*args, "progress_feedback", "opening", {"milestone": "start",
                       "messages": [{"text": "🔵 **[进行中]** 核对变更后的夹具计划。"}]})
        requirement = E.write_report(*args, "dual_review", "required", {})
        parent = E.write_report(*args, "solution_review", "original", {
            "gate": "PASS", "plan_ref": self.plan, "requirement_ref": requirement,
            "dispatch_id": self.review_dispatch("scope_guardian", "original")})
        def start_guardian():
            dispatch = D.begin(*args, "scope_guardian")["id"]
            D.accepted(*args, dispatch, "first-amendment", host_executor_id="host-first-amendment")
            return dispatch
        old_dispatch = start_guardian() if early else None
        spec = self.check | {"coverage": ["other"]} if narrower else self.check
        leaf = P.save_plan(*args, "candidate", {"unit": spec})
        old_dispatch = old_dispatch or start_guardian()
        D.complete(*args, old_dispatch, executors_stopped=True, stop_evidence="fixture returned")
        amendment = {"gate": "PASS", "plan_ref": leaf, "requirement_ref": requirement,
                     "amends": parent, "dispatch_id": old_dispatch}
        old = E.write_report(*args, "solution_review", "first-amendment", amendment)
        retry = amendment | {"retries": old, "dispatch_id": self.review_dispatch("scope_guardian", "new-review")}
        return parent, old, retry

    def retry_solution(self, name, payload):
        result = self.cli("report", "--receipt", self.receipt, "--kind", "solution_review",
                          "--name", name, "--json", json.dumps(payload))
        return result["report"]

    def solution_refs(self):
        import managed_qa
        return [ref for ref, _, _ in managed_qa.solution_chain(self.root, R.status(self.root)["state"])]

    def test_same_candidate_fresh_amendment_retry_and_new_verification(self):
        parent, old, retry = self.amendment_retry_fixture()
        previous_bytes = (self.root / old).read_bytes()
        self.assertEqual(self.solution_refs(), [])
        leaf = self.retry_solution("retry", retry)
        self.assertEqual(self.solution_refs(), [parent, leaf])
        self.assertEqual((self.root / old).read_bytes(), previous_bytes)
        self.plan = retry["plan_ref"]
        proof = self.cli("run", "--receipt", self.receipt, "--plan", self.plan, "--id", "unit",
                         "--name", "actual")["report"]
        judgment = {"gate": "PASS", "solution_ref": old, "checks": [proof],
                    "dispatch_id": self.review_dispatch("tester", "wrong-leaf")}
        self.verification_report("stale", judgment)
        self.cli("qa", "--receipt", self.receipt, "--plan", self.plan, "--name", "stale", expected=1)
        self.verification_report("current", judgment | {"solution_ref": leaf,
                                  "dispatch_id": self.review_dispatch("tester", "current")})
        self.assertEqual(self.cli("qa", "--receipt", self.receipt, "--plan", self.plan,
                                  "--name", "accepted")["gate"], "PASS")

    def test_retry_cannot_replace_a_valid_amendment_or_reduce_coverage(self):
        parent, old, retry = self.amendment_retry_fixture(early=False)
        self.assertEqual(self.solution_refs(), [parent, old])
        self.retry_solution("retry-valid", retry)
        self.assertEqual(self.solution_refs(), [])

    def test_late_tester_cannot_be_the_early_guardian_for_qa_or_supersession(self):
        _, _, retry = self.amendment_retry_fixture()
        leaf = self.retry_solution("new-leaf", retry)
        self.plan = retry["plan_ref"]
        old = self.cli("check", "--receipt", self.receipt, "--name", "wrong-fixture-command",
                       "--scope", "input", "--coverage", "unit", "--", sys.executable, "-B", "-c",
                       "raise SystemExit(2)", expected=1)["report"]
        new = self.cli("run", "--receipt", self.receipt, "--plan", self.plan, "--id", "unit",
                       "--name", "fixed-fixture")["report"]
        shared = "host-first-amendment"
        judgment = {"gate": "PASS", "solution_ref": leaf, "checks": [new],
                    "dispatch_id": self.review_dispatch("tester", "same-host", shared)}
        self.verification_report("not-independent", judgment)
        qa = self.cli("qa", "--receipt", self.receipt, "--plan", self.plan, "--name", "declined", expected=1)
        self.assertIn("dual_review", qa["check_failures"])
        judgment["supersessions"] = [{"obsolete_ref": old, "replacement_ref": new,
                                      "reason": "The fixture runner checks the same input with the correct configuration."}]
        judgment["dispatch_id"] = self.review_dispatch("tester", "new-alias", shared)
        self.verification_report("same-host-disposition", judgment, expected=2)
        judgment["dispatch_id"] = self.review_dispatch("tester", "independent-host")
        self.verification_report("independent-disposition", judgment)
        self.assertEqual(self.cli("qa", "--receipt", self.receipt, "--plan", self.plan,
                                  "--name", "approved")["gate"], "PASS")

    def test_early_guardian_with_unconfirmed_high_risk_route_is_ineligible(self):
        import managed_qa
        import process_compliance
        _, old, retry = self.amendment_retry_fixture()
        self.retry_solution("retry", retry)
        state = R.status(self.root)["state"]
        state["run_review_decision"]["risk"] = "high"
        dispatch = E.read_report(self.root, self.run, old)["payload"]["dispatch_id"]
        with mock.patch.object(process_compliance, "dispatch_issues", wraps=process_compliance.dispatch_issues) as routes:
            self.assertEqual(managed_qa.solution_chain(self.root, state), [])
            checked = routes.call_args.args[2]["scope_guardian"]
            self.assertEqual([item["id"] for item in checked], [dispatch])
            self.assertEqual(process_compliance.dispatch_issues(self.root, state, {"scope_guardian": checked}),
                             {"scope_guardian": "missing_routing_runtime"})

    def test_retry_cannot_drop_parent_check_coverage(self):
        _, _, retry = self.amendment_retry_fixture(narrower=True)
        self.retry_solution("loss", retry)
        self.assertEqual(self.solution_refs(), [])

    def test_retry_rejects_skips_mutations_and_untrusted_reviewers(self):
        parent, old, retry = self.amendment_retry_fixture()
        args = self.root, self.run, self.owner
        alternative = P.save_plan(*args, "other-candidate", {"unit": self.check})
        state_file = self.root / "tmp/pipeline/runs" / (self.run + ".json")
        original = state_file.read_bytes()
        previous = E.read_report(self.root, self.run, old)["payload"]
        for index, mutation in enumerate((
                {"amends": old}, {"retries": parent}, {"plan_ref": alternative},
                {"retries": old.replace(self.run, "0" * 32)}, {"dispatch_id": previous["dispatch_id"]},
                {"requirement_ref": parent}, {"gate": "CONCERNS"}, {"gate": "FAIL"})):
            with self.subTest(mutation=mutation):
                self.retry_solution("bad-" + str(index), retry | mutation)
                self.assertEqual(self.solution_refs(), [])
                state_file.write_bytes(original)
        for gate in ("FAIL", "CONCERNS"):
            middle = self.retry_solution("middle-" + gate.lower(), previous | {"gate": gate})
            for index, skipped in enumerate((old, middle)):
                after = retry | {"retries": skipped, "dispatch_id":
                                 self.review_dispatch("scope_guardian", gate.lower() + str(index))}
                self.retry_solution("after-" + gate.lower() + str(index), after)
                self.assertEqual(self.solution_refs(), [])
            state_file.write_bytes(original)
        leaf = self.retry_solution("retry", retry)
        good_state = json.loads(state_file.read_bytes())
        for ref in (parent, old, leaf, retry["requirement_ref"]):
            state = json.loads(json.dumps(good_state))
            del state["review_digests"][ref]
            R._atomic(state_file, state)
            self.assertEqual(self.solution_refs(), [])
        for index, dispatch_id in enumerate((previous["dispatch_id"], retry["dispatch_id"])):
            state = json.loads(json.dumps(good_state))
            target = next(d for d in state["role_dispatches"] if d["id"] == dispatch_id)
            state["role_dispatches"].append(dict(target, id="fixture-dev", role="developer", executor_id="dev"))
            R._atomic(state_file, state)
            self.assertEqual(self.solution_refs(), [])
        R._atomic(state_file, good_state)
        original_review = (self.root / old).read_bytes()
        (self.root / old).write_bytes(original_review + b" ")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.solution_refs()

    def supersession_fixture(self, *, argv=None, scope=("input",), coverage="unit", intent="test",
                             cwd=".", env=None, after_old=None):
        def report(kind, name, payload):
            return E.write_report(self.root, self.run, self.owner, kind, name, payload)

        report("progress_feedback", "opening", {"milestone": "start",
               "messages": [{"text": "🔵 **[进行中]** 核对测试夹具的检查范围。"}]})
        requirement = report("dual_review", "required", {})
        solution = report("solution_review", "solution", {"gate": "PASS", "plan_ref": self.plan,
                          "requirement_ref": requirement,
                          "dispatch_id": self.review_dispatch("scope_guardian", "planner")})
        argv = argv or [sys.executable, "-B", "-c", "raise SystemExit(3)"]
        old = self.cli("check", "--receipt", self.receipt, "--name", "wrong-command",
                       "--scope", *scope, "--coverage", coverage, "--intent", intent, "--cwd", cwd,
                       "--", *argv, expected=1, env=env)["report"]
        if after_old:
            after_old()
        new = self.cli("run", "--receipt", self.receipt, "--plan", self.plan, "--id", "unit",
                       "--name", "corrected-command")["report"]
        row = {"obsolete_ref": old, "replacement_ref": new,
               "reason": "The corrected invocation checks the fixture input using its current configuration."}
        return {"gate": "PASS", "dispatch_id": self.review_dispatch("tester", "independent"),
                "solution_ref": solution, "checks": [new], "supersessions": [row]}

    def verification_report(self, name, payload, expected=0):
        return self.cli("report", "--receipt", self.receipt, "--kind", "verification_review",
                        "--name", name, "--json", json.dumps(payload), expected=expected)

    def preview(self, expected=0, **kwargs):
        return self.cli("plan-preflight", "--receipt", self.receipt, "--plan", self.plan,
                        "--name", "preview", expected=expected, **kwargs)

    def expanded_runner_fixture(self, *, change_input=True, extra="dependency", **old_options):
        runner = self.root / "input"
        fixed = "from pathlib import Path\nraise SystemExit(0 if Path(__file__).with_name('dependency').exists() else 3)\n"
        runner.write_text("raise SystemExit(3)\n" if change_input else fixed)
        self.check.update(argv=[sys.executable, "-B", str(runner)], scope=["input", extra])

        def correct_fixture():
            if change_input:
                runner.write_text(fixed)
            (self.root / "dependency").touch()

        return self.supersession_fixture(argv=self.check["argv"], after_old=correct_fixture, **old_options)

    def test_corrected_same_runner_with_expanded_scope_seals(self):
        payload = self.expanded_runner_fixture()
        row = payload["supersessions"][0]
        old = self.root / row["obsolete_ref"]
        original = old.read_bytes()
        before = json.loads(original)["payload"]
        after = E.read_report(self.root, self.run, row["replacement_ref"])["payload"]
        self.assertEqual([before["exit_code"], after["exit_code"]], [3, 0])
        for field in ("argv", "cwd", "environment", "intent"):
            self.assertEqual(before[field], after[field], field)
        self.assertNotEqual(before["fingerprint"], E.fingerprint(self.root, before["scope"], before["environment"]))
        self.assertEqual(self.preview(expected=1)["unplanned_checks"], [row["obsolete_ref"]])
        self.verification_report("corrected-inputs", payload)
        self.assertEqual(self.cli("qa", "--receipt", self.receipt, "--plan", self.plan,
                                  "--name", "final")["gate"], "PASS")
        result = self.cli("finish", "--receipt", self.receipt, "--stopped",
                          "--stop-evidence", "all fixture executors returned")
        self.assertTrue(result["delivery_sealed"])
        self.assertEqual(old.read_bytes(), original)

    def test_reviewed_supersession_preserves_evidence_through_finish(self):
        (self.root / "config").write_text("new dependency")
        self.check["scope"].append("config")
        payload = self.supersession_fixture()
        row = payload["supersessions"][0]
        old, new = row["obsolete_ref"], row["replacement_ref"]
        proof = E.read_report(self.root, self.run, old)["payload"]
        old_report = (self.root / old).read_bytes()
        old_log = (self.root / proof["log_path"]).read_bytes()
        self.assertEqual(self.preview(expected=1)["unplanned_checks"], [old])
        ref = self.verification_report("reviewed", payload)["report"]
        stored = E.read_report(self.root, self.run, ref)["payload"]["supersessions"][0]
        for key, path in (("obsolete_sha256", old), ("replacement_sha256", new)):
            self.assertEqual(stored[key], hashlib.sha256((self.root / path).read_bytes()).hexdigest())
        self.assertTrue(self.preview()["ready"])
        qa = self.cli("qa", "--receipt", self.receipt, "--plan", self.plan, "--name", "source")
        self.assertEqual(qa["gate"], "PASS")
        import review_control
        import managed_qa
        state = R.status(self.root)["state"]
        with mock.patch.dict(os.environ, self.env, clear=True):
            review_control.validate_pre_activation(self.root, state | {
                "release_plan": {"required_before": ["unit"], "required_after": [], "attempts": []}})
            self.assertEqual(managed_qa.release_requirements(self.root, state), {"unit"})
        completed = self.cli("finish", "--receipt", self.receipt, "--stopped",
                             "--stop-evidence", "all fixture executors returned")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual((self.root / old).read_bytes(), old_report)
        self.assertEqual((self.root / proof["log_path"]).read_bytes(), old_log)
        self.assertEqual(proof["exit_code"], 3)
        self.assertEqual((self.root / "counter").read_text(), "1")

    def host_supersession_fixture(self, *, first_command="old-unit"):
        args = self.root, self.run, self.owner
        E.write_report(*args, "progress_feedback", "opening", {"milestone": "start",
                       "messages": [{"text": "🔵 **[进行中]** 核对宿主观察夹具。"}]})
        requirement = E.write_report(*args, "dual_review", "required", {})
        (self.root / "config").write_text("fixture")
        def observe(name, command, labels, scope, code):
            spec = {"command": command, "scope": scope, "coverage": labels,
                    "environment": {"fixture": "local-host"}, "write_paths": []}
            self.cli("observe-begin", "--receipt", self.receipt, "--id", name, "--json", json.dumps(spec))
            log = name + ".log"
            (self.root / log).write_text("fixture returned " + str(code))
            return self.cli("observe-finish", "--receipt", self.receipt, "--id", name, "--host-stopped",
                            "--stop-evidence", "fixture finished", "--log-path", log,
                            "--exit-code", str(code))["report"]

        olds = [observe("old-unit", first_command, ["unit"], ["input"], 1),
                observe("old-integration", "old-integration", ["integration"], ["config", "input"], 0)]
        (self.root / "input").write_text("corrected")
        new = observe("verified", "verified", ["integration", "unit"], ["input", "config"], 0)
        proof = E.read_report(self.root, self.run, new)["payload"]
        check = {key: proof[key] for key in ("scope", "coverage", "environment", "intent")}
        self.plan = P.save_plan(*args, "native", {"combined": dict(check, observed_ref=new)})
        solution = E.write_report(*args, "solution_review", "solution", {
            "gate": "PASS", "plan_ref": self.plan, "requirement_ref": requirement,
            "dispatch_id": self.review_dispatch("scope_guardian", "planner")})
        return {"gate": "PASS", "solution_ref": solution, "checks": [new],
            "dispatch_id": self.review_dispatch("tester", "tester"), "supersessions": [
                {"obsolete_ref": old, "replacement_ref": new,
                 "reason": "The corrected host invocation covers both fixture inputs and both checks."}
                for old in olds]}

    def test_authenticated_host_checks_can_share_a_corrected_execution(self):
        self.verification_report("combined", self.host_supersession_fixture())
        self.assertTrue(self.preview()["ready"])
        self.assertEqual(self.cli("qa", "--receipt", self.receipt, "--plan", self.plan,
                                  "--name", "host-final")["gate"], "PASS")

    def test_same_command_host_observation_is_ineligible_for_runner_exception(self):
        payload = self.host_supersession_fixture(first_command="verified")
        self.verification_report("host-same-command", payload, expected=2)

    def ordinary_release_source(self, *, amendments=True, failed_ancestor=False):
        import release_checks as release
        environment = mock.patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        args = self.root, self.run, self.owner
        E.write_report(*args, "progress_feedback", "start", {"milestone": "start",
                       "messages": [{"text": "🔵 **[进行中]** 核对常规修订及发布夹具。"}]})
        requirement = E.write_report(*args, "dual_review", "required", {})
        (self.root / "fixture-config").touch()
        (self.root / "fixture-dependency").touch()
        full_scope = ["input", "fixture-config", "fixture-dependency"]
        command = [sys.executable, "-B", "-c",
                   "from pathlib import Path; raise SystemExit(int(Path('failed-fixture').exists()))"]
        unit = self.check | {"argv": command, "scope": full_scope}
        previous = None

        def reviewed(name, checks):
            nonlocal previous
            plan = P.save_plan(*args, name, checks)
            solution = {"gate": "PASS", "plan_ref": plan, "requirement_ref": requirement,
                        "dispatch_id": self.review_dispatch("scope_guardian", name + "-guardian")}
            if previous:
                solution["amends"] = previous
            previous = E.write_report(*args, "solution_review", name, solution)
            return plan

        if amendments:
            first = reviewed("first", {"unit": unit | {"scope": full_scope if failed_ancestor else full_scope[:1]}})
            if failed_ancestor:
                marker = self.root / "failed-fixture"
                marker.touch()
                self.cli("run", "--receipt", self.receipt, "--plan", first, "--id", "unit", "--name", "failed", expected=1)
                marker.unlink()
            reviewed("second", {"unit": unit | {"scope": full_scope[:2]}})
        phases = {name: {"argv": [sys.executable, "-B", "-c", "print('" + name + "')"],
                         "intent": "check", "scope": ["input"], "coverage": [label], "write_paths": []}
                  for name, label in (("preflight", "release_preflight"), ("compatibility", "runner_compatibility"))}
        self.plan = reviewed("leaf", {"unit": unit, **phases})
        source = self.cli("run", "--receipt", self.receipt, "--plan", self.plan, "--id", "unit",
                          "--name", "source-check")["report"]
        refs = [source]
        for phase, spec in phases.items():
            ref = release.execute(*args, phase, phase=phase, **spec)["report"]
            P.bind_check(*args, self.plan, phase, "bind-" + phase, ref)
            refs.append(ref)
        self.verification_report("review", {"gate": "PASS", "solution_ref": previous, "checks": refs,
                                 "dispatch_id": self.review_dispatch("tester", "independent")})
        result = self.cli("qa", "--receipt", self.receipt, "--plan", self.plan, "--name", "source")
        self.assertEqual(result["gate"], "PASS")
        self.assertFalse(R.status(self.root)["state"].get("supersession_reviews"))
        return args

    def ordinary_build(self, args):
        import release_checks as release
        return release.execute(*args, "build", phase="build", scope=["input"], artifact_paths=["artifact"],
                               argv=[sys.executable, "-c", "from pathlib import Path; Path('artifact').write_text('fixture')"])

    def test_ordinary_reviewed_amendments_release_and_finish(self):
        import managed_qa
        import release_checks as release
        args = self.ordinary_release_source()
        self.assertEqual(managed_qa.release_requirements(self.root, R.status(self.root)["state"]),
                         {"unit", "release_preflight", "runner_compatibility"})
        self.ordinary_build(args)
        release.execute(*args, "switch", phase="activate", scope=["input"], argv=[sys.executable, "-c", "print('fixture')"])
        release.execute(*args, "smoke", phase="smoke", scope=["input"], coverage=["release_identity"],
                        argv=[sys.executable, "-c", "print('verified version')"])
        report = release.closeout(*args)["report"]
        self.assertEqual(E.read_report(self.root, self.run, report)["payload"]["gate"], "PASS")
        completed = self.cli("finish", "--receipt", self.receipt, "--stopped", "--stop-evidence", "fixture returned")
        self.assertTrue(completed["delivery_sealed"])

    def test_ordinary_source_revalidation_excludes_only_official_attempt_refs(self):
        import managed_qa
        args = self.ordinary_release_source(amendments=False)
        built = self.ordinary_build(args)["report"]
        self.assertIn(built, self.preview(expected=1)["unplanned_checks"])
        required, replacements = managed_qa.release_source_scope(self.root, R.status(self.root)["state"])
        self.assertEqual(required, {"unit", "release_preflight", "runner_compatibility", "build"})
        self.assertEqual(replacements, {})
        proof = E.read_report(self.root, self.run, built)["payload"]
        self.cli("check", "--receipt", self.receipt, "--name", "unofficial", "--intent", proof["intent"],
                 "--scope", "input", "--coverage", "build", "--", *proof["argv"])
        with self.assertRaisesRegex(ValueError, "current dual expert QA"):
            managed_qa.release_source_scope(self.root, R.status(self.root)["state"])

    def test_failed_ancestor_does_not_disappear_on_ordinary_reviewed_amendment(self):
        import managed_qa
        self.ordinary_release_source(failed_ancestor=True)
        state = R.status(self.root)["state"]
        first = next(ref for ref in state["check_plans"] if ref.endswith("/first.json"))
        inspected = P.inspect_state(self.root, self.run, self.owner, first, state)
        self.assertEqual(inspected["check_failures"], {"unit": "check_not_passed"})
        with self.assertRaisesRegex(ValueError, "source plan acceptance is incomplete"):
            managed_qa.release_source_scope(self.root, state)

    def test_failed_official_preflight_still_prevents_build(self):
        import managed_qa
        import release_checks as release
        args = self.ordinary_release_source()
        failed = release.execute(*args, "new-preflight", phase="preflight", scope=["input"],
                                 argv=[sys.executable, "-c", "raise SystemExit(7)"])
        self.assertEqual(failed["exit_code"], 7)
        self.assertEqual(managed_qa.release_source_scope(self.root, R.status(self.root)["state"])[1], {})
        with self.assertRaisesRegex(release.PreflightIncomplete, "release_preflight"):
            self.ordinary_build(args)
        self.assertFalse((self.root / "artifact").exists())

    def test_reviewed_amendments_and_actual_release_closeout(self):
        self.reviewed_release()

    def test_attested_ancestor_qa_concern_through_release_and_finish(self):
        self.reviewed_release(archive_qa=True)

    def test_ancestor_qa_rejects_additional_historical_failures_even_when_attested(self):
        self.reviewed_release(archive_qa=True, bad_history=True)

    def test_matching_current_command_does_not_attest_an_unmapped_historical_observation(self):
        self.reviewed_release(archive_qa=True, unmatched=True)

    def test_unsigned_fully_executed_intermediate_plan_through_release_and_finish(self):
        self.reviewed_release(archive_qa=True, intermediate="same")

    def test_intermediate_plan_cannot_drop_existing_obligations_or_use_different_evidence(self):
        for change in ("definition", "source", "reordered", "earlier-required", "earlier-judgment"):
            with self.subTest(change=change):
                case = VerifyCliTests()
                case.setUp()
                try:
                    case.reviewed_release(intermediate=change)
                finally:
                    case.doCleanups()

    def reviewed_release(self, *, archive_qa=False, bad_history=False, unmatched=False, intermediate=None):
        import managed_qa
        import release_checks as F

        args = self.root, self.run, self.owner
        E.write_report(*args, "progress_feedback", "opening", {"milestone": "start",
                       "messages": [{"text": "🔵 **[进行中]** 核对隔离发布夹具。"}]})
        requirement = E.write_report(*args, "dual_review", "required", {})

        def reviewed(name, checks, previous=None):
            ref = P.save_plan(*args, name, checks)
            solution = {"gate": "PASS", "plan_ref": ref, "requirement_ref": requirement,
                        "dispatch_id": self.review_dispatch("scope_guardian", name + "-guardian")}
            if previous:
                solution["amends"] = previous
            return ref, E.write_report(*args, "solution_review", name, solution)

        unused, first = reviewed("initial", {"unit": self.check})
        wrong = dict(self.check, argv=[sys.executable, "-B", "-c", "raise SystemExit(3)"])
        prior, second = reviewed("prior", {"unit": wrong}, first)
        old = self.cli("run", "--receipt", self.receipt, "--plan", prior, "--id", "unit",
                       "--name", "obsolete", expected=1)["report"]
        if archive_qa:
            ancestor, second = reviewed("checked-ancestor", {"unit": self.check}, second)
            checked = self.cli("run", "--receipt", self.receipt, "--plan", ancestor, "--id", "unit",
                               "--name", "ancestor-execution")["report"]
            if unmatched:
                self.cli("check", "--receipt", self.receipt, "--name", "older-preflight",
                         "--scope", "input", "--coverage", "release_preflight", "--intent", "check", "--",
                         sys.executable, "-B", "-c", "print('preflight')")
            self.verification_report("ancestor-verdict", {"gate": "PASS", "solution_ref": second, "checks": [checked],
                                     "dispatch_id": self.review_dispatch("tester", "old-verifier")})
            judgment = self.cli("qa", "--receipt", self.receipt, "--plan", ancestor,
                                "--name", "historical-concerns", expected=1)
            self.assertEqual(judgment["check_failures"], {"unplanned_checks": "current_task_checks_not_in_plan"})
            qa_ref = judgment["report"]
            qa_file = self.root / qa_ref
            original_qa = qa_file.read_bytes()
        hooks = {phase: {"argv": [sys.executable, "-B", "-c", "print('" + phase + "')"],
                         "scope": ["input"], "intent": "check", "coverage": [label], "write_paths": []}
                 for phase, label in (("preflight", "release_preflight"), ("compatibility", "runner_compatibility"))}
        if intermediate:
            if intermediate == "earlier-required":
                P.save_plan(*args, "earlier-requirement", {"compatibility": hooks["compatibility"]})
            unit = self.check | {"timeout": 90} if intermediate == "definition" else self.check
            slots = {"unit": unit, "preflight": hooks["preflight"]}
            if intermediate == "reordered":
                slots = dict(reversed(slots.items()))
            middle = P.save_plan(*args, "intermediate", slots)
            if intermediate == "earlier-judgment":
                self.cli("qa", "--receipt", self.receipt, "--plan", middle, "--name", "intermediate-concern", expected=1)
        self.plan, solution = reviewed("leaf", {"unit": self.check, **hooks}, second)
        new = self.cli("run", "--receipt", self.receipt, "--plan", self.plan, "--id", "unit",
                       "--name", "fixed")["report"]
        refs = [new]
        with mock.patch.dict(os.environ, self.env, clear=True):
            for phase, spec in hooks.items():
                actual = F.execute(*args, "official-" + phase, phase=phase, **spec)["report"]
                P.bind_check(*args, self.plan, phase, "bind-" + phase, actual)
                refs.append(actual)
            if intermediate:
                if intermediate == "source":
                    self.cli("run", "--receipt", self.receipt, "--plan", middle,
                             "--id", "unit", "--name", "different-execution")
                else:
                    P.bind_check(*args, middle, "unit", "middle-unit", new)
                P.bind_check(*args, middle, "preflight", "middle-preflight", refs[1])
            passed = {"gate": "PASS", "solution_ref": solution, "checks": refs,
                      "dispatch_id": self.review_dispatch("tester", "leaf-verifier"),
                      "supersessions": [{"obsolete_ref": old, "replacement_ref": new,
                      "reason": "The amended fixture runs the unit check with the current configuration."}]}
            if archive_qa:
                # Wrong or absent attestations cannot dispose of the actual earlier machine judgment.
                for name, rows in (("absent", None), ("wrong-digest", [{"ref": qa_ref, "sha256": "0" * 64}]),
                                   ("wrong-ref", [{"ref": old, "sha256": hashlib.sha256(original_qa).hexdigest()}])):
                    payload = passed | {"dispatch_id": self.review_dispatch("tester", "review-" + name)}
                    if rows is not None:
                        payload["ancestor_qa"] = rows
                    self.verification_report("verification-" + name, payload)
                    self.cli("qa", "--receipt", self.receipt, "--plan", self.plan, "--name", "qa-" + name)
                    with self.assertRaises(ValueError):
                        managed_qa.release_requirements(self.root, R.status(self.root)["state"])
                if bad_history:
                    for index, change in enumerate((
                            {"gate": "FAIL"}, {"engineering_gate": "FAIL"}, {"missing_checks": ["unit"]},
                            {"check_failures": {"unit": "inputs_changed", "unplanned_checks": "current_task_checks_not_in_plan"}},
                            {"required_checks": []}, {"expert_reviews": []}, {"checks": []},
                            {"visual": {"complete": False}}, {"plan_ref": "unrelated-plan"})):
                        with self.subTest(change=change):
                            doc = json.loads(original_qa)
                            doc["payload"].update(change)
                            R._atomic(qa_file, doc)
                            stamp = {"ref": qa_ref, "sha256": hashlib.sha256(qa_file.read_bytes()).hexdigest()}
                            test = passed | {"ancestor_qa": [stamp], "dispatch_id":
                                             self.review_dispatch("tester", "history-" + str(index))}
                            self.verification_report("history-" + str(index), test)
                            self.cli("qa", "--receipt", self.receipt, "--plan", self.plan, "--name", "history-" + str(index))
                            with self.assertRaises(ValueError):
                                managed_qa.release_requirements(self.root, R.status(self.root)["state"])
                    return
                passed["ancestor_qa"] = [{"ref": qa_ref, "sha256": hashlib.sha256(original_qa).hexdigest()}]
                passed["dispatch_id"] = self.review_dispatch("tester", "final-leaf-verifier")
            self.verification_report("release-reviewed", passed)
            self.cli("qa", "--receipt", self.receipt, "--plan", self.plan, "--name", "source")
            state = R.status(self.root)["state"]
            if intermediate:
                inspected = P.inspect_plan(*args, middle)
                self.assertEqual(inspected["check_failures"], {})
                self.assertEqual(inspected["missing_checks"], ["runner_compatibility"])
                if intermediate != "same":
                    with self.assertRaises(ValueError):
                        managed_qa.release_source_scope(self.root, state)
                    return
            if unmatched:
                with self.assertRaisesRegex(ValueError, "historical source QA"):
                    managed_qa.release_source_scope(self.root, state)
                return
            self.assertEqual(managed_qa.release_requirements(self.root, state),
                             {"unit", "release_preflight", "runner_compatibility"})
            if archive_qa:
                qa_file.write_bytes(original_qa + b" ")
                with self.assertRaises(ValueError):
                    managed_qa.release_source_scope(self.root, state)
                for change in ({"gate": "PASS"}, {"plan_ref": self.plan},
                               {"scope": "engineering_release; pending external outcomes are not passed"}):
                    with self.subTest(edited_after_attestation=change):
                        tampered = json.loads(original_qa)
                        tampered["payload"].update(change)
                        R._atomic(qa_file, tampered)
                        with self.assertRaises(ValueError):
                            managed_qa.release_source_scope(self.root, state)
                qa_file.write_bytes(original_qa)
            F.execute(*args, "build", phase="build", scope=["input"], artifact_paths=["artifact"],
                      argv=[sys.executable, "-c", "from pathlib import Path; Path('artifact').write_text('fixture')"])
            F.execute(*args, "switch", phase="activate", scope=["input"],
                      argv=[sys.executable, "-c", "print('fixture activated')"])
            with self.assertRaisesRegex(ValueError, "incomplete"):
                F.closeout(*args)
            F.execute(*args, "smoke", phase="smoke", scope=["input"], coverage=["release_identity"],
                      argv=[sys.executable, "-c", "print('recorded fixture version')"])
            release = F.closeout(*args)
            acceptance = E.read_report(self.root, self.run, release["report"])["payload"]
            self.assertEqual(acceptance["gate"], "PASS")
            self.assertEqual([r["path"] for r in acceptance["coverage"]["sources"]["unit"]], [new])
            self.assertEqual(self.cli("finish", "--receipt", self.receipt, "--stopped",
                                      "--stop-evidence", "fixture finished")["status"], "completed")

            if archive_qa:
                self.assertEqual(qa_file.read_bytes(), original_qa)
                self.assertEqual(json.loads(original_qa)["payload"]["gate"], "CONCERNS")

    def test_supersession_rejects_inexact_or_unreviewed_mappings(self):
        payload = self.supersession_fixture()
        row = payload["supersessions"][0]
        old, new = row["obsolete_ref"], row["replacement_ref"]
        manual = E.write_report(self.root, self.run, self.owner, "check", "manual",
                                {"command": "manual", "scope": ["input"], "coverage": ["unit"]})
        cases = [
            {"gate": "FAIL"}, {"checks": []}, {"solution_ref": self.plan},
            {"dispatch_id": self.review_dispatch("tester", "alias", "host-planner")},
            {"supersessions": []}, {"supersessions": [row, row]},
            {"supersessions": [row, row | {"obsolete_ref": new, "replacement_ref": old}]},
            *({"supersessions": [row | change]} for change in (
                {"obsolete_ref": new}, {"replacement_ref": old}, {"reason": " "},
                {"unexpected": 1}, {"obsolete_sha256": "0" * 64}, {"replacement_sha256": None},
                {"replacement_ref": manual}, {"obsolete_ref": manual}, {"obsolete_ref": "./" + old},
                {"replacement_ref": new.replace(self.run, "0" * 32)}))]
        for index, change in enumerate(cases):
            with self.subTest(change=change):
                self.verification_report("invalid-" + str(index), payload | change, expected=2)
        ordinary = {k: v for k, v in payload.items() if k != "supersessions"}
        self.verification_report("ordinary", ordinary)
        self.verification_report("recycled-dispatch", payload, expected=2)
        self.assertIn(old, self.preview(expected=1)["unplanned_checks"])

    def test_supersession_rejects_disjoint_scope(self):
        (self.root / "other").write_text("different")
        payload = self.supersession_fixture(scope=["other"])
        self.verification_report("wrong-scope", payload, expected=2)

    def test_supersession_rejects_narrower_scope(self):
        (self.root / "config").write_text("old dependency")
        payload = self.supersession_fixture(scope=["input", "config"])
        self.verification_report("narrowed", payload, expected=2)

    def test_scope_expansion_alone_cannot_attest_same_runner(self):
        (self.root / "unrelated").write_text("unchanged")
        payload = self.expanded_runner_fixture(change_input=False, extra="unrelated")
        row = payload["supersessions"][0]
        before = E.read_report(self.root, self.run, row["obsolete_ref"])["payload"]
        after = E.read_report(self.root, self.run, row["replacement_ref"])["payload"]
        self.assertEqual([before["exit_code"], after["exit_code"]], [3, 0])
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        self.assertEqual(before["fingerprint"], E.fingerprint(self.root, before["scope"], before["environment"]))
        result = self.verification_report("unchanged-input", payload, expected=2)
        self.assertIn("original input scope", result["message"])
        self.assertEqual(self.preview(expected=1)["unplanned_checks"], [row["obsolete_ref"]])

    def test_corrected_same_runner_requires_original_cwd(self):
        (self.root / "other").mkdir()
        payload = self.expanded_runner_fixture(cwd="other")
        self.verification_report("changed-cwd", payload, expected=2)

    def test_corrected_same_runner_requires_complete_environment(self):
        payload = self.expanded_runner_fixture(env=self.env | {"FIXTURE_ORIGINAL_ENV": "yes"})
        self.verification_report("changed-environment", payload, expected=2)

    def test_supersession_cannot_drop_old_coverage(self):
        payload = self.supersession_fixture(coverage="integration")
        self.verification_report("wrong-coverage", payload, expected=2)

    def test_supersession_rejects_same_executed_command_with_different_intent(self):
        payload = self.expanded_runner_fixture(intent="check")
        self.verification_report("same-command", payload, expected=2)

    def test_supersession_does_not_cover_a_later_observation_of_the_old_command(self):
        payload = self.supersession_fixture()
        self.verification_report("mapped", payload)
        old = E.read_report(self.root, self.run, payload["supersessions"][0]["obsolete_ref"])["payload"]
        failed = self.cli("check", "--receipt", self.receipt, "--name", "fresh-failure",
                          "--scope", "input", "--coverage", "unit", "--", *old["argv"], expected=1)
        self.assertEqual(self.preview(expected=1)["unplanned_checks"], [failed["report"]])

    def test_supersession_rejects_replacement_predating_the_obsolete_observation(self):
        payload = self.supersession_fixture()
        old = E.read_report(self.root, self.run, payload["supersessions"][0]["obsolete_ref"])["payload"]
        late = self.cli("check", "--receipt", self.receipt, "--name", "later-command",
                        "--scope", "input", "--coverage", "unit", "--", *old["argv"], expected=1)
        payload["supersessions"][0]["obsolete_ref"] = late["report"]
        payload["dispatch_id"] = self.review_dispatch("tester", "later-reviewer")
        self.verification_report("backwards", payload, expected=2)

    def test_supersession_is_revoked_by_later_failure_of_the_replacement(self):
        self.check["argv"] = [sys.executable, "-B", "-c",
                              "from pathlib import Path; raise SystemExit(int(Path('fail-switch').exists()))"]
        payload = self.supersession_fixture()
        self.verification_report("mapped", payload)
        (self.root / "fail-switch").touch()
        self.cli("run", "--receipt", self.receipt, "--plan", self.plan, "--id", "unit",
                 "--name", "later-failure", expected=1)
        self.assertIn(payload["supersessions"][0]["obsolete_ref"], self.preview(expected=1)["unplanned_checks"])

    def test_supersession_revoked_if_dispatch_or_reviewed_plan_changes(self):
        payload = self.supersession_fixture()
        self.verification_report("judgment", payload)
        old = payload["supersessions"][0]["obsolete_ref"]
        state_path = self.root / "tmp/pipeline/runs" / (self.run + ".json")
        original = state_path.read_bytes()
        state = json.loads(original)
        next(d for d in state["role_dispatches"] if d["id"] == payload["dispatch_id"])["status"] = "failed"
        R._atomic(state_path, state)
        self.assertIn(old, self.preview(expected=1)["unplanned_checks"])
        state_path.write_bytes(original)
        self.cli("plan", "--receipt", self.receipt, "--name", "other-plan",
                 "--json", json.dumps({"checks": {"unit": self.check}}))
        self.assertIn(old, self.preview(expected=1)["unplanned_checks"])

    def test_supersession_rejects_review_before_obsolete_report_registration(self):
        args = self.root, self.run, self.owner
        E.write_report(*args, "progress_feedback", "opening", {"milestone": "start",
                       "messages": [{"text": "🔵 **[进行中]** 核对并发检查夹具。"}]})
        requirement = E.write_report(*args, "dual_review", "required", {})
        solution = E.write_report(*args, "solution_review", "solution", {
            "gate": "PASS", "plan_ref": self.plan, "requirement_ref": requirement,
            "dispatch_id": self.review_dispatch("scope_guardian", "planner")})
        argv = CLI.parser().parse_args(["check", "--project", str(self.root), "--receipt", self.receipt,
                                        "--name", "delayed-old", "--write-path", "isolated-old",
                                        "--scope", "input", "--coverage", "unit", "--",
                                        sys.executable, "-B", "-c", "raise SystemExit(3)"])
        ready, resume = threading.Event(), threading.Event()
        original = E.write_report

        def delay_registration(*args, **kwargs):
            if args[3:5] == ("check", "delayed-old"):
                ready.set()
                if not resume.wait(10):
                    raise TimeoutError("fixture did not release old report")
            return original(*args, **kwargs)

        with ThreadPoolExecutor(max_workers=1) as executor, mock.patch.object(E, "write_report", delay_registration):
            future = executor.submit(CLI.dispatch, argv)
            try:
                self.assertTrue(ready.wait(10))
                new = self.cli("run", "--receipt", self.receipt, "--plan", self.plan, "--id", "unit",
                               "--name", "replacement")["report"]
                tester = self.review_dispatch("tester", "independent")
            finally:
                resume.set()
            result, code = future.result(timeout=10)
        self.assertEqual(code, 1)
        old = result["report"]
        report = E.read_report(self.root, self.run, old)
        started = E.read_report(self.root, self.run, new)["payload"]["started_at"]
        ended = next(d["completed_at"] for d in R.status(self.root)["state"]["role_dispatches"] if d["id"] == tester)
        self.assertLess(report["payload"]["finished_at"], started)
        self.assertLess(started, ended)
        self.assertLess(ended, report["recorded_at"])
        judgment = {"gate": "PASS", "solution_ref": solution, "dispatch_id": tester, "checks": [new],
                    "supersessions": [{"obsolete_ref": old, "replacement_ref": new,
                                       "reason": "The corrected invocation checks the same fixture inputs."}]}
        self.verification_report("too-early", judgment, expected=2)
        self.assertEqual(self.preview(expected=1)["unplanned_checks"], [old])

    def test_supersession_requires_review_of_the_binding_currently_in_use(self):
        payload = self.supersession_fixture()
        self.verification_report("review-a", payload)
        row = payload["supersessions"][0]
        bound = self.cli("plan-bind", "--receipt", self.receipt, "--plan", self.plan,
                         "--id", "unit", "--name", "binding-b", "--check", row["replacement_ref"])
        old = row["obsolete_ref"]
        self.assertIn(old, self.preview(expected=1)["unplanned_checks"])
        binding_path = self.root / bound["planned_report"]
        original = binding_path.read_bytes()
        corrupted = json.loads(original)
        corrupted["payload"]["result"]["exit_code"] = 9
        R._atomic(binding_path, corrupted)
        qa = self.cli("qa", "--receipt", self.receipt, "--plan", self.plan, "--name", "stale-binding", expected=1)
        self.assertIn("unplanned_checks", qa["check_failures"])
        payload["dispatch_id"] = self.review_dispatch("tester", "corrupt-reviewer")
        self.verification_report("corrupt-b", payload, expected=2)
        binding_path.write_bytes(original)
        payload["dispatch_id"] = self.review_dispatch("tester", "new-tester")
        self.verification_report("review-b", payload)
        self.assertTrue(self.preview()["ready"])

    def test_supersession_revalidates_observation_review_and_current_environment(self):
        payload = self.supersession_fixture()
        row = payload["supersessions"][0]
        old, new = row["obsolete_ref"], row["replacement_ref"]
        review = self.verification_report("judgment", payload)["report"]
        self.assertTrue(self.preview()["ready"])
        bindings = R.status(self.root)["state"]["supersession_reviews"][review]["bindings"]
        self.assertEqual(len(bindings), 1)
        bound = self.root / next(iter(bindings))
        initial_binding = bound.read_bytes()
        bound.write_bytes(initial_binding + b" ")
        self.assertIn(old, self.preview(expected=1)["unplanned_checks"])
        bound.write_bytes(initial_binding)
        for path in (old, new):
            proof = E.read_report(self.root, self.run, path)["payload"]
            log = self.root / proof["log_path"]
            original = log.read_bytes()
            log.write_bytes(original + b"changed")
            self.assertIn(old, self.preview(expected=1)["unplanned_checks"])
            log.write_bytes(original)
        self.assertIn(old, self.preview(expected=1, env=self.env | {"FIXTURE_DRIFT": "1"})["unplanned_checks"])
        (self.root / "input").write_text("changed")
        self.assertIn(old, self.preview(expected=1)["unplanned_checks"])
        (self.root / "input").write_text("stable")
        original_report = (self.root / old).read_bytes()
        changed = json.loads(original_report)
        changed["payload"]["intent"] = "baseline"
        R._atomic(self.root / old, changed)
        self.assertIn("supersession", self.preview(expected=2)["message"])
        (self.root / old).write_bytes(original_report)
        state_path = self.root / "tmp/pipeline/runs" / (self.run + ".json")
        original_state = state_path.read_bytes()
        for ref in (payload["solution_ref"], review):
            state = json.loads(original_state)
            del state["review_digests"][ref]
            R._atomic(state_path, state)
            self.assertIn(old, self.preview(expected=1)["unplanned_checks"])
            state_path.write_bytes(original_state)
        self.verification_report("new-concerns", {"gate": "CONCERNS"})
        self.assertIn(old, self.preview(expected=1)["unplanned_checks"])

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
        self.assertEqual(first["results"][0]["planned_report"], second["results"][0]["planned_report"])
        self.assertFalse(any("/artifacts/check_reuse/second-" in ref for ref in R.status(self.root)["state"]["artifacts"]))
        self.assertEqual((self.root / "counter").read_text(), "1")
        self.assertEqual(R.status(self.root)["state"]["status"], "running")
        self.assertNotIn("completion", second)
        self.assertFalse((self.root / "tmp/pipeline/delivery/latest.json").exists())

    def test_verify_cannot_keep_binding_with_unverified_executable(self):
        plan = self.plan
        actual_environment = P.C._environment

        def unavailable(*args, **kwargs):
            return actual_environment(*args, **kwargs) | {"executable_sha256": "unavailable"}

        args = self.root, self.run, self.owner, plan, "unit"
        with mock.patch.object(P.C, "_environment", side_effect=unavailable):
            first = P.run_planned(*args, "one", impact_covered=True, keep_current=True)
            second = P.run_planned(*args, "two", impact_covered=True, keep_current=True)
        self.assertEqual(first["exit_code"], 0)
        self.assertEqual(second["exit_code"], 0)
        self.assertFalse(second["reused"])
        self.assertEqual((self.root / "counter").read_text(), "2")

    def test_bind_earlier_actual_result_without_reexecuting_command(self):
        observed = self.verify("original")["results"][0]["report"]
        new = self.cli("plan", "--receipt", self.receipt, "--name", "handoff",
                       "--json", json.dumps({"checks": {"unit": self.check}}))["plan"]
        self.cli("plan-bind", "--receipt", self.receipt, "--plan", new, "--id", "unit",
                 "--name", "binding", "--check", observed)
        self.plan = new
        self.assertEqual(self.verify("bound")["gate"], "PASS")
        self.assertEqual((self.root / "counter").read_text(), "1")

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

    def test_preflight_reports_missing_solution_alongside_reuse(self):
        E.write_report(self.root, self.run, self.owner, "dual_review", "required", {})
        result = self.cli("plan-preflight", "--receipt", self.receipt,
                          "--plan", self.plan, "--name", "review-preview")
        self.assertEqual(result["review"]["status"], "missing_solution_review")
        self.assertFalse(result["reuse"]["unit"]["reusable"])
        self.assertFalse((self.root / "counter").exists())

    def test_new_plan_reports_review_delta_before_execution(self):
        original = self.plan
        E.write_report(self.root, self.run, self.owner, "solution_review", "reviewed",
                       {"gate": "PASS", "plan_ref": original})
        changed = self.check | {"timeout": 30}
        result = self.cli("plan", "--receipt", self.receipt, "--name", "new-plan",
                          "--json", json.dumps({"checks": {"unit": changed}}))
        delta = result["review_delta"]
        self.assertEqual(delta["reviewed_plan"], original)
        self.assertEqual(delta["execution_changed"], ["unit"])
        self.assertFalse((self.root / "counter").exists())

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
        E.write_report(self.root, self.run, self.owner, "progress_feedback", "opening", {
            "milestone": "start", "messages": [{"text": "🔵 **[进行中]** 开始执行检查夹具。"}]})
        return D.begin(self.root, self.run, self.owner, "developer", work_item="fixture-work")


if __name__ == "__main__":
    unittest.main()
