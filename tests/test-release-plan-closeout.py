"""Exercise the public cross-process closeout against actual runner reports."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CLI = Path(__file__).resolve().parents[1] / "skills/pipeline/scripts/pipeline_cli.py"
MARKERS = {
    "activation": {"version_marker": ["Activated ", "，"], "target_marker": ["Published ", "；"]},
    "readback": {"version_marker": ["Observed ", ""], "target_marker": ["Target ", ""]},
}
TARGET = "https://www.example.test"
VERSION = "release-123"
ACTIVATION = "release_activation"
IDENTITY = "release_identity"


class ReleasePlanCloseoutTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        (self.root / "PIPELINE.md").write_text("contract")
        (self.root / "activate.py").write_text(
            "from pathlib import Path\n"
            "with Path('activation-count').open('a') as out: out.write('1\\n')\n"
            f"print('Activated {VERSION}，done')\nprint('Published {TARGET}；done')\n")
        self.write_readback()
        (self.root / "source").write_text("inputs")
        self.env = dict(os.environ, RELEASE_FIXTURE_SESSION="initial")
        self.receipt = None
        start = self.cli("start", "--review-risk", "low", "--review-reason", "isolated test")
        self.run_id, self.receipt = start["run_id"], start["receipt"]
        self.cli("policy-ack", "--revision", start["policy"]["revision"])
        self.checks = {
            "prod": self.script_check("activate.py", [ACTIVATION]),
            "live": self.script_check("readback.py", [IDENTITY, "live_feature"]),
            "unit": {"argv": [sys.executable, "-B", "-c", "print('ok')"],
                     "scope": ["source"], "coverage": ["unit"]},
        }
        self.plan = self.save_plan("release")

    def script_check(self, script, coverage):
        return {"argv": [sys.executable, "-B", script], "scope": [script], "coverage": coverage,
                "environment": {"release_target": TARGET}}

    def write_readback(self, version=VERSION, target=TARGET, extra=""):
        (self.root / "readback.py").write_text(
            f"print('Observed {version}')\nprint('Target {target}')\n{extra}")

    def cli(self, action, *args, expected=0):
        ownership = ("--receipt", self.receipt) if self.receipt else ()
        result = subprocess.run([sys.executable, "-B", CLI, action, "--project", self.root,
                                 *ownership, *args], text=True, capture_output=True, env=self.env)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        return payload

    def save_plan(self, name):
        return self.cli("plan", "--name", name, "--json", json.dumps({"checks": self.checks}))["plan"]

    def execute(self, check, name, *, expected=0):
        return self.cli("run", "--plan", self.plan, "--id", check, "--name", name, expected=expected)

    def bind(self, name="historical", *, expected=0, markers=None):
        return self.cli("plan-bind-release", "--plan", self.plan, "--id", "prod", "--readback-id",
                        "live", "--name", name, "--json", json.dumps(markers or MARKERS), expected=expected)

    def qa(self, name, *, expected=0):
        return self.cli("qa", "--plan", self.plan, "--name", name, expected=expected)

    def finish(self, *, expected=0):
        return self.cli("finish", "--direct", "--stopped", "--stop-evidence", "all child processes ended",
                        "--compact", expected=expected)

    def activate_and_readback(self):
        activation = self.execute("prod", "activation")
        self.env["RELEASE_FIXTURE_SESSION"] = "different process environment"
        readback = self.execute("live", "readback")
        return activation, readback

    def counter_is_one(self):
        self.assertEqual((self.root / "activation-count").read_text(), "1\n")

    def test_unbound_successful_activation_cannot_be_replayed_during_closeout(self):
        self.activate_and_readback()
        self.cli("verify", "--plan", self.plan, "--name", "automatic-closeout", expected=1)
        self.counter_is_one()
        self.execute("prod", "accidental-repeat", expected=2)
        self.counter_is_one()
        self.bind()

    def test_readback_can_follow_original_activation_dependency_after_environment_change(self):
        self.checks["live"]["depends_on"] = ["prod"]
        self.plan = self.save_plan("release-with-dependency")
        self.activate_and_readback()
        self.bind()
        self.execute("live", "new-readback")
        self.execute("live", "refresh-after-stale-binding")
        self.bind("refreshed")
        self.counter_is_one()

    def test_close_original_run_from_changed_environment_without_releasing_again(self):
        self.execute("unit", "old-unit")
        activation, _ = self.activate_and_readback()
        self.assertEqual(self.qa("strict-qa", expected=1)["check_failures"], {
            "prod": "command_or_environment_changed", "unit": "command_or_environment_changed"})
        bound = self.bind()
        self.assertEqual(self.execute("prod", "evidence-only")["report"], activation["report"])
        self.assertEqual(self.qa("still-needs-unit", expected=1)["missing_checks"], ["unit"])
        self.execute("unit", "current-unit")
        qa = self.qa("closed-qa")
        self.assertEqual(qa["release_bindings"]["prod"]["path"], bound["release_binding"])
        result = self.finish()
        self.assertTrue(result["delivery_sealed"])
        self.assertEqual(result["status"], "completed")
        self.counter_is_one()

    def test_log_or_binding_change_after_pass_blocks_finish_and_never_replays(self):
        activation, _ = self.activate_and_readback()
        bound = self.bind()
        self.execute("unit", "unit")
        self.qa("pass")
        binding_path = self.root / bound["release_binding"]
        original = binding_path.read_text()
        packet = json.loads(original)
        packet["payload"]["activation"]["id"] = "different-id"
        binding_path.write_text(json.dumps(packet))
        self.assertEqual(self.execute("prod", "do-not-execute", expected=2)["error"], "ValueError")
        self.finish(expected=2)
        binding_path.write_text(original)
        source_binding = self.root / activation["planned_report"]
        original_source = source_binding.read_text()
        source_binding.write_text(original_source + "\n")
        self.finish(expected=2)
        source_binding.write_text(original_source)
        release_log = self.root / json.loads((self.root / activation["report"]).read_text())["payload"]["log_path"]
        release_log.write_text(release_log.read_text() + "changed\n")
        self.finish(expected=2)
        self.counter_is_one()

    def test_both_marker_values_are_unique_exact_and_required(self):
        self.execute("prod", "activation")
        self.env["RELEASE_FIXTURE_SESSION"] = "new"
        cases = [
            ("other-release", TARGET, ""),
            (VERSION, "https://another.example.test", ""),
            (VERSION, TARGET, "print('Observed another')\n"),
            (VERSION, TARGET, "print('Target https://www.example.test')\n"),
            ("", TARGET, ""),
            (VERSION, "", ""),
            (VERSION + " unexpected", TARGET, ""),
        ]
        for i, (version, target, extra) in enumerate(cases):
            with self.subTest(version=version, target=target, extra=extra):
                self.write_readback(version, target, extra)
                self.execute("live", f"readback-{i}")
                self.assertEqual(self.bind(f"bad-{i}", expected=2)["error"], "ValueError")
        self.write_readback()
        self.execute("live", "good-readback")
        for bad_value in (["pre Observed ", ""], ["Observed ", "missing delimiter"],
                          ["Observed ", "\n"], ["", ""]):
            markers = deepcopy(MARKERS)
            markers["readback"]["version_marker"] = bad_value
            self.bind("invalid-declaration", expected=2, markers=markers)
        self.bind("valid")
        self.counter_is_one()

    def test_markers_cannot_turn_a_different_release_or_target_into_a_match(self):
        self.execute("prod", "activation")
        self.env["RELEASE_FIXTURE_SESSION"] = "new"
        self.write_readback(VERSION + "-different")
        self.execute("live", "different-version")
        markers = deepcopy(MARKERS)
        markers["readback"]["version_marker"][1] = "-different"
        self.bind("truncated-version", expected=2, markers=markers)
        self.write_readback("different-" + VERSION)
        self.execute("live", "different-version-prefix")
        markers["readback"]["version_marker"] = ["Observed different-", ""]
        self.bind("truncated-version-prefix", expected=2, markers=markers)
        self.write_readback(VERSION, TARGET + "/preview")
        self.execute("live", "different-target")
        markers = deepcopy(MARKERS)
        markers["readback"]["target_marker"][1] = "/preview"
        self.bind("truncated-target", expected=2, markers=markers)
        for i, suffix in enumerate(("?preview=true", "#preview", ";preview", ",preview", "=preview")):
            self.write_readback(VERSION, TARGET + suffix)
            self.execute("live", f"uri-target-{i}")
            markers["readback"]["target_marker"][1] = suffix
            self.bind(f"trimmed-uri-{i}", expected=2, markers=markers)
            self.bind(f"full-uri-{i}", expected=2)
        self.write_readback()
        self.execute("live", "unmodified-target")
        self.bind("exact-target")
        self.counter_is_one()

    def test_a_new_readback_requires_a_new_binding_and_latest_activation_failure_blocks(self):
        self.activate_and_readback()
        self.bind()
        self.execute("unit", "unit")
        self.qa("pass")
        self.execute("live", "new-readback")
        self.finish(expected=2)
        self.execute("prod", "must-stay-evidence-only", expected=2)
        self.bind("fresh")
        self.qa("pass-after-rebind")
        self.cli("check", "--name", "failed-new-activation", "--scope", "source",
                 "--coverage", ACTIVATION, "extra-label", "--", sys.executable, "-B", "-c",
                 "raise SystemExit(1)", expected=1)
        self.finish(expected=2)
        self.bind("cannot-fall-back", expected=2)
        self.counter_is_one()

    def test_freshness_other_plan_and_bound_manual_reports_cannot_supply_activation(self):
        self.execute("live", "before-activation")
        activation = self.execute("prod", "activation")
        self.bind(expected=2)
        self.execute("live", "fresh-live")
        self.plan = self.save_plan("other-plan")
        self.cli("plan-bind", "--plan", self.plan, "--id", "prod", "--name", "only-bound",
                 "--check", activation["report"])
        self.execute("live", "fresh-in-other-plan")
        self.bind(expected=2)
        self.counter_is_one()

    def test_manual_reports_cannot_manufacture_runner_provenance(self):
        activation, _ = self.activate_and_readback()
        for kind in ("check", "planned_check", "planned_release"):
            self.cli("report", "--kind", kind, "--name", "manual", "--json", "{}", expected=2)
        # Simulate evidence written by the older generic report command. A
        # plausible run-local report and log still lack a runner-owned span.
        report = json.loads((self.root / activation["report"]).read_text())["payload"]
        log_path = self.root / report["log_path"]
        forged_log = log_path.with_name("legacy-manual.log")
        forged_log.write_bytes(log_path.read_bytes())
        report["log_path"] = str(forged_log.relative_to(self.root))
        binding = json.loads((self.root / activation["planned_report"]).read_text())["payload"]
        binding["result"]["report"] = str((self.root / activation["report"])
                                          .with_name("legacy-manual.json").relative_to(self.root))
        code = (
            "from pathlib import Path; import json, run_evidence as E, sys; "
            "receipt=json.loads(Path(sys.argv[1]).read_text()); "
            "args=(Path.cwd(), receipt['run_id'], receipt['owner_token']); "
            "E.write_report(*args, 'check', 'legacy-manual', json.loads(sys.argv[2])); "
            "E.write_report(*args, 'planned_check', 'legacy-manual', json.loads(sys.argv[3]))"
        )
        subprocess.run([sys.executable, "-B", "-c", code, self.receipt, json.dumps(report), json.dumps(binding)],
                       cwd=self.root, env={**self.env, "PYTHONPATH": str(CLI.parent)}, check=True)
        error = self.bind("no-runner", expected=2)
        self.assertIn("no matching completed runner", error["message"])
        self.counter_is_one()

    def retained_fixture(self, *, corrected_audit=False):
        activation = self.root / "activate.py"
        activation.write_text(activation.read_text() +
            "print('[build-stage] source-gates started')\n"
            "print(' ✓ tests/unit/example.test.ts (4 tests) 10ms')\n"
            "print('[build-stage] source-gates 1.0s exit=0')\n")
        active, _ = self.activate_and_readback()
        binding = self.bind()["release_binding"]
        paired = self.cli("check", "--name", "paired", "--scope", "source",
                          "--coverage", "paired_version", "--", sys.executable, "-B", "-c",
                          f"print('public-retail {VERSION}'); print('public-gtm GTM-EXAMPLE 32')")["report"]
        if corrected_audit:
            self.cli("task-start", "--task-id", "initial-audit", "--retrospective",
                     "--review-risk", "high", "--review-reason", "review release evidence",
                     "--goal", "audit metadata", "--accept", "original records")
            audit = ["--", sys.executable, "-B", "-c", "print('original evidence checked')"]
            for name, labels in [
                ("misclassified-audit", [ACTIVATION, IDENTITY]),
                ("corrected-audit", ["historical_" + ACTIVATION, "historical_" + IDENTITY]),
            ]:
                self.cli("check", "--name", name, "--scope", "source", "--intent", "check",
                         "--coverage", *labels, *audit)
            self.cli("task-end", "--task-id", "initial-audit")
        self.cli("task-start", "--task-id", "historical-review", "--retrospective",
                 "--review-risk", "high", "--review-reason", "original engineering acceptance",
                 "--goal", "review release evidence", "--accept", "same-version evidence")
        self.env["RELEASE_FIXTURE_SESSION"] = "later historical review"
        self.retained = {"binding": binding, "test_groups": {"unit": {"tests/unit/example.test.ts": 4}},
                         "paired_readback": {"report": paired, "version_marker": ["public-retail ", ""],
                                             "paired_marker": ["public-gtm GTM-EXAMPLE ", ""],
                                             "paired_version": "32"}}
        return active

    def retain(self, name, *, expected=0):
        return self.cli("retain-release", "--name", name, "--json", json.dumps(self.retained), expected=expected)

    def test_retains_original_tests_and_both_readbacks_from_changed_environment(self):
        self.retained_fixture()
        retained = self.retain("retained")
        self.assertEqual(retained["test_files"], {"tests/unit/example.test.ts": 4})
        self.assertEqual(retained["coverage"], sorted([ACTIVATION, IDENTITY, "live_feature", "paired_version", "unit"]))
        self.checks = {"archive": retained["check"]}
        self.plan = self.save_plan("retrospective")
        self.assertTrue(self.execute("archive", "evidence-only")["reused"])
        qa = self.qa("needs-experts", expected=1)
        self.assertEqual(qa["missing_checks"], [])
        self.assertEqual(qa["check_failures"], {"dual_review": "review_required_but_not_declared"})
        self.counter_is_one()
        source = self.root / "source"
        source.write_text("edited paired readback scope")
        self.assertIn("archive", self.qa("input-changed", expected=1)["check_failures"])
        self.counter_is_one()

    def test_corrected_retrospective_audit_does_not_hide_a_later_activation_failure(self):
        self.retained_fixture(corrected_audit=True)
        self.retain("corrected-history")
        self.cli("check", "--name", "later-activation-failure", "--scope", "source",
                 "--coverage", ACTIVATION, "--", sys.executable, "-B", "-c",
                 "raise SystemExit(1)", expected=1)
        self.assertIn("newer release evidence", self.retain("must-reject", expected=2)["message"])
        self.counter_is_one()

    def test_later_paired_failure_with_a_different_command_blocks_archive(self):
        self.retained_fixture()
        self.retain("first-pair")
        self.cli("check", "--name", "new-pair-failure", "--scope", "readback.py",
                 "--coverage", "paired_version", "--", sys.executable, "-B", "-c",
                 "raise SystemExit(1)", expected=1)
        self.assertIn("newer paired readback", self.retain("superseded-pair", expected=2)["message"])
        self.counter_is_one()

    def test_rejects_unconfirmed_counts_and_mismatched_paired_version(self):
        self.retained_fixture()
        self.retained["test_groups"]["unit"]["tests/unit/example.test.ts"] = 5
        self.assertIn("file/count", self.retain("wrong-count", expected=2)["message"])
        self.retained["test_groups"]["unit"]["tests/unit/example.test.ts"] = 4
        self.retained["paired_readback"]["paired_version"] = "31"
        self.assertIn("version does not match", self.retain("wrong-pair", expected=2)["message"])
        self.counter_is_one()

    def test_readback_env_and_activation_inputs_remain_strict(self):
        self.activate_and_readback()
        self.bind()
        self.env["RELEASE_FIXTURE_SESSION"] = "third"
        self.assertEqual(self.qa("old-readback", expected=1)["check_failures"]["prod"],
                         "historical_release_binding_invalid")
        self.execute("live", "current-readback")
        self.bind("refreshed")
        activation = self.root / "activate.py"
        activation.write_text(activation.read_text() + "# changed\n")
        self.execute("prod", "cannot-repeat", expected=2)
        self.bind("bad-inputs", expected=2)
        self.counter_is_one()


if __name__ == "__main__":
    unittest.main()
