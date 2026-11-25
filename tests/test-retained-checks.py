"""Historical source observations may be retained only by the current independent tester."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import runpy
import sys
import unittest

fixture = runpy.run_path(str(Path(__file__).with_name("test-plan-verify.py")))
import check_plan as P
import managed_qa as Q
import release_checks as F
import run_evidence as E
import run_state as R


class RetainedChecksTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture["VerifyCliTests"]()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        c = self.case
        self.args = c.root, c.run, c.owner
        E.write_report(*self.args, "progress_feedback", "opening", {"milestone": "start",
                       "messages": [{"text": "🔵 **[进行中]** 核对本地验证。"}]})
        self.requirement = E.write_report(*self.args, "dual_review", "requirement", {})
        self.solution = None

    def plan(self, name, slots):
        c = self.case
        ref = P.save_plan(*self.args, name, slots)
        judgment = {"gate": "PASS", "plan_ref": ref, "requirement_ref": self.requirement,
                    "dispatch_id": c.review_dispatch("scope_guardian", name + "-guardian")}
        if self.solution:
            judgment["amends"] = self.solution
        self.solution = E.write_report(*self.args, "solution_review", name, judgment)
        self.leaf = ref
        return ref

    def run_slot(self, key, name, expected=0):
        c = self.case
        return c.cli("run", "--receipt", c.receipt, "--plan", self.leaf, "--id", key,
                     "--name", name, expected=expected)["report"]

    def digest(self, ref):
        return hashlib.sha256((self.case.root / ref).read_bytes()).hexdigest()

    def prepare(self, *, repair=True, fake=False, old_context=None, narrowed=False, duplicate=False):
        c = self.case
        condition = "Path('input').read_text() == 'stable'" if repair else "not Path('switch').exists()"
        runner = {"argv": [sys.executable, "-B", "-c",
                  "from pathlib import Path; raise SystemExit(int(" + condition + "))"],
                  "scope": ["input"], "coverage": ["unit"]}
        historical = dict(runner)
        if old_context:
            historical["environment"] = old_context
        if narrowed:
            historical["coverage"] = ["original", "unit"]
        main = dict(c.check, coverage=historical["coverage"])
        hook = {"argv": [sys.executable, "-B", "-c", "print('preflight')"], "scope": ["input"],
                "intent": "check", "coverage": ["release_preflight"]}
        self.plan("initial", {"unit": historical})
        self.old = self.run_slot("unit", "failed-unit", expected=1)
        (c.root / "switch").touch()
        if repair:
            (c.root / "input").write_text("corrected input")
        self.passed = self.run_slot("unit", "historical-pass")
        self.plan("ancestor", {"unit": main})
        main_ref = self.run_slot("unit", "main")
        F.execute(*self.args, "history-compat", phase="compatibility", scope=["input"],
                  argv=[sys.executable, "-B", "-c", "print('compatibility')"])
        if fake:
            c.cli("check", "--receipt", c.receipt, "--name", "fake", "--scope", "input",
                  "--coverage", "release_preflight", "--intent", "check", "--", *hook["argv"])
        c.verification_report("ancestor", {"gate": "PASS", "solution_ref": self.solution, "checks": [main_ref],
                              "dispatch_id": c.review_dispatch("tester", "old-tester")})
        qa = c.cli("qa", "--receipt", c.receipt, "--plan", self.leaf, "--name", "concern", expected=1)
        self.assertEqual(qa["check_failures"], {"unplanned_checks": "current_task_checks_not_in_plan"})
        self.qa = qa["report"]
        self.original = (c.root / self.qa).read_bytes()
        # Both old bindings become stale. The old key now runs a different command.
        if repair:
            (c.root / "input").write_text("final input")
        compat = dict(hook, argv=[sys.executable, "-B", "-c", "print('compatibility')"],
                      coverage=["runner_compatibility"])
        slots = {"unit": main, "retained-unit": runner, "preflight": hook, "compat": compat}
        if duplicate:
            slots["ambiguous"] = runner
        self.plan("leaf", slots)
        self.new_main = self.run_slot("unit", "new-main")
        self.new = self.run_slot("retained-unit", "new-unit")
        pre = F.execute(*self.args, "preflight", phase="preflight", **hook)["report"]
        P.bind_check(*self.args, self.leaf, "preflight", "bind-preflight", pre)
        current_compat = F.execute(*self.args, "current-compat", phase="compatibility", **compat)["report"]
        P.bind_check(*self.args, self.leaf, "compat", "bind-compat", current_compat)
        refs = [self.new_main, self.new, pre, current_compat]
        if duplicate:
            P.bind_check(*self.args, self.leaf, "ambiguous", "ambiguous", self.new)
            refs.append(self.new)
        self.judgment = {"gate": "PASS", "solution_ref": self.solution, "checks": refs,
                         "ancestor_qa": [{"ref": self.qa, "sha256": self.digest(self.qa)}],
                         "retained_checks": [self.pair(old, self.new) for old in (self.old, self.passed)]}

    def pair(self, old, new):
        return {"obsolete_ref": old, "obsolete_sha256": self.digest(old),
                "replacement_ref": new, "replacement_sha256": self.digest(new)}

    def publish(self, name, *, host=None, **changes):
        c = self.case
        judgment = dict(self.judgment, dispatch_id=c.review_dispatch("tester", name, host))
        judgment.update(changes)
        c.verification_report(name, judgment)
        c.cli("qa", "--receipt", c.receipt, "--plan", self.leaf, "--name", name)
        return R.status(c.root)["state"]

    def test_exact_reviewed_checks_closeout_and_finish(self):
        self.prepare()
        c = self.case
        state = self.publish("leaf-tester")
        required, replaced = Q.release_source_scope(c.root, state)
        self.assertIn("unit", required)
        self.assertEqual(replaced, {})
        F.execute(*self.args, "build", phase="build", scope=["input"], artifact_paths=["artifact"],
                  argv=[sys.executable, "-c", "from pathlib import Path; Path('artifact').write_text('ok')"])
        F.execute(*self.args, "activate", phase="activate", scope=["input"], argv=[sys.executable, "-c", "print(1)"])
        F.execute(*self.args, "smoke", phase="smoke", scope=["input"], coverage=["release_identity"],
                  argv=[sys.executable, "-c", "print(2)"])
        closed = F.closeout(*self.args)
        self.assertEqual(E.read_report(c.root, c.run, closed["report"])["payload"]["gate"], "PASS")
        self.assertTrue(c.cli("finish", "--receipt", c.receipt, "--stopped",
                              "--stop-evidence", "fixture returned")["delivery_sealed"])
        self.assertEqual((c.root / self.qa).read_bytes(), self.original)

    def test_missing_extra_duplicate_unpinned_or_wrong_source_fails(self):
        self.prepare()
        row, other = self.judgment["retained_checks"]
        variants = [
            {"retained_checks": [row]}, {"retained_checks": [row, other, row]},
            {"retained_checks": []}, {"ancestor_qa": []},
            {"ancestor_qa": [{"ref": self.qa, "sha256": "0" * 64}]},
            {"retained_checks": [row, self.pair(self.passed, self.new_main)]},
            {"retained_checks": [row, other, self.pair(self.new_main, self.new)]},
            *({"retained_checks": [row, other | {field: "0" * 64}]}
              for field in ("obsolete_sha256", "replacement_sha256"))]
        for i, change in enumerate(variants):
            with self.subTest(change=change):
                state = self.publish("invalid-" + str(i), **change)
                with self.assertRaises(ValueError):
                    Q.release_source_scope(self.case.root, state)

    def test_pinned_qa_metadata_and_leaf_binding_are_rechecked(self):
        self.prepare()
        c = self.case
        state = self.publish("tester")
        for change in ({"gate": "PASS"}, {"plan_ref": self.leaf},
                       {"scope": "engineering_release; pending external outcomes are not passed"}):
            tampered = json.loads(self.original)
            tampered["payload"].update(change)
            R._atomic(c.root / self.qa, tampered)
            with self.assertRaises(ValueError):
                Q.release_source_scope(c.root, state)
        (c.root / self.qa).write_bytes(self.original)
        P.bind_check(*self.args, self.leaf, "retained-unit", "late", self.new)
        with self.assertRaisesRegex(ValueError, "before tester acceptance"):
            Q.release_source_scope(c.root, R.status(c.root)["state"])

    def test_real_ledger_provenance_and_independent_dispatch_are_revalidated(self):
        self.prepare()
        c = self.case
        original = self.publish("leaf-tester")
        for change in ("ledger-phase", "ledger-result", "ledger-time", "log", "tester-time", "tester-host"):
            with self.subTest(change=change):
                state = deepcopy(original)
                attempt = next(a for a in state["release_plan"]["attempts"] if a["name"] == "history-compat")
                tester = next(d for d in state["role_dispatches"] if d["executor_id"] == "leaf-tester")
                if change == "ledger-phase":
                    attempt["phase"] = "build"
                elif change == "ledger-result":
                    attempt["result"]["exit_code"] = 7
                elif change == "ledger-time":
                    attempt["finished_at"] = tester["accepted_at"]
                elif change == "log":
                    span = next(x for x in state["operation_spans"] if x["label"] == "history-compat")
                    span["status"] = "failed"
                elif change == "tester-time":
                    tester["accepted_at"] = Q._read(c.root, state, self.qa)["recorded_at"]
                else:
                    tester["host_executor_id"] = "host-leaf-guardian"
                with self.assertRaises(ValueError):
                    Q.release_source_scope(c.root, state)

    def test_source_failure_cannot_be_disposed_even_if_tester_pins_its_bytes(self):
        self.prepare()
        c = self.case
        qa = json.loads(self.original)
        qa["payload"]["gate"] = "FAIL"
        R._atomic(c.root / self.qa, qa)
        state = self.publish("tester", ancestor_qa=[{"ref": self.qa, "sha256": self.digest(self.qa)}])
        with self.assertRaisesRegex(ValueError, "historical source QA"):
            Q.release_source_scope(c.root, state)

    def test_labeled_but_unregistered_preflight_remains_a_historical_obligation(self):
        self.prepare(fake=True)
        with self.assertRaisesRegex(ValueError, "historical source QA"):
            Q.release_source_scope(self.case.root, self.publish("tester"))

    def test_failure_on_unchanged_scoped_inputs_remains_unresolved(self):
        self.prepare(repair=False)
        with self.assertRaisesRegex(ValueError, "exact original check"):
            Q.release_source_scope(self.case.root, self.publish("tester"))

    def test_changed_environment_cannot_dispose_of_original_check(self):
        self.prepare(old_context={"scope": "other"})
        with self.assertRaisesRegex(ValueError, "exact original check"):
            Q.release_source_scope(self.case.root, self.publish("tester"))

    def test_narrower_coverage_cannot_dispose_of_original_check(self):
        self.prepare(narrowed=True)
        with self.assertRaisesRegex(ValueError, "exact original check"):
            Q.release_source_scope(self.case.root, self.publish("tester"))

    def test_ambiguous_leaf_identity_cannot_dispose_of_original_check(self):
        self.prepare(duplicate=True)
        with self.assertRaisesRegex(ValueError, "one current leaf slot"):
            Q.release_source_scope(self.case.root, self.publish("tester"))


if __name__ == "__main__":
    unittest.main()
