"""Synthetic page observations test handoff logic, not live browser acceptance."""
from pathlib import Path
from copy import deepcopy
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/pipeline/scripts"))
import browser_handoff as B
import run_evidence as E
import run_state as R


class BrowserHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for path, text in (("PIPELINE.md", "fixture scope"), ("browser.py", "browser fixture"), ("sample.json", "{}"), ("browser.log", "observed PASS")):
            (self.root / path).write_text(text)
        self.state = R.start(self.root)
        scope = ["browser.py", "sample.json"]
        self.check = E.record_check(self.root, self.state["run_id"], self.state["owner_token"], "browser",
            command="fixture-browser", scope=scope, environment={"browser":"fixture"}, exit_code=0,
            log_path="browser.log", started_fingerprint=E.fingerprint(self.root, scope), coverage=["browser"])
        self.conditions = {"origin":"https://fixture.example", "path":"/entry", "markers":["form-ready"]}
        self.page = {"url":"https://fixture.example/entry", "markers":{"form-ready":True}}

    def record(self, **kwargs):
        return B.record_recipe(self.root, self.state["run_id"], self.state["owner_token"], "entry",
            script="browser.py", fixtures=["sample.json"], preconditions=self.conditions,
            validated_check=kwargs.get("check", self.check))

    def preflight(self, reference, snapshot=None):
        return B.preflight(self.root, self.state["run_id"], reference, self.page if snapshot is None else snapshot)

    def finish(self):
        E.write_report(self.root, self.state["run_id"], self.state["owner_token"], "qa", "final", {"gate":"PASS", "required_checks":["browser"]})
        R.finish(self.root, self.state["run_id"], self.state["owner_token"], "completed",
                 executors_stopped=True, stop_evidence="fixture has no processes")

    def test_ready_is_only_permission_to_reuse_recipe_not_an_acceptance_pass(self):
        ref = self.record()
        result = self.preflight(ref)
        self.assertTrue(result["ready"])
        self.assertEqual(result["script"], "browser.py")
        self.assertEqual(result["acceptance"], "not_run")
        self.assertLessEqual(result["probe_timeout_ms"], 2000)

    def test_wrong_page_missing_markers_and_unknown_observation_stop_before_long_wait(self):
        ref = self.record()
        for page in ({}, {"url":"https://fixture.example/wrong", "markers":{"form-ready":True}},
                     {"url":self.page["url"], "markers":{}}, {"url":self.page["url"], "markers":{"form-ready":False}},
                     {"url":"https://[invalid", "markers":{}}):
            result = self.preflight(ref, page)
            self.assertFalse(result["ready"])
            self.assertNotIn("script", result)
        self.conditions["version"] = "v2"
        # A different name is required for another immutable recipe.
        other = B.record_recipe(self.root, self.state["run_id"], self.state["owner_token"], "versioned",
            script="browser.py", fixtures=["sample.json"], preconditions=self.conditions, validated_check=self.check)
        self.assertIn("version_mismatch_or_unknown", self.preflight(other)["reasons"])

    def test_recipe_and_original_fingerprints_survive_three_delivery_stages(self):
        ref = self.record()
        self.finish()
        self.state = R.start(self.root, {"delivery_from":"latest"})
        self.assertTrue(self.preflight(ref)["ready"])
        self.finish()
        self.state = R.start(self.root, {"delivery_from":"latest"})
        self.assertTrue(self.preflight(ref)["ready"])
        (self.root / "sample.json").write_text('{"changed":true}')
        self.assertFalse(self.preflight(ref)["ready"])

    def test_unrelated_run_does_not_silently_adopt_old_recipe(self):
        ref = self.record()
        self.finish()
        self.state = R.start(self.root)
        self.assertFalse(self.preflight(ref)["ready"])

    def test_changed_missing_or_tampered_assets_reject_reuse(self):
        ref = self.record()
        (self.root / "browser.py").write_text("changed script")
        self.assertFalse(self.preflight(ref)["ready"])
        (self.root / "browser.py").write_text("browser fixture")
        (self.root / self.check).write_text("{}")
        self.assertFalse(self.preflight(ref)["ready"])

    def test_lost_original_log_and_non_browser_pass_cannot_certify_recipe(self):
        ref = self.record()
        (self.root / "browser.log").write_text("modified")
        self.assertFalse(self.preflight(ref)["ready"])
        scope = ["browser.py", "sample.json"]
        metadata = E.record_check(self.root, self.state["run_id"], self.state["owner_token"], "metadata",
            command="fixture-metadata", scope=scope, environment={"tool":"fixture"}, exit_code=0,
            log_path="browser.log", started_fingerprint=E.fingerprint(self.root, scope), coverage=["metadata"])
        with self.assertRaisesRegex(ValueError, "browser evidence"):
            self.record(check=metadata)

    def test_recipe_requires_observed_browser_evidence_and_covered_assets(self):
        (self.root / "outside.json").write_text("{}")
        with self.assertRaises(ValueError):
            B.record_recipe(self.root, self.state["run_id"], self.state["owner_token"], "uncovered",
                script="browser.py", fixtures=["outside.json"], preconditions=self.conditions, validated_check=self.check)
        for condition in ({**self.conditions, "origin":"https://user:secret@fixture.example"},
                          {**self.conditions, "markers":[]}, {**self.conditions, "path":"/entry?token=secret"}):
            with self.assertRaises(ValueError):
                B.record_recipe(self.root, self.state["run_id"], self.state["owner_token"], "invalid",
                    script="browser.py", fixtures=[], preconditions=condition, validated_check=self.check)

    def test_preflight_is_read_only_and_never_stores_page_query_secrets(self):
        ref = self.record()
        before = {p:p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        page = {**self.page, "url":self.page["url"]+"?session=ephemeral-secret"}
        self.assertTrue(self.preflight(ref, page)["ready"])
        after = {p:p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(all(b"ephemeral-secret" not in data for data in after.values()))


class FirstProbeTests(unittest.TestCase):
    def setUp(self):
        self.conditions = {"origin":"http://127.0.0.1:5173", "path":"/review", "markers":["target-visible"]}
        self.page = {"url":"http://127.0.0.1:5173/review", "markers":{"target-visible":True}}

    def test_first_time_probe_needs_no_recipe_or_run_and_cannot_produce_acceptance(self):
        result = B.probe(self.conditions, self.page)
        self.assertTrue(result["ready"])
        self.assertEqual(result["acceptance"], "not_run")
        self.assertNotIn("script", result)
        self.assertNotIn("report", result)
        self.assertEqual(result["probe_timeout_ms"], 1500)

    def test_missing_or_unknown_target_stops_before_any_click_is_proposed(self):
        for markers in ({}, {"target-visible":False}, {"target-visible":None}):
            result = B.probe(self.conditions, {**self.page, "markers":markers})
            self.assertFalse(result["ready"])
            self.assertIn("required_marker_missing_or_unknown", result["reasons"])
        self.assertFalse(B.probe(self.conditions, {**self.page, "url":"http://127.0.0.1:5173/wrong"})["ready"])

    def test_bad_conditions_and_observations_are_not_treated_as_ready(self):
        for conditions in (None, {**self.conditions,"origin":17}, {**self.conditions,"markers":[]}):
            self.assertEqual(B.probe(conditions,self.page)["reasons"],["page_conditions_invalid"])
        self.assertEqual(B.probe(self.conditions,{})["reasons"],["page_observation_missing"])
        self.assertEqual(B.probe(self.conditions,{"url":"https://[invalid"})["reasons"],["page_observation_invalid"])

    def test_probe_does_not_mutate_inputs_or_return_private_url_query(self):
        page = {**self.page,"url":self.page["url"]+"?session=ephemeral-secret"}
        before = deepcopy((self.conditions,page))
        result = B.probe(self.conditions,page)
        self.assertTrue(result["ready"])
        self.assertEqual(before,(self.conditions,page))
        self.assertNotIn("ephemeral-secret",str(result))


if __name__ == "__main__":
    unittest.main()
