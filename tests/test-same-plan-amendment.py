"""Same immutable plan can receive a supplemental, independently observed review."""
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location("managed_qa_fixtures", ROOT / "tests/test-managed-qa.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
Q, P, R, RC = fixtures.Q, fixtures.P, fixtures.R, fixtures.RC

class SamePlanAmendmentTests(fixtures.ManagedQaTests):
    def supplement(self):
        plan, original = self.reviewed_plan({"unit": self.spec})
        R.update(*self.args, {"stage": "implementation"})
        leaf = self.review_report("solution_review", "supplement", {
            "gate": "PASS", "dispatch_id": self.review_dispatch("scope_guardian", "supplemental"),
            "requirement_ref": self.payload(original)["requirement_ref"], "amends": original, "plan_ref": plan})
        return plan, original, leaf

    def test_same_plan_preserves_root_requires_current_independent_verification(self):
        plan, original, leaf = self.supplement()
        state = R.status(self.root)["state"]
        self.assertEqual([ref for ref, _, _ in Q.solution_chain(self.root, state)], [original, leaf])
        RC.validate_development(self.root, state)
        P.run_planned(*self.args, plan, "unit", "current")
        refs = P.inspect_plan(*self.args, plan)["checks"]
        self.verified_refs(original, refs, "stale-leaf")
        self.assertEqual(self.payload(P.qa_from_plan(*self.args, plan, "stale"))["gate"], "CONCERNS")
        self.verified_refs(leaf, refs)
        self.assertEqual(self.payload(P.qa_from_plan(*self.args, plan, "current"))["gate"], "PASS")
        self.assertEqual(self.finish()["status"], "completed")

    def test_same_plan_supplement_does_not_turn_failed_check_into_pass(self):
        self.spec = self.spec | {"argv": [fixtures.sys.executable, "-B", "-c", "raise SystemExit(7)"]}
        plan, original, leaf = self.supplement()
        P.run_planned(*self.args, plan, "unit", "actual-failure")
        refs = P.inspect_plan(*self.args, plan)["checks"]
        self.verified_refs(leaf, refs)
        verdict = self.payload(P.qa_from_plan(*self.args, plan, "still-failed"))
        self.assertNotEqual(verdict["gate"], "PASS")
        self.assertEqual(verdict["check_failures"].get("unit"), "check_not_passed")

    def test_consecutive_same_plan_supplements_are_allowed(self):
        plan, original, first = self.supplement()
        second = self.review_report("solution_review", "supplement-again", {
            "gate": "PASS", "dispatch_id": self.review_dispatch("scope_guardian", "second-supplement"),
            "requirement_ref": self.payload(original)["requirement_ref"], "amends": first, "plan_ref": plan})
        self.assertEqual([ref for ref, _, _ in Q.solution_chain(self.root, R.status(self.root)["state"])],
                         [original, first, second])

    def test_cannot_return_to_an_older_plan_after_scope_expansion(self):
        plan, original = self.reviewed_plan({"unit": self.spec})
        (self.root / "extra").write_text("additional checked dependency")
        expanded = P.save_plan(*self.args, "expanded", {"unit": self.spec | {"scope": ["input", "extra"]}})
        first = self.review_report("solution_review", "expanded-review", {
            "gate": "PASS", "dispatch_id": self.review_dispatch("scope_guardian", "expanded-guardian"),
            "requirement_ref": self.payload(original)["requirement_ref"], "amends": original, "plan_ref": expanded})
        self.review_report("solution_review", "old-plan-return", {
            "gate": "PASS", "dispatch_id": self.review_dispatch("scope_guardian", "returning-guardian"),
            "requirement_ref": self.payload(original)["requirement_ref"], "amends": first, "plan_ref": plan})
        self.assertEqual(Q.solution_chain(self.root, R.status(self.root)["state"]), [])

    def test_same_plan_cannot_reuse_guardian_dispatch(self):
        plan, original = self.reviewed_plan({"unit": self.spec})
        self.review_report("solution_review", "replay", self.payload(original) | {"amends": original})
        self.assertEqual(Q.solution_chain(self.root, R.status(self.root)["state"]), [])

    def test_same_plan_cannot_skip_failed_supplement(self):
        plan, original = self.reviewed_plan({"unit": self.spec})
        self.review_report("solution_review", "failed", {"gate": "FAIL"})
        self.review_report("solution_review", "skipped", self.payload(original) | {
            "amends": original, "dispatch_id": self.review_dispatch("scope_guardian", "fresh")})
        self.assertEqual(Q.solution_chain(self.root, R.status(self.root)["state"]), [])

    def test_same_plan_still_detects_mutated_plan(self):
        plan, _, _ = self.supplement()
        file = self.root / plan
        file.write_text(file.read_text().replace('"unit"', '"changed"'))
        with self.assertRaises(ValueError):
            Q.solution_chain(self.root, R.status(self.root)["state"])

if __name__ == "__main__":
    unittest.main()
