"""Coverage baselines and partial handoff across isolated real run evidence."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import delivery as D
import run_checks as C
import run_evidence as E
import run_state as R

ABSENT = object()


class DeliveryCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "PIPELINE.md").write_text("fixture acceptance contract")
        for category in ("unit", "e2e", "build", "metadata"):
            (self.root / f"{category}.py").write_text(f"{category} fixture input")

    def start(self, predecessor=None):
        return R.start(self.root, {"delivery_from": predecessor} if predecessor is not None else {})

    def record(self, state, category, name=None, coverage=ABSENT):
        name = name or category
        log_path = f"log-{state['run_id']}-{name}.txt"
        (self.root / log_path).write_text(f"observed fixture {name} PASS")
        scope = [f"{category}.py"]
        options = {} if coverage is ABSENT else {"coverage": coverage}
        return E.record_check(self.root, state["run_id"], state["owner_token"], name,
                              command=f"fixture-check-{category}", scope=scope,
                              environment={"tool": "fixture"}, exit_code=0, log_path=log_path,
                              started_fingerprint=E.fingerprint(self.root, scope), **options)

    def finish(self, state, required=ABSENT):
        payload = {"gate": "PASS"}
        if required is not ABSENT:
            payload["required_checks"] = required
        E.write_report(self.root, state["run_id"], state["owner_token"], "qa", "final", payload)
        return R.finish(self.root, state["run_id"], state["owner_token"], "completed",
                        executors_stopped=True, stop_evidence="isolated fixture checks have ended")

    def packet(self, final):
        return D.validated_receipt(self.root, final["delivery_status"]["reference"])

    def test_metadata_only_remains_partial_instead_of_complete_development(self):
        state = self.start()
        self.record(state, "metadata", coverage=["metadata"])
        final = self.finish(state, ["unit", "e2e", "build"])
        self.assertTrue(final["delivery_status"]["sealed"])
        coverage = self.packet(final)["coverage"]
        self.assertEqual(coverage["required"], ["build", "e2e", "unit"])
        self.assertEqual(coverage["covered"], ["metadata"])
        self.assertEqual(coverage["missing"], ["build", "e2e", "unit"])
        self.assertIs(coverage["complete"], False)

    def test_three_stages_preserve_source_checks_and_union_required_baseline(self):
        first = self.start()
        for category in ("unit", "e2e", "build"):
            self.record(first, category, coverage=[category])
        original = self.finish(first, ["unit", "e2e", "build"])
        self.assertTrue(self.packet(original)["coverage"]["complete"])
        middle = self.start("latest")
        self.record(middle, "metadata", coverage=["metadata"])
        intermediate = self.finish(middle, ["metadata"])
        release = self.start("latest")
        final = self.finish(release)
        coverage = self.packet(final)["coverage"]
        self.assertEqual(coverage["required"], ["build", "e2e", "metadata", "unit"])
        self.assertEqual(coverage["covered"], coverage["required"])
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["missing"], [])
        self.assertEqual(len(self.packet(intermediate)["checks"]), 4)
        for label, references in coverage["sources"].items():
            self.assertTrue(references)
            for reference in references:
                payload = D._read_reference(self.root, reference, "check")
                self.assertIn(label, payload["coverage"])
                E.validate_lineage(self.root, payload)
                self.assertEqual(reference["run_id"], release["run_id"])

    def test_one_missing_or_modified_report_preserves_other_valid_categories(self):
        for mode in ("missing", "modified"):
            source = self.start()
            self.record(source, "unit", coverage=["unit"])
            affected = self.record(source, "e2e", coverage=["e2e"])
            self.finish(source, ["unit", "e2e"])
            path = self.root / affected
            if mode == "missing":
                path.unlink()
            else:
                path.write_text(path.read_text() + " ")
            target = self.start("latest")
            status = target["delivery_status"]
            self.assertTrue(status["adopted"])
            self.assertEqual(len(status["checks"]), 1)
            self.assertEqual(len(status["skipped_checks"]), 1)
            self.assertEqual(status["coverage"]["covered"], ["unit"])
            self.assertEqual(status["coverage"]["missing"], ["e2e"])
            self.assertFalse(status["coverage"]["complete"])
            self.finish(target)

    def test_partial_valid_unit_is_inherited_and_missing_categories_can_be_filled(self):
        first = self.start()
        self.record(first, "unit", coverage=["unit"])
        self.finish(first, ["unit", "e2e", "build"])
        next_stage = self.start("latest")
        self.assertTrue(next_stage["delivery_status"]["adopted"])
        self.assertEqual(next_stage["delivery_status"]["coverage"]["covered"], ["unit"])
        self.assertEqual(next_stage["delivery_status"]["coverage"]["missing"], ["build", "e2e"])
        self.assertEqual(len(next_stage["delivery_status"]["checks"]), 1)
        for category in ("e2e", "build"):
            self.record(next_stage, category, coverage=[category])
        final = self.finish(next_stage)
        self.assertTrue(self.packet(final)["coverage"]["complete"])

    def test_changed_input_and_log_remove_only_affected_categories_until_rerun(self):
        first = self.start()
        reports = {category: self.record(first, category, coverage=[category]) for category in ("unit", "e2e", "build")}
        original = self.finish(first, ["unit", "e2e", "build"])
        unit = E.read_report(self.root, first["run_id"], reports["unit"])["payload"]
        (self.root / unit["log_path"]).write_text("altered proof")
        (self.root / "build.py").write_text("changed build input")
        current = self.packet(original)["coverage"]
        self.assertEqual(current["covered"], ["e2e"])
        self.assertEqual(current["missing"], ["build", "unit"])
        self.assertFalse(current["complete"])
        next_stage = self.start("latest")
        self.assertEqual(next_stage["delivery_status"]["coverage"]["missing"], ["build", "unit"])
        self.record(next_stage, "unit", "unit-rerun", coverage=["unit"])
        self.record(next_stage, "build", "build-rerun", coverage=["build"])
        final = self.finish(next_stage)
        self.assertTrue(self.packet(final)["coverage"]["complete"])

    def test_seal_explains_stale_checks_and_does_not_revive_them_after_revert(self):
        state = self.start()
        self.record(state, "unit", coverage=["unit"])
        self.record(state, "metadata", coverage=["metadata"])
        original = (self.root / "unit.py").read_text()
        (self.root / "unit.py").write_text("changed during release preparation")
        final = self.finish(state, ["unit", "metadata"])
        packet = self.packet(final)
        self.assertEqual(packet["coverage"]["missing"], ["unit"])
        excluded = packet["excluded_checks"]
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["coverage"], ["unit"])
        self.assertEqual(excluded[0]["reason"], "inputs_changed")
        self.assertIn(excluded[0], packet["coverage"]["unavailable"])
        self.assertNotIn(excluded[0]["reference"], packet["checks"])
        (self.root / "unit.py").write_text(original)
        adopted = self.start("latest")
        self.assertEqual(adopted["delivery_status"]["coverage"]["missing"], ["unit"])
        self.assertEqual(len(adopted["delivery_status"]["checks"]), 1)
        again = self.finish(adopted)
        self.assertEqual(self.packet(again)["coverage"]["missing"], ["unit"])
        self.assertIn(excluded[0], self.packet(again)["coverage"]["unavailable"])

    def test_relabeling_metadata_as_unit_requires_execution_not_cached_acceptance(self):
        state = self.start()
        argv = [sys.executable, "-c", "from pathlib import Path; p=Path('counter'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"]
        options = dict(argv=argv, scope=["metadata.py"], impact_covered=True)
        metadata = C.execute_check(self.root, state["run_id"], state["owner_token"], "metadata", coverage=["metadata"], **options)
        old = E.read_report(self.root, state["run_id"], metadata["report"])["payload"]
        self.assertEqual(E.reusable_check(self.root, state["run_id"], metadata["report"], command=old["command"],
                                          environment=old["environment"], impact_covered=True, coverage=["unit"])["reason"], "coverage_changed")
        unit = C.execute_check(self.root, state["run_id"], state["owner_token"], "unit", coverage=["unit"], **options)
        self.assertFalse(unit["reused"])
        self.assertEqual((self.root / "counter").read_text(), "2")
        reused = C.execute_check(self.root, state["run_id"], state["owner_token"], "unit-again", coverage=["unit", "unit"], **options)
        self.assertTrue(reused["reused"])
        self.assertEqual((self.root / "counter").read_text(), "2")
        final = self.finish(state, ["unit", "metadata"])
        self.assertTrue(self.packet(final)["coverage"]["complete"])

    def test_legacy_unlabeled_checks_and_receipts_have_unknown_completeness(self):
        state = self.start()
        self.record(state, "metadata")
        final = self.finish(state)
        coverage = self.packet(final)["coverage"]
        self.assertIsNone(coverage["required"])
        self.assertIsNone(coverage["complete"])
        self.assertIsNone(coverage["missing"])
        reference = final["delivery_status"]["reference"]
        path = self.root / reference["path"]
        envelope = json.loads(path.read_text())
        envelope["payload"].pop("coverage")
        path.write_text(json.dumps(envelope))
        legacy = D._reference(self.root, state["run_id"], reference["path"])
        inherited = self.start(legacy)
        self.assertTrue(inherited["delivery_status"]["adopted"])
        self.assertIsNone(inherited["delivery_status"]["coverage"]["complete"])
        self.assertIsNone(inherited["inherited_required_checks"])

    def test_omitted_empty_or_metadata_only_qa_never_clears_inherited_functional_requirements(self):
        first = self.start()
        self.record(first, "unit", coverage=["unit"])
        self.finish(first, ["unit", "e2e", "build"])
        for required in (ABSENT, [], ["metadata"]):
            state = self.start("latest")
            final = self.finish(state, required)
            coverage = self.packet(final)["coverage"]
            self.assertTrue({"unit", "e2e", "build"}.issubset(coverage["required"]))
            self.assertFalse(coverage["complete"])
            self.assertTrue({"build", "e2e"}.issubset(coverage["missing"]))

    def test_newer_failure_with_different_labels_does_not_revive_old_pass(self):
        state = self.start()
        unit_path = self.record(state, "unit", coverage=["unit"])
        unit = E.read_report(self.root, state["run_id"], unit_path)["payload"]
        failed = dict(deepcopy(unit), exit_code=1, coverage=["metadata"])
        E.write_report(self.root, state["run_id"], state["owner_token"], "check", "later-failure", failed)
        self.record(state, "build", coverage=["build"])
        final = self.finish(state, ["unit", "build"])
        coverage = self.packet(final)["coverage"]
        self.assertEqual(coverage["covered"], ["build"])
        self.assertEqual(coverage["missing"], ["unit"])
        self.assertNotIn("unit", coverage["sources"])

    def test_imported_source_cannot_be_retagged_in_place(self):
        first = self.start()
        self.record(first, "metadata", coverage=["metadata"])
        self.finish(first, ["unit", "metadata"])
        second = self.start("latest")
        relative = second["delivery_status"]["checks"][0]
        path = self.root / relative
        envelope = json.loads(path.read_text())
        envelope["payload"]["coverage"] = ["unit"]
        path.write_text(json.dumps(envelope))
        payload = envelope["payload"]
        self.assertFalse(E.reusable_check(self.root, second["run_id"], relative, command=payload["command"],
                                          environment=payload["environment"], impact_covered=True, coverage=["unit"])["reusable"])
        self.record(second, "build", coverage=["build"])
        coverage = self.packet(self.finish(second))["coverage"]
        self.assertIn("unit", coverage["missing"])
        self.assertNotIn("unit", coverage["covered"])

    def test_invalid_coverage_labels_are_rejected_before_execution_or_recording(self):
        state = self.start()
        for index, invalid in enumerate(("unit", ["../unit"], ["Unit"], [""], [1])):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.record(state, "unit", f"bad{index}", coverage=invalid)
                with self.assertRaises(ValueError):
                    C.execute_check(self.root, state["run_id"], state["owner_token"], f"exec{index}",
                                    argv=[sys.executable, "-c", "open('must-not-run','w').close()"],
                                    scope=["unit.py"], coverage=invalid)
        self.assertFalse((self.root / "must-not-run").exists())
        self.assertEqual(R.status(self.root)["state"]["artifacts"], [])


if __name__ == "__main__":
    unittest.main()
