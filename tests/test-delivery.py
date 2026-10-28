"""Development/commit/release continuation in isolated projects, no deployments."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import delivery as D
import run_checks as C
import run_evidence as E
import run_state as R
from input_fingerprint import dependency_identity


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / "PIPELINE.md").write_text("approved fixture contract")
        (self.root / "source.py").write_text("value = 1")
        (self.root / "browser-case.mjs").write_text("existing validated fixture")
        self.argv = [sys.executable, "-c", "from pathlib import Path; p=Path('count'); p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"]

    def start(self, **initial):
        return R.start(self.root, initial)

    def execute(self, state, name, **kwargs):
        options = dict(argv=self.argv, scope=["source.py"], impact_covered=True)
        options.update(kwargs)
        return C.execute_check(self.root, state["run_id"], state["owner_token"], name, **options)

    def approve(self, state, gate="PASS", name="final"):
        return E.write_report(self.root, state["run_id"], state["owner_token"], "qa", name, {"gate": gate})

    def finish(self, state, outcome="completed"):
        return R.finish(self.root, state["run_id"], state["owner_token"], outcome,
                        executors_stopped=True, stop_evidence="all fixture commands completed")

    def accepted_source(self, **options):
        state = self.start(handoff_files=["browser-case.mjs"])
        result = self.execute(state, "development", **options)
        self.approve(state)
        final = self.finish(state)
        self.assertTrue(final["delivery_status"]["sealed"])
        return state, result, final["delivery_status"]["reference"]

    def test_finish_seals_and_next_explicit_phase_reuses_without_running_again(self):
        source, original, reference = self.accepted_source()
        target = self.start(stage="release", delivery_from="latest")
        self.assertTrue(target["delivery_status"]["adopted"])
        self.assertTrue(target["delivery_status"]["assets"][0]["unchanged"])
        result = self.execute(target, "release-source-check")
        self.assertTrue(result["reused"])
        self.assertEqual((self.root / "count").read_text(), "1")
        summary = C.check_summary(self.root)
        self.assertEqual((summary["executed"], summary["reused"], summary["inherited"]), (0, 1, 1))
        inherited = E.read_report(self.root, target["run_id"], result["report"])["payload"]
        self.assertEqual(inherited["inherited_from"]["check"]["run_id"], source["run_id"])
        with self.assertRaises(ValueError):
            E.read_report(self.root, target["run_id"], original["report"])

    def test_repeated_same_run_call_automatically_finds_valid_evidence(self):
        state = self.start()
        self.execute(state, "first")
        self.assertTrue(self.execute(state, "second")["reused"])
        self.assertEqual((self.root / "count").read_text(), "1")

    def test_contract_change_has_specific_reuse_reason_without_weakening_validation(self):
        self.accepted_source()
        target = self.start(delivery_from="latest")
        (self.root / "PIPELINE.md").write_text("changed authorization and acceptance contract")
        result = self.execute(target, "after-contract-change")
        self.assertFalse(result["reused"])
        self.assertEqual(result["reuse_decision"]["reason"], "delivery_contract_changed")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual((self.root / "count").read_text(), "2")

    def test_new_unrelated_work_does_not_adopt_latest_delivery(self):
        self.accepted_source()
        target = self.start()
        self.assertNotIn("delivery_status", target)
        self.assertFalse(self.execute(target, "new-task")["reused"])

    def test_source_edit_environment_change_and_missing_log_require_new_execution(self):
        source, result, _ = self.accepted_source()
        target = self.start(delivery_from="latest")
        (self.root / "source.py").write_text("value = 2")
        self.assertFalse(self.execute(target, "changed")["reused"])
        self.assertFalse(self.execute(target, "environment", environment={"runtime": "changed"})["reused"])
        original = E.read_report(self.root, source["run_id"], result["report"])["payload"]
        (self.root / original["log_path"]).unlink()
        (self.root / "source.py").write_text("value = 1")
        self.assertFalse(self.execute(target, "missing-log")["reused"])

    def test_failed_acceptance_or_only_failed_checks_do_not_publish_delivery(self):
        for gate, argv in [("FAIL", self.argv), ("PASS", [sys.executable, "-c", "raise SystemExit(1)"])]:
            state = self.start()
            self.execute(state, "command", argv=argv)
            self.approve(state, gate)
            self.assertFalse(self.finish(state)["delivery_status"]["sealed"])
            self.assertTrue(json.loads((self.root / D.POINTER).read_text())["blocked"])

    def test_newer_failed_or_paused_delivery_blocks_silent_fallback_to_older_pass(self):
        self.accepted_source()
        failed = self.start()
        self.execute(failed, "current")
        self.approve(failed, "FAIL")
        self.finish(failed)
        next_run = self.start(delivery_from="latest")
        self.assertFalse(next_run["delivery_status"]["adopted"])
        self.assertFalse(self.execute(next_run, "must-check")["reused"])
        self.finish(next_run, "paused")
        after_pause = self.start(delivery_from="latest")
        self.assertFalse(after_pause["delivery_status"]["adopted"])

    def test_latest_failed_command_prevents_reusing_an_earlier_pass(self):
        state = self.start()
        self.execute(state, "passed")
        # Same command identity with a later observed failing exit is not sealed as a PASS.
        old = E.read_report(self.root, state["run_id"], R.status(self.root)["state"]["artifacts"][0])["payload"]
        failed = deepcopy(old)
        failed["exit_code"] = 1
        E.write_report(self.root, state["run_id"], state["owner_token"], "check", "later-failure", failed)
        self.approve(state)
        self.assertFalse(self.finish(state)["delivery_status"]["sealed"])

    def test_contract_changed_and_cross_project_copy_are_not_adopted(self):
        _, _, reference = self.accepted_source()
        (self.root / "PIPELINE.md").write_text("changed acceptance contract")
        target = self.start(delivery_from="latest")
        self.assertFalse(target["delivery_status"]["adopted"])
        self.finish(target, "aborted")
        (self.root / "PIPELINE.md").write_text("approved fixture contract")
        with tempfile.TemporaryDirectory() as other:
            other_root = Path(other).resolve()
            shutil.copytree(self.root / "tmp", other_root / "tmp")
            (other_root / "PIPELINE.md").write_text("approved fixture contract")
            copied = R.start(other_root, {"delivery_from": reference})
            self.assertFalse(copied["delivery_status"]["adopted"])

    def test_source_report_or_approval_tampering_invalidates_inherited_result(self):
        source, original, reference = self.accepted_source()
        target = self.start(delivery_from="latest")
        path = self.root / original["report"]
        content = path.read_text()
        path.write_text(content + " ")
        self.assertFalse(self.execute(target, "modified-proof")["reused"])

    def test_changed_recipe_is_marked_invalid_without_discarding_source_checks(self):
        self.accepted_source()
        (self.root / "browser-case.mjs").write_text("changed recipe")
        state = self.start(delivery_from="latest")
        self.assertTrue(state["delivery_status"]["adopted"])
        self.assertFalse(state["delivery_status"]["assets"][0]["unchanged"])
        self.assertTrue(self.execute(state, "still-valid-source")["reused"])

    def test_three_stage_provenance_chain_and_unknown_legacy_receipts(self):
        self.accepted_source()
        middle = self.start(delivery_from="latest")
        self.assertTrue(self.execute(middle, "commit-phase")["reused"])
        self.approve(middle)
        self.finish(middle)
        release = self.start(delivery_from="latest")
        self.assertEqual(len(release["delivery_status"]["assets"]), 1)
        self.assertTrue(release["delivery_status"]["assets"][0]["unchanged"])
        self.assertTrue(self.execute(release, "publish-phase")["reused"])
        self.assertEqual((self.root / "count").read_text(), "1")
        self.finish(release, "aborted")
        (self.root / D.POINTER).write_text('{"legacy":"PASS"}')
        state = self.start(delivery_from="latest")
        self.assertFalse(state["delivery_status"]["adopted"])
        self.assertFalse(self.execute(state, "legacy")["reused"])

    def test_changed_inherited_asset_is_not_recertified_by_an_intermediate_stage(self):
        self.accepted_source()
        middle = self.start(delivery_from="latest")
        (self.root / "browser-case.mjs").write_text("modified without new browser acceptance")
        self.execute(middle, "commit")
        self.approve(middle)
        self.finish(middle)
        release = self.start(delivery_from="latest")
        self.assertFalse(release["delivery_status"]["assets"][0]["unchanged"])

    def test_partial_adoption_failure_never_registers_orphan_results(self):
        self.accepted_source()
        original = D.R._atomic
        def fail_import(path, data):
            if "/artifacts/check/inherited-" in str(path):
                raise OSError("fixture write failure")
            return original(path, data)
        with patch.object(D.R, "_atomic", side_effect=fail_import):
            target = self.start(delivery_from="latest")
        self.assertFalse(target["delivery_status"]["adopted"])
        self.assertEqual(target["artifacts"], [])
        self.assertFalse(self.execute(target, "fallback")["reused"])

    def test_version_bump_reuses_version_independent_source_but_rebuilds_metadata(self):
        package = self.root / "package.json"
        lock = self.root / "package-lock.json"
        def version(value):
            package.write_text(json.dumps({"name": "fixture", "version": value, "dependencies": {"lib": "1"}}))
            lock.write_text(json.dumps({"name": "fixture", "version": value, "lockfileVersion": 3,
                                        "packages": {"": {"version": value}, "node_modules/lib": {"version": "1"}}}))
        version("1.0.0")
        manifests = ["package.json", "package-lock.json"]
        environment = {"dependencies": dependency_identity(self.root, manifests)}
        source = self.start()
        self.execute(source, "source", environment=environment)
        build = C.execute_check(self.root, source["run_id"], source["owner_token"], "build",
                                argv=[sys.executable, "-c", "print('versioned build')"], scope=["source.py", *manifests])
        self.approve(source)
        self.finish(source)
        version("1.0.1")
        release = self.start(delivery_from="latest")
        self.assertTrue(self.execute(release, "source", environment={"dependencies": dependency_identity(self.root, manifests)})["reused"])
        rebuilt = C.execute_check(self.root, release["run_id"], release["owner_token"], "build",
                                  argv=[sys.executable, "-c", "print('versioned build')"], scope=["source.py", *manifests], impact_covered=True)
        self.assertFalse(rebuilt["reused"])


if __name__ == "__main__":
    unittest.main()
